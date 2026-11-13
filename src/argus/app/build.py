import asyncio
import logging
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from starlette.applications import Starlette

from argus.action.executor import Executor
from argus.action.registry import OverrideOutcome, ToolRegistry
from argus.app.app_key_file import FileAppKey
from argus.app.app_voice import AppVoice
from argus.app.claude_code_hook import claude_code_hook_route
from argus.app.memory_embedding import load_embedder
from argus.app.observability.dashboard import dashboard_page
from argus.app.observability.disk_watch import DiskWatch
from argus.app.observability.home_assistant_watch import HomeAssistantWatch
from argus.app.observability.signature_watch import SignatureWatch
from argus.app.observability.turn_log import SqliteTurnLog
from argus.app.server import HttpServer, build_http_app
from argus.app.settings import (
    CodeProjectsConfig,
    Config,
    MailConfig,
    ModelsConfig,
    ProviderConfig,
    Secrets,
    ToolsConfig,
    fallback_api_key,
)
from argus.app.voice import KeepRunning, SpeechInput, Voice, home_voice
from argus.channels.audio import AudioChunk
from argus.channels.endpoint import Endpoint
from argus.channels.push import PushOutlet
from argus.channels.registry import EndpointRegistry
from argus.codeagent.agent import AgentLimits, CodeAgent, CodeProject
from argus.codeagent.billing import AgentBilling
from argus.codeagent.hooks import ClaudeCodeHooks
from argus.codeagent.permissions import AgentToolGate
from argus.codeagent.runner import CodingAgentRunner, ToolGate
from argus.codeagent.tasks import CodeTask
from argus.codeagent.tools import (
    discard_tool,
    dispatch_tool,
    follow_up_tool,
    publish_tool,
    status_tool,
    stop_tool,
)
from argus.cognition.brain import Brain, TurnMemory
from argus.cognition.memory_context import MemoryContext
from argus.delivery.delivery import Delivery
from argus.delivery.send_to_phone import send_to_phone_tool
from argus.governance.governance import AuditLog, Governance
from argus.governance.pairing import AppPairing
from argus.governance.tickets import Tickets
from argus.infra.clock import SystemClock
from argus.infra.coding.claude_sdk import ClaudeSdkRunner
from argus.infra.coding.workspaces import LocalWorkspaces
from argus.infra.endpoints.app_ws.endpoint import APP_ENDPOINT_ID
from argus.infra.endpoints.app_ws.history import ChatHistoryReader
from argus.infra.endpoints.bark import BarkPush
from argus.infra.event_bus import InProcessEventBus
from argus.infra.ids import UuidIdGen
from argus.infra.integrations.calendar.client import CalDavClient
from argus.infra.integrations.calendar.tools import edit_events_tools, list_events_tool
from argus.infra.integrations.calendar.upcoming import CalendarWatch
from argus.infra.integrations.homeassistant.client import HomeAssistantClient
from argus.infra.integrations.homeassistant.tools import air_conditioner_tools, room_climate_tool
from argus.infra.integrations.mail.account import MailAccount, MailSender
from argus.infra.integrations.mail.imap_client import ImapMailAccount
from argus.infra.integrations.mail.smtp_client import SmtpMailSender
from argus.infra.integrations.mail.tools import mail_draft_tools, mail_read_tools, mail_send_tool
from argus.infra.integrations.maps.amap import AmapClient
from argus.infra.integrations.maps.nearby_tool import place_nearby_tool
from argus.infra.integrations.maps.route_tool import place_route_tool
from argus.infra.integrations.maps.tencent import TencentReverseGeocoder
from argus.infra.integrations.search.iqs import IqsSearchClient
from argus.infra.integrations.search.tools import search_web_tool
from argus.infra.integrations.weather.alerts import WeatherAlertWatch
from argus.infra.integrations.weather.morning import MORNING_FORECAST_KIND, MorningForecastCheck
from argus.infra.integrations.weather.qweather import QWeatherClient
from argus.infra.integrations.weather.tools import weather_tool
from argus.infra.knowledge.birthday_watch import BirthdayWatch
from argus.infra.knowledge.self_files import SelfFilesSource
from argus.infra.llm.openai_compat import OpenAICompatProvider
from argus.infra.sqlite.app_signature import SqliteAppSignatureStore
from argus.infra.sqlite.backup import DailyBackup
from argus.infra.sqlite.code_task import SqliteCodeTaskStore
from argus.infra.sqlite.database import SqliteDatabase
from argus.infra.sqlite.doc_chunk import SqliteChunkStore
from argus.infra.sqlite.doc_source import SqliteCursorStore
from argus.infra.sqlite.gov_audit import SqliteAuditLog
from argus.infra.sqlite.gov_ticket import SqliteTicketStore
from argus.infra.sqlite.llm_cost import SqliteCostLedger
from argus.infra.sqlite.mem_visit import SqliteVisitLog
from argus.infra.sqlite.memory_store import SqliteMemoryStore
from argus.infra.sqlite.pro_dedupe import SqliteSeenKeys
from argus.infra.sqlite.pro_digest import SqliteDigestQueue
from argus.infra.sqlite.pro_mute import SqliteMutedDays
from argus.infra.sqlite.pro_place_reminder import SqlitePlaceReminders
from argus.infra.sqlite.pro_quota import SqliteQuotaStore
from argus.infra.sqlite.pro_todo import SqliteTodoList
from argus.infra.sqlite.pro_trigger import SqliteUselessCounts
from argus.infra.sqlite.pro_usefulness import SqliteUsefulnessLog
from argus.infra.sqlite.sch_job import SqliteJobStore
from argus.infra.sqlite.ses_history import SqliteChatHistoryStore
from argus.infra.sqlite.ses_message import SqliteSessionStore
from argus.infra.sqlite.sit_place import SqlitePlaceStore
from argus.infra.sqlite.vector_index import SqliteVectorIndex
from argus.kernel.clock import Clock
from argus.kernel.events import (
    BirthdayToday,
    CalendarUpcoming,
    CodeTaskFinished,
    CodeTaskNeedsAttention,
    EventBus,
    GeofenceEntered,
    GeofenceExited,
    MemoryForgotten,
    ProactiveDelivered,
    TodayForecast,
    TurnCompleted,
    TurnRecorded,
    WeatherAlert,
)
from argus.kernel.ids import IdGen
from argus.kernel.tools import Permission, ToolFn
from argus.knowledge.index import NotesIndex
from argus.knowledge.sync import KnowledgeSync
from argus.knowledge.tools import notes_search_tool
from argus.llm.budgeted import Budgeted, ModelPricing, TokenPrice
from argus.llm.failover import Failover, Vendor
from argus.llm.gateway import ModelGateway
from argus.llm.probe import CloudProbe
from argus.llm.redacting import Redacting
from argus.llm.redaction import Redactor
from argus.memory.extraction import EXTRACT_KIND, ExtractionQueue, ExtractionWorker
from argus.memory.extractor import Extractor
from argus.memory.location_habits import LocationHabitJob
from argus.memory.memory import Memory
from argus.memory.ports import Embedder
from argus.memory.tools import forget_tool, remember_tools, search_tool
from argus.memory.visits import VisitRecorder
from argus.proactive.birthday import birthday_today
from argus.proactive.code_listening import CODE_LISTENING_TRIGGERS, CodeTaskDetails
from argus.proactive.daily_digest import DIGEST_KIND, DailyDigest
from argus.proactive.dedupe import SeenKeys
from argus.proactive.deferred_answers import DEFERRED_ANSWER_TRIGGERS
from argus.proactive.digest import DigestQueue
from argus.proactive.feedback import UselessCounts
from argus.proactive.host_alerts import HOST_ALERT_TRIGGERS
from argus.proactive.meeting_soon import meeting_soon
from argus.proactive.mute import MutedDays, mute_tools
from argus.proactive.pipeline import Proactive
from argus.proactive.place_reminder import (
    PlaceReminderBell,
    manage_place_reminder_tools,
    place_reminder_due,
    place_reminder_tools,
)
from argus.proactive.quota import QuotaStore
from argus.proactive.reminder import (
    REMINDER_KIND,
    ReminderAlarm,
    manage_reminder_tools,
    reminder_due,
    reminder_missed,
    reminder_tools,
)
from argus.proactive.timer import TIMER_KIND, TimerAlarm, timer_due, timer_missed, timer_tools
from argus.proactive.today_weather import today_weather
from argus.proactive.todo import TodoList, todo_tools
from argus.proactive.triggers import EnabledTriggers, Trigger, TriggerSetting
from argus.proactive.usefulness import UsefulnessLog
from argus.proactive.weather_alert import weather_alert
from argus.routing.intents import IntentTable
from argus.routing.router import Router
from argus.scheduler.scheduler import JobHandler, Scheduler
from argus.session.deferred import DEFERRED_TURN, DeferredTurns
from argus.session.end_conversation import end_conversation_tool
from argus.session.history import ChatHistoryStore
from argus.session.session import Session
from argus.session.store import SessionStore
from argus.situation.busy import BusyNow
from argus.situation.geocoding import ReverseGeocoder
from argus.situation.geofence_watch import GeofenceWatch
from argus.situation.guest_tools import guests_tool
from argus.situation.home_presence import HomePresence
from argus.situation.others_present import OthersPresence
from argus.situation.place_tools import place_tools
from argus.situation.places import PlaceStore
from argus.situation.situation import Situation
from argus.speech.pipeline import UtteranceHandler

