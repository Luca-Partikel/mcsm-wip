"""Leistungs-Verlauf je Server (im Speicher): TPS, Rechenzeit und Spielerzahl über die Zeit.

Ein Hintergrund-Thread liest, solange das Programm läuft, alle :data:`INTERVAL` Sekunden die
``status.json`` des Begleit-Plugins der laufenden Paper-Server und legt die Werte in einem Ringpuffer
ab. Die Übersicht zeichnet daraus eine kleine Sparkline. Beim Programmende ist der Verlauf weg –
es geht um den „Live"-Blick der letzten Minuten, nicht um eine dauerhafte Aufzeichnung.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from . import companion, manager, store

#: Höchstzahl gespeicherter Punkte je Server (bei INTERVAL=10 s ≈ 30 Minuten).
MAX_SAMPLES = 180
#: Abstand zwischen zwei Messungen in Sekunden.
INTERVAL = 10.0
#: So alt darf status.json höchstens sein, damit ein Punkt zählt.
MAX_AGE = 90

_hist: dict[str, deque] = {}
_lock = threading.Lock()


def _sample_once() -> None:
    for cfg in store.all_servers():
        sid = cfg["id"]
        if not companion.applies(cfg) or not manager.is_running(sid):
            continue
        info = companion.status(cfg)
        data = info.get("status")
        age = info.get("age")
        if not isinstance(data, dict) or age is None or age > MAX_AGE:
            continue
        try:
            punkt = {
                "t": int(time.time()),
                "tps": round(min(20.0, max(0.0, float(data.get("tps") or 0))), 2),
                "mspt": round(max(0.0, float(data.get("mspt") or 0)), 2),
                "online": max(0, int(data.get("online") or 0)),
            }
        except (TypeError, ValueError):
            continue
        with _lock:
            dq = _hist.get(sid)
            if dq is None:
                dq = deque(maxlen=MAX_SAMPLES)
                _hist[sid] = dq
            dq.append(punkt)


def samples(server_id: str) -> list[dict]:
    """Die gespeicherten Messpunkte eines Servers (älteste zuerst)."""
    with _lock:
        dq = _hist.get(server_id)
        return list(dq) if dq else []


def clear(server_id: str) -> None:
    with _lock:
        _hist.pop(server_id, None)


def start() -> None:
    """Den Hintergrund-Sampler starten (einmal beim Programmstart)."""
    def loop() -> None:
        while True:
            try:
                _sample_once()
            except Exception:                         # noqa: BLE001
                pass
            time.sleep(INTERVAL)
    threading.Thread(target=loop, daemon=True, name="leistungsverlauf").start()
