import asyncio
import base64
import binascii
import hmac
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect

from argus.channels.endpoint import ConfirmCard, EndpointOffline
from argus.governance.pairing import PairOutcome, key_fingerprint
from argus.infra.clock import SystemClock
from argus.infra.endpoints.app_ws.endpoint import AppEndpoint
from argus.infra.endpoints.app_ws.history import ChatHistoryReader, history_page
from argus.infra.endpoints.app_ws.messages import (
    PROTOCOL_VERSION,
    BargeIn,
    Bye,
    Confirm,
    GetHistory,
    GetTicket,
    Hello,
    Location,
    Log,
    Pair,
    ProtocolError,
    UpText,
    Welcome,
    decode_upstream,
    encode_downstream,
)
from argus.kernel.clock import Clock
from argus.kernel.geo import GeoFix
from argus.kernel.tools import Confirmation, ConfirmVia

logger = logging.getLogger(__name__)

APP_LOGGER_NAME = "app"
_app_log = logging.getLogger(APP_LOGGER_NAME)
_APP_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

AUTH_FAILED_CLOSE_CODE = 1008

VERSION_MISMATCH_CLOSE_CODE = 4001

EndpointHandler = Callable[[AppEndpoint], Coroutine[Any, Any, None]]


class AppSignatureStore(Protocol):
    async def record(
        self, *, expires_at: datetime, app_version: str, reported_at: datetime
    ) -> None: ...


class LocationSink(Protocol):
    def update_location(self, fix: GeoFix) -> None: ...


CARD_ALREADY_ANSWERED = "这张确认单已经批过了，不用再批。"


class PushedCards(Protocol):
    def pushed_card(self, ticket_id: str) -> ConfirmCard | None: ...

    def card_answered(self, ticket_id: str) -> None: ...

    def was_card_answered(self, ticket_id: str) -> bool: ...


class PairingSink(Protocol):
    def pair(self, public_key_der: bytes) -> PairOutcome: ...


_HELLO_TIMEOUT_S = 10.0


def app_connection(
    expected_token: str | None,
    hello_timeout_s: float = _HELLO_TIMEOUT_S,
    *,
    handle_endpoint: EndpointHandler | None = None,
    clock: Clock | None = None,
    signature_store: AppSignatureStore | None = None,
    location_sink: LocationSink | None = None,
    chat_history: ChatHistoryReader | None = None,
    pairing: PairingSink | None = None,
    pushed_cards: PushedCards | None = None,
) -> Callable[[WebSocket], Awaitable[None]]:
    sinks = _ControlSinks(location_sink, chat_history, pairing, pushed_cards)
    connection_clock = clock if clock is not None else SystemClock()
    expected_token_bytes = None if expected_token is None else expected_token.encode()

    async def serve(websocket: WebSocket) -> None:
        await websocket.accept()
        logger.info("App 连上了")
        if expected_token_bytes is None:
            await _refuse(websocket, "主机没配 APP_TOKEN（.env）")
            return

        try:
            async with asyncio.timeout(hello_timeout_s):
                first_message = await websocket.receive()
        except TimeoutError:
            await _refuse(websocket, f"连上 {hello_timeout_s:g} 秒没发 hello")
            return
        if first_message["type"] == "websocket.disconnect":
            _log_disconnect(first_message)
            return
        try:
            hello = _authenticated_hello(first_message, expected_token_bytes)
        except _NotAuthenticated as refusal:
            await _refuse(websocket, str(refusal))
            return
        logger.info(
            "App 认证通过：App %s，签名 %s 到期",
            hello.app_version,
            hello.sig_expires_at.isoformat(),
        )
        if signature_store is not None:
            await signature_store.record(
                expires_at=hello.sig_expires_at,
                app_version=hello.app_version,
                reported_at=connection_clock.now(),
            )
        if hello.v != PROTOCOL_VERSION:
            await _refuse_protocol_version(websocket, app_protocol_version=hello.v)
            return
        try:
            await websocket.send_text(encode_downstream(Welcome(v=PROTOCOL_VERSION)))
        except WebSocketDisconnect:
            await _receive_until_disconnect(websocket, endpoint=None, sinks=sinks)
            return

        if handle_endpoint is None:
            await _receive_until_disconnect(websocket, endpoint=None, sinks=sinks)
        else:
            await _serve_endpoint(websocket, handle_endpoint, connection_clock, sinks)

    return serve