logger = logging.getLogger(__name__)

CloseResource = Callable[[], Awaitable[None]]


class ChatHistory(ChatHistoryStore, ChatHistoryReader, Protocol):
    pass


@dataclass(frozen=True)
class Overrides:
    clock: Clock | None = None
    ids: IdGen | None = None
    llm: ModelGateway | None = None
    memory: TurnMemory | None = None
    extraction_handler: JobHandler | None = None
    embedder: Embedder | None = None
    voice: Voice | None = None
    audit_log: AuditLog | None = None
    session_store: SessionStore | None = None
    chat_history: ChatHistory | None = None
    places: PlaceStore | None = None
    todo: TodoList | None = None
    geocoder: ReverseGeocoder | None = None
    weather: QWeatherClient | None = None
    home_assistant: HomeAssistantClient | None = None
    calendar: CalDavClient | None = None
    mail: MailAccount | None = None
    mail_sender: MailSender | None = None
    search: IqsSearchClient | None = None
    amap: AmapClient | None = None
    push: PushOutlet | None = None
    text_endpoints: Sequence[Endpoint] = ()
    extra_tools: Sequence[tuple[ToolFn, ToolFn | None]] = ()
    seen_keys: SeenKeys | None = None
    quota: QuotaStore | None = None
    useless_counts: UselessCounts | None = None
    usefulness_log: UsefulnessLog | None = None
    digest_queue: DigestQueue | None = None
    muted_days: MutedDays | None = None
    extra_triggers: Sequence[Trigger] = ()
    coding_agent: CodingAgentRunner | None = None


