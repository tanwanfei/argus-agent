from pathlib import Path

import numpy as np
import sherpa_onnx

from argus.channels.audio import SAMPLE_RATE, AudioClip

_INT16_FULL_SCALE = 32768


class SherpaSpeakerEmbedding:
    def __init__(self, model_dir: Path, num_threads: int = 1) -> None:
        model_path = model_dir / f"{model_dir.name}.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(f"找不到声纹模型文件：{model_path}")
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=num_threads, provider="cpu"
        )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.model_name = model_dir.name

    def embed(self, clip: AudioClip) -> tuple[float, ...]:
        samples = np.frombuffer(clip.pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            raise ValueError(f"这句太短，算不出声纹：{clip.duration_ms / 1000:.2f} 秒")
        return tuple(self._extractor.compute(stream))
