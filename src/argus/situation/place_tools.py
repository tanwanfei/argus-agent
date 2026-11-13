from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from argus.kernel.clock import Clock
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool
from argus.situation.coordinates import Wgs84Point
from argus.situation.places import (
    DEFAULT_RADIUS_M,
    MAX_RADIUS_M,
    MIN_RADIUS_M,
    Place,
    PlaceBook,
)


class SavePlaceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description='用户叫它的名字，如"健身房"、"公司"、"爸妈家"')
    radius_m: float | None = Field(
        default=None,
        description=(
            f"多少米以内算在这里，{MIN_RADIUS_M:g}～{MAX_RADIUS_M:g}；"
            f"不写用 {DEFAULT_RADIUS_M:g}，用户没提就别填"
        ),
    )


class RestorePlaceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    previous: dict[str, Any] | None


def place_tools(places: PlaceBook, clock: Clock) -> tuple[ToolFn, ToolFn]:
    @tool(
        "place.save",
        Permission.L2,
        params=SavePlaceArgs,
        personal=True,
        sensitive=("name",),
        description=(
            '把用户现在所在的位置记成一个常去的地点。用户说"把这里记成健身房""这里是公司"时用。'
            "要用手机报来的位置，手机没连着 App 就记不了。同名的会被改成现在这个位置"
        ),
    )
    async def save_place(args: SavePlaceArgs, ctx: ToolContext) -> ToolResult:
        fix = ctx.precise_location
        if fix is None:
            raise ToolFailure(
                "不知道用户现在在哪：手机没有报位置",
                user_action="打开 ARGUS App 并允许定位，等一会儿再说一次",
            )
        radius_m = DEFAULT_RADIUS_M if args.radius_m is None else args.radius_m
        if fix.accuracy_m > radius_m:
            raise ToolFailure(
                f"手机现在的定位误差约 {fix.accuracy_m:.0f} 米，比半径 {radius_m:g} 米还大，记下来不准",
                user_action="走到室外或等定位准一点再说一次",
            )
        try:
            place = Place(
                name=args.name.strip(),
                center=Wgs84Point(lat=fix.lat, lng=fix.lng),
                radius_m=radius_m,
                updated_at=clock.now(),
            )
        except ValueError as error:
            raise ToolFailure(f"没记成：{error}", user_action=None) from error
        previous = next((old for old in await places.all() if old.name == place.name), None)
        await places.save(place)
        text = f"记下了：{place.name}，半径 {place.radius_m:g} 米"
        if previous is not None:
            text += "（原来的位置被换掉了）"
        return ToolResult(text, undo_args={"name": place.name, "previous": _dump(previous)})

    @tool(
        "place.restore",
        Permission.L2,
        params=RestorePlaceArgs,
        personal=True,
        sensitive=("name", "previous"),
        description="撤销刚录的地点：原来没有的删掉，原来有的换回去（撤销用，不给模型）",
    )
    async def restore_place(args: RestorePlaceArgs, ctx: ToolContext) -> ToolResult:
        if args.previous is None:
            await places.remove(args.name)
            return ToolResult("那个地点删掉了")
        await places.save(_load(args.name, args.previous))
        return ToolResult("那个地点换回原来的了")

    return save_place, restore_place


def _dump(place: Place | None) -> dict[str, Any] | None:
    if place is None:
        return None
    return {
        "lat": place.center.lat,
        "lng": place.center.lng,
        "radius_m": place.radius_m,
        "updated_at": place.updated_at.isoformat(),
    }


def _load(name: str, saved: dict[str, Any]) -> Place:
    return Place(
        name=name,
        center=Wgs84Point(lat=saved["lat"], lng=saved["lng"]),
        radius_m=saved["radius_m"],
        updated_at=datetime.fromisoformat(saved["updated_at"]),
    )
