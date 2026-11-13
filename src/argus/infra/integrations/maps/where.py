from argus.kernel.tools import Speaker, ToolContext, ToolFailure
from argus.situation.coordinates import Gcj02Point, wgs84_to_gcj02
from argus.situation.places import HOME_PLACE_NAME, PlaceStore

USER_LOCATION = "用户现在的位置（手机定位）"
HOME_LOCATION = "家（手机最近没报位置，用的是配置里的家坐标）"
CONFIGURED_HOME_LOCATION = "家（配置里的家坐标）"


def where_the_user_is(
    ctx: ToolContext, *, home_latitude: float, home_longitude: float
) -> tuple[Gcj02Point, str]:
    fix = ctx.precise_location
    if fix is None:
        return wgs84_to_gcj02(lat=home_latitude, lng=home_longitude), HOME_LOCATION
    where = CONFIGURED_HOME_LOCATION if ctx.speaker is Speaker.OTHER else USER_LOCATION
    return wgs84_to_gcj02(lat=fix.lat, lng=fix.lng), where


async def recorded_place(
    name: str,
    ctx: ToolContext,
    places: PlaceStore,
    *,
    home_latitude: float,
    home_longitude: float,
    refusal: str,
) -> tuple[Gcj02Point, str]:
    if ctx.speaker is not Speaker.OWNER:
        raise ToolFailure(refusal, user_action=None)
    recorded = await places.all()
    for place in recorded:
        if place.name == name:
            center = wgs84_to_gcj02(lat=place.center.lat, lng=place.center.lng)
            return center, f"用户录过的地点「{name}」"
    if name == HOME_PLACE_NAME:
        return wgs84_to_gcj02(lat=home_latitude, lng=home_longitude), CONFIGURED_HOME_LOCATION
    known = "、".join(place.name for place in recorded) or "一个都没有"
    raise ToolFailure(
        f"没录过叫「{name}」的地点；录过的有：{known}",
        user_action='想用那里的话，人到了那里说"把这里记成……"录下来',
    )


def distance_text(metres: int) -> str:
    if metres < 1000:
        return f"{metres} 米"
    return f"{round(metres / 1000, 1):g} 公里"
