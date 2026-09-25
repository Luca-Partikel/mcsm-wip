"""Anbindung an den Root-Server (Cloud): Anmeldung, Pässe, entfernte Server, Übertragungen.

Die Gegenstelle ist der Daemon aus dem Ordner ``hosted/`` unter https://api.arcardia-nexus.de
(siehe hosted/ARCHITEKTUR.md und hosted/spec-cloud-client.md). Es werden ausschließlich Module der
Standardbibliothek verwendet; bei kaputter Zertifikatsprüfung springt – wie in core/sources.py –
das in Windows enthaltene curl.exe ein.

Sicherheit
----------
* Das Sitzungstoken liegt allein in ``data/cloud-token.json`` (Rechte nur für den angemeldeten
  Benutzer, unter Windows zusätzlich per ``icacls``). Es steht nie in ``data/cloud.json``,
  nie im Protokoll, nie in einer Antwort an die Oberfläche und nie in einer Kommandozeile
  (curl bekommt den Kopf über eine Konfigurationsdatei, nicht als Argument).
* Antwortet der Root-Server mit 401, wird das Token gelöscht und die Oberfläche fordert eine
  neue Anmeldung an.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import manager, sources, store

log = logging.getLogger("mcsm")

# --------------------------------------------------------------------------- Festwerte

BASE_URL = (os.environ.get("MCSM_CLOUD_API") or "https://api.arcardia-nexus.de").rstrip("/")
HOST_DOMAIN = "arcardia-nexus.de"          # Adressen der gehosteten Server: <name>.arcardia-nexus.de
ROOT_IPV4 = "45.132.89.224"                # feste IPv4 des Root-Servers (Notfall-Adresse)

STATE_FILE = store.DATA_DIR / "cloud.json"          # ohne Token: Verknüpfungen, Sitzungen, Konto-Anzeige
TOKEN_FILE = store.DATA_DIR / "cloud-token.json"    # nur das Token, Rechte eingeschränkt
CLOUD_DIR = store.DATA_DIR / "cloud"                # Manifeste laufender Rückholungen
TMP_DIR = store.DATA_DIR / "cloud-tmp"              # Zwischendateien für den curl-Rückfall
DOWNLOAD_DIR = store.DATA_DIR / "cloud-downloads"   # einzeln geholte Dateien (Welt, Protokoll …)

TIMEOUT = 45                     # Sekunden für gewöhnliche Aufrufe
TRANSFER_TIMEOUT = 300           # Sekunden für ein Stück (8 MB) – langsame Leitungen brauchen Luft
CHUNK_SIZE = 8 * 1024 * 1024     # gleiche Stückgröße wie hosted/core/transfer.py
MANIFEST_VERSION = 1
MAX_FILES = 200_000
MAX_FILE_BYTES = 6 * 1024 ** 3
MAX_TOTAL_BYTES = 20 * 1024 ** 3
CACHE_SECONDS = 5.0              # Konto/Serverliste kurz zwischenspeichern (die Oberfläche fragt oft)
STOP_WAIT = 150                  # so lange auf das Stoppen eines entfernten Servers warten
TRANSFER_TRIES = 5               # so oft einen abgerissenen Übertragungsschritt neu versuchen

# Verwaltungsdateien der Gegenseite (paths.RESERVED_NAMES) und Teil-Dateien gehören nie ins Manifest.
RESERVED_NAMES = {".mcsm", "mcsm.json", "mcsm-instanz.json"}
PART_SUFFIX = ".mcsmpart"
# Ordner, die auf dem PC bleiben: Sicherungen sind lokal gemeint und würden die Übertragung vervielfachen.
SKIP_TOP_DIRS = {"backups"}
# Namensregeln von hosted/core/paths.py, damit ein Pfad auf beiden Seiten gültig ist.
_BAD_CHARS = set('<>:"|?*\\')
_DEVICE_NAMES = {"con", "prn", "aux", "nul", "clock$",
                 *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MAX_NAME_LEN = 200
MAX_REL_LEN = 1024
MAX_DEPTH = 32

# Zustände einer Instanz auf dem Root (hosted/core/instances.py)
LOCKED_STATES = ("uploading", "hosted", "awaiting_pull", "downloading", "suspended")
STATE_TEXTS = {
    "local_only": "nur auf diesem PC",
    "uploading": "wird hochgeladen",
    "hosted": "auf dem Root-Server",
    "awaiting_pull": "wartet auf die Rückholung",
    "downloading": "wird zurückgeholt",
    "suspended": "ruht auf dem Root-Server",
}
#: Rollen, wie der Root-Server sie führt – hier nur die Anzeige dazu.
ROLE_TEXTS = {"user": "Benutzer", "admin": "Betreiber", "owner": "Betreiber"}

_lock = threading.RLock()
_ssl_broken = False                                        # nach dem ersten Zertifikatsfehler curl nutzen
_cache: dict[str, tuple[float, object]] = {}
_login: dict = {}                                          # laufende Discord-Anmeldung (nur im Speicher!)


class CloudError(RuntimeError):
    """Fehler vom Root-Server oder auf dem Weg dorthin – die Meldung ist für die Oberfläche."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status)


