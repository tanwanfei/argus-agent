import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from argus.infra.sqlite.vector_extension import load_vector_extension


class SqliteDatabase:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._writer_thread = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"sqlite-writer-{db_path.stem}"
        )
        self._writer_connection = self._writer_thread.submit(self._open_writer).result()

    def _open_writer(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        load_vector_extension(connection)
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    async def write[ResultT](self, work: Callable[[sqlite3.Connection], ResultT]) -> ResultT:
        future = self._writer_thread.submit(self._run_in_transaction, work)
        return await asyncio.shield(asyncio.wrap_future(future))

    def _run_in_transaction[ResultT](
        self, work: Callable[[sqlite3.Connection], ResultT]
    ) -> ResultT:
        connection = self._writer_connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = work(connection)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return result

    async def read[ResultT](self, work: Callable[[sqlite3.Connection], ResultT]) -> ResultT:
        return await asyncio.to_thread(self._read_on_new_connection, work)

    def _read_on_new_connection[ResultT](
        self, work: Callable[[sqlite3.Connection], ResultT]
    ) -> ResultT:
        with closing(sqlite3.connect(self._db_path, isolation_level=None)) as connection:
            load_vector_extension(connection)
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            return work(connection)

    async def close(self) -> None:
        connection_closed = self._writer_thread.submit(self._writer_connection.close)
        self._writer_thread.shutdown(wait=False)
        await asyncio.wrap_future(connection_closed)
