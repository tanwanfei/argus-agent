import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import cast

from argus.kernel.events import Event
from argus.kernel.tracing import trace

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]


@dataclass
class _Subscriber:
    handler: EventHandler
    queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)


class InProcessEventBus:
    def __init__(self) -> None:
        self._subscribers_by_type: dict[type[Event], list[_Subscriber]] = {}
        self._started = False
        self._unfinished = 0
        self._all_finished = asyncio.Event()
        self._all_finished.set()

    def subscribe[EventT: Event](
        self, event_type: type[EventT], handler: Callable[[EventT], Awaitable[None]]
    ) -> None:
        if self._started:
            raise RuntimeError("订阅关系只能在 run() 之前写好，统一写在组合根")
        subscriber = _Subscriber(cast(EventHandler, handler))
        self._subscribers_by_type.setdefault(event_type, []).append(subscriber)

    def publish(self, event: Event) -> None:
        for subscriber in self._subscribers_by_type.get(type(event), []):
            subscriber.queue.put_nowait(event)
            self._unfinished += 1
            self._all_finished.clear()

    async def run(self) -> None:
        self._started = True
        async with asyncio.TaskGroup() as task_group:
            for subscribers in self._subscribers_by_type.values():
                for subscriber in subscribers:
                    task_group.create_task(
                        self._consume(subscriber),
                        name=f"event-consumer:{_name_of(subscriber.handler)}",
                    )

    async def join(self) -> None:
        await self._all_finished.wait()

    async def _consume(self, subscriber: _Subscriber) -> None:
        while True:
            event = await subscriber.queue.get()
            with _trace_of(event):
                try:
                    await subscriber.handler(event)
                except Exception:
                    logger.exception(
                        "事件处理失败：%s 处理 %s 时出错",
                        _name_of(subscriber.handler),
                        type(event).__name__,
                    )
                finally:
                    self._finish_one()

    def _finish_one(self) -> None:
        self._unfinished -= 1
        if self._unfinished == 0:
            self._all_finished.set()


def _trace_of(event: Event) -> AbstractContextManager[None]:
    if event.trace_id is None:
        return nullcontext()
    return trace(event.trace_id)


def _name_of(handler: EventHandler) -> str:
    return getattr(handler, "__qualname__", repr(handler))
