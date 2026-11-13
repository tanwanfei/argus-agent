import asyncio
import logging
from dataclasses import dataclass

from argus.app.voice import SpeechInput, SpeechInputFactory
from argus.channels.registry import EndpointRegistry
from argus.infra.endpoints.app_ws.endpoint import AppEndpoint
from argus.session.session import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Connection:
    endpoint: AppEndpoint
    finished: asyncio.Event


class AppVoice:
    def __init__(
        self, endpoints: EndpointRegistry, session: Session, speech_input_for: SpeechInputFactory
    ) -> None:
        self._endpoints = endpoints
        self._session = session
        self._speech_input_for = speech_input_for
        self._current: _Connection | None = None
        self._taking_over = asyncio.Lock()

    async def serve(self, endpoint: AppEndpoint) -> None:
        connection = _Connection(endpoint, asyncio.Event())
        async with self._taking_over:
            previous = self._current
            if previous is not None:
                logger.info("App 重新连上，先断开之前那条连接")
                previous.endpoint.close()
                await previous.finished.wait()
            self._endpoints.register(endpoint)
            self._current = connection
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._forward_texts(endpoint), name="app-text")
                task_group.create_task(
                    self._forward_confirmations(endpoint), name="app-confirmation"
                )
                speech_input = await asyncio.to_thread(
                    self._speech_input_for, endpoint, self._session
                )
                if await endpoint.wait_for_audio():
                    await self._serve_voice(endpoint, speech_input)
        finally:
            self._endpoints.unregister(endpoint)
            self._current = None
            connection.finished.set()

    async def _serve_voice(self, endpoint: AppEndpoint, speech_input: SpeechInput) -> None:
        self._session.add_endpoint(
            endpoint.id, speech_input, exclusive=True, owner_only=True, name_only_after_stop=True
        )
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._forward_barge_ins(endpoint), name="app-barge-in")
                await speech_input.listen()
        finally:
            await self._session.remove_endpoint(endpoint.id)

    async def _forward_barge_ins(self, endpoint: AppEndpoint) -> None:
        async for _ in endpoint.barge_ins():
            await self._session.on_barge_in(endpoint.id)

    async def _forward_texts(self, endpoint: AppEndpoint) -> None:
        async for text in endpoint.texts():
            await self._session.on_text(endpoint.id, text)

    async def _forward_confirmations(self, endpoint: AppEndpoint) -> None:
        async for confirmation in endpoint.confirmations():
            self._session.on_confirmation(endpoint.id, confirmation)
