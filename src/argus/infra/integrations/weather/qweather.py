import base64
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import httpx2
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.hazmat.primitives.serialization import load_der_private_key
from pydantic import BaseModel, Field, ValidationError

from argus.kernel.clock import Clock
from argus.kernel.tools import ToolFailure

logger = logging.getLogger(__name__)

FORECAST_DAYS = 10

_ISSUED_AT_BACKDATE_S = 30
_TOKEN_LIFETIME_S = 15 * 60
_TIMEOUT = httpx2.Timeout(5.0)


@dataclass(frozen=True)
class QWeatherCredentials:
    api_host: str
    developer_id: str
    project_id: str
    key_id: str
    private_key: str = field(repr=False)


@dataclass(frozen=True)
class CurrentWeather:
    condition: str
    temperature_c: float
    feels_like_c: float
    wind_scale: int


@dataclass(frozen=True)
class HalfDayForecast:
    condition: str
    precipitation_probability: float


@dataclass(frozen=True)
class DailyForecast:
    day: date
    temperature_max_c: float
    temperature_min_c: float
    daytime: HalfDayForecast
    nighttime: HalfDayForecast


@dataclass(frozen=True)
class WeatherReport:
    current: CurrentWeather
    days: tuple[DailyForecast, ...]


@dataclass(frozen=True)
class QWeatherAlert:
    alert_id: str
    message_type: str
    headline: str
    description: str
    severity: str
    expires_at: datetime


class QWeatherClient:
    def __init__(
        self,
        credentials: QWeatherCredentials,
        *,
        clock: Clock,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._signing_key = load_private_key(credentials.private_key)
        self._clock = clock
        self._client = httpx2.AsyncClient(
            base_url=f"https://{credentials.api_host}",
            trust_env=False,
            timeout=_TIMEOUT,
            transport=transport,
        )

    async def forecast(self, latitude: float, longitude: float) -> WeatherReport:
        coordinates = f"{latitude:.2f}/{longitude:.2f}"
        current_body = await self._get(f"/weather/v1/current/{coordinates}", {"lang": "zh"})
        daily_body = await self._get(
            f"/weather/v1/daily/{coordinates}",
            {"days": FORECAST_DAYS, "lang": "zh", "localTime": "true"},
        )
        current = _CurrentPayload.model_validate_json(current_body)
        daily = _DailyPayload.model_validate_json(daily_body)
        return WeatherReport(
            current=CurrentWeather(
                condition=current.condition.text,
                temperature_c=current.temperature.value,
                feels_like_c=current.feels_like.value,
                wind_scale=current.wind.scale,
            ),
            days=tuple(_to_daily_forecast(day) for day in daily.days),
        )

    async def alerts(self, latitude: float, longitude: float) -> list[QWeatherAlert]:
        coordinates = f"{latitude:.2f}/{longitude:.2f}"
        body = await self._get(
            f"/weatheralert/v1/current/{coordinates}",
            {"lang": "zh", "localTime": "true"},
        )
        try:
            payload = _AlertsPayload.model_validate_json(body)
        except ValidationError as error:
            logger.warning("和风天气回的预警看不懂，%d 处不对", error.error_count())
            raise ToolFailure(
                "天气服务回的预警看不懂", user_action="对着和风的文档查一下接口是不是改了"
            ) from None
        return [
            QWeatherAlert(
                alert_id=alert.id,
                message_type=alert.message_type.code,
                headline=alert.headline,
                description=alert.description,
                severity=alert.severity,
                expires_at=alert.expire_time,
            )
            for alert in payload.alerts
        ]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: Mapping[str, str | int]) -> bytes:
        headers = {"Authorization": f"Bearer {self._new_token()}"}
        try:
            response = await self._client.get(path, params=params, headers=headers)
        except httpx2.TimeoutException as error:
            logger.warning("等和风天气回复超时：%s", type(error).__name__)
            raise ToolFailure("天气服务没有及时回复", user_action="过一会儿再试") from error
        except httpx2.RequestError as error:
            logger.warning("连不上和风天气：%s", type(error).__name__)
            raise ToolFailure(
                "连不上天气服务", user_action="检查主机的网络，过一会儿再试"
            ) from error

        if not response.is_success:
            logger.warning("和风天气返回 HTTP %d", response.status_code)
            raise _failure_for_status(response.status_code)
        return response.content

    def _new_token(self) -> str:
        issued_at = int(self._clock.now().timestamp()) - _ISSUED_AT_BACKDATE_S
        claims = {
            "iss": self._credentials.developer_id,
            "sub": self._credentials.project_id,
            "iat": issued_at,
            "exp": issued_at + _TOKEN_LIFETIME_S,
        }
        return jwt.encode(
            claims, self._signing_key, algorithm="EdDSA", headers={"kid": self._credentials.key_id}
        )


