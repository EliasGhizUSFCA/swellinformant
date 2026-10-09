"""Shared schema helpers."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

IATA_RE = re.compile(r"^[A-Z]{3}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _iata(v: str) -> str:
    v = v.strip().upper()
    if not IATA_RE.match(v):
        raise ValueError("must be a 3-letter IATA airport code")
    return v


def _currency(v: str) -> str:
    v = v.strip().upper()
    if not CURRENCY_RE.match(v):
        raise ValueError("must be a 3-letter ISO currency code")
    return v


def _plain_text(v: str) -> str:
    v = v.strip()
    if "<" in v or ">" in v:
        raise ValueError("must not contain < or >")
    return v


IataCode = Annotated[str, AfterValidator(_iata)]
CurrencyCode = Annotated[str, AfterValidator(_currency)]
PlainText = Annotated[str, AfterValidator(_plain_text)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    message: str
