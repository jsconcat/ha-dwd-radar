# Changelog

## 0.6.2
- Zeitleiste flüssig: Bewegung per requestAnimationFrame, Schwung beim Loslassen, weiches Einrasten, Bildwechsel beim Ziehen gedrosselt und asynchron dekodiert, keine Layout-Messungen während der Bewegung

## 0.6.1
- Zeitleiste in Blöcke je Quelle/Auflösung getrennt (Radar, ICON-D2, ICON-EU 1 h/3 h, ICON), eigener Maßstab je Block, Beschriftung bleibt sichtbar

## 0.6.0
- Konfiguration mit deutschen Beschriftungen: Vorhersage 0–7 Tage, Standort (Kartenbereich automatisch per Radius), Rückkehr zur Startansicht 0–120 s
- Pro Dashboard-Karte per URL überschreibbar: ?days=, ?return=, ?zoom=, ?lat=&lon=
- Zeitleiste mit festem Mittelpunkt (Zeit wird darunter verschoben), feiner Maßstab, Tagesmarken; Zeitblase springt zurück zu „Aktuell“
- Zoom-Buttons (+/−) oben rechts beim Antippen
- Caches an die Kartengeometrie gekoppelt (Standortwechsel sicher)

## 0.5.2
- Modellfelder werden in /data zwischengespeichert: Neustart/Update ohne erneuten Download

## 0.5.1
- Robust gegen gecachte alte index.html (fehlende Elemente werden ergänzt)

## 0.5.0
- Vorhersage bis 7 Tage: Radar-Nowcast bis +2 h, danach ICON-D2, ICON-EU, ICON (global) – jeweils beste verfügbare Quelle
- Wolken über den gesamten Zeitraum aus denselben Modellen
- Standard pausiert auf „Aktuell“; Play spielt nur Aktuell bis +2 h
- Zeitleiste: Modellbereich gestaucht mit Tagesmarken; Modellbilder werden erst beim Scrubben geladen

## 0.4.1
- Wolken: Modelllauf wählen, der auch die letzten 2 h abdeckt

## 0.4.0
- Wolken (Gesamtbedeckung ICON-D2, Modell) unter dem Regen, stündlich, auf 5-min-Bilder überblendet
- Weniger Details bei kleinen Zoomstufen: Orte, Flüsse (nach Länge), Seen (nach Größe)
- Autobahnen mit dunklerem Rand

## 0.3.0
- Gewässer (Flüsse, Seen) und Autobahnen unter dem Regen (OSM via Overpass, Fallback Natural Earth)
- Hintergrund leicht salbeigrün

## 0.2.1
- Ortspunkte mit seitlicher Beschriftung, Ausweichpositionen, Pin-Bereich bleibt frei; Pins etwas kleiner

## 0.2.0
- Eigene Grundkarte (Standard): nur Staats-/Landesgrenzen und Ortsnamen über dem Regen, keine Straßen
  (Natural Earth, GeoNames). basemap.de/CARTO/OSM weiterhin wählbar

## 0.1.2
- basemap.de dezenter (CSS-Filter, über basemap_filter anpassbar)
- Relief-Erzeugung setzt nach Neustart fort

## 0.1.1
- Grundkarte wählbar (basemap.de grau, CARTO mit Key, OSM); CARTO liefert seit 09/2026 nur noch mit API-Key

## 0.1.0
- Erste Version: RV-HDF5, Overlays, Leaflet-Seite, Pins, Zeitleiste, Relief
