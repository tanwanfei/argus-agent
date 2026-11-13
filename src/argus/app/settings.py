import re
from datetime import time
from pathlib import Path
from typing import Literal, Self

import yaml
from dotenv import dotenv_values
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    RootModel,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from argus.app.app_key_file import APP_PUBLIC_KEY_FILE_NAME
from argus.app.voiceprint_file import VOICEPRINT_FILE_NAME
from argus.infra.integrations.weather.qweather import QWeatherCredentials, load_private_key
from argus.infra.llm.openai_compat import REQUEST_BODY_KEYS, THINKING_BODY_KEYS, ThinkingLevel
from argus.kernel.tools import Permission
from argus.llm.budgeted import ModelPricing, TokenPrice
from argus.proactive.decision import DecisionThresholds
from argus.proactive.quota import DailyLimits
from argus.proactive.triggers import TriggerSetting
from argus.routing.intents import IntentRule, IntentTable
from argus.situation.busy import SleepHours

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "argus.yaml"
MIGRATIONS_ROOT = REPO_ROOT / "migrations"


class AudioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_device: str = Field(min_length=1)
    input_channel: int = Field(ge=0)
    output_device: str = Field(min_length=1)


class ZipVoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    num_steps: int = Field(ge=1)
    voice_wav: Path
    voice_text: str = Field(min_length=1)


class CosyVoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    voice: str = Field(min_length=1)


class TtsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: Literal["zipvoice", "cosyvoice"] = "zipvoice"
    zipvoice: ZipVoiceConfig | None = None
    cosyvoice: CosyVoiceConfig | None = None

    @model_validator(mode="after")
    def _check_engine_configured(self) -> Self:
        engine_sections = {"zipvoice": self.zipvoice, "cosyvoice": self.cosyvoice}
        if engine_sections[self.engine] is None:
            raise ValueError(f"engine 选了 {self.engine}，要有 {self.engine} 子段")
        return self


class AliyunAsrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)


class AsrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: Literal["aliyun", "sensevoice"] = "aliyun"
    aliyun: AliyunAsrConfig | None = None

    @model_validator(mode="after")
    def _check_engine_configured(self) -> Self:
        if self.engine == "aliyun" and self.aliyun is None:
            raise ValueError("engine 选了 aliyun，要有 aliyun 子段")
        return self


class WakeWordConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keyword: str = Field(min_length=1)
    max_active_paths: int = Field(ge=1)
    keywords_score: float = Field(gt=0)
    keywords_threshold: float = Field(gt=0, lt=1)


class VadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ask_turn_after_silence_ms: int = Field(gt=0)
    max_silence_ms: int = Field(gt=0)
    use_turn_detector: bool

    @model_validator(mode="after")
    def _check_ask_before_max(self) -> Self:
        if self.ask_turn_after_silence_ms >= self.max_silence_ms:
            raise ValueError("ask_turn_after_silence_ms 要小于 max_silence_ms")
        return self


class SpeakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: float = Field(gt=0, lt=1)
    app_threshold: float = Field(gt=0, lt=1)
    save_app_sentences: bool = False


class HomeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @field_validator("latitude", "longitude")
    @classmethod
    def _at_most_two_decimals(cls, degrees: float) -> float:
        if round(degrees, 2) != degrees:
            raise ValueError("最多两位小数")
        return degrees


class HomeAssistantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    air_conditioner: str | None = None

    temperature_sensor: str | None = None
    humidity_sensor: str | None = None

    @field_validator("air_conditioner")
    @classmethod
    def _is_a_climate_entity(cls, entity_id: str | None) -> str | None:
        if entity_id is not None and not re.fullmatch(r"climate\.[a-z0-9_]+", entity_id):
            raise ValueError("要写成 climate.xxx 的样子：HA 里空调的实体 ID")
        return entity_id

    @field_validator("temperature_sensor", "humidity_sensor")
    @classmethod
    def _is_a_sensor_entity(cls, entity_id: str | None) -> str | None:
        if entity_id is not None and not re.fullmatch(r"sensor\.[a-z0-9_]+", entity_id):
            raise ValueError("要写成 sensor.xxx 的样子：HA 里传感器的实体 ID")
        return entity_id


class CalendarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    write_calendar: str | None = Field(default=None, min_length=1)


class MailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    imap_host: str = Field(min_length=1)
    imap_port: int = Field(default=993, ge=1, le=65535)
    address: str | None = Field(default=None, min_length=1)
    drafts_folder: str | None = Field(default=None, min_length=1)
    smtp_host: str | None = Field(default=None, min_length=1)
    smtp_port: int = Field(default=465, ge=1, le=65535)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    port: int = Field(ge=0, le=65535)


class ProactiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    speak_at: float = Field(default=50, gt=0, le=100)
    notify_at: float = Field(default=20, gt=0, le=100)
    drop_below: float = Field(default=3, ge=0, le=10)
    urgent_at: int = Field(default=8, ge=0, le=10)
    speak_per_day: int = Field(default=3, ge=0)
    notify_per_day: int = Field(default=10, ge=0)
    stage: int = Field(default=1, ge=1, le=4)
    digest_at: tuple[time, ...] = Field(default=(time(7), time(20)), min_length=1)
    sleep: tuple[time, time] = (time(23, 30), time(7))

    @field_validator("digest_at", "sleep", mode="before")
    @classmethod
    def _times_written_as_text(cls, value: object) -> object:
        if isinstance(value, list | tuple) and not all(
            isinstance(each, str | time) for each in value
        ):
            raise ValueError('时刻要带引号写，如 "20:00"')
        return value

    @model_validator(mode="after")
    def _check_notify_below_speak(self) -> Self:
        if self.notify_at >= self.speak_at:
            raise ValueError("notify_at 要小于 speak_at")
        return self

    @model_validator(mode="after")
    def _check_sleep_hours(self) -> Self:
        self.sleep_hours()
        return self

    def thresholds(self) -> DecisionThresholds:
        return DecisionThresholds(
            speak_at=self.speak_at,
            notify_at=self.notify_at,
            drop_below=self.drop_below,
            urgent_at=self.urgent_at,
        )

    def daily_limits(self) -> DailyLimits:
        return DailyLimits(speak_per_day=self.speak_per_day, notify_per_day=self.notify_per_day)

    def sleep_hours(self) -> SleepHours:
        return SleepHours(start=self.sleep[0], end=self.sleep[1])


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_root: Path
    backup_dir: Path | None = None
    disk_free_alert_gb: float = Field(default=10, gt=0)
    app_signature_alert_days: float = Field(default=1, gt=0)
    chat_history_days: int = Field(default=7, gt=0)
    proactive: ProactiveConfig = ProactiveConfig()
    home: HomeConfig
    server: ServerConfig
    audio: AudioConfig | None = None
    tts: TtsConfig | None = None
    asr: AsrConfig | None = None
    wakeword: WakeWordConfig | None = None
    vad: VadConfig | None = None
    speaker: SpeakerConfig | None = None
    home_assistant: HomeAssistantConfig | None = None
    calendar: CalendarConfig | None = None
    mail: MailConfig | None = None

    @property
    def secrets_env_path(self) -> Path:
        return self.data_root / "secrets" / ".env"

    @property
    def profile_path(self) -> Path:
        return self.data_root / "secrets" / "关于我.md"

    @property
    def app_public_key_path(self) -> Path:
        return self.data_root / "secrets" / APP_PUBLIC_KEY_FILE_NAME

    @property
    def voiceprint_path(self) -> Path:
        return self.data_root / "secrets" / VOICEPRINT_FILE_NAME

    @property
    def app_sentences_dir(self) -> Path:
        return self.data_root / "secrets" / "app-sentences"

    @property
    def sandbox_denied_dirs(self) -> tuple[Path, ...]:
        return (self.data_root, REPO_ROOT)

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def data_dir(self) -> Path:
        return self.data_root / "data"

    @property
    def hotwords_path(self) -> Path:
        return self.data_dir / "hotwords.txt"

    @property
    def sensitive_words_path(self) -> Path:
        return self.data_dir / "sensitive_words.txt"

    @property
    def models_dir(self) -> Path:
        return self.data_root / "models"

    @property
    def notes_dir(self) -> Path:
        return self.data_root / "self"


class TokenPriceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_cache_hit: float = Field(ge=0)
    input_cache_miss: float = Field(ge=0)
    output: float = Field(ge=0)

    def to_token_price(self) -> TokenPrice:
        return TokenPrice(
            input_cache_hit=self.input_cache_hit,
            input_cache_miss=self.input_cache_miss,
            output=self.output,
        )


class PriceConfig(TokenPriceConfig):
    peak: TokenPriceConfig | None = None
    peak_hours: tuple[tuple[int, int], ...] = ()

    @model_validator(mode="after")
    def _check_peak(self) -> Self:
        if (self.peak is None) != (not self.peak_hours):
            raise ValueError("peak 和 peak_hours 要么都写，要么都不写")
        for start, end in self.peak_hours:
            if not 0 <= start < end <= 24:
                raise ValueError(f"peak_hours 里的 [{start}, {end}] 不对：要 0 ≤ 开始 < 结束 ≤ 24")
        return self

    def to_pricing(self) -> ModelPricing:
        return ModelPricing(
            regular=self.to_token_price(),
            peak=None if self.peak is None else self.peak.to_token_price(),
            peak_hours=self.peak_hours,
        )


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: HttpUrl
    model: str = Field(min_length=1)
    price: PriceConfig
    proxy: AnyUrl | None = None
    extra_body: dict[str, JsonValue] = Field(default_factory=dict)
    thinking: ThinkingLevel | None = None

    @field_validator("extra_body")
    @classmethod
    def _keep_adapter_fields(cls, extra_body: dict[str, JsonValue]) -> dict[str, JsonValue]:
        overlapping_keys = sorted(REQUEST_BODY_KEYS & extra_body.keys())
        if overlapping_keys:
            raise ValueError(f"不能写 {'、'.join(overlapping_keys)}：这些字段由程序自己填")
        return extra_body

    @model_validator(mode="after")
    def _thinking_has_one_owner(self) -> Self:
        overlapping_keys = sorted(THINKING_BODY_KEYS & self.extra_body.keys())
        if self.thinking is not None and overlapping_keys:
            raise ValueError(
                f"配了 thinking 就不能在 extra_body 里再写 {'、'.join(overlapping_keys)}：思考由 thinking 管"
            )
        return self


class FailoverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first_chunk_timeout_s: float = Field(gt=0)
    cooldown_s: float = Field(ge=0)
    fallback: ProviderConfig


class CodeAgentModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: HttpUrl
    model: str = Field(min_length=1)
    price: PriceConfig


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    monthly_budget_yuan: float = Field(gt=0)
    main: ProviderConfig
    failover: FailoverConfig | None = None
    code_agent: CodeAgentModelConfig | None = None


class IntentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    patterns: list[str] = Field(min_length=1)
    tool: str | None = Field(default=None, min_length=1)
    args: dict[str, str | int | float | bool] = Field(default_factory=dict)
    defaults: dict[str, str | int | float | bool] = Field(default_factory=dict)
    reply: str
    ends_conversation: bool = False


class IntentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    names: list[str] = Field(default_factory=list)
    intents: list[IntentConfig] = Field(default_factory=list)


_PERMISSION_BY_LEVEL = {
    "L1": Permission.L1,
    "L2": Permission.L2,
    "L3a": Permission.L3A,
    "L3b": Permission.L3B,
}


class ToolOverrideConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["L1", "L2", "L3a", "L3b"] | None = Field(default=None, alias="permission")
    personal: bool | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.level is None and self.personal is None:
            raise ValueError("要写 permission 或 personal，至少一项")
        return self

    @property
    def permission(self) -> Permission | None:
        return None if self.level is None else _PERMISSION_BY_LEVEL[self.level]


class CodeAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_turns: int = Field(default=200, gt=0)
    timeout_min: float = Field(default=60, gt=0)
    work_root: Path = Path("D:/ARGUS-work")
    refused_commands: tuple[str, ...] = ()
    keep_days: int = Field(default=7, gt=0)


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    overrides: dict[str, ToolOverrideConfig] = Field(default_factory=dict)
    code_agent: CodeAgentConfig = CodeAgentConfig()


class CodeProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    writable: bool = False


class CodeProjectsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projects: dict[str, CodeProjectConfig] = Field(default_factory=dict)


class TriggerConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    enabled: bool

    def setting(self) -> TriggerSetting:
        return TriggerSetting(enabled=self.enabled, params=dict(self.model_extra or {}))


class TriggersConfig(RootModel[dict[str, TriggerConfig]]):
    pass


class Secrets(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    llm_api_key: SecretStr = Field(alias="LLM_API_KEY")
    dashscope_api_key: SecretStr | None = Field(default=None, alias="DASHSCOPE_API_KEY")
    app_token: SecretStr | None = Field(default=None, alias="APP_TOKEN")
    tencent_map_key: SecretStr | None = Field(default=None, alias="TENCENT_MAP_KEY")
    bark_device_key: SecretStr | None = Field(default=None, alias="BARK_DEVICE_KEY")
    bark_encryption_key: SecretStr | None = Field(default=None, alias="BARK_ENCRYPTION_KEY")
    ha_token: SecretStr | None = Field(default=None, alias="HA_TOKEN")
    caldav_username: str | None = Field(default=None, alias="CALDAV_USERNAME")
    caldav_password: SecretStr | None = Field(default=None, alias="CALDAV_PASSWORD")
    mail_username: str | None = Field(default=None, alias="MAIL_USERNAME")
    mail_password: SecretStr | None = Field(default=None, alias="MAIL_PASSWORD")
    iqs_api_key: SecretStr | None = Field(default=None, alias="IQS_API_KEY")
    amap_key: SecretStr | None = Field(default=None, alias="AMAP_KEY")
    qweather_api_host: str = Field(alias="QWEATHER_API_HOST")
    qweather_developer_id: str = Field(alias="QWEATHER_DEVELOPER_ID")
    qweather_project_id: str = Field(alias="QWEATHER_PROJECT_ID")
    qweather_key_id: str = Field(alias="QWEATHER_KEY_ID")
    qweather_private_key: SecretStr = Field(alias="QWEATHER_PRIVATE_KEY")

    @field_validator("qweather_api_host")
    @classmethod
    def _host_only(cls, host: str) -> str:
        if "/" in host:
            raise ValueError("只填域名，不带 https:// 和路径")
        return host

    @field_validator("app_token")
    @classmethod
    def _long_enough(cls, app_token: SecretStr | None) -> SecretStr | None:
        if app_token is not None and len(app_token.get_secret_value()) < 16:
            raise ValueError("至少 16 个字符")
        return app_token

    @field_validator("bark_encryption_key")
    @classmethod
    def _bark_key_length(cls, bark_key: SecretStr | None) -> SecretStr | None:
        if bark_key is not None and len(bark_key.get_secret_value().encode("utf-8")) != 32:
            raise ValueError("要正好 32 个字符，用 new-bark-key 生成")
        return bark_key

    @model_validator(mode="after")
    def _bark_keys_together(self) -> Self:
        if (self.bark_device_key is None) != (self.bark_encryption_key is None):
            raise ValueError("BARK_DEVICE_KEY 和 BARK_ENCRYPTION_KEY 要一起配")
        return self

    @field_validator("qweather_private_key")
    @classmethod
    def _decodable_private_key(cls, private_key: SecretStr) -> SecretStr:
        load_private_key(private_key.get_secret_value())
        return private_key

    def qweather_credentials(self) -> QWeatherCredentials:
        return QWeatherCredentials(
            api_host=self.qweather_api_host,
            developer_id=self.qweather_developer_id,
            project_id=self.qweather_project_id,
            key_id=self.qweather_key_id,
            private_key=self.qweather_private_key.get_secret_value(),
        )


class ConfigError(Exception):
    pass


def fallback_api_key(models: ModelsConfig, secrets: Secrets) -> SecretStr | None:
    if models.failover is None:
        return None
    if secrets.dashscope_api_key is None:
        raise ConfigError(
            ".env 里没有 DASHSCOPE_API_KEY：models.yaml 配了 failover，故障转移的第二家是阿里云百炼，"
            "用这把密钥。不做故障转移就删掉 models.yaml 的 failover 段"
        )
    return secrets.dashscope_api_key


def check_mail_credentials(config: Config, secrets: Secrets) -> None:
    if config.mail is None:
        return
    missing = [
        key_name
        for key_name, value in (
            ("MAIL_USERNAME", secrets.mail_username),
            ("MAIL_PASSWORD", secrets.mail_password),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(
            f".env 里没有 {'、'.join(missing)}：argus.yaml 配了 mail 段，连邮箱要用它们"
            "。暂时不接邮箱就把那一段注释掉"
        )


def check_calendar_credentials(config: Config, secrets: Secrets) -> None:
    if config.calendar is None:
        return
    missing = [
        key_name
        for key_name, value in (
            ("CALDAV_USERNAME", secrets.caldav_username),
            ("CALDAV_PASSWORD", secrets.caldav_password),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(
            f".env 里没有 {'、'.join(missing)}：argus.yaml 配了 calendar 段，连日历服务要用它们"
            "。暂时不接日历就把那一段注释掉"
        )


def check_home_assistant_token(config: Config, secrets: Secrets) -> None:
    if config.home_assistant is not None and secrets.ha_token is None:
        raise ConfigError(
            ".env 里没有 HA_TOKEN：argus.yaml 配了 home_assistant 段，连 Home Assistant 要用它"
            "。暂时不接家居就把那一段注释掉"
        )


def load_config(path: Path) -> Config:
    return _load_yaml_file(path, Config)


def models_config_path(config_path: Path) -> Path:
    return config_path.with_name("models.yaml")


def load_models_config(path: Path) -> ModelsConfig:
    return _load_yaml_file(path, ModelsConfig)


def intents_path(config_path: Path) -> Path:
    return config_path.with_name("intents.yaml")


def tools_config_path(config_path: Path) -> Path:
    return config_path.with_name("tools.yaml")


def load_tools_config(path: Path) -> ToolsConfig:
    return _load_yaml_file(path, ToolsConfig)


def code_projects_path(config_path: Path) -> Path:
    return config_path.with_name("code_projects.yaml")


def load_code_projects(path: Path) -> CodeProjectsConfig:
    if not path.exists():
        return CodeProjectsConfig()
    return _load_yaml_file(path, CodeProjectsConfig)


def check_code_projects(config: Config, tools: ToolsConfig, projects: CodeProjectsConfig) -> None:
    protected = {
        "ARGUS 代码目录": REPO_ROOT,
        "数据根目录": config.data_root,
    }
    work_root = tools.code_agent.work_root
    for name, directory in protected.items():
        if _overlaps(work_root, directory):
            raise ConfigError(
                f"tools.yaml 的 code_agent.work_root 和{name}重叠：{work_root}，换一个和它们不相干的目录"
            )
    protected["派活的工作根目录"] = work_root
    for alias, project in projects.projects.items():
        for name, directory in protected.items():
            if _overlaps(project.path, directory):
                raise ConfigError(
                    f"code_projects.yaml 的「{alias}」和{name}重叠：{project.path}。"
                    "不能给代理派活改 ARGUS 自己和它的数据，删掉这一条再启动"
                )


def _overlaps(first: Path, second: Path) -> bool:
    first, second = first.resolve(), second.resolve()
    return first.is_relative_to(second) or second.is_relative_to(first)


def triggers_config_path(config_path: Path) -> Path:
    return config_path.with_name("triggers.yaml")


def load_triggers_config(path: Path) -> dict[str, TriggerSetting]:
    config = _load_yaml_file(path, TriggersConfig)
    return {trigger_id: entry.setting() for trigger_id, entry in config.root.items()}


def load_intents(path: Path) -> IntentTable:
    config = _load_yaml_file(path, IntentsConfig)
    rules = []
    for intent in config.intents:
        try:
            rules.append(
                IntentRule(
                    id=intent.id,
                    patterns=tuple(re.compile(pattern) for pattern in intent.patterns),
                    tool=intent.tool,
                    args=intent.args,
                    defaults=intent.defaults,
                    reply=intent.reply,
                    ends_conversation=intent.ends_conversation,
                )
            )
        except (re.error, ValueError) as error:
            raise ConfigError(f"意图表有错：{path}\n  {intent.id}: {error}") from error
    try:
        return IntentTable.build(config.names, rules)
    except ValueError as error:
        raise ConfigError(f"意图表有错：{path}\n  {error}") from error


def persona_path(config_path: Path) -> Path:
    return config_path.with_name("persona.md")


def load_persona(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError(f"读不到人格文件：{path}\n  {error}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"人格文件不是 UTF-8 编码：{path}\n  {error}") from error
    persona = text.strip()
    if not persona:
        raise ConfigError(f"人格文件是空的：{path}")
    return persona


def load_hotwords(path: Path) -> list[str]:
    return _load_word_list(path, "热词文件")


def load_sensitive_words(path: Path) -> list[str]:
    return _load_word_list(path, "敏感词文件")


def _load_word_list(path: Path, file_label: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ConfigError(f"读不到{file_label}：{path}\n  {error}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"{file_label}不是 UTF-8 编码：{path}") from error
    words: list[str] = []
    for line in text.splitlines():
        word = line.strip()
        if word and not word.startswith("#") and word not in words:
            words.append(word)
    return words


def _load_yaml_file[SchemaT: BaseModel](path: Path, schema: type[SchemaT]) -> SchemaT:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"读不到配置文件：{path}\n  {error}") from error

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"配置文件不是合法的 YAML：{path}\n  {error}") from error

    if raw is None:
        raw = {}

    try:
        return schema.model_validate(raw)
    except ValidationError as error:
        problem_lines = []
        for problem in error.errors():
            field_path = ".".join(str(part) for part in problem["loc"])
            if not field_path:
                field_path = "（整个文件）"
            problem_lines.append(f"  {field_path}: {problem['msg']}")
        raise ConfigError(f"配置有错：{path}\n" + "\n".join(problem_lines)) from error


def load_secrets(env_path: Path) -> Secrets:
    try:
        values = dotenv_values(env_path, encoding="utf-8-sig", interpolate=False)
    except OSError as error:
        raise ConfigError(f"读不到密钥文件：{env_path}\n  {error}") from error

    filled_values = {key: value for key, value in values.items() if value}

    try:
        return Secrets.model_validate(filled_values)
    except ValidationError as error:
        problem_lines = []
        for problem in error.errors():
            key_name = ".".join(str(part) for part in problem["loc"])
            if problem["type"] == "missing":
                problem_lines.append(f"  缺少 {key_name}")
            elif not key_name:
                problem_lines.append(f"  {problem['msg']}")
            else:
                problem_lines.append(f"  {key_name}: {problem['msg']}")
        file_note = "" if env_path.is_file() else "（文件不存在）"
        raise ConfigError(
            f"密钥有错：{env_path}{file_note}\n" + "\n".join(problem_lines)
        ) from error
