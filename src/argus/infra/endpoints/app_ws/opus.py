from typing import Literal

import av
from av.audio.codeccontext import AudioCodecContext

from argus.channels.audio import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE, SAMPLE_WIDTH

_BIT_RATE = 24_000


def _create_libopus(mode: Literal["r", "w"]) -> AudioCodecContext:
    context = av.CodecContext.create("libopus", mode)
    assert isinstance(context, AudioCodecContext)
    return context


class OpusEncoder:
    def __init__(self) -> None:
        context = _create_libopus("w")
        context.sample_rate = SAMPLE_RATE
        context.layout = "mono"
        context.format = "s16"
        context.bit_rate = _BIT_RATE
        context.options = {"application": "voip", "frame_duration": "20"}
        context.open()
        self._context = context

    def encode(self, pcm_20ms: bytes) -> bytes:
        if len(pcm_20ms) != FRAME_BYTES:
            raise ValueError(f"一次要编正好 {FRAME_BYTES} 字节（20ms），收到 {len(pcm_20ms)} 字节")
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.sample_rate = SAMPLE_RATE
        frame.planes[0].update(pcm_20ms)
        packets = self._context.encode(frame)
        if len(packets) != 1:
            raise RuntimeError(f"喂了 20ms，编码器出了 {len(packets)} 个包，应该是 1 个")
        return bytes(packets[0])


class OpusDecoder:
    def __init__(self) -> None:
        context = _create_libopus("r")
        context.layout = "mono"
        context.open()
        self._context = context
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

    def decode(self, packet: bytes) -> bytes:
        if not packet:
            raise ValueError("空包")
        pcm = bytearray()
        try:
            for decoded in self._context.decode(av.Packet(packet)):
                for resampled in self._resampler.resample(decoded):
                    pcm += bytes(resampled.planes[0])[: resampled.samples * SAMPLE_WIDTH]
        except av.FFmpegError as error:
            raise ValueError(f"解不开：{error}") from error
        return bytes(pcm)
