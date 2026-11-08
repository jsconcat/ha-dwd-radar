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
| `area_radius_km` | Kartenbereich um den Standort (Radius) |
| `resolution_m` | Pixelgröße der Overlays (Web-Mercator-Meter) |
| `smoothing_km` | Glättung (0 = keine) |
| `zoom/min_zoom/max_zoom` | Start- und Grenzzoom |
| `frame_ms/hold_now_ms/hold_end_ms` | Animationstempo |
| `controls_hide_s` | Zeitleiste blendet sich nach x s aus |
| `reset_view_s` | nach Verschieben/Zoomen nach x s zurück zur Startansicht (0 = aus, max. 120) |
| `stale_minutes` | ab diesem Datenalter Hinweis „Stand …“ |
| `show_clouds` / `cloud_opacity` | Wolken aus ICON-D2/EU/ICON (Modell, kein Satellit) |
| `forecast_days` | 0 = keine Modellvorhersage, 1–7 Tage (nach den 2 h Radar-Nowcast) |
| `color_style` | `classes` = feste Farbstufen wie WarnWetter, `smooth` = stufenloser Verlauf |
| `smooth_motion` | Zwischenbilder per Optical Flow (Radar 1 min, Modelle 10/30 min). Kostet CPU und etwas Speicher |
| `basemap` | `own` (Grenzen/Orte/Gewässer/Autobahnen selbst erzeugt), `basemapde`, `carto` (Key), `osm` |
| `hillshade*` | Relief aus offenen Höhendaten (einmalig erzeugt) |

## Status in Home Assistant
- `sensor.dwd_radar_status`: `ok` / `stale` / `error`, Attribute u. a. `data_age_min`,
  `dwd_latency_s`, `last_error`.
- Fehler zusätzlich im HA-Systemprotokoll (Logger `dwd_radar`).

## Quellen / Lizenzen
Radar: © Deutscher Wetterdienst (CC BY 4.0). Karte: © OpenStreetMap-Mitwirkende, © CARTO.
Relief: AWS Terrain Tiles (Mapzen). Leaflet: BSD-2-Clause.

## Einstellungen pro Dashboard-Karte (iframe-URL)
`/local/dwd-radar/index.html?loader=1&days=3&return=30&zoom=10`
- `days` 0–7 (höchstens so viele wie in der App eingestellt), `return` 0–120 s, `zoom`, `lat`/`lon` (Kartenmitte + Heimat-Pin,
  muss im Kartenbereich der App liegen).
