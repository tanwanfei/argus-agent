import logging
from collections.abc import Sequence
from datetime import datetime, time, timedelta

from argus.delivery.delivery import Delivery
from argus.kernel.clock import Clock
from argus.proactive.digest import DigestItem, DigestQueue
from argus.scheduler.scheduler import Scheduler
from argus.scheduler.store import Job

logger = logging.getLogger(__name__)

DIGEST_KIND = "proactive.digest"

MISSED_GRACE = timedelta(hours=2)

_BODY_CHARS = 600


def next_digest_at(after: datetime, times: Sequence[time]) -> datetime:
    for days_later in (0, 1):
        day = after.date() + timedelta(days=days_later)
        for at in sorted(times):
            candidate = datetime.combine(day, at, tzinfo=after.tzinfo)
            if candidate > after:
                return candidate
    raise ValueError("没有简报时刻")


def digest_body(items: Sequence[DigestItem]) -> tuple[str, list[DigestItem]]:
    lines: list[str] = []
    length = 0
    for item in items:
        line = f"{len(lines) + 1}. {item.text}"
        if not lines and len(line) > _BODY_CHARS:
            line = line[:_BODY_CHARS] + "…"
        elif lines and length + 1 + len(line) > _BODY_CHARS:
            break
        length += len(line) + (1 if lines else 0)
        lines.append(line)
    listed = list(items[: len(lines)])
    left = len(items) - len(listed)
    if left:
        lines.append(f"还有 {left} 条，下次简报再列。")
    return "\n".join(lines), listed


class DailyDigest:
    def __init__(
        self,
        queue: DigestQueue,
        delivery: Delivery,
        scheduler: Scheduler,
        *,
        times: Sequence[time],
        clock: Clock,
    ) -> None:
        self._queue = queue
        self._delivery = delivery
        self._scheduler = scheduler
        self._times = times
        self._clock = clock

    async def schedule_next(self) -> None:
        try:
            await self._schedule_after(self._clock.now())
        except Exception:
            logger.exception("开机时没排上简报")

    async def send(self, job: Job) -> None:
        await self._schedule_after(job.due_at)
        pending = await self._queue.pending()
        if not pending:
            logger.info("没有攒着的，简报不发")
            return
        body, listed = digest_body(pending)
        if not await self._delivery.push_digest(body):
            raise RuntimeError("简报没推出去")
        await self._queue.remove([item.id for item in listed])
        logger.info(
            "简报推出去了，列了 %d 条，留到下一次 %d 条", len(listed), len(pending) - len(listed)
        )

    async def missed(self, job: Job) -> None:
        logger.info("%s 的简报错过太久，不补发，攒着的留到下一次", job.due_at)

    async def _schedule_after(self, after: datetime) -> None:
        due_at = next_digest_at(after, self._times)
        await self._scheduler.schedule(
            DIGEST_KIND, due_at, {}, key=f"{DIGEST_KIND}@{due_at.isoformat()}", ttl=MISSED_GRACE
        )
