import json
import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from argus.infra.endpoints.app_ws.connection import APP_LOGGER_NAME
from argus.kernel.tracing import current_trace_id

_KEEP_DAYS = 30
_DAILY_LOG_NAME = re.compile(r"^[a-z]+-([0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl$")


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": current_trace_id(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            fields["exc"] = self.formatException(record.exc_info)
        return json.dumps(fields, ensure_ascii=False)


class DailyFileHandler(logging.Handler):
    def __init__(self, logs_dir: Path) -> None:
        super().__init__()
        self._logs_dir = logs_dir
        self._open_day: date | None = None
        self._open_file: TextIO | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            record_day = datetime.fromtimestamp(record.created).date()
            log_file = self._file_for(record_day)
            log_file.write(line + "\n")
            log_file.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            self._close_open_file()
        finally:
            self.release()
        super().close()

    def _file_for(self, day: date) -> TextIO:
        if self._open_file is None or day != self._open_day:
            self._close_open_file()
            path = self._logs_dir / f"argus-{day.isoformat()}.jsonl"
            self._open_file = path.open("a", encoding="utf-8", newline="\n")
            self._open_day = day
            _delete_old_logs(self._logs_dir, today=day)
        return self._open_file

    def _close_open_file(self) -> None:
        if self._open_file is not None:
            self._open_file.close()
            self._open_file = None


def _delete_old_logs(logs_dir: Path, today: date) -> None:
    oldest_day_kept = today - timedelta(days=_KEEP_DAYS)
    for path in logs_dir.iterdir():
        name_match = _DAILY_LOG_NAME.match(path.name)
        if name_match is None:
            continue
        try:
            log_day = date.fromisoformat(name_match.group(1))
        except ValueError:
            continue
        if log_day >= oldest_day_kept:
            continue
        try:
            path.unlink()
        except OSError:
            pass


@contextmanager
def log_to_daily_files(logs_dir: Path) -> Generator[None]:
    handler = DailyFileHandler(logs_dir)
    handler.setFormatter(JsonLinesFormatter())
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    http_logger = logging.getLogger("httpx2")
    previous_http_level = http_logger.level
    http_logger.setLevel(logging.WARNING)
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    previous_app_level = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        app_logger.setLevel(previous_app_level)
        http_logger.setLevel(previous_http_level)
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)
        handler.close()
