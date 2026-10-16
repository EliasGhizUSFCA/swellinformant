#!/usr/bin/env python
"""Harmonic tide constants at every surf spot from the EOT20 global ocean tide model.

EOT20 (DGFI-TUM; Hart-Davis et al. 2021, https://doi.org/10.5194/essd-13-3869-2021) is
distributed by SEANOE (https://doi.org/10.17882/79489) under CC BY 4.0 as one 2.3 GB zip,
``85762.zip``. Its layout (read from the archive's own zip directories):

    85762.zip
      load_tides.zip   1.12 GB (deflated)   load tides: not needed (see below)
      ocean_tides.zip  1.21 GB (deflated)   17 files ocean_tides/<C>_ocean_eot20.nc

Each ``.nc`` file is a netCDF-4 (HDF5) grid at 1/8 degree of one constituent (2N2, J1, K1,
K2, M2, M4, MF, MM, N2, O1, P1, Q1, S1, S2, SA, SSA, T2) with its amplitude (cm) and
Greenwich phase lag (degrees). The ocean tide (not the geocentric tide = ocean + load) is
what a tide gauge on land measures, so only ``ocean_tides.zip`` is read.

The whole archive is never downloaded or stored:

1. the outer zip's central directory is read with HTTP Range requests (a seekable HTTP
   file handed to :mod:`zipfile`), which gives the offset of ``ocean_tides.zip``;
2. ``ocean_tides.zip`` is itself deflated inside the outer zip, so its members cannot be
   reached by offset. Its compressed bytes (and only those: ``load_tides.zip`` is skipped)
   are streamed once through zlib, its local file headers are parsed in order, and each
   constituent file is written to ``--work`` ONE AT A TIME, checked against its CRC-32,
   read for the cells around every spot, and deleted before the next one is written.
   Peak disk use is one constituent file (reported at the end).
3. The cell windows read in step 2 are cached in ``--work/eot20_windows.npz`` so the cell
   selection (step 4) can be re-run without downloading anything (``--from-cache``).
4. For every spot the tide point is the nearest ocean cell to the break (``latitude``,
   ``longitude`` in data/surf_spots/spots.json) at which all 17 constituents are defined;
   its distances to the break and to the spot's forecast point are recorded.

Output (``--out``, default data/tides/constants_eot20.json): per spot the cell position and
the amplitude (metres) and Greenwich phase lag (degrees) of each constituent, with the
archive provenance (file names, sizes, CRC-32, cell coordinates) and the licence.
app/services/tides/harmonic_model.py evaluates these constants; heights are relative to the
model's mean sea level (datum "model_msl"), not to any chart datum.

Requirements: numpy, httpx and h5py (``pip install -e 'backend[tides-extract]' h5py``; the
extra installs pyTMD 3.0.9, used only by ``--check-pytmd`` to cross-check
app/services/tides/harmonic_model.py against an independent implementation).

    python scripts/extract_tide_constants.py --work /tmp/eot20      # download + extract
    python scripts/extract_tide_constants.py --work /tmp/eot20 --from-cache
    python scripts/extract_tide_constants.py --work /tmp/eot20 --check-pytmd
    python scripts/extract_tide_constants.py --work /tmp/coops --validate 2026-10-14

Network: about 1.21 GB streamed (the ocean-tide part of the archive), nothing kept.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import struct
import sys
import time
import zipfile
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np

ARCHIVE_URL = "https://www.seanoe.org/data/00683/79489/data/85762.zip"
DATASET_DOI = "https://doi.org/10.17882/79489"
PAPER_DOI = "https://doi.org/10.5194/essd-13-3869-2021"
USER_AGENT = "SwellTravelAgent/1.0 (+https://github.com/eliasghizusfca/swellinformant)"
OCEAN_MEMBER = "ocean_tides.zip"
CONSTITUENTS = (
    "2N2",
    "J1",
    "K1",
    "K2",
    "M2",
    "M4",
    "MF",
    "MM",
    "N2",
    "O1",
    "P1",
    "Q1",
    "S1",
    "S2",
    "SA",
    "SSA",
    "T2",
)
# Cells read around each spot (half-width in degrees); the selected cell is the nearest
# valid one to the break, which is within this box for every spot (checked at run time).
WINDOW_HALF_DEG = 1.0
EARTH_RADIUS_KM = 6371.0088
REPO = Path(__file__).resolve().parents[1]
DEFAULT_SPOTS = REPO / "data" / "surf_spots" / "spots.json"
DEFAULT_OUT = REPO / "data" / "tides" / "constants_eot20.json"
CHUNK = 1 << 20

LICENCE = "CC BY 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"
ATTRIBUTION = "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE"
# CC BY 4.0 section 3(a)(1)(B): say that the material was modified, and how.
CHANGES = (
    "Modified: the harmonic constants of the EOT20 cell nearest each break were extracted, "
    "amplitudes converted from cm to m, and tide heights are computed from them by this app."
)
CITATION = (
    "Hart-Davis, M. G., Piccioni, G., Dettmering, D., Schwatke, C., Passaro, M. and Seitz, F. "
    "(2021): EOT20: a global ocean tide model from multi-mission satellite altimetry. Earth "
    f"System Science Data 13, 3869-3884, {PAPER_DOI}. Data: Hart-Davis, M. et al. (2021), "
    f"EOT20 - A global Empirical Ocean Tide model from multi-mission satellite altimetry, "
    f"SEANOE, {DATASET_DOI}."
)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


class HttpRangeFile(io.RawIOBase):
    """A read-only, seekable file over HTTP Range requests (enough for zipfile)."""

    def __init__(self, client: httpx.Client, url: str) -> None:
        self.client, self.url = client, url
        head = client.head(url, follow_redirects=True)
        head.raise_for_status()
        if head.headers.get("accept-ranges") != "bytes":
            raise SystemExit(f"{url} does not advertise byte ranges; refusing a full download")
        self.size = int(head.headers["content-length"])
        self.pos = 0
        self.fetched = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self.pos
        if size == 0 or self.pos >= self.size:
            return b""
        end = min(self.size, self.pos + size) - 1
        r = self.client.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        if r.status_code != 206:
            raise SystemExit(f"Range request answered HTTP {r.status_code}; refusing")
        self.pos += len(r.content)
        self.fetched += len(r.content)
        return r.content

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def stream_ocean_member(client: httpx.Client, log: dict[str, Any]) -> Iterator[bytes]:
    """The decompressed bytes of ocean_tides.zip, streamed (never stored)."""
    remote = HttpRangeFile(client, ARCHIVE_URL)
    outer = zipfile.ZipFile(remote)
    info = outer.getinfo(OCEAN_MEMBER)
    log["archive"] = {
        "url": ARCHIVE_URL,
        "size_bytes": remote.size,
        "members": [
            {"name": i.filename, "size_bytes": i.file_size, "compressed_bytes": i.compress_size}
            for i in outer.infolist()
        ],
    }
    remote.seek(info.header_offset)
    header = remote.read(30)
    sig, *_rest, name_len, extra_len = struct.unpack("<IHHHHHIIIHH", header)
    if sig != 0x04034B50:
        raise SystemExit("ocean_tides.zip: bad local header")
    start = info.header_offset + 30 + name_len + extra_len
    end = start + info.compress_size - 1
    log["directory_bytes_read"] = remote.fetched
    print(
        f"streaming {OCEAN_MEMBER}: {info.compress_size / 1e9:.2f} GB "
        f"(bytes {start}-{end} of {remote.size})",
        file=sys.stderr,
    )
    inflater = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    streamed = 0
    t0 = time.monotonic()
    with client.stream("GET", ARCHIVE_URL, headers={"Range": f"bytes={start}-{end}"}) as r:
        if r.status_code != 206:
            raise SystemExit(f"Range request answered HTTP {r.status_code}; refusing")
        for chunk in r.iter_bytes(CHUNK):
            streamed += len(chunk)
            if streamed // (100 << 20) != (streamed - len(chunk)) // (100 << 20):
                rate = streamed / 1e6 / max(1e-3, time.monotonic() - t0)
                print(f"  {streamed / 1e9:.2f} GB streamed ({rate:.0f} MB/s)", file=sys.stderr)
            yield inflater.decompress(chunk) if inflater else chunk
        if inflater:
            yield inflater.flush()
    log["streamed_bytes"] = streamed


class ByteStream:
    """Sequential reader with look-ahead over an iterator of byte chunks."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = chunks
        self.buf = bytearray()

    def _fill(self, n: int) -> bool:
        while len(self.buf) < n:
            try:
                self.buf += next(self.chunks)
            except StopIteration:
                return False
        return True

    def read(self, n: int) -> bytes:
        if not self._fill(n):
            raise SystemExit("ocean_tides.zip ended early")
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def copy(self, n: int, sink: Any) -> None:
        """Move ``n`` bytes to ``sink(chunk)`` without holding them all in memory."""
        while n > 0:
            if not self.buf and not self._fill(1):
                raise SystemExit("ocean_tides.zip ended early")
            take = min(n, len(self.buf))
            sink(bytes(self.buf[:take]))
            del self.buf[:take]
            n -= take

    def inflate(self, sink: Any) -> int:
        """Inflate one raw deflate stream from here to its end; ``sink(data)`` gets the
        output. The bytes after the stream stay in the buffer. Returns the compressed size."""
        inflater = zlib.decompressobj(-15)
        used = 0
        while not inflater.eof:
            if not self.buf and not self._fill(1):
                raise SystemExit("ocean_tides.zip ended inside a deflate stream")
            data = bytes(self.buf)
            self.buf.clear()
            sink(inflater.decompress(data))
            used += len(data) - len(inflater.unused_data)
            self.buf[:0] = inflater.unused_data
        return used


