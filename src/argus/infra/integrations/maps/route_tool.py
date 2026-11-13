import asyncio
from dataclasses import dataclass
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from argus.infra.integrations.maps.amap import AmapClient, Route, Traffic, TravelMode
from argus.infra.integrations.maps.where import distance_text, recorded_place, where_the_user_is
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool
from argus.llm.redaction import Placeholders, Redactor
from argus.situation.coordinates import Gcj02Point, Wgs84Point, distance_m
from argus.situation.places import PlaceStore

_MOST_PLACE_NAME_CHARS = 20
_MOST_DESTINATION_CHARS = 40
_FIND_RADIUS_M = 50_000
_WALKING_WORTH_IT_M = 3000
_TIMEOUT_S = 15
_REFUSAL = "录过的地点只有主人本人能用；起点不填就是用户现在的位置，终点可以说店名、地标或地址"

_MODE_NAMES = {
    TravelMode.DRIVING: "开车",
    TravelMode.TRANSIT: "公交地铁",
    TravelMode.WALKING: "步行",
    TravelMode.RIDING: "骑车",
}
_DEFAULT_MODES = (TravelMode.DRIVING, TravelMode.TRANSIT)

type _PlaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_MOST_PLACE_NAME_CHARS)
]


class RouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_place: _PlaceName | None = Field(
        default=None, description="终点是用户录过的地点时填地点名，如公司、家"
    )
    to_name: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, min_length=1, max_length=_MOST_DESTINATION_CHARS
            ),
        ]
        | None
    ) = Field(
        default=None,
        description="终点是店、地标或地址时填它的名字，如北京西站；之前说过的店用你说过的全名。和 to_place 二选一",
    )
    to_near: _PlaceName | None = Field(
        default=None,
        description="to_name 那家店是在用户录过的哪个地点附近搜到的，就填那个地点名，如公司；不填在起点附近找",
    )
    from_place: _PlaceName | None = Field(
        default=None,
        description="从用户录过的地点出发时填，如从公司回家填公司；不填就是用户现在的位置",
    )
    mode: TravelMode | None = Field(
        default=None,
        description=(
            "用户说了怎么去才填：driving 开车、transit 公交地铁、walking 走路、riding 骑车；"
            "没说就不填，开车、公交都算，近的还算步行"
        ),
    )

    @model_validator(mode="after")
    def _one_destination(self) -> Self:
        if (self.to_place is None) == (self.to_name is None):
            raise ValueError("终点填 to_place 或 to_name，只填一个")
        if self.to_near is not None and self.to_name is None:
            raise ValueError("to_near 只和 to_name 一起用")
        return self


@dataclass(frozen=True)
class _Destination:
    point: Gcj02Point
    label: str
    city_code: str | None


def place_route_tool(
    client: AmapClient,
    redactor: Redactor,
    places: PlaceStore,
    *,
    home_latitude: float,
    home_longitude: float,
) -> ToolFn:
    @tool(
        "place.route",
        Permission.L1,
        params=RouteArgs,
        timeout_s=_TIMEOUT_S,
        personal=False,
        sensitive=("to_place", "to_near", "from_place"),
        description=(
            "算去某个地方要多久、多远：开车、公交地铁、步行、骑车。"
            '用户问"过去要多久""从公司回家要多久""坐地铁去北京西站要多久"时用；'
            '问"路上堵不堵"也用它，开车那行有现在的路况。'
            "终点是用户录过的地点填 to_place；是店、地标、地址填 to_name，"
            "刚才说过的店用你说过的全名，它是在哪个录过的地点附近搜到的就把那个地点填进 to_near。"
            "起点默认用户现在的位置（手机报了就用手机的，没报就是家），从录过的地点出发填 from_place。"
            "用户说了怎么去才填 mode；没说就不填，结果里的几种都说，按给的先后说。"
            "名字只写地方本身，不要带用户的名字、手机号"
        ),
    )
    async def place_route(args: RouteArgs, ctx: ToolContext) -> ToolResult:
        if (
            args.to_name is not None
            and redactor.redact(args.to_name, Placeholders()) != args.to_name
        ):
            raise ToolFailure(
                "终点的名字里带着名字、手机号、地址这类个人信息，不能发给地图服务", user_action=None
            )

        async def recorded(name: str) -> tuple[Gcj02Point, str]:
            return await recorded_place(
                name,
                ctx,
                places,
                home_latitude=home_latitude,
                home_longitude=home_longitude,
                refusal=_REFUSAL,
            )

        if args.from_place is not None:
            origin, origin_label = await recorded(args.from_place)
        else:
            origin, origin_label = where_the_user_is(
                ctx, home_latitude=home_latitude, home_longitude=home_longitude
            )
        if args.to_place is not None:
            point, label = await recorded(args.to_place)
            destination = _Destination(point, label, city_code=None)
        else:
            assert args.to_name is not None
            destination = await _find_destination(
                client,
                args.to_name,
                near=await recorded(args.to_near) if args.to_near is not None else None,
                origin=origin,
            )

        modes = _modes_for(args.mode, origin, destination.point)
        city_codes = (
            await _city_codes(client, origin, destination) if TravelMode.TRANSIT in modes else None
        )
        routes = await asyncio.gather(
            *(_route_or_none(client, origin, destination.point, mode, city_codes) for mode in modes)
        )
        return ToolResult(
            describe(origin_label, destination.label, list(zip(modes, routes, strict=True)))
        )

    return place_route


