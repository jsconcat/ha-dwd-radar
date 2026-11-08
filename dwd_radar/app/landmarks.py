"""Landmark layer below the radar: water (lakes, rivers) and motorways.

Data: OpenStreetMap via Overpass API (ODbL), fetched once and cached.
Fallback: bundled Natural Earth rivers/lakes/expressways (coarse).
Tiles at 512 px, shown at 256 px.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw

from hillshade import tiles_for
from mapoverlay import DATA, S, T, gx, gy

log = logging.getLogger("dwd_radar.landmarks")

OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
UA = {"User-Agent": "ha-dwd-radar (Home Assistant add-on)"}

WATER = (170, 200, 228, 255)
RIVER = (150, 188, 222, 255)
MW_CASE = (128, 136, 146, 255)
MW = (255, 255, 255, 255)
PALETTES = {
    False: dict(water=WATER, river=RIVER, mw_case=MW_CASE, mw=MW),
    True: dict(water=(38, 56, 78, 255), river=(46, 72, 100, 255), mw_case=(16, 19, 23, 255), mw=(92, 100, 110, 255)),
}


def river_w(z: int) -> float:
    return {5: 0.6, 6: 0.7, 7: 0.9, 8: 1.1, 9: 1.3, 10: 1.7, 11: 2.2, 12: 2.8}.get(z, 3.2) * S


def mw_w(z: int) -> float:
    return {5: 0.6, 6: 0.7, 7: 0.9, 8: 1.2, 9: 1.6, 10: 2.0, 11: 2.6, 12: 3.2}.get(z, 3.6) * S


def _stitch(ways: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join open way segments of a multipolygon into closed rings."""
    rings, segs = [], [w[:] for w in ways if len(w) > 1]
    while segs:
        ring = segs.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, s in enumerate(segs):
                if s[0] == ring[-1]:
                    ring += s[1:]
                elif s[-1] == ring[-1]:
                    ring += s[::-1][1:]
                elif s[-1] == ring[0]:
                    ring = s[:-1] + ring
                elif s[0] == ring[0]:
                    ring = s[::-1][:-1] + ring
                else:
                    continue
                segs.pop(i)
                changed = True
                break
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def _area_ha(ring) -> float:
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    k = 111320.0
    a = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        a += (x1 * math.cos(lat0) * k) * (y2 * k) - (x2 * math.cos(lat0) * k) * (y1 * k)
    return abs(a) / 2 / 10_000


