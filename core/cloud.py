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
import http.client
import io
import json
import logging
import os
import pathlib
import re
import ssl
import subprocess
import sys
import tarfile
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

# --------------------------------------------------------------- Tempo der Übertragung
#
# Gemessen wurde eine Leitung mit 55 Mbit/s (64 MB in 9,3 s); 630 MB müssten also anderthalb
# Minuten dauern. Gebraucht hat es ein Vielfaches. Vier Ursachen und was dagegen getan wird:
#
# 1. **Nachladbares** (libraries, versions, cache, logs, Serverkern) waren 56 % der Daten. Sie
#    gehen nicht mehr über die Leitung; der Root holt sie selbst (`NACHLADBAR_DIRS`).
# 2. Für **jede** Anfrage baute `urllib` eine neue TLS-Verbindung auf. Jetzt bleibt eine
#    Verbindung offen und wird wiederverwendet (`_Kanal`).
# 3. Eine Datei nach der anderen ließ die Leitung zwischen den Anfragen leerlaufen. Jetzt gehen
#    mehrere Dateien gleichzeitig (`PARALLEL`).
# 4. 92 % aller Dateien sind kleiner als 1 MB. Sie gehen jetzt gebündelt als tar-Strom
#    (`BUNDLE_*`) – ein Paket statt hunderter Einzelanfragen.

#: So viele Dateien gehen gleichzeitig über die Leitung (einstellbar, 1 bis 8).
PARALLEL = max(1, min(8, int(os.environ.get("MCSM_TRANSFER_PARALLEL") or 4)))
#: Bis zu dieser Größe wandert eine Datei in ein Paket statt in eigene Anfragen.
BUNDLE_SMALL_BYTES = 1024 * 1024
#: So groß darf ein Paket höchstens werden (muss zu hosted/core/transfer.py passen).
BUNDLE_MAX_BYTES = 24 * 1024 * 1024
#: So viele Dateien stecken höchstens in einem Paket (dito).
BUNDLE_MAX_FILES = 400
#: So viele offene Dateien lässt sich das Programm je Runde nennen.
MISSING_BATCH = 400
#: Wachsende Pause zwischen den Versuchen – aber niemals aufgeben (Punkt 3 der Beanstandungen).
RETRY_FIRST_WAIT = 2
RETRY_MAX_WAIT = 60
#: So oft wird eine **einzelne** Datei (Plugin nachlegen, Protokoll holen) versucht. Dort wartet
#: jemand vor dem Bildschirm; eine Übertragung im Hintergrund versucht es dagegen unbegrenzt.
TRANSFER_TRIES = 5

# Verwaltungsdateien der Gegenseite (paths.RESERVED_NAMES) und Teil-Dateien gehören nie ins Manifest.
RESERVED_NAMES = {".mcsm", "mcsm.json", "mcsm-instanz.json"}
PART_SUFFIX = ".mcsmpart"
# Ordner, die auf dem PC bleiben: Sicherungen sind lokal gemeint und würden die Übertragung
# vervielfachen (Gegenstück: paths.NUR_LOKAL_DIRS auf dem Root-Server).
SKIP_TOP_DIRS = {"backups"}

# Was der Root-Server selbst beschaffen kann, geht nicht über die Leitung. **Maßgeblich** ist
# hosted/core/paths.py (`NACHLADBAR_DIRS`, `NACHLADBAR_FILES`); hier steht dieselbe Liste noch
# einmal, weil das Programm auf dem PC die Module des Root-Servers nicht mitbringt. Ein Selbsttest
# (hosted/tests/test_transfer.py) vergleicht beide Listen Zeichen für Zeichen.
NACHLADBAR_DIRS = {
    "libraries", "versions", "cache", ".paper-remapped", "bundler",
    "logs", "crash-reports", "debug",
    "definitions", "minecraftpe", "treatments", "internalstorage", "world_templates",
    "premium_cache",
}
NACHLADBAR_FILES = {
    "server.jar", "paper.jar", "build.txt",
    "bedrock_server", "bedrock_server.exe", "bedrock_server_how_to.html",
    "release-notes.txt", "profanity_filter.wlist", "dedicated_server.txt",
}
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


# --------------------------------------------------------------------------- Eine Verbindung
#
# `urllib.request.urlopen` baut für **jede** Anfrage eine neue TCP- und TLS-Verbindung auf. Bei
# einer Instanz mit 246 Dateien, von denen 92 % kleiner als 1 MB sind, kostet das mehr Zeit als
# die Daten selbst: Handschlag, Zertifikatsprüfung und Anlauf des Fensters fallen jedes Mal neu an.
#
# Deshalb hält dieses Modul offene Verbindungen vor und benutzt sie wieder (HTTP/1.1
# Keep-Alive, `http.client`). Weil mehrere Dateien gleichzeitig gehen, ist es ein kleiner Vorrat
# und nicht eine einzelne Verbindung – jeder Faden nimmt sich eine und gibt sie zurück.
#
# Wird eine zurückgelegte Verbindung von der Gegenseite inzwischen geschlossen (nginx tut das nach
# einer Weile), merkt man das erst beim nächsten Gebrauch. Dann wird **einmal** frisch verbunden;
# erst wenn auch das scheitert, gilt der Root-Server als nicht erreichbar.

POOL_IDLE = 40                   # Sekunden: länger unbenutzte Verbindungen werden weggeworfen
POOL_MAX = 10                    # so viele Verbindungen bleiben höchstens liegen

_pool: list[tuple[float, object]] = []
_pool_lock = threading.Lock()
_proxy_info: dict = {}


def _proxy_aktiv() -> bool:
    """Ist für HTTPS ein Vermittlungsrechner (Proxy) eingestellt?

    ``http.client`` kennt keine Proxys. Wo einer eingestellt ist (Firmennetz), bleibt es beim Weg
    über ``urllib`` – lieber langsam als überhaupt nicht.
    """
    if "wert" not in _proxy_info:
        try:
            proxies = urllib.request.getproxies()
        except Exception:                                  # noqa: BLE001 - nie am Proxy scheitern
            proxies = {}
        _proxy_info["wert"] = bool(proxies.get("https") or proxies.get("http"))
    return bool(_proxy_info["wert"])


def _neue_verbindung(timeout: int):
    teile = urllib.parse.urlsplit(BASE_URL)
    host = teile.hostname or ""
    port = teile.port
    if teile.scheme == "http":
        return http.client.HTTPConnection(host, port, timeout=timeout)
    return http.client.HTTPSConnection(host, port, timeout=timeout,
                                       context=ssl.create_default_context())


def _hole_verbindung(timeout: int) -> tuple[object, bool]:
    """Eine Verbindung aus dem Vorrat (``False``) oder eine frische (``True``)."""
    grenze = time.time() - POOL_IDLE
    with _pool_lock:
        while _pool:
            seit, conn = _pool.pop()
            if seit >= grenze:
                try:
                    conn.sock.settimeout(timeout)          # type: ignore[union-attr]
                except (AttributeError, OSError):
                    pass
                return conn, False
            _schliesse(conn)
    return _neue_verbindung(timeout), True