@dataclass(frozen=True)
class _ControlSinks:
    location: LocationSink | None
    chat_history: ChatHistoryReader | None
    pairing: PairingSink | None
    pushed_cards: PushedCards | None


_NO_SINKS = _ControlSinks(location=None, chat_history=None, pairing=None, pushed_cards=None)


async def _serve_endpoint(
    websocket: WebSocket,
    handle_endpoint: EndpointHandler,
    clock: Clock,
    sinks: _ControlSinks,
) -> None:
    first_message = await _receive_reports_until_endpoint_needed(websocket, sinks)
    if first_message is None:
        return
    endpoint = AppEndpoint(clock)
    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(handle_endpoint(endpoint), name="app-endpoint")
        task_group.create_task(_send_until_closed(websocket, endpoint), name="app-send")
        try:
            await _receive_message(first_message, endpoint, sinks)
            await _receive_until_disconnect(websocket, endpoint, sinks)
        finally:
            endpoint.close()


async def _send_until_closed(websocket: WebSocket, endpoint: AppEndpoint) -> None:
    async for frame in endpoint.outgoing():
        try:
            if isinstance(frame, bytes):
                await websocket.send_bytes(frame)
            else:
                await websocket.send_text(frame)
        except WebSocketDisconnect:
            endpoint.close()
            return


