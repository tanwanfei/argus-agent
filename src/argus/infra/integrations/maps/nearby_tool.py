from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from argus.infra.integrations.maps.amap import AmapClient, NearbyPlace
from argus.infra.integrations.maps.where import distance_text, recorded_place, where_the_user_is
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool
from argus.llm.redaction import Placeholders, Redactor
from argus.situation.places import PlaceStore

_MOST_KEYWORD_CHARS = 20
_DEFAULT_RADIUS_M = 1000
_SMALLEST_RADIUS_M = 100
_LARGEST_RADIUS_M = 50_000
_TIMEOUT_S = 8
_REFUSAL = "录过的地点只有主人本人能查附近；不填 near 搜的就是家附近"


class NearbyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_MOST_KEYWORD_CHARS)
    ] = Field(description="找什么：一类店（川菜、咖啡、药店、加油站）或一家店的名字")
    radius_m: int = Field(
        default=_DEFAULT_RADIUS_M,
        ge=_SMALLEST_RADIUS_M,
        le=_LARGEST_RADIUS_M,
        description="搜多远，单位米，默认 1000；按名字找之前说过的某一家时给 50000",
    )
    near: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=_MOST_KEYWORD_CHARS),
        ]
        | None
    ) = Field(
        default=None,
        description="在用户录过的哪个地点附近搜，填地点名，如公司、家；搜用户现在所在的地方就不填",
    )


def place_nearby_tool(
    client: AmapClient,
    redactor: Redactor,
    places: PlaceStore,
    *,
    home_latitude: float,
    home_longitude: float,
) -> ToolFn:
    @tool(
        "place.nearby",
        Permission.L1,
        params=NearbyArgs,
        timeout_s=_TIMEOUT_S,
        personal=False,
        sensitive=("near",),
        description=(
            "搜用户附近的店和地方：吃的、咖啡、药店、超市、加油站、停车场……，也能按名字找某一家。"
            "手机最近报了位置就搜那里，没报就搜家附近；"
            "用户问他录过的地点（公司、家……）附近时，near 填地点名；搜不了别的城市、地址。"
            "每家给距离、评分（满分 5）、人均、今天的营业时间和地址，评分、人均只有吃的、酒店、景点、影院有。"
            "推荐时挑两三家，说店名、多远、评分或人均，地址等用户问再说。"
            "关键词只写店的种类或店名，不要带用户的名字、手机号、地址"
        ),
    )
    async def place_nearby(args: NearbyArgs, ctx: ToolContext) -> ToolResult:
        if redactor.redact(args.keyword, Placeholders()) != args.keyword:
            raise ToolFailure(
                "关键词里带着名字、手机号、地址这类个人信息，不能发给地图服务", user_action=None
            )
        if args.near is not None:
            center, where = await recorded_place(
                args.near,
                ctx,
                places,
                home_latitude=home_latitude,
                home_longitude=home_longitude,
                refusal=_REFUSAL,
            )
        else:
            center, where = where_the_user_is(
                ctx, home_latitude=home_latitude, home_longitude=home_longitude
            )
        found = await client.around(center, keyword=args.keyword, radius_m=args.radius_m)
        return ToolResult(
            describe(found, keyword=args.keyword, radius_m=args.radius_m, where=where)
        )

    return place_nearby


def describe(places: list[NearbyPlace], *, keyword: str, radius_m: int, where: str) -> str:
    area = f"在{where}周围 {distance_text(radius_m)}内"
    if not places:
        return f"{area}没搜到跟「{keyword}」有关的地方"
    lines = [
        f"{area}搜「{keyword}」，按高德的综合排序，前 {len(places)} 家（距离会差一百米上下）："
    ]
    lines += [f"{number}. {_describe_place(place)}" for number, place in enumerate(places, start=1)]
    return "\n".join(lines)


def _describe_place(place: NearbyPlace) -> str:
    parts = [f"{place.name}（{place.kind}）" if place.kind else place.name]
    if place.distance_m is not None:
        parts.append(distance_text(place.distance_m))
    if place.rating is not None:
        parts.append(f"评分 {place.rating:g}")
    if place.cost_yuan is not None:
        parts.append(f"人均 {place.cost_yuan:g} 元")
    if place.open_today is not None:
        parts.append(f"今天营业 {place.open_today}")
    if place.address:
        parts.append(f"地址：{place.address}")
    return "，".join(parts)
