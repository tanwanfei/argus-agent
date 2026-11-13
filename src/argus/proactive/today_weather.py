from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from argus.kernel.events import TodayForecast
from argus.proactive.triggers import Candidate, trigger

_WET = ("雨", "雪")


class TodayWeatherParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cooling_degrees: int = Field(default=5, gt=0)


@trigger(on=TodayForecast, id="today_weather", params=TodayWeatherParams)
def today_weather(event: TodayForecast, params: TodayWeatherParams) -> Candidate | None:
    facts: list[str] = []
    advice: list[str] = []
    wet = [
        f"{when}有{condition}（降水概率 {probability:.0%}）"
        for when, condition, probability in (
            ("白天", event.daytime_condition, event.daytime_precipitation_probability),
            ("夜里", event.nighttime_condition, event.nighttime_precipitation_probability),
        )
        if any(word in condition for word in _WET)
    ]
    if wet:
        facts.append("，".join(wet))
        advice.append("出门带伞")
    if event.yesterday_max_c is not None:
        today_max = round(event.temperature_max_c)
        colder_by = round(event.yesterday_max_c) - today_max
        if colder_by >= params.cooling_degrees:
            facts.append(f"最高 {today_max} 度，比昨天低 {colder_by} 度")
            advice.append("多穿点")
    if not facts:
        return None
    return Candidate(
        trigger_id="today_weather",
        text=f"今天{'；'.join(facts)}，{'、'.join(advice)}。",
        value=4,
        urgency=3,
        ttl=timedelta(hours=12),
        dedupe_key=event.day.isoformat(),
        personal=False,
    )
