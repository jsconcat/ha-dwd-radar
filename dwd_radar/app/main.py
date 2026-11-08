"""DWD Regenradar – main loop.

Every minute: read the RV directory listing, process new releases, keep the
last N analysis frames (past) plus the forecast of the newest release, write
manifest.json for the web page and report status to Home Assistant.
"""
from __future__ import annotations

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

import requests

import dwd
from ha import HA
from hillshade import Hillshade
from render import Extent, Renderer

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
        age_min = (now - newest_obs).total_seconds() / 60 if newest_obs else None
        stale = age_min is None or age_min > self.o["stale_minutes"]
        manifest = {
            "generated": iso(now),
            "bounds": self.renderer.bounds,
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
                       last_success=iso(self.last_success) if self.last_success else None,
                       last_error=self.last_error)


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
    pins = [{"name": opt["home_name"], "lat": opt["home_lat"], "lon": opt["home_lon"], "home": True}]
    for p in opt.get("pins") or []:
        try:
            pins.append({"name": str(p.get("name", "")), "lat": float(p["lat"]), "lon": float(p["lon"]),
                         "home": bool(p.get("home", False))})
        except (KeyError, TypeError, ValueError):
            log.warning("Ungültiger Pin ignoriert: %r", p)
    cfg = {
        "version": VERSION,
        "center": [opt["home_lat"], opt["home_lon"]],
        "zoom": opt["zoom"], "minZoom": opt["min_zoom"], "maxZoom": opt["max_zoom"],
        "bounds": bounds,
        "pins": pins,
        "frameMs": opt["frame_ms"], "holdNowMs": opt["hold_now_ms"], "holdEndMs": opt["hold_end_ms"],
        "controlsHideS": opt["controls_hide_s"], "resetViewS": opt["reset_view_s"],
        "hillshade": bool(opt["hillshade"]),
    }
    atomic_write(WWW / "config.json", json.dumps(cfg, indent=1).encode())


def start_hillshade(opt: dict) -> None:
    if not opt["hillshade"]:
        return
    b = (opt["extent_lat_min"], opt["extent_lat_max"], opt["extent_lon_min"], opt["extent_lon_max"])
    pad_lat = (b[1] - b[0]) * 0.75; pad_lon = (b[3] - b[2]) * 0.75
    wide = (b[0] - pad_lat, b[1] + pad_lat, b[2] - pad_lon, b[3] + pad_lon)
    zmax = min(12, int(opt["max_zoom"]))
    jobs = [(wide, max(5, int(opt["min_zoom"]) - 1), min(zmax, 9)), (b, 10, zmax)] if zmax >= 10 \
        else [(wide, max(5, int(opt["min_zoom"]) - 1), zmax)]

    def run():
        try:
            Hillshade(WWW / "hillshade", DATA / "dem_cache", opt["hillshade_strength"]).ensure(jobs)
        except Exception as e:
            log.error("Relief-Kacheln fehlgeschlagen (Karte funktioniert trotzdem): %s", e)
    threading.Thread(target=run, daemon=True, name="hillshade").start()


def main() -> None:
    opt = load_options()
    logging.getLogger().setLevel(getattr(logging, str(opt["log_level"]).upper(), logging.INFO))
    log.info("DWD Regenradar %s startet – Ausgabe nach %s", VERSION, WWW)
    radar = Radar(opt)
    install_web(opt, radar.renderer.bounds)
    log.info("Overlay %dx%d px, Bounds %s", radar.renderer.nx, radar.renderer.ny, radar.renderer.bounds)
    start_hillshade(opt)
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
