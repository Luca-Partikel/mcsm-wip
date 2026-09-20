#!/usr/bin/env python3
"""Minecraft Server Manager – lokale Weboberfläche zum Einrichten von
Minecraft-Servern (Bedrock/Xbox/PS5 und Java mit Crossplay) unter Windows.

Startet einen kleinen HTTP-Server auf 127.0.0.1 und öffnet die Oberfläche.
Es werden ausschließlich Module der Python-Standardbibliothek verwendet.
Läuft auch ohne Konsole (pythonw.exe); Meldungen landen in data/manager.log.
"""
from __future__ import annotations

import ctypes
import json
import logging
import mimetypes
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import manager, sources, store, tray, updater  # noqa: E402
from core.version import __version__  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
APP_NAME = "Minecraft Server Manager"
INSTANCE_FILE = store.DATA_DIR / "instance.json"
LOG_FILE = store.DATA_DIR / "manager.log"
HTTPD: ThreadingHTTPServer | None = None
TRAY: tray.Tray | None = None                         # Tray-Symbol, damit der Konsolen-Handler es entfernen kann
log = logging.getLogger("mcsm")

# Feste Typen für die eigenen Dateien: mimetypes liest unter Windows die Registry, wo .js auf
# manchen PCs als text/plain steht – mit "nosniff" würde der Browser app.js dann nicht ausführen.
STATIC_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
                ".webmanifest": "application/manifest+json"}


def setup_logging() -> None:
    store.ensure_dirs()
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
    except OSError:
        pass
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:                        # unter pythonw.exe gibt es keine Konsole
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


# --------------------------------------------------------------------- Hilfen

def _server_view(cfg: dict) -> dict:
    view = dict(cfg)
    view["running"] = manager.is_running(cfg["id"])
    view["installing"] = manager.job_running(cfg["id"])
    view["uptime"] = manager.uptime(cfg["id"])
    view["dir"] = str(store.server_dir(cfg["id"]))
    view["firewall_cmds"] = manager.firewall_command(cfg)
    view["ports"] = manager.port_list(cfg)
    view["xbox"] = manager.xbox_status(cfg)
    return view


def _system_info() -> dict:
    return {
        "local_ip": manager.local_ip(),
        "hostname": socket.gethostname(),
        "base_dir": str(BASE),
        "version": __version__,
        "update": updater.last(),
    }


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _require(cfg: dict | None) -> dict:
    if cfg is None:
        raise ApiError("Server nicht gefunden.", 404)
    return cfg


def _q(query: dict, key: str, default: str = "") -> str:
    return (query.get(key) or [default])[0]


def _no_job(server_id: str) -> None:
    """Während einer laufenden Installation nichts ändern, starten oder löschen."""
    if manager.job_running(server_id):
        raise ApiError("Die Einrichtung läuft noch – bitte warten, bis sie abgeschlossen ist.", 409)


# --------------------------------------------------------------------- Routen

def api_ping(_body, _query) -> dict:
    return {"ok": True, "app": APP_NAME, "pid": os.getpid(), "version": __version__}


def api_update(_body, query) -> dict:
    return updater.check(force=_q(query, "check") == "1")


def api_update_apply(_body, _query) -> dict:
    """Update laden, installieren und den Manager neu starten (Server werden sauber gestoppt)."""
    if any(manager.is_running(c["id"]) for c in store.all_servers()):
        raise ApiError("Bitte zuerst alle Server stoppen – beim Update wird der Manager neu gestartet.")
    try:
        job = updater.apply_async(on_done=lambda: request_shutdown(0.0))
    except sources.SourceError as exc:
        raise ApiError(str(exc)) from exc
    log.info("Update wird installiert …")
    return {"job_id": job["id"]}


