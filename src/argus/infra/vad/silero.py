from pathlib import Path

import numpy as np
import onnxruntime as ort

from argus.channels.audio import SAMPLE_RATE

_MODEL_FILE_NAME = "silero_vad.onnx"

_WINDOW_SAMPLES = 512
_CONTEXT_SAMPLES = 64
_STATE_SHAPE = (2, 1, 128)

_INT16_FULL_SCALE = 32768


class SileroSpeechDetector:
    def __init__(self, model_dir: Path) -> None:
        model_path = model_dir / _MODEL_FILE_NAME
        if not model_path.is_file():
            raise FileNotFoundError(f"找不到 Silero VAD 模型文件：{model_path}")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT_SAMPLES), dtype=np.float32)
        self._sample_rate = np.array(SAMPLE_RATE, dtype=np.int64)

    @property
    def window_samples(self) -> int:
        return _WINDOW_SAMPLES

    def speech_probability(self, window_pcm: bytes) -> float:
        samples = np.frombuffer(window_pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE
        if len(samples) != _WINDOW_SAMPLES:
            raise ValueError(f"Silero 的窗口要 {_WINDOW_SAMPLES} 个采样，收到 {len(samples)} 个")
        model_input = np.concatenate([self._context, samples[np.newaxis, :]], axis=1)
        probability, self._state = self._session.run(
            None, {"input": model_input, "state": self._state, "sr": self._sample_rate}
        )
        self._context = model_input[:, -_CONTEXT_SAMPLES:]
        return float(probability[0, 0])
