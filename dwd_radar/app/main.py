"""DWD Regenradar – main loop.

Every minute: read the RV directory listing, process new releases, keep the
last N analysis frames (past) plus the forecast of the newest release, write
manifest.json for the web page and report status to Home Assistant.
"""
from __future__ import annotations

import io
import json
import math
import logging
import os
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
from PIL import Image

import dwd
from ha import HA
from hillshade import Hillshade
from mapoverlay import MapOverlay
from landmarks import Landmarks
from models import ModelStore, TargetGrid, SOURCES
import flow
import rawdata
from temps import Temps
from warnings_dwd import Warnings
from render import classify_png
from render import Extent, Renderer, build_lut, LUT_MIN, LUT_MAX, LUT_N, STOPS

OPTIONS = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
WWW = Path(os.environ.get("WWW_DIR", "/homeassistant/www/dwd-radar"))
STATIC = Path(os.environ.get("STATIC_DIR", "/app/www"))
DATA = Path(os.environ.get("DATA_DIR", "/data"))
VERSION = os.environ.get("ADDON_VERSION", "dev")

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dwd_radar")

# Rendered colour PNGs for the former Leaflet page (index.html). The WebGL page (gl.html) only needs the
# raw layer, so these are off (the Leaflet page itself was removed in 0.11; flag kept for debugging only).
LEGACY_PNG = os.environ.get("LEGACY_PNG", "0") == "1"


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def load_options() -> dict:
    o = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    d = {
        "home_name": "Zuhause", "home_lat": 49.567, "home_lon": 8.317,
        "pins": [],
        "area_radius_km": 120,
        "resolution_m": 500, "smoothing_km": 1.0, "radar_opacity": 1.0,
        "zoom": 10, "min_zoom": 7, "max_zoom": 12,
        "past_minutes": 120, "show_forecast": True,
        "frame_ms": 450, "hold_now_ms": 1500, "hold_end_ms": 2500,
        "controls_hide_s": 6, "reset_view_s": 90, "stale_minutes": 20,
        "hillshade": True, "hillshade_strength": 1.0,
        "basemap": "own", "carto_key": "", "basemap_filter": "",
        "show_clouds": True, "cloud_opacity": 1.0, "forecast_days": 7,
        "color_style": "classes", "smooth_motion": True, "theme": "auto", "temperatures": "toggle", "warnings": "toggle",
        "log_level": "info",
    }
    d.update({k: v for k, v in o.items() if v is not None})
    # map/data area: square-ish box around the home location
    r = float(d["area_radius_km"])
    dlat = r / 111.2
    dlon = r / (111.32 * math.cos(math.radians(float(d["home_lat"]))))
    d["extent_lat_min"], d["extent_lat_max"] = round(d["home_lat"] - dlat, 3), round(d["home_lat"] + dlat, 3)
    d["extent_lon_min"], d["extent_lon_max"] = round(d["home_lon"] - dlon * 1.15, 3), round(d["home_lon"] + dlon * 1.15, 3)
    d["forecast_days"] = max(0, min(7, int(d["forecast_days"])))
    d["reset_view_s"] = max(0, min(120, int(d["reset_view_s"])))
    return d