def inner_members(
    stream: ByteStream, work: Path, wanted: dict[str, str], log: dict[str, Any]
) -> Iterator[tuple[str, Path]]:
    """Write each wanted member of the inner zip to ``work``, yield it, then delete it."""
    files: list[dict[str, Any]] = log.setdefault("files", [])
    while True:
        sig = struct.unpack("<I", stream.read(4))[0]
        if sig != 0x04034B50:
            return  # central directory (0x02014b50): no more members
        (_ver, flag, method, _t, _d, crc, csize, usize, nlen, elen) = struct.unpack(
            "<HHHHHIIIHH", stream.read(26)
        )
        name = stream.read(nlen).decode()
        extra = stream.read(elen)
        descriptor = bool(flag & 0x08)  # sizes and CRC follow the data
        if descriptor and method != zipfile.ZIP_DEFLATED:
            raise SystemExit(f"{name}: stored member with a data descriptor is not supported")
        if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF:
            # zip64: the real sizes are in the extra field (id 0x0001).
            pos = 0
            while pos + 4 <= len(extra):
                hid, hlen = struct.unpack("<HH", extra[pos : pos + 4])
                if hid == 0x0001:
                    usize, csize = struct.unpack("<QQ", extra[pos + 4 : pos + 20])
                pos += 4 + hlen
        if method not in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
            raise SystemExit(f"{name}: compression method {method} not supported")
        constituent = wanted.get(name)
        target = work / Path(name).name
        crc_now = 0
        written = 0
        out = target.open("wb") if constituent is not None else None

        def sink(data: bytes, out: Any = out) -> None:
            nonlocal crc_now, written
            crc_now = zlib.crc32(data, crc_now)
            written += len(data)
            if out is not None:
                out.write(data)

        try:
            if descriptor:
                stream.inflate(sink)
                # Data descriptor: optional signature, CRC-32, then 4- or 8-byte sizes.
                head = stream.read(4)
                if struct.unpack("<I", head)[0] == 0x08074B50:
                    head = stream.read(4)
                crc = struct.unpack("<I", head)[0]
                sizes = stream.read(8)
                csize, usize = struct.unpack("<II", sizes)
                if usize != written:  # a zip64 descriptor: 8-byte sizes
                    sizes += stream.read(8)
                    csize, usize = struct.unpack("<QQ", sizes)
            elif method == zipfile.ZIP_DEFLATED:
                stream.inflate(sink)
            else:
                stream.copy(csize, sink)
        finally:
            if out is not None:
                out.close()
        if constituent is None:
            continue
        size = target.stat().st_size
        if size != usize or crc_now != crc:
            target.unlink()
            raise SystemExit(f"{name}: size or CRC-32 mismatch")
        files.append({"name": name, "size_bytes": size, "crc32": f"{crc:08x}"})
        log["peak_member_bytes"] = max(log.get("peak_member_bytes", 0), size)
        try:
            yield constituent, target
        finally:
            target.unlink(missing_ok=True)


