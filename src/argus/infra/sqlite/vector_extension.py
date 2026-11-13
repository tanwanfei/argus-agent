import sqlite3
from collections.abc import Sequence

import sqlite_vec


def load_vector_extension(connection: sqlite3.Connection) -> None:
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)


def serialize_vector(vector: Sequence[float]) -> bytes:
    return bytes(sqlite_vec.serialize_float32(list(vector)))
