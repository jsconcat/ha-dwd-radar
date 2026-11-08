"""Own reference layer: country + state borders and place names (no roads).

Tiles are rendered at 512 px (crisp on HiDPI) and shown at 256 px by Leaflet.
Label placement is decided once per zoom level over the whole area (collision
test, largest places first), so labels are consistent across tile edges.

Places: GeoNames cities1000 (CC BY 4.0), downloaded once on the HA host;
fallback: bundled Natural Earth places. Borders: Natural Earth 10m (bundled).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import zipfile
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from hillshade import tiles_for

log = logging.getLogger("dwd_radar.overlay")

DATA = Path(__file__).parent / "data"
GEONAMES = "https://download.geonames.org/export/dump/cities1000.zip"
UA = {"User-Agent": "ha-dwd-radar (Home Assistant add-on)"}
T = 512                       # rendered tile size (2x)
S = T / 256                   # scale factor vs. CSS pixels

# minimum population per zoom (dense enough to orient, collision does the rest)
MIN_POP = {5: 400_000, 6: 250_000, 7: 150_000, 8: 60_000, 9: 22_000, 10: 7_000, 11: 2_000, 12: 1_000,
           13: 1_000, 14: 1_000}
COUNTRY = dict(fill=(96, 106, 116, 255), width=round(1.6 * S))
STATE = dict(fill=(140, 150, 160, 230), width=round(1.0 * S))
TEXT = (38, 48, 58, 255)
HALO = (255, 255, 255, 235)
DOT = (70, 80, 90, 255)
PALETTES = {
    False: dict(country=COUNTRY, state=STATE, text=TEXT, halo=HALO, dot=DOT),
    True: dict(country=dict(fill=(150, 160, 172, 255), width=COUNTRY["width"]),
               state=dict(fill=(92, 102, 114, 235), width=STATE["width"]),
               text=(226, 231, 236, 255), halo=(18, 22, 27, 225), dot=(205, 212, 218, 255)),
}


def gx(lon: float, z: int) -> float:
    return (lon + 180.0) / 360.0 * (2 ** z) * T


def gy(lat: float, z: int) -> float:
    return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * (2 ** z) * T


class MapOverlay:
    def __init__(self, out_dir: Path, cache_dir: Path, box: tuple[float, float, float, float],
                 pins: list[dict] | None = None, dark: bool = False):
        self.out = out_dir
        self.dark = dark
        self.pal = PALETTES[dark]
        self.cache = cache_dir
        self.box = box            # lat_min, lat_max, lon_min, lon_max (widest area)
        self.pins = pins or []    # screen area of pins is kept free of labels
        b = json.loads((DATA / "borders.json").read_text())
        self.country = b["country"]
        self.state = b["state"]
        self.fallback_places = b["places"]
        self.font_m = str(DATA / "Roboto-Medium.ttf")
        self.font_r = str(DATA / "Roboto-Regular.ttf")
        self._fonts: dict = {}

    # ----------------------------------------------------------- places
    def places(self) -> list[tuple[str, float, float, int]]:
        self.cache.mkdir(parents=True, exist_ok=True)
        z = self.cache / "cities1000.zip"
        if not z.exists():
            try:
                r = requests.get(GEONAMES, timeout=120, headers=UA)
                r.raise_for_status()
                tmp = z.with_suffix(".tmp")
                tmp.write_bytes(r.content)
                os.replace(tmp, z)
                log.info("GeoNames-Orte geladen (%d MB)", len(r.content) // 1_000_000)
            except Exception as e:
                log.warning("GeoNames nicht erreichbar (%s) – nutze Natural-Earth-Orte (weniger Orte)", e)
                return [tuple(p) for p in self.fallback_places]
        lat0, lat1, lon0, lon1 = self.box
        out = []
        with zipfile.ZipFile(z) as zf:
            with zf.open("cities1000.txt") as fh:
                for line in io.TextIOWrapper(fh, encoding="utf-8"):
                    c = line.rstrip("\n").split("\t")
                    if len(c) < 15:
                        continue
                    try:
                        lat, lon, pop = float(c[4]), float(c[5]), int(c[14] or 0)
                    except ValueError:
                        continue
                    if not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1):
                        continue
                    if c[7] in ("PPLX", "PPLH", "PPLQ", "PPLW"):   # districts, historical, abandoned
                        continue
                    out.append((c[1], lat, lon, pop))
        log.info("%d Orte im Gebiet", len(out))
        return out

    # ------------------------------------------------------------ labels
    def font(self, pop: int):
        if pop >= 400_000:
            key = ("m", 15.5)
        elif pop >= 100_000:
            key = ("m", 14.5)
        elif pop >= 20_000:
            key = ("m", 13)
        elif pop >= 5_000:
            key = ("r", 12.5)
        else:
            key = ("r", 12)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(self.font_m if key[0] == "m" else self.font_r,
                                                  round(key[1] * S))
        return self._fonts[key]

    def place_labels(self, places, z: int, area: tuple[float, float, float, float]) -> list[tuple]:
        """Collision-free label list for zoom z within area (lat0, lat1, lon0, lon1)."""
        mp = MIN_POP.get(z, 1_000)
        lat0, lat1, lon0, lon1 = area
        cand = sorted((p for p in places if p[3] >= mp and lat0 <= p[1] <= lat1 and lon0 <= p[2] <= lon1),
                      key=lambda p: -p[3])
        placed: list[tuple] = []
        grid: dict[tuple[int, int], list] = {}
        cell = 256
        pad = 3 * S
        sw = round(2 * S)

        def keys(bb):
            return [(i, j) for i in range(int(bb[0] // cell), int(bb[2] // cell) + 1)
                    for j in range(int(bb[1] // cell), int(bb[3] // cell) + 1)]

        def free(bb):
            for k in keys(bb):
                for o in grid.get(k, ()):
                    if bb[0] < o[2] and bb[2] > o[0] and bb[1] < o[3] and bb[3] > o[1]:
                        return False
            return True

        def occupy(bb):
            for k in keys(bb):
                grid.setdefault(k, []).append(bb)

        for p in self.pins:          # pin icons (anchored at their tip)
            x, y = gx(p["lon"], z), gy(p["lat"], z)
            w, h = (12, 33) if p.get("home") else (9, 25)
            occupy((x - w * S, y - h * S, x + w * S, y + 3 * S))

        dot = 2.6 * S
        gap = 4 * S
        for name, lat, lon, pop in cand:
            f = self.font(pop)
            x, y = gx(lon, z), gy(lat, z)
            dbb = (x - dot - 1, y - dot - 1, x + dot + 1, y + dot + 1)
            if not free(dbb):
                continue
            l, t, r, b = f.getbbox(name, anchor="lm", stroke_width=sw)
            w, hgt = r - l, b - t
            # right, left, above, below of the dot
            options = [((x + dot + gap, y), "lm"), ((x - dot - gap, y), "rm"),
                       ((x, y - dot - gap), "mb"), ((x, y + dot + gap), "mt")]
            for (tx, ty), anchor in options:
                l, t, r, b = f.getbbox(name, anchor=anchor, stroke_width=sw)
                bb = (tx + l - pad, ty + t - pad, tx + r + pad, ty + b + pad)
                if free(bb):
                    occupy(bb); occupy(dbb)
                    full = (min(bb[0], dbb[0]), min(bb[1], dbb[1]), max(bb[2], dbb[2]), max(bb[3], dbb[3]))
                    placed.append((tx, ty, name, f, full, anchor, x, y))
                    break
        return placed

    # ------------------------------------------------------------- tiles
    def _lines_px(self, lines, z):
        out = []
        for ln in lines:
            pts = [(gx(lo, z), gy(la, z)) for lo, la in ln]
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            out.append((min(xs), min(ys), max(xs), max(ys), pts))
        return out

    def render_tile(self, z, x, y, country_px, state_px, labels) -> Image.Image:
        ox, oy = x * T, y * T
        img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        m = 8
        P = self.pal
        for lines, style in ((state_px, P["state"]), (country_px, P["country"])):
            for x0, y0, x1, y1, pts in lines:
                if x1 < ox - m or x0 > ox + T + m or y1 < oy - m or y0 > oy + T + m:
                    continue
                d.line([(px - ox, py - oy) for px, py in pts], joint="curve", **style)
        r = 2.6 * S
        for lx, ly, text, f, bb, anchor, px, py in labels:
            if bb[2] < ox or bb[0] > ox + T or bb[3] < oy or bb[1] > oy + T:
                continue
            cx, cy = px - ox, py - oy
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=P["dot"], outline=P["halo"], width=round(1 * S))
            d.text((lx - ox, ly - oy), text, font=f, anchor=anchor, fill=P["text"],
                   stroke_width=round(2 * S), stroke_fill=P["halo"])
        return img

    def ensure(self, jobs: list[tuple[tuple[float, float, float, float], int, int]]) -> None:
        src = hashlib.sha1((DATA / "borders.json").read_bytes()).hexdigest()[:8]
        geon = (self.cache / "cities1000.zip").exists()
        sig = hashlib.sha1(json.dumps([jobs, MIN_POP, src, geon, self.pal, self.pins, 4]).encode()).hexdigest()[:12]
        marker = self.out / f".done_{sig}"
        if marker.exists():
            log.info("Karten-Overlay vorhanden (%s)", sig)
            return
        places = self.places()
        geon = (self.cache / "cities1000.zip").exists()
        sig = hashlib.sha1(json.dumps([jobs, MIN_POP, src, geon, self.pal, self.pins, 4]).encode()).hexdigest()[:12]
        marker = self.out / f".done_{sig}"
        self.out.mkdir(parents=True, exist_ok=True)
        zs = sorted({z for _, z0, z1 in jobs for z in range(z0, z1 + 1)})
        total = 0
        for z in zs:
            boxes = [box for box, z0, z1 in jobs if z0 <= z <= z1]
            m = 0.3
            area = (min(b[0] for b in boxes) - m, max(b[1] for b in boxes) + m,
                    min(b[2] for b in boxes) - m, max(b[3] for b in boxes) + m)
            labels = self.place_labels(places, z, area)
            cpx, spx = self._lines_px(self.country, z), self._lines_px(self.state, z)
            tiles = {(tx, ty) for box, z0, z1 in jobs if z0 <= z <= z1 for tx, ty in tiles_for(box, z)}
            for tx, ty in tiles:
                p = self.out / str(z) / str(tx) / f"{ty}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                img = self.render_tile(z, tx, ty, cpx, spx, labels)
                tmp = p.with_suffix(".tmp")
                img.save(tmp, "PNG", optimize=True)
                os.replace(tmp, p)
            total += len(tiles)
            log.info("Karten-Overlay z%d: %d Kacheln, %d Ortsnamen", z, len(tiles), len(labels))
        for old in self.out.glob(".done_*"):
            old.unlink()
        marker.write_text("ok")
        log.info("Karten-Overlay fertig (%d Kacheln)", total)
