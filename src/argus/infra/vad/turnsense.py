import math
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np
import onnxruntime as ort

from argus.channels.audio import SAMPLE_RATE, SAMPLE_WIDTH, AudioClip
from argus.speech.vad import TurnVerdict

_MODEL_FILE_NAME = "model_int8.onnx"
_CMVN_FILE_NAME = "am.mvn"

_NUM_THREADS = 4
_MAX_SECONDS = 8

_LFR_STACKED_FRAMES = 7
_LFR_STRIDE = 6

_VERDICT_BY_OUTPUT_INDEX = (TurnVerdict.FINISHED, TurnVerdict.UNFINISHED, TurnVerdict.INVALID)


class TurnSenseDetector:
    def __init__(self, model_dir: Path) -> None:
        model_path = model_dir / _MODEL_FILE_NAME
        cmvn_path = model_dir / _CMVN_FILE_NAME
        for required_path in (model_path, cmvn_path):
            if not required_path.is_file():
                raise FileNotFoundError(f"找不到 TurnSense 模型文件：{required_path}")

        options = ort.SessionOptions()
        options.intra_op_num_threads = _NUM_THREADS
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self._cmvn_shift, self._cmvn_scale = _load_cmvn(cmvn_path)

        self._fbank_options = knf.FbankOptions()
        self._fbank_options.frame_opts.samp_freq = SAMPLE_RATE
        self._fbank_options.frame_opts.dither = 0.0
        self._fbank_options.frame_opts.window_type = "hamming"
        self._fbank_options.frame_opts.frame_shift_ms = 10.0
        self._fbank_options.frame_opts.frame_length_ms = 25.0
        self._fbank_options.frame_opts.snip_edges = True
        self._fbank_options.mel_opts.num_bins = 80
        self._fbank_options.energy_floor = 0

    def judge(self, clip: AudioClip) -> TurnVerdict:
        features = self._features(clip.pcm[-_MAX_SECONDS * SAMPLE_RATE * SAMPLE_WIDTH :])
        if features is None:
            return TurnVerdict.INVALID
        logits = self._session.run(
            None,
            {
                "feats": features[np.newaxis, ...],
                "feat_lengths": np.array([len(features)], dtype=np.int64),
            },
        )[0][0]
        return _VERDICT_BY_OUTPUT_INDEX[int(np.argmax(logits))]

    def _features(self, pcm: bytes) -> np.ndarray | None:
        fbank = knf.OnlineFbank(self._fbank_options)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        fbank.accept_waveform(SAMPLE_RATE, samples.tolist())
        if fbank.num_frames_ready == 0:
            return None
        frames = np.array(
            [fbank.get_frame(index) for index in range(fbank.num_frames_ready)], dtype=np.float32
        )

        padded = np.vstack([np.tile(frames[0], ((_LFR_STACKED_FRAMES - 1) // 2, 1)), frames])
        stacked_rows = []
        for output_index in range(math.ceil(len(frames) / _LFR_STRIDE)):
            group_start = output_index * _LFR_STRIDE
            group = padded[group_start : group_start + _LFR_STACKED_FRAMES]
            if len(group) < _LFR_STACKED_FRAMES:
                missing = _LFR_STACKED_FRAMES - len(group)
                group = np.vstack([group, np.tile(padded[-1], (missing, 1))])
            stacked_rows.append(group.reshape(-1))
        stacked = np.vstack(stacked_rows)
        return ((stacked + self._cmvn_shift) * self._cmvn_scale).astype(np.float32)


def _load_cmvn(cmvn_path: Path) -> tuple[np.ndarray, np.ndarray]:
    lines = cmvn_path.read_text(encoding="utf-8").splitlines()
    shift = scale = None
    for line, next_line in zip(lines, [*lines[1:], ""], strict=True):
        words, next_words = line.split(), next_line.split()
        if not words or next_words[:1] != ["<LearnRateCoef>"]:
            continue
        if words[0] == "<AddShift>":
            shift = np.array(next_words[3:-1], dtype=np.float64)
        elif words[0] == "<Rescale>":
            scale = np.array(next_words[3:-1], dtype=np.float64)
    if shift is None or scale is None:
        raise ValueError(
            f"{cmvn_path} 里找不到 <AddShift> 和 <Rescale> 两段，不是 TurnSense 的 am.mvn"
        )
    return shift, scale
