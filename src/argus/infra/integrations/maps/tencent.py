import logging

import httpx2
from pydantic import BaseModel, ValidationError

from argus.situation.coordinates import Gcj02Point
from argus.situation.geocoding import SENT_DECIMALS, ReverseGeocodeError, coarsened

logger = logging.getLogger(__name__)

_BASE_URL = "https://apis.map.qq.com"
_GEOCODER_PATH = "/ws/geocoder/v1/"
_TIMEOUT = httpx2.Timeout(1.0)

_KEY_PROBLEM_STATUSES = frozenset({110, 111, 112, 113, 190, 199, 301, 311})
_TOO_FAST_STATUS = 120
_DAILY_QUOTA_STATUS = 121


class TencentReverseGeocoder:
    def __init__(self, key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._key = key
        self._client = httpx2.AsyncClient(
            base_url=_BASE_URL,
            trust_env=False,
            timeout=_TIMEOUT,
            transport=transport,
        )

    async def district_and_road(self, point: Gcj02Point) -> str:
        sent = coarsened(point)
        location = f"{sent.lat:.{SENT_DECIMALS}f},{sent.lng:.{SENT_DECIMALS}f}"
        try:
            response = await self._client.get(
                _GEOCODER_PATH, params={"location": location, "key": self._key}
            )
        except httpx2.TimeoutException as error:
            logger.warning("等腾讯地图回复超时：%s", type(error).__name__)
            raise ReverseGeocodeError("地图服务没有及时回复") from None
        except httpx2.RequestError as error:
            logger.warning("连不上腾讯地图：%s", type(error).__name__)
            raise ReverseGeocodeError("连不上地图服务，检查主机的网络") from None

        if not response.is_success:
            logger.warning("腾讯地图返回 HTTP %d", response.status_code)
            raise ReverseGeocodeError(f"地图服务出错，HTTP 状态码 {response.status_code}")
        try:
            payload = _Payload.model_validate_json(response.content)
        except ValidationError:
            raise ReverseGeocodeError("地图服务的回复看不懂，多半是腾讯改了接口") from None
        if payload.status != 0:
            logger.warning("腾讯地图返回 status %d", payload.status)
            raise _error_for_status(payload.status)
        if payload.result is None:
            raise ReverseGeocodeError("地图服务的回复看不懂，多半是腾讯改了接口")

        address = payload.result.address_component
        district_and_road = f"{address.city}{address.district}{address.street}"
        if not district_and_road:
            raise ReverseGeocodeError("地图服务没给出这里的地名")
        return district_and_road

    async def aclose(self) -> None:
        await self._client.aclose()


def _error_for_status(status: int) -> ReverseGeocodeError:
    if status in _KEY_PROBLEM_STATUSES:
        return ReverseGeocodeError(
            f"地图服务不认这个 key（status {status}）："
            "检查 .env 里的 TENCENT_MAP_KEY，和控制台里这个 key 勾没勾 WebServiceAPI"
        )
    if status == _TOO_FAST_STATUS:
        return ReverseGeocodeError("地图服务嫌请求太快，暂时限流了，过一会儿再试")
    if status == _DAILY_QUOTA_STATUS:
        return ReverseGeocodeError("地图服务今天的额度用完了，明天恢复；去控制台看一下额度")
    if status >= 500:
        return ReverseGeocodeError(f"地图服务出错（status {status}），过一会儿再试")
    return ReverseGeocodeError(f"地图服务不认这个请求（status {status}），对着腾讯的文档查一下")


class _AddressComponent(BaseModel):
    city: str = ""
    district: str = ""
    street: str = ""


class _Result(BaseModel):
    address_component: _AddressComponent


class _Payload(BaseModel):
    status: int
    result: _Result | None = None
