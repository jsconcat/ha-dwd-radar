"""DWD weather warnings (municipality level) as a simplified GeoJSON overlay.

Source: DWD GeoServer WFS layer dwd:Warnungen_Gemeinden. Polygons of all municipalities with the same warning are
merged and simplified, so the overlay stays small. Output: www/warnings.json
  {"generated": iso, "features": [{"type": "Feature", "geometry": ..., "properties":
     {"ev": event, "sev": 1..4, "on": epoch ms, "ex": epoch ms, "hl": headline, "ds": description, "in": instruction}}]}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
import shapely
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

log = logging.getLogger("dwd_radar.warnings")

WFS = ("https://maps.dwd.de/geoserver/dwd/ows?service=WFS&version=1.0.0&request=GetFeature"
       "&typeName=dwd:Warnungen_Gemeinden&outputFormat=application/json&bbox={lon0},{lat0},{lon1},{lat1}")
SEV = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}


def _ms(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _swap(geom: dict) -> dict:
    def sw(c):
        return [sw(x) for x in c] if isinstance(c[0], list) else [c[1], c[0]]
    return {"type": geom["type"], "coordinates": sw(geom["coordinates"])}


class Warnings:
    def __init__(self, http: requests.Session, out: Path, extent: tuple):
        self.http = http
        self.out = out
        self.extent = extent        # lat0, lat1, lon0, lon1

    def cycle(self) -> int:
        lat0, lat1, lon0, lon1 = self.extent
        r = self.http.get(WFS.format(lat0=lat0, lat1=lat1, lon0=lon0, lon1=lon1), timeout=90)
        r.raise_for_status()
        fc = r.json()
        groups: dict[tuple, dict] = {}
        for f in fc.get("features", []):
            p = f.get("properties") or {}
            g = f.get("geometry")
            if not g or not g.get("coordinates"):
                continue
            # axis order safety: our area is at lat 45..56, lon 3..17
            first = g["coordinates"]
            while isinstance(first[0], list):
                first = first[0]
            if first[0] > 40 and first[1] < 20:
                g = _swap(g)
            key = (p.get("EVENT"), p.get("SEVERITY"), p.get("ONSET"), p.get("EXPIRES"), p.get("HEADLINE"))
            e = groups.setdefault(key, {"geoms": [], "p": p})
            try:
                e["geoms"].append(shape(g).buffer(0))
            except Exception:
                continue
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        feats = []
        for (ev, sev, on, ex, hl), e in groups.items():
            exm = _ms(ex)
            if exm is not None and exm < now:
                continue
            geom = shapely.set_precision(unary_union(e["geoms"]).simplify(0.002, preserve_topology=True), 0.0001)
            if geom.is_empty:
                continue
            p = e["p"]
            feats.append({"type": "Feature", "geometry": mapping(geom), "properties": {
                "ev": (ev or "").strip(), "sev": SEV.get(str(sev or "").lower(), 1),
                "on": _ms(on) or now, "ex": exm or now + 86400000,
                "hl": (hl or "").strip(), "ds": (p.get("DESCRIPTION") or "").strip(),
                "in": (p.get("INSTRUCTION") or "").strip()}})
        feats.sort(key=lambda f: f["properties"]["sev"])     # higher levels drawn on top
        doc = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "type": "FeatureCollection", "features": feats}
        txt = json.dumps(doc, separators=(",", ":"))
        tmp = self.out.with_name(self.out.name + ".tmp")
        tmp.write_text(txt)
        tmp.replace(self.out)
        return len(feats)