def _axis(values: np.ndarray) -> np.ndarray:
    return np.asarray(values, dtype=np.float64).ravel()


def read_windows(
    path: Path, spots: list[dict[str, Any]], log: dict[str, Any]
) -> dict[str, dict[str, np.ndarray]]:
    """Amplitude (m) and phase (deg) in a box around every spot, from one constituent file."""
    import h5py

    out: dict[str, dict[str, np.ndarray]] = {}
    with h5py.File(path, "r") as nc:
        if "structure" not in log:
            log["structure"] = {
                key: {"shape": list(var.shape), "dtype": str(var.dtype)}
                for key, var in nc.items()
                if hasattr(var, "shape")
            }
        lat = _axis(nc["lat"][()])
        lon = _axis(nc["lon"][()])
        amp_var, pha_var = nc["amplitude"], nc["phase"]
        raw_units = amp_var.attrs.get("units", b"")
        units = raw_units.decode() if isinstance(raw_units, bytes) else str(raw_units)
        scale = {"cm": 0.01, "m": 1.0, "mm": 0.001}.get(units.strip())
        if scale is None:
            raise SystemExit(f"{path.name}: unexpected amplitude units {units!r}")
        lat_first = amp_var.shape[0] == lat.size
        for spot in spots:
            lo_lat, hi_lat = spot["latitude"] - WINDOW_HALF_DEG, spot["latitude"] + WINDOW_HALF_DEG
            ii = np.where((lat >= lo_lat) & (lat <= hi_lat))[0]
            lon_mod = np.mod(lon, 360.0)
            centre = spot["longitude"] % 360.0
            dl = np.abs((lon_mod - centre + 180.0) % 360.0 - 180.0)
            jj = np.where(dl <= WINDOW_HALF_DEG)[0]
            jj = jj[np.argsort((lon_mod[jj] - centre + 180.0) % 360.0)]
            i0, i1 = int(ii.min()), int(ii.max()) + 1
            amp_rows, pha_rows = [], []
            for j in jj:
                if lat_first:
                    a = amp_var[i0:i1, int(j)]
                    p = pha_var[i0:i1, int(j)]
                else:
                    a = amp_var[int(j), i0:i1]
                    p = pha_var[int(j), i0:i1]
                amp_rows.append(np.asarray(a, dtype=np.float64))
                pha_rows.append(np.asarray(p, dtype=np.float64))
            amp = np.stack(amp_rows, axis=1)  # (lat, lon)
            pha = np.stack(pha_rows, axis=1)
            # Land and unresolved cells hold the _FillValue 0 (amplitude and phase) or NaN;
            # pyTMD's EOT20 reader masks the same cells (amplitude or phase exactly 0).
            bad = ~np.isfinite(amp) | ~np.isfinite(pha) | (amp == 0.0) | (pha == 0.0)
            amp = np.where(bad, np.nan, amp * scale)
            pha = np.where(bad, np.nan, pha)
            out[spot["slug"]] = {
                "lat": lat[i0:i1],
                "lon": ((lon_mod[jj] + 180.0) % 360.0) - 180.0,
                "amp": amp,
                "phase": pha,
            }
    return out


