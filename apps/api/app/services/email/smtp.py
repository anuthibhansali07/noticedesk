"""SMTP email provider — works with Gmail, Mailtrap, SES-SMTP, Postmark, etc.

Uses ``aiosmtplib`` for proper async delivery. STARTTLS by default; set
``SMTP_USE_TLS=false`` only when talking to a local maildev / mailhog dev
server on plaintext.
"""

from __future__ import annotations

from email.message import EmailMessage as MIMEMessage
from typing import Final

from app.services.email.base import (
    EmailError,
    EmailMessage,
    EmailProvider,
    EmailTransientError,
)

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0


class SMTPEmailProvider(EmailProvider):
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        use_tls: bool = True,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not host:
            raise EmailError("SMTP_HOST must be set for the smtp email provider")
        self._host = host
        self._port = port
        self._username = username or None
        self._password = password or None
        self._use_tls = use_tls
        self._timeout = timeout_seconds

    async def send(self, message: EmailMessage) -> str:
        try:
            import aiosmtplib  # type: ignore[import-not-found]
        except ImportError as e:
            raise EmailError(
                "aiosmtplib not installed; install it or switch EMAIL_PROVIDER=stub"
            ) from e

        if not message.to:
            raise EmailError("email message must have at least one recipient")
        if not message.from_address:
            raise EmailError("email message from_address is empty")

        mime = MIMEMessage()
        mime["From"] = (
            f"{message.from_name} <{message.from_address}>"
            if message.from_name
            else message.from_address
        )
        mime["To"] = ", ".join(message.to)
        if message.reply_to:
            mime["Reply-To"] = message.reply_to
        mime["Subject"] = message.subject
        mime.set_content(message.body_text)
        if message.body_html:
            mime.add_alternative(message.body_html, subtype="html")

        try:
            # ``start_tls=True`` issues STARTTLS after connect (port 587);
            # ``use_tls=True`` would mean direct TLS (port 465). Most providers
            # want STARTTLS so we default that on and expose the toggle.
            result = await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                start_tls=self._use_tls,
                timeout=self._timeout,
            )
        except aiosmtplib.SMTPAuthenticationError as e:
            raise EmailError(f"SMTP auth failed: {e}") from e
        except aiosmtplib.SMTPException as e:
            # Connection / temporary delivery issues are retryable.
            raise EmailTransientError(f"SMTP error: {e}") from e
        except (TimeoutError, OSError) as e:
            raise EmailTransientError(f"SMTP transport error: {e}") from e

        # aiosmtplib.send returns (errors_dict, response_str). The response
        # often contains the queued message-id; we surface it best-effort.
        try:
            _, response = result
            return str(response).strip()
        except (TypeError, ValueError):
            return ""
