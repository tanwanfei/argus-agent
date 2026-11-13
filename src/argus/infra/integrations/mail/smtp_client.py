import asyncio
import logging
import smtplib
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage

from argus.infra.integrations.mail.account import OutgoingMail
from argus.infra.integrations.mail.compose import compose
from argus.kernel.tools import ToolFailure

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30
_SMTPS_PORT = 465


class SmtpMailSender:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        address: str | None = None,
        connect: Callable[[], smtplib.SMTP] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._address = address or username
        self._connect = connect or (lambda: _connect_with_tls(host, port))

    async def send(self, mail: OutgoingMail, *, message_id: str, created_at: datetime) -> None:
        await asyncio.to_thread(self._send, mail, message_id, created_at)

    def _send(self, mail: OutgoingMail, message_id: str, created_at: datetime) -> None:
        if "@" not in self._address:
            raise ToolFailure(
                "不知道发件地址：登录用户名不是邮箱地址",
                user_action="在 argus.yaml 的 mail.address 里写发件的邮箱地址",
            )
        message = compose(
            mail, from_address=self._address, message_id=message_id, created_at=created_at
        )
        smtp = self._log_in()
        try:
            refused = self._deliver(smtp, message)
        finally:
            _quit(smtp)
        if refused:
            raise ToolFailure(
                f"信发出去了，但服务器拒绝了其中 {len(refused)} 个收件人：{'、'.join(refused)}",
                user_action="检查这几个地址是不是写对了",
            )

    def _log_in(self) -> smtplib.SMTP:
        try:
            smtp = self._connect()
        except TimeoutError as error:
            raise _not_sent(
                "邮箱服务器没有及时回复", "过一会儿再试；总是这样的话检查网络"
            ) from error
        except (OSError, smtplib.SMTPException) as error:
            logger.warning("连不上发信服务器：%s", type(error).__name__)
            raise _not_sent(
                "连不上发信服务器", "检查网络，以及 argus.yaml 里的 mail.smtp_host"
            ) from error
        try:
            smtp.login(self._username, self._password)
        except smtplib.SMTPAuthenticationError as error:
            logger.warning("发信登录被拒：%s", type(error).__name__)
            _quit(smtp)
            raise _not_sent(
                "邮箱不认用户名或授权码",
                "检查 .env 里的 MAIL_USERNAME、MAIL_PASSWORD，也要确认邮箱设置里开了 SMTP 发信",
            ) from error
        except (OSError, smtplib.SMTPException) as error:
            logger.warning("发信登录出错：%s", type(error).__name__)
            _quit(smtp)
            raise _not_sent("发信服务器登录时出错", "过一会儿再试") from error
        return smtp

    def _deliver(self, smtp: smtplib.SMTP, message: EmailMessage) -> list[str]:
        try:
            refused = smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as error:
            logger.warning("发信：收件人全被拒")
            raise _not_sent(
                f"服务器拒绝了所有收件人：{'、'.join(error.recipients)}", "检查地址是不是写对了"
            ) from error
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
            logger.warning("发信被服务器拒绝：%s %s", type(error).__name__, error.smtp_code)
            raise _not_sent(
                f"服务器没有收这封信，状态码 {error.smtp_code}",
                "确认发件地址和登录账号一致；总是这样的话在邮箱网页版里试试能不能发",
            ) from error
        except (TimeoutError, smtplib.SMTPServerDisconnected, OSError) as error:
            logger.warning("发信时连接出了问题：%s", type(error).__name__)
            raise ToolFailure(
                "发信时连接断了，不确定信有没有发出去",
                user_action="先到邮箱的已发送里看有没有，没有再重发，免得发两遍",
            ) from error
        except smtplib.SMTPException as error:
            logger.warning("发信出错：%s", type(error).__name__)
            raise _not_sent("发信服务器出错", "过一会儿再试") from error
        return sorted(refused)


def _connect_with_tls(host: str, port: int) -> smtplib.SMTP:
    if port == _SMTPS_PORT:
        return smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT_S)
    smtp = smtplib.SMTP(host, port, timeout=_TIMEOUT_S)
    smtp.starttls()
    return smtp


def _not_sent(reason: str, user_action: str) -> ToolFailure:
    return ToolFailure(f"{reason}，信没有发出去", user_action=user_action)


def _quit(smtp: smtplib.SMTP) -> None:
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        pass
