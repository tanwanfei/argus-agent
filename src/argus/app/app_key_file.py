from pathlib import Path

APP_PUBLIC_KEY_FILE_NAME = "app-public-key.der"


class FileAppKey:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> bytes | None:
        if not self._path.exists():
            return None
        return self._path.read_bytes()

    def write_new(self, public_key_der: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("xb") as key_file:
            key_file.write(public_key_der)
