import logging
from datetime import datetime, time, timedelta
from typing import Any, Protocol

from argus.infra.integrations.weather.qweather import WeatherReport
from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, TodayForecast
from argus.scheduler.scheduler import Scheduler
from argus.scheduler.store import Job

logger = logging.getLogger(__name__)

MORNING_FORECAST_KIND = "weather.morning_forecast"

_CHECK_AT = time(6, 50)
_VALID_FOR = timedelta(minutes=10)


class DailyForecasts(Protocol):
    async def forecast(self, latitude: float, longitude: float) -> WeatherReport: ...


class MorningForecastCheck:
    def __init__(
        self,
        weather: DailyForecasts,
        scheduler: Scheduler,
        bus: EventBus,
        clock: Clock,
        *,
        latitude: float,
        longitude: float,
    ) -> None:
        self._weather = weather
        self._scheduler = scheduler
        self._bus = bus
        self._clock = clock
        self._latitude = latitude
        self._longitude = longitude

    async def schedule_first(self) -> None:
        try:
            if await self._scheduler.pending(MORNING_FORECAST_KIND):
                return
            await self._schedule_after(self._clock.now(), {})
        except Exception:
            logger.exception("开机时没排上早上查预报")

    async def check(self, job: Job) -> None:
        report = await self._weather.forecast(self._latitude, self._longitude)
        today = self._clock.now().date()
        forecast = next((day for day in report.days if day.day == today), None)
        if forecast is None:
            logger.warning("和风的预报里没有今天，今天不提醒下雨、降温")
            await self._schedule_after(job.due_at, {})
            return
        await self._schedule_after(job.due_at, {"max_c": forecast.temperature_max_c})
        self._bus.publish(
            TodayForecast(
                day=today,
                daytime_condition=forecast.daytime.condition,
                daytime_precipitation_probability=forecast.daytime.precipitation_probability,
                nighttime_condition=forecast.nighttime.condition,
                nighttime_precipitation_probability=forecast.nighttime.precipitation_probability,
                temperature_max_c=forecast.temperature_max_c,
                yesterday_max_c=job.payload.get("max_c"),
                ts=self._clock.now(),
            )
        )

    async def missed(self, job: Job) -> None:
        logger.info("今天早上没查成预报，不提醒下雨、降温了")
        await self._schedule_after(job.due_at, {})

    async def _schedule_after(self, after: datetime, payload: dict[str, Any]) -> None:
        due_at = _next_check_at(after)
        await self._scheduler.schedule(
            MORNING_FORECAST_KIND,
            due_at,
            payload,
            key=f"{MORNING_FORECAST_KIND}@{due_at.isoformat()}",
            ttl=_VALID_FOR,
        )


def _next_check_at(after: datetime) -> datetime:
    today_at = datetime.combine(after.date(), _CHECK_AT, tzinfo=after.tzinfo)
    return today_at if today_at > after else today_at + timedelta(days=1)