async def _receive_until_disconnect(
    websocket: WebSocket, endpoint: AppEndpoint | None, sinks: _ControlSinks
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            _log_disconnect(message)
            return
        await _receive_message(message, endpoint, sinks)


async def _receive_message(
    message: Message, endpoint: AppEndpoint | None, sinks: _ControlSinks
) -> None:
    packet = message.get("bytes")
    if packet is not None and endpoint is not None:
        endpoint.receive_packet(packet)
    frame_text = message.get("text")
    if frame_text is not None:
        await _receive_control(frame_text, endpoint, sinks)


async def _receive_reports_until_endpoint_needed(
    websocket: WebSocket, sinks: _ControlSinks
) -> Message | None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            _log_disconnect(message)
            return None
        frame_text = message.get("text")
        if frame_text is None or _needs_endpoint(frame_text):
            return message
        await _receive_control(frame_text, None, sinks)


def _needs_endpoint(frame_text: str) -> bool:
    try:
        message = decode_upstream(frame_text)
    except ProtocolError:
        return False
    return not isinstance(message, Log | Location | Pair)


async def _receive_control(
    frame_text: str, endpoint: AppEndpoint | None, sinks: _ControlSinks
) -> None:
    try:
        message = decode_upstream(frame_text)
    except ProtocolError as error:
        logger.warning("App 发来的一条控制消息丢掉了：%s", error)
        return
    if isinstance(message, Log):
        _app_log.log(_APP_LOG_LEVELS[message.level], message.msg)
    elif isinstance(message, Location):
        logger.info(
            "App 报了位置：精度 %.0f 米，%s",
            message.accuracy,
            "在移动" if message.moving else "没在动",
        )
        if sinks.location is not None:
            sinks.location.update_location(
                GeoFix(
                    lat=message.lat,
                    lng=message.lng,
                    accuracy_m=message.accuracy,
                    at=message.ts,
                    moving=message.moving,
                )
            )
    elif isinstance(message, BargeIn) and endpoint is not None:
        endpoint.receive_barge_in(message)
    elif isinstance(message, UpText) and endpoint is not None:
        endpoint.receive_text(message.text)
    elif (
        isinstance(message, GetHistory) and endpoint is not None and sinks.chat_history is not None
    ):
        endpoint.send_history(await history_page(sinks.chat_history, message))
    elif isinstance(message, Pair):
        _receive_pair(message, sinks.pairing)
    elif isinstance(message, Confirm) and endpoint is not None:
        _receive_confirm(message, endpoint)
        if message.approved and sinks.pushed_cards is not None:
            sinks.pushed_cards.card_answered(message.ticket_id)
    elif isinstance(message, GetTicket) and endpoint is not None and sinks.pushed_cards is not None:
        await _send_pushed_card(message.id, endpoint, sinks.pushed_cards)


async def _send_pushed_card(ticket_id: str, endpoint: AppEndpoint, cards: PushedCards) -> None:
    card = cards.pushed_card(ticket_id)
    if card is None and cards.was_card_answered(ticket_id):
        logger.info("App 要的确认单 %s 已经批过了，不再发卡片", ticket_id)
        try:
            await endpoint.send_text(CARD_ALREADY_ANSWERED)
        except EndpointOffline:
            pass
        return
    if card is None:
        logger.info("App 要的确认单 %s 不在了（过期了或没推过），不回", ticket_id)
        return
    try:
        await endpoint.show_card(card)
    except EndpointOffline:
        return
    logger.info("App 从通知点进来，发了确认单 %s 的卡片", ticket_id)


def _receive_confirm(message: Confirm, endpoint: AppEndpoint) -> None:
    if not endpoint.is_online():
        logger.warning("确认单 %s 的答复到了一条已经被顶掉的连接上，没收", message.ticket_id)
        return
    if not message.approved:
        logger.info("你在卡片上拒绝了确认单 %s", message.ticket_id)
        endpoint.receive_confirmation(
            Confirmation(
                message.ticket_id, ConfirmVia.CARD, args_hash=message.args_hash, approved=False
            )
        )
        return
    try:
        sig = None if message.sig is None else base64.b64decode(message.sig, validate=True)
    except binascii.Error:
        logger.warning("确认单 %s 的签名不是合规的 base64，不认", message.ticket_id)
        return
    endpoint.receive_confirmation(
        Confirmation(message.ticket_id, ConfirmVia.CARD, args_hash=message.args_hash, sig=sig)
    )


def _receive_pair(message: Pair, pairing: PairingSink | None) -> None:
    try:
        public_key_der = base64.b64decode(message.pubkey, validate=True)
    except binascii.Error:
        logger.warning("App 发来的公钥不是合规的 base64，不登记")
        return
    fingerprint = key_fingerprint(public_key_der)
    if pairing is None:
        logger.info("App 上报了公钥（指纹 %s），主机没接配对，不登记", fingerprint)
        return
    match pairing.pair(public_key_der):
        case PairOutcome.PAIRED:
            logger.info("登记了 App 的公钥，指纹 %s", fingerprint)
        case PairOutcome.SAME_KEY:
            pass
        case PairOutcome.ALREADY_PAIRED:
            logger.warning(
                "App 发来一把新的公钥（指纹 %s），主机已经登记过一把，不认",
                fingerprint,
            )
        case PairOutcome.NOT_A_P256_KEY:
            logger.warning("App 发来的公钥不是 P-256 的（指纹 %s），不登记", fingerprint)


class _NotAuthenticated(Exception):
    pass


def _authenticated_hello(first_message: Message, expected_token: bytes) -> Hello:
    frame_text = first_message.get("text")
    if frame_text is None:
        raise _NotAuthenticated("第一条是二进制帧，不是 hello")
    try:
        message = decode_upstream(frame_text)
    except ProtocolError as error:
        raise _NotAuthenticated(f"第一条不合协议（{error}）") from None
    if not isinstance(message, Hello):
        raise _NotAuthenticated(f"第一条不是 hello，是 {message.t}")
    if not hmac.compare_digest(message.token.encode(), expected_token):
        raise _NotAuthenticated("token 不对")
    return message


async def _refuse(websocket: WebSocket, reason: str) -> None:
    logger.warning("App 没通过认证，断开了：%s", reason)
    await websocket.close(code=AUTH_FAILED_CLOSE_CODE)


async def _refuse_protocol_version(websocket: WebSocket, app_protocol_version: int) -> None:
    if app_protocol_version < PROTOCOL_VERSION:
        what_to_update = "App 旧了要更新 App"
    else:
        what_to_update = "主机旧了要更新主机"
    logger.warning(
        "App 的协议版本对不上，发 bye 断开了：App 是第 %d 版、主机是第 %d 版，%s",
        app_protocol_version,
        PROTOCOL_VERSION,
        what_to_update,
    )
    try:
        await websocket.send_text(encode_downstream(Bye(reason="upgrade")))
        await websocket.close(code=VERSION_MISMATCH_CLOSE_CODE)
    except WebSocketDisconnect:
        await _receive_until_disconnect(websocket, endpoint=None, sinks=_NO_SINKS)


def _log_disconnect(disconnect_message: Message) -> None:
    logger.info("App 断开了（断开码 %s）", disconnect_message.get("code"))
