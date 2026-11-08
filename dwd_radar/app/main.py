"""DWD Regenradar – main loop.

Every minute: read the RV directory listing, process new releases, keep the
last N analysis frames (past) plus the forecast of the newest release, write
manifest.json for the web page and report status to Home Assistant.
"""
from __future__ import annotations

import io
import json
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
from render import Extent, Renderer, build_lut, LUT_MIN, LUT_MAX, LUT_N, STOPS

OPTIONS = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
WWW = Path(os.environ.get("WWW_DIR", "/homeassistant/www/dwd-radar"))
STATIC = Path(os.environ.get("STATIC_DIR", "/app/www"))
DATA = Path(os.environ.get("DATA_DIR", "/data"))
VERSION = os.environ.get("ADDON_VERSION", "dev")

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dwd_radar")


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
        "extent_lat_min": 48.6, "extent_lat_max": 50.6,
        "extent_lon_min": 6.3, "extent_lon_max": 10.3,
        "resolution_m": 500, "smoothing_km": 1.0, "radar_opacity": 1.0,
        "zoom": 9, "min_zoom": 7, "max_zoom": 12,
        "past_minutes": 120, "show_forecast": True,
        "frame_ms": 450, "hold_now_ms": 1500, "hold_end_ms": 2500,
        "controls_hide_s": 6, "reset_view_s": 90, "stale_minutes": 20,
        "hillshade": True, "hillshade_strength": 1.0,
        "basemap": "own", "carto_key": "", "basemap_filter": "",
        "show_clouds": True, "cloud_opacity": 1.0, "forecast_days": 7,
        "log_level": "info",
    }
    d.update({k: v for k, v in o.items() if v is not None})
    return d


