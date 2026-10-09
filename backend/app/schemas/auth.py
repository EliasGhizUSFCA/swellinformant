"""Authentication and account schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.enums import BreakType, SkillLevel
from app.schemas.common import CurrencyCode, IataCode, ORMModel, PlainText


class _PasswordPair(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _match(self) -> _PasswordPair:
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self


class RegisterIn(_PasswordPair):
    full_name: PlainText = Field(min_length=1, max_length=120)
    email: EmailStr


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(_PasswordPair):
    token: str = Field(min_length=10, max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _match(self) -> ChangePasswordIn:
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    confirm: Literal["DELETE"]


class ProfileIn(BaseModel):
    full_name: PlainText | None = Field(default=None, min_length=1, max_length=120)
    home_airport: IataCode | None = None
    home_city: PlainText | None = Field(default=None, max_length=120)
    preferred_wave_min_ft: float | None = Field(default=None, ge=0, le=80)
    preferred_wave_max_ft: float | None = Field(default=None, ge=0, le=80)
    preferred_break_type: BreakType | None = None
    experience_level: SkillLevel | None = None
    units: Literal["ft", "m"] | None = None
    currency: CurrencyCode | None = None

    @model_validator(mode="after")
    def _range(self) -> ProfileIn:
        if (
            self.preferred_wave_min_ft is not None
            and self.preferred_wave_max_ft is not None
            and self.preferred_wave_min_ft > self.preferred_wave_max_ft
        ):
            raise ValueError("Minimum wave height must not exceed the maximum.")
        return self


class ProfileOut(ORMModel):
    home_airport: str | None
    home_city: str | None
    preferred_wave_min_ft: float | None
    preferred_wave_max_ft: float | None
    preferred_break_type: BreakType | None
    experience_level: SkillLevel | None
    units: str
    currency: str


class NotificationPrefsIn(BaseModel):
    email_enabled: bool | None = None
    sms_enabled: bool | None = None
    all_paused: bool | None = None
    max_alerts_per_day: int | None = Field(default=None, ge=1, le=100)


class NotificationPrefsOut(BaseModel):
    email_enabled: bool
    sms_enabled: bool
    all_paused: bool
    phone_number: str | None
    phone_verified: bool
    sms_ready: bool
    sms_opted_out: bool
    max_alerts_per_day: int
    sms_service_available: bool


class PhoneStartIn(BaseModel):
    phone_number: str = Field(min_length=7, max_length=24)


class PhoneConfirmIn(BaseModel):
    code: str = Field(min_length=4, max_length=10)


class UserAirportIn(BaseModel):
    airport_iata: IataCode
    label: PlainText | None = Field(default=None, max_length=60)
    is_home: bool = False


class UserAirportOut(BaseModel):
    id: int
    airport_iata: str
    label: str | None
    is_home: bool
    name: str
    city: str
    country: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    email_verified: bool
    is_admin: bool
    created_at: datetime
    profile: ProfileOut
    notification_preferences: NotificationPrefsOut
    airports: list[UserAirportOut]


class AuthOut(BaseModel):
    user: UserOut
    csrf_token: str
