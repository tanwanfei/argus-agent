import asyncio
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)
from claude_agent_sdk.types import HookContext, HookInput, HookJSONOutput
from pydantic import SecretStr

from argus.codeagent.runner import AgentJob, AgentRun, RunProgress, ToolGate, reported_tests
from argus.codeagent.tasks import WAITING_LINE, AgentReport, asked_question
from argus.llm.gateway import Usage

logger = logging.getLogger(__name__)

_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_DISALLOWED_TOOLS = ["WebSearch", "WebFetch", "AskUserQuestion"]
_INSTRUCTIONS = f"""你是 ARGUS（一个个人管家）派来在后台干活的代理。用户不在电脑前，但可以回话，只是要等一阵。
- 任务说得不清楚、有几种做法要用户拿主意、或者要做收不回来的事：停下来问用户，不要猜着往下做。问的办法：直接结束这一次，最后的回复里只写要问的问题（一次问完，编号列出，给出你建议的选项），最后一行单独写：{WAITING_LINE}。这里没有提问工具，不要去找。用户回话后会接着这个会话告诉你。能自己定的小事不要问
- 只在当前工作目录里改东西。读文件、跑命令、装依赖都不用等批准，直接做
- 有几样会被直接拒绝：往工作目录外写文件、git push 和部署（要不要发布由用户决定）、连别的机器、按名字杀进程（要停你自己起的进程按进程号停）、关机和改系统设置。被拒时会告诉你原因，换个办法接着做，不要原样重试
- 临时文件（截图、下载的东西、用完就扔的脚本）放环境变量 TEMP 指的目录，不要放进工作目录：工作目录里的都会算成你的改动
- 做完用中文总结做了什么，三五句话，说给用户听的，不贴代码
- 做完时总结的最后一行单独写测试结果，只能是这三种之一：测试：通过 / 测试：没通过 / 测试：没跑"""


class ClaudeSdkRunner:
    def __init__(self, *, base_url: str, api_key: SecretStr, model: str, config_dir: Path) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._config_dir = config_dir

    async def run(self, job: AgentJob, gate: ToolGate, progress: RunProgress) -> AgentRun:
        job.temp_dir.mkdir(parents=True, exist_ok=True)
        options = self._options(job, gate)
        session_id: str | None = None
        last_text = ""
        result: ResultMessage | None = None
        usage_by_reply: dict[str, Mapping[str, Any]] = {}
        try:
            async with asyncio.timeout(job.timeout_s):
                async for message in query(prompt=job.prompt, options=options):
                    told_session_id = _session_id_in(message)
                    if told_session_id is not None and told_session_id != session_id:
                        session_id = told_session_id
                        await progress.session_started(session_id)
                    if isinstance(message, AssistantMessage):
                        if message.usage:
                            reply_id = message.message_id or f"第 {len(usage_by_reply)} 条"
                            usage_by_reply[reply_id] = message.usage
                            spent = _usage_from(list(usage_by_reply.values()), self._model)
                            if spent is not None:
                                progress.spent_so_far(spent)
                        texts = [
                            block.text for block in message.content if isinstance(block, TextBlock)
                        ]
                        if texts:
                            last_text = "\n".join(texts)
                    elif isinstance(message, ResultMessage):
                        result = message
        except TimeoutError:
            logger.warning("派活：到了时间上限，停下了")
            return AgentRun(
                session_id,
                AgentReport(
                    finished=False,
                    turns=len(usage_by_reply) if result is None else result.num_turns,
                    summary=(
                        f"到了 {job.timeout_s / 60:g} 分钟的上限，停下了。"
                        "做到一半的留在工作目录里，说接着做可以续上"
                    ),
                ),
                usage=_usage_from(list(usage_by_reply.values()), self._model),
            )
        except ClaudeSDKError as error:
            if result is not None:
                return _run_of(result, last_text, self._model)
            logger.error("派活：代理没跑起来或中途断了：%s", type(error).__name__)
            return AgentRun(
                session_id,
                AgentReport(finished=False, turns=len(usage_by_reply), summary=_why_broken(error)),
                usage=_usage_from(list(usage_by_reply.values()), self._model),
            )
        return _run_of(result, last_text, self._model)

    def _options(self, job: AgentJob, gate: ToolGate) -> ClaudeAgentOptions:
        async def ask_argus(
            hook_input: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            tool_name = str(hook_input.get("tool_name", ""))
            tool_input = hook_input.get("tool_input", {})
            if not isinstance(tool_input, Mapping):
                tool_input = {}
            verdict = await gate.decide(tool_name, tool_input)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if verdict.allowed else "deny",
                    "permissionDecisionReason": verdict.reason,
                }
            }

        key = self._api_key.get_secret_value()
        return ClaudeAgentOptions(
            cwd=job.workspace,
            max_turns=job.max_turns,
            max_buffer_size=_MAX_MESSAGE_BYTES,
            resume=job.resume_session_id,
            model=self._model,
            permission_mode="default",
            setting_sources=[],
            disallowed_tools=list(_DISALLOWED_TOOLS),
            system_prompt={"type": "preset", "preset": "claude_code", "append": _INSTRUCTIONS},
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[ask_argus])]},
            env={
                "ANTHROPIC_BASE_URL": self._base_url,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_API_KEY": key,
                "ANTHROPIC_MODEL": self._model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": self._model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": self._model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": self._model,
                "ANTHROPIC_SMALL_FAST_MODEL": self._model,
                "CLAUDE_CODE_SUBAGENT_MODEL": self._model,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CONFIG_DIR": str(self._config_dir),
                "TEMP": str(job.temp_dir),
                "TMP": str(job.temp_dir),
                "TMPDIR": str(job.temp_dir),
            },
            stderr=lambda _line: None,
        )