def download(spots: list[dict[str, Any]], work: Path, log: dict[str, Any]) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    wanted = {f"ocean_tides/{c}_ocean_eot20.nc": c for c in CONSTITUENTS}
    arrays: dict[str, np.ndarray] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300) as client:
        stream = ByteStream(stream_ocean_member(client, log))
        for constituent, path in inner_members(stream, work, wanted, log):
            print(f"  {path.name}: {path.stat().st_size / 1e6:.1f} MB", file=sys.stderr)
            for slug, win in read_windows(path, spots, log).items():
                for key, value in win.items():
                    arrays[f"{slug}|{constituent}|{key}"] = value
    found = {f["name"] for f in log.get("files", [])}
    missing = sorted(set(wanted) - found)
    if missing:
        raise SystemExit(f"members not found in the archive: {missing}")
    cache = work / "eot20_windows.npz"
    np.savez_compressed(cache, **arrays)
    (work / "eot20_download_log.json").write_text(json.dumps(log, indent=1))
    return cache


def select_points(
    spots: list[dict[str, Any]], cache: Path, log: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    data = np.load(cache)
    points: dict[str, dict[str, Any]] = {}
    for spot in spots:
        slug = spot["slug"]
        lat = data[f"{slug}|M2|lat"]
        lon = data[f"{slug}|M2|lon"]
        valid = np.ones((lat.size, lon.size), dtype=bool)
        for c in CONSTITUENTS:
            valid &= np.isfinite(data[f"{slug}|{c}|amp"]) & np.isfinite(data[f"{slug}|{c}|phase"])
        if not valid.any():
            points[slug] = {"available": False, "reason": "no ocean cell within the window"}
            continue
        best: tuple[float, int, int] | None = None
        for i in range(lat.size):
            for j in range(lon.size):
                if valid[i, j]:
                    d = haversine_km(spot["latitude"], spot["longitude"], lat[i], lon[j])
                    if best is None or d < best[0]:
                        best = (d, i, j)
        assert best is not None
        d, i, j = best
        edge = WINDOW_HALF_DEG * 111.0 * min(1.0, math.cos(math.radians(spot["latitude"])))
        if d > edge:
            raise SystemExit(f"{slug}: nearest valid cell {d:.0f} km away, beyond the window")
        cell_lat, cell_lon = float(lat[i]), float(lon[j])
        constants = {
            c: [
                round(float(data[f"{slug}|{c}|amp"][i, j]), 5),
                round(float(data[f"{slug}|{c}|phase"][i, j]) % 360.0, 3),
            ]
            for c in CONSTITUENTS
        }
        points[slug] = {
            "available": True,
            "latitude": round(cell_lat, 4),
            "longitude": round(cell_lon, 4),
            "distance_km": round(d, 1),
            "forecast_point_distance_km": round(
                haversine_km(
                    spot["forecast_latitude"], spot["forecast_longitude"], cell_lat, cell_lon
                ),
                1,
            ),
            "constants": constants,
        }
    return points


def build_output(
    spots: list[dict[str, Any]], points: dict[str, dict[str, Any]], log: dict[str, Any]
) -> dict[str, Any]:
    return {
        "model": "EOT20",
        "source": "eot20",
        "datum": "model_msl",
        "datum_text": "model mean sea level (EOT20); not a chart datum",
        "licence": LICENCE,
        "licence_url": LICENCE_URL,
        "attribution": ATTRIBUTION,
        "source_url": DATASET_DOI,
        "changes": CHANGES,
        "citation": CITATION,
        "extracted_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "extracted_by": "scripts/extract_tide_constants.py",
        "archive": {
            **log.get("archive", {}),
            "member_read": OCEAN_MEMBER,
            "files": log.get("files", []),
        },
        "grid": "1/8 degree regular latitude-longitude grid (cell centres as listed)",
        "selection": (
            "Nearest grid cell to the break (spots.json latitude/longitude) at which every "
            "constituent is defined (EOT20 masks land and cells it does not resolve)."
        ),
        "units": {"amplitude": "m", "phase": "degrees, Greenwich phase lag (G)"},
        "convention": "h(t) = sum f * A * cos(V0 + u - G), V0 + u for Greenwich (UTC)",
        "constituents": list(CONSTITUENTS),
        "points": {s["slug"]: points[s["slug"]] for s in spots},
    }


def check_pytmd(out_path: Path, days: int = 30) -> float:
    """Largest difference (m) between app/services/tides/harmonic_model.predict and pyTMD
    3.0.9's FES-type arguments and nodal corrections for the same constants, over ``days``
    days from 2026-10-01 at 10-minute steps (no minor constituents on either side)."""
    sys.path.insert(0, str(REPO / "backend"))
    import pyTMD.constituents  # type: ignore[import-not-found]

    from app.services.tides.harmonic_model import load_constants, predict

    start = datetime(2026, 10, 1, tzinfo=UTC).timestamp()
    stamps = [start + 600 * k for k in range(days * 144)]
    when = [datetime.fromtimestamp(t, UTC) for t in stamps]
    mjd = np.array([t / 86400.0 + 40587.0 for t in stamps])
    worst = 0.0
    for slug, spot in load_constants(out_path).items():
        names = [c.lower() for c in spot.names]
        pu, pf, g = pyTMD.constituents.arguments(mjd, names, corrections="FES")
        amp = np.array(spot.amplitudes)[None, :]
        pha = np.radians(np.array(spot.phases))[None, :]
        ref = np.sum(pf * amp * np.cos(np.radians(g) + pu - pha), axis=1)
        diff = float(np.max(np.abs(ref - predict(spot, when))))
        worst = max(worst, diff)
        print(f"{slug:28s} max |harmonic_model - pyTMD| = {diff * 1000:.3f} mm")
    print(f"worst {worst * 1000:.3f} mm over {days} days")
    return worst


# (spot, CO-OPS station, kind): the stations the catalog uses for the US spots, plus two
# harmonic stations near subordinate ones (their full curve can be compared).
VALIDATION_PAIRS = (
    ("pipeline", "1612668", "subordinate"),
    ("sunset-beach", "1612668", "subordinate"),
    ("waimea-bay", "1612668", "subordinate"),
    ("jaws-peahi", "1615680", "harmonic"),
    ("mavericks", "9414131", "harmonic"),
    ("ocean-beach-sf", "9414275", "subordinate"),
    ("ocean-beach-sf", "9414290", "harmonic"),
    ("trestles", "TWC0419", "subordinate"),
    ("trestles", "9410580", "harmonic"),
    ("rincon", "9411270", "harmonic"),
)
COOPS_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def _coops(
    client: httpx.Client, cache: Path, station: str, product: str, start: datetime, days: int
) -> list[tuple[datetime, float, str | None]]:
    """CO-OPS predictions (``product`` "hilo": MLLW high/low waters; "six": 6-minute curve,
    MSL), read from ``cache`` or fetched once and kept there."""
    path = cache / f"{product}_{station}.json"
    if not path.exists():
        params = {
            "product": "predictions",
            "application": "SwellTravelAgent",
            "station": station,
            "begin_date": start.strftime("%Y%m%d"),
            "range": str(24 * days),
            "datum": "MLLW" if product == "hilo" else "MSL",
            "time_zone": "gmt",
            "units": "metric",
            "interval": "hilo" if product == "hilo" else "6",
            "format": "json",
        }
        r = client.get(COOPS_API, params=params)
        r.raise_for_status()
        if "error" in r.json():
            raise SystemExit(f"CO-OPS {station} {product}: {r.json()['error']}")
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text)
    rows = json.loads(path.read_text())["predictions"]
    return [
        (
            datetime.strptime(x["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC),
            float(x["v"]),
            x.get("type"),
        )
        for x in rows
    ]


def validate(constants_path: Path, cache: Path, start: datetime, days: int) -> list[dict[str, Any]]:
    """The EOT20 synthesis at each US spot's tide point against the CO-OPS predictions of
    the station the catalog pairs it with, over ``days`` days from ``start``:

    * curve (harmonic stations, 6-minute values, datum MSL): RMSE after removing the mean
      offset (the datum difference), correlation, range ratio;
    * high and low waters (every station, MLLW): each CO-OPS event matched with the
      model's nearest event of the same kind within 3 h; the share matched, the median
      absolute timing error and the event-height RMSE after removing the mean offset.

    The model events are app.services.tides.harmonic_model.predict_events, as stored by
    the app.
    """
    from datetime import timedelta

    sys.path.insert(0, str(REPO / "backend"))
    from app.services.tides.harmonic_model import load_constants, predict, predict_events

    constants = load_constants(constants_path)
    end = start + timedelta(days=days)
    out: list[dict[str, Any]] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60) as client:
        for slug, station, kind in VALIDATION_PAIRS:
            spot = constants[slug]
            row: dict[str, Any] = {"spot": slug, "station": station, "kind": kind}
            if kind == "harmonic":
                six = _coops(client, cache, station, "six", start, days)
                times = [t for t, _, _ in six if start <= t < end]
                ref = np.array([v for t, v, _ in six if start <= t < end])
                mod = predict(spot, times)
                offset = float(np.mean(ref) - np.mean(mod))
                row.update(
                    curve_points=len(times),
                    rmse_cm=round(100 * float(np.sqrt(np.mean((mod + offset - ref) ** 2))), 1),
                    r=round(float(np.corrcoef(mod, ref)[0, 1]), 4),
                    range_ratio=round(float(np.ptp(mod) / np.ptp(ref)), 3),
                    msl_offset_m=round(offset, 3),
                )
            events = [
                (t, v, "high" if typ in ("H", "HH") else "low")
                for t, v, typ in _coops(client, cache, station, "hilo", start, days)
                if start <= t < end
            ]
            model = predict_events(spot, start - timedelta(hours=6), end + timedelta(hours=6))
            dts: list[float] = []
            dhs: list[float] = []
            for t, v, k in events:
                same = [(abs((mt - t).total_seconds()), mt, mh) for mt, mh, mk in model if mk == k]
                if not same:
                    continue
                gap, mt, mh = min(same)
                if gap <= 3 * 3600:
                    dts.append((mt - t).total_seconds() / 60.0)
                    dhs.append(mh - v)
            dh = np.array(dhs)
            row.update(
                events=len(events),
                events_matched=len(dts),
                median_abs_timing_min=round(float(np.median(np.abs(dts))), 1) if dts else None,
                mean_timing_min=round(float(np.mean(dts)), 1) if dts else None,
                event_height_rmse_cm=round(100 * float(np.sqrt(np.mean((dh - dh.mean()) ** 2))), 1)
                if dts
                else None,
            )
            out.append(row)
            print(json.dumps(row))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spots", type=Path, default=DEFAULT_SPOTS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--work", type=Path, required=True, help="scratch directory")
    parser.add_argument("--from-cache", action="store_true", help="reuse --work's windows")
    parser.add_argument("--check-pytmd", action="store_true", help="compare with pyTMD")
    parser.add_argument(
        "--validate",
        metavar="YYYY-MM-DD",
        help="compare the model with CO-OPS predictions for 14 days from this date (UTC); "
        "CO-OPS answers are cached in --work",
    )
    args = parser.parse_args(argv)
    if args.check_pytmd:
        return 0 if check_pytmd(args.out) < 0.005 else 1
    if args.validate:
        start = datetime.strptime(args.validate, "%Y-%m-%d").replace(tzinfo=UTC)
        validate(args.out, args.work, start, 14)
        return 0
    spots = json.loads(args.spots.read_text())
    log: dict[str, Any] = {}
    cache = args.work / "eot20_windows.npz"
    if args.from_cache:
        log = json.loads((args.work / "eot20_download_log.json").read_text())
    else:
        cache = download(spots, args.work, log)
    points = select_points(spots, cache, log)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(build_output(spots, points, log), indent=1) + "\n")
    print(
        f"wrote {args.out} ({args.out.stat().st_size / 1e3:.1f} kB); peak member on disk "
        f"{log.get('peak_member_bytes', 0) / 1e6:.1f} MB",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
