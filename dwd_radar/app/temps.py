"""Temperatures at DWD stations for the map: MOSMIX_L point forecasts, past/now from station reports (POI).

Output (www/temps.json):
  {"generated": iso, "t0": iso (full hour, UTC), "n": hours,
   "st": [{"i": id, "n": name, "la": lat, "lo": lon, "p": priority rank, "v": [tenths °C | null, ...]}]}
"""
from __future__ import annotations

import io
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("dwd_radar.temps")

CATALOG_URL = "https://www.dwd.de/DE/leistungen/met_verfahren_mosmix/mosmix_stationskatalog.cfg?view=nasPublication&nn=16102"
MOSMIX_URL = "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/{id}/kml/MOSMIX_L_LATEST_{id}.kmz"
POI_URL = "https://opendata.dwd.de/weather/weather_reports/poi/{id}-BEOB.csv"
MAX_STATIONS = 60
FLOAT = re.compile(r"^-?\d+\.\d+$")
INT = re.compile(r"^-?\d+$")


def _degmin(v: str) -> float:
    """MOSMIX catalog coordinates are degrees.minutes (49.31 = 49°31')."""
    x = float(v)
    s = -1 if x < 0 else 1
    x = abs(x)
    d = math.floor(x)
    return s * (d + round((x - d) * 100) / 60.0)


