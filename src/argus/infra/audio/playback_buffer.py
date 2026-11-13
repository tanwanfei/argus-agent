import asyncio
import threading

from argus.channels.audio import SAMPLE_RATE
from argus.infra.audio.devices import AudioDeviceError
from argus.kernel.clock import Clock

_STALLED_AFTER_S = 2.0


class PlaybackBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._finished = False
        self._bytes_read = 0

    def append(self, pcm: bytes) -> None:
        with self._lock:
            self._pending += pcm

    def finish(self) -> None:
        with self._lock:
            self._finished = True

    def read(self, byte_count: int) -> tuple[bytes, bool]:
        with self._lock:
            block = bytes(self._pending[:byte_count])
            del self._pending[:byte_count]
            self._bytes_read += len(block)
            all_played = self._finished and not self._pending
        silence = bytes(byte_count - len(block))
        return block + silence, all_played

    def bytes_read(self) -> int:
        with self._lock:
            return self._bytes_read

    def pending_bytes(self) -> int:
        with self._lock:
            return len(self._pending)


def heard_ms(
    *, bytes_read: int, audio_bytes: int, latency_s: float, bytes_per_sample_frame: int
) -> float:
    played_seconds = min(bytes_read, audio_bytes) / bytes_per_sample_frame / SAMPLE_RATE
    return max(0.0, played_seconds - latency_s) * 1000


async def wait_until_buffered_at_most(
    buffer: PlaybackBuffer,
    seconds: float,
    *,
    bytes_per_sample_frame: int,
    clock: Clock,
    device_name: str,
) -> None:
    bytes_per_second = SAMPLE_RATE * bytes_per_sample_frame
    max_bytes = round(seconds * bytes_per_second)
    bytes_read_before = buffer.bytes_read()
    progressed_at = clock.monotonic()
    while (excess_bytes := buffer.pending_bytes() - max_bytes) > 0:
        await clock.sleep(min(excess_bytes / bytes_per_second, _STALLED_AFTER_S))
        if buffer.bytes_read() != bytes_read_before:
            bytes_read_before = buffer.bytes_read()
            progressed_at = clock.monotonic()
        elif clock.monotonic() - progressed_at >= _STALLED_AFTER_S:
            raise AudioDeviceError(
                f"「{device_name}」{_STALLED_AFTER_S:.0f} 秒没来取声音，可能被拔掉了"
            )


async def wait_until_stream_stops(
    stream_stopped: asyncio.Event, buffer: PlaybackBuffer, clock: Clock, device_name: str
) -> None:
    while True:
        bytes_read_before = buffer.bytes_read()
        try:
            async with clock.timeout(_STALLED_AFTER_S):
                await stream_stopped.wait()
            return
        except TimeoutError:
            if buffer.bytes_read() == bytes_read_before:
                raise AudioDeviceError(
                    f"「{device_name}」{_STALLED_AFTER_S:.0f} 秒没来取声音，可能被拔掉了"
                ) from None
