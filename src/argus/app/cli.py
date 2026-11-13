import argparse
import asyncio
import secrets
import sys
import threading
import time
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from argus.app.build import main_gateway, notes_sync
from argus.app.memory_embedding import load_embedder
from argus.app.observability.turn_log import SqliteTurnLog, TurnLatency
from argus.app.settings import (
    DEFAULT_CONFIG_PATH,
    MIGRATIONS_ROOT,
    AudioConfig,
    Config,
    ConfigError,
    ModelsConfig,
    Secrets,
    fallback_api_key,
    load_config,
    load_models_config,
    load_persona,
    load_secrets,
    load_sensitive_words,
    models_config_path,
    persona_path,
)
from argus.app.voice import (
    load_asr,
    load_speaker_embedding,
    load_speaker_verifier,
    load_tts,
    load_vad,
    load_wake_word,
)
from argus.app.voice_calibration import (
    SHORT_SENTENCE_SECONDS,
    highest_threshold_within_target,
    rates_at,
)
from argus.app.voice_enroll import (
    SUSPICIOUS_SIMILARITY,
    Enrollment,
    NotEnoughSpeech,
    SentenceScore,
    enroll,
    score_recordings,
)
from argus.app.voiceprint_file import (
    VoiceprintFileError,
    named_voiceprint_path,
    save_voiceprint,
)
from argus.channels.audio import (
    CHANNELS,
    FRAME_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioChunk,
    AudioClip,
    AudioFrame,
)
from argus.channels.push import PushError, PushLevel
from argus.cognition.context_builder import build_context
from argus.cognition.sentence_splitter import SentenceSplitter
from argus.infra.audio.devices import AudioDeviceError
from argus.infra.clock import SystemClock
from argus.infra.endpoints.bark import BarkPush
from argus.infra.event_bus import InProcessEventBus
from argus.infra.ids import UuidIdGen
from argus.infra.integrations.maps.tencent import TencentReverseGeocoder
from argus.infra.sqlite.database import SqliteDatabase
from argus.infra.sqlite.doc_chunk import SqliteChunkStore
from argus.infra.sqlite.llm_cost import SqliteCostLedger
from argus.infra.sqlite.memory_store import SqliteMemoryStore
from argus.infra.sqlite.migrator import MigrationError, migrate_all
from argus.infra.sqlite.sit_place import SqlitePlaceStore
from argus.kernel.geo import GeoFix
from argus.kernel.tools import Speaker
from argus.kernel.tracing import trace
from argus.knowledge.index import NotesIndex
from argus.knowledge.sync import SyncReport
from argus.llm.budgeted import budget_month
from argus.llm.gateway import (
    LlmError,
    LlmRequest,
    LlmResponse,
    Message,
    ModelGateway,
    Purpose,
    Role,
    TextDelta,
    Tier,
)
from argus.memory.memory import Memory, ProfileImport
from argus.memory.ports import Embedder
from argus.memory.profile import ProfileEntry, parse_profile
from argus.situation.coordinates import (
    Gcj02Point,
    Wgs84Point,
    gcj02_to_wgs84,
    looks_swapped,
    wgs84_to_gcj02,
)
from argus.situation.geocoding import ReverseGeocodeError
from argus.situation.places import DEFAULT_RADIUS_M, Place, describe_location
from argus.speech.inference import Recognizer, Synthesizer
from argus.speech.speaker import speaker_for
from argus.speech.vad import SpeechEnd, SpeechStart, Vad
from argus.speech.wakeword import WakeWord

if TYPE_CHECKING:
    from argus.infra.audio.capture import MicrophoneCapture
    from argus.infra.audio.playback import SpeakerPlayback

_PLAY_CHUNK_BYTES = SAMPLE_RATE // 10 * SAMPLE_WIDTH

_CHAT_ENDPOINT = "cli_chat"

_HAND_TYPED_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
_APP_TOKEN_GROUPS = 4
_APP_TOKEN_GROUP_LENGTH = 5
_BARK_KEY_LENGTH = 32
_BARK_KEY_FIRST_CHARACTERS = "23456789"
_BARK_KEY_SHOWN_GROUP_LENGTH = 4


class _Playback(Protocol):
    async def play(self, chunks: AsyncIterator[AudioChunk]) -> None: ...


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m argus.app.cli", description="ARGUS 命令行工具")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="主配置文件，默认是仓库里的 config/argus.yaml",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    record_parser = subcommands.add_parser("record", help="用收音设备录一段，存成 wav")
    record_parser.add_argument("output", type=Path, help="输出的 wav 文件")
    record_parser.add_argument("--seconds", type=float, default=5.0, help="录多少秒，默认 5")
    enroll_parser = subcommands.add_parser(
        "enroll-voice", help="用 record 录的你的声音注册声纹，存在 .env 旁边"
    )
    enroll_parser.add_argument(
        "recordings", type=Path, nargs="+", help="record 录的 wav，可以给几段"
    )
    enroll_parser.add_argument(
        "--as",
        dest="name",
        help="另录的一份叫什么（如 phone、airpods），存成 voiceprint-名字.json，只给 App 那条路比；"
        "不给就是家里阵列那份",
    )
    score_parser = subcommands.add_parser(
        "score-voice", help="拿录音和注册的声纹比，每句打印分数、算不算你"
    )
    score_parser.add_argument(
        "recordings", type=Path, nargs="+", help="record 录的 wav，可以给几段"
    )
    calibrate_parser = subcommands.add_parser(
        "calibrate-voice", help="拿你的、别人的录音标定声纹阈值，打印每个阈值下的误拒、误认"
    )
    calibrate_parser.add_argument("--you", type=Path, nargs="+", required=True, help="你的录音")
    calibrate_parser.add_argument(
        "--others", type=Path, nargs="+", required=True, help="别人、电视、播客的录音"
    )
    play_parser = subcommands.add_parser("play", help="从放音设备放一段 wav")
    play_parser.add_argument("input", type=Path, help="16kHz 单声道 16 位的 wav（record 录的就是）")
    ask_parser = subcommands.add_parser("ask", help="用主力档模型问一句，打印回答")
    ask_parser.add_argument("question", help="要问的话")
    say_parser = subcommands.add_parser("say", help="合成一句话，从放音设备放出来")
    say_parser.add_argument("sentence", help="要念的话")
    subcommands.add_parser("chat", help="语音对话：回车开始说，说完再按回车，听它回答")
    wake_parser = subcommands.add_parser("wake", help="一直听唤醒词，每听到一次打印一行")
    wake_parser.add_argument(
        "--minutes", type=float, default=None, help="听多少分钟后自动停，不写就一直听到按 Ctrl+C"
    )
    wake_parser.add_argument("--save", type=Path, default=None, help="把听到的声音存成这个 wav")
    vad_parser = subcommands.add_parser("vad", help="一直听，每说完一句打印一行，看判停切得对不对")
    vad_parser.add_argument(
        "--minutes", type=float, default=None, help="听多少分钟后自动停，不写就一直听到按 Ctrl+C"
    )
    vad_parser.add_argument("--save", type=Path, default=None, help="把听到的声音存成这个 wav")
    subcommands.add_parser(
        "new-app-token", help="生成一个 App 连主机用的 token，填进 .env 的 APP_TOKEN"
    )
    subcommands.add_parser(
        "new-bark-key", help="生成一个 Bark 加密推送的 Key，填进 .env 的 BARK_ENCRYPTION_KEY"
    )
    push_parser = subcommands.add_parser("push", help="经 Bark 往手机推一条加密通知")
    push_parser.add_argument("title", help="通知的标题")
    push_parser.add_argument("body", help="通知的正文")
    push_parser.add_argument(
        "--time-sensitive", action="store_true", help="时效性通知：开着专注模式也弹出来"
    )
    push_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="直接读这份 .env，不读主配置；配置写坏了也推得出去",
    )
    _add_place_parser(subcommands)
    _add_budget_parser(subcommands)
    _add_profile_parser(subcommands)
    subcommands.add_parser("sync-notes", help="把 self/ 下改了的资料同步进资料索引，主进程开着也行")
    arguments = parser.parse_args(argv)

    if arguments.command == "new-app-token":
        return _new_app_token()
    if arguments.command == "new-bark-key":
        return _new_bark_key()
    if arguments.command == "push":
        level = PushLevel.TIME_SENSITIVE if arguments.time_sensitive else PushLevel.NORMAL
        return _push(arguments.config, arguments.env_file, arguments.title, arguments.body, level)
    if arguments.command == "place":
        return _place(arguments)
    if arguments.command == "budget":
        return _budget(arguments)
    if arguments.command == "import-profile":
        return _import_profile(arguments.config, arguments.path, dry_run=arguments.dry_run)
    if arguments.command == "sync-notes":
        return _sync_notes(arguments.config)
    if arguments.command == "record":
        if arguments.seconds <= 0:
            parser.error("--seconds 必须大于 0")
        return _record(arguments.config, arguments.output, arguments.seconds)
    if arguments.command == "enroll-voice":
        return _enroll_voice(arguments.config, arguments.recordings, arguments.name)
    if arguments.command == "score-voice":
        return _score_voice(arguments.config, arguments.recordings)
    if arguments.command == "calibrate-voice":
        return _calibrate_voice(arguments.config, arguments.you, arguments.others)
    if arguments.command in ("wake", "vad"):
        if arguments.minutes is not None and arguments.minutes <= 0:
            parser.error("--minutes 必须大于 0")
        if arguments.command == "wake":
            return _wake(arguments.config, arguments.minutes, arguments.save)
        return _vad(arguments.config, arguments.minutes, arguments.save)
    if arguments.command == "ask":
        return _ask(arguments.config, arguments.question)
    if arguments.command == "say":
        return _say(arguments.config, arguments.sentence)
    if arguments.command == "chat":
        return _chat(arguments.config)
    return _play(arguments.config, arguments.input)


