"""Plain-text, HTML and SMS renderings of alerts and account emails.

Every interpolated value is HTML-escaped. Demo forecasts and mock fares are labelled
prominently so test data can never be mistaken for a real opportunity.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.services.surf_quality.forecast_confidence import lead_phrase

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


def fmt_money(amount: str | float | Decimal | None, currency: str | None) -> str:
    if amount is None or currency is None:
        return "—"
    # Whole units rounded half up, as the app shows them (Intl.NumberFormat): 350.50 is $351
    # in both, never $350 in the email (float formatting rounds half to even).
    value = Decimal(str(amount)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}
    symbol = symbols.get(currency, "")
    text = f"{value:,}"
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


def fmt_utc(value: str | None) -> str:
    if not value:
        return "—"
    return f"{datetime.fromisoformat(value).astimezone(ZoneInfo('UTC')):%Y-%m-%d %H:%M} UTC"


def _buffers(s: dict[str, Any]) -> str:
    a, d = s.get("arrival_buffer_days"), s.get("departure_buffer_days")
    if a is None or d is None:
        return "—"
    return f"arrive {a} day(s) before the swell, leave {d} day(s) after"


RANGE_NOTES = {
    "heuristic": "estimated, not yet verified",
    "ensemble_heuristic": "from the ensemble spread, not yet verified",
    "calibrated": "checked against observations",
}


def _height(s: dict[str, Any]) -> str:
    """Typical-to-sets faces plus the likely range. The range is heuristic: it is meant to
    hold the breaking height about 8 times in 10 (an intended 80% range) but has not been
    verified, so it is called a likely range and its intent is stated, not implied as a
    probability (docs/forecast-confidence.md)."""
    text = f"{s['height_min_ft']:g}–{s['height_max_ft']:g} ft faces"
    if s.get("range_p10_ft") is not None and s.get("range_p90_ft") is not None:
        note = RANGE_NOTES.get(str(s.get("range_kind")), "not yet verified")
        text += (
            f" (likely range {s['range_p10_ft']:g}–{s['range_p90_ft']:g} ft: meant as an 80% "
            f"range, {note})"
        )
    return text


def score10(score: int | float) -> str:
    """85 -> "8.5" (the 0-100 score divided by ten, one decimal, as in the app)."""
    return f"{max(0, min(100, round(score))) / 10:.1f}"


def _provenance(s: dict[str, Any]) -> list[tuple[str, str]]:
    """'Model runs' (each model's own init time; a blend's issue time is only its oldest
    member's) and 'Fetched' (when the data arrived). Snapshots built before the model runs
    were recorded show the run's issue time as 'Model run'."""
    runs = [r for r in s.get("model_runs") or [] if r.get("name")]
    if runs:
        rows = [
            (
                "Model runs" if len(runs) > 1 else "Model run",
                "; ".join(f"{r['name']} ({fmt_utc(r.get('init_time'))})" for r in runs),
            )
        ]
    else:
        rows = [
            (
                "Model run",
                fmt_utc(s.get("forecast_issued"))
                + (f" · {s['forecast_source']}" if s.get("forecast_source") else ""),
            )
        ]
    if s.get("forecast_fetched"):
        rows.append(("Fetched", fmt_utc(s["forecast_fetched"])))
    return rows


def _credits(s: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, http(s) URL) credits the data sources ask for next to their data (e.g.
    "Weather data by Open-Meteo.com", "Copyright ECMWF ... (modified)", CC BY 4.0)."""
    out: list[tuple[str, str]] = []
    for c in s.get("data_credits") or []:
        label, href = str(c.get("label") or ""), str(c.get("href") or "")
        if label and href.startswith(("https://", "http://")):
            out.append((label, href))
    return out


CONFIDENCE_WORDS = {"high": "High", "moderate": "Moderate", "low": "Low"}


def _forecast_confidence(s: dict[str, Any]) -> str:
    """'Moderate — about 6 days away' (forecast-v1 class at the event's peak hour, as in
    the API; the distance is from when the alert was built). Older snapshots without the
    class say so and show the 0-100 index under its own name."""
    cls = s.get("forecast_confidence_class")
    if not cls:
        # The 0-100 index is not the forecast-confidence class; name it for what it is.
        return f"Not assessed (offshore spread index {s['confidence']}/100, not a probability)"
    text = CONFIDENCE_WORDS.get(str(cls), str(cls).title())
    ahead = s.get("peak_in_hours")
    lead = lead_phrase(ahead if ahead is not None else s.get("confidence_lead_hours"))
    return f"{text} — {lead}" if lead else text


RESEARCH_NOTE = "how well this break's ideal conditions are documented; separate from the forecast"


def _research_confidence(s: dict[str, Any]) -> str | None:
    """'Moderate (how well this break's ideal conditions are documented; ...)': the spot's
    research class, never merged with the forecast confidence (spec 20)."""
    cls = s.get("research_confidence_class")
    if not cls:
        return None
    return f"{CONFIDENCE_WORDS.get(str(cls), str(cls).title())} ({RESEARCH_NOTE})"


def _swell_dates(s: dict[str, Any]) -> str:
    """'Nov 10–13 (starts in about 4 days, short notice)': the window plus how far ahead
    it is when the alert was built."""
    text = str(s["window_label"])
    lead = s.get("lead_days")
    if lead is None:
        return text
    days = int(float(lead) + 0.5)  # conventional rounding (4.5 -> 5), as lead_phrase does
    if lead < 1:
        ahead = "starts within a day"
    else:
        ahead = f"starts in about {days} day{'s' if days != 1 else ''}"
    return f"{text} ({ahead}{', short notice' if s.get('short_notice') else ''})"


def _airport(s: dict[str, Any], code_key: str, info_key: str) -> str:
    """'DPS — Ngurah Rai International Airport, Denpasar' (the code alone when unknown)."""
    code = s.get(code_key) or "—"
    info = s.get(info_key) or {}
    name, city = info.get("name"), info.get("city")
    if not name:
        return str(code)
    place = f", {city}" if city and city not in name else ""
    return f"{code} — {name}{place}"


def _transfer(s: dict[str, Any]) -> str | None:
    minutes, mode = s.get("transfer_minutes"), s.get("transfer_mode")
    if minutes is None:
        return None
    how = f" by {str(mode).replace('_', ' ').replace('+', ' + ')}" if mode else ""
    hours = round(float(minutes) / 60.0, 1)
    return f"about {hours:g} h{how} from the airport to the break"


def _local(value: str | None) -> str:
    """'Sat, Nov 8 06:15' from a provider's local wall-clock time."""
    if not value:
        return "—"
    moment = datetime.fromisoformat(str(value))
    return f"{moment:%a}, {fmt_date(moment.date())} {moment:%H:%M}"


