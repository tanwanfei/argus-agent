from datetime import timedelta
from typing import Protocol

from argus.infra.sqlite.app_signature import AppSignature
from argus.kernel.clock import Clock
from argus.kernel.events import AppSignatureExpiring, EventBus

_CHECK_INTERVAL_SECONDS = 3600


class AppSignatureReader(Protocol):
    async def latest(self) -> AppSignature | None: ...


class SignatureWatch:
    def __init__(
        self,
        signatures: AppSignatureReader,
        alert_before: timedelta,
        clock: Clock,
        bus: EventBus,
    ) -> None:
        self._signatures = signatures
        self._alert_before = alert_before
        self._clock = clock
        self._bus = bus

    async def run(self) -> None:
        while True:
            await self.check_once()
            await self._clock.sleep(_CHECK_INTERVAL_SECONDS)

    async def check_once(self) -> None:
        signature = await self._signatures.latest()
        if signature is None:
            return
        now = self._clock.now()
        if now < signature.expires_at - self._alert_before:
            return
        if now >= signature.expires_at:
            return
        self._bus.publish(
            AppSignatureExpiring(
                expires_at=signature.expires_at, reported_at=signature.reported_at, ts=now
            )
        )