def generate_app_token() -> str:
    groups = [
        "".join(secrets.choice(_HAND_TYPED_ALPHABET) for _ in range(_APP_TOKEN_GROUP_LENGTH))
        for _ in range(_APP_TOKEN_GROUPS)
    ]
    return "-".join(groups)


def _new_app_token() -> int:
    print(generate_app_token())
    print(
        "把上面这行填进 .env 的 APP_TOKEN=，重启主进程，再在手机上的阿格斯里照着输一次",
        file=sys.stderr,
    )
    return 0


def generate_bark_key() -> str:
    first = secrets.choice(_BARK_KEY_FIRST_CHARACTERS)
    rest = "".join(secrets.choice(_HAND_TYPED_ALPHABET) for _ in range(_BARK_KEY_LENGTH - 1))
    return first + rest


def _new_bark_key() -> int:
    bark_key = generate_bark_key()
    shown_groups = [
        bark_key[start : start + _BARK_KEY_SHOWN_GROUP_LENGTH]
        for start in range(0, len(bark_key), _BARK_KEY_SHOWN_GROUP_LENGTH)
    ]
    print(bark_key, flush=True)
    print(
        f"手机上照着输（空格不输）：{' '.join(shown_groups)}\n"
        "把不带空格的那行填进 .env 的 BARK_ENCRYPTION_KEY=；Bark 的加密设置选 AES256、GCM，"
        "Key 照着上面输",
        file=sys.stderr,
    )
    return 0


def _push(config_path: Path, env_file: Path | None, title: str, body: str, level: PushLevel) -> int:
    failure_prefix = "没推出去。"
    try:
        if env_file is not None:
            env_path = env_file
        else:
            env_path = load_config(config_path).secrets_env_path
        app_secrets = load_secrets(env_path)
    except ConfigError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return 1
    if app_secrets.bark_device_key is None or app_secrets.bark_encryption_key is None:
        print(
            f"{failure_prefix}.env 里没配 BARK_DEVICE_KEY、BARK_ENCRYPTION_KEY",
            file=sys.stderr,
        )
        return 1
    bark = BarkPush(
        app_secrets.bark_device_key.get_secret_value(),
        app_secrets.bark_encryption_key.get_secret_value(),
    )
    try:
        asyncio.run(_push_once(bark, title, body, level))
    except PushError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return 1
    print("Bark 服务器收下了，看手机")
    return 0


async def _push_once(bark: BarkPush, title: str, body: str, level: PushLevel) -> None:
    async with aclosing(bark):
        await bark.push(title, body, level=level)


def _add_budget_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    budget_parser = subcommands.add_parser(
        "budget", help="看这个月云端花了多少、上限多少；budget add 给这个月追加上限"
    )
    budget_commands = budget_parser.add_subparsers(dest="budget_command")
    add_parser = budget_commands.add_parser("add", help="给这个月的上限追加多少元，只对这个月有效")
    add_parser.add_argument("yuan", type=float, help="追加多少元，如 50")


def _budget(arguments: argparse.Namespace) -> int:
    adding = arguments.budget_command == "add"
    failure_prefix = "没有追加上限。" if adding else "没有查到花费。"
    if adding and not arguments.yuan > 0:
        print(f"{failure_prefix}追加的钱要大于 0", file=sys.stderr)
        return 1
    try:
        config = load_config(arguments.config)
        models_config = load_models_config(models_config_path(arguments.config))
    except ConfigError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return 1
    argus_db_path = _prepare_argus_db(config, failure_prefix)
    if argus_db_path is None:
        return 1

    month_start, spent_yuan, extra_yuan = asyncio.run(
        _budget_this_month(argus_db_path, arguments.yuan if adding else None)
    )
    configured_yuan = models_config.monthly_budget_yuan
    if adding:
        print(f"追加了 ¥{arguments.yuan:g}。主进程下一次调模型就按新的上限算，不用重启")
    print(
        f"{month_start:%Y-%m}：已花 ¥{spent_yuan:.2f}，上限 ¥{configured_yuan + extra_yuan:g}"
        f"（models.yaml ¥{configured_yuan:g}，这个月追加 ¥{extra_yuan:g}）"
    )
    return 0


