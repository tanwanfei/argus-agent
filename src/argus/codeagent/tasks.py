from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from argus.codeagent.workspaces import Workspace

WAITING_LINE = "等用户回话"


def asked_question(summary: str) -> str | None:
    lines = summary.rstrip().splitlines()
    if not lines or lines[-1].strip(" *。.\t") != WAITING_LINE:
        return None
    return "\n".join(lines[:-1]).strip()


_FILES_LISTED = 10


def listed_files(paths: Sequence[str]) -> str:
    listed = "、".join(paths[:_FILES_LISTED])
    return listed if len(paths) <= _FILES_LISTED else f"{listed} 等 {len(paths)} 个"


class TaskStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    PUBLISHED = "published"
    REMOVED = "removed"


@dataclass(frozen=True)
class AgentReport:
    finished: bool
    turns: int
    summary: str
    tests: str | None = None

    @property
    def question(self) -> str | None:
        return asked_question(self.summary)


@dataclass(frozen=True)
class CodeTask:
    id: str
    project: str
    request: str
    workspace: Workspace
    status: TaskStatus
    created_at: datetime
    agent_session_id: str | None = None
    finished_at: datetime | None = None
    report: AgentReport | None = None
    changed_files: int | None = None
    changed_paths: tuple[str, ...] | None = None
    published_to: str | None = None

    def where_it_is(self) -> str | None:
        if self.status is TaskStatus.REMOVED:
            return None
        where = f"做好的在 {self.workspace.path}"
        if self.changed_paths:
            where += "：" + listed_files(self.changed_paths)
        return where

    @property
    def question(self) -> str | None:
        if self.status is not TaskStatus.FINISHED or self.report is None:
            return None
        return self.report.question


class CodeTaskStore(Protocol):
    async def save(self, task: CodeTask) -> None: ...

    async def get(self, task_id: str) -> CodeTask | None: ...

    async def recent(self, limit: int) -> list[CodeTask]: ...

    async def with_status(self, *statuses: TaskStatus) -> list[CodeTask]: ...
