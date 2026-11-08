"""DWD ICON model data (Open Data) for precipitation and cloud cover beyond the radar nowcast.

Sources, best first:
  ICON-D2  2.2 km, regular lat/lon, runs every 3 h, +27 h (03 UTC: +45 h), hourly
  ICON-EU  6.5 km, regular lat/lon, 00/06/12/18 UTC +120 h (others +30 h), hourly to 78 h then 3-hourly
  ICON     13 km, icosahedral grid, 00/12 UTC +180 h (06/18: +120 h), hourly to 78 h then 3-hourly

All fields are resampled once onto a fixed Web-Mercator target grid. Only the
steps that are actually needed are downloaded and cached per run.
"""
from __future__ import annotations

import bz2
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes
import numpy as np
import requests
from scipy import ndimage
from scipy.spatial import cKDTree

log = logging.getLogger("dwd_radar.models")

UA = {"User-Agent": "ha-dwd-radar (Home Assistant add-on)"}
STEP_RE = re.compile(r'href="([^"]+?_(\d{10})_(\d{3})(?:_2d)?_[A-Za-z_]+\.grib2\.bz2)"')


def utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc)


@dataclass
class Source:
    key: str
    label: str
    base: str                      # url template with {hh} and {var}
    vars: dict                     # logical -> directory name
    run_every: int                 # hours
    hourly_until: int              # steps hourly up to here, 3-hourly afterwards

    def max_step(self, run_hour: int) -> int:
        if self.key == "d2":
            return 45 if run_hour == 3 else 27
        if self.key == "eu":
            return 120 if run_hour % 6 == 0 else 30
        return 180 if run_hour in (0, 12) else 120

    def steps(self, run_hour: int) -> list[int]:
        m = self.max_step(run_hour)
        return [s for s in range(0, m + 1) if s <= self.hourly_until or s % 3 == 0]


SOURCES = [
    Source("d2", "ICON-D2", "https://opendata.dwd.de/weather/nwp/icon-d2/grib/{hh:02d}/{var}/",
           {"prec": "tot_prec", "cloud": "clct"}, 3, 999),
    Source("eu", "ICON-EU", "https://opendata.dwd.de/weather/nwp/icon-eu/grib/{hh:02d}/{var}/",
           {"prec": "tot_prec", "cloud": "clct"}, 3, 78),
    Source("gl", "ICON", "https://opendata.dwd.de/weather/nwp/icon/grib/{hh:02d}/{var}/",
           {"prec": "tot_prec", "cloud": "clct"}, 6, 78),
]


class TargetGrid:
    """Fixed Web-Mercator raster (pixel centres) of the model layer."""

    def __init__(self, lat_min, lat_max, lon_min, lon_max, res_m=1000.0):
        R = 6378137.0
        mx = lambda lon: math.radians(lon) * R
        my = lambda lat: R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
        x0, x1, y0, y1 = mx(lon_min), mx(lon_max), my(lat_min), my(lat_max)
        self.nx = max(2, int(round((x1 - x0) / res_m)))
        self.ny = max(2, int(round((y1 - y0) / res_m)))
        x1 = x0 + self.nx * res_m
        y0 = y1 - self.ny * res_m
        ilat = lambda y: math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)
        self.bounds = [[ilat(y0), math.degrees(x0 / R)], [ilat(y1), math.degrees(x1 / R)]]
        xs = x0 + (np.arange(self.nx) + 0.5) * res_m
        ys = y1 - (np.arange(self.ny) + 0.5) * res_m
        lon = np.degrees(xs / R)
        lat = np.degrees(2 * np.arctan(np.exp(ys / R)) - np.pi / 2)
        self.LON, self.LAT = np.meshgrid(lon, lat)


def _grib(raw: bytes):
    gid = eccodes.codes_new_from_message(raw)
    try:
        info = {k: eccodes.codes_get(gid, k) for k in ("gridType", "missingValue")}
        if info["gridType"] == "regular_ll":
            for k in ("Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
                      "latitudeOfLastGridPointInDegrees", "longitudeOfLastGridPointInDegrees"):
                info[k] = eccodes.codes_get(gid, k)
        vals = eccodes.codes_get_values(gid).astype(np.float32)
    finally:
        eccodes.codes_release(gid)
    vals[vals == info["missingValue"]] = np.nan
    return info, vals