def _flight_dates(s: dict[str, Any]) -> str | None:
    """The itinerary's own times (local at each airport), when the alert has one."""
    if not s.get("outbound_departure_local"):
        return None
    return (
        f"leave {s.get('origin')} {_local(s.get('outbound_departure_local'))}, land "
        f"{s.get('destination')} {_local(s.get('outbound_arrival_local'))}; fly home "
        f"{_local(s.get('inbound_departure_local'))}, land "
        f"{_local(s.get('inbound_arrival_local'))} (local times)"
    )


def _fare(s: dict[str, Any]) -> str:
    """The per-traveller fare in the search currency; a converted fare says so and names
    the provider's own fare (FX_RATES_USD_JSON rates are configured, never guessed)."""
    text = f"{fmt_money(s['price'], s['currency'])} per traveller"
    if s.get("quoted_currency"):
        text += (
            f" (converted from {fmt_money(s.get('quoted_price'), s['quoted_currency'])} "
            "with a configured exchange rate; the provider charges in "
            f"{s['quoted_currency']})"
        )
    if s.get("is_mock_flight"):
        text += " (MOCK FARE — not a real price)"
    return text


def _rows(s: dict[str, Any]) -> list[tuple[str, str]]:
    swell = (
        f"{s['swell_height_m']} m @ {s['swell_period_s']} s from {s['swell_direction']}"
        if s.get("swell_height_m") is not None
        else "—"
    )
    # Forecast confidence (the offshore forecast) and research confidence (how well the
    # break is documented) are separate rows and are never merged into one number.
    research = _research_confidence(s)
    confidence = [("Forecast confidence", _forecast_confidence(s))]
    if research:
        confidence.append(("Research confidence", research))
    rows = [
        ("Destination", f"{s['spot_name']}, {s['country']}"),
        ("Swell dates", _swell_dates(s)),
        ("Predicted breaking waves (our estimate)", _height(s)),
        ("Main offshore waves (raw model forecast)", swell),
        (
            "Wind",
            f"{s['wind_relation']}, {s.get('wind_speed_kmh', '—')} km/h from {s['wind_direction']}",
        ),
        (
            "Predicted quality",
            f"{LABELS.get(s['peak_label'], s['peak_label'])} ({score10(s['peak_score'])}/10)",
        ),
        *confidence,
        *_provenance(s),
    ]
    if s.get("price") is not None:
        rows += [
            (
                "Suggested travel dates",
                f"{fmt_date(s.get('arrival_date'))} – {fmt_date(s.get('departure_date'))}",
            ),
            ("Travel buffers", _buffers(s)),
            ("Round-trip flight", _fare(s)),
        ]
        if flights := _flight_dates(s):
            rows.append(("Flight dates", flights))
        rows += [
            ("Departure airport", _airport(s, "origin", "origin_airport")),
            ("Destination airport", _airport(s, "destination", "destination_airport")),
        ]
        if transfer := _transfer(s):
            rows.append(("Ground transfer", transfer))
        rows.append(("Airline", s.get("airline") or "—"))
        if s.get("operating_carriers"):
            rows.append(("Operated by", ", ".join(s["operating_carriers"])))
        rows += [
            (
                "Flight time",
                f"{s.get('outbound_hours', '—')} h out / {s.get('inbound_hours', '—')} h back",
            ),
            ("Price checked", s.get("price_checked_label") or "at search time"),
        ]
    else:
        rows += [
            (
                "Suggested travel dates",
                f"{fmt_date(s.get('arrival_date'))} – {fmt_date(s.get('departure_date'))}",
            ),
            ("Travel buffers", _buffers(s)),
            ("Flights", "No flight within your budget and schedule has been found yet."),
        ]
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
    "Forecasts are model estimates and become less certain the further ahead they look: "
    "size and timing often shift for swells a week or more away, and even a few days out "
    "they can change. Breaking wave heights are approximate. Fares are quotes at search "
    "time and are subject to change until booked."
)


