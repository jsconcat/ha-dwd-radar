# Changelog

## 0.9.1
- Testseite `/local/dwd-radar/gltest.html`: MapLibre GL JS (WebGL) mit eigenen Kacheln und Bewegungsinterpolation + Farbstufen im Shader (synthetische Testdaten), zeigt WebGL-Fähigkeiten und Bildrate

## 0.9.0
- Dunkelmodus: Option `theme` (auto/light/dark). auto folgt dem Hell/Dunkel-Modus von Home Assistant (im Dashboard-iframe), sonst der Geräteeinstellung; Umschalten ohne Neuladen. Pro Karte überschreibbar mit `?theme=`
- Eigene dunkle Kartenkacheln (Grenzen, Orte, Gewässer, Autobahnen), dunkle Zeitleiste und Bedienelemente; Relief und Wolken im Dunkelmodus abgeschwächt

## 0.8.1
- Fix: Absturz („corrupted size vs. prev_size“) – OpenCV-Optical-Flow wurde gleichzeitig aus Radar- und Modell-Thread benutzt; jetzt serialisiert, OpenCV auf 1 Thread begrenzt

## 0.8.0
- Farbstufen wie in der WarnWetter-App: 9 feste Klassen (0,1 … 50 mm/h) mit harten, aber geglätteten Kanten (Supersampling). Option `color_style` (classes/smooth)
- Optical Flow (OpenCV DIS): Zwischenbilder per Bewegungsschätzung – Radar minütlich, Modelle alle 10 min (1-h-Schritte) bzw. 30 min (3-h-Schritte). Option `smooth_motion`
- Modelle werden je nach Auflösung vorgeglättet (ICON-D2 schwach, ICON-EU/ICON stärker), keine Pixeltreppen mehr
- Wolken werden unabhängig von den Radarbildern zeitbasiert überblendet
- Wiedergabe zeitbasiert (5 min Daten pro `frame_ms`), Einrasten nur auf echte Messzeitpunkte

## 0.7.4
- Fix: ICON-D2-Verzeichnisse enthalten auch Ikosaeder-Dateien – es werden nur noch regular-lat-lon-Dateien verwendet (Fehler „KeyError: Ni“)

## 0.7.3
- Fix: Vorhersage >2 h verschwand – Modellfelder wurden gelöscht, wenn in einem Durchlauf kein neuer Lauf gefunden wurde. Jetzt bleibt der bisherige Lauf aktiv, gelöscht werden nur abgelaufene Felder
- Nach Neustart steht die Modell-Zeitleiste sofort aus den vorhandenen Bildern zur Verfügung

## 0.7.2
- Vollbild-Button oben rechts (über +/−): öffnet das Radar als browser_mod-Popup im Vollbild; im Popup schließt er es wieder. Fallback: Fullscreen-API bzw. neuer Tab

## 0.7.1
- Fehlende Kachelstreifen behoben: Kartengröße wird bei jeder Größenänderung des iframes neu erfasst (ResizeObserver + Kontrolle alle 15 s), fehlende Kacheln werden nachgeladen
- Fehlgeschlagene Kacheln werden bis zu zweimal erneut geladen

## 0.7.0
- Stufenlose Anzeige: Überblendung zwischen benachbarten Bildern passend zur Zeitleisten-Position (Wischen, Schwung, Einrasten, Play)
- Kein Flackern mehr: Bilder werden erst nach dem Dekodieren eingeblendet, bis dahin bleibt das vorherige sichtbar; Zeitpunkte ohne Wolkenbild nutzen das nächstgelegene
- Vorhersage-Bereich der Zeitleiste halb so breit (24 h ≈ bisher 12 h)

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