class Argus:
    def __init__(
        self,
        clock: Clock,
        ids: IdGen,
        bus: InProcessEventBus,
        session: Session,
        endpoints: EndpointRegistry,
        speech_inputs: Mapping[str, SpeechInput],
        keep_running: Sequence[KeepRunning],
        close_on_shutdown: Sequence[CloseResource],
        http_app: Starlette,
    ) -> None:
        self.clock = clock
        self.ids = ids
        self.bus = bus
        self.session = session
        self.endpoints = endpoints
        self.http_app = http_app
        self._speech_inputs = speech_inputs
        self._keep_running = keep_running
        self._close_on_shutdown = close_on_shutdown
        self._task_tree_ended = False

    @property
    def task_tree_ended(self) -> bool:
        return self._task_tree_ended

    async def run(self, http_socket: socket.socket | None = None) -> None:
        try:
            await self.session.restore_working_memory()
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self.bus.run(), name="event-bus")
                task_group.create_task(self.session.run(), name="session")
                if http_socket is not None:
                    http_server = HttpServer(self.http_app, http_socket)
                    task_group.create_task(http_server.serve(), name="http-server")
                for endpoint_id, speech_input in self._speech_inputs.items():
                    task_group.create_task(speech_input.listen(), name=f"speech-{endpoint_id}")
                for job in self._keep_running:
                    task_group.create_task(job(), name="keep-running")
                task_group.create_task(
                    self.session.prepare_name_only_reply(), name="name-only-reply"
                )
                never_set = asyncio.Event()
                await never_set.wait()
        finally:
            self._task_tree_ended = True
            for close in reversed(self._close_on_shutdown):
                await close()


def _code_agent(
    code_projects: CodeProjectsConfig | None,
    tools: ToolsConfig | None,
    models: ModelsConfig | None,
    secrets: Secrets | None,
    runner: CodingAgentRunner | None,
    *,
    argus_db: SqliteDatabase,
    clock: Clock,
    ids: IdGen,
    gate_for: Callable[[CodeTask], ToolGate],
    bus: EventBus,
) -> CodeAgent | None:
    if code_projects is None or not code_projects.projects:
        logger.info("没有 code_projects.yaml 或里面没有目录，不能派活")
        return None
    code_agent_config = (tools or ToolsConfig()).code_agent
    model_config = models.code_agent if models is not None else None
    if runner is None:
        if model_config is None or secrets is None:
            logger.info("models.yaml 里没有 code_agent 段，不能派活")
            return None
        runner = ClaudeSdkRunner(
            base_url=str(model_config.base_url),
            api_key=secrets.llm_api_key,
            model=model_config.model,
            config_dir=code_agent_config.work_root / ".claude-config",
        )
    pricing = (
        model_config.price.to_pricing()
        if model_config is not None
        else ModelPricing(regular=TokenPrice(input_cache_hit=0, input_cache_miss=0, output=0))
    )
    billing = AgentBilling(
        ledger=SqliteCostLedger(argus_db),
        pricing=pricing,
        monthly_budget_yuan=models.monthly_budget_yuan if models is not None else float("inf"),
        clock=clock,
    )
    return CodeAgent(
        projects={
            name: CodeProject(path=project.path, writable=project.writable)
            for name, project in code_projects.projects.items()
        },
        limits=AgentLimits(
            max_turns=code_agent_config.max_turns,
            timeout_s=code_agent_config.timeout_min * 60,
            keep_for=timedelta(days=code_agent_config.keep_days),
        ),
        runner=runner,
        workspaces=LocalWorkspaces(code_agent_config.work_root),
        store=SqliteCodeTaskStore(argus_db),
        billing=billing,
        gate_for=gate_for,
        bus=bus,
        clock=clock,
        ids=ids,
    )


def _apply_tool_overrides(tools: ToolsConfig | None, registry: ToolRegistry) -> None:
    if tools is None:
        return
    for name, override in tools.overrides.items():
        outcome = registry.override(
            name, permission=override.permission, personal=override.personal
        )
        match outcome:
            case OverrideOutcome.APPLIED:
                logger.info("tools.yaml 覆盖了工具 %s", name)
                if override.permission is Permission.L2 and registry.undo_of(name) is None:
                    logger.info(
                        "工具 %s 调成了 L2，可它没有撤销工具：照样执行，说撤销撤不了它", name
                    )
            case OverrideOutcome.UNKNOWN_TOOL:
                logger.error("tools.yaml 覆盖的工具 %s 没有注册，这条不认", name)
            case OverrideOutcome.WITHDRAWN:
                logger.error(
                    "tools.yaml 把工具 %s 调成了 L3，可它没有确认模板，这次不提供这个工具",
                    name,
                )


def mail_from_config(
    config: MailConfig, secrets: Secrets
) -> tuple[ImapMailAccount, SmtpMailSender | None]:
    assert secrets.mail_username is not None and secrets.mail_password is not None
    password = secrets.mail_password.get_secret_value()
    account = ImapMailAccount(
        config.imap_host,
        config.imap_port,
        secrets.mail_username,
        password,
        address=config.address,
        drafts_folder=config.drafts_folder,
    )
    if config.smtp_host is None:
        return account, None
    sender = SmtpMailSender(
        config.smtp_host,
        config.smtp_port,
        secrets.mail_username,
        password,
        address=config.address,
    )
    return account, sender


