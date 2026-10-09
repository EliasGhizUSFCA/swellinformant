"""Inbound Twilio SMS webhook: STOP / START keyword handling.

Twilio signs each request: base64(HMAC-SHA1(auth_token, full_url + sorted(k + v)))
in the ``X-Twilio-Signature`` header. Unsigned or mis-signed requests are rejected.
Configure the Messaging webhook URL as ``{API_BASE_URL}/api/webhooks/twilio/sms``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db, utcnow
from app.core.errors import Forbidden, NotFound
from app.models import NotificationPreference

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
