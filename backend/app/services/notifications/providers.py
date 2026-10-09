"""Email and SMS delivery adapters.

Email: console (dev outbox), SMTP (any SMTP server, e.g. Mailpit locally), Resend,
SendGrid. SMS: console (dev outbox), Twilio. Every adapter raises
TransientProviderError for retryable failures and PermanentProviderError otherwise.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path

import httpx

from app.core.config import Settings, get_settings
from app.services.http import (
    PermanentProviderError,
    TransientProviderError,
    request_with_retries,
)

logger = logging.getLogger(__name__)


@dataclass
class EmailMessageData:
    to: str
    subject: str
    text: str
    html: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    tag: str = "alert"


@dataclass
class SendResult:
    provider: str
    message_id: str | None


def _outbox_write(settings: Settings, kind: str, payload: dict[str, object]) -> str:
    message_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    directory = Path(settings.outbox_dir)
    directory.mkdir(parents=True, exist_ok=True)
    record = {"id": message_id, "kind": kind, "at": datetime.now(UTC).isoformat(), **payload}
    with (directory / "outbox.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return message_id


def read_outbox(settings: Settings | None = None, limit: int = 50) -> list[dict[str, object]]:
    settings = settings or get_settings()
    path = Path(settings.outbox_dir) / "outbox.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in reversed(lines) if line.strip()]


# ---------------------------------------------------------------- email
class EmailProvider(ABC):
    name: str

    @abstractmethod
    def send(self, message: EmailMessageData) -> SendResult: ...


class ConsoleEmailProvider(EmailProvider):
    """Development adapter: writes messages to OUTBOX_DIR/outbox.jsonl and the log."""

    name = "console"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, message: EmailMessageData) -> SendResult:
        mid = _outbox_write(
            self.settings,
            "email",
            {
                "to": message.to,
                "subject": message.subject,
                "text": message.text,
                "html": message.html,
                "tag": message.tag,
            },
        )
        logger.info("[console email] %s (%s)", message.subject, mid)
        return SendResult(self.name, mid)


class SmtpEmailProvider(EmailProvider):
    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, message: EmailMessageData) -> SendResult:
        s = self.settings
        msg = EmailMessage()
        name, addr = parseaddr(s.email_from)
        msg["From"] = formataddr((name, addr))
        msg["To"] = message.to
        msg["Subject"] = message.subject
        msg["Message-ID"] = make_msgid(domain=addr.split("@")[-1] or "localhost")
        for k, v in message.headers.items():
            msg[k] = v
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")
        try:
            if s.smtp_use_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    s.smtp_host, s.smtp_port, timeout=20, context=ssl.create_default_context()
                )
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
            with server:
                if s.smtp_use_tls and not s.smtp_use_ssl:
                    server.starttls(context=ssl.create_default_context())
                if s.smtp_username and s.smtp_password:
                    server.login(s.smtp_username, s.smtp_password)
                server.send_message(msg)
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPAuthenticationError,
        ) as exc:
            raise PermanentProviderError(f"SMTP rejected message: {exc}") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise TransientProviderError(f"SMTP unavailable: {exc}") from exc
        return SendResult(self.name, str(msg["Message-ID"]))


class _HttpEmailProvider(EmailProvider):
    def __init__(
        self, settings: Settings, client: httpx.Client | None = None, backoff_seconds: float = 1.0
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)
        self.backoff_seconds = backoff_seconds

    def _check(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise PermanentProviderError(
                f"{self.name} authentication failed (HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise PermanentProviderError(
                f"{self.name} rejected message: HTTP {resp.status_code} {resp.text[:200]}"
            )


class ResendEmailProvider(_HttpEmailProvider):
    name = "resend"

    def send(self, message: EmailMessageData) -> SendResult:
        if not self.settings.resend_api_key:
            raise PermanentProviderError("RESEND_API_KEY is not configured")
        body: dict[str, object] = {
            "from": self.settings.email_from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "tags": [{"name": "category", "value": message.tag}],
        }
        if message.html:
            body["html"] = message.html
        if message.headers:
            body["headers"] = message.headers
        resp = request_with_retries(
            self.client,
            "POST",
            "https://api.resend.com/emails",
            provider="resend",
            backoff_seconds=self.backoff_seconds,
            headers={"Authorization": f"Bearer {self.settings.resend_api_key}"},
            json=body,
        )
        self._check(resp)
        return SendResult(self.name, resp.json().get("id"))


class SendGridEmailProvider(_HttpEmailProvider):
    name = "sendgrid"

    def send(self, message: EmailMessageData) -> SendResult:
        if not self.settings.sendgrid_api_key:
            raise PermanentProviderError("SENDGRID_API_KEY is not configured")
        name, addr = parseaddr(self.settings.email_from)
        content = [{"type": "text/plain", "value": message.text}]
        if message.html:
            content.append({"type": "text/html", "value": message.html})
        body: dict[str, object] = {
            "personalizations": [{"to": [{"email": message.to}]}],
            "from": {"email": addr, "name": name} if name else {"email": addr},
            "subject": message.subject,
            "content": content,
            "categories": [message.tag],
        }
        if message.headers:
            body["headers"] = message.headers
        resp = request_with_retries(
            self.client,
            "POST",
            "https://api.sendgrid.com/v3/mail/send",
            provider="sendgrid",
            backoff_seconds=self.backoff_seconds,
            headers={"Authorization": f"Bearer {self.settings.sendgrid_api_key}"},
            json=body,
        )
        self._check(resp)
        return SendResult(self.name, resp.headers.get("X-Message-Id"))


# ---------------------------------------------------------------- sms
class SmsProvider(ABC):
    name: str

    @abstractmethod
    def send(self, to: str, body: str) -> SendResult: ...


class SmsUnavailable(PermanentProviderError):
    """SMS delivery is disabled or not configured."""


class ConsoleSmsProvider(SmsProvider):
    name = "console"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, body: str) -> SendResult:
        mid = _outbox_write(self.settings, "sms", {"to": to, "text": body})
        logger.info("[console sms] %s", mid)
        return SendResult(self.name, mid)


class DisabledSmsProvider(SmsProvider):
    name = "disabled"

    def send(self, to: str, body: str) -> SendResult:
        raise SmsUnavailable("SMS delivery is disabled (SMS_PROVIDER=disabled)")


class TwilioSmsProvider(SmsProvider):
    name = "twilio"
    # Twilio error codes that will never succeed on retry.
    PERMANENT_CODES = {21211, 21408, 21610, 21612, 21614}
    OPTED_OUT_CODE = 21610

    def __init__(
        self, settings: Settings, client: httpx.Client | None = None, backoff_seconds: float = 1.0
    ) -> None:
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            raise SmsUnavailable("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not configured")
        if not (settings.twilio_from_number or settings.twilio_messaging_service_sid):
            raise SmsUnavailable("TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID required")
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)
        self.backoff_seconds = backoff_seconds

    def send(self, to: str, body: str) -> SendResult:
        s = self.settings
        data = {"To": to, "Body": body}
        if s.twilio_messaging_service_sid:
            data["MessagingServiceSid"] = s.twilio_messaging_service_sid
        else:
            data["From"] = s.twilio_from_number or ""
        resp = request_with_retries(
            self.client,
            "POST",
            f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
            provider="twilio",
            backoff_seconds=self.backoff_seconds,
            auth=(s.twilio_account_sid or "", s.twilio_auth_token or ""),
            data=data,
        )
        if resp.status_code in (200, 201):
            return SendResult(self.name, resp.json().get("sid"))
        try:
            payload = resp.json()
            code = int(payload.get("code") or 0)
            message = payload.get("message", "")
        except ValueError:
            code, message = 0, resp.text[:200]
        if code == self.OPTED_OUT_CODE:
            raise RecipientOptedOut(f"Twilio: recipient has opted out ({message})")
        raise PermanentProviderError(
            f"Twilio rejected SMS: HTTP {resp.status_code} code {code} {message}"
        )


class RecipientOptedOut(PermanentProviderError):
    """The carrier/provider reports that the recipient replied STOP."""


def build_email_provider(settings: Settings | None = None) -> EmailProvider:
    settings = settings or get_settings()
    return {
        "console": ConsoleEmailProvider,
        "smtp": SmtpEmailProvider,
        "resend": ResendEmailProvider,
        "sendgrid": SendGridEmailProvider,
    }[settings.email_provider](settings)


def build_sms_provider(settings: Settings | None = None) -> SmsProvider:
    settings = settings or get_settings()
    if settings.sms_provider == "twilio":
        return TwilioSmsProvider(settings)
    if settings.sms_provider == "console":
        return ConsoleSmsProvider(settings)
    return DisabledSmsProvider()