class Landmarks:
    def __init__(self, out_dir: Path, cache_dir: Path, wide: tuple, extent: tuple, dark: bool = False):
        self.out = out_dir
        self.pal = PALETTES[dark]
        self.cache = cache_dir
        self.wide = wide          # lat0, lat1, lon0, lon1
        self.extent = extent

    # -------------------------------------------------------------- data
    def _overpass(self, query: str) -> dict:
        last = None
        for url in OVERPASS:
            for attempt in range(2):
                try:
                    r = requests.post(url, data={"data": query}, timeout=300, headers=UA)
                    if r.status_code in (429, 504):
                        last = f"{url}: HTTP {r.status_code}"
                        time.sleep(15)
                        continue
                    r.raise_for_status()
                    return r.json()
                except Exception as e:
                    last = f"{url}: {e}"
        raise RuntimeError(f"Overpass fehlgeschlagen ({last})")

    def _bbox(self, b, m=0.15):
        return f"{b[0] - m},{b[2] - m},{b[1] + m},{b[3] + m}"

    def load(self) -> dict:
        self.cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(json.dumps([self.wide, self.extent, 3]).encode()).hexdigest()[:10]
        cf = self.cache / f"osm_{key}.json"
        if cf.exists():
            return json.loads(cf.read_text())
        try:
            bw, be = self._bbox(self.wide), self._bbox(self.extent)
            q1 = (f'[out:json][timeout:240];(way["highway"="motorway"]({bw});'
                  f'way["waterway"="river"]({bw}););out geom qt;')
            q2 = (f'[out:json][timeout:240];(way["natural"="water"]({be});'
                  f'relation["natural"="water"]({be});way["waterway"="riverbank"]({be}););out geom qt;')
            t0 = time.time()
            d1 = self._overpass(q1)
            d2 = self._overpass(q2)
            mw, rv, wa = [], [], []
            for el in d1.get("elements", []):
                g = [(round(p["lon"], 5), round(p["lat"], 5)) for p in el.get("geometry", []) if p]
                if len(g) < 2:
                    continue
                tags = el.get("tags", {})
                if tags.get("highway") == "motorway":
                    mw.append(g)
                else:
                    rv.append([tags.get("name", ""), g])
            for el in d2.get("elements", []):
                if el["type"] == "way":
                    g = [(round(p["lon"], 5), round(p["lat"], 5)) for p in el.get("geometry", []) if p]
                    rings = [g] if len(g) >= 4 and g[0] == g[-1] else []
                elif el["type"] == "relation":
                    outer = [[(round(p["lon"], 5), round(p["lat"], 5)) for p in m.get("geometry", []) if p]
                             for m in el.get("members", []) if m.get("type") == "way" and m.get("role") == "outer"]
                    rings = _stitch(outer)
                else:
                    rings = []
                for r in rings:
                    if _area_ha(r) >= 0.5:
                        wa.append(r)
            data = {"source": "osm", "motorway": mw, "rivers": rv, "water": wa}
            log.info("OSM geladen in %.0f s: %d Autobahn-, %d Fluss-Abschnitte, %d Wasserflächen",
                     time.time() - t0, len(mw), len(rv), len(wa))
        except Exception as e:
            log.warning("OSM nicht verfügbar (%s) – nutze Natural Earth (grober)", e)
            b = json.loads((DATA / "borders.json").read_text())
            return {"source": "ne", "motorway": b["ne_motorway"], "rivers": [["NE", g] for g in b["ne_rivers"]],
                    "water": [p[0] for p in b["ne_lakes"]]}
        tmp = cf.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        os.replace(tmp, cf)
        return data

    # ------------------------------------------------------------ render
    @staticmethod
    def _bin(feats, z, min_px=0.0):
        """Project features to zoom z and index them by tile."""
        bins: dict[tuple[int, int], list] = {}
        for f in feats:
            pts = [(gx(lo, z), gy(la, z)) for lo, la in f]
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            if min_px and max(x1 - x0, y1 - y0) < min_px:
                continue
            m = 8
            for tx in range(int((x0 - m) // T), int((x1 + m) // T) + 1):
                for ty in range(int((y0 - m) // T), int((y1 + m) // T) + 1):
                    bins.setdefault((tx, ty), []).append(pts)
        return bins

    @staticmethod
    def _len_km(g) -> float:
        k = 111.32
        return sum(math.hypot((x2 - x1) * k * math.cos(math.radians(y1)), (y2 - y1) * k)
                   for (x1, y1), (x2, y2) in zip(g, g[1:]))

    def rivers_for_zoom(self, rivers, z: int):
        """Only long rivers when zoomed out (length summed per river name)."""
        if z >= 11:
            return [g for _, g in rivers]
        total: dict[str, float] = {}
        for name, g in rivers:
            if name:
                total[name] = total.get(name, 0.0) + self._len_km(g)
        need = 120 if z <= 8 else (50 if z == 9 else 20)
        return [g for name, g in rivers if name == "NE" or (name and total.get(name, 0) >= need)]

    def ensure(self, jobs) -> None:
        sig = hashlib.sha1(json.dumps([jobs, self.extent, self.pal, 4]).encode()).hexdigest()[:12]
        marker = self.out / f".done_{sig}"
        if marker.exists():
            log.info("Gewässer/Autobahn-Kacheln vorhanden (%s)", sig)
            return
        data = self.load()
        self.out.mkdir(parents=True, exist_ok=True)
        zs = sorted({z for _, z0, z1 in jobs for z in range(z0, z1 + 1)})
        P = self.pal
        for z in zs:
            wmin = {8: 10, 9: 7, 10: 4}.get(z, 3)
            water = self._bin(data["water"], z, min_px=wmin * S) if z >= 8 else {}
            rivers = self._bin(self.rivers_for_zoom(data["rivers"], z), z)
            mw = self._bin(data["motorway"], z) if z >= 6 else {}
            rw, mww = river_w(z), mw_w(z)
            tiles = {(tx, ty) for box, z0, z1 in jobs if z0 <= z <= z1 for tx, ty in tiles_for(box, z)}
            for tx, ty in tiles:
                ox, oy = tx * T, ty * T
                img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                for pts in water.get((tx, ty), ()):
                    d.polygon([(x - ox, y - oy) for x, y in pts], fill=P["water"])
                for pts in rivers.get((tx, ty), ()):
                    d.line([(x - ox, y - oy) for x, y in pts], fill=P["river"], width=max(1, round(rw)), joint="curve")
                segs = [[(x - ox, y - oy) for x, y in pts] for pts in mw.get((tx, ty), ())]
                for sg in segs:
                    d.line(sg, fill=P["mw_case"], width=round(mww + 1.4 * S), joint="curve")
                for sg in segs:
                    d.line(sg, fill=P["mw"], width=max(1, round(mww)), joint="curve")
                p = self.out / str(z) / str(tx) / f"{ty}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                img.save(tmp, "PNG", optimize=True)
                os.replace(tmp, p)
            log.info("Gewässer/Autobahn z%d: %d Kacheln", z, len(tiles))
        for old in self.out.glob(".done_*"):
            old.unlink()
        marker.write_text("ok")
        log.info("Gewässer/Autobahn-Kacheln fertig (Quelle: %s)", data["source"])