async def _budget_this_month(
    argus_db_path: Path, yuan_to_add: float | None
) -> tuple[datetime, float, float]:
    now = SystemClock().now()
    month_start = budget_month(now)
    month = f"{month_start:%Y-%m}"
    async with _open_database(argus_db_path) as argus_db:
        ledger = SqliteCostLedger(argus_db)
        if yuan_to_add is not None:
            await ledger.add_extra_budget(month, yuan_to_add, now)
        return (
            month_start,
            await ledger.spent_since(month_start),
            await ledger.extra_budget_yuan(month),
        )


def _add_profile_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    profile_parser = subcommands.add_parser(
        "import-profile",
        help="把自述档案「关于我.md」导入成语义记忆，常驻在每次请求里",
    )
    profile_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help="档案文件；不写就是数据根目录 secrets 下的「关于我.md」",
    )
    profile_parser.add_argument(
        "--dry-run", action="store_true", help="只打印会加哪些、删哪些，不写库"
    )


def _import_profile(config_path: Path, profile_path: Path | None, *, dry_run: bool) -> int:
    failure_prefix = "没有导入。"
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return 1
    path = config.profile_path if profile_path is None else profile_path
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        print(f"{failure_prefix}读不了档案 {path}：{error}", file=sys.stderr)
        return 1
    parsed = parse_profile(text)
    if parsed.errors:
        for mistake in parsed.errors:
            print(f"第 {mistake.line_number} 行：{mistake.message}", file=sys.stderr)
        print(f"{failure_prefix}档案里有 {len(parsed.errors)} 处写错，改好再导入", file=sys.stderr)
        return 1
    argus_db_path = _prepare_argus_db(config, failure_prefix)
    if argus_db_path is None:
        return 1
    result = asyncio.run(_import_profile_into_db(argus_db_path, parsed.entries, dry_run=dry_run))
    _print_profile_import(result, dry_run=dry_run)
    return 0


async def _import_profile_into_db(
    argus_db_path: Path, entries: Sequence[ProfileEntry], *, dry_run: bool
) -> ProfileImport:
    async with _open_database(argus_db_path) as argus_db:
        memory = Memory(store=SqliteMemoryStore(argus_db), clock=SystemClock())
        return await memory.import_profile(entries, dry_run=dry_run)


def _print_profile_import(result: ProfileImport, *, dry_run: bool) -> None:
    if dry_run:
        for entry in result.added:
            print(f"加：{entry.subject}的{entry.attribute}：{entry.value}")
        for entry in result.removed:
            print(f"删：{entry.subject}的{entry.attribute}：{entry.value}")
    verb = "会" if dry_run else "已经"
    print(
        f"{verb}加 {len(result.added)} 条、{verb}删 {len(result.removed)} 条、"
        f"{result.unchanged} 条没变"
        + ("（预览，没有写库）" if dry_run else "。主进程开着也不用重启，下一句话起就带上了")
    )


def _sync_notes(config_path: Path) -> int:
    failure_prefix = "没有同步。"
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return 1
    if not config.notes_dir.is_dir():
        print(f"{failure_prefix}资料目录 {config.notes_dir} 不存在", file=sys.stderr)
        return 1
    embedder = load_embedder(config.models_dir)
    if embedder is None:
        print(f"{failure_prefix}向量化模型没加载上", file=sys.stderr)
        return 1
    if _prepare_argus_db(config, failure_prefix) is None:
        return 1
    started = time.monotonic()
    reports = asyncio.run(_sync_notes_into_db(config, embedder))
    for report in reports:
        print(f"{report.source}：新加和改了的 {report.changed} 篇，删了的 {report.deleted} 篇")
    print(f"用时 {time.monotonic() - started:.1f} 秒。主进程开着也不用重启，下一次查资料就用上了")
    return 0


async def _sync_notes_into_db(config: Config, embedder: Embedder) -> list[SyncReport]:
    async with _open_database(config.data_dir / "docs.db") as docs_db:
        index = NotesIndex(store=SqliteChunkStore(docs_db), embedder=embedder)
        return await notes_sync(config.notes_dir, docs_db, index).sync()


