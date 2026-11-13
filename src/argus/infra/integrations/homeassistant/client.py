import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
from pydantic import BaseModel

from argus.kernel.tools import ToolFailure

logger = logging.getLogger(__name__)

_TIMEOUT = httpx2.Timeout(5.0)
_LOGGED_BODY_CHARS = 200


@dataclass(frozen=True)
class EntityState:
    entity_id: str
    state: str
    attributes: Mapping[str, Any]


class HomeAssistantClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            timeout=_TIMEOUT,
            transport=transport,
        )

    async def state(self, entity_id: str) -> EntityState:
        response = await self._send(
            "GET",
            f"/api/states/{entity_id}",
            not_found=ToolFailure(
                f"Home Assistant 里没有设备 {entity_id}",
                user_action="检查 argus.yaml 里写的设备名和 HA 里的是不是一样",
            ),
        )
        payload = _StatePayload.model_validate_json(response.content)
        return EntityState(payload.entity_id, payload.state, payload.attributes)

    async def call_service(self, domain: str, service: str, data: Mapping[str, Any]) -> None:
        await self._send(
            "POST",
            f"/api/services/{domain}/{service}",
            json=data,
            not_found=ToolFailure(
                f"Home Assistant 没有 {domain}.{service} 这个服务",
                user_action="HA 的集成可能没装好，在 HA 网页里看看这个设备",
            ),
        )

    async def ping(self) -> ToolFailure | None:
        try:
            response = await self._client.get("/api/")
        except httpx2.TimeoutException:
            return _timed_out()
        except httpx2.RequestError:
            return _unreachable()
        return None if response.is_success else _failure_for_status(response.status_code)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        not_found: ToolFailure,
        json: Mapping[str, Any] | None = None,
    ) -> httpx2.Response:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx2.TimeoutException as error:
            logger.warning("等 Home Assistant 回复超时：%s", type(error).__name__)
            raise _timed_out() from error
        except httpx2.RequestError as error:
            logger.warning("连不上 Home Assistant：%s", type(error).__name__)
            raise _unreachable() from error

        if response.is_success:
            return response
        logger.warning(
            "Home Assistant 返回 HTTP %d：%s %s %s",
            response.status_code,
            method,
            path,
            response.text[:_LOGGED_BODY_CHARS],
        )
        if response.status_code == 404:
            raise not_found
        raise _failure_for_status(response.status_code)


def _timed_out() -> ToolFailure:
    return ToolFailure(
        "Home Assistant 没有及时回复",
        user_action="过一会儿再试；总是这样的话，看看设备在不在线、HA 的虚拟机有没有卡住",
    )


def _unreachable() -> ToolFailure:
    return ToolFailure(
        "连不上家里的 Home Assistant",
        user_action="确认它的虚拟机开着（运行 HA 的启动脚本），再试一次",
    )


def _failure_for_status(status_code: int) -> ToolFailure:
    match status_code:
        case 401:
            return ToolFailure(
                "Home Assistant 不认主机的令牌",
                user_action="检查 .env 里的 HA_TOKEN：令牌可能抄错了，或者在 HA 里被删了",
            )
        case 400:
            return ToolFailure(
                "Home Assistant 没有照办", user_action="设备可能掉线了，在米家 App 里看看它在不在线"
            )
        case _ if status_code >= 500:
            return ToolFailure(
                f"Home Assistant 出错，HTTP 状态码 {status_code}", user_action="过一会儿再试"
            )
        case _:
            return ToolFailure(
                f"Home Assistant 不认这个请求，HTTP 状态码 {status_code}",
                user_action="对着 HA 的文档查一下接口是不是改了",
            )


class _StatePayload(BaseModel):
    entity_id: str
    state: str
    attributes: dict[str, Any]