def _gib_verbindung(conn) -> None:
    with _pool_lock:
        if len(_pool) >= POOL_MAX:
            _schliesse(conn)
            return
        _pool.append((time.time(), conn))


def _schliesse(conn) -> None:
    try:
        conn.close()
    except Exception:                                      # noqa: BLE001 - beim Schließen egal
        pass


def close_connections() -> None:
    """Alle offenen Verbindungen zum Root-Server schließen (Abmelden, Ende einer Übertragung)."""
    with _pool_lock:
        liegend = list(_pool)
        _pool.clear()
    for _seit, conn in liegend:
        _schliesse(conn)


def _keep_alive(status: int, kopf: dict, resp) -> bool:
    """Darf diese Verbindung danach weiterbenutzt werden?"""
    if getattr(resp, "will_close", True):
        return False
    verbindung = str(kopf.get("Connection") or kopf.get("connection") or "").lower()
    return "close" not in verbindung and status != 101


def _http_call(method: str, url: str, headers: dict, body: bytes | None,
               timeout: int) -> tuple[int, bytes, dict]:
    """Ein Aufruf über eine (möglichst schon offene) Verbindung."""
    teile = urllib.parse.urlsplit(url)
    ziel = teile.path + (("?" + teile.query) if teile.query else "")
    letzte: BaseException | None = None
    for _versuch in (1, 2):
        conn, frisch = _hole_verbindung(timeout)
        try:
            conn.request(method, ziel, body=body, headers=headers)      # type: ignore[union-attr]
            resp = conn.getresponse()                                  # type: ignore[union-attr]
            daten = resp.read()
            kopf = {k.title(): v for k, v in resp.getheaders()}
            if _keep_alive(resp.status, kopf, resp):
                _gib_verbindung(conn)
            else:
                _schliesse(conn)
            return int(resp.status), daten, kopf
        except (http.client.HTTPException, OSError, ValueError) as exc:
            _schliesse(conn)
            letzte = exc
            if sources._is_cert_error(exc):                             # noqa: SLF001
                raise
            if frisch:
                break
            # Eine zurückgelegte Verbindung war schon geschlossen – einmal frisch versuchen.
    raise CloudError(f"Der Root-Server ist nicht erreichbar: {letzte}") from letzte


def _raw_call(method: str, url: str, headers: dict, body: bytes | None,
              timeout: int) -> tuple[int, bytes, dict]:
    """Ein Aufruf: (Status, Rumpf, Kopfzeilen). Bei Zertifikatsfehlern über curl."""
    global _ssl_broken
    if _ssl_broken or sources._ssl_broken:             # noqa: SLF001 – einmal kaputt, immer curl
        status, data = _curl_call(method, url, headers, body, timeout)
        return status, data, {}
    if not _proxy_aktiv():
        try:
            return _http_call(method, url, headers, body, timeout)
        except (ssl.SSLError, OSError) as exc:
            if not sources._is_cert_error(exc):                          # noqa: SLF001
                raise CloudError(f"Der Root-Server ist nicht erreichbar: {exc}") from exc
            if sources._windows_curl():                                  # noqa: SLF001
                _ssl_broken = True
                close_connections()
                status, data = _curl_call(method, url, headers, body, timeout)
                return status, data, {}
            raise CloudError(sources.CERT_HINT) from exc
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


# ------------------------------------------------- Xbox-Freunde-Modus auf dem Root-Server
# MCXboxBroadcast meldet Xbox-Live-Freunden nur eine Adresse und einen Port – der Bot müsste also
# nicht auf derselben Maschine wie der Server laufen. Auf dem Root soll er es trotzdem: dann bleiben
# Anmeldung und Bot dort, und die Freunde sehen den Server auch, wenn dieser PC aus ist.
# Die Felder sind dieselben wie bei ``manager.xbox_status`` – die Oberfläche kennt nur eine Karte.

#: Ein Root-Server, der diese Wege noch nicht ausgerollt hat, antwortet mit einem davon.
#: 405 ist dabei: dann gibt es den Weg, aber nicht die Methode – aus Sicht der Karte dasselbe.
XBOX_FEHLT = (404, 405, 501)
XBOX_HINWEIS = ("Der Xbox-Freunde-Modus steht auf dem Root-Server noch nicht bereit. Sobald der "
                "Betreiber den Dienst dort aktualisiert hat, lässt er sich von hier aus einrichten.")
#: Zustände, die der Bot melden darf (alles andere wird aus „running“ abgeleitet).
XBOX_STATES = ("off", "starting", "login", "online")


def _xbox_route(remote_id: str, rest: str) -> str:
    return f"/api/servers/{urllib.parse.quote(remote_id)}/xbox{rest}"


def _xbox_call(remote_id: str, rest: str, *, method: str = "GET", body=None) -> dict | None:
    """Ein Aufruf an den Root-Server – ``None``, wenn er diesen Weg noch nicht kennt.

    Nur die Wege des Xbox-Modus werden so behandelt: ein 404 heißt hier „noch nicht ausgerollt“,
    und die Karte sagt das ruhig, statt einen Fehler über die halbe Seite zu legen.
    """
    try:
        data = _api(method, _xbox_route(remote_id, rest), body=body)
    except CloudError as exc:
        if exc.status in XBOX_FEHLT:
            return None
        raise
    return data if isinstance(data, dict) else {}


def _xbox_leer(*, hint: str = "", supported: bool = True) -> dict:
    """Karte ohne Stand vom Root-Server – dieselben Felder wie ``manager.xbox_status``."""
    return {"supported": bool(supported), "hint": hint, "hosted": True,
            "enabled": False, "installed": False, "running": False, "state": "off",
            "code": "", "url": "", "gamertag": "", "error": "",
            "address": "", "port": 0, "host_name": "", "autostart": True, "token_cached": False}


def _xbox_port(roh: dict) -> int:
    try:
        port = int(str(roh.get("port") or 0).strip())
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def _xbox_view(data: dict) -> dict:
    """Antwort des Root-Servers auf die Felder bringen, die die Oberfläche von lokal kennt.

    Alles kommt von der Gegenseite und wird deshalb beschnitten und geprüft – ein zu langer
    Gamertag oder eine fremde Adresse im Feld ``url`` soll die Karte nicht aufreißen.
    """
    roh = data.get("xbox") if isinstance(data.get("xbox"), dict) else data
    if not isinstance(roh, dict):
        roh = {}
    url = str(roh.get("url") or "").strip()[:200]
    zustand = str(roh.get("state") or "").strip().lower()
    out = _xbox_leer()
    out.update(
        enabled=bool(roh.get("enabled")),
        installed=bool(roh.get("installed")),
        running=bool(roh.get("running")),
        state=zustand if zustand in XBOX_STATES else ("online" if roh.get("running") else "off"),
        # Der Anmeldecode von Microsoft ist kurz und alphanumerisch – mehr wird nicht angezeigt.
        code=re.sub(r"[^A-Za-z0-9]", "", str(roh.get("code") or ""))[:16].upper(),
        url=url if url.startswith("https://") else "",
        gamertag=str(roh.get("gamertag") or "").strip()[:64],
        error=str(roh.get("error") or "").strip()[:500],
        address=str(roh.get("address") or "").strip()[:200],
        port=_xbox_port(roh),
        host_name=str(roh.get("host_name") or "").strip()[:64],
        autostart=bool(roh.get("autostart", True)),
        token_cached=bool(roh.get("token_cached")),
    )
    return out


