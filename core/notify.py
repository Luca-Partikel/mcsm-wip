"""Kleine Brücke zu den Windows-Benachrichtigungen über das Tray-Symbol.

Der Manager läuft im Hintergrund weiter, auch wenn das Fenster zu ist. Wichtige Ereignisse –
vor allem ein Serverabsturz – meldet er deshalb als Desktop-Benachrichtigung, damit man nicht
erst das Fenster öffnen muss. Das Tray-Symbol lebt in app.py; dieses Modul hält nur eine Referenz
darauf, damit auch core-Module (z. B. der Absturz-Watchdog) melden können, ohne app.py zu kennen.
"""
from __future__ import annotations

import threading

_tray = None
_lock = threading.Lock()


def set_tray(tray) -> None:
    """app.py reicht beim Start sein Tray-Symbol herein (oder None, wenn es keins gibt)."""
    global _tray
    with _lock:
        _tray = tray


def send(title: str, message: str) -> None:
    """Eine Benachrichtigung zeigen – scheitert nie, fehlt das Tray, passiert einfach nichts."""
    with _lock:
        tray = _tray
    if tray is None:
        return
    try:
        tray.notify(str(title), str(message))
    except Exception:                                 # noqa: BLE001
        pass
