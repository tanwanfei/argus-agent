import logging
from collections.abc import Mapping
from pathlib import Path

from argus.app.observability.health import disk_space
from argus.kernel.clock import Clock
from argus.kernel.events import DiskSpaceLow, EventBus

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 3600


class DiskWatch:
    def __init__(
        self, disk_dirs: Mapping[str, Path], threshold_gb: float, clock: Clock, bus: EventBus
    ) -> None:
        self._disk_dirs = disk_dirs
        self._threshold_gb = threshold_gb
        self._clock = clock
        self._bus = bus

    async def run(self) -> None:
        while True:
            await self.check_once()
            await self._clock.sleep(_CHECK_INTERVAL_SECONDS)

    async def check_once(self) -> None:
        for disk_name, directory in self._disk_dirs.items():
            space = await disk_space(directory)
            if space is None or space.free_gb >= self._threshold_gb:
                continue
            path = directory.as_posix()
            logger.warning(
                "%s 所在盘只剩 %s GB，低于 %g GB", path, space.free_gb, self._threshold_gb
            )
            self._bus.publish(
                DiskSpaceLow(
                    disk_name=disk_name,
                    path=path,
                    free_gb=space.free_gb,
                    threshold_gb=self._threshold_gb,
                    ts=self._clock.now(),
                )
            )
