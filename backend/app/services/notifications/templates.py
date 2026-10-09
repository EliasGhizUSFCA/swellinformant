"""Plain-text, HTML and SMS renderings of alerts and account emails.

Every interpolated value is HTML-escaped. Demo forecasts and mock fares are labelled
prominently so test data can never be mistaken for a real opportunity.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import get_settings

LABELS = {
    "poor": "Poor",
    "fair": "Fair",
    "good": "Good",
    "very_good": "Very Good",
    "excellent": "Excellent",
    "exceptional": "Exceptional",
}
KIND_PREFIX = {
    "new_opportunity": "Swell Alert",
    "improved": "Swell Upgrade",
    "price_drop": "Price Drop",
    "schedule_change": "Swell Update",
    "surf_only": "Surf Watch",
}


def fmt_date(d: date | str | None) -> str:
    if d is None:
        return "—"
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return f"{d:%b} {d.day}"


def fmt_range(start: str, end: str, tz: str) -> str:
    zone = ZoneInfo(tz)
    a = datetime.fromisoformat(start).astimezone(zone).date()
    b = datetime.fromisoformat(end).astimezone(zone).date()
    if a == b:
        return fmt_date(a)
    if a.month == b.month:
        return f"{a:%b} {a.day}–{b.day}"
    return f"{fmt_date(a)} – {fmt_date(b)}"


def fmt_money(amount: str | float | None, currency: str | None) -> str:
    if amount is None or currency is None:
        return "—"
    value = float(amount)
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}
    symbol = symbols.get(currency, "")
    text = f"{value:,.0f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency}"


def alert_subject(s: dict[str, Any]) -> str:
    prefix = KIND_PREFIX.get(s["kind"], "Swell Alert")
    demo = "[DEMO] " if s.get("is_demo_forecast") or s.get("is_mock_flight") else ""
    label = LABELS.get(s["peak_label"], s["peak_label"])
    if s["kind"] == "price_drop":
        return f"{demo}{prefix} — {s['spot_name']} — now {fmt_money(s['price'], s['currency'])}"
    if s["kind"] == "schedule_change":
        return f"{demo}{prefix} — {s['spot_name']} — new travel dates {fmt_date(s.get('arrival_date'))}–{fmt_date(s.get('departure_date'))}"
    return f"{demo}{prefix} — {s['spot_name']} — {label} Surf Forecast"


def _rows(s: dict[str, Any]) -> list[tuple[str, str]]:
    rows = [
        ("Destination", f"{s['spot_name']}, {s['country']}"),
        (
            "Expected surf",
            f"{s['height_min_ft']:g}–{s['height_max_ft']:g} ft (estimated breaking faces)",
        ),
        ("Swell", f"{s['swell_height_m']} m @ {s['swell_period_s']} s from {s['swell_direction']}"),
        ("Wind", f"{s['wind_relation']}, {s['wind_speed_kmh']} km/h from {s['wind_direction']}"),
        (
            "Predicted quality",
            f"{LABELS.get(s['peak_label'], s['peak_label'])} ({s['peak_score']}/100)",
        ),
        ("Surf window", s["window_label"]),
        ("Forecast confidence", f"{s['confidence_label'].title()} ({s['confidence']}/100)"),
    ]
    if s.get("price") is not None:
        rows += [
            ("Recommended arrival", fmt_date(s.get("arrival_date"))),
            ("Recommended departure", fmt_date(s.get("departure_date"))),
            ("Round-trip flight", f"{fmt_money(s['price'], s['currency'])} per traveller"),
            ("Departure airport", s.get("origin") or "—"),
            ("Destination airport", s.get("destination") or "—"),
            ("Airline", s.get("airline") or "—"),
            (
                "Flight time",
                f"{s.get('outbound_hours', '—')} h out / {s.get('inbound_hours', '—')} h back",
            ),
            ("Price checked", s.get("price_checked_label") or "at search time"),
        ]
    else:
        rows.append(("Flights", "No flight within your budget and schedule has been found yet."))
    return rows


def _intro(s: dict[str, Any]) -> str:
    return {
        "new_opportunity": "A promising surf opportunity matching your search has been detected.",
        "improved": "The forecast for a swell you're tracking has improved.",
        "price_drop": "Flight prices for a swell you're tracking have dropped.",
        "schedule_change": (
            "The forecast timing for a swell you're tracking has shifted, so the recommended "
            "travel dates have changed."
        ),
        "surf_only": (
            "A swell matching your surf preferences is forecast, but no eligible flight "
            "has been found yet."
        ),
    }.get(s["kind"], "Swell update.")


DISCLAIMER = (
    "Forecasts are model estimates and can change significantly, especially more than "
    "5 days ahead; breaking wave heights are approximate. Fares are quotes at search time "
    "and are subject to change until booked."
)


def render_alert_email(s: dict[str, Any], links: dict[str, str]) -> tuple[str, str, str]:
    subject = alert_subject(s)
    demo_note = ""
    if s.get("is_demo_forecast"):
        demo_note += "DEMO DATA: this forecast is synthetic, not a real forecast. "
    if s.get("is_mock_flight"):
        demo_note += "MOCK FARE: this flight offer is generated test data, not a real price."
    lines = [_intro(s), ""]
    if demo_note:
        lines += [demo_note, ""]
    lines += [f"{k}: {v}" for k, v in _rows(s)]
    lines += [
        "",
        f"Opportunity details: {links['opportunity']}",
        f"Detailed surf forecast: {links['forecast']}",
    ]
    if links.get("flights"):
        lines.append(f"Inspect flights: {links['flights']}")
    lines += ["", DISCLAIMER, "", f"Unsubscribe from email alerts: {links['unsubscribe']}"]
    text = "\n".join(lines)

    row_html = "".join(
        f'<tr><td style="padding:6px 12px;color:#4b6475;white-space:nowrap">{escape(k)}</td>'
        f'<td style="padding:6px 12px;color:#0b2233;font-weight:600">{escape(str(v))}</td></tr>'
        for k, v in _rows(s)
    )
    banner = (
        f'<p style="background:#fff4d6;border:1px solid #f0c75e;padding:10px 14px;border-radius:8px;'
        f'color:#7a5300;font-size:13px">{escape(demo_note)}</p>'
        if demo_note
        else ""
    )
    flights_btn = (
        f'<a href="{escape(links["flights"])}" style="display:inline-block;margin:4px 8px 4px 0;'
        f"padding:10px 16px;border-radius:8px;border:1px solid #0e7490;color:#0e7490;"
        f'text-decoration:none">Inspect flights</a>'
        if links.get("flights")
        else ""
    )
    html = f"""<!doctype html><html><body style="margin:0;background:#eef6f8;font-family:Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden">
