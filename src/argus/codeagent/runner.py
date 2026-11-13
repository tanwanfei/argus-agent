import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from argus.codeagent.tasks import AgentReport
from argus.llm.gateway import Usage

TESTS_LINE_PATTERN = re.compile(r"^\s*测试[:：]\s*(通过|没通过|没跑)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class AgentJob:
    workspace: Path
    temp_dir: Path
    prompt: str
    resume_session_id: str | None
    max_turns: int
    timeout_s: float


@dataclass(frozen=True)
class ToolVerdict:
    allowed: bool
    reason: str = ""


class ToolGate(Protocol):
    async def decide(self, tool_name: str, tool_input: Mapping[str, Any]) -> ToolVerdict: ...


class RunProgress(Protocol):
    async def session_started(self, session_id: str) -> None: ...

    def spent_so_far(self, usage: Usage) -> None: ...


@dataclass(frozen=True)
class AgentRun:
    session_id: str | None
    report: AgentReport
    usage: Usage | None


class CodingAgentRunner(Protocol):
    async def run(self, job: AgentJob, gate: ToolGate, progress: RunProgress) -> AgentRun: ...


def reported_tests(summary: str) -> str | None:
    found = TESTS_LINE_PATTERN.findall(summary)
    return found[-1] if found else None
