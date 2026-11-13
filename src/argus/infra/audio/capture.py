import asyncio
import logging
from collections.abc import AsyncGenerator, Buffer

import sounddevice as sd

from argus.channels.audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from argus.infra.audio.devices import AudioDeviceError, find_wasapi_device, take_channel
from argus.infra.audio.system_devices import list_devices

logger = logging.getLogger(__name__)

_MAX_QUEUED_FRAMES = 2000 // FRAME_MS


class MicrophoneCapture:
    def __init__(self, device_name: str, channel: int) -> None:
        self._device_name = device_name
        self._channel = channel

    async def frames(self) -> AsyncGenerator[AudioFrame]:
        device = find_wasapi_device(list_devices(), self._device_name, "input")
        if self._channel >= device.input_channels:
            raise AudioDeviceError(
                f"「{device.name}」只有 {device.input_channels} 路，取不了第 {self._channel} 路（从 0 数）"
            )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=_MAX_QUEUED_FRAMES)
        dropped_frames = 0

        def put_latest(frame: AudioFrame) -> None:
            nonlocal dropped_frames
            if queue.full():
                queue.get_nowait()
                dropped_frames += 1
                if dropped_frames == 1:
                    logger.warning("收音的消费方跟不上，开始丢弃最旧的帧")
            queue.put_nowait(frame)

        def on_audio(indata: Buffer, frame_count: int, time_info: object, status: object) -> None:
            if status:
                logger.warning("收音回调报告异常：%s", status)
            mono_pcm = take_channel(bytes(indata), self._channel, device.input_channels)
            loop.call_soon_threadsafe(put_latest, AudioFrame(mono_pcm))

        try:
            stream = sd.RawInputStream(
                device=device.index,
                samplerate=SAMPLE_RATE,
                channels=device.input_channels,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                callback=on_audio,
                extra_settings=sd.WasapiSettings(exclusive=True),
            )
        except sd.PortAudioError as error:
            raise AudioDeviceError(
                f"打不开「{device.name}」：{error}\n  独占模式下，别的程序正在用这个设备时也会打不开"
            ) from error

        try:
            with stream:
                while True:
                    yield await queue.get()
        finally:
            if dropped_frames:
                logger.warning(
                    "这次收音一共丢了 %d 帧（%d ms）", dropped_frames, dropped_frames * FRAME_MS
                )
