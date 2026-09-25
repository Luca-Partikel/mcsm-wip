#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcsmd – der Hosted-Daemon des Minecraft Server Managers.

Auf dem Root-Server läuft nur diese API; bedient wird sie vom Programm auf dem PC.
Alles kommt aus der Standardbibliothek (Python 3.11), genau wie im lokalen Programm.

* Bindung: 127.0.0.1:8765 (nginx davor), überschreibbar mit ``MCSM_BIND`` und ``MCSM_PORT``.
* Anmeldung: Einladungscode oder Discord (``core/oauth.py``) → Sitzungstoken, danach
  ``Authorization: Bearer <token>`` oder das Cookie ``mcsm_sitzung`` (Verwaltung im Browser).
  Das **erste** über Discord angemeldete Konto wird Betreiber; danach braucht jeder Neue einen
  Einladungscode.
* Zwei Hostnamen, am ``Host``-Kopf unterschieden: unter ``admin.<domain>`` wird zusätzlich die
  Betreiberoberfläche aus ``web/`` ausgeliefert, unter ``api.<domain>`` **nur** die API.
* Daten: ``/srv/mcsm`` (``MCSM_DATA_ROOT``), Zugriffsprotokoll ``/var/log/mcsm/api.log``
  (``MCSM_LOG``). Im Protokoll steht **nie** ein Token.
* Jede Java-Instanz hat eine dauerhafte Unterdomäne; der Daemon schreibt daraus
  ``/srv/mcsm/data/routes.json`` für den Verteiler (``core/routes.py``).
* Hintergrundarbeit: jede Minute Pässe prüfen (10-Minuten-Warnung, sauber stoppen, parken,
  ruhende Server mit neuem Pass wieder startbar machen), jede Stunde aufräumen (Sitzungen,
  liegengebliebene Übertragungen, Ordnergrößen).
* Ein Neustart des Dienstes ist **keine** Zwangsunterbrechung: laufende Server bleiben an und
  werden beim nächsten Start übernommen (``adopt_running``).

Start von Hand (Selbsttest):  ``MCSM_DATA_ROOT=/srv/mcsm python3 mcsmd.py``
"""
from __future__ import annotations

import hashlib
import http.cookies
import http.server
import importlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse

VERSION = "1.0.0"

# --------------------------------------------------------------------------- core laden
#
# hosted/core wird unter einem eigenen Paketnamen geladen: Im Projektordner des PC-Programms
# liegt ein gleichnamiger Ordner core/, der bei einem schlichten ``import core`` zuerst
# gefunden würde. Auf dem Server (/opt/mcsm) wäre das egal, in der Entwicklung nicht.

_HERE = pathlib.Path(__file__).resolve().parent
_PKG = "mcsm_core"
_MODULES = ("store_hosted", "users", "passes", "instances",
            "paths", "transfer", "ports", "isolation", "runner", "sources_linux",
            "oauth", "routes", "companion")


def _load_core() -> dict:
    if _PKG not in sys.modules:
        spec = importlib.machinery.ModuleSpec(_PKG, None, is_package=True)
        pkg = importlib.util.module_from_spec(spec)
        pkg.__path__ = [str(_HERE / "core")]            # type: ignore[attr-defined]
        sys.modules[_PKG] = pkg
    return {name: importlib.import_module(f"{_PKG}.{name}") for name in _MODULES}


_core = _load_core()
store_hosted = _core["store_hosted"]
users = _core["users"]
passes = _core["passes"]
instances = _core["instances"]
paths = _core["paths"]
transfer = _core["transfer"]
ports = _core["ports"]
runner = _core["runner"]
isolation = _core["isolation"]
sources_linux = _core["sources_linux"]
oauth = _core["oauth"]
#: Unterdomänen und die Zuordnungstabelle des Verteilers. Absichtlich nicht ``routes`` –
#: der Name wäre neben der Routenliste ``ROUTES`` dieser Datei zu leicht zu verwechseln.
routen = _core["routes"]
companion = _core["companion"]

# --------------------------------------------------------------------------- Einstellungen

MAX_JSON_BYTES = 1_000_000                  # größere Anfragekörper braucht keine Route
UNAUTH_LIMIT = 64 * 1024                    # ohne gültige Anmeldung wird nur so viel eingelesen
MAX_WORKERS = 32                            # so viele Anfragen werden gleichzeitig bearbeitet
CHUNK_LIMIT = transfer.MAX_CHUNK_BYTES      # 16 MB, wie im Übertragungsmodul
TICK_SECONDS = 60                           # Takt der Überwachungsschleife
WARN_MINUTES = 10                           # so früh wird der Ablauf angekündigt
HOUSEKEEPING_SECONDS = 3600                 # Sitzungen, Übertragungen, Größen
SAVE_ALL_SECONDS = 300                      # so oft bekommen laufende Server ein `save-all`
STOP_TIMEOUT = 90                           # so lange darf ein Server zum Speichern brauchen
SESSION_NOTE_MAX = 60
RUNTIME_FILE = "daemon.json"                # eigene Laufzeitablage des Daemons
ADMIN_FILE = "ERSTER-ADMIN.txt"
DEFAULT_MACHINE_RAM_MB = passes.RAM_TOTAL_MAX_MB      # 10 GB dürfen insgesamt laufen
MACHINE_RAM_RESERVE_MB = 512                # so viel Arbeitsspeicher bleibt frei
WOKEN_KEEP_SECONDS = 14 * 86400             # so lange bleibt „wieder startbar“ als Hinweis stehen

# --------------------------------------------------------------------------- Oberfläche
#: Die drei Dateien der Betreiberoberfläche. Es werden **nur** diese Namen bedient – kein Pfad
#: wird zusammengesetzt, kein Verzeichnis durchgereicht, also gibt es auch keinen Ausbruch.
WEB_DIR = _HERE / "web"
WEB_FILES = {
    "admin.html": "text/html; charset=utf-8",
    "admin.css": "text/css; charset=utf-8",
    "admin.js": "application/javascript; charset=utf-8",
}
COOKIE_NAME = "mcsm_sitzung"                # Sitzung der Verwaltung im Browser

# --------------------------------------------------------------------------- Ratenbremse
#: (Versuche, Fenster in Sekunden, Sperre in Sekunden) je Herkunfts-IP.
#: Streng bei allem, was mit Anmelden und Einladungscodes zu tun hat: dort lohnt Raten.
#: Großzügig bei angemeldeten Aufrufen – das Programm auf dem PC fragt beim Hochladen und beim
#: Lesen der Konsole viel, und ein Betreiber hat die Oberfläche mit 5-Sekunden-Takt offen.
RATE_LIMITS = {
    "anmeldung": (12, 60, 300),
    "abholen": (150, 60, 60),               # das Programm fragt alle 2 Sekunden nach dem Token
    "angemeldet": (900, 60, 60),
    "offen": (120, 60, 120),
    # Abgewiesene Anmeldungen (abgelaufenes Token). Absichtlich milder als „anmeldung“: wessen
    # Sitzung gerade abgelaufen ist, schickt schnell ein paar Anfragen zu viel und soll sich
    # danach trotzdem sofort wieder anmelden können. Ein Sitzungstoken zu raten ist aussichtslos
    # (32 Byte Zufall), hier geht es nur gegen Lärm.
    "abgewiesen": (60, 60, 60),
    # Vorab-Bremse: greift **bevor** das Sitzungstoken aufgelöst wird. Dafür müssen
    # sessions.json und users.json gelesen werden; ohne diese Grenze kostet jede Anfrage mit
    # erfundenem Token diese Arbeit, auch wenn sie gleich danach als „abgewiesen“ gezählt und
    # verworfen wird. Der Wert liegt bewusst über „angemeldet“ (900), damit ein ehrlicher
    # Aufrufer hier nie anstößt.
    "vorab": (1200, 60, 60),
}
RATE_MAX_KEYS = 20000                       # Obergrenze, damit eine Flut den Speicher nicht füllt


def bind_host() -> str:
    return (os.environ.get("MCSM_BIND") or "127.0.0.1").strip()


def bind_port() -> int:
    try:
        return int(os.environ.get("MCSM_PORT") or 8765)
    except ValueError:
        return 8765


def machine_ram_mb() -> int:
    try:
        return max(1024, int(os.environ.get("MCSM_MACHINE_RAM_MB") or DEFAULT_MACHINE_RAM_MB))
    except ValueError:
        return DEFAULT_MACHINE_RAM_MB


def log_path() -> pathlib.Path:
    raw = (os.environ.get("MCSM_LOG") or "").strip()
    return pathlib.Path(raw) if raw else pathlib.Path("/var/log/mcsm/api.log")


def log_to_stdout() -> bool:
    return (os.environ.get("MCSM_LOG_STDOUT") or "1").strip() not in ("0", "nein", "no", "false")


def data_root() -> pathlib.Path:
    """Datenwurzel (MCSM_DATA_ROOT). Wird bei jedem Aufruf gelesen, damit Tests umschalten
    können; ``startup`` gibt den Wert an sources_linux weiter."""
    raw = (os.environ.get("MCSM_DATA_ROOT") or "").strip()
    return pathlib.Path(raw) if raw else pathlib.Path("/srv/mcsm")


def admin_host() -> str:
    """Hostname, unter dem die Betreiberoberfläche ausgeliefert wird."""
    raw = (os.environ.get("MCSM_ADMIN_HOST") or "").strip().lower()
    return raw or f"admin.{routen.domain()}"


def api_host() -> str:
    """Hostname, unter dem **nur** die API antwortet (keine Oberfläche)."""
    raw = (os.environ.get("MCSM_API_HOST") or "").strip().lower()
    return raw or f"api.{routen.domain()}"


def stop_servers_on_exit() -> bool:
    """Sollen beim Beenden des Dienstes auch die Kundenserver gestoppt werden?

    Vorgabe **nein**: ein Update des Dienstes ist dann keine Zwangsunterbrechung – die Server
    laufen in eigener Sitzung weiter und werden beim nächsten Start übernommen. Für eine geplante
    Wartung („Maschine geht aus“) setzt der Betreiber ``MCSM_STOP_SERVERS=1``.
    """
    return (os.environ.get("MCSM_STOP_SERVERS") or "0").strip() in ("1", "ja", "yes", "true")


# --------------------------------------------------------------------------- Protokoll

_log_lock = threading.Lock()
_log_state: dict = {"path": None, "stream": None, "warned": False}


def _log_raw(row: str) -> None:
    target = str(log_path())
    with _log_lock:
        if _log_state["path"] != target:
            old = _log_state.get("stream")
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            stream = None
            try:
                pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
                stream = open(target, "a", encoding="utf-8")
            except OSError as exc:
                if not _log_state["warned"]:
                    print(f"[mcsmd] Das Protokoll {target} ist nicht beschreibbar ({exc}) – "
                          f"es wird nur nach journalctl geschrieben.", file=sys.stderr, flush=True)
                    _log_state["warned"] = True
            _log_state["path"] = target
            _log_state["stream"] = stream
        stream = _log_state.get("stream")
        if stream is not None:
            try:
                stream.write(row + "\n")
                stream.flush()
            except OSError:
                _log_state["stream"] = None
    if log_to_stdout():
        print(row, flush=True)


def close_log() -> None:
    """Protokolldatei schließen (Selbsttests, Neustart des Protokolls)."""
    with _log_lock:
        stream = _log_state.get("stream")
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        _log_state["stream"] = None
        _log_state["path"] = None


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_SECRET_PARAM = re.compile(r"((?:token|code|session)=)[^&]*", re.IGNORECASE)
# Einladungscodes in der Anzeigeform XXXX-XXXX-XXXX: die stehen nicht in einer Abfragezeichenkette
# und würden sonst im Klartext im Protokoll und damit auch im systemd-Journal landen.
_SECRET_CODE = re.compile(r"\b[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}\b")


def scrub(text: str) -> str:
    """Geheimnisse aus einer Zeile entfernen, bevor sie ins Protokoll geht."""
    return _SECRET_CODE.sub("…", _SECRET_PARAM.sub(r"\1…", str(text)))


def log_event(text: str) -> None:
    _log_raw(f"{_stamp()} [dienst] {scrub(text)}")


def log_access(client: str, method: str, path: str, status: int, millis: int,
               user_id: str = "", extra: str = "") -> None:
    who = user_id or "-"
    tail = f" {scrub(extra)}" if extra else ""
    _log_raw(f"{_stamp()} [zugriff] {client} {method} {scrub(path)} -> {status} "
             f"{millis}ms konto={who}{tail}")


# --------------------------------------------------------------------------- Laufzeitablage

_runtime_lock = threading.RLock()
_runtime: dict = {"version": 1, "ports": [], "warned": {}, "last_expiry_check": 0,
                  "housekeeping_at": 0, "save_all_at": 0, "admin_invite": "", "started_at": 0,
                  # {instanzkennung: zeitpunkt} – „ruht nicht mehr, kann wieder gestartet werden“.
                  # Der Hinweis muss den Neustart des Dienstes überleben, deshalb steht er hier.
                  "woken": {},
                  # Letzter Stand der Zuordnungstabelle, nur damit nicht jede Minute dieselbe
                  # Zeile im Protokoll steht.
                  "routen_stand": None}


def runtime_path() -> pathlib.Path:
    return store_hosted.data_dir() / RUNTIME_FILE


def write_private_json(path: pathlib.Path, data) -> None:
    """JSON atomar und nur für den Dienstbenutzer lesbar ablegen.

    Zufälliger Name der Zwischendatei (zwei Prozesse teilen sich keine ``.tmp``) und ein fsync
    auf das Verzeichnis: sonst kann der Verzeichniseintrag bei einem Stromausfall kurz nach dem
    Umbenennen noch fehlen und die Datei fällt auf den alten Stand zurück.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if hasattr(os, "O_DIRECTORY"):
        try:
            fd = os.open(str(path.parent), os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def runtime_load() -> None:
    path = runtime_path()
    with _runtime_lock:
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log_event(f"Die Laufzeitablage {path.name} ist unbrauchbar ({exc}) – "
                      f"es wird mit leeren Werten weitergearbeitet.")
            return
        if isinstance(raw, dict):
            for key in ("ports", "warned", "last_expiry_check", "housekeeping_at",
                        "save_all_at", "admin_invite", "started_at", "woken"):
                if key in raw:
                    _runtime[key] = raw[key]


def runtime_save() -> None:
    with _runtime_lock:
        try:
            write_private_json(runtime_path(), dict(_runtime))
        except OSError as exc:
            log_event(f"Die Laufzeitablage konnte nicht geschrieben werden: {exc}")


def save_ports() -> None:
    with _runtime_lock:
        _runtime["ports"] = POOL.snapshot()
    runtime_save()


# --------------------------------------------------------------------------- Ratenbremse

class Bremse:
    """Ratenbremse je Herkunfts-IP und Zweck.

    Die API hängt am offenen Internet. Ohne Bremse kann jemand Einladungscodes und Abholcodes
    durchprobieren oder den Dienst mit Anfragen belegen. Gezählt wird in einem gleitenden
    Fenster; wer darüber kommt, bekommt eine Sperre mit Wartezeit und HTTP 429.

    Absichtlich nur im Arbeitsspeicher: ein Neustart löscht die Zähler, dafür wird keine
    Herkunfts-IP auf Platte geschrieben (Datensparsamkeit). Die Obergrenze
    ``RATE_MAX_KEYS`` verhindert, dass eine Flut aus vielen Adressen den Speicher füllt.
    """

    def __init__(self) -> None:
        self._eintraege: dict = {}
        self._lock = threading.Lock()

    def pruefen(self, ip: str, gruppe: str, *, now: float | None = None) -> tuple:
        """``(wartezeit, neu_gesperrt)`` – Wartezeit 0 heißt durchlassen."""
        grenze, fenster, sperre = RATE_LIMITS.get(gruppe, RATE_LIMITS["offen"])
        jetzt = time.time() if now is None else float(now)
        key = (str(ip or "-"), str(gruppe))
        with self._lock:
            if len(self._eintraege) > RATE_MAX_KEYS:
                self._aufraeumen(jetzt, fenster)
            eintrag = self._eintraege.get(key)
            if eintrag is None:
                eintrag = {"zeiten": [], "bis": 0.0}
                self._eintraege[key] = eintrag
            if eintrag["bis"] > jetzt:
                return max(1, int(eintrag["bis"] - jetzt)), False
            zeiten = [t for t in eintrag["zeiten"] if t > jetzt - fenster]
            zeiten.append(jetzt)
            eintrag["zeiten"] = zeiten
            if len(zeiten) > grenze:
                eintrag["bis"] = jetzt + sperre
                eintrag["zeiten"] = []
                return int(sperre), True
            return 0, False

    def freigeben(self, ip: str, gruppe: str) -> None:
        """Zähler zurücksetzen – nach einem erfolgreichen Anmelden ist niemand verdächtig."""
        with self._lock:
            self._eintraege.pop((str(ip or "-"), str(gruppe)), None)

    def _aufraeumen(self, jetzt: float, fenster: int) -> None:
        for key, eintrag in list(self._eintraege.items()):
            if eintrag["bis"] > jetzt:
                continue
            if not [t for t in eintrag["zeiten"] if t > jetzt - max(fenster, 60)]:
                self._eintraege.pop(key, None)

    def offen(self) -> int:
        with self._lock:
            return len(self._eintraege)

    def leeren(self) -> None:
        with self._lock:
            self._eintraege.clear()


BREMSE = Bremse()


def host_of(handler) -> str:
    """Der angefragte Hostname ohne Port, klein geschrieben (aus dem ``Host``-Kopf)."""
    raw = (handler.headers.get("Host", "") or "").strip().lower()
    if raw.startswith("["):                                     # [::1]:8765
        return raw.split("]", 1)[0].lstrip("[")
    return raw.split(":", 1)[0]


def _ist_loopback(ip: str) -> bool:
    text = str(ip or "")
    return text in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or text.startswith("127.")


def client_ip(handler) -> str:
    """Die Herkunftsadresse des Aufrufers.

    nginx sitzt auf derselben Maschine davor, deshalb steht in ``client_address`` immer
    ``127.0.0.1``. Die echte Adresse kommt aus ``X-Forwarded-For`` bzw. ``X-Real-IP`` – aber
    **nur**, wenn die Verbindung wirklich von der Rückschleife kommt. Sonst könnte jeder sich
    mit einer erfundenen Kopfzeile eine eigene Bremse aussuchen. nginx hängt die echte Adresse
    hinten an (``$proxy_add_x_forwarded_for``), also gilt der **letzte** Eintrag.
    """
    peer = handler.client_address[0] if handler.client_address else ""
    if _ist_loopback(peer):
        roh = handler.headers.get("X-Forwarded-For", "") or ""
        teile = [t.strip() for t in roh.split(",") if t.strip()]
        if teile:
            return teile[-1][:64]
        real = (handler.headers.get("X-Real-IP", "") or "").strip()
        if real:
            return real[:64]
        # Kein Kopf da: nginx gibt die Adresse nicht weiter. Dann teilen sich alle Aufrufer
        # denselben Zähler – deshalb ein eigener Schlüssel und eine einmalige Klage im Protokoll
        # (die strengen Grenzen würden sonst gemeinsam alle aussperren).
        return "@proxy"
    return peer or "-"


_proxy_warned = {"done": False}


def warn_missing_forwarded(ip: str) -> None:
    """Einmal melden, wenn nginx die Herkunftsadresse nicht weitergibt."""
    if ip != "@proxy" or _proxy_warned["done"]:
        return
    _proxy_warned["done"] = True
    log_event("Achtung: nginx gibt die Herkunftsadresse nicht weiter – die Ratenbremse kann "
              "einzelne Aufrufer nicht unterscheiden. Bitte im Server-Block zwei Zeilen "
              "ergänzen: „proxy_set_header X-Real-IP $remote_addr;“ und „proxy_set_header "
              "X-Forwarded-For $proxy_add_x_forwarded_for;“.")


def bremsgruppe(path: str) -> str:
    """Welche Bremse für diesen Pfad gilt (leer = erst nach der Anmeldung entscheiden)."""
    if path.startswith(("/api/auth/discord/poll", "/auth/discord/poll")):
        return "abholen"
    if path.startswith(("/api/auth/", "/auth/")):
        return "anmeldung"
    return ""


# --------------------------------------------------------------------------- Anmeldevorgänge

#: Offene Discord-Anmeldungen, Schlüssel ist der Zustandswert von ``oauth.start``.
#: Nur im Arbeitsspeicher – ein Neustart macht laufende Anmeldungen ungültig (mit klarer Meldung).
_LOGIN_LOCK = threading.Lock()
_LOGINS: dict = {}
LOGIN_MAX = 200
LOGIN_FEHLVERSUCHE_MAX = 5


def _login_anlegen(state: str, *, flow: str, ziel: str = "", device: str = "",
                   secret: str = "", browser: str = "", now: int | None = None) -> None:
    stamp = store_hosted.now() if now is None else int(now)
    eintrag = {"flow": flow, "ziel": ziel, "device": device, "fehler": "", "ergebnis": None,
               "expires_at": stamp + oauth.STATE_TTL_SECONDS, "versuche": 0,
               "hash": hashlib.sha256(secret.encode("utf-8")).hexdigest() if secret else "",
               # Merkmal des Browsers, der diese Anmeldung begonnen hat (siehe `_browser_passt`).
               "browser": hashlib.sha256(browser.encode("utf-8")).hexdigest() if browser else ""}
    with _LOGIN_LOCK:
        for key, alt in list(_LOGINS.items()):
            if int(alt.get("expires_at") or 0) <= stamp:
                _LOGINS.pop(key, None)
        while len(_LOGINS) >= LOGIN_MAX:
            aeltester = min(_LOGINS, key=lambda k: _LOGINS[k].get("expires_at") or 0)
            _LOGINS.pop(aeltester, None)
        _LOGINS[str(state)] = eintrag


def _login_lesen(state: str) -> dict:
    with _LOGIN_LOCK:
        eintrag = _LOGINS.get(str(state))
        return dict(eintrag) if eintrag else {}


def _login_setzen(state: str, *, ergebnis: dict | None = None, fehler: str = "") -> bool:
    """Ergebnis der Rückleitung am Vorgang hinterlegen. False, wenn es den Vorgang nicht gibt."""
    with _LOGIN_LOCK:
        eintrag = _LOGINS.get(str(state))
        if eintrag is None:
            return False
        eintrag["ergebnis"] = ergebnis
        eintrag["fehler"] = str(fehler or "")
        return True


def _login_abholen(state: str, secret: str, *, now: int | None = None) -> dict:
    """Weg A des PC-Programms: Token gegen Zustandswert **und** Geheimnis, genau einmal."""
    stamp = store_hosted.now() if now is None else int(now)
    with _LOGIN_LOCK:
        eintrag = _LOGINS.get(str(state))
        if eintrag is None or int(eintrag.get("expires_at") or 0) <= stamp:
            _LOGINS.pop(str(state), None)
            raise ApiError("Diese Anmeldung ist abgelaufen, wurde schon abgeschlossen oder gehört "
                           "nicht zu diesem Server. Bitte die Anmeldung erneut starten.", 403)
        erwartet = str(eintrag.get("hash") or "")
        gegeben = hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()
        if not erwartet or not secrets.compare_digest(erwartet, gegeben):
            eintrag["versuche"] = int(eintrag.get("versuche") or 0) + 1
            if eintrag["versuche"] >= LOGIN_FEHLVERSUCHE_MAX:
                _LOGINS.pop(str(state), None)
            raise ApiError("Diese Anmeldung gehört nicht zu diesem Programm. Bitte die Anmeldung "
                           "erneut starten.", 403)
        if eintrag.get("fehler"):
            text = str(eintrag["fehler"])
            _LOGINS.pop(str(state), None)
            raise ApiError(text, 400)
        if eintrag.get("ergebnis") is None:
            return {"pending": True}
        out = dict(eintrag["ergebnis"])
        _LOGINS.pop(str(state), None)
        return out


def login_vorgaenge_aufraeumen(*, now: int | None = None) -> int:
    stamp = store_hosted.now() if now is None else int(now)
    with _LOGIN_LOCK:
        alt = [k for k, v in _LOGINS.items() if int(v.get("expires_at") or 0) <= stamp]
        for key in alt:
            _LOGINS.pop(key, None)
    return len(alt)


# --------------------------------------------------------------------------- Zustand im Speicher

POOL = ports.PortPool()
JOBS: dict = {}
JOBS_LOCK = threading.RLock()       # reentrant: job_new_exclusive prüft und legt darunter an
#: Startvorgänge laufen einzeln: die Prüfung „passt das noch auf die Maschine?“ liest den
#: Zustand der Runner, und der ist erst nach `run.start()` fortgeschrieben.
START_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def on_runner_exit(run, code: int) -> None:
    """Nach dem Ende eines Servers: Ports freigeben, Zustand nachtragen."""
    iid = run.spec.instance_id
    try:
        instances.set_running(iid, False)
    except ValueError:
        pass
    try:
        POOL.release(iid)
        save_ports()
    except ValueError:
        pass
    # Minecraft schreibt manche Dateien (level.dat) über eine Zwischendatei und damit als 0600.
    # Der Daemon müsste sie für die Rückholung lesen können, darf sie aber nicht umstellen –
    # also lässt er es den Server-Benutzer selbst tun, sobald der Server aus ist.
    grant_group_access(run.active_spec)
    # Der Eintrag bleibt in der Zuordnungstabelle stehen: der Verteiler meldet dann „Dieser
    # Server läuft gerade nicht“, und das ist für den Spieler die bessere Auskunft als
    # „gibt es hier nicht“.
    schreibe_routen(f"Server {iid} beendet")
    log_event(f"Server {iid} beendet (Code {code}).")


def grant_group_access(spec) -> None:
    """Dateirechte im Instanzordner für den Dienstbenutzer nachziehen (siehe core/isolation.py)."""
    _grant_group(getattr(spec, "directory", None), str(getattr(spec, "run_as", "") or ""),
                 str(getattr(spec, "instance_id", "?")))


def grant_group_for(inst: dict) -> None:
    """Wie `grant_group_access`, aber aus dem Instanz-Datensatz heraus."""
    _grant_group(instance_dir(inst), isolation.user_for_owner(str(inst.get("owner") or "")),
                 str(inst.get("id") or "?"))


def _grant_group(folder, run_as: str, iid: str) -> None:
    if not run_as or folder is None:
        return
    try:
        isolation.grant_group(folder, run_as)
    except Exception:                                           # noqa: BLE001 - nie den Ablauf stören
        log_event(f"Die Dateirechte von {iid} ließen sich nicht nachziehen:\n"
                  + traceback.format_exc())


REGISTRY = runner.RunnerRegistry(on_exit=on_runner_exit)


# --------------------------------------------------------------------------- Fehler

class ApiError(Exception):
    """Ein Fehler mit fertigem deutschen Satz und passendem HTTP-Status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = str(message)
        self.status = int(status)


def status_for_value_error(exc: Exception) -> int:
    """Passender HTTP-Status zu einem deutschen Satz aus den Modulen."""
    text = str(exc)
    if "zu wenig Platz" in text or "Reserve frei bleiben" in text:
        return 507                                  # Platte voll – ein zweiter Versuch hilft nicht
    if "gibt es kein" in text or "gibt keinen" in text:
        return 404
    return 400


# --------------------------------------------------------------------------- Kleinigkeiten

def instance_dir(inst: dict) -> pathlib.Path:
    """/srv/mcsm/users/<konto>/<instanz> – der Ordner heißt nach der Kennung, nie nach dem Namen."""
    return sources_linux.users_dir() / str(inst.get("owner") or "") / str(inst.get("id") or "")


def ensure_instance_dir(inst: dict) -> pathlib.Path:
    """Instanzordner anlegen und die Rechte setzen.

    Mit eigenem Server-Benutzer (core/isolation.py) bekommt der Ordner dessen Gruppe und den
    Modus 2770; der Kontoordner darüber wird nur durchlässig (2710), nicht lesbar. Ohne
    Trennung bleibt es beim bisherigen 0750 für den Dienstbenutzer.
    """
    folder = instance_dir(inst)
    folder.mkdir(parents=True, exist_ok=True)
    run_as = isolation.user_for_owner(str(inst.get("owner") or ""))
    if run_as and isolation.apply_dir(folder, run_as):
        isolation.apply_dir(folder.parent, run_as, mode=0o2710)
        return folder
    try:
        os.chmod(folder.parent, 0o750)
        os.chmod(folder, 0o750)
    except OSError:
        pass
    return folder


def read_properties(path: pathlib.Path) -> dict:
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def patch_properties(path: pathlib.Path, updates: dict) -> None:
    """Werte in einer .properties-Datei setzen; Kommentare und Reihenfolge bleiben erhalten."""
    remaining = {str(k): str(v) for k, v in updates.items()}
    lines: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        pass
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".mcsmtmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install_info_path(folder: pathlib.Path) -> pathlib.Path:
    """Was bei der Einrichtung herauskam (Java-Version, Schalter) – liegt in .mcsm,
    wird also nicht angezeigt und nicht mit zurückgeholt."""
    return folder / runner.MANAGE_DIR / "install.json"


def read_install_info(folder: pathlib.Path) -> dict:
    try:
        with install_info_path(folder).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_install_info(folder: pathlib.Path, info: dict) -> None:
    path = install_info_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private_json(path, info)


def has_companion(folder: pathlib.Path) -> bool:
    """Liegt das Begleit-Plugin bereit? Nur dann versteht der Server ``mcsmstop``."""
    return (folder / "plugins" / "MCSMCompanion.jar").is_file()


def has_geyser(folder: pathlib.Path) -> bool:
    return (folder / "plugins" / "Geyser-Spigot.jar").is_file()


def max_players_of(folder: pathlib.Path, fallback: int = 10) -> int:
    props = read_properties(folder / "server.properties")
    try:
        return max(1, min(200, int(props.get("max-players") or fallback)))
    except ValueError:
        return fallback


def offline_java_major(version: str) -> int:
    """Java-Version ohne Netzabfrage schätzen (der Start darf nicht auf eine API warten)."""
    guess = getattr(sources_linux, "_guess_java_major", None)
    if callable(guess):
        try:
            return int(guess(version))
        except Exception:                                   # noqa: BLE001
            pass
    return 21


def java_for(inst: dict, folder: pathlib.Path) -> tuple[pathlib.Path, tuple]:
    """Pfad zur Java-Laufzeit und die geprüften Schalter für diese Instanz."""
    info = read_install_info(folder)
    major = int(info.get("java_major") or 0) or offline_java_major(inst.get("version") or "")
    flags = tuple(str(f) for f in (info.get("java_flags") or ()))
    found = sources_linux.java_path(major)
    if found is None:
        for candidate in sorted(sources_linux.AVAILABLE_JAVA, reverse=True):
            if candidate >= major:
                found = sources_linux.java_path(candidate)
                if found is not None:
                    break
    if found is None:
        raise ApiError(f"Java {major} ist auf dem Root-Server noch nicht eingerichtet – "
                       f"bitte den Server zuerst einrichten lassen.", 409)
    return found, flags


def build_spec(inst: dict, *, require_java: bool = True) -> "runner.RunSpec":
    """RunSpec aus dem Instanz-Datensatz (ohne Netzabfrage).

    ``require_java=False`` liefert auch dann Angaben, wenn keine Java-Laufzeit eingerichtet ist.
    Das braucht das Einsammeln beim Start: ein weiterlaufender Server soll übernommen (oder
    beendet) werden können, auch wenn seine Laufzeit inzwischen fehlt.
    """
    folder = instance_dir(inst)
    kind = runner.KIND_BEDROCK if str(inst.get("type")) == "bedrock" else runner.KIND_JAVA
    version = str(inst.get("version") or "")
    data = {
        "instance_id": str(inst.get("id") or ""),
        "directory": folder,
        "kind": kind,
        "name": str(inst.get("name") or ""),
        "version": version,
        "ram_mb": int(inst.get("ram_mb") or 4096),
        # Eigener Unix-Benutzer je Konto: ein hochgeladenes Plugin kommt damit weder an fremde
        # Instanzen noch an die Kontodaten (siehe core/isolation.py).
        "run_as": isolation.user_for_owner(str(inst.get("owner") or "")),
    }
    if kind == runner.KIND_JAVA:
        try:
            java, flags = java_for(inst, folder)
        except ApiError:
            if require_java:
                raise
            java, flags = None, tuple(read_install_info(folder).get("java_flags") or ())
            data["java_major"] = int(read_install_info(folder).get("java_major") or 0) \
                or offline_java_major(version)
        if java is not None:
            data["java"] = java
        data["java_flags"] = flags
        data["companion"] = str(inst.get("flavor") or "") == "paper" and has_companion(folder)
        gamerule = sources_linux.command_block_gamerule(version)
        props = read_properties(folder / "server.properties")
        after: list[str] = []
        if gamerule:
            on = str(props.get("enable-command-block", "false")).lower() == "true"
            after.append(f"gamerule {gamerule} {'true' if on else 'false'}")
        data["after_ready_commands"] = tuple(after)
    else:
        data["companion"] = False
    return runner.RunSpec(**data)


def _load_average() -> list:
    """Systemlast [1 min, 5 min, 15 min] – auf Windows (Entwicklung) gibt es sie nicht."""
    getter = getattr(os, "getloadavg", None)
    if getter is None:
        return []
    try:
        return [round(float(v), 2) for v in getter()]
    except OSError:
        return []


def meminfo_available_mb() -> int | None:
    """Wirklich freier Arbeitsspeicher der Maschine (None, wenn nicht ermittelbar)."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, IndexError, ValueError):
        return None
    return None


def write_geyser_config(folder: pathlib.Path, inst: dict, assignment: dict) -> None:
    """Minimale Geyser-Konfiguration; alles Übrige ergänzt Geyser selbst."""
    bedrock_port = int(assignment.get("bedrock_port") or 19132)
    java_port = int(assignment.get("port") or 25565)
    name = str(inst.get("name") or "Minecraft").replace('"', "'")
    path = folder / "plugins" / "Geyser-Spigot" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Von Minecraft Server Manager erzeugt - hier sind die Bedrock-Einstellungen.\n"
        "bedrock:\n"
        "  address: 0.0.0.0\n"
        f"  port: {bedrock_port}\n"
        "  clone-remote-port: false\n"
        f'  motd1: "{name}"\n'
        f'  motd2: "Crossplay"\n'
        f'  server-name: "{name}"\n'
        "  compression-level: 6\n"
        "remote:\n"
        "  address: 127.0.0.1\n"
        f"  port: {java_port}\n"
        "  auth-type: floodgate\n"
        "  use-proxy-protocol: false\n"
        "passthrough-motd: true\n"
        "passthrough-player-counts: true\n"
        "debug-mode: false\n",
        encoding="utf-8")