def _usable_intents(
    intents: IntentTable | None, registry: ToolRegistry, *, optional_tool_prefixes: tuple[str, ...]
) -> IntentTable:
    if intents is None:
        return IntentTable.build([], [])
    usable = []
    for rule in intents.rules:
        if rule.tool is not None and registry.get(rule.tool) is None:
            if rule.tool.startswith(optional_tool_prefixes):
                logger.info("意图 %s 要调的工具 %s 没配，这条先不认", rule.id, rule.tool)
            else:
                logger.error(
                    "intents.yaml 的意图 %s 要调的工具 %s 没有注册，这条不认", rule.id, rule.tool
                )
        else:
            usable.append(rule)
    return IntentTable(intents.names, tuple(usable))


def build(
    config: Config,
    overrides: Overrides | None = None,
    *,
    persona: str,
    intents: IntentTable | None = None,
    tools: ToolsConfig | None = None,
    triggers: Mapping[str, TriggerSetting] | None = None,
    models: ModelsConfig | None = None,
    secrets: Secrets | None = None,
    sensitive_words: Sequence[str] = (),
    code_projects: CodeProjectsConfig | None = None,
) -> Argus:
    if overrides is None:
        overrides = Overrides()
    if overrides.llm is None and (models is None or secrets is None):
        raise ValueError(
            "没换掉模型时要传 models 和 secrets：生产环境的模型要 models.yaml 里的厂商、单价和 .env 里的密钥"
        )
    if overrides.weather is None and secrets is None:
        raise ValueError("没换掉天气时要传 secrets：和风天气的凭据在 .env 里")
    if (
        overrides.home_assistant is None
        and config.home_assistant is not None
        and (secrets is None or secrets.ha_token is None)
    ):
        raise ValueError("配了 home_assistant 段、没换掉 HA 客户端时要传带 HA_TOKEN 的 secrets")
    if (
        overrides.calendar is None
        and config.calendar is not None
        and (secrets is None or secrets.caldav_username is None or secrets.caldav_password is None)
    ):
        raise ValueError("配了 calendar 段、没换掉日历客户端时要传带 CALDAV 账号的 secrets")
    if (
        overrides.mail is None
        and config.mail is not None
        and (secrets is None or secrets.mail_username is None or secrets.mail_password is None)
    ):
        raise ValueError("配了 mail 段、没换掉邮箱时要传带 MAIL 账号的 secrets")
    clock = overrides.clock if overrides.clock is not None else SystemClock()
    ids = overrides.ids if overrides.ids is not None else UuidIdGen()
    bus = InProcessEventBus()

    if overrides.voice is not None:
        voice = overrides.voice
    elif config.audio is None:
        logger.warning("argus.yaml 里没有 audio 段，不接家里的阵列")
        voice = Voice(_NothingToSynthesize(), (), _no_speech_input)
    else:
        voice = home_voice(config, secrets, clock)

    close_on_shutdown: list[CloseResource] = []
    if overrides.weather is not None:
        weather = overrides.weather
    else:
        assert secrets is not None
        weather = QWeatherClient(secrets.qweather_credentials(), clock=clock)
    close_on_shutdown.append(weather.aclose)
    home_assistant: HomeAssistantClient | None = None
    if overrides.home_assistant is not None:
        home_assistant = overrides.home_assistant
    elif config.home_assistant is not None:
        assert secrets is not None and secrets.ha_token is not None
        home_assistant = HomeAssistantClient(
            str(config.home_assistant.url), secrets.ha_token.get_secret_value()
        )
    if home_assistant is not None:
        close_on_shutdown.append(home_assistant.aclose)
    calendar: CalDavClient | None = None
    if overrides.calendar is not None:
        calendar = overrides.calendar
    elif config.calendar is not None:
        assert secrets is not None
        assert secrets.caldav_username is not None and secrets.caldav_password is not None
        calendar = CalDavClient(
            str(config.calendar.url),
            secrets.caldav_username,
            secrets.caldav_password.get_secret_value(),
        )
    if calendar is not None:
        close_on_shutdown.append(calendar.aclose)
    mail: MailAccount | None = None
    mail_sender: MailSender | None = None
    if overrides.mail is not None:
        mail = overrides.mail
        mail_sender = overrides.mail_sender
    elif config.mail is not None:
        assert secrets is not None
        mail, mail_sender = mail_from_config(config.mail, secrets)
    search: IqsSearchClient | None = None
    if overrides.search is not None:
        search = overrides.search
    elif secrets is not None and secrets.iqs_api_key is not None:
        search = IqsSearchClient(secrets.iqs_api_key.get_secret_value())
    else:
        logger.info(".env 里没有 IQS_API_KEY，不提供联网搜索")
    if search is not None:
        close_on_shutdown.append(search.aclose)
    amap: AmapClient | None = None
    if overrides.amap is not None:
        amap = overrides.amap
    elif secrets is not None and secrets.amap_key is not None:
        amap = AmapClient(secrets.amap_key.get_secret_value())
    else:
        logger.info(".env 里没有 AMAP_KEY，不提供周边搜索和路线")
    if amap is not None:
        close_on_shutdown.append(amap.aclose)
    geocoder: ReverseGeocoder | None = None
    if overrides.geocoder is not None:
        geocoder = overrides.geocoder
    elif secrets is not None and secrets.tencent_map_key is not None:
        tencent = TencentReverseGeocoder(secrets.tencent_map_key.get_secret_value())
        close_on_shutdown.append(tencent.aclose)
        geocoder = tencent
    else:
        logger.info(".env 里没有 TENCENT_MAP_KEY，不反查地名")
    push: PushOutlet | None = None
    if overrides.push is not None:
        push = overrides.push
    elif (
        secrets is not None
        and secrets.bark_device_key is not None
        and secrets.bark_encryption_key is not None
    ):
        bark = BarkPush(
            secrets.bark_device_key.get_secret_value(),
            secrets.bark_encryption_key.get_secret_value(),
        )
        close_on_shutdown.append(bark.aclose)
        push = bark
    else:
        logger.info(
            ".env 里没有 BARK_DEVICE_KEY、BARK_ENCRYPTION_KEY，告警、没送到的文字回复只记日志"
        )
    argus_db = SqliteDatabase(config.data_dir / "argus.db")
    close_on_shutdown.append(argus_db.close)
    bus.subscribe(TurnCompleted, SqliteTurnLog(argus_db).on_turn_completed)
    audit_db: SqliteDatabase | None = None
    if overrides.audit_log is not None:
        audit_log = overrides.audit_log
    else:
        audit_db = SqliteDatabase(config.data_dir / "audit.db")
        close_on_shutdown.append(audit_db.close)
        audit_log = SqliteAuditLog(audit_db)
    keep_running = list(voice.keep_running)
    disk_dirs = {"data": config.data_root}
    if config.backup_dir is None:
        logger.info("argus.yaml 里没有 backup_dir，不备份库")
    else:
        backup = DailyBackup(config.data_dir, config.backup_dir, ("argus", "audit"), clock, bus)
        keep_running.append(backup.run)
        disk_dirs["backup"] = config.backup_dir
    disk_watch = DiskWatch(disk_dirs, config.disk_free_alert_gb, clock, bus)
    keep_running.append(disk_watch.run)
    app_signature_store = SqliteAppSignatureStore(argus_db)
    signature_watch = SignatureWatch(
        app_signature_store, timedelta(days=config.app_signature_alert_days), clock, bus
    )
    keep_running.append(signature_watch.run)
    home_assistant_watch: HomeAssistantWatch | None = None
    if home_assistant is not None:
        home_assistant_watch = HomeAssistantWatch(home_assistant, clock, bus)
        keep_running.append(home_assistant_watch.run)
    app_pairing = AppPairing(FileAppKey(config.app_public_key_path))
    if app_pairing.public_key is None:
        logger.info("App 还没配对：L3b 的卡片确认要等 App 上报公钥")
    else:
        logger.info("App 公钥已登记")
    app_token = None
    if secrets is not None and secrets.app_token is not None:
        app_token = secrets.app_token.get_secret_value()
    else:
        logger.info(".env 里没有 APP_TOKEN，App 连不上")

    if overrides.llm is not None:
        llm: ModelGateway = overrides.llm
    else:
        assert models is not None and secrets is not None
        llm, providers = main_gateway(models, secrets, sensitive_words, argus_db, clock, bus)
        close_on_shutdown.extend(provider.aclose for provider in providers)
    keep_running.append(CloudProbe(llm, clock).run)

    endpoints = [*voice.endpoints, *overrides.text_endpoints]
    endpoint_registry = EndpointRegistry()
    for endpoint in endpoints:
        endpoint_registry.register(endpoint)
    speaker_endpoint_id = voice.endpoints[0].id if voice.endpoints else None
    places = overrides.places if overrides.places is not None else SqlitePlaceStore(argus_db)
    geofence_watch = GeofenceWatch(places, clock, bus)
    keep_running.append(geofence_watch.run)
    home_presence = HomePresence(
        geofence_watch,
        clock,
        home_mic_endpoint_ids=frozenset(endpoint.id for endpoint in voice.endpoints),
    )
    bus.subscribe(TurnCompleted, home_presence.on_turn_completed)
    others_presence = OthersPresence(
        clock, home_mic_endpoint_ids=frozenset(endpoint.id for endpoint in voice.endpoints)
    )
    bus.subscribe(TurnCompleted, others_presence.on_turn_completed)
    delivery = Delivery(
        voice.synthesizer,
        endpoint_registry,
        clock=clock,
        push=push,
        speaker_endpoint_id=speaker_endpoint_id,
        app_endpoint_id=APP_ENDPOINT_ID,
        at_home=home_presence.at_home,
        others_present=others_presence.present,
        cards_approvable=lambda: app_pairing.public_key is not None,
        bus=bus,
    )
    keep_running.append(delivery.run)
    scheduler = Scheduler(SqliteJobStore(argus_db), clock=clock, ids=ids)
    registry = ToolRegistry()
    registry.register(
        weather_tool(
            weather, home_latitude=config.home.latitude, home_longitude=config.home.longitude
        )
    )
    if search is not None:
        registry.register(search_web_tool(search, Redactor(sensitive_words), clock))
    if amap is not None:
        for maps_tool in (place_nearby_tool, place_route_tool):
            registry.register(
                maps_tool(
                    amap,
                    Redactor(sensitive_words),
                    places,
                    home_latitude=config.home.latitude,
                    home_longitude=config.home.longitude,
                )
            )
    registry.register(send_to_phone_tool(delivery, phone_endpoint_id=APP_ENDPOINT_ID))
    set_timer, cancel_timer = timer_tools(scheduler, clock)
    registry.register(set_timer, undo=cancel_timer)
    alarm = TimerAlarm(bus, clock)
    scheduler.register(TIMER_KIND, alarm.ring, on_expired=alarm.missed)
    set_reminder, cancel_reminder = reminder_tools(scheduler, clock, ids)
    registry.register(set_reminder, undo=cancel_reminder)
    place_reminders = SqlitePlaceReminders(argus_db)
    list_reminders, remove_reminder, restore_reminder = manage_reminder_tools(
        scheduler, place_reminders
    )
    registry.register(list_reminders)
    registry.register(remove_reminder, undo=restore_reminder)
    reminders = ReminderAlarm(bus, clock, scheduler)
    scheduler.register(REMINDER_KIND, reminders.ring, on_expired=reminders.missed)
    set_place_reminder, cancel_place_reminder = place_reminder_tools(
        place_reminders, places, clock, ids
    )
    registry.register(set_place_reminder, undo=cancel_place_reminder)
    remove_place_reminder, restore_place_reminder = manage_place_reminder_tools(place_reminders)
    registry.register(remove_place_reminder, undo=restore_place_reminder)
    place_reminder_bell = PlaceReminderBell(place_reminders, bus)
    bus.subscribe(GeofenceEntered, place_reminder_bell.on_entered)
    bus.subscribe(GeofenceExited, place_reminder_bell.on_exited)
    enabled_triggers = EnabledTriggers.build(
        [
            timer_due,
            timer_missed,
            reminder_due,
            reminder_missed,
            place_reminder_due,
            meeting_soon,
            weather_alert,
            today_weather,
            birthday_today,
            *HOST_ALERT_TRIGGERS,
            *CODE_LISTENING_TRIGGERS,
            *DEFERRED_ANSWER_TRIGGERS,
            *overrides.extra_triggers,
        ],
        triggers or {},
    )
    digest_queue = (
        overrides.digest_queue
        if overrides.digest_queue is not None
        else SqliteDigestQueue(argus_db)
    )
    muted_days = (
        overrides.muted_days if overrides.muted_days is not None else SqliteMutedDays(argus_db)
    )
    calendar_watch = (
        CalendarWatch(calendar, clock, bus)
        if calendar is not None and CalendarUpcoming in enabled_triggers.event_types()
        else None
    )
    busy = BusyNow(config.proactive.sleep_hours(), calendar_watch)
    proactive = Proactive(
        triggers=enabled_triggers,
        seen=overrides.seen_keys if overrides.seen_keys is not None else SqliteSeenKeys(argus_db),
        quota=overrides.quota if overrides.quota is not None else SqliteQuotaStore(argus_db),
        useless=overrides.useless_counts
        if overrides.useless_counts is not None
        else SqliteUselessCounts(argus_db),
        usefulness=overrides.usefulness_log
        if overrides.usefulness_log is not None
        else SqliteUsefulnessLog(argus_db),
        digest=digest_queue,
        muted=muted_days,
        busy=busy.at,
        thresholds=config.proactive.thresholds(),
        limits=config.proactive.daily_limits(),
        delivery=delivery,
        clock=clock,
        ids=ids,
    )
    for event_type in enabled_triggers.event_types():
        bus.subscribe(event_type, proactive.on_event)
    if calendar_watch is not None:
        keep_running.append(calendar_watch.run)
    if WeatherAlert in enabled_triggers.event_types():
        keep_running.append(
            WeatherAlertWatch(
                weather,
                clock,
                bus,
                latitude=config.home.latitude,
                longitude=config.home.longitude,
            ).run
        )
    if TodayForecast in enabled_triggers.event_types():
        morning_forecast = MorningForecastCheck(
            weather,
            scheduler,
            bus,
            clock,
            latitude=config.home.latitude,
            longitude=config.home.longitude,
        )
        scheduler.register(
            MORNING_FORECAST_KIND, morning_forecast.check, on_expired=morning_forecast.missed
        )
        keep_running.append(morning_forecast.schedule_first)
    if BirthdayToday in enabled_triggers.event_types():
        keep_running.append(BirthdayWatch(config.profile_path, clock, bus).run)
    daily_digest = DailyDigest(
        digest_queue, delivery, scheduler, times=config.proactive.digest_at, clock=clock
    )
    scheduler.register(DIGEST_KIND, daily_digest.send, on_expired=daily_digest.missed)
    keep_running.append(daily_digest.schedule_next)
    embedder = overrides.embedder or load_embedder(config.models_dir)
    memory_store = SqliteMemoryStore(argus_db)
    keep_running.append(memory_store.warn_if_vectors_mismatch)
    long_term_memory = Memory(
        store=memory_store,
        clock=clock,
        embedder=embedder,
        index=SqliteVectorIndex(argus_db),
    )
    remember, unremember = remember_tools(long_term_memory)
    registry.register(remember, undo=unremember)
    registry.register(search_tool(long_term_memory))
    registry.register(forget_tool(long_term_memory, events=bus, clock=clock))
    extraction_queue = ExtractionQueue(scheduler=scheduler)
    extraction_worker = ExtractionWorker(extractor=Extractor(llm), memory=long_term_memory)
    scheduler.register(
        EXTRACT_KIND,
        overrides.extraction_handler
        if overrides.extraction_handler is not None
        else extraction_worker.handle,
    )
    bus.subscribe(TurnRecorded, extraction_queue.on_turn_recorded)
    visits = SqliteVisitLog(argus_db)
    visit_recorder = VisitRecorder(visits)
    bus.subscribe(GeofenceEntered, visit_recorder.on_entered)
    bus.subscribe(GeofenceExited, visit_recorder.on_exited)
    keep_running.append(LocationHabitJob(visits, memory_store, clock).run)
    if not config.notes_dir.is_dir():
        logger.info("数据根目录下没有 self/，不提供资料检索")
    elif embedder is None:
        logger.warning("向量化模型没加载上，self/ 里的资料查不了")
    else:
        docs_db = SqliteDatabase(config.data_dir / "docs.db")
        close_on_shutdown.append(docs_db.close)
        notes_index = NotesIndex(store=SqliteChunkStore(docs_db), embedder=embedder)
        registry.register(notes_search_tool(notes_index))
        startup_sync = notes_sync(config.notes_dir, docs_db, notes_index)
        keep_running.append(lambda: _sync_notes_at_startup(startup_sync))
    save_place, restore_place = place_tools(SqlitePlaceStore(argus_db), clock)
    registry.register(save_place, undo=restore_place)
    registry.register(guests_tool(others_presence))
    home_config = config.home_assistant
    if home_assistant is None or home_config is None:
        logger.info("argus.yaml 里没有配 home_assistant，不提供家居工具")
    else:
        if home_config.air_conditioner is not None:
            air_conditioner = air_conditioner_tools(home_assistant, home_config.air_conditioner)
            registry.register(air_conditioner.turn_on, undo=air_conditioner.restore)
            registry.register(air_conditioner.turn_off, undo=air_conditioner.restore)
            registry.register(air_conditioner.set_temperature, undo=air_conditioner.restore)
        if home_config.temperature_sensor is not None or home_config.humidity_sensor is not None:
            registry.register(
                room_climate_tool(
                    home_assistant,
                    temperature_entity=home_config.temperature_sensor,
                    humidity_entity=home_config.humidity_sensor,
                )
            )
    todos = todo_tools(
        overrides.todo if overrides.todo is not None else SqliteTodoList(argus_db), clock
    )
    registry.register(todos.list_all)
    registry.register(todos.add, undo=todos.remove)
    registry.register(todos.done, undo=todos.reopen)
    mute_today, unmute = mute_tools(muted_days, clock)
    registry.register(mute_today, undo=unmute)
    registry.register(unmute, undo=mute_today)
    registry.register(end_conversation_tool())
    if calendar is None or config.calendar is None:
        logger.info("argus.yaml 里没有配 calendar，不提供日历工具")
    else:
        registry.register(list_events_tool(calendar, clock))
        add_event, remove_event = edit_events_tools(
            calendar, clock, ids, write_calendar=config.calendar.write_calendar
        )
        registry.register(add_event, undo=remove_event)
    if mail is None:
        logger.info("argus.yaml 里没有配 mail，不提供邮件工具")
    else:
        list_mail, read_mail = mail_read_tools(mail, clock)
        registry.register(list_mail)
        registry.register(read_mail)
        draft_mail, discard_draft = mail_draft_tools(mail, clock, ids)
        registry.register(draft_mail, undo=discard_draft)
        if mail_sender is not None:
            registry.register(mail_send_tool(mail_sender, clock, ids))
    code_agent = _code_agent(
        code_projects,
        tools,
        models,
        secrets,
        overrides.coding_agent,
        argus_db=argus_db,
        clock=clock,
        ids=ids,
        gate_for=lambda task: gate_for(task),
        bus=bus,
    )
    if code_agent is not None:
        registry.register(dispatch_tool(code_agent))
        stop = stop_tool(code_agent)
        registry.register(stop)
        registry.register(follow_up_tool(code_agent), undo=stop)
        registry.register(status_tool(code_agent))
        registry.register(publish_tool(code_agent))
        registry.register(discard_tool(code_agent))
        keep_running.append(code_agent.run)
        code_task_details = CodeTaskDetails(delivery.push_text)
        bus.subscribe(CodeTaskFinished, code_task_details.on_finished)
        bus.subscribe(CodeTaskNeedsAttention, code_task_details.on_needs_attention)
    for extra_tool, undo in overrides.extra_tools:
        registry.register(extra_tool, undo=undo)
    _apply_tool_overrides(tools, registry)
    router = Router(
        _usable_intents(intents, registry, optional_tool_prefixes=("ha.",)),
        cloud_health=llm.health,
    )
    governance = Governance(
        audit_log=audit_log, clock=clock, proactive_stage=config.proactive.stage
    )
    tickets = Tickets(SqliteTicketStore(argus_db), clock=clock, ids=ids, card_key=app_pairing)
    executor = Executor(registry=registry, governance=governance, tickets=tickets, clock=clock)
    code_agent_config = (tools or ToolsConfig()).code_agent

    def gate_for(task: CodeTask) -> ToolGate:
        return AgentToolGate(
            task_id=task.id,
            workspace=task.workspace.path,
            temp_dir=task.workspace.temp_dir,
            protected_dirs=config.sandbox_denied_dirs,
            refused_commands=code_agent_config.refused_commands,
            governance=governance,
            ids=ids,
        )

    brain = Brain(
        llm,
        persona,
        registry=registry,
        executor=executor,
        clock=clock,
        memory=overrides.memory
        if overrides.memory is not None
        else MemoryContext(long_term_memory),
    )
    deferred = DeferredTurns(scheduler, brain, llm.health, bus, clock=clock, ids=ids)
    scheduler.register(DEFERRED_TURN, deferred.run)
    keep_running.append(scheduler.run)
    session_store = (
        overrides.session_store
        if overrides.session_store is not None
        else SqliteSessionStore(argus_db)
    )
    chat_history = (
        overrides.chat_history
        if overrides.chat_history is not None
        else SqliteChatHistoryStore(argus_db, clock, timedelta(days=config.chat_history_days))
    )
    situation = Situation(geocoder=geocoder, places=places, clock=clock, geofences=geofence_watch)
    session = Session(
        clock,
        ids,
        bus,
        brain,
        delivery,
        router=router,
        executor=executor,
        deferred=deferred,
        store=session_store,
        history=chat_history,
        situation=situation,
        feedback=proactive,
        mute_state=muted_days,
        home_latitude=config.home.latitude,
        home_longitude=config.home.longitude,
    )
    bus.subscribe(MemoryForgotten, session.on_memory_forgotten)
    bus.subscribe(ProactiveDelivered, session.on_proactive_delivered)
    speech_inputs: dict[str, SpeechInput] = {}
    for endpoint in voice.endpoints:
        speech_input = voice.speech_input(endpoint, session)
        session.add_endpoint(endpoint.id, speech_input)
        speech_inputs[endpoint.id] = speech_input
    app_voice = None
    if voice.app_speech_input is not None:
        app_voice = AppVoice(endpoint_registry, session, voice.app_speech_input)
    http_app = build_http_app(
        endpoints,
        disk_dirs,
        app_token=app_token,
        handle_app_endpoint=None if app_voice is None else app_voice.serve,
        home_assistant_status=None if home_assistant_watch is None else home_assistant_watch.report,
        clock=clock,
        app_signature_store=app_signature_store,
        app_location_sink=situation,
        app_chat_history=chat_history,
        app_pairing=app_pairing,
        app_pushed_cards=delivery,
        claude_code_hook=claude_code_hook_route(ClaudeCodeHooks(bus, clock)),
        dashboard=None
        if models is None
        else dashboard_page(
            argus_db,
            audit_db,
            clock=clock,
            monthly_budget_yuan=models.monthly_budget_yuan,
            daily_limits=config.proactive.daily_limits(),
        ),
    )
    return Argus(
        clock,
        ids,
        bus,
        session,
        endpoint_registry,
        speech_inputs,
        keep_running,
        close_on_shutdown,
        http_app,
    )