class LatLonRegrid:
    def __init__(self, tg: TargetGrid, smooth_px: float):
        self.tg, self.smooth = tg, smooth_px
        self.key = None

    def __call__(self, info, vals) -> np.ndarray:
        ni, nj = info["Ni"], info["Nj"]
        la1, lo1 = info["latitudeOfFirstGridPointInDegrees"], info["longitudeOfFirstGridPointInDegrees"]
        la2, lo2 = info["latitudeOfLastGridPointInDegrees"], info["longitudeOfLastGridPointInDegrees"]
        lo1 = lo1 - 360 if lo1 > 180 else lo1
        lo2 = lo2 - 360 if lo2 > 180 else lo2
        key = (ni, nj, la1, lo1, la2, lo2)
        if key != self.key:
            dlat, dlon = (la2 - la1) / (nj - 1), (lo2 - lo1) / (ni - 1)
            rows = (self.tg.LAT - la1) / dlat
            cols = (self.tg.LON - lo1) / dlon
            self.r0 = max(0, int(rows.min()) - 4); self.r1 = min(nj, int(rows.max()) + 5)
            self.c0 = max(0, int(cols.min()) - 4); self.c1 = min(ni, int(cols.max()) + 5)
            self.coords = np.stack([rows - self.r0, cols - self.c0]).astype(np.float32)
            self.key = key
        d = vals.reshape(nj, ni)[self.r0:self.r1, self.c0:self.c1]
        d = np.nan_to_num(d, nan=0.0)
        if self.smooth > 0:
            d = ndimage.gaussian_filter(d, self.smooth, mode="nearest")
        return ndimage.map_coordinates(d, self.coords, order=1, mode="nearest").astype(np.float32)


class IcoRegrid:
    """Inverse-distance interpolation from the ICON icosahedral grid (3 neighbours)."""

    def __init__(self, tg: TargetGrid, cache: Path, http: requests.Session):
        self.tg, self.cache, self.http = tg, cache, http
        self.idx = None

    def _xyz(self, lat, lon):
        la, lo = np.radians(lat), np.radians(lon)
        return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=-1)

    def prepare(self, run: datetime) -> None:
        if self.idx is not None:
            return
        f = self.cache / "icon_ico_weights.npz"
        if f.exists():
            z = np.load(f)
            self.idx, self.w = z["idx"], z["w"]
            return
        coords = {}
        for v in ("clat", "clon"):
            url = f"https://opendata.dwd.de/weather/nwp/icon/grib/{run.hour:02d}/{v}/"
            lst = self.http.get(url, timeout=60, headers=UA)
            lst.raise_for_status()
            m = re.search(r'href="([^"]+\.grib2\.bz2)"', lst.text)
            if not m:
                raise RuntimeError(f"keine {v.upper()}-Datei für ICON gefunden")
            r = self.http.get(url + m.group(1), timeout=300, headers=UA)
            r.raise_for_status()
            _, coords[v] = _grib(bz2.decompress(r.content))
        lat, lon = np.degrees(coords["clat"]) if np.nanmax(np.abs(coords["clat"])) < 3.2 else coords["clat"], \
            np.degrees(coords["clon"]) if np.nanmax(np.abs(coords["clon"])) < 6.3 else coords["clon"]
        tlat, tlon = self.tg.LAT, self.tg.LON
        m = 1.0
        sel = np.where((lat > tlat.min() - m) & (lat < tlat.max() + m) &
                       (lon > tlon.min() - m) & (lon < tlon.max() + m))[0]
        tree = cKDTree(self._xyz(lat[sel], lon[sel]))
        d, i = tree.query(self._xyz(tlat.ravel(), tlon.ravel()), k=3)
        w = 1.0 / np.maximum(d, 1e-9) ** 2
        w /= w.sum(axis=1, keepdims=True)
        self.idx = sel[i].astype(np.int32)
        self.w = w.astype(np.float32)
        self.cache.mkdir(parents=True, exist_ok=True)
        np.savez(f, idx=self.idx, w=self.w)
        log.info("ICON-Ikosaeder-Gewichte berechnet (%d Zellen im Gebiet)", len(sel))

    def __call__(self, info, vals) -> np.ndarray:
        v = np.nan_to_num(vals, nan=0.0)
        out = (v[self.idx] * self.w).sum(axis=1)
        return out.reshape(self.tg.LAT.shape).astype(np.float32)


