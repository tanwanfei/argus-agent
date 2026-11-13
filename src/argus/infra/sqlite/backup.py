import asyncio
import logging
import re
import shutil
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from argus.kernel.clock import Clock
from argus.kernel.events import BackupFailed, EventBus

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 3600
_KEEP_BACKUPS = 14
_PARTIAL_SUFFIX = ".partial"
_BACKUP_DIR_NAME = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})(\.partial)?$")


class DailyBackup:
    def __init__(
        self,
        data_dir: Path,
        backup_dir: Path,
        db_names: Sequence[str],
        clock: Clock,
        bus: EventBus,
    ) -> None:
        self._data_dir = data_dir
        self._backup_dir = backup_dir
        self._db_names = db_names
        self._clock = clock
        self._bus = bus

    async def run(self) -> None:
        while True:
            today = self._clock.now().date()
            try:
                await asyncio.to_thread(self.back_up_if_missing, today)
            except (OSError, sqlite3.Error) as error:
                logger.warning("今天的库没备份成功，一小时后再试：%s", error)
                self._bus.publish(BackupFailed(reason=str(error), ts=self._clock.now()))
            await self._clock.sleep(_CHECK_INTERVAL_SECONDS)

    def back_up_if_missing(self, today: date) -> None:
        finished_dir = self._backup_dir / today.isoformat()
        if finished_dir.exists():
            return
        if not self._backup_dir.is_dir():
            raise FileNotFoundError(f"备份目录不存在：{self._backup_dir}（备份盘没接上？）")
        partial_dir = self._backup_dir / (today.isoformat() + _PARTIAL_SUFFIX)
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        partial_dir.mkdir()
        for db_name in self._db_names:
            _back_up_database(self._data_dir / f"{db_name}.db", partial_dir / f"{db_name}.db")
        partial_dir.rename(finished_dir)
        logger.info("库备份好了：%s", finished_dir)
        try:
            _delete_old_backups(self._backup_dir)
        except OSError as error:
            logger.warning("旧备份没删掉，明天备份完再删：%s", error)


def _back_up_database(source_db: Path, target_db: Path) -> None:
    source_uri = f"{source_db.as_uri()}?mode=rw"
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as source,
        closing(sqlite3.connect(target_db)) as target,
    ):
        source.execute("PRAGMA query_only = ON")
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")


def _delete_old_backups(backup_dir: Path) -> None:
    finished_dirs: list[Path] = []
    for path in backup_dir.iterdir():
        name_match = _BACKUP_DIR_NAME.match(path.name)
        if name_match is None or not path.is_dir():
            continue
        try:
            date.fromisoformat(name_match.group(1))
        except ValueError:
            continue
        if name_match.group(2) is not None:
            shutil.rmtree(path)
        else:
            finished_dirs.append(path)
    finished_dirs.sort(key=lambda path: path.name)
    for old_dir in finished_dirs[:-_KEEP_BACKUPS]:
        shutil.rmtree(old_dir)