class AuthError(CloudError):
    """Nicht (mehr) angemeldet – die Oberfläche muss eine neue Anmeldung anbieten."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or "Die Anmeldung am Root-Server ist abgelaufen – "
                                    "bitte melde dich neu an.", 401)


# --------------------------------------------------------------------------- Dateien und Rechte

def _restrict(path: pathlib.Path) -> None:
    """Zugriff auf den angemeldeten Benutzer beschränken (Token, Zwischendateien)."""
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass
    if sys.platform != "win32":
        return
    who = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if not who:
        return
    account = f"{domain}\\{who}" if domain else who
    icacls = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    if not icacls.exists():
        return
    try:
        subprocess.run([str(icacls), str(path), "/inheritance:r", "/grant:r", f"{account}:(F)"],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass                                               # ohne eingeschränkte Rechte, aber nutzbar


def _tmp_dir() -> pathlib.Path:
    if not TMP_DIR.is_dir():
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        _restrict(TMP_DIR)
    return TMP_DIR


def _atomic_json(path: pathlib.Path, data: dict, *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    if secret:
        _restrict(tmp)
    tmp.replace(path)
    if secret:
        _restrict(path)


# --------------------------------------------------------------------------- Zustand (ohne Token)

def _read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1}
    return data if isinstance(data, dict) else {"version": 1}


def _write_state(data: dict) -> None:
    data["version"] = 1
    data["saved_at"] = int(time.time())
    _atomic_json(STATE_FILE, data)


def _update_state(change) -> dict:
    with _lock:
        data = _read_state()
        change(data)
        _write_state(data)
        return data


# --------------------------------------------------------------------------- Token

def _read_token() -> dict:
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not str(data.get("token") or ""):
        return {}
    expires = int(data.get("expires_at") or 0)
    if expires and expires < time.time():
        return {}
    return data


def _token() -> str:
    with _lock:
        return str(_read_token().get("token") or "")


def _save_token(token: str, expires_at: int = 0) -> None:
    with _lock:
        _atomic_json(TOKEN_FILE, {"token": str(token), "expires_at": int(expires_at or 0),
                                  "saved_at": int(time.time()), "base": BASE_URL}, secret=True)
        _cache.clear()


def _clear_token() -> None:
    with _lock:
        try:
            TOKEN_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        _cache.clear()


def logged_in() -> bool:
    return bool(_token())


# --------------------------------------------------------------------------- HTTP

def _url(path: str, query: dict | None = None) -> str:
    url = BASE_URL + ("" if path.startswith("/") else "/") + path
    if query:
        clean = {k: str(v) for k, v in query.items() if v not in (None, "")}
        if clean:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    return url


def _headers(auth: bool, extra: dict | None = None) -> dict:
    head = {"User-Agent": sources.UA, "Accept": "application/json"}
    if auth:
        token = _token()
        if not token:
            raise AuthError("Du bist nicht am Root-Server angemeldet.")
        head["Authorization"] = "Bearer " + token
    head.update(extra or {})
    return head


def _error_text(status: int, raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except (UnicodeDecodeError, ValueError):
        pass
    if status == 404:
        return "Diesen Aufruf kennt der Root-Server nicht (oder der Server ist dort gelöscht)."
    if status == 502 or status == 503:
        return "Der Root-Server ist gerade nicht erreichbar – bitte in einer Minute erneut versuchen."
    return f"Der Root-Server hat mit Fehler {status} geantwortet."


def _curl_call(method: str, url: str, headers: dict, body: bytes | None,
               timeout: int) -> tuple[int, bytes]:
    """Aufruf über Windows-curl. Kopfzeilen (und damit das Token) gehen über eine
    Konfigurationsdatei – niemals über die Kommandozeile, die jeder Prozess lesen kann."""
    curl = sources._windows_curl()                     # noqa: SLF001 – bewusst dieselbe Erkennung
    if not curl:
        raise CloudError(sources.CERT_HINT)
    folder = _tmp_dir()
    stamp = f"{int(time.time() * 1000)}-{os.getpid()}"
    cfg_file = folder / f"req-{stamp}.conf"
    out_file = folder / f"out-{stamp}.bin"
    body_file = folder / f"body-{stamp}.bin"
    lines = [f'url = "{url}"', f'request = "{method}"',
             f'output = "{out_file.as_posix()}"',
             'write-out = "%{http_code}"', "silent", "show-error", "location",
             f"max-time = {int(timeout)}"]
    for key, value in headers.items():
        lines.append(f'header = "{key}: {value}"')
    if body is not None:
        body_file.write_bytes(body)
        _restrict(body_file)
        lines.append(f'data-binary = "@{body_file.as_posix()}"')
    cfg_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _restrict(cfg_file)
    try:
        res = subprocess.run([curl, "-K", str(cfg_file)], capture_output=True,
                             timeout=timeout + 30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if res.returncode != 0 and not out_file.exists():
            raise CloudError("Die Verbindung zum Root-Server ist fehlgeschlagen: "
                             + (res.stderr.decode("utf-8", "replace").strip() or f"curl {res.returncode}"))
        digits = re.findall(rb"\d{3}", res.stdout or b"")
        status = int(digits[-1]) if digits else (200 if res.returncode == 0 else 0)
        data = out_file.read_bytes() if out_file.exists() else b""
        if not status:
            raise CloudError("Die Verbindung zum Root-Server ist fehlgeschlagen: "
                             + (res.stderr.decode("utf-8", "replace").strip() or "unbekannter Fehler"))
        return status, data
    except subprocess.SubprocessError as exc:
        raise CloudError(f"Die Verbindung zum Root-Server ist fehlgeschlagen: {exc}") from exc
    finally:
        for path in (cfg_file, out_file, body_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _raw_call(method: str, url: str, headers: dict, body: bytes | None,
              timeout: int) -> tuple[int, bytes, dict]:
    """Ein Aufruf: (Status, Rumpf, Kopfzeilen). Bei Zertifikatsfehlern über curl."""
    global _ssl_broken
    if _ssl_broken or sources._ssl_broken:             # noqa: SLF001 – einmal kaputt, immer curl
        status, data = _curl_call(method, url, headers, body, timeout)
        return status, data, {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})
    except urllib.error.URLError as exc:
        if sources._is_cert_error(exc) and sources._windows_curl():      # noqa: SLF001
            _ssl_broken = True
            status, data = _curl_call(method, url, headers, body, timeout)
            return status, data, {}
        if sources._is_cert_error(exc):                                  # noqa: SLF001
            raise CloudError(sources.CERT_HINT) from exc
        raise CloudError(f"Der Root-Server ist nicht erreichbar: {getattr(exc, 'reason', exc)}") from exc
    except (OSError, ValueError) as exc:
        raise CloudError(f"Der Root-Server ist nicht erreichbar: {exc}") from exc


def _api(method: str, path: str, *, body=None, raw: bytes | None = None, query: dict | None = None,
         headers: dict | None = None, auth: bool = True, want_bytes: bool = False,
         timeout: int = TIMEOUT):
    """Aufruf der Root-Server-API. Gibt JSON (dict) oder rohe Bytes zurück."""
    extra = dict(headers or {})
    payload: bytes | None = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        extra["Content-Type"] = "application/json; charset=utf-8"
    elif raw is not None:
        payload = bytes(raw)
        extra["Content-Type"] = "application/octet-stream"
    status, data, _head = _raw_call(method, _url(path, query), _headers(auth, extra), payload, timeout)
    if status == 401:
        _clear_token()
        raise AuthError(_error_text(status, data) if data else "")
    if status >= 400:
        raise CloudError(_error_text(status, data), status)
    if want_bytes:
        return data
    if not data:
        return {}
    try:
        out = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CloudError("Der Root-Server hat eine unverständliche Antwort geschickt.") from exc
    return out if isinstance(out, dict) else {"data": out}


# --------------------------------------------------------------------------- Anmeldung

def health() -> dict:
    """Erreichbarkeit prüfen (ohne Anmeldung)."""
    return _api("GET", "/api/health", auth=False, timeout=15)


def login_start(device: str = "") -> dict:
    """Discord-Anmeldung beginnen: liefert die Adresse für den Browser.

    Das Geheimnis zum Abholen des Tokens bleibt im Speicher dieses Prozesses."""
    name = (device or os.environ.get("COMPUTERNAME") or "PC")[:40]
    data = _api("GET", "/api/auth/discord/start", query={"device": name}, auth=False, timeout=25)
    url = str(data.get("url") or "")
    ticket = str(data.get("state") or "")
    if not url.startswith("https://") or not ticket:
        raise CloudError("Der Root-Server hat keine Anmeldeadresse geliefert.")
    with _lock:
        _login.clear()
        _login.update({"state": ticket, "secret": str(data.get("poll_secret") or ""),
                       "started": time.time(), "expires_at": int(data.get("expires_at") or 0)})
    return {"url": url, "pending": True}


def login_poll() -> dict:
    """Nachsehen, ob die Anmeldung im Browser fertig ist.

    Drei mögliche Ausgänge:

    * ``{"pending": True}`` – der Benutzer ist im Browser noch nicht fertig,
    * ``{"needs_invite": True, …}`` – Discord hat geklappt, aber dieses Konto gibt es auf dem
      Root-Server noch nicht. Der Root legt dafür einen **Merkzettel** an; die Oberfläche fragt
      jetzt einmal den Einladungscode ab und schickt ihn mit :func:`login_register`.
    * ``{"logged_in": True}`` – fertig, das Sitzungstoken liegt auf der Platte.
    """
    with _lock:
        ticket = str(_login.get("state") or "")
        secret = str(_login.get("secret") or "")
        started = float(_login.get("started") or 0)
        zettel = str(_login.get("zettel") or "")
    if zettel:
        # Der Merkzettel liegt schon vor – die Oberfläche muss nur noch den Code nachreichen.
        return invite_pending()
    if not ticket:
        return {"pending": False, "logged_in": logged_in()}
    if time.time() - started > 600:
        with _lock:
            _login.clear()
        raise CloudError("Die Anmeldung hat zu lange gedauert – bitte neu beginnen.")
    data = _api("GET", "/api/auth/discord/poll", query={"state": ticket, "secret": secret},
                auth=False, timeout=20)
    if data.get("pending"):
        return {"pending": True}
    zettel = str(data.get("pending_invite") or "")
    if zettel:
        # Der Abholvorgang ist damit verbraucht (der Root gibt ihn genau einmal heraus) – den
        # Zustandswert deshalb wegwerfen und nur noch den Merkzettel merken.
        person = data.get("discord") if isinstance(data.get("discord"), dict) else {}
        gueltig = max(60, int(data.get("gueltig_sekunden") or 900))
        with _lock:
            _login.clear()
            _login.update({"zettel": zettel, "discord": _discord_person(person),
                           "zettel_bis": time.time() + gueltig})
        return invite_pending()
    token = str(data.get("token") or "")
    if not token:
        raise CloudError("Der Root-Server hat kein Sitzungstoken geliefert.")
    _finish_login(token, int(data.get("expires_at") or 0), data.get("user"))
    with _lock:
        _login.clear()
    return {"pending": False, "logged_in": True}


def _discord_person(person: dict) -> dict:
    """Nur die Felder für die Anzeige – und Bilder nur von Discord selbst."""
    bild = str(person.get("avatar_url") or person.get("avatar") or "")
    return {
        "name": str(person.get("anzeigename") or person.get("name")
                    or person.get("username") or "")[:64],
        "avatar_url": bild if bild.startswith("https://cdn.discordapp.com/") else "",
    }


def invite_pending() -> dict:
    """Stand der offenen Erstanmeldung (Merkzettel liegt vor, Code fehlt noch)."""
    with _lock:
        zettel = str(_login.get("zettel") or "")
        bis = float(_login.get("zettel_bis") or 0)
        person = dict(_login.get("discord") or {})
    if not zettel:
        return {"pending": False, "needs_invite": False, "logged_in": logged_in()}
    rest = int(bis - time.time()) if bis else 0
    if bis and rest <= 0:
        with _lock:
            _login.clear()
        raise CloudError("Die Anmeldung ist abgelaufen – bitte noch einmal mit Discord anmelden.")
    return {"pending": False, "needs_invite": True, "logged_in": False,
            "discord": person, "gueltig_sekunden": max(0, rest)}


def format_code(code: str) -> str:
    """Einladungscode zur Anzeige: Großbuchstaben, Vierergruppen mit Trennstrich."""
    roh = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    return "-".join(roh[i:i + 4] for i in range(0, len(roh), 4))


def login_register(code: str, note: str = "") -> dict:
    """Zweiter Schritt der Erstanmeldung: Einladungscode zum vorgemerkten Discord-Konto."""
    with _lock:
        zettel = str(_login.get("zettel") or "")
        bis = float(_login.get("zettel_bis") or 0)
    if not zettel:
        raise CloudError("Für diese Anmeldung liegt nichts mehr vor – bitte noch einmal auf "
                         "„Mit Discord anmelden“ klicken.")
    if bis and time.time() > bis:
        with _lock:
            _login.clear()
        raise CloudError("Die Anmeldung ist abgelaufen – bitte noch einmal mit Discord anmelden.")
    sauber = format_code(code)
    if not sauber:
        raise CloudError("Bitte den Einladungscode eintragen.")
    device = str(note or "").strip() or (os.environ.get("COMPUTERNAME") or "PC")[:40]
    data = _api("POST", "/api/auth/discord/register",
                body={"zettel": zettel, "code": sauber, "note": device}, auth=False, timeout=30)
    token = str(data.get("token") or "")
    if not token:
        raise CloudError("Der Root-Server hat kein Sitzungstoken geliefert.")
    _finish_login(token, int(data.get("expires_at") or 0), data.get("user"))
    with _lock:
        _login.clear()
    return {"logged_in": True}


def login_abort() -> dict:
    """Angefangene Anmeldung verwerfen (Knopf „Abbrechen“ im Dialog)."""
    with _lock:
        _login.clear()
    return {"pending": False, "needs_invite": False, "logged_in": logged_in()}


def login_invite(code: str, name: str) -> dict:
    """Ersatzweg, solange Discord auf dem Root-Server noch nicht eingerichtet ist."""
    code = str(code or "").strip()
    name = str(name or "").strip()
    if not code:
        raise CloudError("Bitte den Einladungscode eintragen.")
    if not name:
        raise CloudError("Bitte einen Namen für das Konto eintragen.")
    device = (os.environ.get("COMPUTERNAME") or "PC")[:40]
    data = _api("POST", "/api/auth/invite", body={"code": code, "name": name, "note": device},
                auth=False, timeout=30)
    token = str(data.get("token") or "")
    if not token:
        raise CloudError("Der Root-Server hat kein Sitzungstoken geliefert.")
    _finish_login(token, int(data.get("expires_at") or 0), data.get("user"))
    return {"logged_in": True}


def _finish_login(token: str, expires_at: int, user) -> None:
    _save_token(token, expires_at)
    info = user if isinstance(user, dict) else {}
    _update_state(lambda d: d.update({"user": {"id": str(info.get("id") or ""),
                                               "name": str(info.get("name") or ""),
                                               "role": str(info.get("role") or "user")},
                                      "expires_at": int(expires_at or 0),
                                      "logged_in_at": int(time.time())}))
    log.info("Am Root-Server angemeldet (%s).", BASE_URL)      # ohne Token – der steht nie im Protokoll


def logout(*, remote: bool = True) -> dict:
    """Abmelden: Sitzung auf dem Root beenden, Token lokal löschen."""
    if remote and logged_in():
        try:
            _api("POST", "/api/auth/logout", body={}, timeout=15)
        except CloudError:
            pass                                       # lokal wird trotzdem abgemeldet
    _clear_token()
    _update_state(lambda d: (d.pop("user", None), d.pop("expires_at", None)))
    with _lock:
        _login.clear()
    return {"logged_in": False}


# --------------------------------------------------------------------------- Konto-Einstellungen
#
# Drei der vier Wege gibt es auf dem Root-Server vielleicht noch nicht. Statt in die Oberfläche
# zu krachen, liefern sie dann ``{"supported": False, "hint": "<deutscher Satz>"}`` – die
# Oberfläche zeigt den Satz an und lässt den Rest des Dialogs unberührt. Offene Punkte dazu
# stehen in `spec-cloud-client-offen.md`.

#: Antworten, die „diesen Weg gibt es hier (noch) nicht“ bedeuten.
_NICHT_DA = (404, 405, 501)


def _nicht_da(hint: str) -> dict:
    return {"supported": False, "hint": hint}


def sessions() -> dict:
    """Aktive Sitzungen des Kontos (`GET /api/sessions`)."""
    try:
        data = _api("GET", "/api/sessions", timeout=20)
    except CloudError as exc:
        if exc.status in _NICHT_DA:
            return _nicht_da("Dieser Root-Server führt noch keine Liste der Sitzungen.")
        raise
    roh = data.get("sessions")
    liste = [s for s in roh if isinstance(s, dict)] if isinstance(roh, list) else []
    liste.sort(key=lambda s: int(s.get("last_seen") or s.get("created_at") or 0), reverse=True)
    state = _read_state()
    return {"supported": True, "sessions": liste,
            "current_expires_at": int(state.get("expires_at") or 0),
            "logged_in_at": int(state.get("logged_in_at") or 0)}


def set_name(name: str) -> dict:
    """Anzeigenamen des Kontos ändern (`POST /api/me`, falls der Root das kann)."""
    name = str(name or "").strip()
    if len(name) < 2 or len(name) > 32:
        raise CloudError("Bitte einen Namen mit 2 bis 32 Zeichen eintragen.")
    try:
        data = _api("POST", "/api/me", body={"name": name}, timeout=20)
    except CloudError as exc:
        if exc.status in _NICHT_DA:
            return _nicht_da("Dieser Root-Server kann den Anzeigenamen noch nicht ändern. "
                             "Bitte den Betreiber darum bitten.")
        raise
    user = data.get("user") if isinstance(data.get("user"), dict) else {"name": name}
    neu = str(user.get("name") or name)
    _update_state(lambda d: d.__setitem__("user", {**(d.get("user") or {}), "name": neu}))
    _drop_cache()
    return {"supported": True, "user": user, "name": neu}


def revoke_session(created_at: int, note: str = "") -> dict:
    """Eine einzelne Sitzung beenden (`POST /api/sessions/revoke`, falls vorhanden)."""
    try:
        created_at = int(created_at)
    except (TypeError, ValueError) as exc:
        raise CloudError("Diese Sitzung lässt sich nicht zuordnen.") from exc
    if created_at <= 0:
        raise CloudError("Diese Sitzung lässt sich nicht zuordnen.")
    try:
        _api("POST", "/api/sessions/revoke",
             body={"created_at": created_at, "note": str(note or "")}, timeout=20)
    except CloudError as exc:
        if exc.status in _NICHT_DA:
            return _nicht_da("Dieser Root-Server kann einzelne Sitzungen noch nicht beenden. "
                             "„Abmelden“ beendet die Sitzung dieses PCs.")
        raise
    return {"supported": True, "ok": True}


def discord_link_start(ziel: str = "") -> dict:
    """Discord nachträglich mit einem bestehenden Konto verknüpfen.

    Der Root-Server schickt den Browser nach dem Bestätigen auf ``ziel#verknuepft=1`` zurück –
    dieses Programm kann die Rückleitung nicht auffangen und bittet deshalb darum, danach auf
    „Aktualisieren“ zu klicken."""
    query = {"link": "1"}
    if str(ziel or "").strip():
        query["ziel"] = str(ziel).strip()
    try:
        data = _api("GET", "/api/auth/discord/start", query=query, timeout=25)
    except CloudError as exc:
        if exc.status in _NICHT_DA:
            return _nicht_da("Dieser Root-Server kann Discord noch nicht nachträglich verknüpfen.")
        raise
    url = str(data.get("url") or "")
    if not url.startswith("https://"):
        return _nicht_da("Der Root-Server hat keine Adresse zum Verknüpfen geliefert.")
    return {"supported": True, "url": url}


# --------------------------------------------------------------------------- Konto, Pässe, Server

def _cached(key: str, fetch, force: bool = False):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and not force and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = fetch()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def account(force: bool = False) -> dict:
    """GET /api/me – Konto, Pässe, Grenzen, laufende Server, Hinweise."""
    return _cached("me", lambda: _api("GET", "/api/me"), force)


def remote_servers(force: bool = False) -> list[dict]:
    data = _cached("servers", lambda: _api("GET", "/api/servers"), force)
    servers = data.get("servers")
    return [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []


def _drop_cache() -> None:
    with _lock:
        _cache.clear()


def address_of(remote: dict) -> str:
    """Adresse zum Eintippen: was der Root-Server meldet, sonst <name>.arcardia-nexus.de."""
    for key in ("address", "host", "hostname"):
        value = str(remote.get(key) or "").strip().lower()
        if value:
            return value
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(remote.get("name") or "").lower())).strip("-")
    return f"{slug}.{HOST_DOMAIN}" if slug else ROOT_IPV4


def _port_of(remote: dict) -> int:
    live = remote.get("ports_live") or {}
    ports = live if isinstance(live, dict) and live else (remote.get("ports") or {})
    if not isinstance(ports, dict):
        return 0
    key = "bedrock" if str(remote.get("type")) == "bedrock" else "java"
    try:
        return int(ports.get(key) or ports.get("java") or ports.get("bedrock") or 0)
    except (TypeError, ValueError):
        return 0


def remote_view(remote: dict) -> dict:
    """Entfernte Instanz für die Oberfläche (mit Adresse und lokaler Zuordnung)."""
    local = _local_for(str(remote.get("id") or ""))
    state = str(remote.get("state") or "")
    out = dict(remote)
    out["address"] = address_of(remote)
    out["port"] = _port_of(remote)
    out["state_hint"] = STATE_TEXTS.get(state, state)
    out["local_id"] = local["id"] if local else ""
    out["local_name"] = local["name"] if local else ""
    out["can_pull"] = state in ("hosted", "awaiting_pull", "downloading") and not remote.get("running")
    out["needs_pull"] = state in ("awaiting_pull", "downloading")
    return out


# --------------------------------------------------------------------------- Verknüpfung lokal ↔ Root

def link_of(cfg: dict) -> dict:
    link = cfg.get("cloud")
    return dict(link) if isinstance(link, dict) else {}


def locked(cfg: dict) -> bool:
    """Liegt der Server gerade auf dem Root-Server? Dann darf die lokale Kopie nicht starten."""
    return str(link_of(cfg).get("state") or "") in LOCKED_STATES


def lock_hint(cfg: dict) -> str:
    link = link_of(cfg)
    state = str(link.get("state") or "")
    if state in ("awaiting_pull", "downloading"):
        return (f"„{cfg['name']}“ liegt noch auf dem Root-Server und wartet auf die Rückholung. "
                f"Hole den Server zuerst auf diesen PC – erst danach ist die lokale Kopie wieder "
                f"spielbar.")
    if state == "uploading":
        return f"„{cfg['name']}“ wird gerade auf den Root-Server übertragen – bitte warten."
    return (f"„{cfg['name']}“ läuft gerade auf dem Root-Server. Ein Server ist immer nur an einer "
            f"Stelle spielbar – hole ihn zurück auf diesen PC oder starte ihn im Bereich „Cloud“.")


def _set_link(cfg: dict, **changes) -> dict:
    """Verknüpfung am lokalen Server fortschreiben (Zustand, Instanz, Sitzung)."""
    with _lock:
        fresh = store.get(cfg["id"]) or cfg
        link = link_of(fresh)
        link.update({k: v for k, v in changes.items() if v is not None})
        for key, value in changes.items():
            if value is None:
                link.pop(key, None)
        link["updated"] = int(time.time())
        fresh = dict(fresh)
        fresh["cloud"] = link
        store.save(fresh)
        return fresh


def _local_for(remote_id: str) -> dict | None:
    if not remote_id:
        return None
    for cfg in store.all_servers():
        if str(link_of(cfg).get("instance") or "") == remote_id:
            return cfg
    return None


def sync_links(servers: list[dict] | None = None) -> None:
    """Zustände der Instanzen in die lokalen Server übernehmen (Sperre, Anzeige)."""
    try:
        remotes = servers if servers is not None else remote_servers()
    except CloudError:
        return
    known = {str(r.get("id") or ""): r for r in remotes}
    for cfg in store.all_servers():
        link = link_of(cfg)
        rid = str(link.get("instance") or "")
        if not rid:
            continue
        remote = known.get(rid)
        if remote is None:
            if link.get("state") and link.get("state") != "local_only":
                _set_link(cfg, state="local_only", running=False)     # auf dem Root gelöscht
            continue
        want = str(remote.get("state") or "")
        if link.get("state") != want or bool(link.get("running")) != bool(remote.get("running")):
            _set_link(cfg, state=want, running=bool(remote.get("running")),
                      name=str(remote.get("name") or ""), address=address_of(remote))


# --------------------------------------------------------------------------- Gesamtbild für die Oberfläche

def _anzeige_user(user) -> dict:
    """Konto für die Anzeige. Das Bild darf **nur** von Discord kommen.

    Der Root-Server prüft das schon; hier wird es ein zweites Mal geprüft, damit die Oberfläche
    unter keinen Umständen ein Bild von einer fremden Adresse nachlädt."""
    info = dict(user) if isinstance(user, dict) else {}
    bild = str(info.get("avatar_url") or "")
    info["avatar_url"] = bild if bild.startswith("https://cdn.discordapp.com/") else ""
    info["name"] = str(info.get("name") or "")
    info["role"] = str(info.get("role") or "user")
    info["role_text"] = ROLE_TEXTS.get(info["role"], info["role"])
    info["discord_name"] = str(info.get("discord_name") or "")
    info["discord_linked"] = bool(info.get("discord_linked") or info.get("discord_name"))
    return info


def status(force: bool = False) -> dict:
    """Alles, was der Bereich „Cloud“ braucht. Enthält nie das Token."""
    state = _read_state()
    with _lock:
        pending = bool(_login.get("state"))
        offen = bool(_login.get("zettel"))
        person = dict(_login.get("discord") or {})
        zettel_bis = float(_login.get("zettel_bis") or 0)
    out = {
        "base": BASE_URL, "domain": HOST_DOMAIN, "logged_in": logged_in(),
        "login_pending": pending,
        "needs_invite": offen,
        "invite_discord": person,
        "invite_seconds": max(0, int(zettel_bis - time.time())) if zettel_bis else 0,
        "user": _anzeige_user(state.get("user") or {}),
        "passes": [], "limits": {}, "limits_text": "",
        "servers": [], "notices": [], "machine": {}, "error": "", "auth_error": "",
        "transfer": state.get("transfer") or {}, "pull": [],
        "session_expires_at": int(state.get("expires_at") or 0),
        "logged_in_at": int(state.get("logged_in_at") or 0),
    }
    if not out["logged_in"]:
        return out
    try:
        me = account(force)
        remotes = remote_servers(force)
    except AuthError as exc:
        out["logged_in"] = False
        out["auth_error"] = str(exc)
        return out
    except CloudError as exc:
        out["error"] = str(exc)
        return out
    sync_links(remotes)
    out["user"] = _anzeige_user(me.get("user") or out["user"])
    out["passes"] = [p for p in (me.get("passes") or []) if isinstance(p, dict)]
    out["limits"] = me.get("limits") or {}
    out["limits_text"] = str(me.get("limits_text") or "")
    out["notices"] = [n for n in (me.get("notices") or []) if isinstance(n, dict)]
    out["machine"] = me.get("machine") or {}
    out["slots_used"] = me.get("slots_used")
    out["slots_free"] = me.get("slots_free")
    out["ram_used_mb"] = me.get("ram_used_mb")
    out["ram_total_mb"] = me.get("ram_total_mb")
    out["servers"] = [remote_view(r) for r in remotes]
    out["pull"] = [s for s in out["servers"] if s.get("needs_pull")]
    return out


def pending_pull() -> list[dict]:
    """Server, die auf dem Root liegen und zurückgeholt werden müssen (Hinweis beim Programmstart)."""
    if not logged_in():
        return []
    try:
        return [s for s in (remote_view(r) for r in remote_servers()) if s.get("needs_pull")]
    except CloudError:
        return []


# --------------------------------------------------------------------------- Steuern entfernter Server

def remote_detail(remote_id: str) -> dict:
    data = _api("GET", f"/api/servers/{urllib.parse.quote(remote_id)}")
    server = data.get("server") or {}
    return remote_view(server) if server else {}


def remote_start(remote_id: str) -> dict:
    data = _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/start", body={})
    _drop_cache()
    return {"ok": True, "ports": data.get("ports") or {},
            "server": remote_view(data.get("server") or {}) if data.get("server") else {}}


def remote_stop(remote_id: str, announce_seconds: int = 0) -> dict:
    data = _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/stop",
                body={"announce_seconds": int(announce_seconds or 0)})
    _drop_cache()
    return {"ok": True, "message": str(data.get("message") or "Der Server wird gestoppt.")}


def remote_command(remote_id: str, command: str) -> dict:
    text = str(command or "").strip()
    if not text:
        raise CloudError("Kein Befehl angegeben.")
    _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/command", body={"command": text})
    return {"ok": True}


def remote_console(remote_id: str, since: int = 0, tail: int = 0) -> dict:
    query = {"since": int(since or 0)}
    if tail:
        query["tail"] = int(tail)
    data = _api("GET", f"/api/servers/{urllib.parse.quote(remote_id)}/console", query=query)
    live = data.get("live") or {}
    return {"next": int(data.get("next") or 0), "lines": data.get("lines") or [],
            "running": bool(live.get("running")) if isinstance(live, dict) else False,
            "uptime": int((live or {}).get("uptime") or 0) if isinstance(live, dict) else 0}


def remote_files(remote_id: str, path: str = "") -> dict:
    return _api("GET", f"/api/servers/{urllib.parse.quote(remote_id)}/files",
                query={"path": path, "action": "list"})


def remote_file_read(remote_id: str, path: str) -> dict:
    return _api("GET", f"/api/servers/{urllib.parse.quote(remote_id)}/files",
                query={"path": path, "action": "read"})


def remote_file_write(remote_id: str, path: str, text: str) -> dict:
    return _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/files",
                body={"action": "write", "path": path, "text": str(text)})


def remote_file_delete(remote_id: str, path: str) -> dict:
    """Eine Datei oder einen Ordner auf dem Root-Server löschen (z. B. ein Plugin)."""
    return _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/files",
                body={"action": "delete", "path": str(path)})


def remote_file_mkdir(remote_id: str, path: str) -> dict:
    """Einen Ordner auf dem Root-Server anlegen."""
    return _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/files",
                body={"action": "mkdir", "path": str(path)})


def remote_file_rename(remote_id: str, path: str, new_path: str) -> dict:
    """Eine Datei oder einen Ordner auf dem Root-Server umbenennen."""
    return _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/files",
                body={"action": "rename", "path": str(path), "new_path": str(new_path)})


def remote_file_info(remote_id: str, path: str, *, mit_hash: bool = False) -> dict:
    """Angaben zu einem einzelnen Eintrag (Größe, Art) – ohne die ganze Liste.

    ``mit_hash`` holt zusätzlich die Prüfsumme. Die kostet auf dem Root-Server einen
    Durchlauf über die Datei, deshalb nicht von selbst.
    """
    query = {"path": path, "action": "info"}
    if mit_hash:
        query["hash"] = "1"
    return _api("GET", f"/api/servers/{urllib.parse.quote(remote_id)}/files", query=query)


# ------------------------------------------------- Einzelne Dateien hoch und herunter

def remote_file_upload(remote_id: str, quelle, ziel: str = "", *, progress=None) -> dict:
    """Eine **einzelne** Datei auf den Root-Server legen – z. B. ein Plugin.

    Bisher konnte das Programm nur den ganzen Serverordner hochladen; ein Plugin nachzulegen
    ging nur direkt gegen die Daemon-API. Benutzt wird derselbe Weg wie beim großen Upload
    (Manifest → Stücke → Prüfsumme), nur mit ``purpose="files"``: der Server muss dafür schon
    auf dem Root liegen und darf nicht laufen.
    """
    if not logged_in():
        raise AuthError("Bitte zuerst am Root-Server anmelden.")
    pfad = pathlib.Path(quelle)
    if not pfad.is_file():
        raise CloudError(f"Die Datei „{pfad}“ gibt es auf diesem PC nicht.")
    rel = str(ziel or pfad.name).replace("\\", "/").strip("/")
    if not _usable_rel(rel):
        raise CloudError(f"„{rel}“ ist als Name auf dem Root-Server nicht erlaubt.")
    size = pfad.stat().st_size
    if size > MAX_FILE_BYTES:
        raise CloudError(f"„{rel}“ ist mit {human(size)} zu groß für eine einzelne Übertragung.")
    manifest = {"version": MANIFEST_VERSION, "file_count": 1, "total_bytes": size,
                "files": [{"path": rel, "size": size, "sha256": sha256_file(pfad),
                           "mtime": int(pfad.stat().st_mtime)}]}
    data = _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/upload/begin",
                body={"manifest": manifest, "purpose": "files"}, timeout=120)
    session = data.get("session") or {}
    sid = str(session.get("id") or "")
    if not sid:
        raise CloudError("Der Root-Server hat keine Übertragung eröffnet.")
    try:
        offen = session.get("missing") or [{"path": rel, "offset": 0}]
        versatz = int((offen[0] or {}).get("offset") or 0) if offen else 0
        counter = {"sent": versatz}
        _upload_file(remote_id, sid, pfad, rel, versatz, size, counter, size, progress)
        fertig = _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/upload/finish",
                      body={"session": sid, "hashes": True}, timeout=300)
    except BaseException:
        try:
            _api("POST", f"/api/servers/{urllib.parse.quote(remote_id)}/upload/abort",
                 body={"session": sid})
        except CloudError:
            pass
        raise
    _drop_cache()
    log.info("Datei „%s“ auf den Root-Server gelegt (%s).", rel, human(size))
    return {"ok": True, "path": rel, "size": size, "report": fertig.get("report") or {}}


def remote_file_download(remote_id: str, rel: str, ziel=None, *, progress=None) -> dict:
    """Eine **einzelne** Datei vom Root-Server holen – z. B. ``world/level.dat``.

    Geladen wird in Stücken; am Ende wird die Prüfsumme gegen die Angabe des Root-Servers
    gehalten, genau wie bei der großen Rückholung. Ohne ``ziel`` landet die Datei im
    Download-Ordner des Programms.
    """
    if not logged_in():
        raise AuthError("Bitte zuerst am Root-Server anmelden.")
    name = str(rel or "").replace("\\", "/").strip("/")
    if not _usable_rel(name):
        raise CloudError(f"„{rel}“ ist kein Pfad, den beide Seiten annehmen.")
    info = remote_file_info(remote_id, name, mit_hash=True)
    if str(info.get("type") or "") == "dir" or info.get("is_dir"):
        raise CloudError(f"„{name}“ ist ein Ordner – bitte eine einzelne Datei angeben.")
    size = int(info.get("size") or 0)
    erwartet = str(info.get("sha256") or "").lower()
    if ziel is None:
        ordner = DOWNLOAD_DIR
        ordner.mkdir(parents=True, exist_ok=True)
        target = ordner / name.split("/")[-1]
    else:
        target = pathlib.Path(ziel)
        if target.is_dir():
            target = target / name.split("/")[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + PART_SUFFIX)
    part.unlink(missing_ok=True)
    offset = 0
    tries = 0
    while offset < size:
        try:
            block = _api("GET", f"/api/servers/{urllib.parse.quote(remote_id)}/download",
                         query={"path": name, "offset": offset,
                                "length": min(CHUNK_SIZE, size - offset)},
                         want_bytes=True, timeout=TRANSFER_TIMEOUT)
        except CloudError as exc:
            if exc.status in (401, 403, 404):
                raise
            tries += 1
            if tries >= TRANSFER_TRIES:
                part.unlink(missing_ok=True)
                raise CloudError(f"„{name}“ lässt sich nicht herunterladen: {exc}") from exc
            time.sleep(2 * tries)
            continue
        if not block:
            part.unlink(missing_ok=True)
            raise CloudError(f"Der Root-Server liefert für „{name}“ keine Daten mehr.")
        with part.open("ab") as fh:
            fh.write(block)
        offset += len(block)
        tries = 0
        if progress:
            progress(offset, size)
    if size == 0:
        part.write_bytes(b"")
    digest = sha256_file(part)
    if erwartet and digest != erwartet:
        part.unlink(missing_ok=True)
        raise CloudError(f"Die Prüfsumme von „{name}“ stimmt nicht – bitte noch einmal "
                         f"herunterladen.")
    os.replace(str(part), str(target))
    log.info("Datei „%s“ vom Root-Server geholt (%s).", name, human(size))
    return {"ok": True, "path": name, "size": size, "sha256": digest, "local": str(target)}


def _wait_stopped(remote_id: str, job: dict | None = None) -> None:
    """Auf das Ende eines laufenden Servers warten (vor einer Rückholung)."""
    deadline = time.time() + STOP_WAIT
    while time.time() < deadline:
        detail = remote_detail(remote_id)
        if not detail.get("running"):
            return
        if job is not None:
            job["detail"] = "Der Server wird auf dem Root-Server gestoppt und die Welt gespeichert …"
        time.sleep(3)
    raise CloudError("Der Server auf dem Root-Server hört nicht auf zu laufen – bitte später "
                     "erneut versuchen.")


# --------------------------------------------------------------------------- Manifest und Prüfsummen

def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def _usable_rel(rel: str) -> bool:
    """Pfad, den beide Seiten annehmen (hosted/core/paths.py)."""
    if not rel or len(rel) > MAX_REL_LEN:
        return False
    parts = rel.split("/")
    if len(parts) > MAX_DEPTH:
        return False
    if parts[0] in RESERVED_NAMES or parts[0].lower() in SKIP_TOP_DIRS:
        return False
    for name in parts:
        if not name or name in (".", "..") or name in RESERVED_NAMES:
            return False
        if name.endswith((".", " ")) or name.startswith(" "):
            return False
        if name.lower().endswith(PART_SUFFIX):
            return False
        if name.split(".")[0].lower() in _DEVICE_NAMES:
            return False
        if len(name.encode("utf-8")) > MAX_NAME_LEN:
            return False
        if any(ch in _BAD_CHARS or ord(ch) < 32 for ch in name):
            return False
    return True


def build_manifest(folder: pathlib.Path, *, progress=None) -> dict:
    """Manifest eines lokalen Serverordners – Format wie hosted/core/transfer.py."""
    folder = pathlib.Path(folder)
    files: list[dict] = []
    skipped: list[str] = []
    total = 0
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            rel = entry.relative_to(folder).as_posix()
            if entry.is_symlink():
                skipped.append(rel)
                continue
            if entry.is_dir():
                if _usable_rel(rel + "/x"):
                    stack.append(entry)
                elif rel.split("/")[0].lower() not in SKIP_TOP_DIRS:
                    skipped.append(rel)
                continue
            if not entry.is_file():
                continue
            if not _usable_rel(rel):
                if rel.split("/")[0].lower() not in SKIP_TOP_DIRS and not rel.endswith(PART_SUFFIX):
                    skipped.append(rel)
                continue
            try:
                size = entry.stat().st_size
                mtime = int(entry.stat().st_mtime)
                digest = sha256_file(entry)
            except OSError:
                skipped.append(rel)
                continue
            if size > MAX_FILE_BYTES:
                raise CloudError(f"Die Datei „{rel}“ ist mit {human(size)} zu groß für eine "
                                 f"Übertragung.")
            files.append({"path": rel, "size": size, "sha256": digest, "mtime": mtime})
            total += size
            if len(files) > MAX_FILES:
                raise CloudError("Der Serverordner enthält zu viele Dateien für eine Übertragung.")
            if total > MAX_TOTAL_BYTES:
                raise CloudError(f"Der Serverordner ist mit über {human(MAX_TOTAL_BYTES)} zu groß "
                                 f"für eine Übertragung.")
            if progress:
                progress(total, 0)
    files.sort(key=lambda e: e["path"])
    return {"version": MANIFEST_VERSION, "created_at": int(time.time()), "file_count": len(files),
            "total_bytes": total, "files": files, "skipped": skipped[:50]}


def verify_folder(folder: pathlib.Path, manifest: dict, *, progress=None) -> dict:
    """Prüft einen lokalen Ordner gegen ein Manifest (Größe **und** SHA256 je Datei)."""
    folder = pathlib.Path(folder)
    missing: list[str] = []
    wrong: list[str] = []
    done = 0
    for entry in manifest.get("files") or []:
        target = folder / entry["path"]
        try:
            if not target.is_file():
                missing.append(entry["path"])
                continue
            if target.stat().st_size != int(entry["size"]):
                wrong.append(entry["path"])
                continue
            if sha256_file(target) != str(entry.get("sha256") or "").lower():
                wrong.append(entry["path"])
                continue
        except OSError:
            wrong.append(entry["path"])
            continue
        done += int(entry["size"])
        if progress:
            progress(done, int(manifest.get("total_bytes") or 0))
    return {"ok": not missing and not wrong, "missing": missing[:20], "missing_count": len(missing),
            "wrong": wrong[:20], "wrong_count": len(wrong), "bytes": done}


def human(size) -> str:
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            text = f"{size:.1f}".rstrip("0").rstrip(".") if unit != "B" else f"{int(size)}"
            return f"{text} {unit}".replace(".", ",")
        size /= 1024
    return f"{size:.1f} TB"


# --------------------------------------------------------------------------- Hochladen (PC → Root)

def upload_async(cfg: dict) -> dict:
    """Ganzen Serverordner auf den Root-Server verschieben. Läuft als Job (manager.new_job)."""
    if not logged_in():
        raise AuthError("Bitte zuerst am Root-Server anmelden.")
    if manager.is_running(cfg["id"]):
        raise CloudError(f"„{cfg['name']}“ läuft gerade – bitte zuerst stoppen.")
    if not cfg.get("installed"):
        raise CloudError("Der Server ist noch nicht fertig eingerichtet.")
    link = link_of(cfg)
    if str(link.get("state") or "") in ("hosted", "suspended"):
        raise CloudError(f"„{cfg['name']}“ liegt schon auf dem Root-Server.")
    if str(link.get("state") or "") in ("awaiting_pull", "downloading"):
        raise CloudError(f"„{cfg['name']}“ wartet auf die Rückholung – bitte erst zurückholen.")
    job = manager.new_job(cfg["id"], ["Dateien erfassen", "Server auf dem Root anmelden",
                                      "Übertragung beginnen", "Dateien übertragen", "Abschließen"])
    return manager._run_job(job, lambda: _upload_work(job, cfg))       # noqa: SLF001


def _ensure_remote_instance(cfg: dict) -> str:
    """Instanz auf dem Root anlegen oder die verknüpfte wiederverwenden."""
    link = link_of(cfg)
    rid = str(link.get("instance") or "")
    if rid:
        try:
            if remote_detail(rid):
                return rid
        except CloudError as exc:
            if exc.status != 404:
                raise
    data = _api("POST", "/api/servers", body={
        "name": cfg["name"], "type": cfg["type"], "flavor": cfg.get("flavor") or "paper",
        "version": cfg.get("version") or "", "ram_mb": int(cfg.get("ram_mb") or 4096),
        "origin": "local", "geyser": bool(cfg.get("geyser")),
    })
    remote = data.get("server") or {}
    rid = str(remote.get("id") or "")
    if not rid:
        raise CloudError("Der Root-Server hat keine Serverkennung geliefert.")
    _set_link(cfg, instance=rid, state=str(remote.get("state") or "local_only"),
              name=str(remote.get("name") or cfg["name"]), address=address_of(remote))
    return rid


def _begin_upload(cfg: dict, rid: str, manifest: dict) -> dict:
    """Vorhandene Übertragung fortsetzen oder eine neue anmelden."""
    old = str(link_of(cfg).get("session") or "")
    if old:
        try:
            data = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/upload/status",
                        query={"session": old})
            session = data.get("session") or {}
            if session and str(session.get("state")) == "open" \
                    and int(session.get("file_count") or 0) == int(manifest["file_count"]):
                return session
        except CloudError:
            pass                                       # abgelaufen oder weg: neu anmelden
    data = _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/upload/begin",
                body={"manifest": {k: v for k, v in manifest.items() if k != "skipped"},
                      "purpose": "instance"}, timeout=120)
    session = data.get("session") or {}
    if not session.get("id"):
        raise CloudError("Der Root-Server hat keine Übertragung eröffnet.")
    return session


def _upload_work(job: dict, cfg: dict) -> None:
    folder = store.server_dir(cfg["id"])
    if not folder.is_dir():
        raise CloudError("Den Ordner dieses Servers gibt es auf dem PC nicht.")

    manager._step(job, 0, "Dateien werden gelesen und geprüft …")       # noqa: SLF001
    manifest = build_manifest(folder, progress=manager._progress_cb(job, "Gelesen"))  # noqa: SLF001
    if not manifest["files"]:
        raise CloudError("In diesem Serverordner gibt es keine Dateien zum Übertragen.")

    manager._step(job, 1, "Server wird auf dem Root-Server angemeldet …")            # noqa: SLF001
    rid = _ensure_remote_instance(cfg)

    manager._step(job, 2, f"{manifest['file_count']} Dateien, {human(manifest['total_bytes'])}")
    session = _begin_upload(cfg, rid, manifest)
    sid = str(session["id"])
    _set_link(cfg, instance=rid, session=sid, state="uploading")
    _update_state(lambda d: d.__setitem__("transfer", {
        "kind": "upload", "local": cfg["id"], "instance": rid, "session": sid,
        "files": manifest["file_count"], "bytes": manifest["total_bytes"],
        "started": int(time.time())}))

    manager._step(job, 3, "Übertragung läuft …")                                     # noqa: SLF001
    total = int(manifest["total_bytes"])
    progress = manager._progress_cb(job, "Hochladen")                               # noqa: SLF001
    index = {e["path"]: e for e in manifest["files"]}
    counter = {"sent": 0}
    tries = 0
    while True:
        data = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/upload/status",
                    query={"session": sid})
        session = data.get("session") or {}
        if session.get("complete"):
            break
        if str(session.get("state")) != "open":
            raise CloudError("Die Übertragung wurde auf dem Root-Server beendet – bitte erneut "
                             "beginnen.")
        open_files = session.get("missing") or ([session["next"]] if session.get("next") else [])
        if not open_files:
            raise CloudError("Der Root-Server nennt keine offenen Dateien, meldet die Übertragung "
                             "aber auch nicht als fertig.")
        before = int(session.get("received_bytes") or 0)
        counter["sent"] = before                       # der Root kennt den Stand – auch nach Abbruch
        try:
            for item in open_files:
                rel = str(item.get("path") or "")
                entry = index.get(rel)
                if entry is None:
                    raise CloudError(f"Der Root-Server verlangt die unbekannte Datei „{rel}“.")
                _upload_file(rid, sid, folder / rel, rel, int(item.get("offset") or 0),
                             int(entry["size"]), counter, total, progress)
        except CloudError as exc:
            if exc.status in (401, 403, 404, 409):
                raise
            tries += 1
            if tries >= TRANSFER_TRIES:
                raise CloudError(f"Die Übertragung bricht immer wieder ab: {exc} Die bereits "
                                 f"übertragenen Dateien bleiben erhalten – du kannst es später "
                                 f"erneut versuchen.") from exc
            job["detail"] = f"Verbindung unterbrochen – {tries + 1}. Versuch …"
            time.sleep(2 * tries)
            continue
        data = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/upload/status",
                    query={"session": sid})
        if int((data.get("session") or {}).get("received_bytes") or 0) <= before \
                and not (data.get("session") or {}).get("complete"):
            tries += 1
            if tries >= TRANSFER_TRIES:
                raise CloudError("Die Übertragung kommt nicht voran – bitte später erneut "
                                 "versuchen.")
        else:
            tries = 0

    manager._step(job, 4, "Übertragung wird abgeschlossen …")                        # noqa: SLF001
    data = _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/upload/finish",
                body={"session": sid, "hashes": True}, timeout=600)
    remote = data.get("server") or {}
    _set_link(cfg, instance=rid, session=None, state=str(remote.get("state") or "hosted"),
              name=str(remote.get("name") or cfg["name"]), address=address_of(remote),
              running=False)
    _update_state(lambda d: d.pop("transfer", None))
    _drop_cache()
    report = data.get("report") or {}
    job["detail"] = (f"{report.get('files', manifest['file_count'])} Dateien "
                     f"({human(report.get('bytes', manifest['total_bytes']))}) liegen auf dem "
                     f"Root-Server.")
    log.info("Server „%s“ auf den Root-Server verschoben (%s Dateien).",
                     cfg["name"], report.get("files", manifest["file_count"]))


def _upload_file(rid: str, sid: str, path: pathlib.Path, rel: str, offset: int, size: int,
                 counter: dict, total: int, progress) -> None:
    """Eine Datei ab ``offset`` in Stücken hochladen; ``counter["sent"]`` zählt für den Fortschritt."""
    if not path.is_file():
        raise CloudError(f"Die Datei „{rel}“ gibt es auf dem PC nicht mehr – bitte die Übertragung "
                         f"neu beginnen.")
    with path.open("rb") as fh:
        fh.seek(offset)
        while offset < size:
            block = fh.read(min(CHUNK_SIZE, size - offset))
            if not block:
                raise CloudError(f"Die Datei „{rel}“ ist kürzer geworden – bitte die Übertragung "
                                 f"neu beginnen.")
            # Der Pfad geht doppelt mit: prozentkodiert im Kopf X-MCSM-Path (Kopfzeilen können kein
            # UTF-8) und zusätzlich als Abfrageparameter, den der Daemon ohnehin schon entschlüsselt
            # (Welten heißen z. B. „Bedrock level“ – mit Leerzeichen).
            result = _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/upload/chunk",
                          raw=block, timeout=TRANSFER_TIMEOUT,
                          query={"session": sid, "path": rel, "offset": offset},
                          headers={"X-MCSM-Session": sid,
                                   "X-MCSM-Path": urllib.parse.quote(rel),
                                   "X-MCSM-Offset": str(offset)})
            reached = int(result.get("offset") or (offset + len(block)))
            counter["sent"] += max(0, reached - offset)
            offset = reached
            if progress:
                progress(min(counter["sent"], total), total)
            if result.get("done"):
                return


# --------------------------------------------------------------------------- Zurückholen (Root → PC)

def download_async(remote_id: str) -> dict:
    """Instanz vom Root-Server auf den PC holen und den Ordner dort freigeben."""
    if not logged_in():
        raise AuthError("Bitte zuerst am Root-Server anmelden.")
    remote = remote_detail(remote_id)
    if not remote:
        raise CloudError("Diesen Server gibt es auf dem Root-Server nicht (mehr).")
    cfg = _local_for(remote_id) or _create_local(remote)
    if manager.is_running(cfg["id"]):
        raise CloudError(f"Auf diesem PC läuft „{cfg['name']}“ noch – bitte zuerst stoppen.")
    if manager.job_running(cfg["id"]):
        raise CloudError(f"Für „{cfg['name']}“ läuft schon ein Vorgang – bitte warten.")
    job = manager.new_job(cfg["id"], ["Server auf dem Root stoppen", "Dateiliste holen",
                                      "Dateien herunterladen", "Prüfsummen prüfen",
                                      "Root-Ordner freigeben"])
    return manager._run_job(job, lambda: _download_work(job, cfg, remote_id))    # noqa: SLF001


def _create_local(remote: dict) -> dict:
    """Für eine Instanz ohne lokale Heimat (Premium) einen lokalen Server anlegen."""
    kind = "bedrock" if str(remote.get("type")) == "bedrock" else "java"
    raw = {"name": str(remote.get("name") or "Root-Server"), "type": kind,
           "version": str(remote.get("version") or ""),
           "ram_mb": int(remote.get("ram_mb") or 4096),
           "port": 19132 if kind == "bedrock" else 25565,
           "eula_accepted": True}
    cfg = store.sanitize(raw)
    cfg["installed"] = True                     # die Dateien kommen fertig eingerichtet vom Root
    cfg["cloud"] = {"instance": str(remote.get("id") or ""), "state": str(remote.get("state") or ""),
                    "name": raw["name"], "address": address_of(remote), "updated": int(time.time())}
    store.save(cfg)
    log.info("Lokaler Server für die Rückholung angelegt: %s", cfg["name"])
    return cfg


def _pull_file(remote_id: str) -> pathlib.Path:
    CLOUD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", remote_id)[:64]
    return CLOUD_DIR / f"pull-{safe}.json"


def _download_work(job: dict, cfg: dict, rid: str) -> None:
    folder = store.server_dir(cfg["id"])
    folder.mkdir(parents=True, exist_ok=True)

    manager._step(job, 0, "Der Server wird auf dem Root-Server gestoppt …")          # noqa: SLF001
    detail = remote_detail(rid)
    if detail.get("running"):
        remote_stop(rid, 0)
        _wait_stopped(rid, job)
    _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/pull", body={}, timeout=60)
    _set_link(cfg, instance=rid, state="downloading", running=False)

    manager._step(job, 1, "Die Dateiliste wird vom Root-Server geholt …")            # noqa: SLF001
    data = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/manifest", timeout=600)
    manifest = data.get("manifest") or {}
    files = manifest.get("files") or []
    if not isinstance(files, list):
        raise CloudError("Der Root-Server hat keine gültige Dateiliste geliefert.")
    _pull_file(rid).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _update_state(lambda d: d.__setitem__("transfer", {
        "kind": "download", "local": cfg["id"], "instance": rid,
        "files": len(files), "bytes": int(manifest.get("total_bytes") or 0),
        "started": int(time.time())}))

    manager._step(job, 2, f"{len(files)} Dateien, {human(manifest.get('total_bytes'))}")
    total = int(manifest.get("total_bytes") or 0)
    progress = manager._progress_cb(job, "Herunterladen")                           # noqa: SLF001
    got = 0
    unusable: list[str] = []
    for entry in files:
        rel = str(entry.get("path") or "")
        size = int(entry.get("size") or 0)
        if not _usable_rel(rel):
            unusable.append(rel)
            continue
        target = folder / rel
        part = target.with_name(target.name + PART_SUFFIX)
        if target.is_file() and target.stat().st_size == size \
                and sha256_file(target) == str(entry.get("sha256") or "").lower():
            got += size
            progress(got, total)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        offset = part.stat().st_size if part.is_file() else 0
        if offset > size:
            part.unlink(missing_ok=True)
            offset = 0
        tries = 0
        while offset < size:
            try:
                block = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/download",
                             query={"path": rel, "offset": offset,
                                    "length": min(CHUNK_SIZE, size - offset)},
                             want_bytes=True, timeout=TRANSFER_TIMEOUT)
            except CloudError as exc:
                if exc.status in (401, 403, 404):
                    raise
                tries += 1
                if tries >= TRANSFER_TRIES:
                    raise CloudError(f"„{rel}“ lässt sich nicht herunterladen: {exc} Die schon "
                                     f"geholten Dateien bleiben erhalten – du kannst es später "
                                     f"erneut versuchen.") from exc
                job["detail"] = f"Verbindung unterbrochen – {tries + 1}. Versuch …"
                time.sleep(2 * tries)
                continue
            if not block:
                raise CloudError(f"Der Root-Server liefert für „{rel}“ keine Daten mehr.")
            with part.open("ab") as fh:
                fh.write(block)
            offset += len(block)
            got += len(block)
            tries = 0
            progress(min(got, total), total)
        if size == 0:
            part.write_bytes(b"")
        digest = sha256_file(part)
        if digest != str(entry.get("sha256") or "").lower():
            part.unlink(missing_ok=True)
            raise CloudError(f"Die Prüfsumme von „{rel}“ stimmt nicht – bitte die Rückholung "
                             f"erneut starten.")
        os.replace(str(part), str(target))
        if entry.get("mtime"):
            try:
                os.utime(str(target), (int(entry["mtime"]), int(entry["mtime"])))
            except OSError:
                pass

    manager._step(job, 3, "Alle Dateien werden geprüft …")                           # noqa: SLF001
    report = verify_folder(folder, manifest, progress=manager._progress_cb(job, "Geprüft"))  # noqa: SLF001
    if not report["ok"]:
        raise CloudError(_verify_text(report))

    manager._step(job, 4, "Der Ordner auf dem Root-Server wird freigegeben …")       # noqa: SLF001
    _release(cfg, rid, manifest)
    job["detail"] = (f"{len(files)} Dateien ({human(manifest.get('total_bytes'))}) liegen wieder "
                     f"auf diesem PC."
                     + (f" {len(unusable)} Datei(en) mit Namen, die Windows nicht erlaubt, "
                        f"blieben auf dem Root-Server." if unusable else ""))
    log.info("Server „%s“ vom Root-Server zurückgeholt (%s Dateien).", cfg["name"], len(files))


def _verify_text(report: dict) -> str:
    parts = []
    if report.get("missing_count"):
        parts.append(f"{report['missing_count']} Datei(en) fehlen (z. B. "
                     f"{', '.join(report['missing'][:3])})")
    if report.get("wrong_count"):
        parts.append(f"{report['wrong_count']} Datei(en) stimmen nicht (z. B. "
                     f"{', '.join(report['wrong'][:3])})")
    return ("Die Rückholung ist noch nicht vollständig: " + "; ".join(parts) +
            ". Der Ordner auf dem Root-Server bleibt erhalten – bitte die Rückholung erneut "
            "starten.")


def _release(cfg: dict, rid: str, manifest: dict) -> dict:
    """Freigabe melden – erst danach löscht der Root-Server seinen Ordner."""
    claimed = {"version": MANIFEST_VERSION, "created_at": int(manifest.get("created_at") or time.time()),
               "file_count": len(manifest.get("files") or []),
               "total_bytes": int(manifest.get("total_bytes") or 0),
               "files": [{"path": e["path"], "size": int(e["size"]),
                          "sha256": str(e.get("sha256") or "").lower(),
                          **({"mtime": int(e["mtime"])} if e.get("mtime") else {})}
                         for e in (manifest.get("files") or [])]}
    data = _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/release",
                body={"manifest": claimed}, timeout=300)
    _set_link(cfg, instance=rid, state="local_only", session=None, running=False)
    _update_state(lambda d: d.pop("transfer", None))
    try:
        _pull_file(rid).unlink(missing_ok=True)
    except OSError:
        pass
    _drop_cache()
    return {"ok": True, "deleted": data.get("deleted") or {}}


def release_async(remote_id: str) -> dict:
    """Nachträglich freigeben: prüft die lokalen Dateien noch einmal und meldet dann `release`."""
    cfg = _local_for(remote_id)
    if cfg is None:
        raise CloudError("Zu diesem Server gibt es auf diesem PC keine Kopie – bitte zuerst "
                         "zurückholen.")
    path = _pull_file(remote_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CloudError("Die Dateiliste der Rückholung liegt nicht mehr vor – bitte die "
                         "Rückholung erneut starten.") from exc
    if manager.job_running(cfg["id"]):
        raise CloudError(f"Für „{cfg['name']}“ läuft schon ein Vorgang – bitte warten.")
    job = manager.new_job(cfg["id"], ["Dateien prüfen", "Root-Ordner freigeben"])

    def work() -> None:
        manager._step(job, 0, "Alle Dateien werden geprüft …")                       # noqa: SLF001
        report = verify_folder(store.server_dir(cfg["id"]), manifest,
                               progress=manager._progress_cb(job, "Geprüft"))        # noqa: SLF001
        if not report["ok"]:
            raise CloudError(_verify_text(report))
        manager._step(job, 1, "Der Ordner auf dem Root-Server wird freigegeben …")    # noqa: SLF001
        _release(cfg, remote_id, manifest)

    return manager._run_job(job, work)                                               # noqa: SLF001


def abort_transfer(local_id: str) -> dict:
    """Laufende Übertragung auf dem Root abbrechen (fertige Dateien bleiben dort liegen)."""
    cfg = store.get(local_id)
    if cfg is None:
        raise CloudError("Diesen Server gibt es auf diesem PC nicht.")
    link = link_of(cfg)
    rid, sid = str(link.get("instance") or ""), str(link.get("session") or "")
    if not (rid and sid):
        raise CloudError("Für diesen Server läuft keine Übertragung.")
    _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/upload/abort",
         body={"session": sid, "back_to_local": True})
    _set_link(cfg, session=None, state="local_only")
    _update_state(lambda d: d.pop("transfer", None))
    _drop_cache()
    return {"ok": True}
