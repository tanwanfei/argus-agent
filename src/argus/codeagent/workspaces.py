import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class WorkspaceKind(StrEnum):
    GIT = "git"
    FOLDER = "folder"


@dataclass(frozen=True)
class Workspace:
    kind: WorkspaceKind
    path: Path
    source: Path
    branch: str | None = None
    base_commit: str | None = None

    @property
    def temp_dir(self) -> Path:
        return self.path.with_name(self.path.name + ".tmp")


_NOT_ALLOWED_IN_FOLDER_NAMES = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def folder_name(task_id: str, project: str, dispatched_at: datetime) -> str:
    project_part = _NOT_ALLOWED_IN_FOLDER_NAMES.sub("_", project)
    short_id = task_id.removeprefix("code_")[:8]
    return f"{dispatched_at:%m%d-%H%M}_{project_part}_{short_id}"


class WorkspaceError(Exception):
    pass


class Workspaces(Protocol):
    async def create(self, source: Path, task_id: str, folder: str) -> Workspace: ...

    async def changed_files(self, workspace: Workspace) -> list[str]: ...

    async def publish(self, workspace: Workspace) -> str: ...

    async def remove(self, workspace: Workspace) -> None: ...