def _link_lines(links: dict[str, str]) -> list[str]:
    """The booking link (the provider's own, when the offer has one), then the route
    searches, labelled as such: their fares may differ from the quote above."""
    lines: list[str] = []
    if links.get("booking"):
        lines.append(f"Book this fare: {links['booking']}")
    note = links.get("flights_note") or "route search — fares may differ"
    if links.get("flights"):
        lines.append(f"Google Flights ({note}): {links['flights']}")
    if links.get("flights_alt"):
        lines.append(f"Skyscanner ({note}): {links['flights_alt']}")
    return lines


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
    lines += _link_lines(links)
    if links.get("dashboard"):
        lines.append(f"Your dashboard: {links['dashboard']}")
    credits = _credits(s)
    if credits:
        lines += [
            "",
            "Forecast data: " + " · ".join(f"{label} ({href})" for label, href in credits),
        ]
    lines += [
        "",
        DISCLAIMER,
        "",
        f"Manage alerts: {links.get('manage', links['unsubscribe'])}",
        f"Unsubscribe from email alerts: {links['unsubscribe']}",
    ]
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
    button = (
        'style="display:inline-block;margin:4px 8px 4px 0;padding:10px 16px;'
        'border-radius:8px;border:1px solid #0e7490;color:#0e7490;text-decoration:none"'
    )
    note = links.get("flights_note") or "route search — fares may differ"
    flights_btn = "".join(
        f'<a href="{escape(links[key])}" {button}>{escape(label)}</a>'
        for key, label in (
            ("booking", "Book this fare"),
            ("flights", f"Google Flights ({note})"),
            ("flights_alt", f"Skyscanner ({note})"),
        )
        if links.get(key)
    )
    credits_html = (
        '<p style="font-size:12px;color:#6b7f8c;margin-top:16px">Forecast data: '
        + " · ".join(
            f'<a href="{escape(href)}" style="color:#6b7f8c">{escape(label)}</a>'
            for label, href in credits
        )
        + "</p>"
        if credits
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
{credits_html}
<p style="font-size:12px;color:#6b7f8c;margin-top:20px">{escape(DISCLAIMER)}</p>
<p style="font-size:12px;color:#6b7f8c"><a href="{escape(links.get("dashboard", links["opportunity"]))}" style="color:#6b7f8c">Dashboard</a> · <a href="{escape(links.get("manage", links["unsubscribe"]))}" style="color:#6b7f8c">Manage alerts</a> · <a href="{escape(links["unsubscribe"])}" style="color:#6b7f8c">Unsubscribe from email alerts</a></p>
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


def render_verification_email(name: str, link: str, ttl_hours: int = 48) -> tuple[str, str, str]:
    app = get_settings().app_name
    subject = f"Verify your email for {app}"
    expiry = f"This link expires in {ttl_hours} hours and works once."
    text = (
        f"Hi {name},\n\nConfirm your email address to start receiving swell alerts:\n{link}\n\n"
        f"{expiry} If you didn't create an account, ignore this email."
    )
    html = (
        f"<p>Hi {escape(name)},</p><p>Confirm your email address to start receiving swell alerts:</p>"
        f'<p><a href="{escape(link)}">Verify my email</a></p>'
        f"<p style='color:#6b7f8c;font-size:12px'>{escape(expiry)} If you didn't "
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


def render_test_email(name: str, provider: str, app_url: str) -> tuple[str, str, str]:
    """Developer test message: proves the provider path works. Contains no real data."""
    subject = f"[TEST] {get_settings().app_name} email delivery check"
    text = (
        f"Hi {name},\n\nThis is a TEST email sent from the developer tools via the "
        f"'{provider}' email provider. It contains no forecast or flight data.\n\n"
        f"If you can read this in your inbox, delivery works. App: {app_url}"
    )
    html = (
        f"<p>Hi {escape(name)},</p><p>This is a <strong>TEST</strong> email sent from the developer "
        f"tools via the <code>{escape(provider)}</code> email provider. It contains no forecast or "
        "flight data.</p><p>If you can read this in your inbox, delivery works.</p>"
    )
    return subject, text, html
