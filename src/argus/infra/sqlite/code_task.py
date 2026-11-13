import json
import sqlite3
from datetime import datetime
from pathlib import Path

from argus.codeagent.tasks import AgentReport, CodeTask, TaskStatus
from argus.codeagent.workspaces import Workspace, WorkspaceKind
from argus.infra.sqlite.database import SqliteDatabase

_COLUMNS = (
    "id, project, request, status, created_at, workspace_kind, workspace_path, source_path, "
    "branch, base_commit, agent_session_id, finished_at, report_finished, report_turns, "
    "summary, tests, changed_files, changed_paths, published_to"
)
_UPSERT = f"INSERT OR REPLACE INTO code_task ({_COLUMNS}) VALUES ({', '.join('?' * 19)})"
_SELECT_ONE = f"SELECT {_COLUMNS} FROM code_task WHERE id = ?"
_SELECT_RECENT = (
    f"SELECT {_COLUMNS} FROM code_task ORDER BY julianday(created_at) DESC, id DESC LIMIT ?"
)
_SELECT_WITH_STATUS = (
    f"SELECT {_COLUMNS} FROM code_task WHERE status IN (SELECT value FROM json_each(?)) "
    "ORDER BY julianday(created_at), id"
)


class SqliteCodeTaskStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def save(self, task: CodeTask) -> None:
        row = _row_of(task)

        def upsert(connection: sqlite3.Connection) -> None:
            connection.execute(_UPSERT, row)

        await self._database.write(upsert)

    async def get(self, task_id: str) -> CodeTask | None:
        def select_one(connection: sqlite3.Connection) -> CodeTask | None:
            row = connection.execute(_SELECT_ONE, (task_id,)).fetchone()
            return None if row is None else _task_of(row)

        return await self._database.read(select_one)

    async def recent(self, limit: int) -> list[CodeTask]:
        def select_recent(connection: sqlite3.Connection) -> list[CodeTask]:
            return [_task_of(row) for row in connection.execute(_SELECT_RECENT, (limit,))]

        return await self._database.read(select_recent)

    async def with_status(self, *statuses: TaskStatus) -> list[CodeTask]:
        statuses_json = "[" + ", ".join(f'"{status.value}"' for status in statuses) + "]"

        def select_with_status(connection: sqlite3.Connection) -> list[CodeTask]:
            return [
                _task_of(row) for row in connection.execute(_SELECT_WITH_STATUS, (statuses_json,))
            ]

        return await self._database.read(select_with_status)


def _row_of(task: CodeTask) -> tuple[object, ...]:
    workspace = task.workspace
    report = task.report
    return (
        task.id,
        task.project,
        task.request,
        task.status.value,
        task.created_at.isoformat(),
        workspace.kind.value,
        str(workspace.path),
        str(workspace.source),
        workspace.branch,
        workspace.base_commit,
        task.agent_session_id,
        None if task.finished_at is None else task.finished_at.isoformat(),
        None if report is None else int(report.finished),
        None if report is None else report.turns,
        None if report is None else report.summary,
        None if report is None else report.tests,
        task.changed_files,
        None if task.changed_paths is None else json.dumps(task.changed_paths, ensure_ascii=False),
        task.published_to,
    )


def _task_of(row: tuple[object, ...]) -> CodeTask:
    (
        task_id,
        project,
        request,
        status,
        created_at,
        workspace_kind,
        workspace_path,
        source_path,
        branch,
        base_commit,
        agent_session_id,
        finished_at,
        report_finished,
        report_turns,
        summary,
        tests,
        changed_files,
        changed_paths,
        published_to,
    ) = row
    report = None
    if report_finished is not None:
        report = AgentReport(
            finished=bool(report_finished),
            turns=int(str(report_turns)),
            summary=str(summary),
            tests=None if tests is None else str(tests),
        )
    return CodeTask(
        id=str(task_id),
        project=str(project),
        request=str(request),
        workspace=Workspace(
            kind=WorkspaceKind(str(workspace_kind)),
            path=Path(str(workspace_path)),
            source=Path(str(source_path)),
            branch=None if branch is None else str(branch),
            base_commit=None if base_commit is None else str(base_commit),
        ),
        status=TaskStatus(str(status)),
        created_at=datetime.fromisoformat(str(created_at)),
        agent_session_id=None if agent_session_id is None else str(agent_session_id),
        finished_at=None if finished_at is None else datetime.fromisoformat(str(finished_at)),
        report=report,
        changed_files=None if changed_files is None else int(str(changed_files)),
        changed_paths=(None if changed_paths is None else tuple(json.loads(str(changed_paths)))),
        published_to=None if published_to is None else str(published_to),
    )