def apply_ports(inst: dict, folder: pathlib.Path, assignment: dict, geyser: bool) -> None:
    """Die vergebenen Ports in server.properties (und ggf. Geyser) schreiben."""
    updates: dict = {}
    if "port" in assignment:
        updates["server-port"] = assignment["port"]
    if str(inst.get("type")) == "bedrock":
        updates["transport"] = "nethernet"
        if "udp_first" in assignment:
            updates["server-udp-ports"] = f"{assignment['udp_first']}-{assignment['udp_last']}"
        elif "bedrock_port" in assignment:
            updates["server-udp-ports"] = str(assignment["bedrock_port"])
    if updates:
        patch_properties(folder / "server.properties", updates)
    if geyser and str(inst.get("type")) != "bedrock":
        write_geyser_config(folder, inst, assignment)


def mirror_ports(inst: dict, assignment: dict) -> dict:
    """Die vergebenen Ports in den Datensatz spiegeln. Rückgabe: was dort jetzt steht.

    Maßgeblich ist der PortPool (er kennt auch den UDP-Block); der Datensatz ist die Anzeige und
    die Quelle für ``routes.json``. Deshalb darf das Spiegeln **nicht still scheitern**: stünde
    dort ein alter Port, schickte der Verteiler die Spieler an die falsche Stelle.

    Zwei Stolpersteine sind abgefangen:

    * Ein **fremder** Datensatz kann denselben Port noch aus einem früheren Lauf tragen (der
      Server ist längst aus, der Eintrag blieb stehen). ``instances.set_ports`` lehnt dann mit
      „schon an einen anderen Server vergeben“ ab. Wem der Port wirklich gehört, weiß der
      PortPool – der alte Eintrag wird also geräumt.
    * Bleibt es trotzdem beim Fehler, wird der **eigene** Eintrag geleert. Dann steht dort
      nichts Falsches, und die Anzeige nimmt den Wert aus ``ports_live``.
    """
    iid = str(inst.get("id") or "")
    mirror: dict = {}
    if "port" in assignment:
        # Auch bei Bedrock: der TCP-Port ist der, den der Spieler eintippt (NetherNet).
        mirror["java"] = int(assignment["port"])
    if "bedrock_port" in assignment:
        mirror["bedrock"] = int(assignment["bedrock_port"])
    if not mirror:
        return dict((instances.get_instance(iid) or inst).get("ports") or {})
    with store_hosted.lock():
        _fremde_porteintraege_raeumen(iid, mirror)
        try:
            frisch = instances.set_ports(iid, mirror)
            return dict(frisch.get("ports") or {})
        except ValueError as exc:
            log_event(f"Die Ports von {iid} ließen sich nicht in den Datensatz übernehmen "
                      f"({exc}) – der Eintrag wird geleert, damit dort keine falsche Zahl steht. "
                      f"Gültig ist {mirror}.")
            try:
                instances.set_ports(iid, {})
            except ValueError:
                pass
    return {}


