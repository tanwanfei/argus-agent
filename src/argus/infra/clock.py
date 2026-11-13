import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC).astimezone()

    def monotonic(self) -> float:
        return time.perf_counter()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    @asynccontextmanager
    async def timeout(self, seconds: float) -> AsyncGenerator[None]:
        async with asyncio.timeout(seconds):
            yield
