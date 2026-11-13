import json
import warnings
from pathlib import Path

import soundfile
import torch
import torchaudio
from safetensors.torch import load_model
from vocos import Vocos
from zipvoice.models.zipvoice_distill import ZipVoiceDistill
from zipvoice.tokenizer.tokenizer import EmiliaTokenizer
from zipvoice.utils.feature import VocosFbank

from argus.channels.audio import SAMPLE_RATE, AudioChunk
from argus.infra.tts.speakable_text import has_speakable_text, spell_out_numbers

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Couldn't find ffmpeg", category=RuntimeWarning)
    from zipvoice.utils.infer import add_punctuation, remove_silence, rms_norm

_MODEL_SAMPLE_RATE = 24_000
_GUIDANCE_SCALE = 3.0
_T_SHIFT = 0.5
_TARGET_RMS = 0.1
_FEAT_SCALE = 0.1
_SPEED = 1.0
_PROMPT_TRAILING_SILENCE_MS = 200

_OUTPUT_RMS = 0.1
_OUTPUT_PEAK_LIMIT = 0.95

_INT16_FULL_SCALE = 32768


class ZipVoiceTts:
    def __init__(
        self,
        model_dir: Path,
        vocoder_dir: Path,
        voice_wav: Path,
        voice_text: str,
        num_steps: int = 8,
    ) -> None:
        model_files = [
            model_dir / name for name in ("model.json", "model.safetensors", "tokens.txt")
        ]
        vocoder_files = [vocoder_dir / name for name in ("config.yaml", "pytorch_model.bin")]
        for required_path in [*model_files, *vocoder_files, voice_wav]:
            if not required_path.is_file():
                raise FileNotFoundError(f"找不到 TTS 要用的文件：{required_path}")
        if not torch.cuda.is_available():
            raise RuntimeError(
                "没有可用的显卡（torch.cuda.is_available() 为 False），TTS 要用显卡推理"
            )

        self._device = torch.device("cuda", 0)
        self._num_steps = num_steps
        self._tokenizer = EmiliaTokenizer(token_file=str(model_dir / "tokens.txt"))
        self._model = self._load_model(model_dir)
        self._vocoder = self._load_vocoder(vocoder_dir)
        self._feature_extractor = VocosFbank()
        self._load_voice(voice_wav, voice_text)
        self.synthesize(voice_text)

    def synthesize(self, sentence: str) -> AudioChunk:
        sentence = spell_out_numbers(sentence)
        if not has_speakable_text(sentence):
            return AudioChunk(b"")
        with torch.inference_mode():
            tokens_str = self._tokenizer.texts_to_tokens([add_punctuation(sentence)])[0]
            tokens = self._tokenizer.tokens_to_token_ids([tokens_str])
            features, features_lens, _, _ = self._model.sample(
                tokens=tokens,
                prompt_tokens=self._prompt_tokens,
                prompt_features=self._prompt_features,
                prompt_features_lens=self._prompt_features_lens,
                speed=_SPEED,
                t_shift=_T_SHIFT,
                duration="predict",
                num_step=self._num_steps,
                guidance_scale=_GUIDANCE_SCALE,
            )
            features = features.permute(0, 2, 1) / _FEAT_SCALE
            wav = self._vocoder.decode(features[:, :, : features_lens[0]]).squeeze(1)
            wav = _normalize_loudness(wav)
            wav = remove_silence(wav, _MODEL_SAMPLE_RATE, only_edge=True, trail_sil=0)
            wav = torchaudio.functional.resample(wav, _MODEL_SAMPLE_RATE, SAMPLE_RATE)
        pcm = (wav * _INT16_FULL_SCALE).round().clamp(-_INT16_FULL_SCALE, _INT16_FULL_SCALE - 1)
        return AudioChunk(pcm.to(torch.int16).numpy().tobytes())

    def _load_model(self, model_dir: Path) -> ZipVoiceDistill:
        model_config = json.loads((model_dir / "model.json").read_text(encoding="utf-8"))
        model = ZipVoiceDistill(
            **model_config["model"],
            vocab_size=self._tokenizer.vocab_size,
            pad_id=self._tokenizer.pad_id,
        )
        load_model(model, str(model_dir / "model.safetensors"))
        return model.to(self._device).eval()

    def _load_vocoder(self, vocoder_dir: Path) -> Vocos:
        vocoder = Vocos.from_hparams(str(vocoder_dir / "config.yaml"))
        state_dict = torch.load(
            vocoder_dir / "pytorch_model.bin", map_location="cpu", weights_only=True
        )
        vocoder.load_state_dict(state_dict)
        return vocoder.to(self._device).eval()

    def _load_voice(self, voice_wav: Path, voice_text: str) -> None:
        samples, wav_sample_rate = soundfile.read(voice_wav, dtype="float32", always_2d=True)
        prompt_wav = torch.from_numpy(samples).T.contiguous()
        if wav_sample_rate != _MODEL_SAMPLE_RATE:
            prompt_wav = torchaudio.functional.resample(
                prompt_wav, wav_sample_rate, _MODEL_SAMPLE_RATE
            )
        prompt_wav = remove_silence(
            prompt_wav,
            _MODEL_SAMPLE_RATE,
            only_edge=False,
            trail_sil=_PROMPT_TRAILING_SILENCE_MS,
        )
        prompt_wav, _ = rms_norm(prompt_wav, _TARGET_RMS)
        prompt_features = self._feature_extractor.extract(
            prompt_wav, sampling_rate=_MODEL_SAMPLE_RATE
        )
        self._prompt_features = (prompt_features.unsqueeze(0) * _FEAT_SCALE).to(self._device)
        self._prompt_features_lens = torch.full(
            (1,), self._prompt_features.size(1), device=self._device
        )
        prompt_tokens_str = self._tokenizer.texts_to_tokens([add_punctuation(voice_text)])[0]
        self._prompt_tokens = self._tokenizer.tokens_to_token_ids([prompt_tokens_str])


def _normalize_loudness(wav: torch.Tensor) -> torch.Tensor:
    peak = torch.max(torch.abs(wav)).item()
    if peak == 0:
        return wav
    rms = torch.sqrt(torch.mean(torch.square(wav))).item()
    gain = min(_OUTPUT_RMS / rms, _OUTPUT_PEAK_LIMIT / peak)
    return wav * gain
