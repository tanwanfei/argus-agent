import logging

from argus.kernel.clock import Clock
from argus.llm.gateway import CloudHealth, LlmError, ModelGateway

logger = logging.getLogger(__name__)

PROBE_INTERVAL_S = 30.0


class CloudProbe:
    def __init__(
        self, gateway: ModelGateway, clock: Clock, *, interval_s: float = PROBE_INTERVAL_S
    ) -> None:
        self._gateway = gateway
        self._clock = clock
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            await self._clock.sleep(self._interval_s)
            if self._gateway.health() is CloudHealth.UP:
                continue
            try:
                await self._gateway.probe()
            except LlmError:
                continue
            except Exception:
                logger.exception("探活出错")