class Radar:
    def __init__(self, opt: dict):
        self.o = opt
        self.ha = HA()
        self.http = requests.Session()
        self.renderer = Renderer(
            Extent(opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"]),
            opt["resolution_m"], opt["smoothing_km"], opt["radar_opacity"])
        self.tg = TargetGrid(opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"],
                             opt["extent_lon_max"], 1000.0)
        self.models = ModelStore(self.tg, DATA / "model_cache")
        self.model_frames: list[dict] = []
        self.model_dir = WWW / "frames" / "model"
        self.cloud_dir = WWW / "frames" / "clouds"
        self.lut = build_lut(opt["radar_opacity"])
        self.obs_dir = WWW / "frames" / "obs"
        self.fc_dir = WWW / "frames" / "fc"
        self.state_file = DATA / "state.json"
        st = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.last_run = datetime.fromisoformat(st["last_run"]) if st.get("last_run") else None
        self.failed_backfill: dict[str, int] = {}
        self.last_error: str | None = None
        self.last_success: datetime | None = None
        self.latency_s: float | None = None
        # a geometry change (extent/resolution) invalidates rendered frames
        geo = json.dumps([self.renderer.bounds, opt["resolution_m"], opt["smoothing_km"], opt["radar_opacity"]])
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
        for p in self.obs_dir.glob("o_*.png"):
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
        for fr in frames:
            if fr.lead == 0:
                path = self.obs_path(fr.valid)
                if path.exists():
                    continue
            else:
                path = self.fc_path(run, fr.lead)
            png, _ = self.renderer.png(grid, fr.rate)
            atomic_write(path, png)
            n += 1
        log.info("Lauf %s: %d MB, %d Bilder erzeugt in %.1f s", run.strftime("%H:%M"),
                 len(blob) // 1_000_000, n, time.time() - t0)
        if with_forecast:
            leads_found = sorted(f.lead for f in frames)
            if leads_found and leads_found[-1] < 120:
                log.warning("Lauf %s enthält nur Leads bis %d min", run.strftime("%H:%M"), leads_found[-1])

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
        have = self.existing_obs()
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

    def precip_png(self, rate: np.ndarray) -> bytes:
        rr = np.nan_to_num(rate, nan=0.0)
        idx = (np.log10(np.maximum(rr, 1e-6)) - LUT_MIN) / (LUT_MAX - LUT_MIN) * (LUT_N - 1)
        rgba = self.lut[np.clip(idx, 0, LUT_N - 1).astype(np.int32)]
        rgba[rr < STOPS[0][0]] = 0
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def model_cycle(self) -> None:
        """Runs in its own thread: refresh ICON sources and render model frames."""
        now = datetime.now(timezone.utc)
        past_start = now - timedelta(minutes=self.o["past_minutes"] + 5)
        nowcast_end = (self.last_run or dwd.floor5(now)) + timedelta(minutes=120)
        plan = self.models.update(past_start, nowcast_end, int(self.o["forecast_days"]) * 24)
        frames, keep = [], set()
        n_new = 0
        for p in plan:
            d = self.model_dir / f"{p['src']}_{p['run']:%Y%m%d%H}"
            pp, pc = d / f"p_{p['t']:%Y%m%d%H}.png", d / f"c_{p['t']:%Y%m%d%H}.png"
            if not pp.exists():
                atomic_write(pp, self.precip_png(self.models.rate(p)))
                n_new += 1
            f = {"t": iso(p["t"]), "k": "m", "u": f"frames/model/{d.name}/{pp.name}", "s": p["src"]}
            if self.o.get("show_clouds"):
                cc = self.models.cloud(p)
                if cc is not None:
                    if not pc.exists():
                        atomic_write(pc, self.cloud_png(cc))
                    f["c"] = f"frames/model/{d.name}/{pc.name}"
            frames.append(f)
            keep.add(d.name)
        self.model_frames = frames
        if n_new:
            log.info("Modellbilder: %d neu, %d gesamt bis %s (Download 24 h: %.0f MB)", n_new, len(frames),
                     frames[-1]["t"] if frames else "-", self.models.download_mb_24h())
        cutoff = time.time() - 900
        if self.model_dir.exists():
            for d in self.model_dir.iterdir():
                if d.is_dir() and d.name not in keep and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)

    def attach_clouds(self, frames: list[dict]):
        if not self.o.get("show_clouds") or not frames:
            return
        rd = self.models.runs.get("d2")
        tag = rd.run.strftime("%Y%m%d%H") if rd else "x"
        keep = set()
        for f in frames:
            if f["k"] == "m":
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
        # forecasts: current run + previous one (clients may still load it)
        runs_fc = sorted(p for p in self.fc_dir.glob("*") if p.is_dir()) if self.fc_dir.exists() else []
        for p in runs_fc[:-2]:
            shutil.rmtree(p, ignore_errors=True)
        frames = [{"t": iso(t), "k": "o", "u": f"frames/obs/{p.name}"} for t, p in sorted(obs.items())]
        fc_run = None
        if self.o["show_forecast"] and self.last_run:
            d = self.fc_dir / f"{self.last_run:%Y%m%d%H%M}"
            if d.is_dir():
                fc_run = self.last_run
                for p in sorted(d.glob("f_*.png")):
                    lead = int(p.stem[2:])
                    t = self.last_run + timedelta(minutes=lead)
                    frames.append({"t": iso(t), "k": "f", "u": f"frames/fc/{d.name}/{p.name}"})
        last_t = frames[-1]["t"] if frames else ""
        frames += [f for f in self.model_frames if f["t"] > last_t]
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
        atomic_write(WWW / "manifest.json", json.dumps(manifest, separators=(",", ":")).encode())
        state = "error" if self.last_error and stale else ("stale" if stale else "ok")
        if stale and not self.last_error and newest_obs:
            self.ha.log("warning", f"Keine neuen DWD-Radardaten seit {newest_obs:%H:%M} UTC – zeige letzten Stand")
        self.ha.status(state,
                       latest_frame=iso(newest_obs) if newest_obs else None,
                       data_age_min=round(age_min, 1) if age_min is not None else None,
                       forecast_run=iso(fc_run) if fc_run else None,
                       frames=len(frames),
                       dwd_latency_s=self.latency_s,
                       model_runs={k: iso(v.run) for k, v in self.models.runs.items() if k != "d2prev"},
                       model_frames=len(self.model_frames),
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
        b["maxNativeZoom"] = min(12, int(opt["max_zoom"]))
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
        "hillshade": bool(opt["hillshade"]),
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
            try:
                wide = jobs[0][0]
                MapOverlay(WWW / "overlay", DATA / "geo_cache", wide, pins_of(opt)).ensure(jobs)
            except Exception as e:
                log.error("Karten-Overlay fehlgeschlagen: %s", e)
            try:
                ext = (opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"])
                Landmarks(WWW / "base", DATA / "geo_cache", jobs[0][0], ext).ensure(jobs)
            except Exception as e:
                log.error("Gewässer/Autobahn-Kacheln fehlgeschlagen: %s", e)
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
