from datetime import date

from pydantic import BaseModel, ConfigDict

from argus.infra.integrations.weather.qweather import (
    FORECAST_DAYS,
    CurrentWeather,
    DailyForecast,
    HalfDayForecast,
    QWeatherClient,
    WeatherReport,
)
from argus.kernel.tools import Permission, Speaker, ToolContext, ToolFn, ToolResult, tool

USER_LOCATION_LINE = "地点：用户现在的位置（手机定位）"
HOME_LOCATION_LINE = "地点：家（手机最近没报位置，用的是配置里的家坐标）"
OTHER_HOME_LOCATION_LINE = "地点：家（配置里的家坐标）"

_NEAR_DAY_NAMES = ("今天", "明天", "后天")
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class WeatherArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def weather_tool(client: QWeatherClient, *, home_latitude: float, home_longitude: float) -> ToolFn:
    @tool(
        "weather.get",
        Permission.L1,
        params=WeatherArgs,
        timeout_s=5,
        personal=False,
        description=(
            f"查用户所在地的天气：现在的天气，加今天起 {FORECAST_DAYS} 天的预报。"
            "手机最近报了位置就查那里，没报就查家里；查不了指定的城市"
        ),
    )
    async def get_weather(args: WeatherArgs, ctx: ToolContext) -> ToolResult:
        fix = ctx.precise_location
        if fix is None:
            report = await client.forecast(home_latitude, home_longitude)
            location_line = HOME_LOCATION_LINE
        else:
            report = await client.forecast(fix.lat, fix.lng)
            location_line = (
                OTHER_HOME_LOCATION_LINE if ctx.speaker is Speaker.OTHER else USER_LOCATION_LINE
            )
        text = describe(report, location_line=location_line)
        return ToolResult(text, fields=spoken_fields(report))

    return get_weather


def describe(report: WeatherReport, *, location_line: str) -> str:
    if len(report.days) != FORECAST_DAYS:
        raise ValueError(f"和风给了 {len(report.days)} 天的预报，要的是 {FORECAST_DAYS} 天")
    lines = [location_line, f"现在：{_describe_current(report.current)}"]
    for days_from_today, forecast in enumerate(report.days):
        lines.append(f"{_day_label(days_from_today, forecast.day)}：{_describe_day(forecast)}")
    return "\n".join(lines)


def spoken_fields(report: WeatherReport) -> dict[str, str]:
    current = report.current
    return {
        "now": f"{current.condition}，{round(current.temperature_c)} 度",
        "today": _spoken_day(report.days[0]),
        "tomorrow": _spoken_day(report.days[1]),
    }


def _spoken_day(forecast: DailyForecast) -> str:
    daytime, nighttime = forecast.daytime, forecast.nighttime
    if daytime.condition == nighttime.condition:
        condition = daytime.condition
    else:
        condition = f"{daytime.condition}转{nighttime.condition}"
    rain = max(daytime.precipitation_probability, nighttime.precipitation_probability)
    low, high = round(forecast.temperature_min_c), round(forecast.temperature_max_c)
    return f"{condition}，{low} 到 {high} 度，降水概率 {rain:.0%}"


def _describe_current(current: CurrentWeather) -> str:
    return (
        f"{current.condition}，{round(current.temperature_c)} 度，"
        f"体感 {round(current.feels_like_c)} 度，风力 {current.wind_scale} 级"
    )


def _day_label(days_from_today: int, day: date) -> str:
    if days_from_today < len(_NEAR_DAY_NAMES):
        return f"{_NEAR_DAY_NAMES[days_from_today]}（{_describe_date(day)}）"
    return _describe_date(day)


def _describe_date(day: date) -> str:
    return f"{day.month}月{day.day}日，{_WEEKDAY_NAMES[day.weekday()]}"


def _describe_day(forecast: DailyForecast) -> str:
    return (
        f"最高 {round(forecast.temperature_max_c)} 度，最低 {round(forecast.temperature_min_c)} 度；"
        f"白天{_describe_half_day(forecast.daytime)}；夜里{_describe_half_day(forecast.nighttime)}"
    )


def _describe_half_day(half_day: HalfDayForecast) -> str:
    return f"{half_day.condition}，降水概率 {half_day.precipitation_probability:.0%}"
