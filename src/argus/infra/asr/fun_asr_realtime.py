import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from argus.channels.audio import SAMPLE_RATE, SAMPLE_WIDTH, AudioClip, AudioFrame
from argus.infra.connection_log import ConnectionLog
from argus.speech.inference import AsrError, AsrSession

logger = logging.getLogger(__name__)

DASHSCOPE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"

_SEND_CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH // 10

_CLOSE_TIMEOUT_SECONDS = 1.0


class FunAsrRealtime:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        url: str = DASHSCOPE_URL,
        connection_wait_seconds: float = 0.0,
        result_wait_seconds: float = 3.0,
        first_retry_delay_seconds: float = 1.0,
        max_retry_delay_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = url
        self._connection_wait_seconds = connection_wait_seconds
        self._result_wait_seconds = result_wait_seconds
        self._first_retry_delay_seconds = first_retry_delay_seconds
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._connection: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._one_task_at_a_time = asyncio.Lock()
        self._vocabulary_id: str | None = None
        self._connection_log = ConnectionLog(logger, "云端识别")

    async def keep_connected(self) -> None:
        retry_delay = self._first_retry_delay_seconds
        while True:
            try:
                async with connect(
                    self._url,
                    additional_headers={"Authorization": f"bearer {self._api_key}"},
                    close_timeout=_CLOSE_TIMEOUT_SECONDS,
                    proxy=None,
                ) as connection:
                    self._connection_log.connected()
                    self._connection = connection
                    self._connected.set()
                    retry_delay = self._first_retry_delay_seconds
                    await connection.wait_closed()
                self._connection_log.dropped(retry_delay)
            except (OSError, TimeoutError, WebSocketException) as error:
                self._connection_log.failed(error, retry_delay)
            finally:
                self._connected.clear()
                self._connection = None
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, self._max_retry_delay_seconds)

    def start(self, heard_so_far: AudioClip) -> AsrSession:
        return _FunAsrSession(self, heard_so_far, self._result_wait_seconds)

    def use_vocabulary(self, vocabulary_id: str) -> None:
        self._vocabulary_id = vocabulary_id

    async def _recognize(self, outgoing: asyncio.Queue[bytes | None]) -> str:
        async with self._one_task_at_a_time:
            connection = await self._wait_for_connection()
            task_id = uuid.uuid4().hex
            task_finished = False
            try:
                await self._send(connection, self._run_task_message(task_id))
                await _wait_for_task_started(connection)
                sending = asyncio.create_task(_send_audio(connection, outgoing, task_id))
                try:
                    sentences = await _receive_sentences(connection)
                finally:
                    sending.cancel()
                task_finished = True
                return "".join(sentences)
            finally:
                if not task_finished:
                    await connection.close()
                    self._forget(connection)

    def _forget(self, connection: ClientConnection) -> None:
        if self._connection is connection:
            self._connection = None
            self._connected.clear()

    async def _wait_for_connection(self) -> ClientConnection:
        if not self._connected.is_set():
            try:
                async with asyncio.timeout(self._connection_wait_seconds):
                    await self._connected.wait()
            except TimeoutError:
                raise AsrError("这句开始传时没连着云端识别") from None
        assert self._connection is not None
        return self._connection

    async def _send(self, connection: ClientConnection, message: str) -> None:
        try:
            await connection.send(message)
        except ConnectionClosed as error:
            raise AsrError(f"云端识别的连接断了：{error}") from error

    def _run_task_message(self, task_id: str) -> str:
        parameters: dict[str, Any] = {"format": "pcm", "sample_rate": SAMPLE_RATE}
        if self._vocabulary_id is not None:
            parameters["vocabulary_id"] = self._vocabulary_id
        return json.dumps(
            {
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": self._model,
                    "parameters": parameters,
                    "input": {},
                },
            }
        )


class _FunAsrSession:
    def __init__(
        self, asr: FunAsrRealtime, heard_so_far: AudioClip, result_wait_seconds: float
    ) -> None:
        self._result_wait_seconds = result_wait_seconds
        self._outgoing: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._outgoing.put_nowait(heard_so_far.pcm)
        self._recognizing = asyncio.create_task(asr._recognize(self._outgoing), name="cloud-asr")

    def feed(self, frame: AudioFrame) -> None:
        self._outgoing.put_nowait(frame.pcm)

    async def finish(self, clip: AudioClip) -> str:
        self._outgoing.put_nowait(None)
        try:
            async with asyncio.timeout(self._result_wait_seconds):
                return await asyncio.shield(self._recognizing)
        except TimeoutError:
            self._recognizing.cancel()
            raise AsrError(f"判停后 {self._result_wait_seconds:g} 秒还没拿到最终文字") from None

    def cancel(self) -> None:
        self._recognizing.cancel()


async def _wait_for_task_started(connection: ClientConnection) -> None:
    while (await _receive_event(connection)).name != "task-started":
        pass


async def _send_audio(
    connection: ClientConnection, outgoing: asyncio.Queue[bytes | None], task_id: str
) -> None:
    pending = bytearray()
    try:
        while (audio := await outgoing.get()) is not None:
            pending += audio
            while len(pending) >= _SEND_CHUNK_BYTES:
                await connection.send(bytes(pending[:_SEND_CHUNK_BYTES]))
                del pending[:_SEND_CHUNK_BYTES]
        if pending:
            await connection.send(bytes(pending))
        await connection.send(_finish_task_message(task_id))
    except ConnectionClosed:
        pass


async def _receive_sentences(connection: ClientConnection) -> list[str]:
    sentences: list[str] = []
    while True:
        event = await _receive_event(connection)
        if event.name == "task-finished":
            return sentences
        if event.name == "result-generated":
            if event.sentence.get("sentence_end") and not event.sentence.get("heartbeat"):
                sentences.append(str(event.sentence.get("text", "")))


@dataclass(frozen=True)
class _ServerEvent:
    name: str
    sentence: dict[str, Any]


async def _receive_event(connection: ClientConnection) -> _ServerEvent:
    try:
        message = await connection.recv()
    except ConnectionClosed as error:
        raise AsrError(f"云端识别的连接半路断了：{error}") from error
    try:
        parsed = json.loads(message)
        header = parsed["header"]
        event_name = header["event"]
        sentence: Any = {}
        if event_name == "result-generated":
            sentence = parsed["payload"]["output"]["sentence"]
        if not isinstance(event_name, str) or not isinstance(sentence, dict):
            raise TypeError("event 不是字符串，或者 sentence 不是对象")
    except (ValueError, KeyError, TypeError) as error:
        raise AsrError(f"云端识别发来看不懂的消息：{error!r}") from error
    if event_name == "task-failed":
        raise AsrError(f"云端识别报错：{header.get('error_code')} {header.get('error_message')}")
    return _ServerEvent(event_name, sentence)


def _finish_task_message(task_id: str) -> str:
    return json.dumps(
        {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }
    )
