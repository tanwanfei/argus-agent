from collections.abc import Sequence

from argus.channels.audio import AudioClip
from argus.infra.speaker.voiceprint import SpeakerEmbedding, Voiceprint, similarity


class VoiceprintVerifier:
    def __init__(self, embedding: SpeakerEmbedding, voiceprints: Sequence[Voiceprint]) -> None:
        if not voiceprints:
            raise ValueError("一份声纹也没有，没法比")
        for voiceprint in voiceprints:
            if voiceprint.model != embedding.model_name:
                raise ValueError(
                    f"声纹是用 {voiceprint.model} 算的，现在的声纹模型是 {embedding.model_name}："
                    "重新注册一次"
                )
        self._embedding = embedding
        self._voiceprints = tuple(voiceprints)

    def score(self, clip: AudioClip) -> float:
        embedding = self._embedding.embed(clip)
        return max(similarity(voiceprint, embedding) for voiceprint in self._voiceprints)
