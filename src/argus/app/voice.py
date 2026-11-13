import logging
from collections.abc import Callable, Coroutine, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from argus.app.app_sentences import SavingVerifier
from argus.app.settings import (
    AsrConfig,
    Config,
    ConfigError,
    Secrets,
    SpeakerConfig,
    TtsConfig,
    VadConfig,
    WakeWordConfig,
    ZipVoiceConfig,
    load_hotwords,
)
from argus.app.voiceprint_file import (
    VoiceprintFileError,
    load_voiceprint,
    named_voiceprint_paths,
)
from argus.channels.endpoint import Endpoint
from argus.delivery.delivery import SentenceSynthesizer
from argus.infra.asr.dashscope_vocabulary import HotwordError, HotwordVocabulary
from argus.infra.asr.fun_asr_realtime import FunAsrRealtime
from argus.infra.endpoints.home_mic import HomeMicEndpoint
from argus.infra.speaker.verifier import VoiceprintVerifier
from argus.infra.tts.cosyvoice import CosyVoiceTts
from argus.kernel.clock import Clock
from argus.session.session import WakeControl
from argus.speech.inference import (
    FallbackAsr,
    Recognizer,
    StreamingAsr,
    Synthesizer,
    WholeClipAsr,
)
from argus.speech.pipeline import SpeechPipeline, UtteranceHandler
from argus.speech.speaker import SpeakerCheck, SpeakerVerifier
from argus.speech.vad import TurnTakingVad

if TYPE_CHECKING:
    from argus.infra.asr.sense_voice import SenseVoiceAsr
    from argus.infra.speaker.sherpa_embedding import SherpaSpeakerEmbedding
    from argus.infra.tts.zip_voice import ZipVoiceTts
    from argus.infra.wakeword.sherpa_kws import SherpaKeywordWakeWord

logger = logging.getLogger(__name__)

HOME_MIC_ENDPOINT_ID = "home.livingroom"

_ASR_MODEL_DIR_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
_WAKEWORD_MODEL_DIR_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
_SPEECH_DETECTOR_MODEL_DIR_NAME = "silero-vad"
_TURN_DETECTOR_MODEL_DIR_NAME = "turnsense-1.0"
_SPEAKER_MODEL_DIR_NAME = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced"


class SpeechInput(WakeControl, Protocol):
    async def listen(self) -> None: ...


SpeechInputFactory = Callable[[Endpoint, UtteranceHandler], SpeechInput]

