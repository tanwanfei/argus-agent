import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from argus.codeagent.billing import AgentBilling
from argus.codeagent.runner import AgentJob, CodingAgentRunner, ToolGate, ToolVerdict
from argus.codeagent.tasks import AgentReport, CodeTask, CodeTaskStore, TaskStatus
from argus.codeagent.workspaces import WorkspaceError, Workspaces, folder_name
from argus.governance.code_agent import step_kind
from argus.kernel.clock import Clock
from argus.kernel.events import CodeTaskFinished, CodeTaskNeedsAttention, EventBus
from argus.kernel.ids import IdGen
from argus.llm.gateway import Usage

logger = logging.getLogger(__name__)

_SWEEP_EVERY = timedelta(days=1)

_INTERRUPTED = AgentReport(
    finished=False, turns=0, summary="主进程重启时它还在干，这一次断了。做到一半的留在工作目录里"
)


@dataclass(frozen=True)
class CodeProject:
    path: Path
    writable: bool


@dataclass(frozen=True)
class AgentLimits:
    max_turns: int
    timeout_s: float
    keep_for: timedelta = timedelta(days=7)


_TODO_TOOL = "TodoWrite"


@dataclass(frozen=True)
class LiveProgress:
    running_for: timedelta
    steps: int
    last_step_kind: str | None
    todos_total: int | None
    todos_done: int | None
    doing: str | None


class _LiveRun:
    def __init__(
        self, task: CodeTask, store: CodeTaskStore, gate: ToolGate, started_at: datetime
    ) -> None:
        self.task = task
        self.spent: Usage | None = None
        self._store = store
        self._gate = gate
        self._started_at = started_at
        self._steps = 0
        self._last_step_kind: str | None = None
        self._todos: list[Mapping[str, Any]] | None = None

    async def decide(self, tool_name: str, tool_input: Mapping[str, Any]) -> ToolVerdict:
        self._steps += 1
        self._last_step_kind = step_kind(tool_name) or self._last_step_kind
        todos = tool_input.get("todos") if tool_name == _TODO_TOOL else None
        if isinstance(todos, list):
            self._todos = [todo for todo in todos if isinstance(todo, Mapping)]
        return await self._gate.decide(tool_name, tool_input)

    def progress(self, now: datetime) -> LiveProgress:
        todos = self._todos
        in_progress = [todo for todo in todos or () if todo.get("status") == "in_progress"]
        return LiveProgress(
            running_for=now - self._started_at,
            steps=self._steps,
            last_step_kind=self._last_step_kind,
            todos_total=None if todos is None else len(todos),
            todos_done=(
                None
                if todos is None
                else sum(1 for todo in todos if todo.get("status") == "completed")
            ),
            doing=str(in_progress[0].get("content", "")) or None if in_progress else None,
        )

    async def session_started(self, session_id: str) -> None:
        if session_id == self.task.agent_session_id:
            return
        self.task = replace(self.task, agent_session_id=session_id)
        await self._store.save(self.task)

    def spent_so_far(self, usage: Usage) -> None:
        self.spent = usage


class DispatchRefused(Exception):
    pass


