from datetime import datetime
from typing import Protocol

from argus.delivery.outbound import Verdict


class UsefulnessLog(Protocol):
    async def record_sent(self, trigger_id: str, verdict: Verdict, at: datetime) -> None: ...

    async def record_useless(self, trigger_id: str, at: datetime) -> None: ...
