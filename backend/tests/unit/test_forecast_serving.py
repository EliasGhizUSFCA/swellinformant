"""The pure parts of the version-aware serving (design D11): which ``?v=`` a response may
be shared under, the stored version marker's validation, and the seen report's body."""

from __future__ import annotations

import json

import pytest

from app.api.forecasts import seen_version_id
from app.services.forecasts import serving, versions
from app.services.public_cache import NO_VERSION, Published, Snapshot, serves_version


def snap(version: str | None) -> Snapshot:
    return Snapshot(b"[]", version=version)


def test_an_unversioned_request_keeps_the_shared_windows() -> None:
    assert serves_version(None, snap("7"))
    assert serves_version(None, snap(None))  # Redis down: the windows of 18a5bee
    assert serves_version(None, None)


@pytest.mark.parametrize("asked", ["7", " 7 "])
def test_the_version_served_may_be_shared(asked: str) -> None:
    assert serves_version(asked, snap("7"))


@pytest.mark.parametrize("asked", ["6", "8", "07", "", "null", "abc", "7;"])
def test_another_version_is_never_shared(asked: str) -> None:
    assert not serves_version(asked, snap("7"))


def test_an_unknown_served_version_is_never_shared_under_a_versioned_url() -> None:
    assert not serves_version("7", snap(None))
    assert not serves_version("7", None)


@pytest.mark.parametrize("asked", ["", "0", "null", "None"])
def test_before_any_version_the_legacy_values_match(asked: str) -> None:
    assert serves_version(asked, snap(NO_VERSION))


def test_before_any_version_a_version_id_does_not_match() -> None:
    assert not serves_version("1", snap(NO_VERSION))


def test_the_published_tag() -> None:
    assert Published("m", 12).tag == "12"
    assert Published("m", None).tag == NO_VERSION == "0"


def test_a_stored_marker_is_completed_with_the_c4_fields() -> None:
    payload = serving.normalised({"version_id": 3, "map_run_id": None, "provider": "demo"})
    assert payload is not None
    assert set(payload) == set(versions.version_payload(None))
    assert payload["version_id"] == 3 and payload["provider"] == "demo"
    assert payload["attribution"] == [] and payload["spots_total"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "x",
        {},
        {"provider": "demo"},
        {"version_id": "3"},
        {"version_id": True},
        {"version_id": 3.5},
        {"version_id": 3, "map_run_id": "9"},
    ],
)
def test_a_marker_that_is_not_a_version_payload_is_refused(raw: object) -> None:
    assert serving.normalised(raw) is None


def test_parse_marker_reads_json_text_and_bytes() -> None:
    body = serving.encode_marker(versions.version_payload(None))
    assert serving.parse_marker(body) == versions.version_payload(None)
    assert serving.parse_marker(body.encode()) == versions.version_payload(None)
    assert serving.parse_marker(b"{not json") is None
    assert serving.parse_marker(b"") is None
    assert serving.parse_marker(None) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (json.dumps({"version_id": 41}).encode(), 41),
        (b'{"version_id": 41, "extra": true}', 41),
        (b'{"version_id": 0}', None),
        (b'{"version_id": -4}', None),
        (b'{"version_id": "41"}', None),
        (b'{"version_id": true}', None),
        (b'{"version_id": 4.1}', None),
        (b"[41]", None),
        (b"41", None),
        (b"", None),
        (b"\xff\xfe", None),
        (b"{", None),
    ],
)
def test_the_seen_report_body(body: bytes, expected: int | None) -> None:
    assert seen_version_id(body) == expected


def test_windy_data_carry_the_credit_its_terms_name_and_the_data_source() -> None:
    from app.models import ForecastSource
    from app.services.forecasts import queries

    source = ForecastSource(code="windy_gfswave", name="w", provider="windy", wave_model="gfsWave")
    links = [(link.label, link.href) for link in queries.attribution_links_for(source)]
    assert links[0] == ("Contains data from the Windy database", "https://www.windy.com/")
    assert ("NOAA NCEP GFS and GFS-Wave (modified)", queries._NOAA_GFS_LINK.href) in links
    # The plain-text credit of the version payload says the same.
    assert versions.attribution_for("windy") == [links[0][0]]
