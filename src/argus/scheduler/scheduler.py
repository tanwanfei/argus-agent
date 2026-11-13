import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from argus.kernel.clock import Clock
from argus.kernel.ids import IdGen
from argus.scheduler.store import Job, JobStore

logger = logging.getLogger(__name__)

JobHandler = Callable[[Job], Awaitable[None]]

_MAX_WAIT_S = 60.0

_FIRST_RETRY_S = 30.0
_MAX_RETRY_S = 3600.0
_MAX_ATTEMPTS = 10


class Scheduler:
    def __init__(self, store: JobStore, *, clock: Clock, ids: IdGen) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._handlers: dict[str, JobHandler] = {}
        self._on_expired: dict[str, JobHandler] = {}
        self._changed: dict[str, asyncio.Event] = {}
        self._started = False

    def register(
        self, kind: str, handler: JobHandler, *, on_expired: JobHandler | None = None
    ) -> None:
        if self._started:
            raise RuntimeError("处理函数只能在 run() 之前注册，统一写在组合根")
        if kind in self._handlers:
            raise ValueError(f"{kind} 已经注册过处理函数")
        self._handlers[kind] = handler
        if on_expired is not None:
            self._on_expired[kind] = on_expired
        self._changed[kind] = asyncio.Event()

    async def schedule(
        self,
        kind: str,
        due_at: datetime,
        payload: Mapping[str, Any],
        *,
        key: str | None = None,
        ttl: timedelta | None = None,
    ) -> str:
        if kind not in self._handlers:
            raise ValueError(f"{kind} 没有注册处理函数，排进去也没人办")
        if due_at.tzinfo is None:
            raise ValueError("due_at 要带时区：存进表里按时刻比先后")
        if ttl is not None and ttl <= timedelta(0):
            raise ValueError("ttl 要大于 0：一到点就过期的任务永远不会按原样办")
        job = Job(
            id=self._ids.new("job"), kind=kind, due_at=due_at, payload=payload, key=key, ttl=ttl
        )
        await self._store.add(job)
        self._changed[kind].set()
        return job.id

    async def enqueue(self, kind: str, payload: Mapping[str, Any]) -> str:
        return await self.schedule(kind, self._clock.now(), payload)

    async def cancel(self, job_id_or_key: str) -> None:
        await self._store.delete(job_id_or_key)

    async def pending(self, kind: str) -> list[Job]:
        return await self._store.pending(kind)

    async def run(self) -> None:
        self._started = True
        async with asyncio.TaskGroup() as task_group:
            for kind in self._handlers:
                task_group.create_task(self._run_kind(kind), name=f"scheduler:{kind}")

    async def _run_kind(self, kind: str) -> None:
        while True:
            try:
                await self._run_next(kind)
            except Exception:
                logger.exception("任务表读写出错，%g 秒后再查：%s", _MAX_WAIT_S, kind)
                await self._clock.sleep(_MAX_WAIT_S)

    async def _run_next(self, kind: str) -> None:
        changed = self._changed[kind]
        changed.clear()
        job = await self._store.earliest(kind)
        if job is not None and job.run_at <= self._clock.now():
            await self._handle(job)
            return
        wait_s = _MAX_WAIT_S
        if job is not None:
            wait_s = min((job.run_at - self._clock.now()).total_seconds(), _MAX_WAIT_S)
        try:
            async with self._clock.timeout(wait_s):
                await changed.wait()
        except TimeoutError:
            pass

    async def _handle(self, job: Job) -> None:
        handler = self._handlers[job.kind]
        if self._is_expired(job):
            on_expired = self._on_expired.get(job.kind)
            if on_expired is None:
                logger.warning("任务过了有效期、没有降级办法，不再办：%s %s", job.kind, job.id)
                await self._store.delete(job.id)
                return
            logger.info("任务过了有效期，改按降级办：%s %s", job.kind, job.id)
            handler = on_expired
        try:
            await handler(job)
        except Exception:
            await self._after_failure(job)
            return
        await self._store.delete(job.id)

    def _is_expired(self, job: Job) -> bool:
        return job.ttl is not None and self._clock.now() > job.due_at + job.ttl

    async def _after_failure(self, job: Job) -> None:
        failures = job.attempts + 1
        if failures >= _MAX_ATTEMPTS:
            logger.exception("任务试了 %d 次都没办成，不再办：%s %s", failures, job.kind, job.id)
            await self._store.mark_failed(job.id)
            return
        delay_s = min(_FIRST_RETRY_S * 2 ** (failures - 1), _MAX_RETRY_S)
        logger.warning(
            "任务没办成（第 %d 次），%.0f 秒后再试：%s %s",
            failures,
            delay_s,
            job.kind,
            job.id,
            exc_info=True,
        )
        retry_at = self._clock.now() + timedelta(seconds=delay_s)
        await self._store.retry_later(job.id, retry_at=retry_at)
