import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from argus.channels.audio import SAMPLE_RATE, SAMPLE_WIDTH, AudioChunk
from argus.infra.connection_log import ConnectionLog
from argus.speech.inference import TtsError

logger = logging.getLogger(__name__)

DASHSCOPE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"

_NAME_IN_LETTERS = re.compile("argus", re.IGNORECASE)
_NAME_IN_CHINESE = "阿格斯"

_CLOSE_TIMEOUT_SECONDS = 1.0


class CosyVoiceTts:
    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        *,
        url: str = DASHSCOPE_URL,
        connection_wait_seconds: float = 3.0,
        silence_timeout_seconds: float = 5.0,
        first_retry_delay_seconds: float = 1.0,
        max_retry_delay_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._url = url
        self._connection_wait_seconds = connection_wait_seconds
        self._silence_timeout_seconds = silence_timeout_seconds
        self._first_retry_delay_seconds = first_retry_delay_seconds
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._connection: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._one_task_at_a_time = asyncio.Lock()
        self._connection_log = ConnectionLog(logger, "云端 TTS")

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

    async def synthesize(self, sentence: str) -> AsyncGenerator[AudioChunk]:
        text = _NAME_IN_LETTERS.sub(_NAME_IN_CHINESE, sentence)
        async with self._one_task_at_a_time:
            connection = await self._wait_for_connection()
            task_id = uuid.uuid4().hex
            task_finished = False
            try:
                await self._send(connection, self._run_task_message(task_id))
                while not task_finished:
                    message = await self._receive(connection)
                    if isinstance(message, bytes):
                        yield _audio_chunk(message)
                        continue
                    event = _parse_event(message)
                    if event["event"] == "task-started":
                        await self._send(connection, _continue_task_message(task_id, text))
                        await self._send(connection, _finish_task_message(task_id))
                    elif event["event"] == "task-finished":
                        task_finished = True
                    elif event["event"] == "task-failed":
                        raise TtsError(
                            f"云端 TTS 报错：{event.get('error_code')} {event.get('error_message')}"
                        )
            finally:
                if not task_finished:
                    self._forget(connection)
                    connection.transport.abort()

    def _forget(self, connection: ClientConnection) -> None:
        if self._connection is connection:
            self._connection = None
            self._connected.clear()

    async def _wait_for_connection(self) -> ClientConnection:
        try:
            async with asyncio.timeout(self._connection_wait_seconds):
                await self._connected.wait()
        except TimeoutError:
            raise TtsError(f"等了 {self._connection_wait_seconds:g} 秒还没连上云端 TTS") from None
        assert self._connection is not None
        return self._connection

    async def _send(self, connection: ClientConnection, message: str) -> None:
        try:
            await connection.send(message)
        except ConnectionClosed as error:
            raise TtsError(f"云端 TTS 的连接断了：{error}") from error

    async def _receive(self, connection: ClientConnection) -> str | bytes:
        try:
            async with asyncio.timeout(self._silence_timeout_seconds):
                return await connection.recv()
        except TimeoutError:
            raise TtsError(f"云端 TTS 超过 {self._silence_timeout_seconds:g} 秒没有回音") from None
        except ConnectionClosed as error:
            raise TtsError(f"云端 TTS 的连接半路断了：{error}") from error

    def _run_task_message(self, task_id: str) -> str:
        return json.dumps(
            {
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio",
                    "task": "tts",
                    "function": "SpeechSynthesizer",
                    "model": self._model,
                    "parameters": {
                        "text_type": "PlainText",
                        "voice": self._voice,
                        "format": "pcm",
                        "sample_rate": SAMPLE_RATE,
                    },
                    "input": {},
                },
            }
        )


def _continue_task_message(task_id: str, text: str) -> str:
    return json.dumps(
        {
            "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {"text": text}},
        }
    )


def _finish_task_message(task_id: str) -> str:
    return json.dumps(
        {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }
    )


def _parse_event(message: str) -> dict[str, str]:
    try:
        header = json.loads(message)["header"]
        if not isinstance(header, dict) or not isinstance(header.get("event"), str):
            raise TypeError("header 里没有 event")
    except (ValueError, KeyError, TypeError) as error:
        raise TtsError(f"云端 TTS 发来看不懂的消息：{error}") from error
    return header


def _audio_chunk(frame: bytes) -> AudioChunk:
    if len(frame) % SAMPLE_WIDTH != 0:
        raise TtsError(f"云端 TTS 发来的音频是 {len(frame)} 字节，不是整数个采样")
    return AudioChunk(frame)