def _add_place_parser(subcommands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    place_parser = subcommands.add_parser("place", help="录常去的地点（家、公司……）")
    place_commands = place_parser.add_subparsers(dest="place_command", required=True)

    set_parser = place_commands.add_parser("set", help="录一个地点，同名的就改")
    set_parser.add_argument("name", help="名字，如 家、公司")
    _add_coordinate_options(set_parser)
    set_parser.add_argument(
        "--radius",
        type=float,
        default=DEFAULT_RADIUS_M,
        help=f"多少米以内算在这里，默认 {DEFAULT_RADIUS_M:g}",
    )

    place_commands.add_parser("list", help="列出录过的地点（不打印坐标）")
    remove_parser = place_commands.add_parser("remove", help="删掉一个地点")
    remove_parser.add_argument("name", help="地点的名字")

    where_parser = place_commands.add_parser(
        "where", help="站在这个坐标上，ARGUS 会说你在哪：录过的地点、反查的市区路名"
    )
    _add_coordinate_options(where_parser)


def _add_coordinate_options(parser: argparse.ArgumentParser) -> None:
    coordinates = parser.add_mutually_exclusive_group(required=True)
    coordinates.add_argument(
        "--gcj02",
        metavar="纬度,经度",
        help="国内地图上拾取的坐标（腾讯位置服务的坐标拾取器给的就是），先换成 WGS-84",
    )
    coordinates.add_argument(
        "--wgs84", metavar="纬度,经度", help="GPS 坐标，如 iPhone「指南针」App 里看到的"
    )


def _place(arguments: argparse.Namespace) -> int:
    if arguments.place_command == "set":
        return _set_place(
            arguments.config,
            arguments.name,
            gcj02=arguments.gcj02,
            wgs84=arguments.wgs84,
            radius_m=arguments.radius,
        )
    if arguments.place_command == "list":
        return _list_places(arguments.config)
    if arguments.place_command == "where":
        return _where(arguments.config, gcj02=arguments.gcj02, wgs84=arguments.wgs84)
    return _remove_place(arguments.config, arguments.name)


def _set_place(
    config_path: Path, name: str, *, gcj02: str | None, wgs84: str | None, radius_m: float
) -> int:
    failure_prefix = "没有录地点。"
    try:
        place = Place(
            name=name.strip(),
            center=_parse_center(gcj02=gcj02, wgs84=wgs84),
            radius_m=radius_m,
            updated_at=SystemClock().now(),
        )
    except ValueError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return 1
    argus_db_path = _prepare_place_db(config_path, failure_prefix)
    if argus_db_path is None:
        return 1
    asyncio.run(_save_place(argus_db_path, place))
    print(f"录好了：{place.name}，半径 {place.radius_m:g} 米")
    return 0


def _list_places(config_path: Path) -> int:
    argus_db_path = _prepare_place_db(config_path, failure_prefix="没有列出地点。")
    if argus_db_path is None:
        return 1
    places = asyncio.run(_load_places(argus_db_path))
    if not places:
        print("还没录过地点")
    for place in places:
        print(f"{place.name}  半径 {place.radius_m:g} 米  {place.updated_at:%Y-%m-%d %H:%M} 录的")
    return 0


def _remove_place(config_path: Path, name: str) -> int:
    failure_prefix = "没有删地点。"
    name = name.strip()
    argus_db_path = _prepare_place_db(config_path, failure_prefix)
    if argus_db_path is None:
        return 1
    if not asyncio.run(_delete_place(argus_db_path, name)):
        print(f"{failure_prefix}没有叫「{name}」的地点，place list 看看录过哪些", file=sys.stderr)
        return 1
    print(f"删掉了：{name}")
    return 0


def _where(config_path: Path, *, gcj02: str | None, wgs84: str | None) -> int:
    failure_prefix = "没有查。"
    try:
        here = _parse_center(gcj02=gcj02, wgs84=wgs84)
        config = load_config(config_path)
        map_key = load_secrets(config.secrets_env_path).tencent_map_key
    except (ValueError, ConfigError) as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return 1
    argus_db_path = _prepare_argus_db(config, failure_prefix)
    if argus_db_path is None:
        return 1

    places = asyncio.run(_load_places(argus_db_path))
    fix = GeoFix(lat=here.lat, lng=here.lng, accuracy_m=0.0, at=SystemClock().now(), moving=False)
    print(f"录过的地点：{describe_location(fix, places) or '不在任何一个的半径内'}")
    if map_key is None:
        print("地图服务：没配 TENCENT_MAP_KEY，不反查")
        return 0
    try:
        district_and_road = asyncio.run(_reverse_geocode(map_key.get_secret_value(), here))
    except ReverseGeocodeError as error:
        print(f"地图服务：没查到。{error}", file=sys.stderr)
        return 1
    print(f"地图服务：{district_and_road}")
    return 0


async def _reverse_geocode(map_key: str, point: Wgs84Point) -> str:
    geocoder = TencentReverseGeocoder(map_key)
    try:
        return await geocoder.district_and_road(wgs84_to_gcj02(lat=point.lat, lng=point.lng))
    finally:
        await geocoder.aclose()


def _parse_center(*, gcj02: str | None, wgs84: str | None) -> Wgs84Point:
    if gcj02 is not None:
        lat, lng = _parse_lat_lng(gcj02)
        return gcj02_to_wgs84(Gcj02Point(lat=lat, lng=lng))
    assert wgs84 is not None
    lat, lng = _parse_lat_lng(wgs84)
    return Wgs84Point(lat=lat, lng=lng)


def _parse_lat_lng(text: str) -> tuple[float, float]:
    parts = text.replace("，", ",").split(",")
    if len(parts) != 2:
        raise ValueError("坐标写成 纬度,经度，中间一个逗号")
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        raise ValueError("坐标写成 纬度,经度，两个都要是数") from None
    if looks_swapped(lat=lat, lng=lng):
        raise ValueError("纬度、经度像是写反了：先写纬度（国内在 4～53 之间），再写经度")
    return lat, lng


def _prepare_place_db(config_path: Path, failure_prefix: str) -> Path | None:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return None
    return _prepare_argus_db(config, failure_prefix)


async def _save_place(argus_db_path: Path, place: Place) -> None:
    async with _open_database(argus_db_path) as argus_db:
        await SqlitePlaceStore(argus_db).save(place)


async def _load_places(argus_db_path: Path) -> list[Place]:
    async with _open_database(argus_db_path) as argus_db:
        return await SqlitePlaceStore(argus_db).all()


async def _delete_place(argus_db_path: Path, name: str) -> bool:
    async with _open_database(argus_db_path) as argus_db:
        return await SqlitePlaceStore(argus_db).remove(name)


def _record(config_path: Path, output_path: Path, seconds: float) -> int:
    try:
        audio_config = _load_audio_config(config_path)
    except ConfigError as error:
        print(f"没有录音。{error}", file=sys.stderr)
        return 1

    from argus.infra.audio.capture import MicrophoneCapture

    capture = MicrophoneCapture(audio_config.input_device, audio_config.input_channel)
    frame_count = round(seconds * 1000 / FRAME_MS)
    print(f"开始录音，{seconds:g} 秒……", flush=True)
    try:
        clip = asyncio.run(_collect(capture.frames(), frame_count))
    except AudioDeviceError as error:
        print(f"没有录音。{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("录音被 Ctrl+C 中断，没有保存", file=sys.stderr)
        return 1

    try:
        _save_wav(output_path, clip)
    except OSError as error:
        print(f"录好了但没存下来：{output_path}\n  {error}", file=sys.stderr)
        return 1
    print(f"已保存 {output_path}（{clip.duration_ms / 1000:g} 秒）")
    return 0


def _enroll_voice(config_path: Path, recording_paths: Sequence[Path], name: str | None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"没有注册。{error}", file=sys.stderr)
        return 1
    voiceprint_path = config.voiceprint_path
    if name is not None:
        try:
            voiceprint_path = named_voiceprint_path(config.voiceprint_path, name)
        except ValueError as error:
            print(f"没有注册。{error}", file=sys.stderr)
            return 1
    vad_config = config.vad
    if vad_config is None:
        print(f"没有注册。{config_path} 里要有 vad 段：按主进程的判停切句", file=sys.stderr)
        return 1
    try:
        recordings = [_load_wav(path) for path in recording_paths]
    except (OSError, wave.Error, ValueError) as error:
        print(f"没有注册。读不了录音\n  {error}", file=sys.stderr)
        return 1

    print("切句、算声纹……", flush=True)
    try:
        enrollment = enroll(
            recordings,
            lambda: load_vad(config.models_dir, vad_config),
            load_speaker_embedding(config.models_dir),
        )
    except NotEnoughSpeech as error:
        print(
            f"没有注册。{error}：录长一点，一句一句说，句子之间停一下",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as error:
        print(f"没有注册。{error}", file=sys.stderr)
        return 1

    replacing = voiceprint_path.exists()
    try:
        save_voiceprint(voiceprint_path, enrollment.voiceprint)
    except OSError as error:
        print(f"算好了但没存下来：{voiceprint_path}\n  {error}", file=sys.stderr)
        return 1
    _print_enrollment(enrollment)
    replaced_note = "，换掉了原来的" if replacing else ""
    print(f"已注册，存在 {voiceprint_path}{replaced_note}")
    return 0


def _print_enrollment(enrollment: Enrollment) -> None:
    sentences = enrollment.sentences
    similarities = sorted(sentence.score for sentence in sentences)
    total_ms = round(sum(sentence.seconds for sentence in sentences) * 1000)
    print(
        f"切出 {len(sentences)} 句，共 {_recording_position(total_ms)}；"
        f"每句和平均出来的声纹比，最低 {similarities[0]:.2f}，中位 {similarities[len(similarities) // 2]:.2f}"
    )
    if not enrollment.suspicious:
        return
    print(
        f"有 {len(enrollment.suspicious)} 句不太像你（低于 {SUSPICIOUS_SIMILARITY}）：多半录进了电视、别人的声音，"
        "或者只切到半个字。听一下这几处，是的话重录、再注册一次"
    )
    for sentence in enrollment.suspicious:
        print(f"  {_sentence_position(sentence)}，{sentence.score:.2f}")


def _score_voice(config_path: Path, recording_paths: Sequence[Path]) -> int:
    scored = _score_recording_groups(config_path, [recording_paths])
    if scored is None:
        return 1
    threshold, [scores] = scored

    for sentence in scores:
        verdict = "是你" if speaker_for(sentence.score, threshold) is Speaker.OWNER else "不是你"
        print(f"  {_sentence_position(sentence)}，{sentence.score:.2f} {verdict}")
    owner_count = sum(
        speaker_for(sentence.score, threshold) is Speaker.OWNER for sentence in scores
    )
    print(
        f"切出 {len(scores)} 句：是你 {owner_count} 句，不是你 {len(scores) - owner_count} 句"
        f"（阈值 {threshold:.2f}）"
    )
    return 0


def _calibrate_voice(
    config_path: Path, owner_paths: Sequence[Path], other_paths: Sequence[Path]
) -> int:
    scored = _score_recording_groups(config_path, [owner_paths, other_paths])
    if scored is None:
        return 1
    current_threshold, [owner, others] = scored
    if not owner or not others:
        print("没有标定。你的、别人的录音都要切得出句子", file=sys.stderr)
        return 1

    owner_scores = [sentence.score for sentence in owner]
    other_scores = [sentence.score for sentence in others]
    short_owner_scores = [
        sentence.score for sentence in owner if sentence.seconds <= SHORT_SENTENCE_SECONDS
    ]
    suggested = highest_threshold_within_target(owner_scores)
    print(
        f"你的 {len(owner)} 句（{SHORT_SENTENCE_SECONDS:g} 秒以内的短句 {len(short_owner_scores)} 句），"
        f"别人的 {len(others)} 句"
    )
    print("  阈值  你被拒  别人被认成你")
    thresholds = sorted(
        {round(0.20 + 0.05 * step, 2) for step in range(9)} | {current_threshold, suggested}
    )
    for threshold in thresholds:
        rates = rates_at(threshold, owner_scores, other_scores)
        note = "  ← argus.yaml 现在的" if threshold == current_threshold else ""
        print(f"  {threshold:.2f}  {rates.owner_rejected:6.1%}  {rates.other_accepted:6.1%}{note}")

    rates = rates_at(suggested, owner_scores, other_scores)
    short_note = ""
    if short_owner_scores:
        short_rejected = rates_at(suggested, short_owner_scores, other_scores).owner_rejected
        short_note = f"（短句 {short_rejected:.1%}）"
    print(
        f"你被拒不到 5% 的最高阈值是 {suggested:.2f}：你被拒 {rates.owner_rejected:.1%}{short_note}，"
        f"别人被认成你 {rates.other_accepted:.1%}。定下来就改 argus.yaml 的 speaker.threshold，重启主进程"
    )
    if rates.other_accepted > 0:
        print(
            "这时还有别人被认成你：先看注册语料是不是录得太少、太单一，"
            "还不行就换后备模型 ERes2NetV2"
        )
    return 0


def _score_recording_groups(
    config_path: Path, recording_groups: Sequence[Sequence[Path]]
) -> tuple[float, list[tuple[SentenceScore, ...]]] | None:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"没有比。{error}", file=sys.stderr)
        return None
    vad_config, speaker_config = config.vad, config.speaker
    if vad_config is None or speaker_config is None:
        print(
            f"没有比。{config_path} 里要有 vad 段和 speaker 段：vad 定怎么切句，speaker 定阈值",
            file=sys.stderr,
        )
        return None
    try:
        groups = [[_load_wav(path) for path in paths] for paths in recording_groups]
    except (OSError, wave.Error, ValueError) as error:
        print(f"没有比。读不了录音\n  {error}", file=sys.stderr)
        return None

    try:
        verifier = load_speaker_verifier(config.models_dir, config.voiceprint_path)
        print("切句、比声纹……", flush=True)
        scores = [
            score_recordings(recordings, lambda: load_vad(config.models_dir, vad_config), verifier)
            for recordings in groups
        ]
    except VoiceprintFileError as error:
        print(f"没有比。{error}", file=sys.stderr)
        return None
    except (OSError, ValueError) as error:
        print(f"没有比。{error}", file=sys.stderr)
        return None
    return speaker_config.threshold, scores


def _sentence_position(sentence: SentenceScore) -> str:
    start = _recording_position(round(sentence.start_seconds * 1000))
    return f"第 {sentence.recording_index + 1} 段录音 {start} 起，{sentence.seconds:.1f} 秒"


def _play(config_path: Path, input_path: Path) -> int:
    try:
        audio_config = _load_audio_config(config_path)
    except ConfigError as error:
        print(f"没有放音。{error}", file=sys.stderr)
        return 1
    try:
        clip = _load_wav(input_path)
    except (OSError, wave.Error, ValueError) as error:
        print(f"没有放音。读不了 {input_path}\n  {error}", file=sys.stderr)
        return 1

    from argus.infra.audio.playback import SpeakerPlayback

    playback = SpeakerPlayback(audio_config.output_device, SystemClock())
    print(f"开始放音，{clip.duration_ms / 1000:g} 秒……", flush=True)
    try:
        asyncio.run(playback.play(_split_into_chunks(clip)))
    except AudioDeviceError as error:
        print(f"没有放音。{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("放音被 Ctrl+C 中断", file=sys.stderr)
        return 1
    print("放完了")
    return 0


def _ask(config_path: Path, question: str) -> int:
    try:
        config = load_config(config_path)
        models_config = load_models_config(models_config_path(config_path))
        app_secrets = load_secrets(config.secrets_env_path)
        fallback_api_key(models_config, app_secrets)
        sensitive_words = load_sensitive_words(config.sensitive_words_path)
    except ConfigError as error:
        print(f"没有提问。{error}", file=sys.stderr)
        return 1

    argus_db_path = _prepare_argus_db(config, failure_prefix="没有提问。")
    if argus_db_path is None:
        return 1

    try:
        response = asyncio.run(
            _ask_main_tier(models_config, app_secrets, sensitive_words, argus_db_path, question)
        )
    except LlmError as error:
        print(f"没有拿到回答。{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("提问被 Ctrl+C 中断", file=sys.stderr)
        return 1
    print(response.text)
    usage = response.usage
    print(
        f"（{usage.model}：输入 {usage.input_tokens} token，其中缓存命中 {usage.cached_input_tokens}；"
        f"输出 {usage.output_tokens} token）"
    )
    return 0


def _say(config_path: Path, sentence: str) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"没有合成。{error}", file=sys.stderr)
        return 1
    zipvoice = None if config.tts is None else config.tts.zipvoice
    if zipvoice is None or config.audio is None:
        print(
            f"没有合成。{config_path} 里要有 tts.zipvoice 段和 audio 段：定用什么声音念、从哪个设备放",
            file=sys.stderr,
        )
        return 1

    from argus.infra.audio.playback import SpeakerPlayback

    print("加载 TTS 模型……", flush=True)
    load_started = time.perf_counter()
    try:
        tts = load_tts(config.models_dir, zipvoice)
    except (OSError, RuntimeError) as error:
        print(f"没有合成。{error}", file=sys.stderr)
        return 1
    load_seconds = time.perf_counter() - load_started

    synthesis_started = time.perf_counter()
    chunk = tts.synthesize(sentence)
    synthesis_ms = (time.perf_counter() - synthesis_started) * 1000
    print(
        f"加载 {load_seconds:.1f} 秒；合成 {synthesis_ms:.0f}ms，念出来 {chunk.duration_ms / 1000:g} 秒",
        flush=True,
    )

    playback = SpeakerPlayback(config.audio.output_device, SystemClock())
    try:
        asyncio.run(playback.play(_split_into_chunks(AudioClip(chunk.pcm))))
    except AudioDeviceError as error:
        print(f"没有放音。{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("放音被 Ctrl+C 中断", file=sys.stderr)
        return 1
    print("放完了")
    return 0


def _chat(config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"没有开始对话。{error}", file=sys.stderr)
        return 1
    zipvoice = None if config.tts is None else config.tts.zipvoice
    if zipvoice is None or config.audio is None:
        print(
            f"没有开始对话。{config_path} 里要有 tts.zipvoice 段和 audio 段：定用什么声音念、用哪个设备录和放",
            file=sys.stderr,
        )
        return 1
    try:
        models_config = load_models_config(models_config_path(config_path))
        persona = load_persona(persona_path(config_path))
        app_secrets = load_secrets(config.secrets_env_path)
        fallback_api_key(models_config, app_secrets)
        sensitive_words = load_sensitive_words(config.sensitive_words_path)
    except ConfigError as error:
        print(f"没有开始对话。{error}", file=sys.stderr)
        return 1
    argus_db_path = _prepare_argus_db(config, failure_prefix="没有开始对话。")
    if argus_db_path is None:
        return 1

    from argus.infra.audio.capture import MicrophoneCapture
    from argus.infra.audio.playback import SpeakerPlayback

    print("加载 ASR、TTS 模型……", flush=True)
    load_started = time.perf_counter()
    try:
        asr = load_asr(config.models_dir)
        tts = load_tts(config.models_dir, zipvoice)
    except (OSError, RuntimeError) as error:
        print(f"没有开始对话。{error}", file=sys.stderr)
        return 1
    print(f"加载 {time.perf_counter() - load_started:.1f} 秒", flush=True)

    capture = MicrophoneCapture(config.audio.input_device, config.audio.input_channel)
    playback = SpeakerPlayback(config.audio.output_device, SystemClock())
    recognizer = Recognizer(asr)
    synthesizer = Synthesizer(tts)
    try:
        asyncio.run(
            _chat_loop(
                models_config,
                app_secrets,
                sensitive_words,
                argus_db_path,
                persona,
                recognizer,
                synthesizer,
                capture,
                playback,
            )
        )
    except AudioDeviceError as error:
        print(f"对话中断。{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("对话被 Ctrl+C 中断", file=sys.stderr)
        return 1
    return 0


async def _chat_loop(
    models_config: ModelsConfig,
    app_secrets: Secrets,
    sensitive_words: Sequence[str],
    argus_db_path: Path,
    persona: str,
    recognizer: Recognizer,
    synthesizer: Synthesizer,
    capture: "MicrophoneCapture",
    playback: "SpeakerPlayback",
) -> None:
    lines = _start_line_reader()
    ids = UuidIdGen()
    async with (
        _open_database(argus_db_path) as argus_db,
        _open_main_gateway(models_config, app_secrets, sensitive_words, argus_db) as gateway,
    ):
        turn_log = SqliteTurnLog(argus_db)
        while True:
            print("\n回车开始说话；输入 q 再回车退出", flush=True)
            line = await lines.get()
            if line is None or line.strip().lower() == "q":
                return
            print("在听……说完按回车", flush=True)
            recording = await _record_until_enter(capture, lines)
            if recording is None:
                return
            clip, enter_pressed_at = recording
            await _answer(
                clip,
                enter_pressed_at,
                ids.new("trace"),
                persona,
                recognizer,
                synthesizer,
                gateway,
                playback,
                turn_log,
            )


async def _record_until_enter(
    capture: "MicrophoneCapture", lines: asyncio.Queue[str | None]
) -> tuple[AudioClip, float] | None:
    collected: list[AudioFrame] = []
    stop_requested = asyncio.Event()

    async def collect_until_stopped() -> None:
        async with aclosing(capture.frames()) as frames:
            async for frame in frames:
                collected.append(frame)
                if stop_requested.is_set():
                    break

    recording = asyncio.create_task(collect_until_stopped())
    line = await lines.get()
    enter_pressed_at = time.perf_counter()
    stop_requested.set()
    await recording
    if line is None:
        return None
    return AudioClip.from_frames(collected), enter_pressed_at


async def _answer(
    clip: AudioClip,
    enter_pressed_at: float,
    trace_id: str,
    persona: str,
    recognizer: Recognizer,
    synthesizer: Synthesizer,
    gateway: ModelGateway,
    playback: _Playback,
    turn_log: SqliteTurnLog,
) -> None:
    with trace(trace_id):
        heard = await recognizer.transcribe(clip)
        asr_finished_at = time.perf_counter()
        timings = _ReplyTimings()
        if heard:
            print(f"你说：{heard}", flush=True)
            request = LlmRequest(
                tier=Tier.MAIN,
                purpose=Purpose.DIALOG,
                messages=build_context(
                    persona=persona,
                    situation="",
                    history=(),
                    utterance=heard,
                    spoken=True,
                    speaker=Speaker.OWNER,
                ),
            )
            async with aclosing(
                _speak_reply(gateway, request, synthesizer, timings)
            ) as reply_audio:
                await playback.play(reply_audio)
            print(_format_reply_timings(enter_pressed_at, asr_finished_at, timings), flush=True)
        else:
            print("没听清，再说一次", flush=True)
        await turn_log.record(_turn_latency(trace_id, enter_pressed_at, asr_finished_at, timings))


@dataclass
class _ReplyTimings:
    first_text_at: float | None = None
    model_finished_at: float | None = None
    first_audio_at: float | None = None
    gap_seconds: float = 0.0


async def _speak_reply(
    gateway: ModelGateway, request: LlmRequest, synthesizer: Synthesizer, timings: _ReplyTimings
) -> AsyncGenerator[AudioChunk]:
    sentences: asyncio.Queue[str | None] = asyncio.Queue()
    reader = asyncio.create_task(_read_reply_sentences(gateway, request, sentences, timings))
    try:
        playback_ends_at: float | None = None
        while True:
            sentence = await sentences.get()
            if sentence is None:
                break
            async with aclosing(synthesizer.synthesize(sentence)) as sentence_audio:
                async for audio in sentence_audio:
                    handed_over_at = time.perf_counter()
                    if playback_ends_at is None:
                        timings.first_audio_at = handed_over_at
                        starts_at = handed_over_at
                    else:
                        starts_at = max(handed_over_at, playback_ends_at)
                        timings.gap_seconds += starts_at - playback_ends_at
                    playback_ends_at = starts_at + audio.duration_ms / 1000
                    yield audio
        await reader
    finally:
        if not reader.done():
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader


async def _read_reply_sentences(
    gateway: ModelGateway,
    request: LlmRequest,
    sentences: asyncio.Queue[str | None],
    timings: _ReplyTimings,
) -> None:
    splitter = SentenceSplitter()
    handed_over: list[str] = []

    def hand_over(new_sentences: list[str]) -> None:
        for sentence in new_sentences:
            print(f"ARGUS：{sentence}" if not handed_over else f"       {sentence}", flush=True)
            handed_over.append(sentence)
            sentences.put_nowait(sentence)

    try:
        async with aclosing(gateway.stream(request)) as deltas:
            async for delta in deltas:
                if not isinstance(delta, TextDelta):
                    continue
                if timings.first_text_at is None:
                    timings.first_text_at = time.perf_counter()
                hand_over(splitter.feed(delta.text))
        timings.model_finished_at = time.perf_counter()
        hand_over(splitter.flush())
    except LlmError as error:
        if timings.first_text_at is None:
            print(f"没有拿到回答。{error}", file=sys.stderr)
        else:
            print(f"模型说到一半出错了，后面的不念了。{error}", file=sys.stderr)
    finally:
        sentences.put_nowait(None)


def _format_reply_timings(
    enter_pressed_at: float, asr_finished_at: float, timings: _ReplyTimings
) -> str:
    def since_enter(moment: float) -> str:
        return f"{moment - enter_pressed_at:.1f} 秒"

    parts = [f"ASR {(asr_finished_at - enter_pressed_at) * 1000:.0f}ms"]
    if timings.first_text_at is not None:
        parts.append(f"模型第一个字 {since_enter(timings.first_text_at)}")
    if timings.first_audio_at is not None:
        parts.append(f"开始放音 {since_enter(timings.first_audio_at)}")
    if timings.model_finished_at is not None:
        parts.append(f"模型说完 {since_enter(timings.model_finished_at)}")
    if timings.first_audio_at is not None:
        parts.append(f"句间空档 {timings.gap_seconds:.1f} 秒")
    return f"（从停止录音算起：{'｜'.join(parts)}）"


def _turn_latency(
    trace_id: str, enter_pressed_at: float, asr_finished_at: float, timings: _ReplyTimings
) -> TurnLatency:
    def ms_since_stop(moment: float | None) -> int | None:
        return None if moment is None else round((moment - enter_pressed_at) * 1000)

    first_audio_ms = ms_since_stop(timings.first_audio_at)
    return TurnLatency(
        ts=SystemClock().now(),
        trace_id=trace_id,
        endpoint=_CHAT_ENDPOINT,
        asr_final_ms=ms_since_stop(asr_finished_at),
        route_done_ms=None,
        llm_first_token_ms=ms_since_stop(timings.first_text_at),
        tts_first_chunk_ms=first_audio_ms,
        play_start_ms=first_audio_ms,
    )


def _start_line_reader() -> asyncio.Queue[str | None]:
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    def read_lines() -> None:
        while True:
            line = sys.stdin.readline()
            try:
                loop.call_soon_threadsafe(lines.put_nowait, line or None)
            except RuntimeError:
                return
            if not line:
                return

    threading.Thread(target=read_lines, name="stdin-reader", daemon=True).start()
    return lines


async def _ask_main_tier(
    models_config: ModelsConfig,
    app_secrets: Secrets,
    sensitive_words: Sequence[str],
    argus_db_path: Path,
    question: str,
) -> LlmResponse:
    request = LlmRequest(
        tier=Tier.MAIN, purpose=Purpose.DIALOG, messages=(Message(Role.USER, question),)
    )
    async with (
        _open_database(argus_db_path) as argus_db,
        _open_main_gateway(models_config, app_secrets, sensitive_words, argus_db) as gateway,
    ):
        return await gateway.complete(request)


def _wake(config_path: Path, minutes: float | None, save_path: Path | None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"没有开始听。{error}", file=sys.stderr)
        return 1
    if config.wakeword is None or config.audio is None:
        print(
            f"没有开始听。{config_path} 里要有 wakeword 段和 audio 段：wakeword 定听什么词、参数多少，"
            "audio 定用哪个设备听",
            file=sys.stderr,
        )
        return 1

    from argus.infra.audio.capture import MicrophoneCapture

    try:
        wake_word = load_wake_word(config.models_dir, config.wakeword)
    except (OSError, ValueError) as error:
        print(f"没有开始听。{error}", file=sys.stderr)
        return 1

    try:
        recording = None if save_path is None else _open_wav_for_writing(save_path)
    except OSError as error:
        print(f"没有开始听。存录音的文件建不了：{save_path}\n  {error}", file=sys.stderr)
        return 1

    capture = MicrophoneCapture(config.audio.input_device, config.audio.input_channel)
    frame_limit = None if minutes is None else round(minutes * 60_000 / FRAME_MS)
    stop_hint = "按 Ctrl+C 停" if minutes is None else f"{minutes:g} 分钟后自动停，也可以按 Ctrl+C"
    print(f"开始听唤醒词，{stop_hint}", flush=True)
    tally = _WakeTally()
    try:
        asyncio.run(
            _listen_for_wake_word(capture.frames(), wake_word, frame_limit, tally, recording)
        )
    except AudioDeviceError as error:
        print(f"收音出错，停了。{error}", file=sys.stderr)
        print(tally.summary())
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        if recording is not None:
            recording.close()
            print(f"听到的声音存在 {save_path}")
    print(tally.summary())
    return 0


@dataclass
class _WakeTally:
    wake_count: int = 0
    frame_count: int = 0
    slowest_feed_ms: float = 0.0

    def summary(self) -> str:
        listened_minutes = self.frame_count * FRAME_MS / 60_000
        return (
            f"听了 {listened_minutes:.1f} 分钟，唤醒 {self.wake_count} 次，"
            f"单次检测最慢 {self.slowest_feed_ms:.1f}ms"
        )


async def _listen_for_wake_word(
    frames: AsyncGenerator[AudioFrame],
    wake_word: WakeWord,
    frame_limit: int | None,
    tally: _WakeTally,
    recording: wave.Wave_write | None = None,
) -> None:
    async with aclosing(frames):
        async for frame in frames:
            feed_started = time.perf_counter()
            detected = wake_word.feed(frame)
            feed_ms = (time.perf_counter() - feed_started) * 1000
            tally.slowest_feed_ms = max(tally.slowest_feed_ms, feed_ms)
            if recording is not None:
                recording.writeframes(frame.pcm)
            tally.frame_count += 1
            if detected:
                tally.wake_count += 1
                position = _recording_position(tally.frame_count * FRAME_MS)
                print(
                    f"第 {tally.wake_count} 次唤醒  {time.strftime('%H:%M:%S')}（开始听后第 {position}）",
                    flush=True,
                )
            if frame_limit is not None and tally.frame_count >= frame_limit:
                break


def _vad(config_path: Path, minutes: float | None, save_path: Path | None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        print(f"没有开始听。{error}", file=sys.stderr)
        return 1
    if config.vad is None or config.audio is None:
        print(
            f"没有开始听。{config_path} 里要有 vad 段和 audio 段：vad 定判停的参数，audio 定用哪个设备听",
            file=sys.stderr,
        )
        return 1

    from argus.infra.audio.capture import MicrophoneCapture

    try:
        vad = load_vad(config.models_dir, config.vad)
    except (OSError, ValueError) as error:
        print(f"没有开始听。{error}", file=sys.stderr)
        return 1

    try:
        recording = None if save_path is None else _open_wav_for_writing(save_path)
    except OSError as error:
        print(f"没有开始听。存录音的文件建不了：{save_path}\n  {error}", file=sys.stderr)
        return 1

    capture = MicrophoneCapture(config.audio.input_device, config.audio.input_channel)
    frame_limit = None if minutes is None else round(minutes * 60_000 / FRAME_MS)
    stop_hint = "按 Ctrl+C 停" if minutes is None else f"{minutes:g} 分钟后自动停，也可以按 Ctrl+C"
    print(f"开始听，每说完一句打印一行，{stop_hint}", flush=True)
    tally = _VadTally()
    try:
        asyncio.run(_listen_for_speech(capture.frames(), vad, frame_limit, tally, recording))
    except AudioDeviceError as error:
        print(f"收音出错，停了。{error}", file=sys.stderr)
        print(tally.summary())
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        if recording is not None:
            recording.close()
            print(f"听到的声音存在 {save_path}")
    print(tally.summary())
    return 0


@dataclass
class _VadTally:
    utterance_count: int = 0
    frame_count: int = 0
    slowest_feed_ms: float = 0.0

    def summary(self) -> str:
        listened_minutes = self.frame_count * FRAME_MS / 60_000
        return (
            f"听了 {listened_minutes:.1f} 分钟，说完 {self.utterance_count} 句，"
            f"单次判停最慢 {self.slowest_feed_ms:.1f}ms"
        )


async def _listen_for_speech(
    frames: AsyncGenerator[AudioFrame],
    vad: Vad,
    frame_limit: int | None,
    tally: _VadTally,
    recording: wave.Wave_write | None = None,
) -> None:
    async with aclosing(frames):
        async for frame in frames:
            feed_started = time.perf_counter()
            event = vad.feed(frame)
            feed_ms = (time.perf_counter() - feed_started) * 1000
            tally.slowest_feed_ms = max(tally.slowest_feed_ms, feed_ms)
            if recording is not None:
                recording.writeframes(frame.pcm)
            tally.frame_count += 1
            if isinstance(event, SpeechStart):
                print(f"开口  {time.strftime('%H:%M:%S')}", flush=True)
            elif isinstance(event, SpeechEnd):
                tally.utterance_count += 1
                end_ms = tally.frame_count * FRAME_MS
                start_ms = round(end_ms - event.clip.duration_ms)
                print(
                    f"第 {tally.utterance_count} 句说完  {time.strftime('%H:%M:%S')}，"
                    f"整句 {event.clip.duration_ms / 1000:.1f} 秒，含句尾等判停的静音"
                    f"（开始听后第 {_recording_position(start_ms)}～{_recording_position(end_ms)}）",
                    flush=True,
                )
            if frame_limit is not None and tally.frame_count >= frame_limit:
                break


def _recording_position(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


@asynccontextmanager
async def _open_database(db_path: Path) -> AsyncGenerator[SqliteDatabase]:
    database = SqliteDatabase(db_path)
    try:
        yield database
    finally:
        await database.close()


@asynccontextmanager
async def _open_main_gateway(
    models_config: ModelsConfig,
    app_secrets: Secrets,
    sensitive_words: Sequence[str],
    argus_db: SqliteDatabase,
) -> AsyncGenerator[ModelGateway]:
    gateway, providers = main_gateway(
        models_config, app_secrets, sensitive_words, argus_db, SystemClock(), InProcessEventBus()
    )
    async with AsyncExitStack() as stack:
        for provider in providers:
            stack.push_async_callback(provider.aclose)
        yield gateway


def _prepare_argus_db(config: Config, failure_prefix: str) -> Path | None:
    try:
        config.data_dir.mkdir(exist_ok=True)
    except OSError as error:
        print(f"{failure_prefix}建不了目录：{config.data_dir}\n  {error}", file=sys.stderr)
        return None
    try:
        migrate_all(MIGRATIONS_ROOT, config.data_dir)
    except MigrationError as error:
        print(f"{failure_prefix}{error}", file=sys.stderr)
        return None
    return config.data_dir / "argus.db"


def _load_audio_config(config_path: Path) -> AudioConfig:
    config = load_config(config_path)
    if config.audio is None:
        raise ConfigError(f"{config_path} 里没有 audio 段，不知道用哪个声卡设备")
    return config.audio


async def _collect(frames: AsyncGenerator[AudioFrame], frame_count: int) -> AudioClip:
    collected: list[AudioFrame] = []
    async with aclosing(frames):
        async for frame in frames:
            collected.append(frame)
            if len(collected) >= frame_count:
                break
    return AudioClip.from_frames(collected)


async def _split_into_chunks(clip: AudioClip) -> AsyncGenerator[AudioChunk]:
    for start in range(0, len(clip.pcm), _PLAY_CHUNK_BYTES):
        yield AudioChunk(clip.pcm[start : start + _PLAY_CHUNK_BYTES])


def _save_wav(path: Path, clip: AudioClip) -> None:
    with _open_wav_for_writing(path) as wav_file:
        wav_file.writeframes(clip.pcm)


def _open_wav_for_writing(path: Path) -> wave.Wave_write:
    wav_file = wave.open(str(path), "wb")
    wav_file.setnchannels(CHANNELS)
    wav_file.setsampwidth(SAMPLE_WIDTH)
    wav_file.setframerate(SAMPLE_RATE)
    return wav_file


def _load_wav(path: Path) -> AudioClip:
    with wave.open(str(path), "rb") as wav_file:
        actual_format = (wav_file.getframerate(), wav_file.getnchannels(), wav_file.getsampwidth())
        if actual_format != (SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH):
            rate, channels, width = actual_format
            raise ValueError(
                f"格式是 {rate}Hz、{channels} 路、{width * 8} 位，"
                f"只支持 {SAMPLE_RATE}Hz、单声道、{SAMPLE_WIDTH * 8} 位（record 录出来的就是）"
            )
        return AudioClip(wav_file.readframes(wav_file.getnframes()))


if __name__ == "__main__":
    sys.exit(main())