def load_private_key(base64_line: str) -> Ed25519PrivateKey:
    private_key: PrivateKeyTypes | None
    try:
        private_key = load_der_private_key(base64.b64decode(base64_line, validate=True), None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        private_key = None
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("不是 Ed25519 私钥：要填私钥文件中间那一行")
    return private_key


def _failure_for_status(status_code: int) -> ToolFailure:
    match status_code:
        case 401:
            return ToolFailure(
                "天气服务密钥无效",
                user_action="检查 .env 里和风天气的五项，以及控制台里上传的公钥和私钥是不是一对",
            )
        case 403:
            return ToolFailure(
                "天气服务的额度用完了，或者账号欠费", user_action="去和风天气的控制台看一下"
            )
        case 429:
            return ToolFailure("天气服务嫌请求太多，暂时限流了", user_action="过一会儿再试")
        case _ if status_code >= 500:
            return ToolFailure(
                f"天气服务出错，HTTP 状态码 {status_code}", user_action="过一会儿再试"
            )
        case _:
            return ToolFailure(
                f"天气服务不认这个请求，HTTP 状态码 {status_code}",
                user_action="对着和风的文档查一下接口是不是改了",
            )


class _Temperature(BaseModel):
    value: float
    unit: Literal["°C"]


class _Condition(BaseModel):
    text: str


class _Wind(BaseModel):
    scale: int


class _CurrentPayload(BaseModel):
    condition: _Condition
    temperature: _Temperature
    feels_like: _Temperature = Field(alias="feelsLike")
    wind: _Wind


class _Precipitation(BaseModel):
    probability: float


class _HalfDayPayload(BaseModel):
    condition: _Condition
    precipitation: _Precipitation


class _DayPayload(BaseModel):
    forecast_start_time: datetime = Field(alias="forecastStartTime")
    temperature_max: _Temperature = Field(alias="temperatureMax")
    temperature_min: _Temperature = Field(alias="temperatureMin")
    daytime: _HalfDayPayload
    nighttime: _HalfDayPayload


class _DailyPayload(BaseModel):
    days: list[_DayPayload]


class _MessageType(BaseModel):
    code: str


class _AlertPayload(BaseModel):
    id: str
    message_type: _MessageType = Field(alias="messageType")
    headline: str
    description: str
    severity: str
    expire_time: datetime = Field(alias="expireTime")


class _AlertsPayload(BaseModel):
    alerts: list[_AlertPayload] = Field(default_factory=list)


def _to_daily_forecast(day: _DayPayload) -> DailyForecast:
    return DailyForecast(
        day=day.forecast_start_time.date(),
        temperature_max_c=day.temperature_max.value,
        temperature_min_c=day.temperature_min.value,
        daytime=_to_half_day(day.daytime),
        nighttime=_to_half_day(day.nighttime),
    )


def _to_half_day(half_day: _HalfDayPayload) -> HalfDayForecast:
    return HalfDayForecast(
        condition=half_day.condition.text,
        precipitation_probability=half_day.precipitation.probability,
    )
