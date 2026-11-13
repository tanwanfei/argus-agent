import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx2

logger = logging.getLogger(__name__)

CUSTOMIZATION_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/customization"

_PREFIX = "argushome"
_WEIGHT = 5
_TIMEOUT = httpx2.Timeout(10.0)


class HotwordError(Exception):
    pass


class HotwordVocabulary:
    def __init__(
        self,
        api_key: str,
        target_model: str,
        *,
        url: str = CUSTOMIZATION_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        deploy_wait_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._target_model = target_model
        self._url = url
        self._transport = transport
        self._deploy_wait_seconds = deploy_wait_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def sync(self, words: Sequence[str]) -> str:
        wanted = [{"text": word, "weight": _WEIGHT} for word in words]
        async with httpx2.AsyncClient(
            trust_env=False, timeout=_TIMEOUT, transport=self._transport
        ) as http:
            try:
                return await self._sync_with(http, wanted)
            except (KeyError, TypeError, ValueError) as error:
                raise HotwordError(f"热词表接口返回的内容看不懂：{type(error).__name__}") from error

    async def _sync_with(self, http: httpx2.AsyncClient, wanted: list[dict[str, Any]]) -> str:
        listed = await self._call(http, {"action": "list_vocabulary", "prefix": _PREFIX})
        existing_ids = [str(item["vocabulary_id"]) for item in listed["vocabulary_list"]]
        if not existing_ids:
            vocabulary_id = await self._create(http, wanted)
            logger.info("新建了热词表：%d 个词", len(wanted))
        else:
            vocabulary_id, *extra_ids = existing_ids
            for extra_id in extra_ids:
                await self._call(http, {"action": "delete_vocabulary", "vocabulary_id": extra_id})
                logger.info("删掉了一张多出来的热词表")
            await self._update_if_changed(http, vocabulary_id, wanted)
        await self._wait_until_deployed(http, vocabulary_id)
        return vocabulary_id

    async def _create(self, http: httpx2.AsyncClient, wanted: list[dict[str, Any]]) -> str:
        output = await self._call(
            http,
            {
                "action": "create_vocabulary",
                "target_model": self._target_model,
                "prefix": _PREFIX,
                "vocabulary": wanted,
            },
        )
        return str(output["vocabulary_id"])

    async def _update_if_changed(
        self, http: httpx2.AsyncClient, vocabulary_id: str, wanted: list[dict[str, Any]]
    ) -> None:
        current = await self._call(
            http, {"action": "query_vocabulary", "vocabulary_id": vocabulary_id}
        )
        if _words_and_weights(current["vocabulary"]) == _words_and_weights(wanted):
            logger.info("热词表没变：%d 个词", len(wanted))
            return
        await self._call(
            http,
            {"action": "update_vocabulary", "vocabulary_id": vocabulary_id, "vocabulary": wanted},
        )
        logger.info("更新了热词表：%d 个词", len(wanted))

    async def _wait_until_deployed(self, http: httpx2.AsyncClient, vocabulary_id: str) -> None:
        last_status = "还没查到"
        try:
            async with asyncio.timeout(self._deploy_wait_seconds):
                while True:
                    queried = await self._call(
                        http, {"action": "query_vocabulary", "vocabulary_id": vocabulary_id}
                    )
                    last_status = str(queried["status"])
                    if last_status == "OK":
                        return
                    await asyncio.sleep(self._poll_interval_seconds)
        except TimeoutError:
            raise HotwordError(
                f"热词表 {self._deploy_wait_seconds:g} 秒还没部署好，最后的状态是 {last_status}"
            ) from None

    async def _call(self, http: httpx2.AsyncClient, action_input: dict[str, Any]) -> dict[str, Any]:
        action = action_input["action"]
        try:
            response = await http.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": "speech-biasing", "input": action_input},
            )
        except httpx2.RequestError as error:
            raise HotwordError(f"热词表接口 {action} 连不上：{type(error).__name__}") from error
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            error_code = body.get("code") if isinstance(body, dict) else None
            raise HotwordError(
                f"热词表接口 {action} 返回 HTTP {response.status_code}，错误码 {error_code}"
            )
        output = body.get("output") if isinstance(body, dict) else None
        if not isinstance(output, dict):
            raise HotwordError(f"热词表接口 {action} 返回的内容里没有 output")
        return output


def _words_and_weights(vocabulary: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(str(entry["text"]), int(entry["weight"])) for entry in vocabulary]