class CodeAgent:
    def __init__(
        self,
        *,
        projects: Mapping[str, CodeProject],
        limits: AgentLimits,
        runner: CodingAgentRunner,
        workspaces: Workspaces,
        store: CodeTaskStore,
        billing: AgentBilling,
        gate_for: Callable[[CodeTask], ToolGate],
        bus: EventBus,
        clock: Clock,
        ids: IdGen,
    ) -> None:
        self._projects = projects
        self._limits = limits
        self._runner = runner
        self._workspaces = workspaces
        self._store = store
        self._billing = billing
        self._gate_for = gate_for
        self._bus = bus
        self._clock = clock
        self._ids = ids
        self._task_group: asyncio.TaskGroup | None = None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stopping: set[str] = set()
        self._live_runs: dict[str, _LiveRun] = {}

    @property
    def project_names(self) -> tuple[str, ...]:
        return tuple(self._projects)

    async def run(self) -> None:
        for task in await self._store.with_status(TaskStatus.RUNNING):
            await self._store.save(
                replace(
                    task,
                    status=TaskStatus.FAILED,
                    finished_at=self._clock.now(),
                    report=_INTERRUPTED,
                )
            )
            logger.info("派活：任务 %s 上次没跑完，标成没干成", task.id)
        async with asyncio.TaskGroup() as task_group:
            self._task_group = task_group
            try:
                while True:
                    try:
                        await self.sweep_once()
                    except Exception:
                        logger.exception("派活：清理过期的工作目录出错了")
                    await self._clock.sleep(_SWEEP_EVERY.total_seconds())
            finally:
                self._task_group = None

    async def sweep_once(self) -> None:
        cutoff = self._clock.now() - self._limits.keep_for
        for task in await self._store.with_status(
            TaskStatus.FINISHED, TaskStatus.FAILED, TaskStatus.PUBLISHED
        ):
            if task.id in self._running or task.finished_at is None or task.finished_at > cutoff:
                continue
            if task.question is not None:
                continue
            await self._remove_workspace(task)

    async def discard(self, task_id: str) -> CodeTask:
        task = await self._pick(task_id)
        if task.status is TaskStatus.RUNNING:
            raise DispatchRefused("它还在干，等它做完再删")
        if task.status is TaskStatus.REMOVED:
            raise DispatchRefused("这个任务的工作目录已经清掉了")
        if not await self._remove_workspace(task):
            raise DispatchRefused("工作目录没删掉，主机日志里有原因")
        removed = await self._store.get(task.id)
        assert removed is not None
        return removed

    async def _remove_workspace(self, task: CodeTask) -> bool:
        try:
            await self._workspaces.remove(task.workspace)
        except (WorkspaceError, OSError) as error:
            logger.warning("派活：任务 %s 的工作目录没删掉：%s", task.id, type(error).__name__)
            return False
        await self._store.save(replace(task, status=TaskStatus.REMOVED))
        logger.info("派活：清掉了任务 %s 的工作目录", task.id)
        return True

    async def dispatch(self, project_name: str, request: str) -> CodeTask:
        project = self._projects.get(project_name)
        if project is None:
            names = "、".join(self._projects) or "（还没配）"
            raise DispatchRefused(f"没有叫「{project_name}」的目录。能派活的有：{names}")
        await self._check_can_start()
        task_id = self._ids.new("code")
        dispatched_at = self._clock.now()
        try:
            workspace = await self._workspaces.create(
                project.path, task_id, folder_name(task_id, project_name, dispatched_at)
            )
        except WorkspaceError as error:
            raise DispatchRefused(str(error)) from error
        task = CodeTask(
            id=task_id,
            project=project_name,
            request=request,
            workspace=workspace,
            status=TaskStatus.RUNNING,
            created_at=dispatched_at,
        )
        await self._store.save(task)
        logger.info("派活：开工，任务 %s（%s）", task_id, workspace.kind.value)
        self._start(task, request)
        return task

    async def _check_can_start(self) -> None:
        if self._task_group is None:
            raise DispatchRefused("派活的服务没在跑，可能正在停机")
        if not await self._billing.budget_left():
            raise DispatchRefused(
                "本月的模型预算用完了，派不了活。下个月 1 日恢复，急用可以追加上限"
            )

    async def follow_up(self, task_id: str | None, request: str) -> CodeTask:
        task = await self._pick(task_id)
        if task.status is TaskStatus.RUNNING:
            raise DispatchRefused("它还在干，等它做完再追加")
        if task.status is TaskStatus.REMOVED:
            raise DispatchRefused("这个任务的工作目录已经清掉了，要做就重新派一个")
        if task.agent_session_id is None:
            raise DispatchRefused("这个任务的代理没起来过，接不上，重新派一个吧")
        await self._check_can_start()
        prompt = request if task.question is None else f"用户回答了你的问题：{request}"
        resumed = replace(task, status=TaskStatus.RUNNING)
        await self._store.save(resumed)
        logger.info("派活：续接任务 %s", task.id)
        self._start(resumed, prompt)
        return resumed

    async def stop_running(self, task_id: str | None) -> list[CodeTask]:
        task_ids = list(self._running) if task_id is None else [task_id]
        stopped = []
        for running_id in task_ids:
            task = await self._store.get(running_id)
            running = self._running.get(running_id)
            if task is None or running is None:
                continue
            self._stopping.add(running_id)
            running.cancel()
            await asyncio.wait({running})
            stopped.append(task)
        return stopped

    async def task(self, task_id: str) -> CodeTask | None:
        return await self._store.get(task_id)

    def live_progress(self, task_id: str) -> LiveProgress | None:
        live_run = self._live_runs.get(task_id)
        return None if live_run is None else live_run.progress(self._clock.now())

    async def recent(self, limit: int) -> list[CodeTask]:
        return await self._store.recent(limit)

    async def publish(self, task_id: str | None) -> tuple[CodeTask, str]:
        task = await self._pick(task_id)
        if task.status is TaskStatus.RUNNING:
            raise DispatchRefused("它还在干，等它做完再发布")
        if task.status is TaskStatus.REMOVED:
            raise DispatchRefused("这个任务的工作目录已经清掉了，没东西可发布")
        project = self._projects.get(task.project)
        if project is None:
            raise DispatchRefused(f"code_projects.yaml 里已经没有「{task.project}」了，写不回去")
        if not project.writable:
            raise DispatchRefused(
                f"「{task.project}」配的是不能写回（code_projects.yaml 里没写 writable: true），"
                f"做好的在 {task.workspace.path}"
            )
        try:
            said = await self._workspaces.publish(task.workspace)
        except WorkspaceError as error:
            raise DispatchRefused(str(error)) from error
        published = replace(task, status=TaskStatus.PUBLISHED, published_to=said)
        await self._store.save(published)
        logger.info("派活：任务 %s 写回去了", task.id)
        return published, said

    async def _pick(self, task_id: str | None) -> CodeTask:
        if task_id is not None:
            task = await self._store.get(task_id)
            if task is None:
                raise DispatchRefused(f"没有任务号是 {task_id} 的任务")
            return task
        latest = await self._store.recent(1)
        if not latest:
            raise DispatchRefused("还没派过活")
        return latest[0]

    def _start(self, task: CodeTask, prompt: str) -> None:
        assert self._task_group is not None
        self._running[task.id] = self._task_group.create_task(
            self._carry_out(task, prompt), name=f"code-task-{task.id}"
        )

    async def _carry_out(self, task: CodeTask, prompt: str) -> None:
        progress = _LiveRun(task, self._store, self._gate_for(task), self._clock.now())
        self._live_runs[task.id] = progress
        try:
            job = AgentJob(
                workspace=task.workspace.path,
                temp_dir=task.workspace.temp_dir,
                prompt=prompt,
                resume_session_id=task.agent_session_id,
                max_turns=self._limits.max_turns,
                timeout_s=self._limits.timeout_s,
            )
            run = await self._runner.run(job, progress, progress)
            task = progress.task
            if run.usage is not None:
                await self._billing.record(run.usage)
            try:
                changed: tuple[str, ...] | None = tuple(
                    await self._workspaces.changed_files(task.workspace)
                )
            except WorkspaceError:
                changed = None
            done = replace(
                task,
                status=TaskStatus.FINISHED if run.session_id is not None else TaskStatus.FAILED,
                agent_session_id=run.session_id or task.agent_session_id,
                finished_at=self._clock.now(),
                report=run.report,
                changed_files=None if changed is None else len(changed),
                changed_paths=changed,
            )
            await self._store.save(done)
            if done.question is None:
                self._announce(done)
            else:
                self._ask_you(done, done.question)
            logger.info(
                "派活：任务 %s 跑完，%s，%d 轮", task.id, done.status.value, run.report.turns
            )
        except asyncio.CancelledError:
            await self._bill_what_was_spent(progress)
            if task.id not in self._stopping:
                raise
            await self._store.save(
                replace(
                    progress.task,
                    status=TaskStatus.FINISHED,
                    finished_at=self._clock.now(),
                    report=AgentReport(
                        finished=False,
                        turns=0,
                        summary="你让它停下了，已经改了的留在工作目录里，说接着做可以续上",
                    ),
                )
            )
            logger.info("派活：任务 %s 按你说的停下了", task.id)
        except Exception:
            logger.exception("派活：任务 %s 出错了", task.id)
            await self._bill_what_was_spent(progress)
            await self._save_failed(progress.task)
        finally:
            self._running.pop(task.id, None)
            self._stopping.discard(task.id)
            self._live_runs.pop(task.id, None)

    async def _bill_what_was_spent(self, progress: _LiveRun) -> None:
        if progress.spent is None:
            return
        try:
            await self._billing.record(progress.spent)
        except Exception:
            logger.exception("派活：任务 %s 中途断了，花的没记上账", progress.task.id)

    def _announce(self, task: CodeTask) -> None:
        report = task.report
        assert report is not None
        self._bus.publish(
            CodeTaskFinished(
                ts=self._clock.now(),
                project=task.project,
                session_id=task.id,
                dispatched=True,
                finished=report.finished,
                changed_files=task.changed_files,
                tests=report.tests,
                details=report.summary or None,
                where=task.where_it_is(),
            )
        )

    def _ask_you(self, task: CodeTask, question: str) -> None:
        self._bus.publish(
            CodeTaskNeedsAttention(
                ts=self._clock.now(),
                project=task.project,
                session_id=task.id,
                dispatched=True,
                question=question,
            )
        )

    async def _save_failed(self, task: CodeTask) -> None:
        failed = replace(
            task,
            status=TaskStatus.FAILED,
            finished_at=self._clock.now(),
            report=AgentReport(
                finished=False, turns=0, summary="ARGUS 这边出错了，主机日志里有原因"
            ),
        )
        try:
            await self._store.save(failed)
        except Exception:
            logger.exception("派活：任务 %s 的结果没记下来", task.id)
        self._announce(failed)
