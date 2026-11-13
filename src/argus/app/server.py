import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.types import ASGIApp

from argus.app.observability.health import healthz
from argus.channels.endpoint import Endpoint
from argus.infra.endpoints.app_ws.connection import (
    AppSignatureStore,
    EndpointHandler,
    LocationSink,
    PairingSink,
    PushedCards,
    app_connection,
)
from argus.infra.endpoints.app_ws.history import ChatHistoryReader
from argus.kernel.clock import Clock

logger = logging.getLogger(__name__)

LOCALHOST = "127.0.0.1"

_GRACEFUL_SHUTDOWN_S = 3


class ServerSetupError(Exception):
    pass


def build_http_app(
    endpoints: Sequence[Endpoint],
    disk_dirs: Mapping[str, Path],
    *,
    app_token: str | None,
    handle_app_endpoint: EndpointHandler | None = None,
    clock: Clock | None = None,
    app_signature_store: AppSignatureStore | None = None,
    app_location_sink: LocationSink | None = None,
    app_chat_history: ChatHistoryReader | None = None,
    app_pairing: PairingSink | None = None,
    app_pushed_cards: PushedCards | None = None,
    dashboard: Callable[[Request], Awaitable[Response]] | None = None,
    home_assistant_status: Callable[[], Mapping[str, bool | str | None]] | None = None,
    claude_code_hook: Callable[[Request], Awaitable[Response]] | None = None,
) -> Starlette:
    routes: list[BaseRoute] = [
        Route("/healthz", healthz(endpoints, disk_dirs, home_assistant_status), methods=["GET"]),
        WebSocketRoute(
            "/app/ws",
            app_connection(
                app_token,
                handle_endpoint=handle_app_endpoint,
                clock=clock,
                signature_store=app_signature_store,
                location_sink=app_location_sink,
                chat_history=app_chat_history,
                pairing=app_pairing,
                pushed_cards=app_pushed_cards,
            ),
        ),
    ]
    if dashboard is not None:
        routes.append(Route("/dashboard", dashboard, methods=["GET"]))
    if claude_code_hook is not None:
        routes.append(Route("/hooks/claude-code", claude_code_hook, methods=["POST"]))
    return Starlette(routes=routes)


def bind_localhost(port: int) -> socket.socket:
    try:
        return socket.create_server((LOCALHOST, port))
    except OSError as error:
        raise ServerSetupError(
            f"HTTP 端口 {port} 绑不上（argus.yaml 的 server.port），可能已经有一个 ARGUS 在跑\n  {error}"
        ) from error


class HttpServer:
    def __init__(self, app: ASGIApp, listening_socket: socket.socket) -> None:
        config = uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
        )
        self._server = _NoSignalServer(config)
        self._socket = listening_socket

    async def serve(self) -> None:
        port = self._socket.getsockname()[1]
        logger.info("HTTP 服务在 http://%s:%d", LOCALHOST, port)
        serving = asyncio.create_task(self._server.serve(sockets=[self._socket]), name="uvicorn")
        try:
            await asyncio.wait([serving])
        finally:
            self._server.should_exit = True
            await serving
        raise RuntimeError("HTTP 服务没被取消就自己停了")


class _NoSignalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield
