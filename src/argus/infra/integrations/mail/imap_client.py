import asyncio
import base64
import email
import email.policy
import imaplib
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from importlib.metadata import version

from argus.infra.integrations.mail.account import MailMessage, MailPage, MailSummary, OutgoingMail
from argus.infra.integrations.mail.compose import compose
from argus.kernel.tools import ToolFailure

logger = logging.getLogger(__name__)

_TIMEOUT_S = 20
_MOST_BODY_CHARS = 3000
_INBOX = "INBOX"

_UID_IN_REPLY = re.compile(rb"UID (\d+)")
_FLAGS_IN_REPLY = re.compile(rb"FLAGS \(([^)]*)\)")
_SEEN_FLAG = b"\\Seen"

_BLOCK_TAGS = frozenset({"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"})
_SKIPPED_TAGS = frozenset({"script", "style", "head"})


class ImapMailAccount:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        address: str | None = None,
        drafts_folder: str | None = None,
        connect: Callable[[], imaplib.IMAP4] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._address = address or username
        self._drafts_folder = drafts_folder
        self._connect = connect or (lambda: imaplib.IMAP4_SSL(host, port, timeout=_TIMEOUT_S))

    async def recent(self, *, unread_only: bool, limit: int) -> MailPage:
        return await asyncio.to_thread(self._recent, unread_only, limit)

    async def read(self, uid: int) -> MailMessage:
        return await asyncio.to_thread(self._read, uid)

    async def save_draft(
        self, draft: OutgoingMail, *, message_id: str, created_at: datetime
    ) -> None:
        await asyncio.to_thread(self._save_draft, draft, message_id, created_at)

    async def delete_draft(self, message_id: str) -> bool:
        return await asyncio.to_thread(self._delete_draft, message_id)

    def _save_draft(self, draft: OutgoingMail, message_id: str, created_at: datetime) -> None:
        if "@" not in self._address:
            raise ToolFailure(
                "不知道发件地址：登录用户名不是邮箱地址",
                user_action="在 argus.yaml 的 mail.address 里写发件的邮箱地址",
            )
        message = compose(
            draft, from_address=self._address, message_id=message_id, created_at=created_at
        )
        with self._session(folder=None) as session:
            folder = self._find_drafts_folder(session)
            typ, _ = session.append(
                _quote(folder),
                "(\\Draft \\Seen)",
                imaplib.Time2Internaldate(created_at),
                message.as_bytes(),
            )
            _require_ok(typ, "存草稿")

    def _delete_draft(self, message_id: str) -> bool:
        with self._session(folder=None) as session:
            folder = self._find_drafts_folder(session)
            typ, _ = session.select(_quote(folder), readonly=False)
            _require_ok(typ, "打开草稿箱")
            typ, found = session.uid("SEARCH", "HEADER", "Message-ID", _quote(message_id))
            _require_ok(typ, "找草稿")
            uids = (found[0] or b"").split()
            for uid in uids:
                typ, _ = session.uid("STORE", uid.decode(), "+FLAGS", "(\\Deleted)")
                _require_ok(typ, "删草稿")
                if "UIDPLUS" in session.capabilities:
                    typ, _ = session.uid("EXPUNGE", uid.decode())
                else:
                    typ, _ = session.expunge()
                _require_ok(typ, "删草稿")
            return bool(uids)

    def _find_drafts_folder(self, session: imaplib.IMAP4) -> str:
        if self._drafts_folder is not None:
            return _wire_folder_name(self._drafts_folder)
        typ, listing = session.list()
        _require_ok(typ, "找草稿箱")
        folders = [parsed for line in listing if (parsed := _parse_list_line(line)) is not None]
        for attributes, name in folders:
            if b"\\drafts" in attributes.lower().split():
                return name
        if any(name == "Drafts" for _, name in folders):
            return "Drafts"
        raise ToolFailure(
            "邮箱里找不到草稿箱",
            user_action="在 argus.yaml 的 mail.drafts_folder 里写草稿箱在邮箱里的名字",
        )

    def _recent(self, unread_only: bool, limit: int) -> MailPage:
        with self._session() as session:
            typ, found = session.uid("SEARCH", "UNSEEN" if unread_only else "ALL")
            _require_ok(typ, "查邮件")
            uids = sorted(int(uid) for uid in (found[0] or b"").split())
            newest_first = uids[::-1][:limit]
            summaries = self._fetch_summaries(session, newest_first)
            return MailPage(sorted(summaries, key=lambda s: s.uid, reverse=True), len(uids))

    def _read(self, uid: int) -> MailMessage:
        with self._session() as session:
            typ, data = session.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
            _require_ok(typ, "读邮件")
            records = _fetch_records(data)
            if not records:
                raise ToolFailure(f"邮箱里没有编号 {uid} 的邮件", user_action=None)
            reply, raw_message = records[0]
            message = email.message_from_bytes(raw_message, policy=email.policy.default)
            assert isinstance(message, EmailMessage)
            body, truncated = _readable_body(message)
            return MailMessage(
                summary=_summary_of(uid, reply, message),
                recipients=str(message["To"] or ""),
                body=body,
                body_truncated=truncated,
                attachments=tuple(
                    part.get_filename() or "（没有文件名）" for part in message.iter_attachments()
                ),
            )

    def _fetch_summaries(self, session: imaplib.IMAP4, uids: list[int]) -> list[MailSummary]:
        if not uids:
            return []
        typ, data = session.uid(
            "FETCH",
            ",".join(str(uid) for uid in uids),
            "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
        )
        _require_ok(typ, "查邮件")
        summaries = []
        for reply, header_bytes in _fetch_records(data):
            found = _UID_IN_REPLY.search(reply)
            assert found is not None, "UID FETCH 的回答里一定带 UID（RFC 3501 §6.4.8）"
            headers = email.message_from_bytes(header_bytes, policy=email.policy.default)
            summaries.append(_summary_of(int(found.group(1)), reply, headers))
        return summaries

    @contextmanager
    def _session(self, folder: str | None = _INBOX) -> Iterator[imaplib.IMAP4]:
        try:
            connection = self._connect()
        except TimeoutError as error:
            raise _no_reply() from error
        except OSError as error:
            logger.warning("连不上邮箱服务器：%s", type(error).__name__)
            raise ToolFailure(
                "连不上邮箱服务器", user_action="检查网络，以及 argus.yaml 里的 mail.imap_host"
            ) from error
        try:
            try:
                connection.login(self._username, self._password)
            except imaplib.IMAP4.error as error:
                logger.warning("邮箱登录被拒：%s", type(error).__name__)
                raise ToolFailure(
                    "邮箱不认用户名或授权码",
                    user_action="检查 .env 里的 MAIL_USERNAME、MAIL_PASSWORD：授权码可能抄错了或被关掉了，"
                    "也要确认邮箱设置里开了 IMAP",
                ) from error
            _announce_client(connection)
            if folder is not None:
                typ, _ = connection.select(_quote(folder), readonly=True)
                _require_ok(typ, "打开收件箱")
            yield connection
        except TimeoutError as error:
            raise _no_reply() from error
        except (imaplib.IMAP4.error, OSError) as error:
            logger.warning("邮箱服务器出错：%s", type(error).__name__)
            raise ToolFailure("邮箱服务器出错", user_action="过一会儿再试") from error
        finally:
            _log_out(connection)


def _announce_client(connection: imaplib.IMAP4) -> None:
    if "ID" not in connection.capabilities:
        return
    connection.xatom("ID", f'("name" "ARGUS" "version" "{version("argus")}" "vendor" "ARGUS")')


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_LINE = re.compile(
    rb'\((?P<attributes>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)\s*$'
)


def _parse_list_line(line: object) -> tuple[bytes, str] | None:
    if not isinstance(line, bytes):
        return None
    found = _LIST_LINE.search(line)
    if found is None:
        return None
    name = found.group("name").decode()
    if name.startswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    return found.group("attributes"), name


def _wire_folder_name(configured: str) -> str:
    if configured.isascii():
        return configured
    pieces: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            utf16 = "".join(pending).encode("utf-16-be")
            pieces.append(
                "&" + base64.b64encode(utf16).decode().rstrip("=").replace("/", ",") + "-"
            )
            pending.clear()

    for char in configured:
        if " " <= char <= "~":
            flush()
            pieces.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(pieces)


def _no_reply() -> ToolFailure:
    return ToolFailure("邮箱服务器没有及时回复", user_action="过一会儿再试；总是这样的话检查网络")


def _require_ok(typ: str, doing: str) -> None:
    if typ != "OK":
        raise ToolFailure(f"{doing}时邮箱服务器不答应", user_action="过一会儿再试")


def _log_out(connection: imaplib.IMAP4) -> None:
    try:
        connection.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


def _fetch_records(data: list[object]) -> list[tuple[bytes, bytes]]:
    records = []
    for index, item in enumerate(data):
        if not isinstance(item, tuple):
            continue
        reply, content = item
        following = data[index + 1] if index + 1 < len(data) else b""
        tail = following if isinstance(following, bytes) else b""
        records.append((reply + tail, content))
    return records


def _summary_of(uid: int, reply: bytes, headers: email.message.Message) -> MailSummary:
    flags = _FLAGS_IN_REPLY.search(reply)
    seen = flags is not None and _SEEN_FLAG in flags.group(1).split()
    return MailSummary(
        uid=uid,
        sender=str(headers["From"] or "（没有发件人）"),
        subject=str(headers["Subject"] or "（没有主题）"),
        sent_at=_parse_date(str(headers["Date"] or "")),
        unread=not seen,
    )


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _readable_body(message: EmailMessage) -> tuple[str, bool]:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", False
    text = str(part.get_content())
    if part.get_content_type() == "text/html":
        text = _html_to_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:_MOST_BODY_CHARS], len(text) > _MOST_BODY_CHARS


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pieces: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping += 1
        elif tag in _BLOCK_TAGS:
            self.pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.pieces.append(data)


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return "".join(extractor.pieces)