def _run_of(result: ResultMessage | None, last_text: str, model: str) -> AgentRun:
    if result is None:
        return AgentRun(
            None, AgentReport(finished=False, turns=0, summary="代理没给结果就结束了"), None
        )
    finished = not result.is_error and result.subtype == "success"
    if finished:
        summary = (result.result or last_text).strip()
    else:
        logger.warning(
            "派活：代理没做完就停了：%s / %s / 接口状态码 %s",
            result.subtype,
            result.terminal_reason,
            result.api_error_status,
        )
        last_words = last_text.strip()
        summary = _why_stopped(result) + (f"\n{last_words}" if last_words else "")
    report = AgentReport(
        finished=finished and asked_question(summary) is None,
        turns=result.num_turns,
        summary=summary,
        tests=reported_tests(summary),
    )
    return AgentRun(result.session_id, report, _usage_of(result, model))


def _session_id_in(message: object) -> str | None:
    if isinstance(message, SystemMessage):
        session_id = message.data.get("session_id") if message.subtype == "init" else None
        return session_id if isinstance(session_id, str) else None
    if isinstance(message, AssistantMessage | ResultMessage):
        return message.session_id
    return None


def _why_stopped(result: ResultMessage) -> str:
    if result.subtype == "error_max_turns" or result.terminal_reason == "max_turns":
        return f"到了 {result.num_turns} 轮的上限，停下了，说接着做可以续上。"
    if result.api_error_status is not None or result.terminal_reason == "api_error":
        status = "" if result.api_error_status is None else f"（状态码 {result.api_error_status}）"
        return f"模型接口出错{status}，停下了。"
    return "代理运行中出错，停下了。"


def _why_broken(error: ClaudeSDKError) -> str:
    if isinstance(error, CLIConnectionError):
        return "代理没跑起来：Claude Code 启动不了"
    if isinstance(error, CLIJSONDecodeError):
        return "代理的一条输出太大或者读不懂，这一次断了。做到一半的留在工作目录里"
    return "代理中途断了。做到一半的留在工作目录里"


def _usage_of(result: ResultMessage, model: str) -> Usage | None:
    totals: list[Mapping[str, Any]] = []
    if result.usage:
        totals = [result.usage]
    elif result.model_usage:
        totals = [
            {
                "input_tokens": entry.get("inputTokens", 0),
                "cache_read_input_tokens": entry.get("cacheReadInputTokens", 0),
                "cache_creation_input_tokens": entry.get("cacheCreationInputTokens", 0),
                "output_tokens": entry.get("outputTokens", 0),
            }
            for entry in result.model_usage.values()
        ]
    return _usage_from(totals, model)


def _usage_from(totals: Sequence[Mapping[str, Any]], model: str) -> Usage | None:
    if not totals:
        return None
    cached = sum(int(entry.get("cache_read_input_tokens") or 0) for entry in totals)
    uncached = sum(
        int(entry.get("input_tokens") or 0) + int(entry.get("cache_creation_input_tokens") or 0)
        for entry in totals
    )
    output = sum(int(entry.get("output_tokens") or 0) for entry in totals)
    return Usage(
        model=model,
        input_tokens=uncached + cached,
        cached_input_tokens=cached,
        output_tokens=output,
    )
