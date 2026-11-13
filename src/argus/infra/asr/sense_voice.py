from pathlib import Path

import numpy as np
import sherpa_onnx

from argus.channels.audio import SAMPLE_RATE, AudioClip

_MODEL_FILE_NAME = "model.int8.onnx"
_TOKENS_FILE_NAME = "tokens.txt"

_INT16_FULL_SCALE = 32768


class SenseVoiceAsr:
    def __init__(self, model_dir: Path, num_threads: int = 4) -> None:
        model_path = model_dir / _MODEL_FILE_NAME
        tokens_path = model_dir / _TOKENS_FILE_NAME
        for required_path in (model_path, tokens_path):
            if not required_path.is_file():
                raise FileNotFoundError(f"找不到 SenseVoice 模型文件：{required_path}")

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            num_threads=num_threads,
            language="zh",
            use_itn=False,
        )

    def transcribe(self, clip: AudioClip) -> str:
        samples = np.frombuffer(clip.pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()
