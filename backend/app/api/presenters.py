"""Shared ORM → response converters for account data."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Airport, User, UserAirport
from app.schemas.auth import NotificationPrefsOut, ProfileOut, UserAirportOut, UserOut


def airport_rows(db: Session, user: User) -> list[UserAirportOut]:
    rows = db.execute(
        select(UserAirport, Airport)
        .join(Airport, Airport.iata == UserAirport.airport_iata)
        .where(UserAirport.user_id == user.id)
        .order_by(UserAirport.is_home.desc(), UserAirport.created_at)
    ).all()
    return [
        UserAirportOut(
            id=ua.id,
            airport_iata=ua.airport_iata,
            label=ua.label,
            is_home=ua.is_home,
            name=a.name,
            city=a.city,
            country=a.country,
        )
        for ua, a in rows
    ]


def user_out(db: Session, user: User) -> UserOut:
    p = user.notification_preferences
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        email_verified=user.email_verified_at is not None,
        is_admin=user.is_admin,
        created_at=user.created_at,
        profile=ProfileOut.model_validate(user.profile),
        notification_preferences=NotificationPrefsOut(
            email_enabled=p.email_enabled,
            sms_enabled=p.sms_enabled,
            all_paused=p.all_paused,
            phone_number=p.phone_number,
            phone_verified=p.phone_verified_at is not None,
            sms_ready=p.sms_ready,
            sms_opted_out=p.sms_opted_out_at is not None and not p.sms_ready,
            max_alerts_per_day=p.max_alerts_per_day,
            sms_service_available=get_settings().sms_provider != "disabled",
        ),
        airports=airport_rows(db, user),
    )
