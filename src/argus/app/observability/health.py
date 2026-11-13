import asyncio
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from argus.channels.endpoint import Endpoint

_BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class DiskSpace:
    free_gb: float
    total_gb: float


async def disk_space(directory: Path) -> DiskSpace | None:
    try:
        usage = await asyncio.to_thread(shutil.disk_usage, directory)
    except OSError:
        return None
    return DiskSpace(
        free_gb=round(usage.free / _BYTES_PER_GB, 1),
        total_gb=round(usage.total / _BYTES_PER_GB, 1),
    )


def healthz(
    endpoints: Sequence[Endpoint],
    disk_dirs: Mapping[str, Path],
    home_assistant: Callable[[], Mapping[str, bool | str | None]] | None = None,
) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def report(request: Request) -> JSONResponse:
        disks = [await _disk_report(name, directory) for name, directory in disk_dirs.items()]
        report: dict[str, Any] = {
            "status": "ok",
            "endpoints": [
                {"id": endpoint.id, "online": endpoint.is_online()} for endpoint in endpoints
            ],
            "disks": disks,
        }
        if home_assistant is not None:
            report["home_assistant"] = dict(home_assistant())
        return JSONResponse(report)

    return report


async def _disk_report(name: str, directory: Path) -> dict[str, str | float | None]:
    space = await disk_space(directory)
    if space is None:
        return {"name": name, "path": directory.as_posix(), "free_gb": None, "total_gb": None}
    return {
        "name": name,
        "path": directory.as_posix(),
        "free_gb": space.free_gb,
        "total_gb": space.total_gb,
    }
