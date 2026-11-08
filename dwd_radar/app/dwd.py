"""DWD Open Data access and decoding of the RV composite (ODIM_H5 in tar).

RV = "Komposit der über 5 Minuten akkumulierten Niederschlagsmenge in mm".
Each 5-minute release is one tar  composite_rv_YYYYmmdd_HHMM.tar  with 25 members
composite_rv_YYYYmmdd_HHMM_PPP-hd5, PPP = lead in minutes (000 = analysis).
"""
from __future__ import annotations

import bz2
import gzip
import io
import logging
import re
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import h5py
import numpy as np
import requests

log = logging.getLogger("dwd_radar.dwd")

BASE = "https://opendata.dwd.de/weather/radar/composite/rv/"
NAME_RE = re.compile(r'href="(composite_rv_(\d{8})_(\d{4})\.tar)"')
LISTING_RE = re.compile(
    r'href="(composite_rv_(\d{8})_(\d{4})\.tar)">[^<]*</a>\s+(\d{2}-\w{3}-\d{4} \d{2}:\d{2})'
)
MEMBER_RE = re.compile(r"_(\d{3})-hd5(\.bz2|\.gz)?$")

UA = {"User-Agent": "ha-dwd-radar/0.1 (Home Assistant add-on)"}


@dataclass
class Grid:
    """Static geometry of the composite grid (identical for all frames)."""
    projdef: str
    ul_x: float          # projected coordinate of upper-left corner [m]
    ul_y: float
    xscale: float
    yscale: float
    xsize: int
    ysize: int

    def key(self) -> tuple:
        return (self.projdef, round(self.ul_x), round(self.ul_y),
                self.xscale, self.yscale, self.xsize, self.ysize)


@dataclass
class Frame:
    valid: datetime      # end of the 5-min accumulation window (UTC)
    lead: int            # minutes after release
    rate: np.ndarray     # mm/h, float32, NaN = no data / outside coverage


class DwdError(Exception):
    pass


def list_runs(session: requests.Session, timeout: float = 20) -> list[tuple[datetime, datetime | None]]:
    """Return available releases (nominal time, last-modified) sorted ascending."""
    r = session.get(BASE, timeout=timeout, headers=UA)
    r.raise_for_status()
    runs: dict[datetime, datetime | None] = {}
    for m in LISTING_RE.finditer(r.text):
        ts = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        try:
            lm = datetime.strptime(m.group(4), "%d-%b-%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            lm = None
        runs[ts] = lm
    if not runs:  # listing format changed? fall back to bare names
        for m in NAME_RE.finditer(r.text):
            ts = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            runs[ts] = None
    if not runs:
        raise DwdError("Keine composite_rv_*.tar im Verzeichnis gefunden – Format/Pfad geändert?")
    return sorted(runs.items())


def run_url(ts: datetime) -> str:
    return f"{BASE}composite_rv_{ts:%Y%m%d_%H%M}.tar"


def download_run(session: requests.Session, ts: datetime, timeout: float = 90) -> bytes:
    r = session.get(run_url(ts), timeout=timeout, headers=UA)
    r.raise_for_status()
    return r.content


def _read_h5(raw: bytes) -> tuple[Grid, datetime, np.ndarray]:
    with h5py.File(io.BytesIO(raw), "r") as f:
        w = f["where"].attrs
        projdef = w["projdef"]
        projdef = projdef.decode() if isinstance(projdef, bytes) else str(projdef)
        import pyproj
        p = pyproj.Proj(projdef)
        ul_x, ul_y = p(float(w["UL_lon"]), float(w["UL_lat"]))
        grid = Grid(projdef, ul_x, ul_y, float(w["xscale"]), float(w["yscale"]),
                    int(w["xsize"]), int(w["ysize"]))
        dw = f["dataset1/what"].attrs
        ed = dw.get("enddate", f["what"].attrs["date"])
        et = dw.get("endtime", f["what"].attrs["time"])
        ed = ed.decode() if isinstance(ed, bytes) else str(ed)
        et = et.decode() if isinstance(et, bytes) else str(et)
        valid = datetime.strptime(ed + et, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        what = f["dataset1/data1/what"].attrs
        gain = float(what["gain"]); offset = float(what["offset"])
        nodata = float(what["nodata"]); undetect = float(what["undetect"])
        q = what.get("quantity", b"ACRR")
        q = q.decode() if isinstance(q, bytes) else str(q)
        raw_d = f["dataset1/data1/data"][:]
    if q != "ACRR":
        raise DwdError(f"Unerwartete Größe {q!r} (erwartet ACRR)")
    if raw_d.shape != (grid.ysize, grid.xsize):
        raise DwdError(f"Rastergröße {raw_d.shape} passt nicht zu where/x/ysize")
    d = raw_d.astype(np.float64)
    mm = d * gain + offset
    mm[raw_d == undetect] = 0.0
    mm[raw_d == nodata] = np.nan
    mm[mm < 0] = 0.0
    # 5-minute accumulation -> rate
    return grid, valid, (mm * 12.0).astype(np.float32)


_warned_valid = False


def parse_run(blob: bytes, release: datetime, leads: set[int] | None = None) -> tuple[Grid, list[Frame]]:
    """Decode the requested leads of one release tar (None = all).

    Valid time = release + lead. The time stored in the file is only compared
    (its meaning for forecast members is not documented consistently).
    """
    global _warned_valid
    frames: list[Frame] = []
    grid: Grid | None = None
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            mm = MEMBER_RE.search(m.name)
            if not mm:
                continue
            lead = int(mm.group(1))
            if leads is not None and lead not in leads:
                continue
            raw = tf.extractfile(m).read()
            if mm.group(2) == ".bz2":
                raw = bz2.decompress(raw)
            elif mm.group(2) == ".gz":
                raw = gzip.decompress(raw)
            g, file_valid, rate = _read_h5(raw)
            valid = release + timedelta(minutes=lead)
            if file_valid != valid and not _warned_valid:
                log.info("Hinweis: Zeit in Datei (%s) != Lauf+Lead (%s) bei Lead %d – verwende Lauf+Lead",
                         file_valid.isoformat(), valid.isoformat(), lead)
                _warned_valid = True
            if grid is None:
                grid = g
            elif g.key() != grid.key():
                raise DwdError("Unterschiedliche Raster innerhalb eines Laufs")
            frames.append(Frame(valid=valid, lead=lead, rate=rate))
    if grid is None:
        raise DwdError("Keine *-hd5-Mitglieder im Tar gefunden")
    frames.sort(key=lambda fr: fr.lead)
    return grid, frames


def floor5(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0) - timedelta(minutes=t.minute % 5)