KeepRunning = Callable[[], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class Voice:
    synthesizer: SentenceSynthesizer
    endpoints: Sequence[Endpoint]
    speech_input: SpeechInputFactory
    keep_running: Sequence[KeepRunning] = ()
    app_speech_input: SpeechInputFactory | None = None


class VoiceSetupError(Exception):
    pass


def home_voice(config: Config, secrets: Secrets | None, clock: Clock) -> Voice:
    audio, tts, asr_config = config.audio, config.tts, config.asr
    wakeword, vad, speaker = config.wakeword, config.vad, config.speaker
    sections = {
        "audio": audio,
        "tts": tts,
        "asr": asr_config,
        "wakeword": wakeword,
        "vad": vad,
        "speaker": speaker,
    }
    if (
        audio is None
        or tts is None
        or asr_config is None
        or wakeword is None
        or vad is None
        or speaker is None
    ):
        missing = "、".join(name for name, section in sections.items() if section is None)
        raise VoiceSetupError(
            f"接家里的阵列要 {'、'.join(sections)} 六段一起配，argus.yaml 里缺 {missing} 段"
        )

    dashscope_api_key = _dashscope_key(config, tts, asr_config, secrets)
    synthesizer, tts_keep_running = _home_synthesizer(config, tts, dashscope_api_key)

    from argus.infra.audio.capture import MicrophoneCapture
    from argus.infra.audio.devices import AudioDeviceError, find_wasapi_device
    from argus.infra.audio.playback import SpeakerPlayback
    from argus.infra.audio.system_devices import list_devices, refresh_devices

    try:
        devices = list_devices()
        find_wasapi_device(devices, audio.input_device, "input")
        find_wasapi_device(devices, audio.output_device, "output")
    except AudioDeviceError as error:
        raise VoiceSetupError(f"阵列接不上。{error}") from error

    models_dir = config.models_dir
    asr, asr_keep_running = _home_asr(config, asr_config, dashscope_api_key, clock)
    speaker_check, app_speaker_check = speaker_checks(config, speaker, clock)

    def pipeline_for(
        endpoint: Endpoint,
        handler: UtteranceHandler,
        *,
        speaker_check: SpeakerCheck,
        check_speaker_early: bool = False,
    ) -> SpeechInput:
        try:
            wake_word = load_wake_word(models_dir, wakeword)
            turn_taking_vad = load_vad(models_dir, vad)
        except (
            OSError,
            ValueError,
        ) as error:
            raise VoiceSetupError(f"语音模型加载失败。{error}") from error
        pipeline = SpeechPipeline(
            endpoint.id,
            wake_word,
            turn_taking_vad,
            asr,
            handler,
            clock,
            speaker_check,
            check_speaker_early=check_speaker_early,
        )
        return ListeningPipeline(pipeline, endpoint)

    def home_pipeline_for(endpoint: Endpoint, handler: UtteranceHandler) -> SpeechInput:
        return pipeline_for(endpoint, handler, speaker_check=speaker_check)

    def app_pipeline_for(endpoint: Endpoint, handler: UtteranceHandler) -> SpeechInput:
        return pipeline_for(
            endpoint, handler, speaker_check=app_speaker_check, check_speaker_early=True
        )

    home_mic = HomeMicEndpoint(
        HOME_MIC_ENDPOINT_ID,
        MicrophoneCapture(audio.input_device, audio.input_channel),
        SpeakerPlayback(audio.output_device, clock),
        clock,
        refresh_devices,
    )
    return Voice(
        synthesizer,
        [home_mic],
        home_pipeline_for,
        [*tts_keep_running, *asr_keep_running],
        app_speech_input=app_pipeline_for,
    )


def speaker_checks(
    config: Config, speaker: SpeakerConfig, clock: Clock
) -> tuple[SpeakerCheck, SpeakerCheck]:
    named_paths = named_voiceprint_paths(config.voiceprint_path)
    try:
        home_voiceprint = load_voiceprint(config.voiceprint_path)
        named_voiceprints = [load_voiceprint(path) for path in named_paths.values()]
        embedding = load_speaker_embedding(config.models_dir)
        home_verifier = VoiceprintVerifier(embedding, [home_voiceprint])
        app_verifier: SpeakerVerifier = VoiceprintVerifier(
            embedding, [home_voiceprint, *named_voiceprints]
        )
    except (VoiceprintFileError, ValueError) as error:
        logger.warning("家里的阵列认不了人，每句都当不是你。%s", error)
        home_check = SpeakerCheck(None, speaker.threshold)
        return home_check, home_check.for_app(None, speaker.app_threshold)
    except OSError as error:
        raise VoiceSetupError(f"声纹模型加载失败。{error}") from error
    logger.info("家里的阵列按声纹认人，阈值 %.2f", speaker.threshold)
    logger.info(
        "App 那条路按声纹认人，阈值 %.2f，比 %d 份声纹（%s）取最高的",
        speaker.app_threshold,
        1 + len(named_voiceprints),
        "、".join(["家里阵列", *named_paths]),
    )
    if speaker.save_app_sentences:
        app_verifier = SavingVerifier(app_verifier, config.app_sentences_dir, clock)
        logger.warning(
            "App 那条路比过的每句存进 %s，旁边人的声音也会存：注册完删掉，把 save_app_sentences 关掉",
            config.app_sentences_dir,
        )
    home_check = SpeakerCheck(home_verifier, speaker.threshold)
    return home_check, home_check.for_app(app_verifier, speaker.app_threshold)


def _dashscope_key(
    config: Config, tts: TtsConfig, asr: AsrConfig, secrets: Secrets | None
) -> str | None:
    cloud_choices = []
    if tts.engine == "cosyvoice":
        cloud_choices.append("tts.engine 选了 cosyvoice")
    if asr.engine == "aliyun":
        cloud_choices.append("asr.engine 选了 aliyun")
    if not cloud_choices:
        return None
    api_key = None if secrets is None else secrets.dashscope_api_key
    if api_key is None:
        raise VoiceSetupError(
            f"{'、'.join(cloud_choices)}，密钥文件里要有 DASHSCOPE_API_KEY：{config.secrets_env_path}"
        )
    return api_key.get_secret_value()


def _home_asr(
    config: Config, asr: AsrConfig, dashscope_api_key: str | None, clock: Clock
) -> tuple[StreamingAsr, list[KeepRunning]]:
    try:
        local = Recognizer(load_asr(config.models_dir))
    except OSError as error:
        raise VoiceSetupError(f"语音模型加载失败。{error}") from error

    if asr.engine == "aliyun":
        assert asr.aliyun is not None and dashscope_api_key is not None
        cloud_asr = FunAsrRealtime(dashscope_api_key, asr.aliyun.model)
        logger.info(
            "识别用阿里云 %s：说给它听的那句边说边传，出问题时改用本地 SenseVoice", asr.aliyun.model
        )
        jobs: list[KeepRunning] = [cloud_asr.keep_connected]
        try:
            hotwords = load_hotwords(config.hotwords_path)
        except ConfigError as error:
            raise VoiceSetupError(str(error)) from error
        if hotwords:
            vocabulary = HotwordVocabulary(dashscope_api_key, asr.aliyun.model)
            jobs.append(lambda: sync_hotwords(vocabulary, hotwords, cloud_asr))
        else:
            logger.info("没有热词：%s 不存在或是空的", config.hotwords_path)
        return FallbackAsr(cloud_asr, local, clock), jobs

    logger.info("识别用本地 SenseVoice：声音不出门")
    return WholeClipAsr(local), []


async def sync_hotwords(
    vocabulary: HotwordVocabulary, hotwords: list[str], cloud_asr: FunAsrRealtime
) -> None:
    try:
        vocabulary_id = await vocabulary.sync(hotwords)
    except HotwordError as error:
        logger.warning("热词表没同步好，这次先不带热词：%s", error)
        return
    cloud_asr.use_vocabulary(vocabulary_id)
    logger.info("热词表同步好了：%d 个词", len(hotwords))


def _home_synthesizer(
    config: Config, tts: TtsConfig, dashscope_api_key: str | None
) -> tuple[SentenceSynthesizer, list[KeepRunning]]:
    if tts.engine == "cosyvoice":
        assert tts.cosyvoice is not None and dashscope_api_key is not None
        cloud_tts = CosyVoiceTts(dashscope_api_key, tts.cosyvoice.model, tts.cosyvoice.voice)
        return cloud_tts, [cloud_tts.keep_connected]

    assert tts.zipvoice is not None
    try:
        return Synthesizer(load_tts(config.models_dir, tts.zipvoice)), []
    except (OSError, RuntimeError) as error:
        raise VoiceSetupError(f"语音模型加载失败。{error}") from error


class ListeningPipeline:
    def __init__(self, pipeline: SpeechPipeline, endpoint: Endpoint) -> None:
        self._pipeline = pipeline
        self._endpoint = endpoint

    def set_wake_required(self, required: bool) -> None:
        self._pipeline.set_wake_required(required)

    async def listen(self) -> None:
        async with aclosing(self._endpoint.frames()) as frames:
            await self._pipeline.run(frames)


def load_asr(models_dir: Path) -> "SenseVoiceAsr":
    from argus.infra.asr.sense_voice import SenseVoiceAsr

    return SenseVoiceAsr(models_dir / "asr" / _ASR_MODEL_DIR_NAME)


def load_speaker_embedding(models_dir: Path) -> "SherpaSpeakerEmbedding":
    from argus.infra.speaker.sherpa_embedding import SherpaSpeakerEmbedding

    return SherpaSpeakerEmbedding(models_dir / "speaker" / _SPEAKER_MODEL_DIR_NAME)


def load_speaker_verifier(models_dir: Path, voiceprint_path: Path) -> VoiceprintVerifier:
    voiceprint = load_voiceprint(voiceprint_path)
    return VoiceprintVerifier(load_speaker_embedding(models_dir), [voiceprint])


def load_tts(models_dir: Path, tts_config: ZipVoiceConfig) -> "ZipVoiceTts":
    from argus.infra.tts.zip_voice import ZipVoiceTts

    tts_models_dir = models_dir / "tts"
    return ZipVoiceTts(
        model_dir=tts_models_dir / "zipvoice_distill",
        vocoder_dir=tts_models_dir / "vocos-mel-24khz",
        voice_wav=tts_config.voice_wav,
        voice_text=tts_config.voice_text,
        num_steps=tts_config.num_steps,
    )


def load_wake_word(models_dir: Path, wakeword_config: WakeWordConfig) -> "SherpaKeywordWakeWord":
    from argus.infra.wakeword.sherpa_kws import SherpaKeywordWakeWord

    return SherpaKeywordWakeWord(
        models_dir / "kws" / _WAKEWORD_MODEL_DIR_NAME,
        wakeword_config.keyword,
        max_active_paths=wakeword_config.max_active_paths,
        keywords_score=wakeword_config.keywords_score,
        keywords_threshold=wakeword_config.keywords_threshold,
    )


def load_vad(models_dir: Path, vad_config: VadConfig) -> TurnTakingVad:
    from argus.infra.vad.silero import SileroSpeechDetector
    from argus.infra.vad.turnsense import TurnSenseDetector

    vad_models_dir = models_dir / "vad"
    speech_detector = SileroSpeechDetector(vad_models_dir / _SPEECH_DETECTOR_MODEL_DIR_NAME)
    turn_detector = (
        TurnSenseDetector(vad_models_dir / _TURN_DETECTOR_MODEL_DIR_NAME)
        if vad_config.use_turn_detector
        else None
    )
    return TurnTakingVad(
        speech_detector,
        turn_detector,
        ask_turn_after_silence_ms=vad_config.ask_turn_after_silence_ms,
        max_silence_ms=vad_config.max_silence_ms,
    )