def _xbox_antwort(remote_id: str, data: dict | None, *, meldung: str) -> dict:
    """Gemeinsame Antwort der Schaltwege: kennt der Root sie nicht, sagt die Karte das."""
    if data is None:
        return {"ok": False, "supported": False, "hint": XBOX_HINWEIS}
    out = {"ok": True, "supported": True,
           "message": str(data.get("message") or meldung)[:300]}
    # Schickt der Root gleich den neuen Stand mit, zeigt die Karte ihn ohne zweite Anfrage.
    if isinstance(data.get("xbox"), dict):
        out["xbox"] = _xbox_view(data)
    return out


def remote_xbox_status(remote_id: str) -> dict:
    """Zustand des Bots auf dem Root-Server (off | starting | login | online)."""
    data = _xbox_call(remote_id, "/status")
    if data is None:
        return _xbox_leer(hint=XBOX_HINWEIS, supported=False)
    return _xbox_view(data)


def remote_xbox_setup(remote_id: str, *, host_name: str = "", address: str = "",
                      autostart: bool = True, enabled: bool = True) -> dict:
    """Bot auf dem Root-Server einrichten: Jar holen, Konfiguration schreiben, starten.

    Die Adresse bleibt in der Regel leer – dann nimmt der Root seine eigene, öffentlich
    erreichbare Adresse samt Bedrock-Port. Genau das ist der Gewinn gegenüber dem PC.
    """
    body: dict = {"enabled": bool(enabled), "autostart": bool(autostart)}
    if host_name:
        body["host_name"] = str(host_name)[:64]
    if address:
        body["address"] = str(address)[:200]
    data = _xbox_call(remote_id, "/setup", method="POST", body=body)
    if data is None:
        return {"ok": False, "supported": False, "hint": XBOX_HINWEIS}
    _drop_cache()
    out = _xbox_antwort(remote_id, data, meldung="Der Xbox-Freunde-Modus wird auf dem Root-Server "
                                                 "eingerichtet.")
    for key in ("job_id", "job"):                     # falls der Root seinen Fortschritt meldet
        if key in data:
            out[key] = data[key]
    return out


def remote_xbox_start(remote_id: str) -> dict:
    return _xbox_antwort(remote_id, _xbox_call(remote_id, "/start", method="POST", body={}),
                         meldung="Der Bot wird auf dem Root-Server gestartet.")


def remote_xbox_stop(remote_id: str) -> dict:
    return _xbox_antwort(remote_id, _xbox_call(remote_id, "/stop", method="POST", body={}),
                         meldung="Der Bot auf dem Root-Server wird gestoppt.")


def remote_xbox_reset(remote_id: str) -> dict:
    """Gespeicherte Anmeldung auf dem Root-Server verwerfen – beim Start kommt ein neuer Code."""
    return _xbox_antwort(remote_id, _xbox_call(remote_id, "/reset", method="POST", body={}),
                         meldung="Die Anmeldung auf dem Root-Server wurde verworfen.")


def remote_xbox_disable(remote_id: str) -> dict:
    """Ausschalten: erst den Bot stoppen, dann die Einstellung auf dem Root umlegen.

    Ein eigener Weg dafür ist nicht verabredet; ``setup`` trägt die Einstellungen, also auch
    ``enabled: false``. Der Stopp vorher wirkt selbst dann, wenn der Root das Feld übergeht.
    """
    halt = remote_xbox_stop(remote_id)
    if not halt.get("supported", True):
        return halt
    aus = remote_xbox_setup(remote_id, enabled=False, autostart=False)
    if not aus.get("supported", True):
        return aus
    return {"ok": True, "supported": True,
            "message": "Der Xbox-Freunde-Modus ist auf dem Root-Server ausgeschaltet.",
            **({"xbox": aus["xbox"]} if isinstance(aus.get("xbox"), dict) else {})}


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
        fortschritt = _Fortschritt({}, "Hochladen", size, rueckruf=progress)
        fortschritt.basis(versatz)
        _upload_file(remote_id, sid, pfad, rel, versatz, size, fortschritt)
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


def _nachladbar(rel: str) -> bool:
    """Kann der Root-Server das selbst beschaffen? Dann geht es nicht über die Leitung.

    Dieselbe Entscheidung trifft ``paths.nachladbar`` auf dem Root-Server – für das Manifest der
    Rückholung. Beide Listen müssen gleich sein, sonst gälte beim Zurückholen als „fehlt“, was
    nie hochgeladen wurde.
    """
    teile = [t.lower() for t in str(rel or "").replace("\\", "/").strip("/").split("/")
             if t not in ("", ".")]
    if not teile:
        return False
    if teile[0] in NACHLADBAR_DIRS:
        return True
    return len(teile) == 1 and teile[0] in NACHLADBAR_FILES


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
    """Manifest eines lokalen Serverordners – Format wie hosted/core/transfer.py.

    Was der Root-Server selbst beschaffen kann (``_nachladbar``), kommt gar nicht erst hinein:
    an einem echten Server waren ``libraries``, ``cache``, ``versions`` und ``logs`` zusammen
    56 % der Daten. Der Root lädt sie in Sekunden, die Leitung des Benutzers braucht Minuten
    dafür. Übergangen wird das **still** – es fehlt nichts, es gehört nur nicht dazu.
    """
    folder = pathlib.Path(folder)
    files: list[dict] = []
    skipped: list[str] = []
    ausgelassen = 0
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
            if _nachladbar(rel):
                ausgelassen += 1
                continue
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
            "total_bytes": total, "files": files, "skipped": skipped[:50],
            "nachladbar": ausgelassen}


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


# --------------------------------------------------------------------------- Abbruch und Geduld