def notes_sync(notes_dir: Path, docs_db: SqliteDatabase, index: NotesIndex) -> KnowledgeSync:
    return KnowledgeSync(
        sources=[SelfFilesSource(notes_dir)], cursors=SqliteCursorStore(docs_db), sink=index
    )


async def _sync_notes_at_startup(sync: KnowledgeSync) -> None:
    try:
        await sync.sync()
    except Exception:
        logger.exception(
            "启动时同步资料出错，资料检索先用上次同步的；查清后用命令行 sync-notes 再同步"
        )


def main_gateway(
    models: ModelsConfig,
    secrets: Secrets,
    sensitive_words: Sequence[str],
    argus_db: SqliteDatabase,
    clock: Clock,
    bus: EventBus,
) -> tuple[ModelGateway, tuple[OpenAICompatProvider, ...]]:
    main_tier = models.main
    failover = models.failover
    main_provider = _provider(
        main_tier,
        secrets.llm_api_key.get_secret_value(),
        first_chunk_timeout_s=None if failover is None else failover.first_chunk_timeout_s,
    )
    providers = [main_provider]
    pricing = {main_tier.model: main_tier.price.to_pricing()}
    vendors = [Vendor(main_tier.model, main_provider)]
    fallback_key = fallback_api_key(models, secrets)
    if failover is not None and fallback_key is not None:
        fallback_provider = _provider(
            failover.fallback, fallback_key.get_secret_value(), first_chunk_timeout_s=None
        )
        providers.append(fallback_provider)
        pricing[failover.fallback.model] = failover.fallback.price.to_pricing()
        vendors.append(Vendor(failover.fallback.model, fallback_provider))
    budgeted = Budgeted(
        Failover(
            vendors,
            cooldown_s=0 if failover is None else failover.cooldown_s,
            clock=clock,
            bus=bus,
        ),
        ledger=SqliteCostLedger(argus_db),
        pricing=pricing,
        estimate_model=main_tier.model,
        monthly_budget_yuan=models.monthly_budget_yuan,
        clock=clock,
        bus=bus,
    )
    return Redacting(budgeted, redactor=Redactor(sensitive_words)), tuple(providers)


def _provider(
    tier: ProviderConfig, api_key: str, *, first_chunk_timeout_s: float | None
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=str(tier.base_url),
        model=tier.model,
        api_key=api_key,
        proxy=None if tier.proxy is None else str(tier.proxy),
        extra_body=tier.extra_body,
        thinking_level=tier.thinking,
        first_chunk_timeout_s=first_chunk_timeout_s,
    )


class _NothingToSynthesize:
    def synthesize(self, sentence: str) -> AsyncGenerator[AudioChunk]:
        raise RuntimeError("没配 audio 段，主进程没有能出声的端点，不该合成")


def _no_speech_input(endpoint: Endpoint, handler: UtteranceHandler) -> SpeechInput:
    raise RuntimeError(f"没配 audio 段，端点 {endpoint.id} 没有语音入口可接")