def _fremde_porteintraege_raeumen(iid: str, mirror: dict) -> None:
    """Alte Portangaben anderer Instanzen räumen, die einen dieser Ports noch behaupten.

    Geräumt wird nur, wenn der PortPool den Port **nicht** dieser anderen Instanz zugesagt hat –
    ein wirklich laufender Server verliert seinen Eintrag also nie.
    """
    gesucht = {int(v) for v in mirror.values()}
    for other in instances.all_instances():
        oid = str(other.get("id") or "")
        if oid == str(iid):
            continue
        alt = {}
        for key, value in (other.get("ports") or {}).items():
            try:
                alt[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        behalten = {}
        for key, value in alt.items():
            if value not in gesucht:
                behalten[key] = value
                continue
            pool = ports.BEDROCK_POOL if key == "bedrock" else ports.JAVA_POOL
            try:
                eigner = POOL.owner_of(pool, value)
            except ValueError:
                eigner = None
            if eigner == oid:                       # gehört wirklich dieser Instanz – bleibt
                behalten[key] = value
        if behalten != alt:
            try:
                instances.set_ports(oid, behalten)
                log_event(f"Der Porteintrag von {oid} war veraltet und wurde geräumt "
                          f"(vorher {alt}, jetzt {behalten}).")
            except ValueError as exc:
                log_event(f"Der veraltete Porteintrag von {oid} ließ sich nicht räumen: {exc}")


def ports_live_of(instance_id: str) -> dict:
    """Die gerade gebuchten Ports einer Instanz (leer, wenn sie nicht läuft)."""
    try:
        return POOL.assignment(str(instance_id))
    except ValueError:
        return {}


def ensure_marke(inst: dict) -> str:
    """Unterdomäne der Instanz sicherstellen (siehe core/routes.py). Wirft nie."""
    try:
        return routen.ensure_marke(inst)
    except (ValueError, OSError) as exc:
        log_event(f"Für {inst.get('id')} ließ sich keine Unterdomäne vergeben: {exc}")
        return str(inst.get("marke") or "")


def marke_zuruecklegen(inst: dict, grund: str) -> None:
    """Die Unterdomäne einer verschwundenen Instanz für eine Weile zurückhalten.

    Sonst bekäme sie sofort der Nächste, dessen Servername dieselbe Marke ergibt – alle Spieler
    mit der alten Adresse im Serverbrowser landeten dann bei einem fremden Server. Dem eigenen
    Konto steht sie weiter offen (siehe core/routes.py, `gesperrte_marken`). Wirft nie.
    """
    marke = str(inst.get("marke") or "").strip().lower()
    if not marke:
        return
    try:
        if routen.marke_aufgeben(marke, str(inst.get("owner") or "")):
            log_event(f"Die Unterdomäne „{marke}“ ist frei geworden ({grund}) und bleibt "
                      f"{routen.SPERRE_TAGE} Tage für andere Konten gesperrt.")
    except (OSError, ValueError) as exc:
        log_event(f"Die Unterdomäne „{marke}“ ließ sich nicht zurückhalten: {exc}")


def schreibe_routen(grund: str = "") -> dict:
    """``routes.json`` für den Verteiler neu schreiben. Wirft nie – der Dienst läuft weiter."""
    try:
        data = routen.schreibe_routen(ports_live=ports_live_of)
    except (OSError, ValueError) as exc:
        log_event(f"Die Zuordnungstabelle des Verteilers ließ sich nicht schreiben: {exc}")
        return {}
    eintraege = routen.eintraege(data)
    with _runtime_lock:
        vorher = _runtime.get("routen_stand")
        _runtime["routen_stand"] = sorted(f"{k}:{v['port']}" for k, v in eintraege.items())
        geaendert = vorher != _runtime["routen_stand"]
    if geaendert:
        namen = ", ".join(sorted(eintraege)) or "keine"
        log_event(f"Zuordnungstabelle geschrieben ({len(eintraege)} Einträge: {namen})"
                  + (f" – {grund}" if grund else "") + ".")
    return data


def reserve_router_port() -> None:
    """Den Port des Verteilers buchen, damit ihn keine Instanz bekommt.

    Der Verteiler (Dienst ``mcsm-router``) sitzt auf TCP 25565 – dem ersten Port des
    Java-Bereichs. Ohne Buchung würde die erste Instanz genau diesen Port bekommen; beim Start
    fiele das nur durch einen fehlgeschlagenen Bind auf, und bis dahin stünde eine falsche
    Adresse in der Anzeige. Der Bind-Versuch wird hier ausgelassen: der Port ist ja besetzt –
    das ist der Grund für die Buchung, nicht ein Hindernis.
    """
    port = routen.ROUTER_PORT
    eigner = POOL.owner_of(ports.JAVA_POOL, port)
    if eigner == routen.ROUTER_OWNER:
        return
    if eigner:
        run = REGISTRY.find(eigner)
        if run is not None and run.running:
            log_event(f"Achtung: {eigner} läuft auf Port {port} – dort sitzt sonst der Verteiler. "
                      f"Der Port wird erst nach dem Stoppen dieses Servers für den Verteiler "
                      f"gebucht.")
            return
        POOL.release(eigner, ports.JAVA_POOL)
        log_event(f"Port {port} gehört dem Verteiler – die alte Buchung von {eigner} wurde "
                  f"aufgelöst (die Instanz bekommt beim nächsten Start einen anderen Port).")
    try:
        POOL.reserve(routen.ROUTER_OWNER, ports.JAVA_POOL, port, probe=False)
    except (ValueError, ports.PortsExhausted) as exc:
        log_event(f"Der Port {port} des Verteilers ließ sich nicht buchen: {exc}")


def server_view(inst: dict) -> dict:
    """Instanz für die API: Datensatz, Anzeigetexte und der wirkliche Laufzustand."""
    out = instances.public_instance(inst)
    folder = instance_dir(inst)
    iid = str(inst.get("id") or "")
    run = REGISTRY.find(iid)
    out["live"] = run.status() if run is not None else None
    out["ports_live"] = ports_live_of(iid)
    out["companion"] = has_companion(folder)
    out["geyser"] = has_geyser(folder)
    out["installed"] = ((folder / "bedrock_server").is_file()
                        if str(inst.get("type")) == "bedrock"
                        else (folder / "server.jar").is_file())
    # Adresse zum Eintippen: Java ohne Port (der Verteiler hört auf 25565), Bedrock mit Port.
    out["domain"] = routen.domain()
    out["subdomain"] = routen.subdomain(inst)
    out["address"] = routen.adresse(inst, ports_live=out["ports_live"])
    return out


def public_user(user: dict, *, now: int | None = None) -> dict:
    """Konto für die API – wie `users.public_user`, dazu die Felder für die Anzeige.

    ``discord_name`` und ``avatar_url`` kommen aus der Discord-Anmeldung (siehe
    `_nach_anmeldung`), ``last_seen`` ist die jüngste Sitzung des Kontos. Die Kontenverwaltung
    selbst kennt diese Felder nicht – sie gehören zur Darstellung, nicht zum Kern.
    """
    out = users.public_user(user)
    out["discord_name"] = str(user.get("discord_name") or "")
    avatar = str(user.get("avatar_url") or "")
    # Nur Bilder von Discord selbst – so lädt die Oberfläche nichts von beliebigen Adressen.
    out["avatar_url"] = avatar if avatar.startswith("https://cdn.discordapp.com/") else ""
    letzte = 0
    for session in users.sessions_of(str(user.get("id") or ""), now=now, only_valid=False):
        letzte = max(letzte, int(session.get("last_seen") or 0),
                     int(session.get("created_at") or 0))
    out["last_seen"] = letzte
    return out


def notices_for(user: dict, now: int) -> list[dict]:
    """Hinweise, die das Programm beim Öffnen anzeigen soll (Rückholung, Ruhe, Ablauf)."""
    out: list[dict] = []
    uid = str(user.get("id") or "")
    with _runtime_lock:
        woken = dict(_runtime.get("woken") or {})
    for inst in instances.instances_of(uid):
        state = str(inst.get("state") or "")
        iid = str(inst.get("id") or "")
        if state == "hosted" and iid in woken:
            out.append({"kind": "wieder_startbar", "instance": iid, "name": inst.get("name", ""),
                        "text": f"„{inst.get('name')}“ ruht nicht mehr: es liegt wieder ein "
                                f"gültiger Pass vor, der Server lässt sich also wieder starten. "
                                f"Eingeschaltet wurde er nicht – das entscheidest du."})
        if state == "awaiting_pull":
            out.append({"kind": "pull", "instance": inst["id"], "name": inst.get("name", ""),
                        "text": f"„{inst.get('name')}“ liegt noch auf dem Root-Server und wartet "
                                f"darauf, auf deinen PC zurückgeholt zu werden. Erst wenn alle "
                                f"Dateien geprüft angekommen sind, wird der Ordner dort gelöscht."})
        elif state == "downloading":
            out.append({"kind": "pull_running", "instance": inst["id"], "name": inst.get("name", ""),
                        "text": f"Die Rückholung von „{inst.get('name')}“ ist angefangen, aber noch "
                                f"nicht bestätigt – bitte fortsetzen."})
        elif state == "suspended":
            parked = int(inst.get("parked_at") or 0)
            rest = max(0, instances.PREMIUM_KEEP_DAYS * 86400 - max(0, now - parked)) if parked else 0
            out.append({"kind": "suspended", "instance": inst["id"], "name": inst.get("name", ""),
                        "text": f"„{inst.get('name')}“ ruht, weil kein gültiger Premium-Pass "
                                f"vorliegt. Ohne neuen Pass darf der Betreiber den Server in "
                                f"{passes.duration_text(rest)} löschen."})
    for entry in passes.active_passes(uid, now=now):
        if passes.remaining_seconds(entry, now=now) <= WARN_MINUTES * 60:
            out.append({"kind": "expiring", "pass": entry["id"],
                        "text": f"Dein {passes.kind_text(entry.get('kind'))} "
                                f"{passes.remaining_text(entry, now=now)} – laufende Server werden "
                                f"dann angekündigt gestoppt und gespeichert."})
    return out


# --------------------------------------------------------------------------- Anfrage und Antwort

class Raw:
    """Antwort, die keine JSON-Daten trägt (Datei-Stücke)."""

    def __init__(self, data: bytes, content_type: str = "application/octet-stream",
                 status: int = 200, headers: dict | None = None) -> None:
        self.data = bytes(data)
        self.content_type = content_type
        self.status = int(status)
        self.headers = dict(headers or {})


class Req:
    """Eine Anfrage mit allem, was die Routen brauchen."""

    def __init__(self, handler: "Handler", query: dict, body: bytes) -> None:
        self.handler = handler
        self.query = query
        self.body = body
        self.user: dict | None = None

    # -- Eingaben ------------------------------------------------------
    def json(self, required: bool = True) -> dict:
        if not self.body:
            if required:
                raise ApiError("Es wurden keine Daten gesendet (JSON erwartet).", 400)
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(f"Die gesendeten Daten sind kein gültiges JSON: {exc}", 400) from exc
        if not isinstance(data, dict):
            raise ApiError("Erwartet wird ein JSON-Objekt.", 400)
        return data

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name) or []
        return str(values[0]) if values else default

    def qint(self, name: str, default: int = 0) -> int:
        raw = self.q(name, "")
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise ApiError(f"„{name}“ muss eine ganze Zahl sein.", 400) from None

    def qflag(self, name: str) -> bool:
        return self.q(name, "").lower() in ("1", "ja", "yes", "true")

    def header(self, name: str, default: str = "") -> str:
        return self.handler.headers.get(name, default) or default

    @property
    def host(self) -> str:
        """Der angefragte Hostname ohne Port, klein geschrieben (aus dem ``Host``-Kopf)."""
        return host_of(self.handler)

    @property
    def herkunft(self) -> str:
        """Herkunftsadresse des Aufrufers (siehe `client_ip`)."""
        return client_ip(self.handler)

    # -- Rechte --------------------------------------------------------
    @property
    def me(self) -> dict:
        if self.user is None:
            raise ApiError("Bitte neu anmelden.", 401)
        return self.user

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.get("role") == "admin")

    def instance(self, instance_id: str) -> dict:
        """Instanz holen und prüfen, dass sie dem Konto gehört (oder das Konto Admin ist)."""
        inst = instances.get_instance(instance_id)
        if not inst:
            raise ApiError(f"Es gibt keinen Server mit der Kennung „{instance_id}“.", 404)
        if inst.get("owner") != self.me["id"] and not self.is_admin:
            raise ApiError("Dieser Server gehört nicht zu deinem Konto.", 403)
        return inst


ROUTES: list = []


def route(method: str, pattern: str, *, auth: bool = True, admin: bool = False,
          limit: int = MAX_JSON_BYTES):
    def decorate(fn):
        ROUTES.append({"method": method.upper(), "regex": re.compile(pattern), "fn": fn,
                       "auth": auth, "admin": admin, "limit": int(limit)})
        return fn
    return decorate


# --------------------------------------------------------------------------- Oberfläche

def _web_erlaubt(req: Req) -> bool:
    """Darf unter diesem Hostnamen die Oberfläche ausgeliefert werden?

    Unter ``admin.<domain>`` ja, unter ``api.<domain>`` **nein** – dort gibt es nur die API.
    Die Rückschleife (Selbstprüfung von deploy.sh, Selbsttests) darf ebenfalls, damit sich die
    Auslieferung ohne DNS prüfen lässt.
    """
    host = req.host
    if host == api_host():
        return False
    return host == admin_host() or host in ("", "localhost", "127.0.0.1", "::1")


@route("GET", r"^/(?:admin\.html)?$", auth=False)
def h_web_index(req: Req):
    return h_web(req, "admin.html")


@route("GET", r"^/(admin\.css|admin\.js)$", auth=False)
def h_web(req: Req, name: str = "admin.html"):
    """Die drei Dateien der Betreiberoberfläche ausliefern.

    Es gibt **keine** Pfadzusammensetzung: der Name muss einer der drei bekannten sein, sonst
    404. Damit ist ein Verzeichnis-Ausbruch („../../srv/mcsm/data/users.json“) ausgeschlossen –
    ein solcher Name passt schon auf keine Route.
    """
    if not _web_erlaubt(req):
        raise ApiError("Diese Adresse gibt es auf dem Root-Server nicht.", 404)
    art = WEB_FILES.get(name)
    if art is None:
        raise ApiError("Diese Adresse gibt es auf dem Root-Server nicht.", 404)
    path = WEB_DIR / name
    try:
        data = path.read_bytes()
    except OSError:
        log_event(f"Die Oberfläche fehlt: {path} ist nicht lesbar.")
        raise ApiError("Die Betreiberoberfläche ist auf diesem Server nicht eingerichtet.",
                       503) from None
    # Cache-Control: no-store setzt `_send` für jede Antwort – die Oberfläche ändert sich mit
    # jedem Ausrollen, und ein Browser, der eine alte admin.js behält, zeigt Unsinn an.
    # Dazu ein ETag: ein erneuter Aufruf kostet dann nur die Kopfzeilen.
    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    if req.header("If-None-Match") == etag:
        return Raw(b"", art, 304, {"ETag": etag})
    return Raw(data, art, 200, {"ETag": etag})


@route("GET", r"^/favicon\.ico$", auth=False)
def h_favicon(req: Req):
    """Es gibt kein Symbol – aber auch keinen Fehler im Protokoll für jeden Browseraufruf."""
    return Raw(b"", "image/x-icon", 204)


# --------------------------------------------------------------------------- Anmeldung

@route("GET", r"^/api/health$", auth=False)
def h_health(req: Req):
    return {"ok": True, "service": "mcsmd", "version": VERSION, "time": store_hosted.now(),
            "running": len(REGISTRY.running_ids()),
            # Damit das Programm auf dem PC den Discord-Knopf nur zeigt, wenn er funktioniert.
            "discord": oauth.eingerichtet(),
            # „Dieser Server hat noch keinen Betreiber“ – die Oberfläche klappt dann das Feld für
            # den Code aus ERSTER-ADMIN.txt auf. Kein Geheimnis: Dass noch kein Konto da ist,
            # merkt ohnehin jeder Anmeldeversuch.
            "erstzugang": oauth.erstzugang_offen(),
            "domain": routen.domain(),
            # Die eigene Adresse, wie der Dienst sie sieht. Steht hier „@proxy“, gibt nginx sie
            # nicht weiter und die Ratenbremse kann Aufrufer nicht unterscheiden.
            "herkunft": req.herkunft}


@route("POST", r"^/api/auth/invite$", auth=False)
def h_auth_invite(req: Req):
    data = req.json()
    code = str(data.get("code") or "")
    name = str(data.get("name") or "")
    note = str(data.get("note") or "")[:SESSION_NOTE_MAX]
    with store_hosted.lock():
        first_admin = users.count_admins() == 0
        invite = users.get_invite(code)
        user = users.redeem_invite(code, name)
        wanted = str(_runtime.get("admin_invite") or "")
        if (first_admin and invite is not None and wanted
                and secrets.compare_digest(str(invite.get("code") or ""), wanted)):
            user = users.set_role(user["id"], "admin")
            with _runtime_lock:
                _runtime["admin_invite"] = ""
            oauth.erstzugang_setzen("")
            log_event(f"Das erste Admin-Konto wurde angelegt (Kennung {user['id']}).")
            try:
                (store_hosted.data_dir() / ADMIN_FILE).unlink(missing_ok=True)
            except OSError:
                pass
            runtime_save()
    token, session = users.create_session(user["id"], note=note)
    BREMSE.freigeben(req.herkunft, "anmeldung")      # wer hereingekommen ist, ist nicht verdächtig
    log_event(f"Neues Konto über Einladung angelegt: {user['id']} ({user.get('role')}).")
    return {"token": token, "expires_at": session["expires_at"],
            "user": public_user(user)}


@route("POST", r"^/api/auth/logout$", auth=False)
def h_auth_logout(req: Req):
    token = req.handler.session_token()
    done = users.revoke_session(token) if token else False
    # Das Cookie der Verwaltung im Browser wird mitgelöscht – sonst hängt es bis zum Ablauf.
    return 200, {"ok": bool(done)}, {"Set-Cookie": _cookie_loeschen()}


# --------------------------------------------------------------------------- Discord

def _cookie_setzen(token: str, expires_at: int) -> str:
    """Sitzungscookie für die Verwaltung im Browser.

    ``HttpOnly`` (kein Zugriff aus dem Skript), ``Secure`` (nur über HTTPS) und
    ``SameSite=Lax``. Letzteres ist wichtig: der Browser schickt das Cookie damit **nicht** bei
    einer von fremden Seiten ausgelösten POST-Anfrage mit – ohne das wäre die Verwaltung über
    das Cookie allein angreifbar (CSRF). Die Oberfläche selbst benutzt ohnehin den Kopf
    ``Authorization``; das Cookie ist nur der bequeme Weg direkt nach der Anmeldung.
    """
    dauer = max(60, int(expires_at or 0) - store_hosted.now())
    return (f"{COOKIE_NAME}={token}; Max-Age={dauer}; Path=/; HttpOnly; Secure; SameSite=Lax")


def _cookie_loeschen() -> str:
    return f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"


#: Kurzlebiges Cookie, das den Anmeldevorgang an **den** Browser bindet, der ihn begonnen hat.
ANMELDE_COOKIE = "mcsm_anmeldung"


def _anmelde_cookie_setzen(wert: str) -> str:
    return (f"{ANMELDE_COOKIE}={wert}; Max-Age={oauth.STATE_TTL_SECONDS}; Path=/; "
            f"HttpOnly; Secure; SameSite=Lax")


def _anmelde_cookie_loeschen() -> str:
    return f"{ANMELDE_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"


def _cookie_lesen(req: Req, name: str) -> str:
    """Einen Cookie-Wert aus dem Kopf ``Cookie`` holen (leer, wenn er fehlt)."""
    roh = req.header("Cookie", "")
    for teil in str(roh or "").split(";"):
        schluessel, _, wert = teil.partition("=")
        if schluessel.strip() == name:
            return wert.strip()
    return ""


def _browser_passt(req: Req, vorgang: dict) -> bool:
    """Wurde diese Anmeldung in **diesem** Browser begonnen?

    Der Zustandswert allein genügt nicht: Er ist zwar einmalig und befristet, aber an niemanden
    gebunden. Ein Angreifer kann die Anmeldung mit seinem eigenen Discord-Konto beginnen, die
    Rückleitung abfangen (er führt die Weiterleitung einfach nicht aus) und dem Betreiber die
    Adresse schicken. Dessen Browser holt sie, bekommt das Sitzungstoken des Angreifers gesetzt
    und arbeitet ab da unbemerkt in dessen Konto (Anmelde-CSRF). ``SameSite=Lax`` hilft dabei
    nicht, weil die Sitzung durch eine gewöhnliche GET-Navigation auf die eigene Seite entsteht.

    Deshalb bekommt der Browser beim Start ein kurzlebiges Zufallscookie; hier muss sein
    SHA256 zu dem passen, der am Vorgang hängt.
    """
    erwartet = str(vorgang.get("browser") or "")
    if not erwartet:
        return False
    gegeben = _cookie_lesen(req, ANMELDE_COOKIE)
    if not gegeben:
        return False
    return secrets.compare_digest(
        erwartet, hashlib.sha256(gegeben.encode("utf-8")).hexdigest())


def _admin_ziel(wunsch: str) -> str:
    """Wohin die Rückleitung der Oberfläche zeigt – nur eigene Adressen sind erlaubt.

    Der Wunsch der Seite ist ein Wunsch, kein Befehl: käme hier eine fremde Adresse durch, würde
    das Sitzungstoken in der Raute an einen fremden Server geschickt.
    """
    erlaubt = (f"https://{admin_host()}/admin.html", f"https://{admin_host()}/",
               f"https://{admin_host()}")
    text = str(wunsch or "").strip()
    if text in erlaubt:
        return text if text.endswith((".html", "/")) else text + "/"
    return f"https://{admin_host()}/admin.html"


def _discord_fehlerseite(text: str, *, flow: str, ziel: str = "", status: int = 400) -> Raw:
    """Antwort auf einen gescheiterten Rückweg: die Oberfläche bekommt eine Weiterleitung mit
    ``#fehler=``, alle anderen eine kleine deutsche Seite."""
    if flow == oauth.FLOW_ADMIN:
        url = _admin_ziel(ziel) + "#fehler=" + urllib.parse.quote(text)
        return Raw(b"", "text/html; charset=utf-8", 302,
                   {"Location": url, "Set-Cookie": _cookie_loeschen()})
    return Raw(_seite("Anmeldung nicht abgeschlossen", f"<p>{_html(text)}</p>").encode("utf-8"),
               "text/html; charset=utf-8", status)