async def _find_destination(
    client: AmapClient,
    name: str,
    *,
    near: tuple[Gcj02Point, str] | None,
    origin: Gcj02Point,
) -> _Destination:
    center, area = near if near is not None else (origin, "起点")
    found = await client.find(center, name=name, radius_m=_FIND_RADIUS_M)
    if found is None:
        raise ToolFailure(
            f"在{area}附近 {distance_text(_FIND_RADIUS_M)}内没找到「{name}」", user_action=None
        )
    address = f"，地址：{found.address}" if found.address else ""
    label = f"「{found.name}」（按「{name}」在{area}附近找到的第一个{address}）"
    return _Destination(found.point, label, found.city_code)


def _modes_for(
    mode: TravelMode | None, origin: Gcj02Point, destination: Gcj02Point
) -> list[TravelMode]:
    if mode is not None:
        return [mode]
    straight_m = distance_m(
        Wgs84Point(lat=origin.lat, lng=origin.lng),
        Wgs84Point(lat=destination.lat, lng=destination.lng),
    )
    walking = [TravelMode.WALKING] if straight_m < _WALKING_WORTH_IT_M else []
    return [*_DEFAULT_MODES, *walking]


async def _city_codes(
    client: AmapClient, origin: Gcj02Point, destination: _Destination
) -> tuple[str, str] | None:
    if destination.city_code is not None:
        origin_code = await client.city_code(origin)
        destination_code: str | None = destination.city_code
    else:
        origin_code, destination_code = await asyncio.gather(
            client.city_code(origin), client.city_code(destination.point)
        )
    if origin_code is None or destination_code is None:
        return None
    return origin_code, destination_code


async def _route_or_none(
    client: AmapClient,
    origin: Gcj02Point,
    destination: Gcj02Point,
    mode: TravelMode,
    city_codes: tuple[str, str] | None,
) -> Route | None:
    if mode is TravelMode.TRANSIT and city_codes is None:
        return None
    return await client.route(origin, destination, mode, city_codes=city_codes)


def describe(origin: str, destination: str, routes: list[tuple[TravelMode, Route | None]]) -> str:
    lines = [f"从{origin}到{destination}，时间是高德估的："]
    for mode, route in routes:
        lines.append(
            _describe_route(route) if route is not None else f"{_MODE_NAMES[mode]}：没找到路线"
        )
    return "\n".join(lines)


def _describe_route(route: Route) -> str:
    duration = _duration_text(route.duration_s)
    if route.mode is TravelMode.DRIVING:
        duration += "（按现在的路况）"
    parts = [f"{_MODE_NAMES[route.mode]}{duration}", distance_text(route.distance_m)]
    if route.taxi_cost_yuan is not None:
        parts.append(f"打车约 {route.taxi_cost_yuan:g} 元")
    if route.tolls_yuan:
        parts.append(f"过路费 {route.tolls_yuan:g} 元")
    if route.lines:
        transfers = len(route.lines) - 1
        ride = "坐 " + " 换 ".join(route.lines)
        parts.append(f"{ride}（换乘 {transfers} 次）" if transfers else ride)
    if route.walking_m:
        parts.append(f"要走 {distance_text(route.walking_m)}")
    if route.traffic is not None:
        parts.append(_traffic_text(route.traffic))
    return "，".join(parts)


def _traffic_text(traffic: Traffic) -> str:
    jams = []
    if traffic.jammed_m:
        jams.append(f"拥堵 {distance_text(traffic.jammed_m)}")
    if traffic.slow_m:
        jams.append(f"缓行 {distance_text(traffic.slow_m)}")
    if not jams:
        return "没有堵的路段"
    return "路上" + "、".join(jams)


def _duration_text(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"约 {minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"约 {hours} 小时" + (f" {minutes} 分钟" if minutes else "")