<tr><td style="background:linear-gradient(135deg,#0b2233,#0e7490);padding:22px 26px;color:#ffffff">
<div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;opacity:.8">Swell Travel Agent</div>
<div style="font-size:22px;font-weight:700;margin-top:6px">{escape(subject)}</div></td></tr>
<tr><td style="padding:22px 26px;color:#0b2233;font-size:15px">
<p>{escape(_intro(s))}</p>{banner}
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px;margin:8px 0 16px">{row_html}</table>
<a href="{escape(links["opportunity"])}" style="display:inline-block;margin:4px 8px 4px 0;padding:10px 16px;border-radius:8px;background:#0e7490;color:#ffffff;text-decoration:none">View opportunity</a>
<a href="{escape(links["forecast"])}" style="display:inline-block;margin:4px 8px 4px 0;padding:10px 16px;border-radius:8px;border:1px solid #0e7490;color:#0e7490;text-decoration:none">Full forecast</a>
{flights_btn}
<p style="font-size:12px;color:#6b7f8c;margin-top:20px">{escape(DISCLAIMER)}</p>
<p style="font-size:12px;color:#6b7f8c"><a href="{escape(links["unsubscribe"])}" style="color:#6b7f8c">Unsubscribe from email alerts</a></p>
</td></tr></table></td></tr></table></body></html>"""
    return subject, text, html


def render_alert_sms(s: dict[str, Any], link: str) -> str:
    demo = "[DEMO] " if s.get("is_demo_forecast") or s.get("is_mock_flight") else ""
    label = LABELS.get(s["peak_label"], s["peak_label"])
    base = (
        f"{demo}{KIND_PREFIX.get(s['kind'], 'Swell Alert')}: {s['spot_name']} "
        f"{s['height_min_ft']:g}-{s['height_max_ft']:g}ft {label}, {s['window_label']}."
    )
    if s.get("price") is not None:
        base += (
            f" {s.get('origin')}-{s.get('destination')} {fmt_money(s['price'], s['currency'])}"
            f" (arr {fmt_date(s.get('arrival_date'))}, dep {fmt_date(s.get('departure_date'))})."
        )
    else:
        base += " No eligible flight yet."
    return f"{base} {link} Reply STOP to opt out."


def render_verification_email(name: str, link: str) -> tuple[str, str, str]:
    app = get_settings().app_name
    subject = f"Verify your email for {app}"
    text = (
        f"Hi {name},\n\nConfirm your email address to start receiving swell alerts:\n{link}\n\n"
        "This link expires in 48 hours. If you didn't create an account, ignore this email."
    )
    html = (
        f"<p>Hi {escape(name)},</p><p>Confirm your email address to start receiving swell alerts:</p>"
        f'<p><a href="{escape(link)}">Verify my email</a></p>'
        "<p style='color:#6b7f8c;font-size:12px'>This link expires in 48 hours. If you didn't "
        "create an account, ignore this email.</p>"
    )
    return subject, text, html


def render_password_reset_email(name: str, link: str, minutes: int) -> tuple[str, str, str]:
    subject = "Reset your Swell Travel Agent password"
    text = (
        f"Hi {name},\n\nUse this link to choose a new password (valid for {minutes} minutes):\n"
        f"{link}\n\nIf you didn't request a reset, you can ignore this email; your password is unchanged."
    )
    html = (
        f"<p>Hi {escape(name)},</p><p>Use this link to choose a new password "
        f'(valid for {minutes} minutes):</p><p><a href="{escape(link)}">Reset password</a></p>'
        "<p style='color:#6b7f8c;font-size:12px'>If you didn't request a reset, you can ignore "
        "this email; your password is unchanged.</p>"
    )
    return subject, text, html