def _html(text) -> str:
    """Text für eine HTML-Seite entschärfen."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _seite(titel: str, inhalt: str) -> str:
    """Eine schlichte deutsche Seite ohne Fremddateien – für die Rückleitung von Discord."""
    return ("<!DOCTYPE html>\n<html lang=\"de\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{_html(titel)} – Minecraft Server Manager</title>"
            "<style>body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;"
            "margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}"
            "main{background:#161c26;border:1px solid #263041;border-radius:14px;padding:28px 32px;"
            "max-width:32rem;line-height:1.55}h1{margin:0 0 12px;font-size:1.3rem;color:#3ddc84}"
            "code{background:#0d1117;border:1px solid #263041;border-radius:8px;padding:6px 10px;"
            "display:inline-block;font-size:1.4rem;letter-spacing:2px}p{margin:10px 0 0}"
            "</style></head><body><main>"
            f"<h1>{_html(titel)}</h1>{inhalt}</main></body></html>\n")


def _discord_notiz(req: Req, device: str = "") -> str:
    """Notiz für die Sitzungsliste (woher die Anmeldung kam)."""
    if device:
        return str(device)[:SESSION_NOTE_MAX]
    return "Verwaltung im Browser" if req.host == admin_host() else "Anmeldung über Discord"


def _nach_anmeldung(anmeldung, identitaet) -> None:
    """Nachbereitung jeder Discord-Anmeldung: Anzeigename merken, erstes Admin-Konto abschließen."""
    user = anmeldung.user if isinstance(anmeldung.user, dict) else {}
    uid = str(user.get("id") or "")
    if uid:
        # Discord-Name und Bildadresse gehören nicht zum Kern der Kontenverwaltung, die
        # Betreiberoberfläche zeigt sie aber gern. Sie werden hier fortgeschrieben, damit die
        # Oberfläche sie auch ohne erneute Anmeldung hat.
        try:
            store_hosted.patch_by("users", "id", uid,
                                  {"discord_name": str(identitaet.anzeigename or "")[:64],
                                   "avatar_url": str(identitaet.avatar_url or "")[:200]})
        except ValueError as exc:
            log_event(f"Der Discord-Name von {uid} ließ sich nicht merken: {exc}")
    if not anmeldung.admin_vergeben:
        return
    # Es darf nur **einen** Weg zum ersten Betreiberkonto geben: der Einladungscode aus
    # ERSTER-ADMIN.txt verliert jetzt seine Wirkung.
    code = ""
    with _runtime_lock:
        code = str(_runtime.get("admin_invite") or "")
        _runtime["admin_invite"] = ""
    oauth.erstzugang_setzen("")
    if code:
        try:
            users.revoke_invite(code)
        except ValueError:
            pass
    try:
        (store_hosted.data_dir() / ADMIN_FILE).unlink(missing_ok=True)
    except OSError:
        pass
    runtime_save()
    log_event(f"Das erste Admin-Konto wurde über Discord angelegt (Kennung {uid}). Der "
              f"Einladungscode für den Erstzugang ist damit ungültig.")


@route("GET", r"^(?:/api)?/auth/discord/start$", auth=False)
def h_discord_start(req: Req):
    """Anmeldung über Discord beginnen.

    Drei Aufrufer, ein Weg:

    * die Betreiberoberfläche schickt ``ziel`` (die Adresse dieser Seite) → ``flow=admin``,
    * das Programm auf dem PC schickt ``device`` (Rechnername) → ``flow=app`` mit Abholvorgang,
    * ausdrücklich gesetztes ``flow`` (``admin``/``app``/``lokal``) gilt immer.
    """
    ziel = req.q("ziel", "")
    device = req.q("device", "")[:40]
    roh = req.q("flow", "")
    abhol = req.q("abhol", "")
    flow = oauth.clean_flow(roh) if roh else (oauth.FLOW_APP if device else oauth.FLOW_ADMIN)
    data = oauth.start(flow, invite_code=req.q("code", ""), port=req.qint("port", 0),
                       abhol_hash=abhol, prompt=req.q("prompt", ""))
    out = dict(data)
    secret = ""
    if flow == oauth.FLOW_APP and not abhol:
        # Weg des Programms auf dem PC: es holt das Token später gegen Zustandswert **und**
        # Geheimnis ab (``/poll``). Das Token steht damit nie auf einer Webseite und nie in
        # einer Browseradresse. Die Oberfläche im Browser braucht das nicht – sie bekommt das
        # Token in der Raute der Rückleitung und hier deshalb auch kein Geheimnis.
        secret = secrets.token_urlsafe(24)
        out["poll_secret"] = secret
    browser = secrets.token_urlsafe(24) if flow == oauth.FLOW_ADMIN else ""
    out["ziel"] = _admin_ziel(ziel) if flow == oauth.FLOW_ADMIN else ""
    if abhol:
        # Weg B (Notweg): Das Programm hat sich ein Geheimnis gemerkt und holt das Token später
        # über den Abholcode. Hier **keinen** Vorgang anlegen – sonst meldete `_login_setzen` in
        # der Rückleitung Erfolg, der Abholcode entstünde nie, und das fertige Sitzungstoken
        # bliebe bis zum Ablauf unabholbar im Arbeitsspeicher liegen.
        return out
    _login_anlegen(data["state"], flow=flow, ziel=_admin_ziel(ziel) if ziel else "",
                   device=device, secret=secret, browser=browser)
    if browser:
        return 200, out, {"Set-Cookie": _anmelde_cookie_setzen(browser)}
    return out


@route("GET", r"^(?:/api)?/auth/discord/callback$", auth=False)
def h_discord_callback(req: Req):
    """Rückleitung von Discord – für die Oberfläche **und** für das Programm auf dem PC.

    Discord ruft genau diesen Pfad auf (ohne ``/api``); die Fassung mit ``/api`` bleibt für
    Programme bequem erreichbar.
    """
    state = req.q("state", "")
    vorgang = _login_lesen(state)
    flow = str(vorgang.get("flow") or "")
    if not flow:
        # Weg B: zu diesem Zustandswert gibt es absichtlich keinen Vorgang im Arbeitsspeicher.
        try:
            flow = oauth.clean_flow(req.q("flow", ""))
        except oauth.OAuthFehler:
            flow = oauth.FLOW_APP
    ziel = str(vorgang.get("ziel") or "")
    if flow == oauth.FLOW_ADMIN and not _browser_passt(req, vorgang):
        # Ohne das zum Vorgang gehörende Cookie wird hier **keine** Sitzung gesetzt.
        log_event("Eine Rückleitung von Discord kam ohne das Merkmal des Browsers an, der die "
                  "Anmeldung begonnen hat – sie wurde abgewiesen.")
        return _discord_fehlerseite(
            "Diese Anmeldung wurde in einem anderen Browser begonnen. Aus Sicherheitsgründen "
            "wird hier keine Sitzung eingerichtet: Sonst könnte dir jemand eine Adresse "
            "schicken, mit der du unbemerkt in **seinem** Konto arbeitest. Bitte die Verwaltung "
            "in diesem Browser öffnen und dort auf „Mit Discord anmelden“ klicken.",
            flow=flow, ziel=ziel, status=403)
    try:
        ergebnis = oauth.complete(state, req.q("code", ""), error=req.q("error", ""),
                                  error_description=req.q("error_description", ""))
        flow = ergebnis.flow or flow
        anmeldung = oauth.anmelden(ergebnis, note=_discord_notiz(req, str(vorgang.get("device") or "")))
    except oauth.OAuthFehler as exc:
        _login_setzen(state, fehler=exc.message)
        log_event(f"Anmeldung über Discord gescheitert: {exc.message}")
        return _discord_fehlerseite(exc.message, flow=flow, ziel=ziel, status=exc.status)
    except ValueError as exc:
        _login_setzen(state, fehler=str(exc))
        return _discord_fehlerseite(str(exc), flow=flow, ziel=ziel)

    _nach_anmeldung(anmeldung, ergebnis.identitaet)
    BREMSE.freigeben(req.herkunft, "anmeldung")
    log_event(f"Konto {anmeldung.user.get('id')} über Discord angemeldet "
              f"(neu: {'ja' if anmeldung.neu else 'nein'}, Weg {flow}).")
    daten = anmeldung.as_dict()

    if flow == oauth.FLOW_ADMIN:
        # Die Oberfläche liest das Token aus der Raute (die geht nie an den Server) und legt es
        # in localStorage; zusätzlich wird das Cookie gesetzt.
        url = _admin_ziel(ziel) + "#token=" + urllib.parse.quote(daten["token"])
        _login_setzen(state, ergebnis=daten)
        return Raw(b"", "text/html; charset=utf-8", 302,
                   {"Location": url,
                    "Set-Cookie": [_cookie_setzen(daten["token"], daten["expires_at"]),
                                   _anmelde_cookie_loeschen()]})

    if _login_setzen(state, ergebnis=daten):
        # Weg A: das Programm holt das Token gleich mit ``/poll`` ab. Die Seite zeigt es nicht.
        return Raw(_seite("Geschafft", "<p>Die Anmeldung ist abgeschlossen. Du kannst dieses "
                                       "Fenster schließen – das Programm auf deinem PC ist "
                                       "gleich angemeldet.</p>").encode("utf-8"),
                   "text/html; charset=utf-8")

    # Weg B (Notweg): kein Abholvorgang bekannt – das Token wartet unter einem Abholcode.
    code, gueltig = oauth.abholcode_anlegen(daten, claim_hash=ergebnis.abhol_hash)
    return Raw(_seite("Fast fertig",
                      f"<p>Bitte diesen Abholcode im Programm eintragen:</p>"
                      f"<p><code>{_html(oauth.abholcode_formatieren(code))}</code></p>"
                      f"<p>Er gilt {max(1, gueltig // 60)} Minuten und nur einmal. Danach kann "
                      f"dieses Fenster geschlossen werden.</p>").encode("utf-8"),
               "text/html; charset=utf-8")


@route("GET", r"^(?:/api)?/auth/discord/poll$", auth=False)
def h_discord_poll(req: Req):
    """Das Programm auf dem PC fragt nach dem Sitzungstoken (Zustandswert + Geheimnis)."""
    out = _login_abholen(req.q("state", ""), req.q("secret", ""))
    if not out.get("pending"):
        BREMSE.freigeben(req.herkunft, "anmeldung")
    return out


@route("POST", r"^(?:/api)?/auth/discord/finish$", auth=False)
def h_discord_finish(req: Req):
    """Weg A mit eigenem Zuhörer: Das Programm hat ``code`` und ``state`` selbst aufgefangen."""
    data = req.json()
    try:
        ergebnis = oauth.complete(str(data.get("state") or ""), str(data.get("code") or ""))
        anmeldung = oauth.anmelden(ergebnis, note=str(data.get("device") or "")[:SESSION_NOTE_MAX]
                                   or "Programm auf dem PC")
    except oauth.OAuthFehler as exc:
        raise ApiError(exc.message, exc.status) from exc
    _nach_anmeldung(anmeldung, ergebnis.identitaet)
    BREMSE.freigeben(req.herkunft, "anmeldung")
    log_event(f"Konto {anmeldung.user.get('id')} über Discord angemeldet (Weg lokal).")
    return anmeldung.as_dict()


@route("POST", r"^(?:/api)?/auth/discord/claim$", auth=False)
def h_discord_claim(req: Req):
    """Weg B: Abholcode einlösen. Das Geheimnis kennt nur das Programm, das ihn angefordert hat."""
    data = req.json()
    try:
        out = oauth.abholcode_einloesen(str(data.get("code") or ""),
                                        geheimnis=str(data.get("geheimnis")
                                                      or data.get("secret") or ""))
    except oauth.OAuthFehler as exc:
        raise ApiError(exc.message, exc.status) from exc
    BREMSE.freigeben(req.herkunft, "anmeldung")
    return out


@route("POST", r"^(?:/api)?/auth/discord/link$")
def h_discord_link(req: Req):
    """Ein bestehendes Konto dauerhaft mit einem Discord-Konto verknüpfen."""
    data = req.json()
    try:
        ergebnis = oauth.complete(str(data.get("state") or ""), str(data.get("code") or ""))
        user = oauth.verknuepfen(req.me["id"], ergebnis.identitaet)
    except oauth.OAuthFehler as exc:
        raise ApiError(exc.message, exc.status) from exc
    try:
        store_hosted.patch_by("users", "id", str(user.get("id") or ""),
                              {"discord_name": str(ergebnis.identitaet.anzeigename or "")[:64],
                               "avatar_url": str(ergebnis.identitaet.avatar_url or "")[:200]})
    except ValueError:
        pass
    log_event(f"Konto {user.get('id')} ist jetzt mit Discord verknüpft.")
    return {"user": public_user(user), "discord": ergebnis.identitaet.as_dict()}


@route("GET", r"^/api/me$")
def h_me(req: Req):
    now = store_hosted.now()
    user = req.me
    free = instances.free_disk_bytes()
    out = instances.overview(user["id"], now=now, free_bytes=free)
    out["user"] = public_user(user)
    out["passes"] = [passes.public_pass(p, now=now) for p in passes.passes_of(user["id"])]
    out["notices"] = notices_for(user, now)
    out["server_time"] = now
    out["discord"] = oauth.eingerichtet()
    out["domain"] = routen.domain()
    # Die fertigen Adressen der eigenen Server – damit das Programm sie zum Kopieren anbieten
    # kann, ohne sie selbst aus dem Namen zu raten.
    out["addresses"] = {str(i.get("id")): routen.adresse(i, ports_live=ports_live_of(i.get("id")))
                        for i in instances.instances_of(user["id"])}
    out["machine"] = {
        "ram_running_mb": REGISTRY.running_ram_mb(),
        "ram_machine_mb": machine_ram_mb(),
        "ram_available_mb": meminfo_available_mb(),
        "disk_free_bytes": int(free or 0),
        "disk_free_text": transfer.human(int(free or 0)),
        "free_ports_java": POOL.free_ports(ports.JAVA_POOL),
        "free_ports_bedrock": POOL.free_ports(ports.BEDROCK_POOL),
    }
    out["sessions"] = len(users.sessions_of(user["id"], now=now))
    return out


@route("GET", r"^/api/sessions$")
def h_sessions(req: Req):
    return {"sessions": users.sessions_of(req.me["id"])}


# --------------------------------------------------------------------------- Instanzen

@route("GET", r"^/api/servers$")
def h_servers(req: Req):
    user = req.me
    return {"servers": [server_view(i) for i in instances.instances_of(user["id"])]}


@route("POST", r"^/api/servers$")
def h_server_create(req: Req):
    data = req.json()
    user = req.me
    inst = instances.create_instance(
        user["id"],
        str(data.get("name") or ""),
        server_type=str(data.get("type") or "java"),
        flavor=str(data.get("flavor") or "paper"),
        version=str(data.get("version") or ""),
        ram_mb=data.get("ram_mb", 4096),
        origin=str(data.get("origin") or "local"),
        geyser=bool(data.get("geyser")),
    )
    ensure_instance_dir(inst)
    # Die Unterdomäne wird jetzt vergeben und bleibt der Instanz für immer – auch wenn sie später
    # umbenannt wird. Die Spieler sollen ihre Adresse behalten.
    marke = ensure_marke(inst)
    inst = instances.get_instance(inst["id"]) or inst
    schreibe_routen(f"Instanz {inst['id']} angelegt")
    log_event(f"Instanz {inst['id']} angelegt (Konto {user['id']}, Herkunft {inst['origin']}, "
              f"Unterdomäne „{marke}“).")
    return 201, {"server": server_view(inst)}


@route("GET", r"^/api/servers/([A-Za-z0-9._-]{1,64})$")
def h_server_detail(req: Req, iid: str):
    inst = req.instance(iid)
    view = server_view(inst)
    view["check_start"] = _check_start_dict(inst)
    return {"server": view}


def _check_start_dict(inst: dict) -> dict:
    try:
        check = instances.check_start(inst["id"])
    except ValueError as exc:
        return {"ok": False, "code": "fehler", "reason": str(exc)}
    return {"ok": bool(check.ok), "code": check.code, "reason": check.reason}


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/settings$")
def h_server_settings(req: Req, iid: str):
    inst = req.instance(iid)
    data = req.json()
    if "name" in data:
        inst = instances.rename_instance(iid, str(data.get("name") or ""))
    if "ram_mb" in data:
        if instances.is_running(inst):
            raise ApiError(f"„{inst.get('name')}“ läuft – der Arbeitsspeicher lässt sich erst "
                           f"nach dem Stoppen ändern.", 409)
        inst = instances.set_ram(iid, data.get("ram_mb"))
    if "version" in data:
        inst = instances.set_version(iid, str(data.get("version") or ""))
    # Die Unterdomäne bleibt beim Umbenennen **absichtlich** gleich: die Spieler haben die
    # Adresse im Serverbrowser stehen. Nachgetragen wird nur, wenn noch gar keine da ist.
    ensure_marke(instances.get_instance(iid) or inst)
    return {"server": server_view(instances.get_instance(iid) or inst)}


@route("DELETE", r"^/api/servers/([A-Za-z0-9._-]{1,64})$")
def h_server_delete(req: Req, iid: str):
    inst = req.instance(iid)
    state = str(inst.get("state") or "")
    origin = str(inst.get("origin") or "local")
    if instances.is_running(inst) or (REGISTRY.find(iid) and REGISTRY.find(iid).running):
        raise ApiError(f"„{inst.get('name')}“ läuft noch – bitte zuerst stoppen.", 409)
    if origin == "local" and state in ("hosted", "uploading", "awaiting_pull", "downloading") \
            and not req.qflag("force"):
        raise ApiError(f"„{inst.get('name')}“ liegt gerade auf dem Root-Server. Hole ihn zuerst "
                       f"auf deinen PC zurück – oder wiederhole den Aufruf mit force=1, wenn die "
                       f"Dateien dort wirklich verloren gehen dürfen.", 409)
    folder = instance_dir(inst)
    REGISTRY.drop(iid, timeout=STOP_TIMEOUT)
    POOL.release(iid)
    save_ports()
    removed = {"deleted": False}
    if folder.is_dir():
        try:
            removed = transfer.delete_tree(folder, sources_linux.users_dir())
        except ValueError as exc:
            raise ApiError(str(exc), 400) from exc
    instances.delete_instance(iid)
    marke_zuruecklegen(inst, "gelöscht")
    _woken_vergessen(iid)
    schreibe_routen(f"Instanz {iid} gelöscht")
    log_event(f"Instanz {iid} gelöscht (Konto {inst.get('owner')}).")
    return {"ok": True, "deleted": removed}


# --------------------------------------------------------------------------- Steuern

@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/start$")
def h_server_start(req: Req, iid: str):
    """Server einschalten.

    Der ganze Vorgang läuft unter `START_LOCK`, und die Pass-Grenzen werden mit
    `instances.reserve_start` geprüft **und** im selben Zug eingetragen. Ohne beides ließen sich
    „wie viele gleichzeitig“ und „wie viel Arbeitsspeicher zusammen“ durch zwei gleichzeitige
    Aufrufe einfach umgehen: beide sehen noch „nichts läuft“ und beide starten.
    """
    inst = req.instance(iid)
    with START_LOCK:
        free = instances.free_disk_bytes()
        need = int(inst.get("ram_mb") or 0)
        running_total = REGISTRY.running_ram_mb()
        if running_total + need > machine_ram_mb():
            raise ApiError(f"Auf dem Root-Server laufen gerade Server mit zusammen "
                           f"{passes.gb_text(running_total)}; mit diesem wären es mehr als die "
                           f"{passes.gb_text(machine_ram_mb())}, die die Maschine hergibt. "
                           f"Bitte später erneut versuchen.", 409)
        available = meminfo_available_mb()
        if available is not None and available - need < MACHINE_RAM_RESERVE_MB:
            raise ApiError(f"Der Root-Server hat gerade nur noch {passes.gb_text(available)} "
                           f"Arbeitsspeicher frei – dieser Server braucht {passes.gb_text(need)}. "
                           f"Bitte später erneut versuchen.", 409)
        check = instances.reserve_start(iid, free_bytes=free)
        if not check.ok:
            raise ApiError(check.reason, 409)

        try:
            folder = ensure_instance_dir(inst)
            geyser = has_geyser(folder)
            kind = "bedrock" if str(inst.get("type")) == "bedrock" else "java"
            POOL.allocate_for(iid, kind, geyser=geyser, max_players=max_players_of(folder))
            assignment = POOL.assignment(iid)
            apply_ports(inst, folder, assignment, geyser)
            mirror_ports(inst, assignment)
            ensure_marke(inst)
            # Das Begleit-Plugin wird vor **jedem** Start bereitgelegt (auch wenn es gelöscht
            # wurde): nur damit versteht der Server „mcsmstop“ und kündigt einen Stopp im Spiel
            # an, und nur damit liefert er Kennzahlen für die Anzeige.
            if companion.prepare(folder, inst, version=VERSION):
                # Nur der Plugin-Ordner, nicht die ganze Instanz: ein „chmod -R“ über eine
                # gewachsene Welt dauert und liefe hier unter der Startsperre.
                _grant_group(folder / "plugins",
                             isolation.user_for_owner(str(inst.get("owner") or "")), iid)
            spec = build_spec(instances.get_instance(iid) or inst)
            run = REGISTRY.get(spec)
            run.start()
        except Exception:
            instances.release_start(iid)         # Reservierung zurücknehmen, der Start ging schief
            POOL.release(iid)
            save_ports()
            raise
        save_ports()
    _woken_vergessen(iid)
    schreibe_routen(f"Server {iid} gestartet")
    log_event(f"Server {iid} gestartet (Ports {assignment}).")
    return {"ok": True, "ports": assignment, "live": run.status(),
            "server": server_view(instances.get_instance(iid) or inst)}


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/stop$")
def h_server_stop(req: Req, iid: str):
    inst = req.instance(iid)
    data = req.json(required=False)
    try:
        announce = max(0, min(900, int(data.get("announce_seconds") or 0)))
    except (TypeError, ValueError):
        announce = 0
    run = REGISTRY.find(iid)
    if run is None or not run.running:
        try:
            instances.set_running(iid, False)
        except ValueError:
            pass
        raise ApiError(f"„{inst.get('name')}“ läuft nicht.", 409)
    thread = threading.Thread(target=_stop_runner, args=(run, announce), daemon=True,
                              name=f"stop-{iid}")
    thread.start()
    return 202, {"ok": True, "announce_seconds": announce,
                 "message": (f"„{inst.get('name')}“ wird gestoppt – die Welt wird gespeichert."
                             if not announce else
                             f"„{inst.get('name')}“ wird in {announce} Sekunden gestoppt; "
                             f"die Spieler werden im Spiel gewarnt.")}


def _stop_runner(run, announce: int) -> None:
    try:
        run.stop(timeout=STOP_TIMEOUT, announce_seconds=announce)
    except Exception as exc:                                        # noqa: BLE001
        log_event(f"Beim Stoppen von {run.spec.instance_id} gab es einen Fehler: {exc}")


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/command$")
def h_server_command(req: Req, iid: str):
    inst = req.instance(iid)
    data = req.json()
    text = str(data.get("command") or "")
    run = REGISTRY.find(iid)
    if run is None or not run.running:
        raise ApiError(f"„{inst.get('name')}“ läuft nicht – Befehle gehen nur an einen laufenden "
                       f"Server.", 409)
    run.send(text)
    return {"ok": True, "console_next": run.console(0)["next"]}


@route("GET", r"^/api/servers/([A-Za-z0-9._-]{1,64})/console$")
def h_server_console(req: Req, iid: str):
    req.instance(iid)
    run = REGISTRY.find(iid)
    if run is None:
        return {"next": 0, "lines": [], "live": None}
    tail = req.qint("tail", 0)
    out = run.console(req.qint("since", 0), tail=tail or None)
    out["live"] = run.status()
    return out


# --------------------------------------------------------------------------- Dateien

def _instance_root(req: Req, iid: str) -> tuple[dict, pathlib.Path]:
    inst = req.instance(iid)
    folder = instance_dir(inst)
    if not folder.is_dir():
        raise ApiError(f"Für „{inst.get('name')}“ liegt auf dem Root-Server (noch) kein Ordner.", 404)
    return inst, folder


def _no_changes_while_running(inst: dict) -> None:
    run = REGISTRY.find(str(inst.get("id")))
    if instances.is_running(inst) or (run is not None and run.running):
        raise ApiError(f"„{inst.get('name')}“ läuft – Dateien lassen sich erst nach dem Stoppen "
                       f"ändern.", 409)


@route("GET", r"^/api/servers/([A-Za-z0-9._-]{1,64})/files$")
def h_files_get(req: Req, iid: str):
    inst, folder = _instance_root(req, iid)
    rel = paths.check_user_read(req.q("path", ""))
    action = req.q("action", "list")
    if action == "list":
        return paths.list_dir(folder, rel)
    if action == "info":
        try:
            info = paths.entry_info(folder, rel)
        except OSError as exc:
            raise ApiError("Diese Datei gibt es auf dem Root-Server nicht (mehr).", 404) from exc
        if req.qflag("hash") and not info.get("is_dir"):
            # Damit das Programm eine einzeln geholte Datei prüfen kann, ohne das Manifest des
            # ganzen Ordners anzufordern. Kostet einen Durchlauf über die Datei, deshalb nur
            # auf ausdrückliche Nachfrage.
            try:
                info["sha256"] = paths.pruefsumme(folder, rel)
            except PermissionError:
                grant_group_for(inst)
                try:
                    info["sha256"] = paths.pruefsumme(folder, rel)
                except (OSError, ValueError):
                    info["sha256"] = ""
            except (OSError, ValueError):
                info["sha256"] = ""
        return info
    if action == "read":
        if not paths.is_text(rel):
            raise ApiError("Diese Datei lässt sich nicht als Text anzeigen – bitte herunterladen.",
                           400)
        target = paths.resolve(folder, rel)
        if not target.is_file():
            raise ApiError("Diese Datei gibt es auf dem Root-Server nicht (mehr).", 404)
        size = target.stat().st_size
        if size > paths.MAX_EDIT_BYTES:
            raise ApiError(f"Diese Datei ist zu groß für den Editor "
                           f"({transfer.human(size)}).", 400)
        # Gelesen wird über einen Ordner-Deskriptor (paths.lies_stueck): zwischen der Prüfung
        # oben und dem Zugriff darf der Serverbenutzer in seinem Ordner umbenennen – über den
        # Pfad ließe sich sonst eine Verknüpfung nach /srv/mcsm/data dazwischenschieben.
        try:
            roh = paths.lies_stueck(folder, rel)
        except PermissionError:
            # Der Server legt manche Dateien als 0600 an (level.dat & Co.). Erst nachziehen,
            # dann noch einmal versuchen – ein Rundumschlag bei jedem Lesen wäre zu teuer.
            grant_group_for(inst)
            try:
                roh = paths.lies_stueck(folder, rel)
            except OSError as exc:
                raise ApiError("Die Datei kann auf dem Server nicht gelesen werden.", 400) from exc
        except ValueError as exc:
            raise ApiError(str(exc), 400) from exc
        except OSError as exc:
            raise ApiError("Die Datei kann auf dem Server nicht gelesen werden.", 400) from exc
        text = roh.decode("utf-8", errors="replace")
        return {"path": rel, "size": size, "text": text}
    raise ApiError("Unbekannte Aktion – erlaubt sind list, info und read.", 400)


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/files$")
def h_files_post(req: Req, iid: str):
    inst, folder = _instance_root(req, iid)
    data = req.json()
    action = str(data.get("action") or "")
    _no_changes_while_running(inst)

    if action == "write":
        rel = paths.check_user_write(str(data.get("path") or ""))
        text = data.get("text")
        if not isinstance(text, str):
            raise ApiError("Es fehlt der Text, der gespeichert werden soll.", 400)
        if len(text.encode("utf-8")) > paths.MAX_EDIT_BYTES:
            raise ApiError("Der Text ist zu groß, um ihn so zu speichern.", 400)
        target = paths.resolve(folder, rel)
        if target.is_dir():
            raise ApiError(f"„{rel}“ ist ein Ordner.", 400)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Geschrieben wird über Ordner-Deskriptoren (paths.schreibe_atomar) – sonst könnte
            # der Serverbenutzer im richtigen Augenblick einen Ordner des Pfads gegen eine
            # Verknüpfung nach /srv/mcsm/data tauschen.
            paths.schreibe_atomar(folder, rel, text.encode("utf-8"))
        except ValueError as exc:
            raise ApiError(str(exc), 400) from exc
        except OSError as exc:
            raise ApiError(f"„{rel}“ kann auf dem Server nicht geschrieben werden.", 400) from exc
        return {"ok": True, "path": rel, "entry": paths.entry_info(folder, rel)}

    if action == "mkdir":
        rel = paths.check_user_write(str(data.get("path") or ""))
        target = paths.resolve(folder, rel)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ApiError(f"Der Ordner „{rel}“ konnte nicht angelegt werden.", 400) from exc
        return {"ok": True, "path": rel}

    if action == "delete":
        rel = paths.check_user_delete(str(data.get("path") or ""))
        target = paths.resolve(folder, rel)
        if not paths.inside(folder, target):
            raise ApiError("Dieser Pfad liegt nicht im Ordner des Servers.", 400)
        if not target.exists():
            raise ApiError(f"„{rel}“ gibt es auf dem Root-Server nicht (mehr).", 404)
        try:
            if target.is_dir():
                shutil.rmtree(str(target))
            else:
                target.unlink()
        except OSError as exc:
            raise ApiError(f"„{rel}“ konnte nicht gelöscht werden: {exc}", 400) from exc
        return {"ok": True, "path": rel}

    if action == "rename":
        rel = paths.check_user_delete(str(data.get("path") or ""))
        new_rel = paths.check_user_write(str(data.get("new_path") or ""))
        source = paths.resolve(folder, rel)
        target = paths.resolve(folder, new_rel)
        if not source.exists():
            raise ApiError(f"„{rel}“ gibt es auf dem Root-Server nicht (mehr).", 404)
        if target.exists():
            raise ApiError(f"„{new_rel}“ gibt es dort schon.", 409)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(source), str(target))
        except OSError as exc:
            raise ApiError(f"„{rel}“ konnte nicht umbenannt werden: {exc}", 400) from exc
        return {"ok": True, "path": new_rel}

    raise ApiError("Unbekannte Aktion – erlaubt sind write, mkdir, delete und rename.", 400)


# --------------------------------------------------------------------------- Übertragung: hoch

def transfer_store() -> pathlib.Path:
    folder = sources_linux.transfer_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _session_of(req: Req, inst: dict, session_id: str):
    if not session_id:
        raise ApiError("Es fehlt die Kennung der Übertragung.", 400)
    try:
        session = transfer.open_session(transfer_store(), session_id)
    except ValueError as exc:
        raise ApiError(str(exc), 404) from exc
    if str(session.data.get("instance_id") or "") != str(inst.get("id")):
        raise ApiError("Diese Übertragung gehört zu einem anderen Server.", 403)
    owner = str(session.data.get("user_id") or "")
    if owner and owner != req.me["id"] and not req.is_admin:
        raise ApiError("Diese Übertragung gehört zu einem anderen Konto.", 403)
    return session


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/upload(?:/begin)?$")
def h_upload_begin(req: Req, iid: str):
    inst = req.instance(iid)
    data = req.json()
    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        raise ApiError("Es fehlt das Manifest der Übertragung.", 400)
    purpose = str(data.get("purpose") or "files")
    if purpose not in ("files", "instance"):
        raise ApiError("Unbekannter Zweck – erlaubt sind „instance“ und „files“.", 400)
    state = str(inst.get("state") or "")

    if purpose == "instance":
        if state not in ("local_only", "uploading"):
            raise ApiError(f"„{inst.get('name')}“ ist gerade {instances.state_text(state)} – "
                           f"eine ganze Instanz lässt sich nur aus dem Zustand „nur auf dem PC“ "
                           f"hochladen.", 409)
    else:
        if state not in ("hosted", "suspended"):
            raise ApiError(f"„{inst.get('name')}“ ist gerade {instances.state_text(state)} – "
                           f"einzelne Dateien lassen sich erst hochladen, wenn der Server auf dem "
                           f"Root-Server liegt.", 409)
        _no_changes_while_running(inst)
        for entry in (manifest.get("files") or []):
            if isinstance(entry, dict):
                paths.check_user_write(str(entry.get("path") or ""))

    folder = ensure_instance_dir(inst)
    session = transfer.UploadSession.begin(transfer_store(), folder, manifest,
                                           user_id=str(inst.get("owner") or ""),
                                           instance_id=str(inst.get("id") or ""))
    session.data["purpose"] = purpose
    session.save()
    if purpose == "instance" and state == "local_only":
        instances.set_state(iid, "uploading")
    log_event(f"Übertragung {session.id} begonnen ({purpose}, Instanz {iid}).")
    return {"session": session.status()}


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/upload/chunk$", limit=CHUNK_LIMIT + 4096)
def h_upload_chunk(req: Req, iid: str):
    inst = req.instance(iid)
    session = _session_of(req, inst, req.header("X-MCSM-Session", req.q("session", "")))
    # Der Pfad kommt **prozentkodiert** im Kopf: Kopfzeilen tragen kein UTF-8 und kein
    # Leerzeichen sicher, die Standardwelt von Bedrock heißt aber „Bedrock level“ und im
    # libraries-Baum von Paper stehen Namen wie „…-deprecated+build.21“. Zusätzlich schickt
    # das Programm denselben Pfad als Abfrageparameter – den hat parse_qs schon
    # entschlüsselt, er dient als Rückfall (siehe spec-cloud-client.md, Abschnitt 1.2).
    rel = urllib.parse.unquote(req.header("X-MCSM-Path", "")) or req.q("path", "")
    if not rel:
        raise ApiError("Es fehlt der Name der Datei (Kopf X-MCSM-Path oder Abfrageparameter "
                       "„path“).", 400)
    try:
        offset = int(req.header("X-MCSM-Offset", "0"))
    except ValueError:
        raise ApiError("Der Kopf X-MCSM-Offset muss eine ganze Zahl sein.", 400) from None
    if not req.body:
        raise ApiError("Das Stück enthält keine Daten.", 400)
    result = session.write_chunk(rel, offset, req.body)
    result["received_bytes"] = session.received_bytes()
    return result


@route("GET", r"^/api/servers/([A-Za-z0-9._-]{1,64})/upload/status$")
def h_upload_status(req: Req, iid: str):
    inst = req.instance(iid)
    session = _session_of(req, inst, req.q("session", ""))
    return {"session": session.status()}


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/upload/finish$")
def h_upload_finish(req: Req, iid: str):
    inst = req.instance(iid)
    data = req.json(required=False)
    session = _session_of(req, inst, str(data.get("session") or req.q("session", "")))
    hashes = bool(data.get("hashes", True))
    report = session.finish(hashes=hashes)
    folder = instance_dir(inst)
    if str(session.data.get("purpose") or "files") == "instance":
        if str(inst.get("state")) == "uploading":
            instances.set_state(iid, "hosted")
            ensure_marke(instances.get_instance(iid) or inst)
            schreibe_routen(f"Instanz {iid} liegt jetzt auf dem Root")
    try:
        instances.set_size(iid, transfer.dir_stats(folder)["bytes"])
    except (ValueError, OSError):
        pass
    transfer.forget_session(session.id)
    log_event(f"Übertragung {session.id} abgeschlossen ({report['files']} Dateien, "
              f"{transfer.human(report['bytes'])}).")
    return {"ok": True, "report": report,
            "server": server_view(instances.get_instance(iid) or inst)}


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/upload/abort$")
def h_upload_abort(req: Req, iid: str):
    inst = req.instance(iid)
    data = req.json(required=False)
    session = _session_of(req, inst, str(data.get("session") or req.q("session", "")))
    result = session.abort()
    # Abgebrochen heißt abgeräumt: Sitzungsdaten aus dem Arbeitsspeicher **und** von der Platte.
    # Sonst blieben Manifest und Ordner bis zum Aufräumen nach sieben Tagen liegen – eine Schleife
    # aus „begin“ und „abort“ würde den Daemon aufbrauchen.
    session.discard()
    if (str(session.data.get("purpose") or "files") == "instance"
            and str(inst.get("state")) == "uploading" and bool(data.get("back_to_local"))):
        instances.set_state(iid, "local_only")
        # „Nur auf deinem PC“ heißt: auf dem Root liegt nichts mehr. `session.abort()` räumt nur
        # die halb übertragenen Stücke weg – schon **fertige** Dateien blieben sonst liegen. Sie
        # hätten dann keinen Zustand mehr, der sie erklärt: die Instanz gilt als lokal, der
        # Ordner belegte aber weiter Platz, und ein späterer Upload würde auf alten Dateien
        # aufsetzen.
        entfernt = wipe_instance_folder(inst, "Übertragung abgebrochen")
        result = dict(result or {})
        result["ordner_geloescht"] = bool(entfernt.get("deleted"))
        schreibe_routen(f"Übertragung von {iid} abgebrochen")
    return {"ok": True, "result": result,
            "server": server_view(instances.get_instance(iid) or inst)}


# --------------------------------------------------------------------------- Übertragung: zurück

@route("GET", r"^/api/servers/([A-Za-z0-9._-]{1,64})/manifest$")
def h_manifest(req: Req, iid: str):
    inst, folder = _instance_root(req, iid)
    grant_group_for(inst)          # Dateien, die der Server als 0600 angelegt hat, lesbar machen
    try:
        manifest = transfer.build_manifest(folder, hashes=not req.qflag("fast"))
    except OSError as exc:
        raise ApiError("Der Ordner des Servers kann nicht gelesen werden.", 400) from exc
    try:
        instances.set_size(iid, int(manifest.get("total_bytes") or 0))
    except ValueError:
        pass
    return {"manifest": manifest}


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/pull$")
def h_pull(req: Req, iid: str):
    """Rückholung anmelden: hosted → awaiting_pull → downloading."""
    inst = req.instance(iid)
    state = str(inst.get("state") or "")
    run = REGISTRY.find(iid)
    if run is not None and run.running:
        raise ApiError(f"„{inst.get('name')}“ läuft noch – bitte zuerst stoppen.", 409)
    if state == "hosted":
        inst = instances.set_state(iid, "awaiting_pull")
        state = "awaiting_pull"
    if state == "awaiting_pull":
        inst = instances.set_state(iid, "downloading")
    elif state != "downloading":
        raise ApiError(f"„{inst.get('name')}“ ist gerade {instances.state_text(state)} – "
                       f"eine Rückholung ist da nicht vorgesehen.", 409)
    POOL.release(iid)
    save_ports()
    _woken_vergessen(iid)
    schreibe_routen(f"Instanz {iid} wird zurückgeholt")
    return {"server": server_view(instances.get_instance(iid) or inst)}


@route("GET", r"^/api/servers/([A-Za-z0-9._-]{1,64})/download$")
def h_download(req: Req, iid: str):
    inst, folder = _instance_root(req, iid)
    rel = paths.check_transfer(req.q("path", ""))
    offset = req.qint("offset", 0)
    length = req.qint("length", transfer.CHUNK_SIZE)
    if length <= 0 or length > CHUNK_LIMIT:
        raise ApiError(f"Die Stückgröße muss zwischen 1 und {CHUNK_LIMIT} Bytes liegen.", 400)
    try:
        data = transfer.read_chunk(folder, rel, offset, length)
    except PermissionError:
        grant_group_for(inst)      # 0600-Datei des Servers: Rechte nachziehen und noch einmal
        try:
            data = transfer.read_chunk(folder, rel, offset, length)
        except OSError as exc:
            raise ApiError("Die Datei kann auf dem Server nicht gelesen werden.", 400) from exc
    except OSError as exc:
        raise ApiError("Die Datei kann auf dem Server nicht gelesen werden.", 400) from exc
    return Raw(data, headers={"X-MCSM-Path": urllib.parse.quote(rel),
                              "X-MCSM-Offset": str(offset),
                              "X-MCSM-Length": str(len(data))})


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/release$")
def h_release(req: Req, iid: str):
    """Der PC meldet: alles angekommen und geprüft. Erst jetzt darf der Ordner weg."""
    inst = req.instance(iid)
    data = req.json()
    if str(inst.get("state")) not in ("downloading", "awaiting_pull"):
        raise ApiError("Freigeben ist erst möglich, wenn der Server zurückgeholt wurde.", 409)
    folder = instance_dir(inst)
    claimed = transfer.check_manifest(data.get("manifest") or {})
    if folder.is_dir():
        grant_group_for(inst)      # sonst zählt eine 0600-Datei des Servers als „nicht lesbar“
        try:
            here = transfer.build_manifest(folder)
        except OSError as exc:
            raise ApiError("Der Ordner des Servers kann nicht gelesen werden.", 400) from exc
        report = transfer.compare_manifests(here, claimed)
        if not report["ok"]:
            raise ApiError("Die Rückholung ist noch nicht vollständig: "
                           + transfer.problem_text(report) + ".", 409)
        # Was nicht ins Manifest passt, hat der PC nie bekommen. Ohne diese Prüfung würde
        # `delete_tree` solche Dateien ungesehen mitlöschen und der Benutzer bekäme trotzdem
        # „Instanz zurückgeholt“ gemeldet.
        offen = here.get("skipped") or []
        if offen and not bool(data.get("force")):
            namen = ", ".join(f"„{e.get('path')}“" for e in offen[:5])
            mehr = f" und {len(offen) - 5} weitere" if len(offen) > 5 else ""
            raise ApiError(
                f"Im Serverordner liegen {len(offen)} Datei(en), die sich nicht auf den PC "
                f"übertragen lassen: {namen}{mehr}. Grund der ersten: "
                f"{offen[0].get('reason')} Diese Dateien würden beim Löschen verloren gehen. "
                f"Bitte sie über die Dateiliste umbenennen oder sichern – oder den Aufruf mit "
                f"force=1 wiederholen, wenn sie wirklich weg dürfen.", 409)
    REGISTRY.drop(iid, timeout=10)
    POOL.release(iid)
    save_ports()
    deleted = {"deleted": False, "files": 0, "bytes": 0}
    if folder.is_dir():
        deleted = transfer.delete_tree(folder, sources_linux.users_dir())
    inst = instances.release_instance(iid)
    _woken_vergessen(iid)
    schreibe_routen(f"Instanz {iid} freigegeben")
    if bool(data.get("forget")):
        instances.delete_instance(iid)
        marke_zuruecklegen(inst, "zurückgeholt und vergessen")
        log_event(f"Instanz {iid} zurückgeholt und der Datensatz entfernt.")
        return {"ok": True, "deleted": deleted, "server": None}
    log_event(f"Instanz {iid} zurückgeholt – Ordner auf dem Root gelöscht "
              f"({deleted.get('files')} Dateien).")
    return {"ok": True, "deleted": deleted, "server": server_view(inst)}


# --------------------------------------------------------------------------- Einrichten (Jobs)

def job_new(kind: str, instance_id: str, owner: str = "") -> dict:
    """Neuen Vorgang anlegen. Das Konto steht **im** Vorgang, nicht nur am Instanz-Datensatz.

    Sonst fiele die Rechteprüfung weg, sobald die Instanz gelöscht oder zurückgeholt wurde: der
    Datensatz ist dann weg, und `h_job` hätte nichts mehr zum Vergleichen.
    """
    job = {"id": secrets.token_hex(6), "kind": kind, "instance": instance_id,
           "owner": str(owner or ""), "state": "läuft", "step": "wird vorbereitet …",
           "done": 0, "total": 0, "error": "", "result": None,
           "started_at": store_hosted.now(), "finished_at": 0}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        if len(JOBS) > 50:
            for old in sorted(JOBS.values(), key=lambda j: j["started_at"])[:10]:
                if old["state"] != "läuft":
                    JOBS.pop(old["id"], None)
    return job


def _job_running_locked(instance_id: str) -> dict | None:
    for job in JOBS.values():
        if str(job.get("instance") or "") == str(instance_id) and job.get("state") == "läuft":
            return dict(job)
    return None


def job_new_exclusive(kind: str, instance_id: str, owner: str = "") -> dict:
    """Vorgang anlegen, aber nur wenn für diese Instanz keiner läuft – Prüfung und Anlegen in
    einem Zug, sonst kämen zwei gleichzeitige Aufrufe beide durch."""
    with JOBS_LOCK:
        laeuft = _job_running_locked(instance_id)
        if laeuft is not None:
            raise ApiError(f"Für diesen Server läuft schon ein Vorgang ({laeuft.get('step')}). "
                           f"Bitte warten, bis er fertig ist.", 409)
        return job_new(kind, instance_id, owner)


@route("GET", r"^/api/jobs/([A-Za-z0-9]{1,32})$")
def h_job(req: Req, jid: str):
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if job is None:
        raise ApiError("Diesen Vorgang gibt es nicht (mehr).", 404)
    owner = str(job.get("owner") or "")
    if not owner:
        inst = instances.get_instance(str(job.get("instance") or ""))
        owner = str((inst or {}).get("owner") or "")
    if owner != req.me["id"] and not req.is_admin:
        raise ApiError("Dieser Vorgang gehört nicht zu deinem Konto.", 403)
    return {"job": dict(job)}


@route("POST", r"^/api/servers/([A-Za-z0-9._-]{1,64})/install$")
def h_install(req: Req, iid: str):
    inst = req.instance(iid)
    data = req.json()
    if str(inst.get("state")) not in ("hosted", "suspended"):
        raise ApiError(f"„{inst.get('name')}“ ist gerade {instances.state_text(inst.get('state'))} – "
                       f"einrichten lässt sich nur ein Server, der auf dem Root-Server liegt.", 409)
    run = REGISTRY.find(iid)
    if (run is not None and run.running) or instances.is_running(inst):
        raise ApiError(f"„{inst.get('name')}“ läuft noch – bitte zuerst stoppen.", 409)
    if not bool(data.get("eula")):
        raise ApiError("Die Mojang-EULA muss im Programm bestätigt werden, bevor ein Server "
                       "eingerichtet werden kann.", 400)
    version = str(data.get("version") or inst.get("version") or "")
    if not version:
        raise ApiError("Es fehlt die Minecraft-Version.", 400)
    geyser = bool(data.get("geyser"))
    # Zwei Einrichtungen derselben Instanz gleichzeitig würden in dieselben Dateien schreiben.
    job = job_new_exclusive("install", iid, owner=str(inst.get("owner") or ""))
    thread = threading.Thread(target=_install_worker, args=(dict(inst), version, geyser, job),
                              daemon=True, name=f"install-{iid}")
    thread.start()
    return 202, {"job": dict(job)}


def _install_worker(inst: dict, version: str, geyser: bool, job: dict) -> None:
    iid = str(inst.get("id"))

    def status(text: str) -> None:
        job["step"] = str(text)

    def progress(done: int, total: int) -> None:
        job["done"] = int(done)
        job["total"] = int(total)

    try:
        folder = ensure_instance_dir(inst)
        if str(inst.get("type")) == "bedrock":
            status("Bedrock-Server wird geladen …")
            info = sources_linux.install_bedrock(version, folder, progress=progress, status=status)
            write_install_info(folder, {"version": info.get("version", version),
                                        "binary": info.get("binary", "bedrock_server")})
            patch_properties(folder / "server.properties",
                             {"server-name": str(inst.get("name") or "Minecraft"),
                              "gamemode": "survival", "difficulty": "normal",
                              "allow-cheats": "false", "max-players": "10",
                              "online-mode": "true", "level-name": "Bedrock level"})
        else:
            major = offline_java_major(version)
            status(f"Java {major} wird eingerichtet …")
            sources_linux.ensure_java(major, progress=progress, status=status)
            status(f"Paper {version} wird geladen …")
            info = sources_linux.install_paper(version, folder, True,
                                               progress=progress, status=status)
            if int(info.get("java_major") or 0) and int(info["java_major"]) != major:
                status(f"Java {info['java_major']} wird nachgeladen …")
                sources_linux.ensure_java(int(info["java_major"]),
                                          progress=progress, status=status)
            write_install_info(folder, {"version": info.get("version", version),
                                        "build": info.get("build", ""),
                                        "jar": info.get("jar", "server.jar"),
                                        "java_major": int(info.get("java_major") or major),
                                        "java_flags": list(info.get("java_flags") or [])})
            if geyser:
                status("Crossplay (Geyser, Floodgate, Via*) wird eingerichtet …")
                sources_linux.install_crossplay(folder, progress=progress, status=status)
            else:
                sources_linux.remove_crossplay(folder)
            patch_properties(folder / "server.properties",
                             {"motd": str(inst.get("name") or "Minecraft"),
                              "gamemode": "survival", "difficulty": "normal",
                              "online-mode": "true", "max-players": "10",
                              "enable-command-block": "false", "spawn-protection": "0"})
        try:
            instances.set_version(iid, version)
        except ValueError:
            pass
        try:
            instances.set_size(iid, transfer.dir_stats(folder)["bytes"])
        except (ValueError, OSError):
            pass
        job["result"] = {"version": version, "geyser": geyser}
        job["state"] = "fertig"
        job["step"] = "Der Server ist eingerichtet."
        log_event(f"Instanz {iid} eingerichtet (Version {version}, Crossplay {geyser}).")
    except Exception as exc:                                        # noqa: BLE001
        job["state"] = "fehler"
        job["error"] = str(exc) or "Beim Einrichten ist ein Fehler aufgetreten."
        log_event(f"Einrichten von {iid} gescheitert: {exc}")
    finally:
        job["finished_at"] = store_hosted.now()


@route("GET", r"^/api/versions$")
def h_versions(req: Req):
    """Verfügbare Versionen – der Daemon fragt die APIs, damit der PC nicht selbst laden muss."""
    kind = req.q("type", "java")
    try:
        if kind == "bedrock":
            return {"type": "bedrock", "versions": sources_linux.bedrock_versions()}
        return {"type": "java", "versions": sources_linux.paper_versions()}
    except sources_linux.SourceError as exc:
        raise ApiError(str(exc), 400) from exc


# --------------------------------------------------------------------------- Adminrouten

@route("GET", r"^/api/admin/status$", admin=True)
def h_admin_status(req: Req):
    now = store_hosted.now()
    free = instances.free_disk_bytes() or 0
    total = 0
    try:
        usage = shutil.disk_usage(str(data_root()))
        total = int(usage.total)
    except OSError:
        pass
    percent = (free / total * 100) if total else 0.0
    return {
        "version": VERSION,
        "time": now,
        "started_at": int(_runtime.get("started_at") or 0),
        "users": len(users.all_users()),
        "instances": len(instances.all_instances()),
        "passes": len(passes.all_passes()),
        "running": REGISTRY.statuses(),
        "ram_running_mb": REGISTRY.running_ram_mb(),
        "ram_machine_mb": machine_ram_mb(),
        "ram_available_mb": meminfo_available_mb(),
        "disk_free_bytes": free,
        "disk_free_text": transfer.human(free),
        "disk_total_bytes": total,
        "disk_free_percent": round(percent, 1),
        "disk_warning": bool(total and percent < 15),
        "ports": POOL.snapshot(),
        "firewall": ports.firewall_ranges(),
        "load": _load_average(),
        "domain": routen.domain(),
        "admin_host": admin_host(),
        "api_host": api_host(),
        "discord": oauth.eingerichtet(),
        "routes": routen.eintraege(routen.tabelle(ports_live=ports_live_of)),
        "adopted": REGISTRY.adopted_ids(),
        "rate_keys": BREMSE.offen(),
        "premium_delete_candidates": [instances.public_instance(i)
                                      for i in instances.premium_delete_candidates()],
        "transfers": transfer.list_sessions(transfer_store()),
        "size_bytes": instances.total_size_bytes(),
    }


@route("GET", r"^/api/admin/users$", admin=True)
def h_admin_users(req: Req):
    now = store_hosted.now()
    out = []
    for user in users.all_users():
        uid = user["id"]
        entry = public_user(user)
        entry["passes"] = [passes.public_pass(p, now=now) for p in passes.passes_of(uid)]
        entry["limits"] = passes.effective_limits(uid, now=now)
        entry["limits_text"] = passes.limits_text(entry["limits"])
        entry["instances"] = len(instances.instances_of(uid))
        entry["running"] = len(instances.running_of(uid))
        entry["size_bytes"] = instances.total_size_bytes(uid)
        entry["sessions"] = len(users.sessions_of(uid, now=now))
        out.append(entry)
    return {"users": out}


@route("POST", r"^/api/admin/users/([A-Za-z0-9]{1,32})/role$", admin=True)
def h_admin_role(req: Req, uid: str):
    data = req.json()
    user = users.set_role(uid, str(data.get("role") or ""))
    log_event(f"Rolle von {uid} auf {user.get('role')} gesetzt.")
    return {"user": public_user(user)}


@route("POST", r"^/api/admin/users/([A-Za-z0-9]{1,32})/block$", admin=True)
def h_admin_block(req: Req, uid: str):
    data = req.json()
    blocked = bool(data.get("blocked", True))
    user = users.set_blocked(uid, blocked, str(data.get("reason") or ""))
    if blocked:
        for inst in instances.running_of(uid):
            run = REGISTRY.find(inst["id"])
            if run is not None and run.running:
                threading.Thread(target=_stop_runner, args=(run, runner.STOP_COUNTDOWN),
                                 daemon=True).start()
    log_event(f"Konto {uid} {'gesperrt' if blocked else 'entsperrt'}.")
    return {"user": public_user(user)}


@route("POST", r"^/api/admin/users/([A-Za-z0-9]{1,32})/rename$", admin=True)
def h_admin_rename(req: Req, uid: str):
    data = req.json()
    return {"user": public_user(users.rename_user(uid, str(data.get("name") or "")))}


@route("DELETE", r"^/api/admin/users/([A-Za-z0-9]{1,32})$", admin=True)
def h_admin_delete_user(req: Req, uid: str):
    user = users.get_user(uid)
    if not user:
        raise ApiError(f"Es gibt kein Konto mit der Kennung „{uid}“.", 404)
    own = instances.instances_of(uid)
    if any(instances.is_running(i) for i in own) and not req.qflag("force"):
        raise ApiError("Zu diesem Konto laufen noch Server – bitte zuerst stoppen "
                       "(oder force=1).", 409)
    for inst in own:
        REGISTRY.drop(inst["id"], timeout=30)
        POOL.release(inst["id"])
        folder = instance_dir(inst)
        if folder.is_dir():
            try:
                transfer.delete_tree(folder, sources_linux.users_dir())
            except ValueError as exc:
                log_event(f"Ordner von {inst['id']} nicht gelöscht: {exc}")
        try:
            instances.delete_instance(inst["id"])
        except ValueError:
            pass
    for entry in passes.passes_of(uid):
        passes.delete_pass(entry["id"])
    users.delete_user(uid)
    folder = sources_linux.users_dir() / uid
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
    save_ports()
    for inst in own:
        _woken_vergessen(str(inst.get("id")))
    schreibe_routen(f"Konto {uid} gelöscht")
    log_event(f"Konto {uid} samt {len(own)} Instanzen gelöscht.")
    return {"ok": True, "instances": len(own)}


@route("GET", r"^/api/admin/invites$", admin=True)
def h_admin_invites(req: Req):
    now = store_hosted.now()
    out = []
    for invite in users.all_invites():
        out.append({"code": users.format_code(invite.get("code", "")),
                    "created_by": invite.get("created_by", ""),
                    "created_at": invite.get("created_at", 0),
                    "uses_max": invite.get("uses_max", 1),
                    "uses": len(invite.get("redeemed_by") or []),
                    "expires_at": invite.get("expires_at", 0),
                    "revoked_at": invite.get("revoked_at", 0),
                    "note": invite.get("note", ""),
                    "state": users.invite_state(invite, now=now),
                    "state_text": users.invite_state_text(invite, now=now)})
    return {"invites": out}


@route("POST", r"^/api/admin/invites$", admin=True)
def h_admin_invite_create(req: Req):
    data = req.json(required=False)
    invite = users.create_invite(req.me["id"], uses_max=data.get("uses_max", 1),
                                 days=data.get("days", users.INVITE_DAYS_DEFAULT),
                                 note=str(data.get("note") or ""))
    log_event(f"Einladung ausgestellt (Verwendungen {invite['uses_max']}).")
    return 201, {"invite": {"code": users.format_code(invite["code"]),
                            "expires_at": invite["expires_at"],
                            "uses_max": invite["uses_max"],
                            "note": invite["note"]}}


@route("POST", r"^/api/admin/invites/([A-Za-z0-9-]{1,32})/revoke$", admin=True)
def h_admin_invite_revoke(req: Req, code: str):
    invite = users.revoke_invite(code)
    return {"invite": {"code": users.format_code(invite["code"]),
                       "revoked_at": invite["revoked_at"]}}


@route("GET", r"^/api/admin/passes$", admin=True)
def h_admin_passes(req: Req):
    now = store_hosted.now()
    uid = req.q("user", "")
    source = passes.passes_of(uid) if uid else passes.all_passes()
    return {"passes": [passes.public_pass(p, now=now) for p in source],
            "templates": passes.TEMPLATES,
            "limits": {"days": [passes.DAYS_MIN, passes.DAYS_MAX],
                       "max_concurrent": [passes.CONCURRENT_MIN, passes.CONCURRENT_MAX],
                       "ram_total_mb": [passes.RAM_TOTAL_MIN_MB, passes.RAM_TOTAL_MAX_MB]}}


@route("POST", r"^/api/admin/passes$", admin=True)
def h_admin_pass_issue(req: Req):
    data = req.json()
    entry = passes.issue_pass(str(data.get("user_id") or ""),
                              str(data.get("kind") or "local"),
                              data.get("days", 30),
                              data.get("max_concurrent", 1),
                              data.get("ram_total_mb", 4096),
                              issued_by=req.me["id"],
                              note=str(data.get("note") or ""))
    log_event(f"Pass {entry['id']} für {entry['user_id']} ausgestellt "
              f"({entry['kind']}, {entry['days']} Tage).")
    # Mit dem neuen Pass dürfen ruhende Server des Kontos wieder starten.
    geweckt = wake_parked(str(entry.get("user_id") or ""))
    return 201, {"pass": passes.public_pass(entry),
                 "wieder_startbar": [server_view(i) for i in geweckt]}


@route("POST", r"^/api/admin/passes/([A-Za-z0-9]{1,32})/revoke$", admin=True)
def h_admin_pass_revoke(req: Req, pid: str):
    entry = passes.revoke_pass(pid)
    uid = str(entry.get("user_id") or "")
    log_event(f"Pass {pid} zurückgezogen – die Server des Kontos werden angekündigt gestoppt.")
    threading.Thread(target=_park_nach_widerruf, args=(uid,),
                     daemon=True, name="park-after-revoke").start()
    return {"pass": passes.public_pass(entry),
            # Wann die Server wirklich ausgehen – die Oberfläche kann das anzeigen.
            "stoppt_um": widerruf_frist(uid, store_hosted.now())}


@route("POST", r"^/api/admin/passes/([A-Za-z0-9]{1,32})/extend$", admin=True)
def h_admin_pass_extend(req: Req, pid: str):
    data = req.json()
    entry = passes.extend_pass(pid, data.get("days", 30))
    log_event(f"Pass {pid} verlängert (bis {entry.get('expires_at')}).")
    geweckt = wake_parked(str(entry.get("user_id") or ""))
    return {"pass": passes.public_pass(entry),
            "wieder_startbar": [server_view(i) for i in geweckt]}


@route("POST", r"^/api/admin/passes/([A-Za-z0-9]{1,32})/limits$", admin=True)
def h_admin_pass_limits(req: Req, pid: str):
    data = req.json()
    entry = passes.set_limits(pid, max_concurrent=data.get("max_concurrent"),
                              ram_total_mb=data.get("ram_total_mb"))
    threading.Thread(target=_park_user, args=(str(entry.get("user_id") or ""),),
                     daemon=True, name="park-after-limits").start()
    geweckt = wake_parked(str(entry.get("user_id") or ""))
    return {"pass": passes.public_pass(entry),
            "wieder_startbar": [server_view(i) for i in geweckt]}


@route("DELETE", r"^/api/admin/passes/([A-Za-z0-9]{1,32})$", admin=True)
def h_admin_pass_delete(req: Req, pid: str):
    passes.delete_pass(pid)
    return {"ok": True}


@route("GET", r"^/api/admin/instances$", admin=True)
def h_admin_instances(req: Req):
    return {"instances": [server_view(i) for i in instances.all_instances()]}


@route("POST", r"^/api/admin/instances/([A-Za-z0-9._-]{1,64})/state$", admin=True)
def h_admin_state(req: Req, iid: str):
    data = req.json()
    inst = instances.set_state(iid, str(data.get("state") or ""), force=True)
    ensure_marke(inst)
    schreibe_routen(f"Zustand von {iid} von Hand gesetzt")
    log_event(f"Zustand von {iid} auf {inst.get('state')} gesetzt (Betreiber).")
    return {"server": server_view(instances.get_instance(iid) or inst)}


# --------------------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"mcsmd/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120                   # hängende Verbindungen blockieren keinen Thread auf Dauer

    # Kein eigenes Protokoll nach stderr – wir schreiben selbst nach api.log.
    def log_message(self, fmt, *args):                              # noqa: A003
        return

    def bearer_token(self) -> str:
        raw = self.headers.get("Authorization", "") or ""
        if raw[:7].lower() == "bearer ":
            return raw[7:].strip()
        return ""

    def cookie_token(self) -> str:
        """Sitzungstoken aus dem Cookie ``mcsm_sitzung`` (Verwaltung im Browser).

        Das Cookie ist ``SameSite=Lax``: der Browser schickt es bei einer von fremden Seiten
        ausgelösten POST-Anfrage nicht mit. Die Oberfläche selbst benutzt trotzdem den Kopf
        ``Authorization`` – das Cookie ist nur der bequeme Weg direkt nach der Anmeldung.
        """
        raw = self.headers.get("Cookie", "") or ""
        if COOKIE_NAME not in raw:
            return ""
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
        except http.cookies.CookieError:
            return ""
        keks = jar.get(COOKIE_NAME)
        return (keks.value or "").strip() if keks is not None else ""

    def session_token(self) -> str:
        return self.bearer_token() or self.cookie_token()

    def do_GET(self):                                               # noqa: N802
        self._handle("GET")

    def do_POST(self):                                              # noqa: N802
        self._handle("POST")

    def do_DELETE(self):                                            # noqa: N802
        self._handle("DELETE")

    def do_PUT(self):                                               # noqa: N802
        self._handle("PUT")

    def do_HEAD(self):                                              # noqa: N802
        self._handle("GET")

    # -- Antworten -----------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              headers: dict | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                # Eine Liste wird zu mehreren Kopfzeilen – nötig für zwei „Set-Cookie“ in einer
                # Antwort (Sitzung setzen und das Cookie des Anmeldevorgangs wegräumen).
                for einzeln in (value if isinstance(value, (list, tuple)) else [value]):
                    self.send_header(str(key), str(einzeln))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def _send_json(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    # -- Ablauf --------------------------------------------------------
    def _handle(self, method: str) -> None:
        started = time.time()
        user_id = ""
        status = 500
        parsed = urllib.parse.urlsplit(self.path)
        try:
            path = urllib.parse.unquote(parsed.path)
        except Exception:                                           # noqa: BLE001
            path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            status, user_id = self._dispatch(method, path, query)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            status = 499
        except Exception:                                           # noqa: BLE001
            log_event("Unerwarteter Fehler:\n" + traceback.format_exc())
            try:
                self._send_json(500, {"error": "Auf dem Root-Server ist ein Fehler aufgetreten – "
                                               "der Betreiber findet Näheres im Protokoll."})
            except Exception:                                       # noqa: BLE001
                self.close_connection = True
            status = 500
        millis = int((time.time() - started) * 1000)
        log_access(client_ip(self), method, parsed.path, status, millis, user_id)

    def _admin_host_ok(self, path: str) -> bool:
        """Dürfen die Betreiberwege unter diesem Hostnamen bedient werden?

        ``admin.<domain>`` ja, ``api.<domain>`` nein. Die Rückschleife (Selbstprüfung von
        deploy.sh, Selbsttests) darf ebenfalls – dort gibt es keinen DNS-Namen.
        """
        if not str(path or "").startswith("/api/admin/"):
            return True
        host = host_of(self)
        if host == api_host():
            return False
        return host == admin_host() or host in ("", "localhost", "127.0.0.1", "::1")

    def _bremse(self, gruppe: str, ip: str) -> int:
        """Ratenbremse anwenden. Rückgabe: 0 = weiter, sonst wurde schon eine 429 geschickt."""
        wartezeit, neu = BREMSE.pruefen(ip, gruppe)
        if not wartezeit:
            return 0
        if neu:
            log_event(f"Ratenbremse: {ip} hat bei „{gruppe}“ zu viele Anfragen geschickt und ist "
                      f"{wartezeit} Sekunden gesperrt.")
        self.close_connection = True
        minuten = max(1, wartezeit // 60)
        self._send_json(429, {"error": f"Zu viele Anfragen von deiner Adresse. Bitte etwa "
                                      f"{minuten} Minute(n) warten und dann erneut versuchen."},
                        {"Retry-After": str(wartezeit)})
        return wartezeit

    def _dispatch(self, method: str, path: str, query: dict) -> tuple:
        # Die Bremse greift **vor** allem anderen: Anmeldung und Einladungscodes sind streng
        # begrenzt (dort lohnt sich Raten), alles Übrige großzügig.
        ip = client_ip(self)
        warn_missing_forwarded(ip)
        gruppe = bremsgruppe(path)
        if gruppe and self._bremse(gruppe, ip):
            return 429, ""

        found = None
        other_methods = False
        for entry in ROUTES:
            match = entry["regex"].match(path)
            if not match:
                continue
            if entry["method"] == method:
                found = (entry, match)
                break
            other_methods = True
        if found is None:
            # Auch eine Flut auf unbekannte Adressen kostet Fäden – deshalb hier schon bremsen.
            if self._bremse("offen", ip):
                return 429, ""
            if other_methods:
                self.close_connection = True
                self._send_json(405, {"error": f"Diese Adresse nimmt kein {method}."})
                return 405, ""
            self.close_connection = True
            self._send_json(404, {"error": "Diese Adresse gibt es auf dem Root-Server nicht."})
            return 404, ""
        entry, match = found

        # Die Betreiberwege gibt es nur unter dem Verwaltungsnamen. nginx bedient beide Namen aus
        # einem Block; ohne diese Prüfung bliebe eine spätere Absicherung „admin nur aus dem
        # Büro-Netz“ wirkungslos, weil derselbe Weg über den API-Namen offenstünde.
        if not self._admin_host_ok(path):
            self.close_connection = True
            self._send_json(404, {"error": "Diese Adresse gibt es auf dem Root-Server nicht."})
            return 404, ""

        # Vorab-Bremse, **bevor** das Token aufgelöst wird: dafür werden sessions.json und
        # users.json gelesen. Die Bremse weiter unten zählt erst danach – eine Flut mit
        # erfundenen Tokens käme sonst beliebig oft bis hierher.
        if not gruppe and self._bremse("vorab", ip):
            return 429, ""

        # Erst die Anmeldung, dann der Körper. Andersherum könnte jeder, der den Dienst erreicht,
        # mit 200 gleichzeitigen „Content-Length: 16 MB“ und einem ungültigen Token über 3 GB
        # Arbeitsspeicher belegen, bevor überhaupt die 401 herausgeht.
        user = None
        user_id = ""
        auth_problem: ApiError | None = None
        token = self.session_token()
        try:
            if token:
                user = users.user_for_token(token)
            if entry["auth"] or entry["admin"]:
                if not token:
                    raise ApiError("Es fehlt die Anmeldung – erwartet wird der Kopf "
                                   "„Authorization: Bearer <token>“.", 401)
                if user is None:
                    raise ApiError("Bitte neu anmelden.", 401)
                if entry["admin"] and user.get("role") != "admin":
                    raise ApiError("Das darf nur der Betreiber.", 403)
            if user is not None:
                user_id = str(user.get("id") or "")
        except ApiError as exc:
            auth_problem = exc

        if auth_problem is not None:
            # Abgewiesene Anfragen zählen in einem eigenen Topf mit – sonst könnte jemand mit
            # Tokens um sich werfen, und ein abgelaufenes Token soll niemanden lange aussperren.
            if self._bremse(gruppe or "abgewiesen", ip):
                return 429, user_id
            # Den Körper gar nicht erst einlesen – die Verbindung wird ohnehin geschlossen.
            self.close_connection = True
            self._send_json(auth_problem.status, {"error": auth_problem.message})
            return auth_problem.status, user_id

        # Jetzt ist bekannt, ob der Aufrufer angemeldet ist: angemeldete dürfen viel (Hochladen,
        # Konsole, die Oberfläche fragt im 5-Sekunden-Takt), offene Aufrufe deutlich weniger.
        if not gruppe and self._bremse("angemeldet" if user is not None else "offen", ip):
            return 429, user_id

        limit = int(entry["limit"]) if user is not None else min(int(entry["limit"]), UNAUTH_LIMIT)
        body, problem = self._read_body(limit)
        if problem is not None:
            self.close_connection = True
            self._send_json(problem[0], {"error": problem[1]})
            return problem[0], user_id

        req = Req(self, query, body)
        if user is not None:
            req.user = user
        try:
            result = entry["fn"](req, *match.groups())
            status, payload, headers = self._unpack(result)
            if isinstance(payload, Raw):
                self._send(payload.status, payload.data, payload.content_type, payload.headers)
                return payload.status, user_id
            self._send_json(status, payload, headers)
            return status, user_id
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return exc.status, user_id
        except oauth.OAuthFehler as exc:
            # Die Module liefern fertige deutsche Sätze samt passendem Status.
            self._send_json(exc.status, {"error": exc.message})
            return exc.status, user_id
        except ports.PortsExhausted as exc:
            self._send_json(409, {"error": str(exc)})
            return 409, user_id
        except sources_linux.DiskFull as exc:
            self._send_json(507, {"error": str(exc)})
            return 507, user_id
        except ValueError as exc:
            code = status_for_value_error(exc)
            self._send_json(code, {"error": str(exc)})
            return code, user_id
        except OSError as exc:
            log_event(f"Systemfehler bei {method} {path}: {exc}")
            self._send_json(400, {"error": "Auf dem Root-Server ließ sich das gerade nicht "
                                           "ausführen (Platte oder Rechte)."})
            return 400, user_id

    @staticmethod
    def _unpack(result) -> tuple:
        if isinstance(result, Raw):
            return result.status, result, None
        if isinstance(result, tuple):
            if len(result) == 2:
                return int(result[0]), result[1], None
            if len(result) == 3:
                return int(result[0]), result[1], result[2]
            raise ValueError("Ungültige Antwort einer Route.")
        return 200, result, None

    def _read_body(self, limit: int) -> tuple:
        raw = self.headers.get("Content-Length", "")
        if raw == "" or raw is None:
            if (self.headers.get("Transfer-Encoding", "") or "").lower().strip() == "chunked":
                return b"", (400, "Stückweise Übertragung (chunked) nimmt dieser Dienst nicht an – "
                                  "bitte Content-Length setzen.")
            return b"", None
        try:
            length = int(raw)
        except ValueError:
            return b"", (400, "Der Kopf Content-Length ist keine Zahl.")
        if length < 0:
            return b"", (400, "Der Kopf Content-Length ist ungültig.")
        if length > limit:
            return b"", (400, f"Die Anfrage ist zu groß ({length} Bytes, erlaubt sind {limit}).")
        if length == 0:
            return b"", None
        try:
            data = self.rfile.read(length)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True
            return b"", (400, "Die Anfrage kam nicht vollständig an.")
        if len(data) != length:
            self.close_connection = True
            return b"", (400, "Die Anfrage kam nicht vollständig an.")
        return data, None


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP-Dienst mit begrenzt vielen gleichzeitigen Bearbeitungen.

    Ohne Obergrenze könnte jede Verbindung einen Faden und einen Anfragepuffer belegen; mit 16 MB
    je Stück wäre der Arbeitsspeicher der Maschine schnell weg. Wer über der Grenze ankommt,
    bekommt nach kurzer Wartezeit eine 503 statt eines abgebrochenen Fadens.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    timeout = 30
    max_workers = MAX_WORKERS
    wait_for_slot = 5.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(int(self.max_workers))

    def process_request_thread(self, request, client_address) -> None:
        if not self._slots.acquire(timeout=self.wait_for_slot):
            body = json.dumps({"error": "Der Root-Server ist gerade ausgelastet – bitte kurz "
                                        "warten und dann erneut versuchen."},
                              ensure_ascii=False).encode("utf-8")
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Type: application/json; charset=utf-8\r\n"
                                b"Connection: close\r\n"
                                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def build_server(host: str | None = None, port: int | None = None) -> Server:
    return Server((host if host is not None else bind_host(),
                   port if port is not None else bind_port()), Handler)


# --------------------------------------------------------------------------- Hintergrundarbeit

def companion_warn(run, seconds: int, reason: str) -> bool:
    """`mcsmstop <sekunden> <grund>`, wenn das Begleit-Plugin da ist und bestätigt."""
    if not getattr(run.active_spec, "companion", False):
        return False
    mark = run.console(0)["next"]
    try:
        run.send(f"mcsmstop {max(1, min(3600, int(seconds)))} {reason}")
    except ValueError:
        return False
    deadline = time.time() + runner.ANNOUNCE_ANSWER
    while time.time() < deadline:
        for line in run.console(mark)["lines"]:
            if "Herunterfahren angefordert" in line:
                return True
        time.sleep(0.2)
    return False


#: So alt darf ``status.json`` des Begleit-Plugins höchstens sein, damit die Spielerzahl zählt.
COMPANION_FRISCH = 120


def companion_spieler(inst: dict) -> int:
    """Wie viele Spieler das Begleit-Plugin zuletzt gemeldet hat (``-1`` = unbekannt)."""
    try:
        info = companion.status(instance_dir(inst), inst)
    except (OSError, ValueError):
        return -1
    daten = info.get("status")
    alter = info.get("age")
    if not isinstance(daten, dict) or alter is None:
        return -1
    try:
        if int(alter) > COMPANION_FRISCH:
            return -1
        return max(0, int(daten.get("online") or 0))
    except (TypeError, ValueError):
        return -1


def warn_expiring(entry: dict, now: int) -> int:
    """Die 10-Minuten-Warnung an alle laufenden Server eines Kontos. Rückgabe: Zahl der Server.

    Achtung bei ``mcsmstop``: Der Befehl ist eine **Abschaltanforderung**. Das Begleit-Plugin
    zählt damit im Spiel herunter und stoppt danach selbst – ist aber gerade **niemand** auf dem
    Server, fährt es ihn sofort herunter (``ShutdownService.start``). Als Vorwarnung eingesetzt
    hieße das: ein leerer Server geht zehn Minuten zu früh aus. Deshalb wird der Befehl nur
    geschickt, wenn das Plugin frische Spielerzahlen meldet **und** wirklich jemand da ist;
    sonst bleibt es bei ``say``. Gestoppt wird in jedem Fall erst zum Ablauf (`_park_user`),
    und dort kündigt der Runner den Stopp noch einmal an.
    """
    uid = str(entry.get("user_id") or "")
    left = max(1, int(entry.get("expires_at") or 0) - now)
    reason = "Pass läuft ab"
    count = 0
    for inst in instances.running_of(uid):
        run = REGISTRY.find(str(inst.get("id")))
        if run is None or not run.running:
            continue
        mit_plugin = companion_spieler(inst) > 0 and companion_warn(run, left, reason)
        if not mit_plugin:
            run.announce(f"Dein Pass {passes.remaining_text(entry, now=now)} – der Server wird "
                         f"dann gestoppt und die Welt gespeichert.")
        run.log(f"[Manager] Der Pass {passes.remaining_text(entry, now=now)}. "
                f"Der Server wird danach gestoppt.")
        count += 1
    return count


def widerruf_frist(uid: str, now: int) -> int:
    """Bis wann laufen die Server eines Kontos nach einem Widerruf noch weiter? (0 = sofort)

    Ein Widerruf trifft die Spieler mitten im Spiel. Die Rückfrage in der Betreiberoberfläche
    sagt ausdrücklich zu, dass die Server erst in **10 Minuten** heruntergefahren werden
    (spec-admin.md, Abschnitt „Pässe“) – ein Widerruf bekommt deshalb dieselbe Ankündigungsfrist
    wie ein Ablauf. Länger als bis zum ohnehin vorgesehenen Ende des Passes wird nicht gewartet.

    Gerechnet wird jedes Mal neu aus ``revoked_at``; ein Neustart des Dienstes verliert die
    Frist damit nicht.
    """
    stamp = int(now)
    ende = 0
    for entry in passes.passes_of(str(uid)):
        rev = int(entry.get("revoked_at") or 0)
        ablauf = int(entry.get("expires_at") or 0)
        if rev <= 0 or ablauf <= rev:
            continue                       # nicht zurückgezogen oder damals längst abgelaufen
        ende = max(ende, min(rev + WARN_MINUTES * 60, ablauf))
    return ende if ende > stamp else 0


def warn_revoked(uid: str, frist: int, now: int) -> int:
    """Einmalige Ankündigung nach einem Widerruf. Rückgabe: Zahl der gewarnten Server."""
    with _runtime_lock:
        gewarnt = dict(_runtime.get("widerruf_gewarnt") or {})
    if str(gewarnt.get(str(uid)) or "") == str(int(frist)):
        return 0
    rest = max(1, int(frist) - int(now))
    minuten = max(1, (rest + 59) // 60)
    count = 0
    for inst in instances.running_of(str(uid)):
        run = REGISTRY.find(str(inst.get("id")))
        if run is None or not run.running:
            continue
        if companion_spieler(inst) <= 0 or not companion_warn(run, rest, "Pass zurückgezogen"):
            run.announce(f"Der Pass für diesen Server wurde zurückgezogen – der Server wird in "
                         f"{minuten} Minute(n) gestoppt und die Welt gespeichert.")
        run.log(f"[Manager] Der Pass wurde zurückgezogen. Der Server wird in {minuten} "
                f"Minute(n) gestoppt; die Welt wird dabei gespeichert.")
        count += 1
    with _runtime_lock:
        gewarnt[str(uid)] = str(int(frist))
        _runtime["widerruf_gewarnt"] = gewarnt
    runtime_save()
    log_event(f"Pass von {uid} zurückgezogen – {count} laufende Server angekündigt gestoppt "
              f"(in {minuten} Minute(n)).")
    return count


def _widerruf_vergessen(uid: str) -> None:
    with _runtime_lock:
        gewarnt = dict(_runtime.get("widerruf_gewarnt") or {})
        if str(uid) not in gewarnt:
            return
        gewarnt.pop(str(uid), None)
        _runtime["widerruf_gewarnt"] = gewarnt
    runtime_save()


def stop_for_expiry(inst: dict, announce: int) -> None:
    run = REGISTRY.find(str(inst.get("id")))
    if run is None or not run.running:
        return
    log_event(f"Server {inst.get('id')} wird gestoppt (Pass abgelaufen oder zurückgezogen).")
    try:
        run.stop(timeout=STOP_TIMEOUT, announce_seconds=announce)
    except Exception as exc:                                        # noqa: BLE001
        log_event(f"Beim Stoppen von {inst.get('id')} gab es einen Fehler: {exc}")


def wipe_instance_folder(inst: dict, grund: str) -> dict:
    """Den Ordner einer Instanz auf dem Root löschen (nach Abbruch einer Übertragung).

    Wird nur gerufen, wenn der Zustand ausdrücklich „nur auf deinem PC“ ist – dann darf dort
    nichts mehr liegen. `transfer.delete_tree` prüft selbst, dass der Pfad unterhalb des
    Benutzerordners liegt.
    """
    iid = str(inst.get("id") or "")
    folder = instance_dir(inst)
    if not folder.is_dir():
        return {"deleted": False, "files": 0, "bytes": 0}
    grant_group_for(inst)
    try:
        out = transfer.delete_tree(folder, sources_linux.users_dir())
    except (ValueError, OSError) as exc:
        log_event(f"Der Ordner von {iid} ließ sich nicht löschen ({grund}): {exc}")
        return {"deleted": False, "files": 0, "bytes": 0}
    try:
        instances.set_size(iid, 0)
    except ValueError:
        pass
    log_event(f"Der Ordner von {iid} wurde auf dem Root gelöscht ({grund}; "
              f"{out.get('files')} Dateien, {transfer.human(int(out.get('bytes') or 0))}).")
    return out


def _woken_merken(instance_id: str, now: int | None = None) -> None:
    """„Ruht nicht mehr“ vermerken, damit der Besitzer eine Meldung bekommt."""
    stamp = store_hosted.now() if now is None else int(now)
    with _runtime_lock:
        woken = dict(_runtime.get("woken") or {})
        woken[str(instance_id)] = stamp
        _runtime["woken"] = woken
    runtime_save()


def _woken_vergessen(instance_id: str) -> None:
    """Die Meldung ist erledigt (gestartet, zurückgeholt, gelöscht)."""
    with _runtime_lock:
        woken = dict(_runtime.get("woken") or {})
        if str(instance_id) not in woken:
            return
        woken.pop(str(instance_id), None)
        _runtime["woken"] = woken
    runtime_save()


def _woken_aufraeumen(now: int) -> None:
    """Alte Meldungen und solche zu Instanzen, die nicht mehr „gehostet“ sind, wegwerfen."""
    with _runtime_lock:
        woken = dict(_runtime.get("woken") or {})
    if not woken:
        return
    behalten = {}
    for iid, stamp in woken.items():
        if now - int(stamp or 0) > WOKEN_KEEP_SECONDS:
            continue
        inst = instances.get_instance(str(iid))
        if not inst or str(inst.get("state")) != "hosted" or instances.is_running(inst):
            continue
        behalten[str(iid)] = int(stamp or 0)
    if behalten != woken:
        with _runtime_lock:
            _runtime["woken"] = behalten
        runtime_save()


def wake_parked(uid: str, now: int | None = None) -> list:
    """Ruhende Instanzen wieder startbar machen, sobald ein passender Pass gilt.

    Der Rückweg ``suspended -> hosted`` (bei lokalen Servern ``awaiting_pull -> hosted``) fehlte
    bisher ganz: ein ruhender Premium-Server blieb auch mit einem frischen, gültigen Pass
    unstartbar, weil `instances.check_start` den Zustand prüft, und den Zustand hat niemand
    zurückgestellt.

    Gestartet wird hier **nichts** – das entscheidet der Besitzer. Er bekommt nur die Meldung,
    dass es wieder geht (siehe `notices_for`).
    """
    if not uid:
        return []
    stamp = store_hosted.now() if now is None else int(now)
    active = passes.active_passes(str(uid), now=stamp)
    if not active:
        return []
    geweckt: list = []
    with store_hosted.lock():
        for inst in instances.instances_of(str(uid)):
            state = str(inst.get("state") or "")
            if state not in ("suspended", "awaiting_pull"):
                continue
            origin = str(inst.get("origin") or "local")
            if not any(passes.allows_origin(p, origin) for p in active):
                continue
            try:
                geweckt.append(instances.set_state(str(inst.get("id")), "hosted", now=stamp))
            except ValueError as exc:
                log_event(f"„{inst.get('name')}“ ließ sich nicht wieder startbar machen: {exc}")
    for inst in geweckt:
        iid = str(inst.get("id"))
        _woken_merken(iid, now=stamp)
        ensure_marke(inst)
        log_event(f"Instanz {iid} („{inst.get('name')}“) ruht nicht mehr – es liegt wieder ein "
                  f"gültiger Pass vor. Gestartet wurde sie nicht.")
    if geweckt:
        schreibe_routen("Instanzen wieder startbar")
    return geweckt


def _park_nach_widerruf(uid: str) -> None:
    """Nach einem Widerruf: sofort ansagen, zum Ende der Frist stoppen.

    Die Überwachungsschleife (`tick`) macht dasselbe noch einmal – sie ist das Netz darunter,
    falls der Dienst zwischendurch neu startet.
    """
    _park_user(uid)
    while not STOP_EVENT.is_set():
        rest = widerruf_frist(uid, store_hosted.now()) - store_hosted.now()
        if rest <= 0:
            break
        if STOP_EVENT.wait(min(30, max(1, rest))):
            return
    _park_user(uid)


def _park_user(uid: str, now: int | None = None) -> None:
    """Nicht mehr gedeckte Server stoppen und parken, überzählige Server stoppen."""
    if not uid:
        return
    stamp = store_hosted.now() if now is None else int(now)
    try:
        uncovered = instances.uncovered(uid, now=stamp)
        # Nach einem Widerruf bekommen die Spieler dieselbe Ankündigungsfrist wie beim Ablauf:
        # erst ansagen, dann in zehn Minuten stoppen – nicht mitten im Spiel abschalten.
        frist = widerruf_frist(uid, stamp) if uncovered else 0
        if frist:
            warn_revoked(uid, frist, stamp)
            return
        _widerruf_vergessen(uid)
        for inst in uncovered:
            stop_for_expiry(inst, runner.STOP_COUNTDOWN)
        if uncovered:
            parked = instances.park_uncovered(uid, now=stamp)
            for inst in parked:
                POOL.release(str(inst.get("id")))
                log_event(f"Instanz {inst.get('id')} steht jetzt auf "
                          f"„{instances.state_text(inst.get('state'))}“.")
            if parked:
                save_ports()
                schreibe_routen(f"Instanzen von {uid} geparkt")
        for inst in instances.excess_running(uid, now=stamp):
            stop_for_expiry(inst, runner.STOP_COUNTDOWN)
            try:
                instances.set_running(str(inst.get("id")), False)
            except ValueError:
                pass
    except ValueError as exc:
        log_event(f"Beim Aufräumen für Konto {uid} gab es einen Fehler: {exc}")


def save_all_worlds() -> int:
    """`save-all` an alle laufenden Java-Server schicken. Rückgabe: Anzahl."""
    count = 0
    for run in REGISTRY.all():
        try:
            if not run.running or not run.ready or not run.active_spec.is_java:
                continue
            run.send("save-all")
            count += 1
        except (ValueError, OSError):
            continue
    return count


def housekeeping(now: int) -> None:
    removed = users.purge_sessions(now=now)
    if removed:
        log_event(f"{removed} alte Sitzungen aufgeräumt.")
    # Zustandswerte, Abholcodes und angefangene Anmeldungen leben nur im Arbeitsspeicher.
    oauth.zustaende_aufraeumen(now=now)
    oauth.abholcodes_aufraeumen(now=now)
    login_vorgaenge_aufraeumen(now=now)
    try:
        frei = routen.sperrliste_aufraeumen(now=now)
        if frei:
            log_event(f"{frei} zurückgehaltene Unterdomäne(n) sind wieder frei.")
    except (OSError, ValueError) as exc:
        log_event(f"Die zurückgehaltenen Unterdomänen ließen sich nicht aufräumen: {exc}")
    try:
        gone = transfer.cleanup_old(transfer_store())
        if gone:
            log_event(f"{len(gone)} liegengebliebene Übertragungen aufgeräumt.")
    except OSError as exc:
        log_event(f"Übertragungen ließen sich nicht aufräumen: {exc}")
    # „Nur auf deinem PC“ und trotzdem ein Ordner auf dem Root: das darf es nicht geben (eine
    # abgebrochene Übertragung ließ früher fertige Dateien liegen). Hier wird es sichergestellt.
    for inst in instances.all_instances():
        if str(inst.get("state")) != "local_only":
            continue
        folder = instance_dir(inst)
        try:
            leer = not folder.is_dir() or not any(folder.iterdir())
        except OSError:
            continue
        if leer:
            continue
        log_event(f"Instanz {inst.get('id')} gilt als „nur auf dem PC“, auf dem Root liegen aber "
                  f"noch Dateien – sie werden jetzt entfernt.")
        wipe_instance_folder(inst, "Zustand „nur auf deinem PC“")
    for inst in instances.all_instances():
        if str(inst.get("state")) in ("local_only",) or instances.is_running(inst):
            continue
        folder = instance_dir(inst)
        if not folder.is_dir():
            continue
        try:
            instances.set_size(str(inst.get("id")), transfer.dir_stats(folder)["bytes"])
        except (ValueError, OSError):
            continue
    candidates = instances.premium_delete_candidates(now=now)
    if candidates:
        names = instances.name_list([i.get("name") for i in candidates])
        log_event(f"{len(candidates)} ruhende Premium-Instanzen liegen länger als "
                  f"{instances.PREMIUM_KEEP_DAYS} Tage ohne Pass: {names} – der Betreiber darf sie "
                  f"löschen (der Daemon löscht nichts von selbst).")
    free = instances.free_disk_bytes()
    try:
        total = shutil.disk_usage(str(data_root())).total
    except OSError:
        total = 0
    if free is not None and total and free / total < 0.15:
        log_event(f"Achtung: auf dem Root-Server sind nur noch {transfer.human(free)} "
                  f"({free / total * 100:.0f} %) frei.")


def tick(now: int | None = None) -> None:
    """Ein Durchlauf der Überwachungsschleife (etwa jede Minute)."""
    stamp = store_hosted.now() if now is None else int(now)

    # 1. Vorwarnung
    with _runtime_lock:
        warned = dict(_runtime.get("warned") or {})
    for entry in passes.expiring_soon(WARN_MINUTES, now=stamp):
        key = str(entry.get("id"))
        if str(warned.get(key) or "") == str(entry.get("expires_at")):
            continue
        count = warn_expiring(entry, stamp)
        warned[key] = str(entry.get("expires_at"))
        log_event(f"Pass {key} {passes.remaining_text(entry, now=stamp)} – "
                  f"{count} laufende Server gewarnt.")

    # 2. Abgelaufene oder zurückgezogene Pässe merken (fürs Protokoll)
    since = int(_runtime.get("last_expiry_check") or 0) or (stamp - TICK_SECONDS)
    for entry in passes.newly_expired(since, now=stamp):
        log_event(f"Pass {entry.get('id')} von {entry.get('user_id')} ist "
                  f"{'zurückgezogen' if entry.get('revoked_at') else 'abgelaufen'}.")
        warned.pop(str(entry.get("id")), None)

    # 3. Nicht mehr gedeckte und überzählige Server (auch nach einem Widerruf) – und der
    #    Rückweg: ruhende Server, für die wieder ein gültiger Pass da ist, werden startbar.
    for user in users.all_users():
        uid = str(user.get("id"))
        _park_user(uid, now=stamp)
        wake_parked(uid, now=stamp)
    _woken_aufraeumen(stamp)

    # 4. Welten regelmäßig sichern. Stirbt der Daemon unerwartet, beenden die aufgesammelten
    #    Prozesse sich beim nächsten Start mit SIGTERM – zwischen zwei Autosaves liegt dann
    #    höchstens diese Zeit an Spielfortschritt.
    with _runtime_lock:
        save_due = stamp - int(_runtime.get("save_all_at") or 0) >= SAVE_ALL_SECONDS
        if save_due:
            _runtime["save_all_at"] = stamp
    if save_due:
        save_all_worlds()

    with _runtime_lock:
        _runtime["warned"] = {k: v for k, v in warned.items()
                              if passes.get_pass(k) is not None}
        _runtime["last_expiry_check"] = stamp
        due = stamp - int(_runtime.get("housekeeping_at") or 0) >= HOUSEKEEPING_SECONDS
        if due:
            _runtime["housekeeping_at"] = stamp
    runtime_save()
    if due:
        housekeeping(stamp)


def worker_loop() -> None:
    while not STOP_EVENT.is_set():
        try:
            tick()
        except Exception:                                           # noqa: BLE001
            log_event("Fehler in der Überwachungsschleife:\n" + traceback.format_exc())
        STOP_EVENT.wait(TICK_SECONDS)


# --------------------------------------------------------------------------- Start

def ensure_first_admin() -> None:
    """Beim allerersten Start einen Einladungscode für das Admin-Konto anlegen."""
    if users.count_admins() > 0:
        oauth.erstzugang_setzen("")
        return
    code = str(_runtime.get("admin_invite") or "")
    invite = users.get_invite(code) if code else None
    if invite is None or users.invite_state(invite) != "open":
        invite = users.create_invite("system", uses_max=1, days=0,
                                     note="Erstes Admin-Konto (vom Dienst angelegt)")
        with _runtime_lock:
            _runtime["admin_invite"] = invite["code"]
        runtime_save()
    # Auch der Weg über Discord verlangt diesen Code, sonst bekäme auf einer frischen Anlage
    # schlicht der Schnellste das Betreiberkonto (siehe core/oauth.py, `konto_fuer`).
    oauth.erstzugang_setzen(invite["code"])
    pretty = users.format_code(invite["code"])
    path = store_hosted.data_dir() / ADMIN_FILE
    text = (
        "Minecraft Server Manager – erstes Admin-Konto\n"
        "=============================================\n\n"
        f"Einladungscode: {pretty}\n\n"
        "Im Programm auf dem PC unter „Gehostet“ diesen Code einlösen. Das erste damit\n"
        "angelegte Konto wird automatisch Betreiber (Rolle admin) – danach verliert der\n"
        "Code seine Wirkung und diese Datei wird gelöscht.\n\n"
        "Der Code ist ein Geheimnis: Wer ihn hat, bekommt das Betreiberkonto.\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        log_event(f"{ADMIN_FILE} konnte nicht geschrieben werden: {exc}")
    # Der Code selbst darf **nicht** ins Protokoll: das liegt als Datei unter /var/log/mcsm und
    # zusätzlich im systemd-Journal (Gruppe adm/systemd-journal lesen mit). Wer ihn hat, bekommt
    # das Betreiberkonto – er steht nur in der Datei, die allein root und mcsm lesen dürfen.
    log_event(f"Noch kein Betreiberkonto vorhanden. Der Einladungscode für das erste "
              f"Admin-Konto steht in {path} (nur für root und den Dienstbenutzer lesbar).")


def _reserve_ports_for_running(inst: dict, folder: pathlib.Path) -> dict:
    """Die Ports eines übernommenen Servers zurückbuchen, damit sie niemand doppelt bekommt.

    Normalerweise stehen sie in der Laufzeitablage (``daemon.json``) und werden beim Start
    wiederhergestellt. Fehlt die Ablage, kommen sie aus ``server.properties`` – dort steht, womit
    der Server wirklich gestartet ist. Der Bind-Versuch muss dabei ausfallen: die Ports sind ja
    besetzt, genau vom Server, um den es geht.
    """
    iid = str(inst.get("id") or "")
    vorhanden = ports_live_of(iid)
    if vorhanden.get("port"):
        return vorhanden
    props = read_properties(folder / "server.properties")
    try:
        tcp = int(props.get("server-port") or 0)
    except ValueError:
        tcp = 0
    if tcp:
        try:
            POOL.reserve(iid, ports.JAVA_POOL, tcp, probe=False)
        except (ValueError, ports.PortsExhausted) as exc:
            log_event(f"Der Port {tcp} von {iid} ließ sich nicht zurückbuchen: {exc}")
    roh = str(props.get("server-udp-ports") or "").split(",")[0].strip()
    if roh:
        teile = roh.split("-")
        try:
            erster = int(teile[0])
            letzter = int(teile[-1]) if len(teile) > 1 else erster
        except ValueError:
            erster = letzter = 0
        if erster and letzter >= erster:
            try:
                POOL.reserve(iid, ports.BEDROCK_POOL, erster, letzter - erster + 1, probe=False)
            except (ValueError, ports.PortsExhausted) as exc:
                log_event(f"Der UDP-Block {roh} von {iid} ließ sich nicht zurückbuchen: {exc}")
    return ports_live_of(iid)


def adopt_running() -> None:
    """Server aus einem früheren Lauf einsammeln – weiterlaufen lassen, nicht abschießen.

    Die Server laufen in eigener Sitzung und überleben den Dienst absichtlich (die Unit setzt
    ``KillMode=process``). Ein Update des Dienstes soll für die Spieler keine Zwangsunterbrechung
    sein, deshalb gilt hier:

    * Sagt der Datensatz „läuft“ und der Zustand ist ``hosted``, wird der Prozess **übernommen**:
      Ports zurückbuchen, Runner anhängen, Konsole so weit wie möglich wieder mitlesen. Befehle
      nimmt der Server erst nach dem nächsten eigenen Start an – das sagt der Runner auch.
    * Nur wirklich **verwaiste** Prozesse werden gestoppt: solche, deren Instanz gar nicht mehr
      „läuft“ oder nicht mehr auf dem Root liegt (geparkt, zurückgeholt, gelöscht).
    """
    uebernommen: list = []
    beendet: list = []
    for inst in instances.all_instances():
        iid = str(inst.get("id") or "")
        try:
            spec = build_spec(inst, require_java=False)
        except (ValueError, ApiError) as exc:
            log_event(f"Für {iid} ließen sich die Startangaben nicht bilden ({exc}) – "
                      f"ein etwaiger Prozess bleibt unangetastet.")
            if inst.get("running"):
                try:
                    instances.set_running(iid, False)
                except ValueError:
                    pass
            continue
        try:
            pid = runner.orphan_pid(spec)
        except (ValueError, OSError):
            pid = None
        if pid is None:
            if inst.get("running"):
                log_event(f"Der Datensatz von {iid} stand auf „läuft“, es läuft aber kein "
                          f"Prozess mehr – der Eintrag wird berichtigt.")
                try:
                    instances.set_running(iid, False)
                except ValueError:
                    pass
            continue
        weiter = str(inst.get("state") or "") == "hosted" and bool(inst.get("running"))
        if not weiter:
            log_event(f"Server {iid} läuft noch, gehört aber nicht mehr hierher "
                      f"(Zustand „{instances.state_text(inst.get('state'))}“, "
                      f"Datensatz „{'läuft' if inst.get('running') else 'aus'}“) – "
                      f"er wird sauber beendet.")
            try:
                runner.stop_orphan(spec)
            except (ValueError, OSError) as exc:
                log_event(f"Der Server {iid} ließ sich nicht beenden: {exc}")
            beendet.append(iid)
            if inst.get("running"):
                try:
                    instances.set_running(iid, False)
                except ValueError:
                    pass
            continue
        _, seit = runner.pid_file_info(spec)
        try:
            zuteilung = _reserve_ports_for_running(inst, instance_dir(inst))
            if zuteilung:
                mirror_ports(inst, zuteilung)
            REGISTRY.adopt(spec, pid, started_at=seit)
        except (ValueError, OSError) as exc:
            log_event(f"Server {iid} ließ sich nicht übernehmen ({exc}) – er wird beendet.")
            try:
                runner.stop_orphan(spec)
            except (ValueError, OSError):
                pass
            try:
                instances.set_running(iid, False)
            except ValueError:
                pass
            beendet.append(iid)
            continue
        uebernommen.append(iid)
        log_event(f"Server {iid} („{inst.get('name')}“) läuft weiter (Prozess {pid}) und wurde "
                  f"übernommen – die Spieler haben nichts gemerkt. Die Konsole zeigt Zeilen erst "
                  f"ab jetzt.")
    if uebernommen:
        log_event(f"{len(uebernommen)} laufende Server übernommen: {', '.join(uebernommen)}.")
    if beendet:
        log_event(f"{len(beendet)} verwaiste Server beendet: {', '.join(beendet)}.")


#: Alter Name – die Selbsttests und Notizen von früher rufen ihn noch.
collect_orphans = adopt_running


def startup() -> None:
    root = sources_linux.set_data_root(data_root())
    os.environ.setdefault("MCSM_DATA", str(root / "data"))
    sources_linux.ensure_dirs()
    runner.set_instances_root(sources_linux.users_dir())
    store_hosted.ensure_dir()
    for kind in store_hosted.KINDS:
        store_hosted.load(kind)                 # beschädigte Datei = ValueError, laut melden
    runtime_load()
    with _runtime_lock:
        _runtime["started_at"] = store_hosted.now()
    notes = POOL.restore(_runtime.get("ports") or [])
    for note in notes:
        log_event(note)
    # Der Verteiler sitzt auf 25565 – der Port muss gebucht sein, sonst bekommt ihn die erste
    # Instanz und niemand käme mehr über die Unterdomänen herein.
    reserve_router_port()
    log_event(oauth.startmeldung())
    oauth.alles_leeren()                # angefangene Anmeldungen aus dem letzten Lauf verfallen
    warnung = isolation.warn_once()
    if warnung:
        log_event(warnung)
    else:
        lage = isolation.status()
        if lage["aktiv"]:
            log_event(f"Trennung der Server-Benutzer ist aktiv ({lage['benutzer']} Benutzer im "
                      f"Vorrat) – jedes Konto läuft unter einem eigenen Unix-Benutzer.")
    adopt_running()
    save_ports()
    # Unterdomänen nachtragen (Instanzen aus der Zeit vor dieser Fassung haben noch keine) und
    # die Zuordnungstabelle einmal frisch schreiben – der Verteiler liest sie von selbst neu.
    for inst in instances.all_instances():
        ensure_marke(inst)
    schreibe_routen("Start des Dienstes")
    ensure_first_admin()


_shutdown_lock = threading.Lock()
_shutdown_state = {"done": False}


def shutdown(server: Server | None = None) -> None:
    """Sauber beenden. Nur einmal wirksam.

    **Die Kundenserver bleiben an.** Ein Neustart des Dienstes – und damit jedes Update – wäre
    sonst eine Zwangsunterbrechung für alle Spieler: der alte Stand hat hier jeden Server
    angekündigt gestoppt. Die Server laufen in eigener Sitzung (``setsid``) und hängen an keinem
    Elternprozess; der neu gestartete Dienst sammelt sie in `adopt_running` wieder ein. Damit
    zwischen zwei Autosaves möglichst wenig Spielfortschritt liegt, bekommt vorher jeder laufende
    Server noch ein ``save-all``.

    Der Datensatz bleibt absichtlich auf „läuft“ – daran erkennt der nächste Start, dass der
    Prozess dazugehört und übernommen werden darf.

    Wer wirklich alles anhalten will (geplante Wartung, Maschine geht aus), setzt
    ``MCSM_STOP_SERVERS=1``.

    Läuft absichtlich im Hauptfaden (nach ``serve_forever``) – ein Hintergrundfaden würde beim
    Ende des Prozesses mitten im Speichern der Welt abgeschnitten.
    """
    with _shutdown_lock:
        if _shutdown_state["done"]:
            return
        _shutdown_state["done"] = True
    STOP_EVENT.set()
    if server is not None:
        try:
            server.shutdown()
        except Exception:                                           # noqa: BLE001
            pass
    laufende = REGISTRY.running_ids()
    if stop_servers_on_exit():
        log_event("Der Dienst hält an – alle Server werden angekündigt gestoppt "
                  "(MCSM_STOP_SERVERS=1).")
        try:
            REGISTRY.stop_all(timeout=60, announce_seconds=runner.STOP_COUNTDOWN)
        except Exception:                                           # noqa: BLE001
            log_event("Beim Stoppen der Server:\n" + traceback.format_exc())
        for inst in instances.all_instances():
            if inst.get("running"):
                try:
                    instances.set_running(str(inst.get("id")), False)
                except ValueError:
                    pass
    elif laufende:
        log_event(f"Der Dienst hält an. {len(laufende)} Server laufen absichtlich weiter "
                  f"({', '.join(laufende)}) – die Spieler bleiben im Spiel, und der nächste "
                  f"Start übernimmt sie wieder. Vorher wird die Welt gesichert.")
        try:
            gesichert = save_all_worlds()
            if gesichert:
                log_event(f"{gesichert} Welten gesichert (save-all).")
        except Exception:                                           # noqa: BLE001
            log_event("Beim Sichern der Welten:\n" + traceback.format_exc())
    else:
        log_event("Der Dienst hält an – es läuft kein Server.")
    save_ports()
    log_event("Der Dienst ist beendet.")


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--version" in argv:
        print(f"mcsmd {VERSION}")
        return 0
    try:
        startup()
    except ValueError as exc:
        log_event(f"Der Dienst startet nicht: {exc}")
        print(f"[mcsmd] Start abgebrochen: {exc}", file=sys.stderr, flush=True)
        return 2
    if "--check" in argv:
        log_event("Selbstprüfung in Ordnung – der Dienst wird nicht gestartet (--check).")
        return 0

    host, port = bind_host(), bind_port()
    try:
        server = build_server(host, port)
    except OSError as exc:
        log_event(f"Die Adresse {host}:{port} lässt sich nicht belegen: {exc}")
        return 3
    log_event(f"mcsmd {VERSION} hört auf {host}:{port} "
              f"(Daten {data_root()}, Python {sys.version.split()[0]}).")

    worker = threading.Thread(target=worker_loop, daemon=True, name="paesse")
    worker.start()

    def on_signal(signum, _frame):
        # Im Signalhandler wird nur geweckt: serve_forever läuft im Hauptfaden und darf nicht
        # von hier aus angehalten werden. Das Stoppen der Server macht der Hauptfaden danach.
        log_event(f"Signal {signum} bekommen – der Dienst hält an.")
        STOP_EVENT.set()
        threading.Thread(target=server.shutdown, daemon=True, name="wecken").start()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()                      # im Hauptfaden: der Prozess wartet, bis alles steht
        try:
            server.server_close()
        except OSError:
            pass
        close_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
