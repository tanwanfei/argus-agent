import logging
import time
from collections.abc import Callable


class ConnectionLog:
    def __init__(
        self,
        logger: logging.Logger,
        what: str,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._what = what + " " if what[-1].isascii() else what
        self._monotonic = monotonic
        self._down_since: float | None = None
        self._failures = 0
        self._reasons_logged: set[str] = set()

    def connected(self) -> None:
        if self._down_since is None:
            self._logger.info("%s连上了", self._what)
            return
        down_for = _duration(self._monotonic() - self._down_since)
        self._logger.info(
            "%s又连上了：断了 %s，中间试了 %d 次没连上", self._what, down_for, self._failures
        )
        self._down_since = None
        self._failures = 0
        self._reasons_logged.clear()

    def dropped(self, retry_delay: float) -> None:
        self._logger.info("%s的连接断了，%g 秒后重连", self._what, retry_delay)

    def failed(self, error: Exception, retry_delay: float) -> None:
        if self._down_since is None:
            self._down_since = self._monotonic()
        self._failures += 1
        reason = str(error)
        level = logging.DEBUG if reason in self._reasons_logged else logging.WARNING
        self._reasons_logged.add(reason)
        self._logger.log(level, "%s连不上，%g 秒后再试：%s", self._what, retry_delay, reason)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)} 秒"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"
