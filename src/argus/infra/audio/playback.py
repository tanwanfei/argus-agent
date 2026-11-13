import asyncio
import logging
from collections.abc import AsyncIterator, Buffer

import sounddevice as sd

from argus.channels.audio import SAMPLE_RATE, SAMPLE_WIDTH, AudioChunk
from argus.infra.audio.devices import AudioDeviceError, duplicate_channel, find_wasapi_device
from argus.infra.audio.playback_buffer import (
    PlaybackBuffer,
    heard_ms,
    wait_until_buffered_at_most,
    wait_until_stream_stops,
)
from argus.infra.audio.system_devices import list_devices
from argus.kernel.clock import Clock
from argus.kernel.tracing import LatencyMark, mark_latency

logger = logging.getLogger(__name__)

_LEAD_S = 1.0


class SpeakerPlayback:
    def __init__(self, device_name: str, clock: Clock) -> None:
        self._device_name = device_name
        self._clock = clock
        self._played_ms = 0.0

    def played_ms(self) -> float:
        return self._played_ms

    async def play(self, chunks: AsyncIterator[AudioChunk]) -> None:
        self._played_ms = 0.0
        device = find_wasapi_device(list_devices(), self._device_name, "output")
        channel_count = device.output_channels
        bytes_per_sample_frame = SAMPLE_WIDTH * channel_count

        loop = asyncio.get_running_loop()
        buffer = PlaybackBuffer()
        stream_finished = asyncio.Event()

        def on_audio(outdata: Buffer, frame_count: int, time_info: object, status: object) -> None:
            if status:
                logger.warning("放音回调报告异常：%s", status)
            block, all_played = buffer.read(frame_count * bytes_per_sample_frame)
            memoryview(outdata)[:] = block
            if all_played:
                raise sd.CallbackStop

        def on_finished() -> None:
            loop.call_soon_threadsafe(stream_finished.set)

        try:
            stream = sd.RawOutputStream(
                device=device.index,
                samplerate=SAMPLE_RATE,
                channels=channel_count,
                dtype="int16",
                callback=on_audio,
                finished_callback=on_finished,
            )
        except sd.PortAudioError as error:
            raise AudioDeviceError(f"打不开「{device.name}」：{error}") from error

        audio_bytes = 0
        tail_bytes = 0
        everything_played = False
        try:
            started = False
            async for chunk in chunks:
                pcm = duplicate_channel(chunk.pcm, channel_count)
                buffer.append(pcm)
                audio_bytes += len(pcm)
                if not started:
                    try:
                        stream.start()
                    except sd.PortAudioError as error:
                        raise AudioDeviceError(f"「{device.name}」开始放音失败：{error}") from error
                    started = True
                    mark_latency(LatencyMark.PLAY_START, delay_seconds=stream.latency)
                await wait_until_buffered_at_most(
                    buffer,
                    _LEAD_S,
                    bytes_per_sample_frame=bytes_per_sample_frame,
                    clock=self._clock,
                    device_name=device.name,
                )
            if not started:
                return

            tail_bytes = round(stream.latency * SAMPLE_RATE) * bytes_per_sample_frame
            buffer.append(bytes(tail_bytes))
            buffer.finish()
            await wait_until_stream_stops(stream_finished, buffer, self._clock, device.name)
            everything_played = buffer.bytes_read() == audio_bytes + tail_bytes
            if not everything_played:
                raise AudioDeviceError(f"「{device.name}」的放音流提前停了，可能被拔掉了")
        finally:
            stream.abort()
            if everything_played:
                self._played_ms = audio_bytes / bytes_per_sample_frame / SAMPLE_RATE * 1000
            else:
                self._played_ms = heard_ms(
                    bytes_read=buffer.bytes_read(),
                    audio_bytes=audio_bytes,
                    latency_s=stream.latency,
                    bytes_per_sample_frame=bytes_per_sample_frame,
                )
            stream.close()
