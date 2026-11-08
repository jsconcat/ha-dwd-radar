# DWD Regenradar

Erzeugt alle 5 Minuten Regenradar-Overlays ausschließlich aus DWD-Open-Data und stellt
sie als Leaflet-Seite bereit: `/local/dwd-radar/index.html` (iframe-Karte im Dashboard).

## Daten
- Produkt **RV** (ODIM-HDF5, `opendata.dwd.de/weather/radar/composite/rv/`):
  5-Minuten-Niederschlagsmenge, 1 km, Analyse + Vorhersage bis +120 min.
- Vergangenheit: Analyse-Schritt jedes Laufs; fehlende Zeitschritte werden nachgeladen.
- Umrechnung mm/5 min × 12 = mm/h, Umprojektion polarstereografisch → Web Mercator,
  Glättung im log-Raum, eigene Farbskala, transparente PNGs.

## Optionen (Auswahl)
| Option | Bedeutung |
|---|---|
| `home_lat/home_lon/home_name` | Heimatort: hervorgehobener Pin, Kartenmitte |
| `pins` | weitere Pins: `- name: Mannheim` / `lat:` / `lon:` (optional `home: true`) |
| `extent_*` | Ausschnitt, für den Regenbilder berechnet werden |
| `resolution_m` | Pixelgröße der Overlays (Web-Mercator-Meter) |
| `smoothing_km` | Glättung (0 = keine) |
| `zoom/min_zoom/max_zoom` | Start- und Grenzzoom |
| `frame_ms/hold_now_ms/hold_end_ms` | Animationstempo |
| `controls_hide_s` | Zeitleiste blendet sich nach x s aus |
| `reset_view_s` | nach x s ohne Bedienung zurück zur Startansicht (0 = aus) |
| `stale_minutes` | ab diesem Datenalter Hinweis „Stand …“ |
| `hillshade*` | Relief aus offenen Höhendaten (einmalig erzeugt) |

## Status in Home Assistant
- `sensor.dwd_radar_status`: `ok` / `stale` / `error`, Attribute u. a. `data_age_min`,
  `dwd_latency_s`, `last_error`.
- Fehler zusätzlich im HA-Systemprotokoll (Logger `dwd_radar`).

## Quellen / Lizenzen
Radar: © Deutscher Wetterdienst (CC BY 4.0). Karte: © OpenStreetMap-Mitwirkende, © CARTO.
Relief: AWS Terrain Tiles (Mapzen). Leaflet: BSD-2-Clause.
