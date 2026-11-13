from typing import Protocol

from argus.infra.integrations.weather.qweather import QWeatherAlert
from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, WeatherAlert
from argus.kernel.tools import ToolFailure

_CHECK_EVERY_SECONDS = 15 * 60
_CANCEL = "cancel"


class CurrentAlerts(Protocol):
    async def alerts(self, latitude: float, longitude: float) -> list[QWeatherAlert]: ...


class WeatherAlertWatch:
    def __init__(
        self,
        weather: CurrentAlerts,
        clock: Clock,
        bus: EventBus,
        *,
        latitude: float,
        longitude: float,
    ) -> None:
        self._weather = weather
        self._clock = clock
        self._bus = bus
        self._latitude = latitude
        self._longitude = longitude
        self._published: set[str] = set()

    async def run(self) -> None:
        while True:
            await self.check_once()
            await self._clock.sleep(_CHECK_EVERY_SECONDS)

    async def check_once(self) -> None:
        try:
            alerts = await self._weather.alerts(self._latitude, self._longitude)
        except ToolFailure:
            return
        now = self._clock.now()
        current = [alert for alert in alerts if alert.message_type != _CANCEL]
        for alert in current:
            if alert.alert_id in self._published:
                continue
            self._bus.publish(
                WeatherAlert(
                    alert_id=alert.alert_id,
                    headline=alert.headline,
                    description=alert.description,
                    severity=alert.severity,
                    expires_at=alert.expires_at,
                    ts=now,
                )
            )
        self._published = {alert.alert_id for alert in current}