def parse_catalog(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        tok = line.split()
        # find "LAT LON ELEV" (two decimals followed by an integer)
        for i in range(len(tok) - 2):
            if FLOAT.match(tok[i]) and FLOAT.match(tok[i + 1]) and INT.match(tok[i + 2]):
                break
        else:
            continue
        # station id (5 chars) followed by ICAO (4 chars or ----)
        j = next((k for k in range(i - 1) if len(tok[k]) == 5 and len(tok[k + 1]) == 4), None)
        if j is None:
            continue
        name = " ".join(tok[j + 2:i]).strip()
        try:
            out.append({"i": tok[j], "n": name, "la": _degmin(tok[i]), "lo": _degmin(tok[i + 1])})
        except ValueError:
            continue
    return out


def _dist_km(a, b) -> float:
    dlat = math.radians(b[0] - a[0])
    dlon = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371.0 * math.hypot(dlat, dlon)


def pretty_name(n: str) -> str:
    return " ".join(w.capitalize() if w.isupper() else w for w in n.replace("/", " / ").split())


class Temps:
    def __init__(self, http: requests.Session, cache: Path, out: Path, extent: tuple, home: tuple, hours: int):
        self.http = http
        self.cache = cache
        self.out = out
        self.extent = extent            # lat0, lat1, lon0, lon1
        self.home = home
        self.hours = hours
        self.cache.mkdir(parents=True, exist_ok=True)
        self.stations: list[dict] = []
        self.mos: dict[str, dict] = {}   # id -> {"lm": last-modified, "ts": [epoch], "v": [K|None], "la", "lo"}
        self.obs: dict[str, dict] = {}   # id -> {"lm", "data": {epoch_hour: °C}}
        self.no_poi: dict[str, float] = {}
        self.last_catalog = 0.0
        state = self.cache / "temps_state.json"
        if state.exists():
            try:
                st = json.loads(state.read_text())
                self.mos = st.get("mos", {})
                self.obs = {k: {"lm": v["lm"], "data": {int(h): t for h, t in v["data"].items()}}
                            for k, v in st.get("obs", {}).items()}
            except Exception:
                pass

    # ------------------------------------------------------------ stations
    def load_catalog(self) -> None:
        p = self.cache / "mosmix_catalog.cfg"
        if not p.exists() or time.time() - p.stat().st_mtime > 7 * 86400:
            try:
                r = self.http.get(CATALOG_URL, timeout=60)
                r.raise_for_status()
                p.write_bytes(r.content)
            except Exception as e:
                if not p.exists():
                    raise RuntimeError(f"MOSMIX-Stationskatalog nicht ladbar: {e}")
                log.warning("MOSMIX-Stationskatalog: %s – nutze gespeicherte Fassung", e)
        allst = parse_catalog(p.read_text(encoding="latin-1"))
        lat0, lat1, lon0, lon1 = self.extent
        m = 0.05
        cand = [s for s in allst if lat0 - m <= s["la"] <= lat1 + m and lon0 - m <= s["lo"] <= lon1 + m]
        # WMO stations (5 digits) first – they also report observations; then by distance to home
        cand.sort(key=lambda s: (not s["i"].isdigit(), _dist_km(self.home, (s["la"], s["lo"]))))
        sel, d = cand, 6.0
        while True:
            sel = []
            for s in cand:
                if all(_dist_km((s["la"], s["lo"]), (o["la"], o["lo"])) >= d for o in sel):
                    sel.append(s)
            if len(sel) <= MAX_STATIONS:
                break
            d *= 1.2
        for s in sel:
            s["n"] = pretty_name(s["n"])
        self.stations = sel
        self.last_catalog = time.time()
        log.info("Temperatur: %d Stationen im Kartenbereich (von %d im Katalog, Abstand ≥ %.0f km)",
                 len(sel), len(allst), d)

    # ------------------------------------------------------------ downloads
    def _get(self, url: str, lm: str | None):
        h = {"If-Modified-Since": lm} if lm else {}
        r = self.http.get(url, headers=h, timeout=60)
        if r.status_code == 304:
            return None, lm
        r.raise_for_status()
        return r.content, r.headers.get("Last-Modified")

    def fetch_mosmix(self, sid: str) -> None:
        old = self.mos.get(sid)
        blob, lm = self._get(MOSMIX_URL.format(id=sid), old and old.get("lm"))
        if blob is None:
            return
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            kml = z.read(z.namelist()[0])
        root = ET.fromstring(kml)
        ts = [int(datetime.fromisoformat(e.text.strip().replace("Z", "+00:00")).timestamp())
              for e in root.iter() if e.tag.endswith("}TimeStep")]
        vals = None
        for fc in root.iter():
            if not fc.tag.endswith("}Forecast"):
                continue
            name = next((v for k, v in fc.attrib.items() if k.endswith("elementName")), None)
            if name == "TTT":
                txt = next((e.text for e in fc if e.tag.endswith("}value")), "") or ""
                vals = [None if x in ("-", "") else float(x) for x in txt.split()]
                break
        if not ts or not vals:
            raise ValueError("TTT fehlt")
        ent = {"lm": lm, "ts": ts[:len(vals)], "v": vals[:len(ts)]}
        co = next((e.text for e in root.iter() if e.tag.endswith("}coordinates")), None)
        if co:
            try:
                lo, la = (float(x) for x in co.split(",")[:2])
                ent["la"], ent["lo"] = la, lo          # exact decimal position (catalog is rounded to minutes)
            except ValueError:
                pass
        self.mos[sid] = ent

    def fetch_poi(self, sid: str) -> None:
        if not sid.isdigit() or self.no_poi.get(sid, 0) > time.time():
            return
        old = self.obs.get(sid)
        try:
            blob, lm = self._get(POI_URL.format(id=sid), old and old.get("lm"))
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self.no_poi[sid] = time.time() + 86400      # station without reports
                return
            raise
        if blob is None:
            return
        lines = blob.decode("utf-8", "replace").splitlines()
        hdr = [l.split(";") for l in lines[:3]]
        col = None
        for h in hdr:
            for k, name in enumerate(h):
                if "dry_bulb_temperature_at_2_meter" in name or name.strip().startswith("Temperatur (2m)"):
                    col = k
                    break
            if col is not None:
                break
        if col is None:
            raise ValueError("Spalte Temperatur fehlt")
        data = {}
        for l in lines[3:]:
            f = l.split(";")
            if len(f) <= col:
                continue
            try:
                t = datetime.strptime(f[0] + " " + f[1], "%d.%m.%y %H:%M").replace(tzinfo=timezone.utc)
                v = float(f[col].replace(",", "."))
            except ValueError:
                continue
            if t.minute == 0:
                data[int(t.timestamp())] = v
        self.obs[sid] = {"lm": lm, "data": data}

    # ------------------------------------------------------------ cycle
    def cycle(self) -> None:
        if not self.stations or time.time() - self.last_catalog > 86400:
            self.load_catalog()
        errs = 0
        for s in self.stations:
            try:
                self.fetch_mosmix(s["i"])
            except Exception as e:
                errs += 1
                log.debug("MOSMIX %s: %s", s["i"], e)
            try:
                self.fetch_poi(s["i"])
            except Exception as e:
                log.debug("Meldung %s: %s", s["i"], e)
        if errs:
            log.warning("Temperatur: %d von %d MOSMIX-Stationen nicht ladbar", errs, len(self.stations))
        self.write()
        try:
            (self.cache / "temps_state.json").write_text(json.dumps({
                "mos": self.mos,
                "obs": {k: {"lm": v["lm"], "data": {str(h): t for h, t in v["data"].items()}} for k, v in self.obs.items()}}))
        except OSError:
            pass

    def write(self) -> None:
        now = datetime.now(timezone.utc)
        t0 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
        e0 = int(t0.timestamp())
        n = 3 + self.hours + 1
        now_e = int(now.timestamp())
        out = []
        for rank, s in enumerate(self.stations):
            m = self.mos.get(s["i"])
            mm = dict(zip(m["ts"], m["v"])) if m else {}
            ob = self.obs.get(s["i"], {}).get("data", {})
            vals = []
            for k in range(n):
                e = e0 + k * 3600
                v = ob.get(e) if e <= now_e else None
                if v is None:
                    kv = mm.get(e)
                    v = kv - 273.15 if kv is not None else None
                vals.append(None if v is None else int(round(v * 10)))
            if any(v is not None for v in vals):
                la, lo = (m.get("la", s["la"]), m.get("lo", s["lo"])) if m else (s["la"], s["lo"])
                out.append({"i": s["i"], "n": s["n"], "la": round(la, 4), "lo": round(lo, 4), "p": rank,
                            "v": vals})
        doc = {"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "n": n, "st": out}
        tmp = self.out.with_name(self.out.name + ".tmp")
        tmp.write_text(json.dumps(doc, separators=(",", ":")))
        tmp.replace(self.out)
