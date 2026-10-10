"""Inbound provider webhooks.

* Twilio SMS (STOP / START keywords): base64(HMAC-SHA1(auth_token, full_url + sorted(k + v)))
  in ``X-Twilio-Signature``. URL: ``{API_BASE_URL}/api/webhooks/twilio/sms``.
* Resend delivery events (Svix-signed, RESEND_WEBHOOK_SECRET): ``/api/webhooks/resend``.
* SendGrid Event Webhook (ECDSA-signed, SENDGRID_WEBHOOK_PUBLIC_KEY): ``/api/webhooks/sendgrid``.

Unsigned or mis-signed requests are rejected; an endpoint whose secret is not configured
answers 404.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db, utcnow
from app.core.errors import Forbidden, NotFound
from app.models import NotificationPreference
from app.services.notifications.delivery_events import (
    apply_provider_event,
    resend_events,
    sendgrid_events,
    verify_sendgrid_signature,
    verify_svix_signature,
)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT"}
START_WORDS = {"START", "YES", "UNSTOP"}
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


@router.post("/twilio/sms")
async def twilio_sms(request: Request, db: Session = Depends(get_db)) -> Response:
    settings = get_settings()
    if settings.sms_provider != "twilio" or not settings.twilio_auth_token:
        raise NotFound("Not found.")
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    url = f"{settings.api_base_url}{request.url.path}"
    expected = twilio_signature(settings.twilio_auth_token, url, params)
    given = request.headers.get("x-twilio-signature", "")
    if not hmac.compare_digest(expected, given):
        raise Forbidden("Invalid signature.")
    sender = params.get("From", "")
    word = params.get("Body", "").strip().upper()
    opt_type = params.get("OptOutType", "").upper()
    prefs = db.scalar(
        select(NotificationPreference).where(NotificationPreference.phone_number == sender)
    )
    if prefs is not None:
        now = utcnow()
        if word in STOP_WORDS or opt_type == "STOP":
            prefs.sms_opted_out_at = now
            prefs.sms_enabled = False
        elif (word in START_WORDS or opt_type == "START") and prefs.phone_verified_at:
            prefs.sms_opt_in_at = now
            prefs.sms_opted_out_at = None
        db.commit()
    return Response(EMPTY_TWIML, media_type="application/xml")


@router.post("/resend")
async def resend_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, int]:
    secret = get_settings().resend_webhook_secret
    if not secret:
        raise NotFound("Not found.")
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not verify_svix_signature(secret, headers, body, utcnow()):
        raise Forbidden("Invalid signature.")
    try:
        payload: dict[str, Any] = json.loads(body)
    except ValueError as exc:
        raise Forbidden("Invalid payload.") from exc
    matched = 0
    for message_id, event, at, raw in resend_events(payload):
        matched += apply_provider_event(db, "resend", message_id, event, at, raw)
    db.commit()
    return {"matched": matched}


@router.post("/sendgrid")
async def sendgrid_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, int]:
    public_key = get_settings().sendgrid_webhook_public_key
    if not public_key:
        raise NotFound("Not found.")
    body = await request.body()
    signature = request.headers.get("x-twilio-email-event-webhook-signature", "")
    timestamp = request.headers.get("x-twilio-email-event-webhook-timestamp", "")
    if not (signature and timestamp) or not verify_sendgrid_signature(
        public_key, signature, timestamp, body
    ):
        raise Forbidden("Invalid signature.")
    try:
        items = json.loads(body)
    except ValueError as exc:
        raise Forbidden("Invalid payload.") from exc
    matched = 0
    for message_id, event, at, raw in sendgrid_events(items if isinstance(items, list) else []):
        matched += apply_provider_event(db, "sendgrid", message_id, event, at, raw)
    db.commit()
    return {"matched": matched}
