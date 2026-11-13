import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from argus.infra.sqlite.vector_extension import load_vector_extension

logger = logging.getLogger(__name__)

_MIGRATION_FILE_NAME = re.compile(r"(\d{3})_.+\.sql")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class _Migration:
    version: int
    path: Path


def migrate_all(migrations_root: Path, data_dir: Path) -> None:
    migrations_dirs = sorted(path for path in migrations_root.iterdir() if path.is_dir())
    for migrations_dir in migrations_dirs:
        migrate(data_dir / f"{migrations_dir.name}.db", migrations_dir)


def migrate(db_path: Path, migrations_dir: Path) -> None:
    migrations = _load_migrations(migrations_dir)
    latest_version = len(migrations)
    with closing(sqlite3.connect(db_path, isolation_level=None)) as connection:
        load_vector_extension(connection)
        current_version = _read_user_version(connection)
        if current_version > latest_version:
            raise MigrationError(
                f"库的版本比代码新：{db_path} 是第 {current_version} 版，"
                f"而 {migrations_dir} 里最新的迁移是第 {latest_version} 版"
            )
        for migration in migrations:
            if migration.version > current_version:
                _apply(connection, db_path, migration)


def _load_migrations(migrations_dir: Path) -> list[_Migration]:
    migrations = []
    for path in sorted(migrations_dir.glob("*.sql")):
        matched = _MIGRATION_FILE_NAME.fullmatch(path.name)
        if matched is None:
            raise MigrationError(f"迁移文件名不合规范，应为 NNN_描述.sql：{path}")
        migrations.append(_Migration(version=int(matched.group(1)), path=path))

    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        version_list = "、".join(f"{version:03d}" for version in versions)
        raise MigrationError(
            f"迁移编号要从 001 开始连续、不重复：{migrations_dir}\n  现有编号：{version_list}"
        )
    return migrations


def _read_user_version(connection: sqlite3.Connection) -> int:
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    return int(version)


def _apply(connection: sqlite3.Connection, db_path: Path, migration: _Migration) -> None:
    sql = migration.path.read_text(encoding="utf-8-sig")
    script = f"BEGIN;\n{sql}\n;\nPRAGMA user_version = {migration.version};\nCOMMIT;"
    try:
        connection.executescript(script)
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise MigrationError(f"迁移失败：{db_path}\n  {migration.path.name}: {error}") from error
    logger.info(
        "迁移 %s：执行 %s，到第 %d 版", db_path.name, migration.path.name, migration.version
    )