@dataclass
class RunData:
    run: datetime
    fields: dict = field(default_factory=dict)     # (var, step) -> array


class ModelStore:
    def __init__(self, tg: TargetGrid, cache: Path):
        self.tg = tg
        self.cache = cache
        self.http = requests.Session()
        self.regrid = {"d2": LatLonRegrid(tg, 0.8), "eu": LatLonRegrid(tg, 0.0),
                       "gl": IcoRegrid(tg, cache, self.http)}
        self.runs: dict[str, RunData] = {}
        self.lock = threading.Lock()
        self.listings: dict[tuple, tuple[float, dict]] = {}
        self.bytes_log: list[tuple[float, int]] = []
        self.error: str | None = None

    # ------------------------------------------------------------ helpers
    def _list(self, src: Source, hh: int, var: str) -> dict:
        key = (src.key, hh, var)
        now = time.time()
        if key in self.listings and now - self.listings[key][0] < 600:
            return self.listings[key][1]
        r = self.http.get(src.base.format(hh=hh, var=var), timeout=60, headers=UA)
        if r.status_code == 404:
            files = {}
        else:
            r.raise_for_status()
            files = {(utc(m.group(2)), int(m.group(3))): m.group(1) for m in STEP_RE.finditer(r.text)}
        self.listings[key] = (now, files)
        return files

    def download_mb_24h(self) -> float:
        cut = time.time() - 86400
        self.bytes_log = [b for b in self.bytes_log if b[0] > cut]
        return round(sum(b[1] for b in self.bytes_log) / 1e6, 1)

    def _fetch(self, src: Source, run: datetime, var: str, step: int, name: str) -> np.ndarray:
        url = src.base.format(hh=run.hour, var=src.vars[var]) + name
        r = self.http.get(url, timeout=180, headers=UA)
        r.raise_for_status()
        self.bytes_log.append((time.time(), len(r.content)))
        info, vals = _grib(bz2.decompress(r.content))
        if src.key == "gl":
            self.regrid["gl"].prepare(run)
        return self.regrid[src.key](info, vals)

    # ------------------------------------------------------- run selection
    def _choose_run(self, src: Source, need_steps_fn) -> RunData | None:
        """Complete run to use. ICON-D2: newest. ICON-EU/ICON: longest reach (then newest)."""
        now = datetime.now(timezone.utc)
        latest = now.replace(minute=0, second=0, microsecond=0)
        latest -= timedelta(hours=latest.hour % src.run_every)
        best = None
        for k in range(0, 8):
            run = latest - timedelta(hours=src.run_every * k)
            steps = need_steps_fn(run)
            if steps is None:
                continue
            ok = True
            for var in ("prec", "cloud"):
                files = self._list(src, run.hour, src.vars[var])
                if any((run, s) not in files for s in steps):
                    ok = False
                    break
            if not ok:
                continue
            reach = run + timedelta(hours=src.max_step(run.hour))
            if src.key == "d2":
                best = run
                break
            if best is None or reach > best[0]:
                best = (reach, run)
        if best is None:
            return None
        run = best if src.key == "d2" else best[1]
        cur = self.runs.get(src.key)
        return cur if cur and cur.run == run else RunData(run)

    def _ensure_steps(self, src: Source, rd: RunData, steps: list[int]) -> None:
        for var in ("prec", "cloud"):
            files = self._list(src, rd.run.hour, src.vars[var])
            for s in steps:
                if (var, s) in rd.fields:
                    continue
                name = files.get((rd.run, s))
                if not name:
                    raise RuntimeError(f"{src.label} {rd.run:%d.%m. %H} UTC: Schritt {s} ({var}) fehlt")
                rd.fields[(var, s)] = self._fetch(src, rd.run, var, s, name)

    # -------------------------------------------------------------- update
    def update(self, past_start: datetime, nowcast_end: datetime, horizon_h: int = 168) -> list[dict]:
        """Refresh all sources; return the model frame plan [{t, src, step, dt}]."""
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=horizon_h)
        first = (nowcast_end + timedelta(minutes=59)).replace(minute=0, second=0, microsecond=0)
        if first <= nowcast_end:
            first += timedelta(hours=1)
        plan: list[dict] = []
        cursor = first        # first frame time not yet covered
        new_runs = {}
        for src in SOURCES:
            if cursor > end:
                break

            def need(run, src=src, cursor=cursor):
                steps_all = src.steps(run.hour)
                last = run + timedelta(hours=src.max_step(run.hour))
                if last < cursor:
                    return None
                lo = min(past_start, cursor) if src.key == "d2" else cursor
                need_steps = set()
                for s in steps_all:
                    t = run + timedelta(hours=s)
                    if t < lo - timedelta(hours=3) or t > end:
                        continue
                    need_steps.add(s)
                # precipitation needs the previous step too
                for s in list(need_steps):
                    i = steps_all.index(s)
                    if i > 0:
                        need_steps.add(steps_all[i - 1])
                return sorted(need_steps)

            try:
                rd = self._choose_run(src, need)
                if rd is None:
                    log.warning("%s: kein passender Lauf verfügbar", src.label)
                    continue
                steps = need(rd.run) or []
                self._ensure_steps(src, rd, steps)
            except Exception as e:
                self.error = f"{src.label}: {type(e).__name__}: {e}"
                log.warning("Modell %s", self.error)
                cur = self.runs.get(src.key)
                if not cur:
                    continue
                rd = cur
            new_runs[src.key] = rd
            steps_all = src.steps(rd.run.hour)
            for i, s in enumerate(steps_all):
                if i == 0:
                    continue
                t = rd.run + timedelta(hours=s)
                if t < cursor or t > end:
                    continue
                if ("prec", s) not in rd.fields or ("prec", steps_all[i - 1]) not in rd.fields:
                    continue
                plan.append({"t": t, "src": src.key, "step": s, "prev": steps_all[i - 1],
                             "run": rd.run, "dt": s - steps_all[i - 1]})
            if plan:
                cursor = plan[-1]["t"] + timedelta(hours=1)
        with self.lock:
            for k, rd in new_runs.items():
                old = self.runs.get(k)
                if k == "d2" and old and old.run != rd.run:
                    # keep the previous run's clouds for past radar frames before the new run starts
                    self.runs["d2prev"] = RunData(old.run, {kk: v for kk, v in old.fields.items() if kk[0] == "cloud"})
                if not old or old.run != rd.run:
                    log.info("%s-Lauf %s aktiv (%d Felder)", next(s.label for s in SOURCES if s.key == k),
                             rd.run.strftime("%d.%m. %H UTC"), len(rd.fields))
                self.runs[k] = rd
            # drop fields that are no longer used (keep memory small)
            for k, rd in list(self.runs.items()):
                if k in ("d2", "d2prev"):
                    continue
                used = {("prec", p["step"]) for p in plan if p["src"] == k} | \
                       {("prec", p["prev"]) for p in plan if p["src"] == k} | \
                       {("cloud", p["step"]) for p in plan if p["src"] == k}
                if k in ("d2", "d2prev"):
                    continue          # d2 clouds are also used for radar frames
                for key in list(rd.fields):
                    if key not in used:
                        del rd.fields[key]
        return plan

    # -------------------------------------------------------------- access
    def rate(self, p: dict) -> np.ndarray:
        rd = self.runs[p["src"]]
        a = rd.fields[("prec", p["step"])] - rd.fields[("prec", p["prev"])]
        return np.maximum(a, 0) / max(1, p["dt"])        # mm/h

    def cloud(self, p: dict) -> np.ndarray | None:
        return self.runs[p["src"]].fields.get(("cloud", p["step"]))

    def cloud_at(self, t: datetime) -> np.ndarray | None:
        """Cloud cover for radar frame times (ICON-D2, blended between hours)."""
        for key in ("d2", "d2prev"):
            rd = self.runs.get(key)
            if not rd:
                continue
            h = (t - rd.run).total_seconds() / 3600
            s0 = math.floor(h)
            w = h - s0
            a, b = rd.fields.get(("cloud", s0)), rd.fields.get(("cloud", s0 + 1))
            if a is None:
                continue
            if b is None or w < 1e-6:
                return a
            return a * (1 - w) + b * w
        return None
