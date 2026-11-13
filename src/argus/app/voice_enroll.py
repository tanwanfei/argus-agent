from collections.abc import Callable, Sequence
from dataclasses import dataclass

from argus.channels.audio import FRAME_BYTES, FRAME_MS, AudioClip, AudioFrame
from argus.infra.speaker.voiceprint import (
    SpeakerEmbedding,
    Voiceprint,
    average_voiceprint,
    similarity,
)
from argus.speech.speaker import SpeakerVerifier
from argus.speech.vad import SpeechEnd, Vad

MIN_SENTENCES = 20
SUSPICIOUS_SIMILARITY = 0.3
_LEADING_SILENCE_MS = 1000
_TRAILING_SILENCE_MS = 2000


@dataclass(frozen=True)
class SentenceScore:
    recording_index: int
    start_seconds: float
    seconds: float
    score: float


@dataclass(frozen=True)
class Enrollment:
    voiceprint: Voiceprint
    sentences: tuple[SentenceScore, ...]

    @property
    def suspicious(self) -> tuple[SentenceScore, ...]:
        return tuple(
            sentence for sentence in self.sentences if sentence.score < SUSPICIOUS_SIMILARITY
        )


class NotEnoughSpeech(Exception):
    def __init__(self, sentence_count: int) -> None:
        super().__init__(f"只切出 {sentence_count} 句，至少要 {MIN_SENTENCES} 句")
        self.sentence_count = sentence_count


def enroll(
    recordings: Sequence[AudioClip],
    new_vad: Callable[[], Vad],
    embedding: SpeakerEmbedding,
) -> Enrollment:
    clips = _cut_recordings(recordings, new_vad)
    if len(clips) < MIN_SENTENCES:
        raise NotEnoughSpeech(len(clips))

    embeddings = [embedding.embed(clip) for _, _, clip in clips]
    voiceprint = average_voiceprint(embedding.model_name, embeddings)
    sentences = tuple(
        SentenceScore(
            recording_index=recording_index,
            start_seconds=start_seconds,
            seconds=clip.duration_ms / 1000,
            score=similarity(voiceprint, sentence_embedding),
        )
        for (recording_index, start_seconds, clip), sentence_embedding in zip(
            clips, embeddings, strict=True
        )
    )
    return Enrollment(voiceprint, sentences)


def score_recordings(
    recordings: Sequence[AudioClip],
    new_vad: Callable[[], Vad],
    verifier: SpeakerVerifier,
) -> tuple[SentenceScore, ...]:
    return tuple(
        SentenceScore(
            recording_index=recording_index,
            start_seconds=start_seconds,
            seconds=clip.duration_ms / 1000,
            score=verifier.score(clip),
        )
        for recording_index, start_seconds, clip in _cut_recordings(recordings, new_vad)
    )


def _cut_recordings(
    recordings: Sequence[AudioClip], new_vad: Callable[[], Vad]
) -> list[tuple[int, float, AudioClip]]:
    clips = []
    for recording_index, recording in enumerate(recordings):
        for start_seconds, clip in _cut_sentences(recording, new_vad()):
            clips.append((recording_index, start_seconds, clip))
    return clips


def _cut_sentences(recording: AudioClip, vad: Vad) -> list[tuple[float, AudioClip]]:
    leading = bytes(_LEADING_SILENCE_MS // FRAME_MS * FRAME_BYTES)
    trailing = bytes(_TRAILING_SILENCE_MS // FRAME_MS * FRAME_BYTES)
    pcm = leading + recording.pcm + trailing

    sentences = []
    for frame_index, frame_start in enumerate(range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)):
        event = vad.feed(AudioFrame(pcm[frame_start : frame_start + FRAME_BYTES]))
        if isinstance(event, SpeechEnd):
            end_ms = (frame_index + 1) * FRAME_MS
            start_ms = end_ms - event.clip.duration_ms - _LEADING_SILENCE_MS
            sentences.append((max(start_ms, 0) / 1000, event.clip))
    return sentences
