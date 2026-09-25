"""Pässe: Laufzeit, gleichzeitig laufende Server und Arbeitsspeicher je Benutzer.

Ein Pass wird vom Betreiber ausgestellt und gehört genau einem Konto (siehe ARCHITEKTUR.md):

* ``kind``            ``local`` (Server, die vom PC hochgeladen werden) oder ``premium``
                      (reine Root-Server, die dauerhaft hier liegen)
* ``days``            Laufzeit in Tagen (1 = Tages-, 7 = Wochen-, 30 = Monatspass)
* ``max_concurrent``  wie viele Server gleichzeitig **eingeschaltet** sein dürfen
* ``ram_total_mb``    Arbeitsspeicher über alle **laufenden** Server zusammen

Das **Anlegen** von Serverinstanzen ist unbegrenzt – begrenzt ist nur das Einschalten. Hat ein Konto
mehrere gültige Pässe, gilt je Grenze der **größte** Wert (eine Summe würde die Maschine überbuchen).
"""
from __future__ import annotations

import re

from . import store_hosted, users

KINDS = ("local", "premium")

DAYS_MIN = 1
DAYS_MAX = 3650
CONCURRENT_MIN = 1
CONCURRENT_MAX = 10
RAM_TOTAL_MIN_MB = 1024
# Die Maschine hat 13 GB; vergeben werden höchstens 10 GB, der Rest bleibt für System und Java-Verwaltung.
RAM_TOTAL_MAX_MB = 10240

# Vorlagen für die Oberfläche des Betreibers (Tages-, Wochen-, Monatspass).
TEMPLATES = {
    "tag": {"days": 1, "max_concurrent": 1, "ram_total_mb": 4096},
    "woche": {"days": 7, "max_concurrent": 2, "ram_total_mb": 6144},
    "monat": {"days": 30, "max_concurrent": 2, "ram_total_mb": 8192},
}


# ----------------------------------------------------------------------- Textbausteine

def gb_text(mb) -> str:
    """Arbeitsspeicher als deutsche Angabe: 8192 -> „8 GB“, 1536 -> „1,5 GB“, 512 -> „512 MB“."""
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        mb = 0
    if mb < 1024:
        return f"{max(0, mb)} MB"
    gb = mb / 1024
    if abs(gb - round(gb)) < 0.005:
        return f"{int(round(gb))} GB"
    return f"{gb:.1f} GB".replace(".", ",")


def _dativ(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular}" if value == 1 else f"{value} {plural}"


