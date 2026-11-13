from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from argus.kernel.events import WeatherAlert
from argus.proactive.triggers import Candidate, trigger

_BLUE = "minor"


class WeatherAlertParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@trigger(on=WeatherAlert, id="weather_alert", params=WeatherAlertParams)
def weather_alert(event: WeatherAlert, params: WeatherAlertParams) -> Candidate | None:
    if event.severity == _BLUE:
        return None
    return Candidate(
        trigger_id="weather_alert",
        text=f"{event.headline}\n{event.description}",
        value=6,
        urgency=6,
        ttl=timedelta(hours=1),
        dedupe_key=event.alert_id,
        personal=False,
    )
