"""Reporting to Home Assistant via the Supervisor proxy (status sensor + HA log)."""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("dwd_radar.ha")

API = "http://supervisor/core/api"
TOKEN = os.environ.get("SUPERVISOR_TOKEN")
ENTITY = "sensor.dwd_radar_status"


class HA:
    def __init__(self) -> None:
        self.s = requests.Session()
        if TOKEN:
            self.s.headers["Authorization"] = f"Bearer {TOKEN}"
        self._last_err_msg: str | None = None
        self._last_err_ts = 0.0

    def _post(self, path: str, payload: dict) -> None:
        if not TOKEN:
            return
        try:
            r = self.s.post(f"{API}{path}", json=payload, timeout=10)
            if r.status_code >= 400:
                log.debug("HA API %s -> %s %s", path, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.debug("HA API %s nicht erreichbar: %s", path, e)

    def status(self, state: str, **attrs) -> None:
        attrs.setdefault("friendly_name", "DWD Regenradar Status")
        attrs.setdefault("icon", "mdi:radar")
        self._post(f"/states/{ENTITY}", {"state": state, "attributes": attrs})

    def log(self, level: str, message: str, repeat_after_s: float = 1800) -> None:
        """Write into the HA system log (Einstellungen > System > Protokolle).

        The same message is only repeated every `repeat_after_s` seconds.
        """
        now = time.time()
        if message == self._last_err_msg and now - self._last_err_ts < repeat_after_s:
            return
        self._last_err_msg, self._last_err_ts = message, now
        self._post("/services/system_log/write",
                   {"message": f"DWD Regenradar: {message}", "level": level, "logger": "dwd_radar"})

    def clear_error(self) -> None:
        if self._last_err_msg is not None:
            self._post("/services/system_log/write",
                       {"message": "DWD Regenradar: wieder Daten verfügbar", "level": "info",
                        "logger": "dwd_radar"})
        self._last_err_msg = None