def api_portmap(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    result = manager.request_port_mappings(cfg)
    log.info("UPnP-Portfreigabe für %s: %s", cfg["name"], "ok" if result["ok"] else "Fehler")
    return result


def api_portmap_remove(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    return manager.remove_port_mappings(cfg)


def api_bootstrap(_body, _query) -> dict:
    return {"servers": [_server_view(c) for c in store.all_servers()],
            "system": _system_info()}


def api_versions(_body, query) -> dict:
    kind = _q(query, "type", "bedrock")
    if kind == "java":
        return {"versions": sources.paper_versions()}
    return {"bedrock": sources.bedrock_versions()}


def _sanitize(body, existing=None) -> dict:
    """store.sanitize mit verständlicher Fehlermeldung (400 statt „Unerwarteter Fehler“)."""
    try:
        return store.sanitize(body, existing=existing)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def api_create(body, _query) -> dict:
    cfg = _sanitize(body)
    if not cfg.get("eula_accepted"):
        raise ApiError("Bitte zuerst die Minecraft-EULA bestätigen.")
    if not cfg["version"]:
        raise ApiError("Bitte eine Version auswählen.")
    store.save(cfg)
    job = manager.install_async(cfg)
    log.info("Server angelegt: %s (%s %s)", cfg["name"], cfg["type"], cfg["version"])
    return {"server": _server_view(cfg), "job_id": job["id"]}


def api_settings(body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    _no_job(server_id)
    if manager.is_running(server_id):
        raise ApiError("Bitte den Server zuerst stoppen, um Einstellungen zu ändern.")
    updated = _sanitize(body, existing=cfg)
    store.save(updated)
    manager.apply_config(updated)
    return {"server": _server_view(updated)}


def api_reinstall(body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    _no_job(server_id)
    if manager.is_running(server_id):
        raise ApiError("Bitte den Server zuerst stoppen.")
    updated = _sanitize(body, existing=cfg)
    store.save(updated)
    job = manager.install_async(updated)
    return {"job_id": job["id"]}


def api_start(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    _no_job(server_id)
    if not cfg.get("installed"):
        raise ApiError("Der Server ist noch nicht fertig installiert.")
    if manager.is_running(server_id):
        return {"ok": True}
    # Bedrock (NetherNet) und Java verbinden beide über TCP auf dem Hauptport; Geyser braucht UDP.
    if manager.port_in_use(cfg["port"], False) or manager.port_in_use(cfg["port"], False, ipv6=True):
        raise ApiError(f"Port {cfg['port']} (TCP) ist bereits belegt. Bitte einen anderen Port wählen.")
    if cfg["type"] == "java" and cfg.get("geyser") and manager.port_in_use(cfg["bedrock_port"], True):
        raise ApiError(f"Bedrock-Port {cfg['bedrock_port']} (UDP) ist bereits belegt.")
    if manager.needs_preparation(cfg):
        job = manager.prepare_and_start_async(cfg)
        log.info("Start mit Nachladen von Abhängigkeiten: %s", cfg["name"])
        return {"ok": True, "job_id": job["id"]}
    try:
        manager.instance(cfg).start()
    except RuntimeError as exc:
        raise ApiError(str(exc)) from exc
    manager.start_broadcaster_if_enabled(cfg)
    if cfg.get("auto_portmap"):
        threading.Thread(target=manager.request_port_mappings, args=(cfg,), daemon=True).start()
    log.info("Server gestartet: %s", cfg["name"])
    return {"ok": True}


def api_stop(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    threading.Thread(target=manager.instance(cfg).stop, daemon=True).start()
    threading.Thread(target=manager.stop_broadcaster, args=(cfg,), daemon=True).start()
    log.info("Server wird gestoppt: %s", cfg["name"])
    return {"ok": True}


# -- Xbox-Freunde-Modus

def api_xbox_status(_body, _query, server_id: str = "") -> dict:
    return manager.xbox_status(_require(store.get(server_id)))


def api_xbox_setup(body, _query, server_id: str = "") -> dict:
    """Vom Assistenten abgefragte Werte speichern, MCXboxBroadcast laden und den Bot starten."""
    cfg = _require(store.get(server_id))
    _no_job(server_id)
    raw = {"xbox_enabled": True}
    for key in ("xbox_address", "xbox_host_name", "xbox_autostart"):
        if key in body:
            raw[key] = body[key]
    updated = store.sanitize(raw, existing=cfg)
    store.save(updated)
    job = manager.xbox_setup_async(updated)
    log.info("Xbox-Freunde-Modus wird eingerichtet: %s", cfg["name"])
    return {"job_id": job["id"], "server": _server_view(updated)}


def api_xbox_start(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        manager.broadcaster(cfg).start()
    except RuntimeError as exc:
        raise ApiError(str(exc)) from exc
    return {"ok": True}


def api_xbox_stop(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    threading.Thread(target=manager.stop_broadcaster, args=(cfg,), daemon=True).start()
    return {"ok": True}


def api_xbox_disable(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    manager.stop_broadcaster(cfg)
    updated = store.sanitize({"xbox_enabled": False}, existing=cfg)
    store.save(updated)
    return {"server": _server_view(updated)}


def api_xbox_reset(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    manager.xbox_reset(cfg)
    return {"ok": True}


def api_xbox_console(_body, query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        since = int(_q(query, "since", "0"))
    except ValueError:
        since = 0
    data = manager.broadcaster(cfg).console(since)
    data["running"] = manager.broadcaster(cfg).running
    return data


def api_command(body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    command = str(body.get("command", "")).strip()
    if not command:
        raise ApiError("Kein Befehl angegeben.")
    try:
        manager.instance(cfg).send(command)
    except RuntimeError as exc:
        raise ApiError(str(exc)) from exc
    return {"ok": True}


def api_console(_body, query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        since = int(_q(query, "since", "0"))
        tail = int(_q(query, "tail")) if "tail" in query else None
    except ValueError:
        since, tail = 0, None
    data = manager.instance(cfg).console(since, tail)
    data["running"] = manager.is_running(server_id)
    data["uptime"] = manager.uptime(server_id)
    return data


def api_firewall(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        return {"message": manager.open_firewall(cfg)}
    except RuntimeError as exc:
        raise ApiError(str(exc)) from exc


def api_delete(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    _no_job(server_id)
    try:
        manager.delete_server(cfg)
    except RuntimeError as exc:
        raise ApiError(str(exc)) from exc
    log.info("Server gelöscht: %s", cfg["name"])
    return {"ok": True}


def api_files(_body, query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        return manager.list_dir(cfg, _q(query, "path"))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def api_file_get(_body, query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        return manager.read_file(cfg, _q(query, "path"))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def _props_editable(server_id: str) -> None:
    """server.properties wird wie die Einstellungen nur bei gestopptem Server geändert – sonst zeigten
    Übersicht, Firewall-Befehle und Xbox-Adresse Werte, die der laufende Server noch nicht kennt."""
    _no_job(server_id)
    if manager.is_running(server_id):
        raise ApiError("Bitte den Server zuerst stoppen, um server.properties zu ändern.")


def api_file_put(body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    path = str(body.get("path", ""))
    if path.replace("\\", "/").strip("/") == "server.properties":
        _props_editable(server_id)
    try:
        return manager.write_file(cfg, path, body.get("content"))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def api_props_get(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    return manager.read_props(cfg)


def api_props_put(body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    _props_editable(server_id)
    try:
        updated = manager.write_props(cfg, body.get("values"))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return {"server": _server_view(updated), **manager.read_props(updated)}


def api_worlds(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    return {"worlds": manager.worlds(cfg), "backups": manager.backups(cfg)}


def api_backup(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        return manager.backup_worlds(cfg)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def api_open_folder(body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    try:
        manager.open_in_explorer(cfg, str(body.get("path", "")))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return {"ok": True}


def api_job(_body, _query, job_id: str = "") -> dict:
    job = manager.get_job(job_id)
    if job is None:
        raise ApiError("Vorgang nicht gefunden.", 404)
    return job


def api_public_ip(_body, query) -> dict:
    """Öffentliche IP abfragen. Zuerst der eigene Router (UPnP, ohne Fremddienst); api.ipify.org
    nur auf ausdrücklichen Klick (ohne ?auto=1) – das Dashboard fragt nur den Router."""
    ip = manager.fritz_external_ip()
    if ip:
        return {"ip": ip, "source": "router"}
    if _q(query, "auto") == "1":
        raise ApiError("Der Router hat keine öffentliche IPv4 gemeldet (UPnP aus oder DS-Lite).")
    try:
        req = urllib.request.Request("https://api.ipify.org?format=json",
                                     headers={"User-Agent": sources.UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return {"ip": json.loads(resp.read().decode("utf-8")).get("ip", "")}
    except Exception as exc:                          # noqa: BLE001
        raise ApiError(f"Öffentliche IP konnte nicht ermittelt werden: {exc}") from exc


def request_shutdown(delay: float = 0.0) -> None:
    """Stoppt alle Server und beendet den HTTP-Server (GUI-Knopf, Tray-Menü)."""
    def work() -> None:
        if delay:
            time.sleep(delay)
        log.info("Manager wird beendet – Server werden gestoppt …")
        manager.stop_all()
        if HTTPD is not None:
            HTTPD.shutdown()

    threading.Thread(target=work, daemon=True).start()


def api_shutdown(_body, _query) -> dict:
    """Stoppt alle Server und beendet den Manager (Knopf „Manager beenden“)."""
    request_shutdown(0.4)
    return {"ok": True}


# --------------------------------------------------------------------- Tray-Symbol

def start_tray(url: str):
    """Symbol neben der Uhr: Klick öffnet die Oberfläche, Menü stoppt/beendet. Nie Pflicht."""
    if not tray.available or os.environ.get("MCSM_NO_TRAY"):
        return None
    try:
        icon = tray.Tray(BASE / "app.ico", APP_NAME, on_open=lambda: open_ui(url),
                         on_stop_all=manager.stop_all, on_quit=request_shutdown)
        if not icon.start():
            return None
    except Exception:                                 # noqa: BLE001
        log.exception("Tray-Symbol nicht verfügbar")
        return None

    def tips() -> None:
        last = None
        while icon.ok:
            n = sum(1 for c in store.all_servers() if manager.is_running(c["id"]))
            text = f"{APP_NAME}\n" + ("Kein Server läuft" if n == 0 else "1 Server läuft" if n == 1 else f"{n} Server laufen")
            if text != last:
                icon.set_tip(text)
                last = text
            time.sleep(5)

    threading.Thread(target=tips, daemon=True).start()
    return icon


ROUTES = {
    ("GET", "ping"): api_ping,
    ("GET", "bootstrap"): api_bootstrap,
    ("GET", "versions"): api_versions,
    ("GET", "publicip"): api_public_ip,
    ("POST", "servers"): api_create,
    ("POST", "shutdown"): api_shutdown,
    ("GET", "update"): api_update,
    ("POST", "update/apply"): api_update_apply,
}
# Hinweis: Routen mit "/" im Namen (update/apply) laufen über den allgemeinen Verteiler unten.

SERVER_ROUTES = {
    ("POST", "settings"): api_settings,
    ("POST", "reinstall"): api_reinstall,
    ("POST", "start"): api_start,
    ("POST", "stop"): api_stop,
    ("POST", "command"): api_command,
    ("GET", "console"): api_console,
    ("POST", "firewall"): api_firewall,
    ("POST", "folder"): api_open_folder,
    ("GET", "files"): api_files,
    ("GET", "file"): api_file_get,
    ("POST", "file"): api_file_put,
    ("GET", "props"): api_props_get,
    ("POST", "props"): api_props_put,
    ("GET", "worlds"): api_worlds,
    ("POST", "backup"): api_backup,
    ("GET", "xbox"): api_xbox_status,
    ("POST", "xbox/setup"): api_xbox_setup,
    ("POST", "xbox/start"): api_xbox_start,
    ("POST", "xbox/stop"): api_xbox_stop,
    ("POST", "xbox/disable"): api_xbox_disable,
    ("POST", "portmap"): api_portmap,
    ("POST", "portmap/remove"): api_portmap_remove,
    ("POST", "xbox/reset"): api_xbox_reset,
    ("GET", "xbox/console"): api_xbox_console,
    ("DELETE", ""): api_delete,
}


# --------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "MCServerManager"

    def log_message(self, fmt, *args):                # Konsole ruhig halten
        pass

    # -- Antworten
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # -- Sicherheit: nur lokale Aufrufe mit gültigem Token
    def _authorized(self, query: dict) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            return False
        token = self.headers.get("X-Token") or _q(query, "t")
        return secrets.compare_digest(token, TOKEN)

    # -- Verteiler
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path.startswith("/api/"):
            if not self._authorized(query):
                self._json(403, {"error": "Nicht autorisiert."})
                return
            self._handle_api(method, path[5:], query)
            return

        if method != "GET":
            self._json(405, {"error": "Methode nicht erlaubt."})
            return
        self._serve_static(path)

    def _handle_api(self, method: str, route: str, query: dict) -> None:
        body: dict = {}
        if method in ("POST", "DELETE"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 4_000_000:
                self._json(413, {"error": "Anfrage zu groß."})
                return
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    self._json(400, {"error": "Ungültige Daten."})
                    return
            if not isinstance(body, dict):
                body = {}

        parts = [p for p in route.split("/") if p]
        try:
            if len(parts) >= 2 and parts[0] == "servers":
                action = "/".join(parts[2:])
                handler = SERVER_ROUTES.get((method, action))
                if handler is None:
                    raise ApiError("Unbekannter Aufruf.", 404)
                self._json(200, handler(body, query, parts[1]))
                return
            if len(parts) == 2 and parts[0] == "job":
                self._json(200, api_job(body, query, parts[1]))
                return
            handler = ROUTES.get((method, "/".join(parts))) or ROUTES.get((method, parts[0] if parts else ""))
            if handler is None:
                raise ApiError("Unbekannter Aufruf.", 404)
            self._json(200, handler(body, query))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except sources.SourceError as exc:
            self._json(502, {"error": str(exc)})
        except Exception as exc:                      # noqa: BLE001
            log.exception("API-Fehler bei %s %s", method, route)
            self._json(500, {"error": f"Unerwarteter Fehler: {exc}"})

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if ".." in rel.replace("\\", "/").split("/"):
            self._send(403, b"Forbidden", "text/plain")
            return
        target = (store.WEB_DIR / rel).resolve()
        try:
            target.relative_to(store.WEB_DIR.resolve())
        except ValueError:
            self._send(403, b"Forbidden", "text/plain")
            return
        if not target.is_file():
            self._send(404, b"Not found", "text/plain")
            return
        ctype = (STATIC_TYPES.get(target.suffix.lower())
                 or mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        if ctype.startswith(("text/", "application/javascript")):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


# --------------------------------------------------------------------- Start

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_ui(url: str) -> None:
    """Öffnet die Oberfläche möglichst als eigenständiges App-Fenster."""
    candidates = [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ]
    for raw in candidates:
        exe = pathlib.Path(os.path.expandvars(raw))
        if exe.is_file():
            try:
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=1400,940"])
                return
            except OSError:
                pass
    webbrowser.open(url)


def running_instance() -> str | None:
    """URL eines bereits laufenden Managers, falls es einen gibt."""
    try:
        data = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        port, token = int(data["port"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-Token": token})
        with urllib.request.urlopen(req, timeout=2) as resp:
            if json.loads(resp.read().decode("utf-8")).get("app") == APP_NAME:
                return f"http://127.0.0.1:{port}/index.html?t={token}"
    except Exception:                                 # noqa: BLE001 - alter Eintrag
        pass
    return None


if sys.platform == "win32":
    import ctypes.wintypes as wt
    _HandlerRoutine = ctypes.WINFUNCTYPE(wt.BOOL, wt.DWORD)

    @_HandlerRoutine
    def _on_console_ctrl(ctrl_type):
        # 2 = Fenster geschlossen, 5 = Abmelden, 6 = Herunterfahren: Windows gewährt ~5 s, danach
        # wird der Prozess beendet – was dann noch läuft, beendet das Job-Objekt (core/manager.py).
        # Ohne diesen Handler würde Python sofort beendet und der finally-Block liefe nie.
        if ctrl_type in (2, 5, 6):
            if TRAY:                                  # sonst bleibt ein „Geister-Symbol“ neben der Uhr zurück
                TRAY.stop(0.5)
            manager.emergency_stop_all(4.0)
            return True
        return False          # Strg+C / Strg+Untbr: normal an Python weiterreichen (KeyboardInterrupt -> finally)


def main() -> int:
    global HTTPD, TRAY
    setup_logging()

    existing = running_instance()
    if existing:
        log.info("Manager läuft bereits – Fenster wird geöffnet.")
        if not os.environ.get("MCSM_NO_BROWSER"):
            open_ui(existing)
        return 0

    port = int(os.environ.get("MCSM_PORT") or 0) or free_port()
    HTTPD = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    HTTPD.daemon_threads = True
    url = f"http://127.0.0.1:{port}/index.html?t={TOKEN}"
    INSTANCE_FILE.write_text(json.dumps({"port": port, "token": TOKEN, "pid": os.getpid()}), encoding="utf-8")

    log.info("%s gestartet", APP_NAME)
    log.info("Oberfläche : %s", url)
    log.info("Ordner     : %s", BASE)
    if sys.stdout is not None:
        print("Zum Beenden: Knopf „Manager beenden“ in der Oberfläche oder Strg+C.", flush=True)

    tray_icon = TRAY = start_tray(url)

    def update_checks() -> None:                      # kurz nach dem Start, danach alle 6 Stunden
        time.sleep(6)
        while True:
            try:
                info = updater.check(force=True)
                if info.get("available"):
                    log.info("Update verfügbar: %s (installiert: %s)", info["latest"], info["current"])
                    if tray_icon:
                        tray_icon.notify(APP_NAME, f"Update auf Version {info['latest']} verfügbar – "
                                                   "in der Oberfläche unten links installieren.")
            except Exception:                         # noqa: BLE001
                pass
            time.sleep(6 * 3600)

    threading.Thread(target=update_checks, daemon=True).start()
    if not os.environ.get("MCSM_NO_BROWSER"):
        threading.Timer(0.6, open_ui, args=(url,)).start()
        hint = store.DATA_DIR / ".tray-hint-shown"
        if tray_icon and not hint.exists():
            hint.write_text("1", encoding="utf-8")
            threading.Timer(3.0, tray_icon.notify, args=(
                APP_NAME, "Läuft im Hintergrund weiter, wenn du das Fenster schließt. "
                          "Doppelklick auf dieses Symbol öffnet die Oberfläche wieder.")).start()
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_on_console_ctrl, True)
    try:
        HTTPD.serve_forever()
    except KeyboardInterrupt:
        log.info("Strg+C – Server werden gestoppt …")
    finally:
        manager.stop_all()
        HTTPD.server_close()
        INSTANCE_FILE.unlink(missing_ok=True)
        if tray_icon:
            tray_icon.stop()
        log.info("Manager beendet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
