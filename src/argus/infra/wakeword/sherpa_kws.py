import tempfile
from pathlib import Path

import numpy as np
import sherpa_onnx

from argus.channels.audio import SAMPLE_RATE, AudioFrame

_MODEL_FILE_STEM = "epoch-12-avg-2-chunk-16-left-64"
_TOKENS_FILE_NAME = "tokens.txt"

_NUM_THREADS = 1

_INT16_FULL_SCALE = 32768


class SherpaKeywordWakeWord:
    def __init__(
        self,
        model_dir: Path,
        keyword: str,
        *,
        max_active_paths: int,
        keywords_score: float,
        keywords_threshold: float,
    ) -> None:
        tokens_path = model_dir / _TOKENS_FILE_NAME
        encoder_path = model_dir / f"encoder-{_MODEL_FILE_STEM}.onnx"
        decoder_path = model_dir / f"decoder-{_MODEL_FILE_STEM}.onnx"
        joiner_path = model_dir / f"joiner-{_MODEL_FILE_STEM}.onnx"
        for required_path in (tokens_path, encoder_path, decoder_path, joiner_path):
            if not required_path.is_file():
                raise FileNotFoundError(f"找不到唤醒词模型文件：{required_path}")
        _check_keyword(keyword, tokens_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            keywords_path = Path(temp_dir) / "keywords.txt"
            keywords_path.write_text(keyword + "\n", encoding="utf-8", newline="\n")
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(tokens_path),
                encoder=str(encoder_path),
                decoder=str(decoder_path),
                joiner=str(joiner_path),
                keywords_file=str(keywords_path),
                num_threads=_NUM_THREADS,
                max_active_paths=max_active_paths,
                keywords_score=keywords_score,
                keywords_threshold=keywords_threshold,
            )
        self._stream = self._spotter.create_stream()

    def feed(self, frame: AudioFrame) -> bool:
        samples = np.frombuffer(frame.pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE
        self._stream.accept_waveform(SAMPLE_RATE, samples)
        detected = False
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            if self._spotter.get_result(self._stream):
                detected = True
                self._spotter.reset_stream(self._stream)
        return detected


def _check_keyword(keyword: str, tokens_path: Path) -> None:
    lines = tokens_path.read_text(encoding="utf-8").splitlines()
    known_tokens = {line.split()[0] for line in lines if line.strip()}
    keyword_tokens = keyword.split()
    if not keyword_tokens:
        raise ValueError("唤醒词是空的")
    unknown_tokens = [token for token in keyword_tokens if token not in known_tokens]
    if unknown_tokens:
        raise ValueError(
            f"唤醒词 {keyword!r} 里有模型不认识的音：{' '.join(unknown_tokens)}。"
            f'要把声母、韵母分开写、带声调，如 "ā g é s ī"；能用的音见 {tokens_path}'
        )