class Radar:
    def __init__(self, opt: dict):
        self.o = opt
        self.ha = HA()
        self.http = requests.Session()
        self.renderer = Renderer(
            Extent(opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"]),
            opt["resolution_m"], opt["smoothing_km"], opt["radar_opacity"], opt.get("color_style", "classes"))
        self.tg = TargetGrid(opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"],
                             opt["extent_lon_max"], 1000.0)
        self.models = ModelStore(self.tg, DATA / "model_cache")
        self.model_frames: list[dict] = []
        self.model_dir = WWW / "frames" / "model"
        self.cloud_dir = WWW / "frames" / "clouds"
        self.lut = build_lut(opt["radar_opacity"])
        self.raw = WWW / "frames" / "raw"
        self.model_frames = self.model_frames_from_disk() if LEGACY_PNG else []
        if not LEGACY_PNG:
            for old in ("obs", "fc", "model", "clouds"):
                shutil.rmtree(WWW / "frames" / old, ignore_errors=True)
            (WWW / "manifest.json").unlink(missing_ok=True)
        self.model_frames2, self.model_clouds2 = self.model_frames2_from_disk()
        self.obs_dir = WWW / "frames" / "obs"
        self.fc_dir = WWW / "frames" / "fc"
        self.state_file = DATA / "state.json"
        st = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.last_run = datetime.fromisoformat(st["last_run"]) if st.get("last_run") else None
        self.failed_backfill: dict[str, int] = {}
        self.obs_cache: dict[datetime, np.ndarray] = {}     # target-grid rates of analysis frames
        self.last_error: str | None = None
        self.last_success: datetime | None = None
        self.latency_s: float | None = None
        # a geometry change (extent/resolution) invalidates rendered frames
        geo = json.dumps([self.renderer.bounds, opt["resolution_m"], opt["smoothing_km"], opt["radar_opacity"],
                          opt.get("color_style", "classes"), bool(opt.get("smooth_motion", True)), "r4"])
        if st.get("geometry") != geo:
            log.info("Ausschnitt/Darstellung geändert – vorhandene Bilder werden neu erzeugt")
            shutil.rmtree(WWW / "frames", ignore_errors=True)
            self.last_run = None
        self.geometry = geo

    # ------------------------------------------------------------------ files
    def obs_path(self, t: datetime) -> Path:
        return self.obs_dir / f"o_{t:%Y%m%d%H%M}.png"

    def fc_path(self, run: datetime, lead: int) -> Path:
        return self.fc_dir / f"{run:%Y%m%d%H%M}" / f"f_{lead:03d}.png"

    def existing_obs(self) -> dict[datetime, Path]:
        out = {}
        for p in (self.raw / "obs").glob("r_*.png"):
            try:
                out[datetime.strptime(p.stem[2:], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)] = p
            except ValueError:
                pass
        return out

    def save_state(self) -> None:
        atomic_write(self.state_file, json.dumps({
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "geometry": self.geometry}).encode())

    # ------------------------------------------------------------- processing
    def process_run(self, run: datetime, with_forecast: bool) -> None:
        t0 = time.time()
        blob = dwd.download_run(self.http, run)
        leads = None if with_forecast else {0}
        grid, frames = dwd.parse_run(blob, run, leads)
        n = 0
        rates: dict[int, np.ndarray] = {}
        for fr in frames:
            r = self.renderer.rate_on_target(grid, fr.rate)
            rates[fr.lead] = r
            if fr.lead == 0:
                self.obs_cache[fr.valid] = r.astype(np.float16)
            if not LEGACY_PNG:
                continue
            path = self.obs_path(fr.valid) if fr.lead == 0 else self.fc_path(run, fr.lead)
            if fr.lead == 0 and path.exists():
                continue
            atomic_write(path, self.renderer.png_from_target(r))
        # raw layer (WebGL page): encoded fields + motion fields
        rdir = self.raw / "fc" / f"{run:%Y%m%d%H%M}"
        for fr in frames:
            if fr.lead == 0:
                rp = self.raw / "obs" / f"r_{fr.valid:%Y%m%d%H%M}.png"
                if not rp.exists():
                    atomic_write(rp, rawdata.rain_png(rates[0]))
                    n += 1
            else:
                atomic_write(rdir / f"r_{fr.lead:03d}.png", rawdata.rain_png(rates[fr.lead]))
                n += 1
        nfl = 0
        if with_forecast:
            ls = sorted(rates)
            fls = {}
            for a, b in zip(ls, ls[1:]):
                png, sc = rawdata.flow_png(rates[a], rates[b])
                atomic_write(rdir / f"fl_{a:03d}.png", png)
                fls[f"{a:03d}"] = sc
                nfl += 1
            atomic_write(rdir / "flows.json", json.dumps(fls).encode())
        if 0 in rates:
            nfl += self.obs_flows(run)
        ni = 0
        if LEGACY_PNG and self.o.get("smooth_motion", True):
            if with_forecast:
                ls = sorted(rates)
                for a, b in zip(ls, ls[1:]):
                    steps = b - a
                    if steps < 2:
                        continue
                    targets = [self.fc_dir / f"{run:%Y%m%d%H%M}" / f"i_{a:03d}_{k:02d}.png" for k in range(1, steps)]
                    if all(p.exists() for p in targets):
                        continue
                    mids = flow.intermediates(rates[a], rates[b], [k / steps for k in range(1, steps)])
                    for p, m in zip(targets, mids):
                        atomic_write(p, self.renderer.png_from_target(m))
                        ni += 1
            if 0 in rates:
                ni += self.obs_intermediates(run)
        self.prune_obs_cache()
        log.info("Lauf %s: %d MB, %d Bilder, %d Bewegungsfelder%s in %.1f s", run.strftime("%H:%M"),
                 len(blob) // 1_000_000, n, nfl, f", {ni} Zwischenbilder" if ni else "", time.time() - t0)
        if with_forecast:
            leads_found = sorted(f.lead for f in frames)
            if leads_found and leads_found[-1] < 120:
                log.warning("Lauf %s enthält nur Leads bis %d min", run.strftime("%H:%M"), leads_found[-1])

    def obs_intermediates(self, t: datetime) -> int:
        """Motion-interpolated 1-minute frames between t and its 5-minute neighbours (if known)."""
        n = 0
        for a, b in ((t - timedelta(minutes=5), t), (t, t + timedelta(minutes=5))):
            if a not in self.obs_cache or b not in self.obs_cache:
                continue
            targets = [self.obs_dir / f"oi_{a + timedelta(minutes=k):%Y%m%d%H%M}.png" for k in range(1, 5)]
            if all(p.exists() for p in targets):
                continue
            mids = flow.intermediates(self.obs_cache[a].astype(np.float32), self.obs_cache[b].astype(np.float32),
                                      [k / 5 for k in range(1, 5)])
            for p, m in zip(targets, mids):
                atomic_write(p, self.renderer.png_from_target(m))
                n += 1
        return n

    def prune_obs_cache(self) -> None:
        cut = datetime.now(timezone.utc) - timedelta(minutes=self.o["past_minutes"] + 15)
        for k in [k for k in self.obs_cache if k < cut]:
            del self.obs_cache[k]

    @staticmethod
    def _read_json(p: Path) -> dict:
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def obs_flows(self, t: datetime) -> int:
        """Motion fields between analysis frame t and its 5-minute neighbours (raw layer)."""
        ro = self.raw / "obs"
        fls = self._read_json(ro / "flows.json")
        n = 0
        for a, b in ((t - timedelta(minutes=5), t), (t, t + timedelta(minutes=5))):
            key = f"{a:%Y%m%d%H%M}"
            if a not in self.obs_cache or b not in self.obs_cache or key in fls:
                continue
            png, sc = rawdata.flow_png(self.obs_cache[a], self.obs_cache[b])
            atomic_write(ro / f"fl_{key}.png", png)
            fls[key] = sc
            n += 1
        if n:
            atomic_write(ro / "flows.json", json.dumps(fls).encode())
        return n

    def cycle(self) -> None:
        runs = dwd.list_runs(self.http)
        newest, lm = runs[-1]
        now = datetime.now(timezone.utc)
        if self.last_run is None or newest > self.last_run:
            if lm:
                self.latency_s = (lm - newest).total_seconds()
            self.process_run(newest, self.o["show_forecast"])
            self.last_run = newest
            self.save_state()
        # backfill missing analysis frames (past window), newest first, a few per cycle
        have = set(self.existing_obs())
        window_start = now - timedelta(minutes=self.o["past_minutes"])
        avail = {t for t, _ in runs}
        missing = [t for t in sorted(avail, reverse=True)
                   if t >= window_start and t not in have and self.failed_backfill.get(iso(t), 0) < 3]
        for t in missing[:6]:
            try:
                self.process_run(t, with_forecast=False)
            except Exception as e:  # keep going with others
                self.failed_backfill[iso(t)] = self.failed_backfill.get(iso(t), 0) + 1
                log.warning("Nachladen %s fehlgeschlagen: %s", t.strftime("%H:%M"), e)
        self.last_success = now
        self.last_error = None
        self.ha.clear_error()

    # ----------------------------------------------------------------- model layer
    def cloud_png(self, cc: np.ndarray) -> bytes:
        c = np.clip(cc, 0, 100) / 100.0
        alpha = np.clip((c - 0.10) / 0.90, 0, 1) ** 1.05 * 0.80 * float(self.o.get("cloud_opacity", 1.0))
        col = np.stack([252 - 50 * c, 252 - 46 * c, 253 - 40 * c], axis=-1)
        rgba = np.dstack([col, alpha * 255]).round().astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
        return buf.getvalue()

    SMOOTH_PX = {"d2": 1.0, "eu": 2.5, "gl": 4.0}     # model native resolution on the 1 km grid

    def model_rate(self, p: dict) -> np.ndarray:
        from scipy import ndimage
        r = self.models.rate(p)
        sg = self.SMOOTH_PX.get(p["src"], 1.0)
        return ndimage.gaussian_filter(r, sg, mode="nearest") if sg > 0 else r

    def precip_png(self, rate: np.ndarray) -> bytes:
        if self.o.get("color_style", "classes") == "classes":
            return classify_png(rate, self.o["radar_opacity"])
        rr = np.nan_to_num(rate, nan=0.0)
        idx = (np.log10(np.maximum(rr, 1e-6)) - LUT_MIN) / (LUT_MAX - LUT_MIN) * (LUT_N - 1)
        rgba = self.lut[np.clip(idx, 0, LUT_N - 1).astype(np.int32)]
        rgba[rr < STOPS[0][0]] = 0
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def model_frames_from_disk(self) -> list[dict]:
        """Model frames already rendered (used right after a restart until the first model cycle is done)."""
        now = datetime.now(timezone.utc)
        best: dict[datetime, dict] = {}
        order = {"d2": 0, "eu": 1, "gl": 2}
        if not self.model_dir.exists():
            return []
        for d in self.model_dir.iterdir():
            try:
                src, run_s = d.name.split("_")
                run = datetime.strptime(run_s, "%Y%m%d%H").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            for p in d.glob("p_*.png"):
                t = datetime.strptime(p.stem[2:], "%Y%m%d%H").replace(tzinfo=timezone.utc)
                if t <= now:
                    continue
                cand = {"t": iso(t), "k": "m", "u": f"frames/model/{d.name}/{p.name}", "s": src, "_r": run}
                c = d / f"c_{p.stem[2:]}.png"
                if c.exists():
                    cand["c"] = f"frames/model/{d.name}/{c.name}"
                old = best.get(t)
                # prefer the finer source, then the newer run
                if old is None or (order.get(src, 9), -run.timestamp()) < (order.get(old["s"], 9), -old["_r"].timestamp()):
                    best[t] = cand
        # interpolated frames, only where both enclosing real frames come from the same run directory
        real_ts = sorted(best)
        import bisect
        chosen = {f["u"].split("/")[2]: f["s"] for f in best.values()}
        extra = {}
        for dname, src in chosen.items():
            for p in (self.model_dir / dname).glob("pi_*.png"):
                try:
                    t = datetime.strptime(p.stem[3:], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                i = bisect.bisect_left(real_ts, t)
                if t <= now or i == 0 or i >= len(real_ts) or real_ts[i] == t:
                    continue
                a, b = best[real_ts[i - 1]], best[real_ts[i]]
                if a["u"].split("/")[2] == dname and b["u"].split("/")[2] == dname:
                    extra[t] = {"t": iso(t), "k": "m", "u": f"frames/model/{dname}/{p.name}", "s": src, "x": 1,
                                "_r": a["_r"]}
        best.update(extra)
        frames = [best[t] for t in sorted(best)]
        ts = [datetime.strptime(f["t"], "%Y-%m-%dT%H:%M:%SZ") for f in frames]
        for i, f in enumerate(frames):
            # step length = distance to a neighbour of the same source (1 h or 3 h)
            dt = 1
            real = [j for j in range(max(0, i - 8), min(len(frames), i + 9))
                    if j != i and frames[j]["s"] == f["s"] and not frames[j].get("x") ]
            if len(real) >= 1:
                j = min(real, key=lambda j: abs(j - i))
                if not f.get("x"):
                    dt = int(round(abs((ts[i] - ts[j]).total_seconds()) / 3600)) or 1
                else:
                    nxt = [j for j in real if j > i]
                    if nxt:
                        prv = [j for j in real if j < i]
                        if prv:
                            dt = int(round((ts[nxt[0]] - ts[prv[-1]]).total_seconds() / 3600)) or 1
            f["d"] = dt
        for f in frames:
            f.pop("_r", None)
        if frames:
            log.info("Modell-Zeitleiste aus vorhandenen Bildern: %d Bilder bis %s", len(frames), frames[-1]["t"])
        return frames

    def model_frames2_from_disk(self) -> tuple[list[dict], list[dict]]:
        """Raw model frames already on disk (restart until the first model cycle is done)."""
        now = datetime.now(timezone.utc)
        root = self.raw / "model"
        order = {"d2": 0, "eu": 1, "gl": 2}
        best: dict[datetime, dict] = {}
        flows: dict[str, dict] = {}
        if not root.exists():
            return [], []
        for d in root.iterdir():
            try:
                src, run_s = d.name.split("_")
                run = datetime.strptime(run_s, "%Y%m%d%H").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            flows[d.name] = self._read_json(d / "flows.json")
            for p in d.glob("r_*.png"):
                t = datetime.strptime(p.stem[2:], "%Y%m%d%H").replace(tzinfo=timezone.utc)
                if t <= now:
                    continue
                old = best.get(t)
                if old is None or (order.get(src, 9), -run.timestamp()) < (order.get(old["s"], 9), -old["_r"].timestamp()):
                    best[t] = {"t": iso(t), "k": "m", "g": "m", "u": f"frames/raw/model/{d.name}/{p.name}", "s": src,
                               "_r": run, "_d": d.name, "_t": t}
        frames = [best[t] for t in sorted(best)]
        clouds = []
        for i, f in enumerate(frames):
            same = [j for j in (i - 1, i + 1) if 0 <= j < len(frames) and frames[j]["s"] == f["s"]]
            f["d"] = int(round(min(abs((frames[j]["_t"] - f["_t"]).total_seconds()) for j in same) / 3600)) if same else 1
            key = f"{f['_t']:%Y%m%d%H}"
            if i + 1 < len(frames) and frames[i + 1]["_d"] == f["_d"] and key in flows[f["_d"]] \
                    and (root / f["_d"] / f"fl_{key}.png").exists():
                f["fl"] = f"frames/raw/model/{f['_d']}/fl_{key}.png"
                f["fs"] = flows[f["_d"]][key]
            if (root / f["_d"] / f"c_{key}.png").exists():
                clouds.append({"t": f["t"], "u": f"frames/raw/model/{f['_d']}/c_{key}.png"})
        for f in frames:
            for k in ("_r", "_d", "_t"):
                f.pop(k, None)
        return frames, clouds

    def model_cycle(self) -> None:
        """Runs in its own thread: refresh ICON sources and render model frames."""
        now = datetime.now(timezone.utc)
        past_start = now - timedelta(minutes=self.o["past_minutes"] + 5)
        nowcast_end = (self.last_run or dwd.floor5(now)) + timedelta(minutes=120)
        plan = self.models.update(past_start, nowcast_end, int(self.o["forecast_days"]) * 24)
        frames, keep = [], set()
        frames2, clouds2, flows_by_dir = [], [], {}
        n_new = 0
        prev, prev_rate = None, None
        for p in plan:
            dname = f"{p['src']}_{p['run']:%Y%m%d%H}"
            rd = self.raw / "model" / dname
            if dname not in flows_by_dir:
                flows_by_dir[dname] = [self._read_json(rd / "flows.json"), False]
            fls = flows_by_dir[dname][0]
            tk = f"{p['t']:%Y%m%d%H}"
            rp, rc = rd / f"r_{tk}.png", rd / f"c_{tk}.png"
            rate_raw = None
            if not rp.exists():
                rate_raw = self.model_rate(p)
                atomic_write(rp, rawdata.rain_png(rate_raw))
                n_new += 1
            f2 = {"t": iso(p["t"]), "k": "m", "g": "m", "u": f"frames/raw/model/{dname}/{rp.name}", "s": p["src"],
                  "d": p["dt"]}
            if prev is not None and prev["src"] == p["src"] and prev["run"] == p["run"] and frames2:
                pk = f"{prev['t']:%Y%m%d%H}"
                if pk not in fls or not (rd / f"fl_{pk}.png").exists():
                    ra = self.model_rate(prev)
                    rb = rate_raw if rate_raw is not None else self.model_rate(p)
                    png, sc = rawdata.flow_png(ra, rb)
                    atomic_write(rd / f"fl_{pk}.png", png)
                    fls[pk] = sc
                    flows_by_dir[dname][1] = True
                frames2[-1]["fl"] = f"frames/raw/model/{dname}/fl_{pk}.png"
                frames2[-1]["fs"] = fls[pk]
            frames2.append(f2)
            if self.o.get("show_clouds"):
                cc = self.models.cloud(p)
                if cc is not None:
                    if not rc.exists():
                        atomic_write(rc, rawdata.cloud_png(cc))
                    clouds2.append({"t": iso(p["t"]), "u": f"frames/raw/model/{dname}/{rc.name}"})
            d = self.model_dir / f"{p['src']}_{p['run']:%Y%m%d%H}"
            pp, pc = d / f"p_{p['t']:%Y%m%d%H}.png", d / f"c_{p['t']:%Y%m%d%H}.png"
            rate = None
            if LEGACY_PNG and not pp.exists():
                rate = self.model_rate(p)
                atomic_write(pp, self.precip_png(rate))
                n_new += 1
            # motion-interpolated frames between this and the previous step of the same source
            if LEGACY_PNG and self.o.get("smooth_motion", True) and prev is not None and prev["src"] == p["src"] \
                    and prev["run"] == p["run"]:
                nsub = 6
                span = p["t"] - prev["t"]
                ts = [prev["t"] + span * k / nsub for k in range(1, nsub)]
                targets = [d / f"pi_{t:%Y%m%d%H%M}.png" for t in ts]
                if not all(x.exists() for x in targets):
                    if rate is None:
                        rate = self.model_rate(p)
                    if prev_rate is None:
                        prev_rate = self.model_rate(prev)
                    mids = flow.intermediates(prev_rate, rate, [k / nsub for k in range(1, nsub)])
                    for x, m in zip(targets, mids):
                        atomic_write(x, self.precip_png(m))
                        n_new += 1
                for t, x in zip(ts, targets):
                    frames.append({"t": iso(t), "k": "m", "u": f"frames/model/{d.name}/{x.name}", "s": p["src"],
                                   "d": p["dt"], "x": 1})
            f = {"t": iso(p["t"]), "k": "m", "u": f"frames/model/{d.name}/{pp.name}", "s": p["src"], "d": p["dt"]}
            if LEGACY_PNG and self.o.get("show_clouds"):
                cc = self.models.cloud(p)
                if cc is not None:
                    if not pc.exists():
                        atomic_write(pc, self.cloud_png(cc))
                    f["c"] = f"frames/model/{d.name}/{pc.name}"
            if LEGACY_PNG:
                frames.append(f)
            keep.add(d.name)
            prev, prev_rate = p, rate
        for dname, (fls, changed) in flows_by_dir.items():
            if changed:
                atomic_write(self.raw / "model" / dname / "flows.json", json.dumps(fls).encode())
        self.model_frames = frames
        self.model_frames2, self.model_clouds2 = frames2, clouds2
        if n_new:
            log.info("Modellbilder: %d neu, %d gesamt bis %s (Download 24 h: %.0f MB)", n_new, len(frames2),
                     frames2[-1]["t"] if frames2 else "-", self.models.download_mb_24h())
        cutoff = time.time() - 900
        if self.model_dir.exists():
            for d in self.model_dir.iterdir():
                if d.is_dir() and d.name not in keep and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        if (self.raw / "model").exists():
            for d in (self.raw / "model").iterdir():
                if d.is_dir() and d.name not in keep and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)

    def attach_clouds(self, frames: list[dict]):
        if not self.o.get("show_clouds") or not frames:
            return
        rd = self.models.runs.get("d2")
        tag = rd.run.strftime("%Y%m%d%H") if rd else "x"
        keep = set()
        for f in frames:
            if f["k"] == "m" or f.get("x"):
                continue
            t = datetime.strptime(f["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            p = self.cloud_dir / f"c_{t:%Y%m%d%H%M}_{tag}.png"
            if not p.exists():
                cc = self.models.cloud_at(t)
                if cc is None:
                    continue
                atomic_write(p, self.cloud_png(cc))
            keep.add(p.name)
            f["c"] = f"frames/clouds/{p.name}"
        cutoff = time.time() - 900
        if self.cloud_dir.exists():
            for p in self.cloud_dir.glob("c_*.png"):
                if p.name not in keep and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)

    # ------------------------------------------------------- raw layer manifest
    @staticmethod
    def _tparse(stem: str, fmt: str) -> datetime | None:
        try:
            return datetime.strptime(stem, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def raw_clouds(self, frames: list[dict]) -> list[dict]:
        if not self.o.get("show_clouds") or not frames:
            return []
        cdir = self.raw / "clouds"
        rd = self.models.runs.get("d2")
        tag = rd.run.strftime("%Y%m%d%H") if rd else "x"
        rts = [datetime.strptime(f["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
               for f in frames if f["k"] != "m"]
        out, keep = [], set()
        if rts:
            h = min(rts).replace(minute=0, second=0, microsecond=0)
            h1 = max(rts)
            while h <= h1 + timedelta(hours=1):
                p = cdir / f"c_{h:%Y%m%d%H}_{tag}.png"
                if not p.exists():
                    cc = self.models.cloud_at(h)
                    if cc is not None:
                        atomic_write(p, rawdata.cloud_png(cc))
                if p.exists():
                    keep.add(p.name)
                    out.append({"t": iso(h), "u": f"frames/raw/clouds/{p.name}"})
                h += timedelta(hours=1)
        last = out[-1]["t"] if out else ""
        out += [c for c in self.model_clouds2 if c["t"] > last]
        cutoff = time.time() - 900
        if cdir.exists():
            for p in cdir.glob("c_*.png"):
                if p.name not in keep and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        return out

    def write_manifest2(self, now, keep_from, newest_obs, fc_run, stale) -> None:
        ro = self.raw / "obs"
        obs = {}
        for p in ro.glob("r_*.png") if ro.exists() else []:
            t = self._tparse(p.stem[2:], "%Y%m%d%H%M")
            if t is None:
                continue
            if t < keep_from:
                p.unlink(missing_ok=True)
            else:
                obs[t] = p
        for p in ro.glob("fl_*.png") if ro.exists() else []:
            t = self._tparse(p.stem[3:], "%Y%m%d%H%M")
            if t is not None and t < keep_from:
                p.unlink(missing_ok=True)
        fls = self._read_json(ro / "flows.json")
        pruned = {k: v for k, v in fls.items() if (self._tparse(k, "%Y%m%d%H%M") or now) >= keep_from}
        if len(pruned) != len(fls):
            atomic_write(ro / "flows.json", json.dumps(pruned).encode())
        frames = []
        ts = sorted(obs)
        for i, t in enumerate(ts):
            f = {"t": iso(t), "k": "o", "g": "r", "u": f"frames/raw/obs/{obs[t].name}"}
            key = f"{t:%Y%m%d%H%M}"
            if i + 1 < len(ts) and ts[i + 1] - t == timedelta(minutes=5) and key in pruned \
                    and (ro / f"fl_{key}.png").exists():
                f["fl"] = f"frames/raw/obs/fl_{key}.png"
                f["fs"] = pruned[key]
            frames.append(f)
        fc_root = self.raw / "fc"
        runs = sorted(p for p in fc_root.iterdir() if p.is_dir()) if fc_root.exists() else []
        for p in runs[:-2]:
            shutil.rmtree(p, ignore_errors=True)
        if fc_run:
            d = fc_root / f"{fc_run:%Y%m%d%H%M}"
            if d.is_dir():
                ffl = self._read_json(d / "flows.json")
                leads = sorted(int(p.stem[2:]) for p in d.glob("r_*.png"))
                if frames and frames[-1]["t"] == iso(fc_run) and leads and leads[0] == 5 and "000" in ffl:
                    frames[-1]["fl"] = f"frames/raw/fc/{d.name}/fl_000.png"
                    frames[-1]["fs"] = ffl["000"]
                for j, L in enumerate(leads):
                    f = {"t": iso(fc_run + timedelta(minutes=L)), "k": "f", "g": "r",
                         "u": f"frames/raw/fc/{d.name}/r_{L:03d}.png"}
                    key = f"{L:03d}"
                    if j + 1 < len(leads) and leads[j + 1] - L == 5 and key in ffl:
                        f["fl"] = f"frames/raw/fc/{d.name}/fl_{key}.png"
                        f["fs"] = ffl[key]
                    frames.append(f)
        last_t = frames[-1]["t"] if frames else ""
        frames += [f for f in self.model_frames2 if f["t"] > last_t]
        clouds = self.raw_clouds(frames)
        r, tg = self.renderer, self.tg
        m2 = {
            "generated": iso(now),
            "grids": {"r": {"b": r.bounds, "w": r.nx, "h": r.ny}, "m": {"b": tg.bounds, "w": tg.nx, "h": tg.ny}},
            "sources": {s.key: s.label for s in SOURCES},
            "model_error": self.models.error,
            "now": iso(newest_obs) if newest_obs else None,
            "run": iso(fc_run) if fc_run else None,
            "stale": stale,
            "error": self.last_error,
            "frames": frames,
            "clouds": clouds,
        }
        atomic_write(WWW / "manifest2.json", json.dumps(m2, separators=(",", ":")).encode())
        return len(frames)

    # --------------------------------------------------------------- manifest
    def cleanup_and_manifest(self) -> None:
        now = datetime.now(timezone.utc)
        obs = self.existing_obs()
        # keep the past window; if data stopped, keep the newest frames anyway
        keep_from = now - timedelta(minutes=self.o["past_minutes"] + 2)
        newest_obs = max(obs) if obs else None
        if newest_obs and newest_obs < keep_from + timedelta(minutes=30):
            keep_from = newest_obs - timedelta(minutes=self.o["past_minutes"])
        for t, p in list(obs.items()):
            if t < keep_from:
                p.unlink(missing_ok=True)
                del obs[t]
        inter = {}
        for p in self.obs_dir.glob("oi_*.png") if self.obs_dir.exists() else []:
            try:
                t = datetime.strptime(p.stem[3:], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if t < keep_from:
                p.unlink(missing_ok=True)
            elif newest_obs and t < newest_obs:
                inter[t] = p
        # forecasts: current run + previous one (clients may still load it)
        runs_fc = sorted(p for p in self.fc_dir.glob("*") if p.is_dir()) if self.fc_dir.exists() else []
        for p in runs_fc[:-2]:
            shutil.rmtree(p, ignore_errors=True)
        allobs = [(t, p, False) for t, p in obs.items()] + [(t, p, True) for t, p in inter.items()]
        frames = []
        for t, p, x in sorted(allobs, key=lambda e: e[0]):
            f = {"t": iso(t), "k": "o", "u": f"frames/obs/{p.name}"}
            if x:
                f["x"] = 1
            frames.append(f)
        fc_run = None
        if self.o["show_forecast"] and self.last_run:
            d = self.fc_dir / f"{self.last_run:%Y%m%d%H%M}"
            if d.is_dir():
                fc_run = self.last_run
                fc = []
                for p in d.glob("f_*.png"):
                    fc.append((self.last_run + timedelta(minutes=int(p.stem[2:])), p, False))
                for p in d.glob("i_*.png"):
                    _, lead, k = p.stem.split("_")
                    fc.append((self.last_run + timedelta(minutes=int(lead) + int(k)), p, True))
                for t, p, x in sorted(fc, key=lambda e: e[0]):
                    f = {"t": iso(t), "k": "f", "u": f"frames/fc/{d.name}/{p.name}"}
                    if x:
                        f["x"] = 1
                    frames.append(f)
        # the raw layer has its own nowcast directory (the legacy PNG one no longer exists)
        if fc_run is None and self.o["show_forecast"] and self.last_run \
                and (self.raw / "fc" / f"{self.last_run:%Y%m%d%H%M}").is_dir():
            fc_run = self.last_run
        last_t = frames[-1]["t"] if frames else ""
        frames += [f for f in self.model_frames if f["t"] > last_t]
        if LEGACY_PNG:
            self.attach_clouds(frames)
        age_min = (now - newest_obs).total_seconds() / 60 if newest_obs else None
        stale = age_min is None or age_min > self.o["stale_minutes"]
        manifest = {
            "generated": iso(now),
            "bounds": self.renderer.bounds,
            "mbounds": self.tg.bounds,
            "sources": {s.key: s.label for s in SOURCES},
            "model_error": self.models.error,
            "now": iso(newest_obs) if newest_obs else None,
            "run": iso(fc_run) if fc_run else None,
            "stale": stale,
            "error": self.last_error,
            "frames": frames,
        }
        if LEGACY_PNG:
            atomic_write(WWW / "manifest.json", json.dumps(manifest, separators=(",", ":")).encode())
        n2 = 0
        try:
            n2 = self.write_manifest2(now, keep_from, newest_obs, fc_run, stale)
        except Exception as e:
            log.error("manifest2 fehlgeschlagen: %s", e)
            log.debug(traceback.format_exc())
        state = "error" if self.last_error and stale else ("stale" if stale else "ok")
        if stale and not self.last_error and newest_obs:
            self.ha.log("warning", f"Keine neuen DWD-Radardaten seit {newest_obs:%H:%M} UTC – zeige letzten Stand")
        self.ha.status(state,
                       latest_frame=iso(newest_obs) if newest_obs else None,
                       data_age_min=round(age_min, 1) if age_min is not None else None,
                       forecast_run=iso(fc_run) if fc_run else None,
                       frames=n2 if not LEGACY_PNG else len(frames),
                       dwd_latency_s=self.latency_s,
                       model_runs={k: iso(v.run) for k, v in self.models.runs.items() if k != "d2prev"},
                       model_frames=len(self.model_frames2),
                       model_download_mb_24h=self.models.download_mb_24h(),
                       model_error=self.models.error,
                       last_success=iso(self.last_success) if self.last_success else None,
                       last_error=self.last_error)


BASEMAPS = {
    "own": {
        "url": None, "labels": "overlay/{z}/{x}/{y}.png", "own": True,
        "attribution": "© OpenStreetMap · Natural Earth · GeoNames", "maxNativeZoom": 12, "filter": "",
    },
    "basemapde": {
        "url": "https://sgx.geodatenzentrum.de/wmts_basemapde/tile/1.0.0/de_basemapde_web_raster_grau/default/GLOBAL_WEBMERCATOR/{z}/{y}/{x}.png",
        "attribution": "© GeoBasis-DE / BKG (2026) CC BY 4.0",
        "maxNativeZoom": 18, "labels": None, "filter": "contrast(0.55) brightness(1.12) saturate(0.6)",
    },
    "carto": {
        "url": "https://{s}.basemaps.cartocdn.com/rastertiles/light_nolabels/{z}/{x}/{y}{r}.png?key={key}",
        "labels": "https://{s}.basemaps.cartocdn.com/rastertiles/light_only_labels/{z}/{x}/{y}{r}.png?key={key}",
        "attribution": "© OpenStreetMap · © CARTO", "maxNativeZoom": 19, "subdomains": "abcd", "filter": "",
    },
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap", "maxNativeZoom": 19, "labels": None,
        "filter": "grayscale(0.9) brightness(1.06) contrast(0.88)",
    },
}


def basemap_cfg(opt: dict) -> dict:
    name = opt.get("basemap", "own")
    if name == "carto" and not opt.get("carto_key"):
        log.warning("basemap=carto ohne carto_key – CARTO liefert ohne Key keine Kacheln; nutze basemap.de")
        name = "basemapde"
    b = dict(BASEMAPS.get(name, BASEMAPS["basemapde"]))
    key = str(opt.get("carto_key", ""))
    if b.get("url"):
        b["url"] = b["url"].replace("{key}", key)
    if b.get("labels"):
        b["labels"] = b["labels"].replace("{key}", key)
    if b.get("own"):
        b["labels"] += f"?v={VERSION}"
        b["base"] = f"base/{{z}}/{{x}}/{{y}}.png?v={VERSION}"
        b["labelsDark"] = b["labels"].replace("overlay/", "overlay_dark/")
        b["baseDark"] = b["base"].replace("base/", "base_dark/")
        b["maxNativeZoom"] = min(12, int(opt["max_zoom"]))
    else:
        b["filterDark"] = "invert(1) hue-rotate(180deg) brightness(0.85) contrast(0.9)"
    if opt.get("basemap_filter"):
        b["filter"] = str(opt["basemap_filter"])
    b["name"] = name
    return b


def pins_of(opt: dict) -> list[dict]:
    pins = [{"name": opt["home_name"], "lat": opt["home_lat"], "lon": opt["home_lon"], "home": True}]
    for p in opt.get("pins") or []:
        try:
            pins.append({"name": str(p.get("name", "")), "lat": float(p["lat"]), "lon": float(p["lon"]),
                         "home": bool(p.get("home", False))})
        except (KeyError, TypeError, ValueError):
            log.warning("Ungültiger Pin ignoriert: %r", p)
    return pins


def install_web(opt: dict, bounds) -> None:
    """Copy the static page and write config.json (read by the page)."""
    WWW.mkdir(parents=True, exist_ok=True)
    for src in STATIC.rglob("*"):
        if src.is_file():
            dst = WWW / src.relative_to(STATIC)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.name == "index.html":
                dst.write_text(src.read_text().replace("__V__", VERSION))
            else:
                shutil.copy2(src, dst)
    pins = pins_of(opt)
    cfg = {
        "version": VERSION,
        "center": [opt["home_lat"], opt["home_lon"]],
        "zoom": opt["zoom"], "minZoom": opt["min_zoom"], "maxZoom": opt["max_zoom"],
        "bounds": bounds,
        "pins": pins,
        "frameMs": opt["frame_ms"], "holdNowMs": opt["hold_now_ms"], "holdEndMs": opt["hold_end_ms"],
        "controlsHideS": opt["controls_hide_s"], "resetViewS": opt["reset_view_s"],
        "forecastDays": opt["forecast_days"],
        "theme": opt.get("theme", "auto"),
        "temps": opt.get("temperatures", "toggle"),
        "warnings": opt.get("warnings", "toggle"),
        "hillshade": bool(opt["hillshade"]),
        "cloudOpacity": float(opt.get("cloud_opacity", 1.0)),
        "basemap": basemap_cfg(opt),
    }
    atomic_write(WWW / "config.json", json.dumps(cfg, indent=1).encode())


def background_jobs(opt: dict) -> list:
    b = (opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"])
    pad_lat = (b[1] - b[0]) * 0.75; pad_lon = (b[3] - b[2]) * 0.75
    wide = (b[0] - pad_lat, b[1] + pad_lat, b[2] - pad_lon, b[3] + pad_lon)
    zmax = min(12, int(opt["max_zoom"]))
    zmin = max(5, int(opt["min_zoom"]) - 1)
    if zmax >= 10:
        return [(wide, zmin, min(zmax, 9)), (b, 10, zmax)]
    return [(wide, zmin, zmax)]


def start_background(opt: dict) -> None:
    """One-off map assets: own border/place overlay first, then hillshade."""
    jobs = background_jobs(opt)

    def run():
        if opt.get("basemap", "own") == "own":
            ext = (opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"])
            variants = [False] if opt.get("theme", "auto") == "light" else [False, True]
            if opt.get("theme") == "dark":
                variants = [True, False]
            for dark in variants:
                sfx = "_dark" if dark else ""
                try:
                    MapOverlay(WWW / f"overlay{sfx}", DATA / "geo_cache", jobs[0][0], pins_of(opt), dark).ensure(jobs)
                except Exception as e:
                    log.error("Karten-Overlay%s fehlgeschlagen: %s", " (dunkel)" if dark else "", e)
                try:
                    Landmarks(WWW / f"base{sfx}", DATA / "geo_cache", jobs[0][0], ext, dark).ensure(jobs)
                except Exception as e:
                    log.error("Gewässer/Autobahn-Kacheln%s fehlgeschlagen: %s", " (dunkel)" if dark else "", e)
        if opt["hillshade"]:
            try:
                Hillshade(WWW / "hillshade", DATA / "dem_cache", opt["hillshade_strength"]).ensure(jobs)
            except Exception as e:
                log.error("Relief-Kacheln fehlgeschlagen (Karte funktioniert trotzdem): %s", e)
    threading.Thread(target=run, daemon=True, name="mapassets").start()


def main() -> None:
    opt = load_options()
    logging.getLogger().setLevel(getattr(logging, str(opt["log_level"]).upper(), logging.INFO))
    log.info("DWD Regenradar %s startet – Ausgabe nach %s", VERSION, WWW)
    radar = Radar(opt)
    install_web(opt, radar.renderer.bounds)
    log.info("Overlay %dx%d px, Bounds %s", radar.renderer.nx, radar.renderer.ny, radar.renderer.bounds)
    start_background(opt)

    if opt.get("temperatures", "toggle") != "off":
        def temps_loop():
            time.sleep(20)
            t = Temps(requests.Session(), DATA / "temps", WWW / "temps.json",
                      (opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"]),
                      (opt["home_lat"], opt["home_lon"]), max(24, int(opt["forecast_days"]) * 24))
            while True:
                try:
                    t.cycle()
                except Exception as e:
                    log.error("Temperaturen fehlgeschlagen: %s", e)
                    log.debug(traceback.format_exc())
                time.sleep(900)
        threading.Thread(target=temps_loop, daemon=True, name="temps").start()
    else:
        (WWW / "temps.json").unlink(missing_ok=True)

    if opt.get("warnings", "toggle") != "off":
        def warn_loop():
            time.sleep(10)
            w = Warnings(requests.Session(), WWW / "warnings.json",
                         (opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"]))
            last = None
            while True:
                try:
                    n = w.cycle()
                    if n != last:
                        log.info("Unwetterwarnungen: %d aktive Warnungen im Kartenbereich", n)
                        last = n
                except Exception as e:
                    log.warning("Unwetterwarnungen nicht ladbar: %s", e)
                    log.debug(traceback.format_exc())
                time.sleep(300)
        threading.Thread(target=warn_loop, daemon=True, name="warnings").start()
    else:
        (WWW / "warnings.json").unlink(missing_ok=True)

    def model_loop():
        time.sleep(5)
        while True:
            try:
                radar.model_cycle()
            except Exception as e:
                radar.models.error = f"{type(e).__name__}: {e}"
                log.error("Modelldaten fehlgeschlagen: %s", radar.models.error)
                log.debug(traceback.format_exc())
                radar.ha.log("warning", f"Modelldaten (ICON) fehlgeschlagen: {radar.models.error}")
            time.sleep(300)
    threading.Thread(target=model_loop, daemon=True, name="models").start()
    while True:
        t0 = time.time()
        try:
            radar.cycle()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            radar.last_error = msg
            log.error("Abruf/Verarbeitung fehlgeschlagen: %s", msg)
            log.debug(traceback.format_exc())
            radar.ha.log("error", f"Abruf/Verarbeitung fehlgeschlagen: {msg}")
        try:
            radar.cleanup_and_manifest()
        except Exception as e:
            log.error("Manifest fehlgeschlagen: %s", e)
            radar.ha.log("error", f"Manifest fehlgeschlagen: {e}")
        time.sleep(max(5.0, 60.0 - (time.time() - t0)))


if __name__ == "__main__":
    main()
