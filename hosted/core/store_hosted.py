"""Ablage der Kontodaten des Hosted-Modus.

Alle Datensätze liegen als JSON unter ``/srv/mcsm/data`` – überschreibbar mit der Umgebungsvariablen
``MCSM_DATA``, damit Tests in einem Temp-Ordner laufen können. Je Datenart eine Datei mit dem
Aufbau ``{"<art>": [ ... ], "saved_at": <unix>}``.

Geschrieben wird immer atomar (``.tmp`` + ``os.replace``) und unter einer gemeinsamen Sperre, damit
sich gleichzeitige Anfragen des Daemons nicht in die Quere kommen. Wer mehrere Dateien in einem Zug
ändern muss (z. B. Einladung einlösen = Konto anlegen + Einladung abhaken), umschließt das mit
``with store_hosted.lock():``.

Eine beschädigte Datei wird **nicht** stillschweigend als „leer“ behandelt – sonst wären bei einem
Lesefehler alle Konten verschwunden und der Dienst würde sie beim nächsten Speichern überschreiben.
Stattdessen gibt es einen ValueError, den der Daemon melden kann.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import secrets
import threading
import time
from typing import Callable, Iterable, TypeVar

try:                                       # nur auf Linux vorhanden – Windows-Tests laufen ohne
    import fcntl
except ImportError:                        # pragma: no cover - Windows
    fcntl = None                           # type: ignore[assignment]

DEFAULT_DATA_DIR = "/srv/mcsm/data"

#: Name der Sperrdatei im Datenordner (schützt gegen einen zweiten Prozess auf denselben Daten).
LOCK_FILE = ".lock"

# Datenart -> Dateiname. Der Schlüssel ist gleichzeitig der Wurzelschlüssel in der JSON-Datei.
KINDS: dict[str, str] = {
    "users": "users.json",
    "invites": "invites.json",
    "passes": "passes.json",
    "instances": "instances.json",
    "sessions": "sessions.json",
}

_lock = threading.RLock()

#: Zustand der Dateisperre: offene Datei und Schachtelungstiefe (beides nur unter ``_lock``).
_flock_state: dict = {"fh": None, "depth": 0, "path": ""}

T = TypeVar("T")


def lock() -> threading.RLock:
    """Die gemeinsame Sperre – für Änderungen, die mehrere Dateien betreffen."""
    return _lock


class _FileLock:
    """Sperre über Prozessgrenzen hinweg (``flock`` auf ``<datenordner>/.lock``).

    Der Faden-RLock reicht nur innerhalb eines Prozesses. Ein Pflegeskript des Betreibers oder ein
    versehentlich zweimal gestarteter Daemon würde sonst gleichzeitig schreiben. Fehlt ``fcntl``
    (Windows im Test) oder lässt sich die Datei nicht anlegen, bleibt es beim Faden-RLock.
    """

    def __enter__(self) -> "_FileLock":
        _lock.acquire()
        if fcntl is None:
            return self
        try:
            wanted = str(data_dir() / LOCK_FILE)
            fh = _flock_state["fh"]
            if fh is None or _flock_state["path"] != wanted:
                if fh is not None and _flock_state["depth"] == 0:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    _flock_state["fh"] = None
                if _flock_state["depth"] == 0:
                    data_dir().mkdir(parents=True, exist_ok=True)
                    fh = open(wanted, "a+b")            # noqa: SIM115 - bleibt absichtlich offen
                    _flock_state["fh"] = fh
                    _flock_state["path"] = wanted
            if _flock_state["depth"] == 0 and _flock_state["fh"] is not None:
                fcntl.flock(_flock_state["fh"].fileno(), fcntl.LOCK_EX)
            _flock_state["depth"] += 1
        except OSError:
            pass                                        # ohne Dateisperre weiter (Faden-RLock hält)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if fcntl is not None and _flock_state["depth"] > 0:
                _flock_state["depth"] -= 1
                if _flock_state["depth"] == 0 and _flock_state["fh"] is not None:
                    try:
                        fcntl.flock(_flock_state["fh"].fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            _lock.release()


def _guard() -> _FileLock:
    """Faden- und Dateisperre in einem – um jedes Lesen und Schreiben."""
    return _FileLock()


def _fsync_dir(path: pathlib.Path) -> None:
    """Den Verzeichniseintrag auf die Platte zwingen (sonst kann ``os.replace`` verloren gehen)."""
    if not hasattr(os, "O_DIRECTORY"):                  # pragma: no cover - Windows kennt das nicht
        return
    try:
        fd = os.open(str(path), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def now() -> int:
    """Aktuelle Zeit in Unix-Sekunden (ganzzahlig, so stehen sie auch in den Dateien)."""
    return int(time.time())


def new_id(nbytes: int = 6) -> str:
    """Zufällige Kennung als Hex-Zeichenkette (Vorgabe 12 Zeichen)."""
    return secrets.token_hex(nbytes)


def data_dir() -> pathlib.Path:
    """Datenordner. Wird bei jedem Aufruf neu aus MCSM_DATA gelesen, damit Tests umschalten können."""
    raw = (os.environ.get("MCSM_DATA") or "").strip()
    return pathlib.Path(raw) if raw else pathlib.Path(DEFAULT_DATA_DIR)


def ensure_dir() -> pathlib.Path:
    """Datenordner anlegen (nur für den Dienstbenutzer lesbar) und zurückgeben."""
    target = data_dir()
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass                      # z. B. Windows im Test – dort gibt es die Rechte so nicht
    return target


def path_of(kind: str) -> pathlib.Path:
    """Dateipfad einer Datenart. Unbekannte Arten werden abgelehnt (kein Pfad aus Benutzereingaben)."""
    if kind not in KINDS:
        raise ValueError(f"Unbekannte Datenart „{kind}“.")
    return data_dir() / KINDS[kind]


#: Zwischenspeicher je Datei: ``pfad -> (marke, text)``. Die Marke ist der Fingerabdruck der
#: Datei (Inode, Größe, Änderungszeit in Nanosekunden) – ändert sich einer der Werte, wird neu
#: gelesen. Gespeichert wird der **Text**, nicht die fertigen Datensätze: so bekommt jeder
#: Aufrufer frische Objekte und kann sie gefahrlos verändern.
_cache: dict = {}
_CACHE_MAX_BYTES = 8 * 1024 * 1024


def _marke(path: pathlib.Path):
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_dev)


def cache_leeren() -> None:
    """Zwischenspeicher wegwerfen (Selbsttests, die die Dateien von Hand austauschen)."""
    with _lock:
        _cache.clear()


def load(kind: str) -> list[dict]:
    """Alle Datensätze einer Art lesen. Fehlende Datei = leere Liste.

    Gelesen wird **ohne** die Dateisperre: ``save`` legt die Datei atomar an die Stelle
    (``os.replace``), ein Leser sieht also immer einen vollständigen Stand. Das ist wichtig,
    weil bei jeder Anfrage das Sitzungstoken aufgelöst werden muss – würde dafür jedes Mal die
    prozessweite Sperre genommen, stünden alle Arbeitsfäden und die Überwachungsschleife
    hintereinander. Ein Fingerabdruck der Datei entscheidet, ob der Inhalt noch gilt.
    """
    with _lock:
        path = path_of(kind)
        schluessel = str(path)
        marke = _marke(path)
        if marke is None:
            _cache.pop(schluessel, None)
            gemerkt = None
        else:
            gemerkt = _cache.get(schluessel)
        if gemerkt is not None and gemerkt[0] == marke:
            text = gemerkt[1]
        else:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    text = fh.read()
            except FileNotFoundError:
                _cache.pop(schluessel, None)
                return []
            except OSError as exc:
                raise ValueError(f"„{path.name}“ kann nicht gelesen werden: {exc}") from exc
            # Erst nach dem Lesen stempeln: ändert sich die Datei währenddessen, gilt der
            # Zwischenspeicher nicht und beim nächsten Mal wird neu gelesen.
            frisch = _marke(path)
            if frisch is not None and len(text) <= _CACHE_MAX_BYTES:
                _cache[schluessel] = (frisch, text)
            else:
                _cache.pop(schluessel, None)
        try:
            raw = json.loads(text)
        except ValueError as exc:
            _cache.pop(schluessel, None)
            raise ValueError(f"„{path.name}“ ist beschädigt und enthält kein gültiges JSON: {exc}") from exc
        records = raw.get(kind) if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            raise ValueError(f"„{path.name}“ hat einen unerwarteten Aufbau – erwartet wird eine Liste "
                             f"unter dem Schlüssel „{kind}“.")
        return [dict(r) for r in records if isinstance(r, dict)]


def save(kind: str, records: Iterable[dict]) -> None:
    """Alle Datensätze einer Art atomar schreiben.

    Der Name der Zwischendatei enthält einen Zufallsanteil: zwei Prozesse auf denselben Daten
    (Pflegeskript, versehentlich zweimal gestarteter Daemon) dürfen sich nicht dieselbe ``.tmp``
    teilen. Nach dem Umbenennen wird auch der Verzeichniseintrag gefsynct, sonst kann ein
    Stromausfall kurz danach die Datei auf den alten Stand zurückfallen lassen.
    """
    with _guard():
        folder = ensure_dir()
        path = path_of(kind)
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
        payload = {kind: [dict(r) for r in records], "saved_at": now()}
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            _cache.pop(str(path), None)             # der Zwischenspeicher gilt jetzt nicht mehr
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        _fsync_dir(folder)


def update(kind: str, change: Callable[[list[dict]], T]) -> T:
    """Datensätze laden, ``change(records)`` darauf anwenden und wieder speichern – alles in einem Zug.

    ``change`` darf die übergebene Liste verändern; der Rückgabewert wird durchgereicht. Löst
    ``change`` eine Ausnahme aus, wird nichts geschrieben. Hat ``change`` nichts geändert, wird
    ebenfalls nicht geschrieben – ein unangemeldetes ``/api/auth/logout`` soll nicht bei jedem
    Aufruf die ganze Datei neu auf die Platte legen.
    """
    with _guard():
        records = load(kind)
        before = copy.deepcopy(records)
        result = change(records)
        if records != before:
            save(kind, records)
        return result


def get_by(kind: str, field: str, value) -> dict | None:
    """Ersten Datensatz finden, dessen Feld genau passt (Kopie, Änderungen wirken nicht)."""
    for record in load(kind):
        if record.get(field) == value:
            return record
    return None


def patch_by(kind: str, field: str, value, changes: dict, missing: str = "") -> dict:
    """Felder eines Datensatzes ändern und die neue Fassung zurückgeben."""
    def apply(records: list[dict]) -> dict:
        for record in records:
            if record.get(field) == value:
                record.update(changes)
                return dict(record)
        raise ValueError(missing or f"Es gibt keinen Eintrag mit {field} = {value!r}.")

    return update(kind, apply)


def remove_by(kind: str, field: str, value) -> int:
    """Alle Datensätze mit passendem Feld löschen. Rückgabe: Anzahl."""
    def apply(records: list[dict]) -> int:
        keep = [r for r in records if r.get(field) != value]
        removed = len(records) - len(keep)
        records[:] = keep
        return removed

    return update(kind, apply)