def duration_text(seconds) -> str:
    """Zeitspanne für Sätze wie „läuft in … ab“ (Dativ): „3 Tagen 4 Stunden“, „1 Minute“."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 0
    if seconds < 60:
        return "weniger als einer Minute"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts: list[str] = []
    if days:
        parts.append(_dativ(days, "Tag", "Tagen"))
    if hours:
        parts.append(_dativ(hours, "Stunde", "Stunden"))
    if minutes and not days:                 # bei Tagen sind Minuten uninteressant
        parts.append(_dativ(minutes, "Minute", "Minuten"))
    return " ".join(parts) or "weniger als einer Minute"


def kind_text(kind: str) -> str:
    return "Premium-Pass" if kind == "premium" else "Pass"


# ----------------------------------------------------------------------- Prüfungen

def clean_kind(raw) -> str:
    kind = str(raw or "").strip().lower()
    if kind not in KINDS:
        raise ValueError("Die Pass-Art muss „local“ oder „premium“ sein.")
    return kind


def _clean_int(raw, low: int, high: int, what: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{what} muss eine ganze Zahl sein.") from None
    if not low <= value <= high:
        raise ValueError(f"{what} muss zwischen {low} und {high} liegen.")
    return value


# ----------------------------------------------------------------------- Ausstellen und Ändern

def all_passes() -> list[dict]:
    return store_hosted.load("passes")


def get_pass(pass_id: str) -> dict | None:
    return store_hosted.get_by("passes", "id", str(pass_id or ""))


def passes_of(user_id: str) -> list[dict]:
    """Alle Pässe eines Kontos, neueste zuerst."""
    found = [p for p in all_passes() if p.get("user_id") == user_id]
    found.sort(key=lambda p: int(p.get("issued_at") or 0), reverse=True)
    return found


def issue_pass(user_id: str, kind: str, days: int, max_concurrent: int, ram_total_mb: int, *,
               issued_by: str = "", note: str = "", now: int | None = None) -> dict:
    """Pass ausstellen. Die Laufzeit beginnt sofort."""
    kind = clean_kind(kind)
    days = _clean_int(days, DAYS_MIN, DAYS_MAX, "Die Laufzeit in Tagen")
    max_concurrent = _clean_int(max_concurrent, CONCURRENT_MIN, CONCURRENT_MAX,
                                "Die Zahl der gleichzeitig laufenden Server")
    ram_total_mb = _clean_int(ram_total_mb, RAM_TOTAL_MIN_MB, RAM_TOTAL_MAX_MB,
                              "Der Arbeitsspeicher in MB")
    stamp = store_hosted.now() if now is None else int(now)

    with store_hosted.lock():
        owner = users.get_user(user_id)
        if not owner:
            raise ValueError(f"Es gibt kein Konto mit der Kennung „{user_id}“.")

        def apply(records: list[dict]) -> dict:
            entry = {
                "id": store_hosted.new_id(),
                "user_id": owner["id"],
                "kind": kind,
                "days": days,
                "max_concurrent": max_concurrent,
                "ram_total_mb": ram_total_mb,
                "issued_at": stamp,
                "expires_at": stamp + days * 86400,
                "revoked_at": 0,
                "issued_by": str(issued_by or ""),
                "note": re.sub(r"[\x00-\x1f]", " ", str(note or "")).strip()[:120],
            }
            records.append(entry)
            return dict(entry)

        return store_hosted.update("passes", apply)


def revoke_pass(pass_id: str, *, now: int | None = None) -> dict:
    """Pass vorzeitig zurückziehen. Die betroffenen Server stoppt der Daemon mit Vorwarnung."""
    stamp = store_hosted.now() if now is None else int(now)
    with store_hosted.lock():
        entry = get_pass(pass_id)
        if not entry:
            raise ValueError(f"Es gibt keinen Pass mit der Kennung „{pass_id}“.")
        if entry.get("revoked_at"):
            return entry
        return store_hosted.patch_by("passes", "id", pass_id, {"revoked_at": stamp})


def extend_pass(pass_id: str, days: int, *, now: int | None = None) -> dict:
    """Pass verlängern. Ein bereits abgelaufener Pass läuft ab jetzt weiter."""
    days = _clean_int(days, DAYS_MIN, DAYS_MAX, "Die Verlängerung in Tagen")
    stamp = store_hosted.now() if now is None else int(now)
    with store_hosted.lock():
        entry = get_pass(pass_id)
        if not entry:
            raise ValueError(f"Es gibt keinen Pass mit der Kennung „{pass_id}“.")
        if entry.get("revoked_at"):
            raise ValueError("Ein zurückgezogener Pass kann nicht verlängert werden – "
                             "bitte einen neuen ausstellen.")
        base = max(int(entry.get("expires_at") or 0), stamp)
        changes = {
            "expires_at": base + days * 86400,
            "days": int(entry.get("days") or 0) + days,
        }
        return store_hosted.patch_by("passes", "id", pass_id, changes)


def set_limits(pass_id: str, *, max_concurrent: int | None = None,
               ram_total_mb: int | None = None) -> dict:
    """Grenzen eines laufenden Passes anpassen (z. B. Kunde bucht auf)."""
    changes: dict = {}
    if max_concurrent is not None:
        changes["max_concurrent"] = _clean_int(max_concurrent, CONCURRENT_MIN, CONCURRENT_MAX,
                                               "Die Zahl der gleichzeitig laufenden Server")
    if ram_total_mb is not None:
        changes["ram_total_mb"] = _clean_int(ram_total_mb, RAM_TOTAL_MIN_MB, RAM_TOTAL_MAX_MB,
                                            "Der Arbeitsspeicher in MB")
    if not changes:
        raise ValueError("Es wurde keine Grenze übergeben, die geändert werden soll.")
    with store_hosted.lock():
        if not get_pass(pass_id):
            raise ValueError(f"Es gibt keinen Pass mit der Kennung „{pass_id}“.")
        return store_hosted.patch_by("passes", "id", pass_id, changes)


def delete_pass(pass_id: str) -> None:
    """Pass ganz aus der Ablage entfernen (nur zum Aufräumen alter Einträge)."""
    if store_hosted.remove_by("passes", "id", pass_id) == 0:
        raise ValueError(f"Es gibt keinen Pass mit der Kennung „{pass_id}“.")


# ----------------------------------------------------------------------- Gültigkeit

def pass_state(entry: dict, *, now: int | None = None) -> str:
    """Zustand eines Passes: ``active``, ``expired`` oder ``revoked``."""
    stamp = store_hosted.now() if now is None else int(now)
    if entry.get("revoked_at"):
        return "revoked"
    if stamp >= int(entry.get("expires_at") or 0):
        return "expired"
    return "active"


def is_active(entry: dict, *, now: int | None = None) -> bool:
    return pass_state(entry, now=now) == "active"


def remaining_seconds(entry: dict, *, now: int | None = None) -> int:
    """Restzeit in Sekunden (0, wenn abgelaufen oder zurückgezogen)."""
    if entry.get("revoked_at"):
        return 0
    stamp = store_hosted.now() if now is None else int(now)
    return max(0, int(entry.get("expires_at") or 0) - stamp)


def remaining_text(entry: dict, *, now: int | None = None) -> str:
    """Restzeit als deutscher Satz für die Oberfläche."""
    state = pass_state(entry, now=now)
    if state == "revoked":
        return "zurückgezogen"
    if state == "expired":
        return "abgelaufen"
    return f"läuft in {duration_text(remaining_seconds(entry, now=now))} ab"


def active_passes(user_id: str, *, now: int | None = None, kind: str = "") -> list[dict]:
    """Gültige Pässe eines Kontos, der am spätesten ablaufende zuerst."""
    stamp = store_hosted.now() if now is None else int(now)
    found = [p for p in passes_of(user_id) if is_active(p, now=stamp)]
    if kind:
        found = [p for p in found if p.get("kind") == clean_kind(kind)]
    found.sort(key=lambda p: int(p.get("expires_at") or 0), reverse=True)
    return found


def allows_origin(entry: dict, origin: str) -> bool:
    """Deckt dieser Pass eine Instanz dieser Herkunft ab?

    Ein Premium-Pass deckt beides ab (er ist der größere), ein lokaler Pass nur hochgeladene Server.
    """
    if entry.get("kind") == "premium":
        return True
    return str(origin or "local") != "premium"


def has_premium(user_id: str, *, now: int | None = None) -> bool:
    return bool(active_passes(user_id, now=now, kind="premium"))


def effective_limits(user_id: str, *, now: int | None = None) -> dict:
    """Wirksame Grenzen eines Kontos: je Grenze der **größte** Wert aller gültigen Pässe.

    Rückgabe (auch ohne Pass, dann alles 0/leer):
    ``max_concurrent``, ``ram_total_mb``, ``premium`` (Premium-Instanzen erlaubt),
    ``pass_ids``, ``pass_count``, ``expires_at`` (spätestes Ende), ``next_expiry`` (nächstes Ende).
    """
    stamp = store_hosted.now() if now is None else int(now)
    active = active_passes(user_id, now=stamp)
    limits = {
        "max_concurrent": 0,
        "ram_total_mb": 0,
        "premium": False,
        "pass_ids": [],
        "pass_count": len(active),
        "expires_at": 0,
        "next_expiry": 0,
    }
    for entry in active:
        limits["max_concurrent"] = max(limits["max_concurrent"], int(entry.get("max_concurrent") or 0))
        limits["ram_total_mb"] = max(limits["ram_total_mb"], int(entry.get("ram_total_mb") or 0))
        if entry.get("kind") == "premium":
            limits["premium"] = True
        limits["pass_ids"].append(entry.get("id", ""))
        ends = int(entry.get("expires_at") or 0)
        limits["expires_at"] = max(limits["expires_at"], ends)
        limits["next_expiry"] = ends if not limits["next_expiry"] else min(limits["next_expiry"], ends)
    return limits


def limits_text(limits: dict) -> str:
    """Grenzen als deutscher Satzteil für Meldungen und Oberfläche."""
    count = int(limits.get("max_concurrent") or 0)
    if not count:
        return "kein gültiger Pass"
    server = "1 gleichzeitig laufender Server" if count == 1 else f"{count} gleichzeitig laufende Server"
    return f"{server}, zusammen {gb_text(limits.get('ram_total_mb'))}"


# ----------------------------------------------------------------------- Ablauf überwachen

def expiring_soon(minutes: int = 10, *, now: int | None = None) -> list[dict]:
    """Gültige Pässe, die in den nächsten ``minutes`` Minuten ablaufen – für die 10-Minuten-Warnung.

    Der am frühesten ablaufende Pass steht vorn.
    """
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("Die Vorwarnzeit muss eine ganze Zahl von Minuten sein.") from None
    if minutes < 0:
        raise ValueError("Die Vorwarnzeit darf nicht negativ sein.")
    stamp = store_hosted.now() if now is None else int(now)
    limit = stamp + minutes * 60
    found = [p for p in all_passes()
             if not p.get("revoked_at") and stamp < int(p.get("expires_at") or 0) <= limit]
    found.sort(key=lambda p: int(p.get("expires_at") or 0))
    return found


def newly_expired(since: int, *, now: int | None = None) -> list[dict]:
    """Pässe, die seit ``since`` abgelaufen oder zurückgezogen wurden (für die Überwachungsschleife)."""
    stamp = store_hosted.now() if now is None else int(now)
    since = int(since)
    out = []
    for entry in all_passes():
        revoked = int(entry.get("revoked_at") or 0)
        if revoked:
            if since < revoked <= stamp:
                out.append(entry)
            continue
        ends = int(entry.get("expires_at") or 0)
        if since < ends <= stamp:
            out.append(entry)
    out.sort(key=lambda p: int(p.get("revoked_at") or p.get("expires_at") or 0))
    return out


def public_pass(entry: dict, *, now: int | None = None) -> dict:
    """Pass für die API: Datensatz plus Zustand und Restzeit im Klartext."""
    stamp = store_hosted.now() if now is None else int(now)
    return {
        "id": entry.get("id", ""),
        "user_id": entry.get("user_id", ""),
        "kind": entry.get("kind", "local"),
        "kind_text": kind_text(entry.get("kind", "local")),
        "days": int(entry.get("days") or 0),
        "max_concurrent": int(entry.get("max_concurrent") or 0),
        "ram_total_mb": int(entry.get("ram_total_mb") or 0),
        "ram_total_text": gb_text(entry.get("ram_total_mb")),
        "issued_at": int(entry.get("issued_at") or 0),
        "expires_at": int(entry.get("expires_at") or 0),
        "revoked_at": int(entry.get("revoked_at") or 0),
        "note": entry.get("note", "") or "",
        "state": pass_state(entry, now=stamp),
        "remaining_seconds": remaining_seconds(entry, now=stamp),
        "remaining_text": remaining_text(entry, now=stamp),
    }
