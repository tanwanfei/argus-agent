import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from argus.channels.audio import AudioClip


class SpeakerEmbedding(Protocol):
    model_name: str

    def embed(self, clip: AudioClip) -> Sequence[float]: ...


@dataclass(frozen=True)
class Voiceprint:
    model: str
    sentence_count: int
    vector: tuple[float, ...]


def average_voiceprint(model: str, embeddings: Sequence[Sequence[float]]) -> Voiceprint:
    if not embeddings:
        raise ValueError("一句都没有，算不出声纹")
    units = [_unit(embedding) for embedding in embeddings]
    dimension = len(units[0])
    if any(len(unit) != dimension for unit in units):
        raise ValueError("各句的声纹维数不一样")
    mean = [math.fsum(unit[index] for unit in units) / len(units) for index in range(dimension)]
    return Voiceprint(model, len(units), _unit(mean))


def similarity(voiceprint: Voiceprint, embedding: Sequence[float]) -> float:
    if len(embedding) != len(voiceprint.vector):
        raise ValueError(
            f"维数不一样：注册的 {len(voiceprint.vector)} 维，这句的 {len(embedding)} 维"
        )
    return math.sumprod(voiceprint.vector, _unit(embedding))


def _unit(vector: Sequence[float]) -> tuple[float, ...]:
    norm = math.hypot(*vector)
    if norm == 0:
        raise ValueError("声纹向量全是 0")
    return tuple(value / norm for value in vector)
