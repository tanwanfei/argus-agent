from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP

from argus.infra.integrations.mail.account import OutgoingMail


def compose(
    draft: OutgoingMail, *, from_address: str, message_id: str, created_at: datetime
) -> EmailMessage:
    message = EmailMessage(policy=SMTP)
    message["From"] = from_address
    message["To"] = ", ".join(draft.to)
    message["Subject"] = draft.subject
    message["Date"] = created_at
    message["Message-ID"] = message_id
    message.set_content(draft.body)
    return message
