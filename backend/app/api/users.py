"""Profile, notification preferences, phone verification, saved airports, deletion."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.presenters import airport_rows, user_out
from app.auth.deps import AuthContext, get_auth
from app.auth.sessions import clear_session_cookie
from app.core.database import get_db, utcnow
from app.core.errors import AppError, NotFound
from app.core.ratelimit import hit
from app.models import Airport, UserAirport
from app.schemas.auth import (
    DeleteAccountIn,
    NotificationPrefsIn,
    PhoneConfirmIn,
    PhoneStartIn,
    ProfileIn,
    UserAirportIn,
    UserAirportOut,
    UserOut,
)
from app.schemas.common import Message
from app.services import accounts

router = APIRouter(prefix="/api/users/me", tags=["account"])


@router.patch("/profile", response_model=UserOut)
def update_profile(
    body: ProfileIn, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> UserOut:
    user = auth.user
    data = body.model_dump(exclude_unset=True)
    if "full_name" in data:
        user.full_name = data.pop("full_name")
    if data.get("home_airport") and db.get(Airport, data["home_airport"]) is None:
        raise AppError("Unknown airport code.", code="unknown_airport")
    for key, value in data.items():
        setattr(user.profile, key, value)
    profile = user.profile
    if (
        profile.preferred_wave_min_ft is not None
        and profile.preferred_wave_max_ft is not None
        and profile.preferred_wave_min_ft > profile.preferred_wave_max_ft
    ):
        raise AppError("Minimum wave height must not exceed the maximum.", code="invalid_range")
    db.commit()
    return user_out(db, user)


@router.patch("/notification-preferences", response_model=UserOut)
def update_notification_prefs(
    body: NotificationPrefsIn, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> UserOut:
    prefs = auth.user.notification_preferences
    data = body.model_dump(exclude_unset=True)
    if data.get("sms_enabled") and not prefs.sms_ready:
        raise AppError(
            "Verify your phone number before enabling SMS alerts.", code="phone_not_verified"
        )
    for key, value in data.items():
        setattr(prefs, key, value)
    db.commit()
    return user_out(db, auth.user)


@router.post("/phone/start", response_model=Message)
def start_phone(
    body: PhoneStartIn, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> Message:
    hit("phone-start", str(auth.user.id), 5, 3600)
    accounts.start_phone_verification(db, auth.user, body.phone_number)
    return Message(message="We sent a 6-digit code by SMS. Enter it to confirm your number.")


@router.post("/phone/confirm", response_model=UserOut)
def confirm_phone(
    body: PhoneConfirmIn, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> UserOut:
    hit("phone-confirm", str(auth.user.id), 10, 900)
    accounts.confirm_phone(db, auth.user, body.code)
    return user_out(db, auth.user)


@router.post("/sms/opt-out", response_model=UserOut)
def sms_opt_out(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)) -> UserOut:
    prefs = auth.user.notification_preferences
    prefs.sms_enabled = False
    prefs.sms_opted_out_at = utcnow()
    db.commit()
    return user_out(db, auth.user)


@router.get("/airports", response_model=list[UserAirportOut])
def list_airports(
    auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> list[UserAirportOut]:
    return airport_rows(db, auth.user)


@router.post("/airports", response_model=list[UserAirportOut], status_code=status.HTTP_201_CREATED)
def add_airport(
    body: UserAirportIn, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> list[UserAirportOut]:
    if db.get(Airport, body.airport_iata) is None:
        raise AppError("Unknown airport code.", code="unknown_airport")
    user = auth.user
    existing = db.scalar(
        select(UserAirport).where(
            UserAirport.user_id == user.id, UserAirport.airport_iata == body.airport_iata
        )
    )
    if len(user.airports) >= 10 and existing is None:
        raise AppError("You can save up to 10 airports.", code="limit_reached")
    if body.is_home:
        db.execute(update(UserAirport).where(UserAirport.user_id == user.id).values(is_home=False))
        user.profile.home_airport = body.airport_iata
    if existing is None:
        db.add(
            UserAirport(
                user_id=user.id,
                airport_iata=body.airport_iata,
                label=body.label,
                is_home=body.is_home,
            )
        )
    else:
        existing.label = body.label
        existing.is_home = body.is_home
    db.commit()
    return airport_rows(db, user)


@router.delete("/airports/{airport_id}", response_model=list[UserAirportOut])
def remove_airport(
    airport_id: int, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
) -> list[UserAirportOut]:
    row = db.get(UserAirport, airport_id)
    if row is None or row.user_id != auth.user.id:
        raise NotFound("Saved airport not found.")
    db.delete(row)
    db.commit()
    return airport_rows(db, auth.user)


@router.delete("", response_model=Message)
def delete_account(
    body: DeleteAccountIn,
    response: Response,
    auth: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
) -> Message:
    hit("delete-account", str(auth.user.id), 5, 900)
    accounts.delete_account(db, auth.user, body.password)
    clear_session_cookie(response)
    return Message(message="Your account and all associated data have been deleted.")
