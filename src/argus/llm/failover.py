import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass

from argus.kernel.clock import Clock
from argus.kernel.events import CloudAvailabilityChanged, EventBus
from argus.llm.gateway import (
    CloudHealth,
    LlmDelta,
    LlmError,
    LlmRequest,
    LlmRequestInvalid,
    LlmResponse,
    ModelGateway,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Vendor:
    name: str
    gateway: ModelGateway


class Failover:
    def __init__(
        self, vendors: Sequence[Vendor], *, cooldown_s: float, clock: Clock, bus: EventBus
    ) -> None:
        if not vendors:
            raise ValueError("Failover 至少要有一家厂商")
        names = [vendor.name for vendor in vendors]
        if len(set(names)) != len(names):
            raise ValueError(f"Failover 的厂商重名了：{names}")
        self._vendors = tuple(vendors)
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._bus = bus
        self._health = CloudHealth.UP
        self._cooling_until: dict[str, float] = {}

    async def complete(self, request: LlmRequest) -> LlmResponse:
        failures: list[str] = []
        for vendor in self._try_order():
            try:
                response = await vendor.gateway.complete(request)
            except LlmRequestInvalid:
                raise
            except LlmError as error:
                self._mark_failed(vendor, error)
                failures.append(f"{vendor.name}：{error}")
                continue
            self._mark_working(vendor)
            return response
        raise self._all_failed(failures)

    async def stream(self, request: LlmRequest) -> AsyncGenerator[LlmDelta]:
        failures: list[str] = []
        for vendor in self._try_order():
            handed_over = False
            try:
                async with aclosing(vendor.gateway.stream(request)) as deltas:
                    async for delta in deltas:
                        handed_over = True
                        yield delta
            except LlmRequestInvalid:
                raise
            except LlmError as error:
                self._mark_failed(vendor, error)
                if handed_over:
                    raise
                failures.append(f"{vendor.name}：{error}")
                continue
            self._mark_working(vendor)
            return
        raise self._all_failed(failures)

    def health(self) -> CloudHealth:
        return self._health

    async def probe(self) -> None:
        failures: list[str] = []
        for vendor in self._try_order():
            try:
                await vendor.gateway.probe()
            except LlmError as error:
                failures.append(f"{vendor.name}：{error}")
                continue
            self._mark_working(vendor)
            return
        raise LlmError("探活时所有模型厂商都不通。" + "；".join(failures))

    def _try_order(self) -> list[Vendor]:
        now = self._clock.monotonic()
        ready = [vendor for vendor in self._vendors if not self._is_cooling(vendor, now)]
        cooling = [vendor for vendor in self._vendors if self._is_cooling(vendor, now)]
        return ready + cooling

    def _is_cooling(self, vendor: Vendor, now: float) -> bool:
        cooling_until = self._cooling_until.get(vendor.name)
        return cooling_until is not None and now < cooling_until

    def _mark_failed(self, vendor: Vendor, error: LlmError) -> None:
        self._cooling_until[vendor.name] = self._clock.monotonic() + self._cooldown_s
        logger.warning(
            "模型厂商 %s 调不通，%g 秒内先找别家：%s", vendor.name, self._cooldown_s, error
        )

    def _mark_working(self, vendor: Vendor) -> None:
        if self._cooling_until.pop(vendor.name, None) is not None:
            logger.info("模型厂商 %s 又调通了", vendor.name)
        if self._health is CloudHealth.DOWN:
            self._health = CloudHealth.UP
            logger.info("云端模型又能用了")
            self._bus.publish(
                CloudAvailabilityChanged(ts=self._clock.now(), available=True, reason="")
            )

    def _all_failed(self, failures: list[str]) -> LlmError:
        reason = "；".join(failures)
        if self._health is CloudHealth.UP:
            self._health = CloudHealth.DOWN
            logger.error("云端模型都调不通了：%s", reason)
            self._bus.publish(
                CloudAvailabilityChanged(ts=self._clock.now(), available=False, reason=reason)
            )
        return LlmError("所有模型厂商都调不通。" + reason)
