from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH


@dataclass(frozen=True, repr=False)
class _PcmAudio:
    pcm: bytes

    def __post_init__(self) -> None:
        if len(self.pcm) % SAMPLE_WIDTH != 0:
            raise ValueError(
                f"{type(self).__name__} 的字节数必须是 {SAMPLE_WIDTH} 的倍数（整数个采样），"
                f"收到 {len(self.pcm)} 字节"
            )

    @property
    def duration_ms(self) -> float:
        sample_count = len(self.pcm) // SAMPLE_WIDTH
        return sample_count * 1000 / SAMPLE_RATE

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.duration_ms:g}ms)"


@dataclass(frozen=True, repr=False)
class AudioFrame(_PcmAudio):
    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.pcm) != FRAME_BYTES:
            raise ValueError(
                f"AudioFrame 必须正好 {FRAME_BYTES} 字节（{FRAME_MS}ms），收到 {len(self.pcm)} 字节"
            )


@dataclass(frozen=True, repr=False)
class AudioChunk(_PcmAudio):
    pass


@dataclass(frozen=True, repr=False)
class AudioClip(_PcmAudio):
    @classmethod
    def from_frames(cls, frames: Iterable[AudioFrame]) -> Self:
        return cls(b"".join(frame.pcm for frame in frames))