class TransferAbgebrochen(CloudError):
    """Der Benutzer hat die Übertragung abgebrochen – kein Fehler der Gegenstelle."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or "Die Übertragung wurde abgebrochen.", 0)


_abbruch: dict[str, threading.Event] = {}


def _abbruch_event(local_id: str) -> threading.Event:
    with _lock:
        ereignis = _abbruch.get(str(local_id))
        if ereignis is None:
            ereignis = threading.Event()
            _abbruch[str(local_id)] = ereignis
        return ereignis


def _pruefe_abbruch(local_id: str) -> None:
    if _abbruch_event(local_id).is_set():
        raise TransferAbgebrochen()


def _warte_geduldig(versuch: int, local_id: str, job: dict | None = None,
                    grund: str = "") -> None:
    """Wachsende Pause vor dem nächsten Versuch – aufgegeben wird nie, abgebrochen schon.

    Früher gab das Programm nach fünf Versuchen auf und die Übertragung blieb liegen. Eine
    Leitung, die kurz weg ist, darf eine Übertragung von 600 MB nicht beenden: die Pause wächst
    von zwei Sekunden bis auf eine Minute und bleibt dann dabei.
    """
    pause = min(RETRY_MAX_WAIT, RETRY_FIRST_WAIT * (2 ** max(0, versuch - 1)))
    if job is not None:
        job["detail"] = (f"Verbindung unterbrochen – neuer Versuch in {int(pause)} s "
                         f"({versuch}. Anlauf). {grund}".strip())
    ereignis = _abbruch_event(local_id)
    if ereignis.wait(pause):
        raise TransferAbgebrochen()


def _dauer_text(sekunden: float) -> str:
    """„40 s“, „4 min“, „1 h 05 min“ – für die geschätzte Restzeit."""
    sekunden = int(max(0, sekunden))
    if sekunden < 90:
        return f"{sekunden} s"
    if sekunden < 3600:
        return f"{sekunden // 60} min"
    return f"{sekunden // 3600} h {sekunden % 3600 // 60:02d} min"


class _Fortschritt:
    """Fortschritt einer Übertragung mit Geschwindigkeit und geschätzter Restzeit.

    Die Angaben stehen im Vorgang (``job``) **und** in ``data/cloud.json``. So sieht man sie auch
    dann noch, wenn man den Bereich wechselt, und das Programm weiß nach einem Neustart, dass
    eine Übertragung offen ist.
    """

    def __init__(self, job: dict, label: str, total: int, *, merken: dict | None = None,
                 rueckruf=None) -> None:
        self.job = job
        self.label = str(label)
        self.total = max(0, int(total or 0))
        self.merken = dict(merken or {})
        self.rueckruf = rueckruf        # zusätzlich melden (einzelne Datei in der Oberfläche)
        self.gesendet = 0
        self.beginn = time.time()
        self.proben: list[tuple[float, int]] = [(self.beginn, 0)]
        self._lock = threading.Lock()
        self._anzeige = 0.0
        self._gemerkt = 0.0

    def basis(self, bytes_schon_da: int) -> None:
        """Was die Gegenseite schon hat (nach einer Wiederaufnahme)."""
        with self._lock:
            self.gesendet = max(self.gesendet, int(bytes_schon_da or 0))
            jetzt = time.time()
            self.proben = [(jetzt, self.gesendet)]
        self.zeige(erzwingen=True)

    def dazu(self, bytes_neu: int) -> None:
        if bytes_neu <= 0:
            return
        with self._lock:
            self.gesendet += int(bytes_neu)
        self.zeige()

    def tempo(self) -> float:
        """Bytes je Sekunde über die letzten Sekunden (0, solange es zu früh ist)."""
        with self._lock:
            if len(self.proben) < 2:
                return 0.0
            (t1, b1), (t2, b2) = self.proben[0], self.proben[-1]
        return (b2 - b1) / (t2 - t1) if t2 > t1 and b2 > b1 else 0.0

    def zeige(self, *, erzwingen: bool = False) -> None:
        jetzt = time.time()
        with self._lock:
            self.proben.append((jetzt, self.gesendet))
            while len(self.proben) > 2 and jetzt - self.proben[0][0] > 20:
                self.proben.pop(0)
            gesendet = self.gesendet
            zu_frueh = (jetzt - self._anzeige) < 0.4
        if zu_frueh and not erzwingen:
            return
        self._anzeige = jetzt
        tempo = self.tempo()
        rest = max(0, self.total - gesendet)
        text = f"{self.label}: {gesendet / 1048576:.1f} / {self.total / 1048576:.1f} MB"
        if tempo > 0:
            text += f" · {tempo / 1048576:.1f} MB/s · noch etwa {_dauer_text(rest / tempo)}"
        self.job["detail"] = text
        self.job["done"] = gesendet
        self.job["total"] = self.total
        self.job["speed_bps"] = int(tempo)
        self.job["speed_text"] = f"{tempo / 1048576:.1f} MB/s".replace(".", ",") if tempo else ""
        self.job["eta_seconds"] = int(rest / tempo) if tempo > 0 else 0
        self.job["eta_text"] = _dauer_text(rest / tempo) if tempo > 0 else ""
        if self.rueckruf is not None:
            try:
                self.rueckruf(gesendet, self.total)
            except Exception:                          # noqa: BLE001 - Anzeige darf nie stören
                pass
        if self.merken and (erzwingen or jetzt - self._gemerkt > 3.0):
            self._gemerkt = jetzt
            angabe = dict(self.merken)
            angabe.update({"done_bytes": gesendet, "speed_bps": int(tempo),
                           "eta_seconds": int(rest / tempo) if tempo > 0 else 0,
                           "updated": int(jetzt)})
            _transfer_merken(angabe)


def _transfer_merken(angabe: dict | None) -> None:
    """Die offene Übertragung in ``data/cloud.json`` festhalten (oder den Eintrag löschen)."""
    if angabe is None:
        _update_state(lambda d: d.pop("transfer", None))
        return
    _update_state(lambda d: d.__setitem__("transfer", dict(angabe)))


def _parallel(auftraege: list, ausfuehren, anzahl: int, local_id: str) -> None:
    """Mehrere Aufträge gleichzeitig abarbeiten; der erste Fehler beendet die Runde.

    Die Reihenfolge der Wiederaufnahme bleibt heil: ein Auftrag ist immer eine **ganze** Datei
    (oder ein Paket). Zwei Stücke derselben Datei gleichzeitig gibt es nicht – der Versatz käme
    sonst durcheinander.
    """
    if not auftraege:
        return
    if len(auftraege) == 1 or anzahl <= 1:
        for auftrag in auftraege:
            _pruefe_abbruch(local_id)
            ausfuehren(auftrag)
        return
    rest = list(auftraege)
    schloss = threading.Lock()
    fehler: list[BaseException] = []

    def arbeiter() -> None:
        while True:
            with schloss:
                if fehler or not rest:
                    return
                auftrag = rest.pop(0)
            try:
                _pruefe_abbruch(local_id)
                ausfuehren(auftrag)
            except BaseException as exc:                # noqa: BLE001 - wird weitergereicht
                with schloss:
                    fehler.append(exc)
                return

    faeden = [threading.Thread(target=arbeiter, daemon=True, name="mcsm-transfer")
              for _ in range(min(int(anzahl), len(auftraege)))]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join()
    if fehler:
        raise fehler[0]


def _pakete_bilden(dateien: list[tuple[str, dict]], *, max_bytes: int = BUNDLE_MAX_BYTES,
                   max_files: int = BUNDLE_MAX_FILES) -> list[list[tuple[str, dict]]]:
    """Kleine Dateien zu Paketen bündeln (Größe und Anzahl begrenzt).

    Ein tar-Eintrag kostet einen Kopf von 512 Bytes und füllt auf ein Vielfaches von 512 auf –
    das wird mitgerechnet, damit ein Paket die Grenze der Gegenseite nicht überschreitet.
    """
    pakete: list[list[tuple[str, dict]]] = []
    laufend: list[tuple[str, dict]] = []
    belegt = 1024
    for rel, eintrag in dateien:
        braucht = 1024 + ((int(eintrag["size"]) + 511) // 512) * 512
        if laufend and (belegt + braucht > max_bytes or len(laufend) >= max_files):
            pakete.append(laufend)
            laufend, belegt = [], 1024
        laufend.append((rel, eintrag))
        belegt += braucht
    if laufend:
        pakete.append(laufend)
    return pakete


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
                body={"manifest": {k: v for k, v in manifest.items()
                                   if k not in ("skipped", "nachladbar")},
                      "purpose": "instance"}, timeout=120)
    session = data.get("session") or {}
    if not session.get("id"):
        raise CloudError("Der Root-Server hat keine Übertragung eröffnet.")
    return session


def _upload_work(job: dict, cfg: dict) -> None:
    folder = store.server_dir(cfg["id"])
    if not folder.is_dir():
        raise CloudError("Den Ordner dieses Servers gibt es auf dem PC nicht.")
    _abbruch_event(cfg["id"]).clear()

    manager._step(job, 0, "Dateien werden gelesen und geprüft …")       # noqa: SLF001
    manifest = build_manifest(folder, progress=manager._progress_cb(job, "Gelesen"))  # noqa: SLF001
    if not manifest["files"]:
        raise CloudError("In diesem Serverordner gibt es keine Dateien zum Übertragen.")

    manager._step(job, 1, "Server wird auf dem Root-Server angemeldet …")            # noqa: SLF001
    rid = _ensure_remote_instance(cfg)

    manager._step(job, 2, f"{manifest['file_count']} Dateien, {human(manifest['total_bytes'])}"
                          + (f" (dazu {manifest['nachladbar']} Einträge, die der Root-Server "
                             f"selbst lädt)" if manifest.get("nachladbar") else ""))
    session = _begin_upload(cfg, rid, manifest)
    sid = str(session["id"])
    _set_link(cfg, instance=rid, session=sid, state="uploading")
    merken = {"kind": "upload", "local": cfg["id"], "instance": rid, "session": sid,
              "name": cfg.get("name") or "", "files": manifest["file_count"],
              "bytes": manifest["total_bytes"], "started": int(time.time())}
    _transfer_merken(merken)

    manager._step(job, 3, "Übertragung läuft …")                                     # noqa: SLF001
    total = int(manifest["total_bytes"])
    fortschritt = _Fortschritt(job, "Hochladen", total, merken=merken)
    index = {e["path"]: e for e in manifest["files"]}
    versuch = 0
    stillstand = 0
    while True:
        _pruefe_abbruch(cfg["id"])
        try:
            data = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/upload/status",
                        query={"session": sid, "missing": MISSING_BATCH})
        except AuthError:
            raise
        except CloudError as exc:
            if exc.status in (403, 404, 409):
                raise
            versuch += 1
            _warte_geduldig(versuch, cfg["id"], job, str(exc))
            continue
        session = data.get("session") or {}
        if session.get("complete"):
            break
        if str(session.get("state")) != "open":
            raise CloudError("Die Übertragung wurde auf dem Root-Server beendet – bitte erneut "
                             "beginnen.")
        offen = session.get("missing") or ([session["next"]] if session.get("next") else [])
        if not offen:
            raise CloudError("Der Root-Server nennt keine offenen Dateien, meldet die Übertragung "
                             "aber auch nicht als fertig.")
        vorher = int(session.get("received_bytes") or 0)
        fortschritt.basis(vorher)      # der Root kennt den Stand – auch nach einem Abbruch
        try:
            _upload_runde(rid, sid, folder, index, offen, fortschritt, cfg["id"], session)
        except TransferAbgebrochen:
            raise
        except AuthError:
            raise
        except CloudError as exc:
            if exc.status in (403, 404, 409):
                raise
            versuch += 1
            _warte_geduldig(versuch, cfg["id"], job, str(exc))
            continue
        versuch = 0
        nachher = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/upload/status",
                       query={"session": sid, "missing": 1}).get("session") or {}
        if not nachher.get("complete") and int(nachher.get("received_bytes") or 0) <= vorher:
            # Kein Abriss, aber auch kein Fortschritt: das ist kein Leitungsproblem, das wiederholt
            # sich beliebig oft. Nach ein paar Anläufen lieber mit einem klaren Satz anhalten.
            stillstand += 1
            if stillstand >= 5:
                raise CloudError("Die Übertragung kommt nicht voran: der Root-Server nimmt die "
                                 "Dateien an, zählt aber nichts dazu. Bitte die Übertragung "
                                 "abbrechen und neu beginnen.")
            _warte_geduldig(stillstand, cfg["id"], job, "Der Root-Server meldet keinen Zuwachs.")
        else:
            stillstand = 0

    manager._step(job, 4, "Übertragung wird abgeschlossen …")                        # noqa: SLF001
    data = _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/upload/finish",
                body={"session": sid, "hashes": True,
                      # Der Root beschafft jetzt, was nicht übertragen wurde: Serverkern und
                      # Java in **derselben** Version wie auf dem PC, dazu Crossplay, wenn hier
                      # eines eingerichtet ist.
                      "version": str(cfg.get("version") or ""),
                      "geyser": bool(cfg.get("geyser"))}, timeout=600)
    remote = data.get("server") or {}
    _set_link(cfg, instance=rid, session=None, state=str(remote.get("state") or "hosted"),
              name=str(remote.get("name") or cfg["name"]), address=address_of(remote),
              running=False)
    _transfer_merken(None)
    _drop_cache()
    close_connections()
    report = data.get("report") or {}
    dauer = max(1e-6, time.time() - fortschritt.beginn)
    job["detail"] = (f"{report.get('files', manifest['file_count'])} Dateien "
                     f"({human(report.get('bytes', manifest['total_bytes']))}) liegen auf dem "
                     f"Root-Server – {human(total / dauer)}/s.")
    _warte_auf_kern(job, rid, data.get("job"))
    log.info("Server „%s“ auf den Root-Server verschoben (%s Dateien, %s, %.1f s).",
             cfg["name"], report.get("files", manifest["file_count"]),
             human(report.get("bytes", total)), dauer)


def _warte_auf_kern(job: dict, rid: str, kern_job) -> None:
    """Auf das Beschaffen des Serverkerns auf dem Root warten (Java, Paper-Jar, Crossplay).

    ``libraries``, ``versions``, ``cache`` und der Serverkern werden nicht übertragen – der Root
    lädt sie selbst. Das darf nicht erst beim ersten Start auffallen, deshalb wird hier gewartet,
    solange es läuft. Geht dabei etwas schief, steht es im Vorgang; die Dateien sind trotzdem
    angekommen.
    """
    kennung = str((kern_job or {}).get("id") or "")
    if not kennung:
        return
    ende = time.time() + 900
    while time.time() < ende:
        try:
            stand = (_api("GET", f"/api/jobs/{urllib.parse.quote(kennung)}",
                          timeout=30).get("job") or {})
        except CloudError:
            return
        zustand = str(stand.get("state") or "")
        if zustand == "fertig":
            job["detail"] = (str(job.get("detail") or "")
                             + " Serverkern und Java liegen auf dem Root-Server bereit.")
            return
        if zustand == "fehler":
            job["detail"] = (str(job.get("detail") or "") + " Hinweis: "
                             + str(stand.get("error") or "Der Serverkern fehlt noch."))
            return
        job["detail"] = f"Auf dem Root-Server: {stand.get('step') or 'wird beschafft …'}"
        time.sleep(2)


def _upload_runde(rid: str, sid: str, folder: pathlib.Path, index: dict, offen: list,
                  fortschritt: _Fortschritt, local_id: str, session: dict) -> None:
    """Eine Runde: alle offenen Dateien schicken – kleine gebündelt, mehrere gleichzeitig."""
    paket_geht = bool(session.get("bundle"))
    klein_grenze = min(int(session.get("bundle_small_bytes") or BUNDLE_SMALL_BYTES),
                       BUNDLE_SMALL_BYTES)
    paket_bytes = min(int(session.get("bundle_max_bytes") or BUNDLE_MAX_BYTES), BUNDLE_MAX_BYTES)
    paket_dateien = min(int(session.get("bundle_max_files") or BUNDLE_MAX_FILES), BUNDLE_MAX_FILES)
    klein: list[tuple[str, dict]] = []
    gross: list[tuple[str, dict, int]] = []
    for item in offen:
        rel = str(item.get("path") or "")
        eintrag = index.get(rel)
        if eintrag is None:
            raise CloudError(f"Der Root-Server verlangt die unbekannte Datei „{rel}“.")
        versatz = int(item.get("offset") or 0)
        groesse = int(eintrag["size"])
        # Nur eine noch gar nicht angefangene kleine Datei darf ins Paket: eine halb übertragene
        # Datei muss bei ihrem Versatz weitergehen, sonst bricht die Wiederaufnahme.
        if paket_geht and versatz == 0 and 0 < groesse <= klein_grenze:
            klein.append((rel, eintrag))
        else:
            gross.append((rel, eintrag, versatz))

    auftraege: list[tuple[str, object]] = []
    for paket in _pakete_bilden(klein, max_bytes=paket_bytes, max_files=paket_dateien):
        auftraege.append(("paket", paket))
    for rel, eintrag, versatz in gross:
        auftraege.append(("datei", (rel, eintrag, versatz)))

    def ausfuehren(auftrag: tuple) -> None:
        art, inhalt = auftrag
        if art == "paket":
            _upload_paket(rid, sid, folder, inhalt, fortschritt)       # type: ignore[arg-type]
            return
        rel, eintrag, versatz = inhalt                                 # type: ignore[misc]
        _upload_file(rid, sid, folder / rel, rel, versatz, int(eintrag["size"]), fortschritt)

    _parallel(auftraege, ausfuehren, PARALLEL, local_id)


def _upload_paket(rid: str, sid: str, folder: pathlib.Path, dateien: list,
                  fortschritt: _Fortschritt) -> None:
    """Viele kleine Dateien als tar-Strom in **einer** Anfrage schicken."""
    puffer = io.BytesIO()
    mitgeschickt = 0
    with tarfile.open(fileobj=puffer, mode="w", format=tarfile.PAX_FORMAT,
                      encoding="utf-8") as archiv:
        for rel, eintrag in dateien:
            pfad = folder / rel
            try:
                groesse = pfad.stat().st_size
            except OSError as exc:
                raise CloudError(f"Die Datei „{rel}“ gibt es auf dem PC nicht mehr – bitte die "
                                 f"Übertragung neu beginnen.") from exc
            if groesse != int(eintrag["size"]):
                raise CloudError(f"Die Datei „{rel}“ hat sich seit dem Beginn der Übertragung "
                                 f"geändert – bitte die Übertragung neu beginnen.")
            inhalt = pfad.read_bytes()
            if len(inhalt) != groesse:
                raise CloudError(f"Die Datei „{rel}“ ließ sich nicht vollständig lesen.")
            info = tarfile.TarInfo(rel)
            info.size = len(inhalt)
            info.mtime = int(eintrag.get("mtime") or time.time())
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            archiv.addfile(info, io.BytesIO(inhalt))
            mitgeschickt += len(inhalt)
    _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/upload/bundle",
         raw=puffer.getvalue(), timeout=TRANSFER_TIMEOUT,
         query={"session": sid}, headers={"X-MCSM-Session": sid})
    fortschritt.dazu(mitgeschickt)


def _upload_file(rid: str, sid: str, path: pathlib.Path, rel: str, offset: int, size: int,
                 fortschritt: _Fortschritt) -> None:
    """Eine Datei ab ``offset`` in Stücken hochladen und den Fortschritt fortschreiben."""
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
            fortschritt.dazu(max(0, reached - offset))
            offset = reached
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
    _abbruch_event(cfg["id"]).clear()

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
    merken = {"kind": "download", "local": cfg["id"], "instance": rid,
              "name": cfg.get("name") or "", "files": len(files),
              "bytes": int(manifest.get("total_bytes") or 0), "started": int(time.time())}
    _transfer_merken(merken)

    manager._step(job, 2, f"{len(files)} Dateien, {human(manifest.get('total_bytes'))}")
    total = int(manifest.get("total_bytes") or 0)
    merken.update({"files": len(files), "bytes": total})
    fortschritt = _Fortschritt(job, "Herunterladen", total, merken=merken)
    unusable: list[str] = []
    brauchbar: list[dict] = []
    for entry in files:
        rel = str(entry.get("path") or "")
        if not _usable_rel(rel):
            unusable.append(rel)                 # Name, den Windows nicht speichern kann
            continue
        brauchbar.append(entry)
    offen, schon_da = _offene_dateien(folder, brauchbar, streng=True)
    fortschritt.basis(schon_da)
    paket_geht = True
    versuch = 0
    while offen:
        _pruefe_abbruch(cfg["id"])
        try:
            paket_geht = _download_runde(rid, folder, offen, fortschritt, cfg["id"], paket_geht)
        except TransferAbgebrochen:
            raise
        except AuthError:
            raise
        except CloudError as exc:
            if exc.status in (403, 404):
                raise
            versuch += 1
            _warte_geduldig(versuch, cfg["id"], job, str(exc))
        else:
            versuch = 0
        offen, _fertig = _offene_dateien(folder, brauchbar, streng=False)

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


def _offene_dateien(folder: pathlib.Path, eintraege: list[dict], *,
                    streng: bool) -> tuple[list[dict], int]:
    """Welche Dateien fehlen noch? Rückgabe ``(offene Einträge, schon vorhandene Bytes)``.

    ``streng`` prüft zusätzlich die Prüfsumme – das lohnt einmal beim Aufsetzen auf eine
    abgebrochene Rückholung. In den Runden danach genügt die Größe: jede Datei ist erst dann an
    ihren endgültigen Namen gekommen, wenn ihre Prüfsumme gestimmt hat.
    """
    offen: list[dict] = []
    da = 0
    for eintrag in eintraege:
        rel = str(eintrag.get("path") or "")
        groesse = int(eintrag.get("size") or 0)
        ziel = folder / rel
        try:
            passt = ziel.is_file() and ziel.stat().st_size == groesse
        except OSError:
            passt = False
        if passt and streng and groesse:
            passt = sha256_file(ziel) == str(eintrag.get("sha256") or "").lower()
        if passt:
            da += groesse
            continue
        offen.append(eintrag)
    return offen, da


def _download_runde(rid: str, folder: pathlib.Path, offen: list[dict],
                    fortschritt: _Fortschritt, local_id: str, paket_geht: bool) -> bool:
    """Eine Runde der Rückholung: kleine Dateien im Paket, große in Stücken, mehrere zugleich.

    Rückgabe: ob der Root-Server Pakete kann (ein älterer Stand kennt die Route nicht – dann
    läuft alles wie früher über einzelne Stücke).
    """
    klein: list[dict] = []
    gross: list[dict] = []
    for eintrag in offen:
        groesse = int(eintrag.get("size") or 0)
        teil = folder / (str(eintrag.get("path")) + PART_SUFFIX)
        angefangen = teil.is_file()
        if paket_geht and not angefangen and 0 < groesse <= BUNDLE_SMALL_BYTES:
            klein.append(eintrag)
        else:
            gross.append(eintrag)

    auftraege: list[tuple[str, object]] = []
    for paket in _pakete_bilden([(str(e["path"]), e) for e in klein]):
        auftraege.append(("paket", [e for _rel, e in paket]))
    for eintrag in gross:
        auftraege.append(("datei", eintrag))
    kann_pakete = {"ja": paket_geht}

    def ausfuehren(auftrag: tuple) -> None:
        art, inhalt = auftrag
        if art == "paket":
            if not kann_pakete["ja"]:
                for eintrag in inhalt:                                 # type: ignore[union-attr]
                    _download_file(rid, folder, eintrag, fortschritt)
                return
            try:
                _download_paket(rid, folder, inhalt, fortschritt)       # type: ignore[arg-type]
            except CloudError as exc:
                if exc.status not in (404, 405, 501):
                    raise
                # Älterer Root-Server: er kennt die Paketroute nicht. Einmal merken und ab jetzt
                # wieder Datei für Datei holen.
                kann_pakete["ja"] = False
                for eintrag in inhalt:                                 # type: ignore[union-attr]
                    _download_file(rid, folder, eintrag, fortschritt)
            return
        _download_file(rid, folder, inhalt, fortschritt)                # type: ignore[arg-type]

    _parallel(auftraege, ausfuehren, PARALLEL, local_id)
    return bool(kann_pakete["ja"])


def _ablegen(folder: pathlib.Path, eintrag: dict, inhalt: bytes) -> None:
    """Eine fertige Datei an ihren Platz legen (über eine Teil-Datei, damit nichts halb dasteht)."""
    rel = str(eintrag.get("path") or "")
    ziel = folder / rel
    teil = ziel.with_name(ziel.name + PART_SUFFIX)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    teil.write_bytes(inhalt)
    os.replace(str(teil), str(ziel))
    if eintrag.get("mtime"):
        try:
            os.utime(str(ziel), (int(eintrag["mtime"]), int(eintrag["mtime"])))
        except OSError:
            pass


def _download_paket(rid: str, folder: pathlib.Path, eintraege: list[dict],
                    fortschritt: _Fortschritt) -> None:
    """Viele kleine Dateien in **einer** Anfrage holen (tar-Strom) und einzeln prüfen."""
    namen = [str(e.get("path") or "") for e in eintraege]
    index = {str(e.get("path") or ""): e for e in eintraege}
    daten = _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/download/bundle",
                 body={"paths": namen}, want_bytes=True, timeout=TRANSFER_TIMEOUT)
    if not daten:
        raise CloudError("Der Root-Server hat ein leeres Paket geschickt.")
    geholt = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(daten), mode="r|", encoding="utf-8") as archiv:
            for mitglied in archiv:
                if not mitglied.isfile():
                    continue
                eintrag = index.get(str(mitglied.name).replace("\\", "/"))
                if eintrag is None or int(mitglied.size) != int(eintrag.get("size") or 0):
                    continue                    # nicht angefordert oder inzwischen verändert
                quelle = archiv.extractfile(mitglied)
                inhalt = quelle.read(int(mitglied.size)) if quelle is not None else b""
                if len(inhalt) != int(eintrag["size"]):
                    continue
                if hashlib.sha256(inhalt).hexdigest() != str(eintrag.get("sha256") or "").lower():
                    raise CloudError(f"Die Prüfsumme von „{eintrag['path']}“ stimmt nicht – bitte "
                                     f"die Rückholung erneut starten.")
                _ablegen(folder, eintrag, inhalt)
                geholt += len(inhalt)
    except tarfile.TarError as exc:
        raise CloudError(f"Das Paket vom Root-Server lässt sich nicht auspacken: {exc}") from exc
    except OSError as exc:
        raise CloudError(f"Die Dateien aus dem Paket lassen sich auf dem PC nicht "
                         f"ablegen: {exc}") from exc
    fortschritt.dazu(geholt)


def _download_file(rid: str, folder: pathlib.Path, eintrag: dict,
                   fortschritt: _Fortschritt) -> None:
    """Eine einzelne Datei in Stücken holen, prüfen und ablegen (Wiederaufnahme über die Teil-Datei)."""
    rel = str(eintrag.get("path") or "")
    groesse = int(eintrag.get("size") or 0)
    ziel = folder / rel
    teil = ziel.with_name(ziel.name + PART_SUFFIX)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    versatz = teil.stat().st_size if teil.is_file() else 0
    if versatz > groesse:
        teil.unlink(missing_ok=True)
        versatz = 0
    if versatz:
        fortschritt.dazu(0)
    while versatz < groesse:
        block = _api("GET", f"/api/servers/{urllib.parse.quote(rid)}/download",
                     query={"path": rel, "offset": versatz,
                            "length": min(CHUNK_SIZE, groesse - versatz)},
                     want_bytes=True, timeout=TRANSFER_TIMEOUT)
        if not block:
            raise CloudError(f"Der Root-Server liefert für „{rel}“ keine Daten mehr.")
        with teil.open("ab") as fh:
            fh.write(block)
        versatz += len(block)
        fortschritt.dazu(len(block))
    if groesse == 0:
        teil.write_bytes(b"")
    if sha256_file(teil) != str(eintrag.get("sha256") or "").lower():
        teil.unlink(missing_ok=True)
        raise CloudError(f"Die Prüfsumme von „{rel}“ stimmt nicht – die Datei wird noch einmal "
                         f"geholt.")
    os.replace(str(teil), str(ziel))
    if eintrag.get("mtime"):
        try:
            os.utime(str(ziel), (int(eintrag["mtime"]), int(eintrag["mtime"])))
        except OSError:
            pass


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
    """Laufende Übertragung abbrechen – der Knopf des Benutzers.

    Zuerst wird der laufende Vorgang angehalten (er versucht es sonst unbegrenzt weiter), dann
    die Gegenseite verständigt. Beim **Hochladen** wird die Übertragung auf dem Root verworfen
    und der Server gilt wieder als „nur auf diesem PC“. Eine **Rückholung** wird nur angehalten:
    die Dateien auf dem Root bleiben, die schon geholten Teil-Dateien auch – der nächste Anlauf
    setzt dort auf.
    """
    cfg = store.get(local_id)
    if cfg is None:
        raise CloudError("Diesen Server gibt es auf diesem PC nicht.")
    _abbruch_event(local_id).set()
    link = link_of(cfg)
    rid, sid = str(link.get("instance") or ""), str(link.get("session") or "")
    zustand = str(link.get("state") or "")
    offen = _read_state().get("transfer") or {}
    if not (rid and sid):
        if zustand in ("awaiting_pull", "downloading") or str(offen.get("kind")) == "download":
            _transfer_merken(None)
            _drop_cache()
            return {"ok": True, "message": "Die Rückholung wurde angehalten. Die Dateien bleiben "
                                           "auf dem Root-Server; der nächste Anlauf setzt dort "
                                           "auf, wo dieser aufgehört hat."}
        raise CloudError("Für diesen Server läuft keine Übertragung.")
    _api("POST", f"/api/servers/{urllib.parse.quote(rid)}/upload/abort",
         body={"session": sid, "back_to_local": True})
    _set_link(cfg, session=None, state="local_only")
    _transfer_merken(None)
    _drop_cache()
    close_connections()
    return {"ok": True, "message": "Die Übertragung wurde abgebrochen."}


# --------------------------------------------------------------------------- Selbstheilung
#
# Eine Übertragung von 600 MB überlebt einen Neustart des Programms: was offen ist, steht in
# `data/cloud.json`. Kurz nach dem Start sieht dieser Faden nach und macht **von selbst** weiter –
# der Benutzer muss nichts anklicken. Dasselbe passiert, wenn ein Vorgang unterwegs aufgibt
# (z. B. weil der PC im Ruhezustand war): beim nächsten Blick läuft er wieder an.

RESUME_FIRST = 8.0               # so lange nach dem Programmstart wird gewartet
RESUME_EVERY = 30.0              # so oft wird danach nachgesehen
RESUME_MAX_WAIT = 300.0          # so weit wächst die Pause nach missglückten Anläufen

_resume = {"gestartet": False, "naechster": 0.0, "fehler": 0}


def pending_transfer() -> dict:
    """Die offene Übertragung aus ``data/cloud.json`` (leer, wenn keine offen ist)."""
    angabe = _read_state().get("transfer")
    return dict(angabe) if isinstance(angabe, dict) else {}


def resume_pending() -> dict:
    """Eine unterbrochene Übertragung fortsetzen. Gibt an, ob etwas angestoßen wurde."""
    angabe = pending_transfer()
    art = str(angabe.get("kind") or "")
    local_id = str(angabe.get("local") or "")
    rid = str(angabe.get("instance") or "")
    if art not in ("upload", "download") or not local_id:
        return {"resumed": False, "reason": "nichts offen"}
    if not logged_in():
        return {"resumed": False, "reason": "nicht angemeldet"}
    cfg = store.get(local_id)
    if cfg is None:
        _transfer_merken(None)                 # der Server ist weg – der Eintrag ist wertlos
        return {"resumed": False, "reason": "Server gibt es nicht mehr"}
    if _abbruch_event(local_id).is_set():
        return {"resumed": False, "reason": "abgebrochen"}
    if manager.job_running(local_id) or manager.is_running(local_id):
        return {"resumed": False, "reason": "läuft schon"}
    if art == "upload":
        job = upload_async(cfg)
    else:
        if not rid:
            _transfer_merken(None)
            return {"resumed": False, "reason": "keine Instanz vermerkt"}
        job = download_async(rid)
    log.info("Offene Übertragung wird von selbst fortgesetzt (%s, %s).", art, cfg.get("name"))
    return {"resumed": True, "kind": art, "job": job.get("id"), "server": local_id}


def _selbstheilung() -> None:
    """Hintergrundfaden: sieht regelmäßig nach, ob eine Übertragung liegengeblieben ist."""
    time.sleep(RESUME_FIRST)
    while True:
        try:
            if time.time() >= float(_resume["naechster"]):
                ergebnis = resume_pending()
                if ergebnis.get("resumed"):
                    _resume["fehler"] = 0
                    _resume["naechster"] = time.time() + RESUME_EVERY
                elif ergebnis.get("reason") in ("nichts offen", "läuft schon", "abgebrochen"):
                    _resume["fehler"] = 0
        except CloudError as exc:
            _resume["fehler"] = int(_resume["fehler"]) + 1
            pause = min(RESUME_MAX_WAIT, RESUME_EVERY * (2 ** min(4, int(_resume["fehler"]))))
            _resume["naechster"] = time.time() + pause
            log.info("Fortsetzen der Übertragung klappt noch nicht (%s) – erneut in %d s.",
                     exc, int(pause))
        except Exception:                      # noqa: BLE001 - dieser Faden darf nie sterben
            log.exception("Fehler beim Fortsetzen einer Übertragung")
            _resume["naechster"] = time.time() + RESUME_MAX_WAIT
        time.sleep(RESUME_EVERY)


def starte_selbstheilung() -> None:
    """Den Hintergrundfaden einmalig starten (wird beim Laden dieses Moduls aufgerufen)."""
    with _lock:
        if _resume["gestartet"] or os.environ.get("MCSM_NO_AUTORESUME"):
            return
        _resume["gestartet"] = True
    threading.Thread(target=_selbstheilung, daemon=True, name="mcsm-cloud-fortsetzen").start()


starte_selbstheilung()
