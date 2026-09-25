#!/usr/bin/env python3
"""Minecraft Server Manager – lokale Weboberfläche zum Einrichten von
Minecraft-Servern (Bedrock/Xbox/PS5 und Java mit Crossplay) unter Windows.

Startet einen kleinen HTTP-Server auf 127.0.0.1 und öffnet die Oberfläche.
Es werden ausschließlich Module der Python-Standardbibliothek verwendet.
Läuft auch ohne Konsole (pythonw.exe); Meldungen landen in data/manager.log.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import json
import logging
import mimetypes
import os
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import cloud, companion, manager, players, sources, store, tray, updater  # noqa: E402
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
    view["companion"] = companion.status(cfg)
    # Cloud: Verknüpfung mit dem Root-Server. Rein lokal aus servers.json – kein Netzverkehr,
    # damit die Übersicht auch ohne Internet sofort da ist.
    view["cloud"] = cloud.link_of(cfg)
    view["cloud_locked"] = cloud.locked(cfg)
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


def _not_hosted(cfg: dict) -> None:
    """Liegt der Server auf dem Root-Server, ist die lokale Kopie gesperrt (nur an einer Stelle spielbar)."""
    if cloud.locked(cfg):
        raise ApiError(cloud.lock_hint(cfg), 409)


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


def api_modpack_search(_body, query) -> dict:
    """Modpacks auf Modrinth suchen: GET /api/modpacks/search?q=…&page=0 → {hits:[…], total, page}."""
    try:
        page = int(_q(query, "page", "0"))
    except ValueError:
        page = 0
    return sources.modrinth_search(_q(query, "q"), page)


def api_modpack_versions(_body, _query, project_id: str = "") -> list:
    """Serverfähige .mrpack-Versionen eines Modpacks: GET /api/modpacks/<project_id>/versions."""
    return sources.modrinth_versions(project_id)


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
    if body.get("modpack") and not store.is_modpack(cfg):
        raise ApiError("Bitte ein Modpack und eine Pack-Version auswählen.")
    if not cfg["version"]:
        raise ApiError("Bitte eine Version auswählen.")
    store.save(cfg)
    job = manager.install_async(cfg)
    log.info("Server angelegt: %s (%s %s %s)", cfg["name"], cfg["type"], cfg.get("flavor", "paper"), cfg["version"])
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
    _not_hosted(cfg)
    if manager.is_running(server_id):
        raise ApiError("Bitte den Server zuerst stoppen.")
    updated = _sanitize(body, existing=cfg)
    store.save(updated)
    job = manager.install_async(updated)
    return {"job_id": job["id"]}


def api_start(_body, _query, server_id: str = "") -> dict:
    cfg = _require(store.get(server_id))
    _no_job(server_id)
    _not_hosted(cfg)
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
    # Portfreigabe immer still anfordern (FritzBox UPnP) – ohne Freigabe oder bei DS-Lite bleibt es beim Hinweis im Log.
    threading.Thread(target=manager.request_port_mappings, args=(cfg,), daemon=True).start()
    log.info("Server gestartet: %s", cfg["name"])
    return {"ok": True}


def api_stop(_body, _query, server_id: str = "") -> dict:
    """Knopf „Stoppen“: auf Paper-Servern kündigt das Begleit-Plugin den Stopp erst im Spiel an."""
    cfg = _require(store.get(server_id))
    threading.Thread(target=manager.instance(cfg).stop, kwargs={"announce": True}, daemon=True).start()
    threading.Thread(target=manager.stop_broadcaster, args=(cfg,), daemon=True).start()
    log.info("Server wird gestoppt: %s", cfg["name"])
    return {"ok": True}


def api_hardcore(body, _query, server_id: str = "") -> dict:
    """MCSM-Hardcore des Begleit-Plugins schalten – läuft der Server, sofort per Konsolenbefehl."""
    cfg = _require(store.get(server_id))
    if not companion.applies(cfg):
        raise ApiError("Der Hardcore-Modus gibt es nur auf Java-Servern mit Paper (Begleit-Plugin).")
    enabled = bool(body.get("enabled"))
    updated = store.sanitize({"hardcore": enabled}, existing=cfg)
    store.save(updated)
    if manager.is_running(server_id):
        try:
            manager.instance(updated).send("hardcore on" if enabled else "hardcore off")
        except RuntimeError as exc:
            raise ApiError(str(exc)) from exc
    elif (store.server_dir(server_id) / "plugins").exists():
        companion.write_config(updated)
    log.info("Hardcore %s: %s", "an" if enabled else "aus", cfg["name"])
    return {"server": _server_view(updated)}


# -- Spielerverwaltung (Freigabeliste, Sperrliste, Online-Spieler)

def _players_cfg(server_id: str) -> dict:
    cfg = _require(store.get(server_id))
    if not players.applies(cfg):
        raise ApiError("Die Spielerverwaltung gibt es nur für Bedrock-Server und für Java-Server mit "
                       "Paper – Modpack-Server haben keine passende Liste.", 404)
    return cfg


def api_players(_body, _query, server_id: str = "") -> dict:
    return players.overview(_players_cfg(server_id))


def api_players_action(body, _query, server_id: str = "") -> dict:
    """{"action": "whitelist_on|whitelist_off|whitelist_add|whitelist_remove|ban|unban|kick", "name", "reason", "duration"}."""
    cfg = _players_cfg(server_id)
    try:
        return players.apply_action(cfg, str(body.get("action", "")), body.get("name", ""),
                                    body.get("reason", ""), body.get("duration", ""))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


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
    _not_hosted(cfg)
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


# -- Cloud (Root-Server): Anmeldung, Konto, entfernte Server, Übertragungen
# Die eigentliche Arbeit steckt in core/cloud.py. Fehler von dort (cloud.CloudError) wandelt der
# Verteiler unten in eine Antwort mit deutschem Satz – 401 führt zusätzlich zum Abmelden.

def _cloud_instance(body: dict, query: dict) -> str:
    """Kennung einer Instanz auf dem Root-Server (aus Rumpf oder Abfrage)."""
    value = str(body.get("instance") or _q(query, "instance")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value or ""):
        raise ApiError("Es fehlt die Kennung des Servers auf dem Root-Server.")
    return value


def api_cloud_status(_body, query) -> dict:
    return cloud.status(force=_q(query, "force") == "1")


def api_cloud_login(_body, _query) -> dict:
    """Discord-Anmeldung beginnen – die Oberfläche öffnet die gelieferte Adresse im Browser."""
    return cloud.login_start(socket.gethostname())


def api_cloud_login_poll(_body, _query) -> dict:
    return cloud.login_poll()


def api_cloud_login_register(body, _query) -> dict:
    """Zweiter Schritt der Erstanmeldung: Einladungscode zum vorgemerkten Discord-Konto."""
    return cloud.login_register(str(body.get("code", "")), str(body.get("note", "")))


def api_cloud_login_abort(_body, _query) -> dict:
    return cloud.login_abort()


def api_cloud_login_invite(body, _query) -> dict:
    """Ersatzweg mit Einladungscode, solange Discord auf dem Root-Server nicht eingerichtet ist."""
    return cloud.login_invite(str(body.get("code", "")), str(body.get("name", "")))


def api_cloud_logout(_body, _query) -> dict:
    log.info("Abmeldung vom Root-Server.")
    return cloud.logout()


# -- Konto-Einstellungen (Anzeigename, Discord-Verknüpfung, Sitzungen)
# Die letzten drei Wege gibt es auf dem Root-Server vielleicht noch nicht; core/cloud.py meldet
# das als {"supported": false, "hint": …} statt als Fehler, damit der Dialog stehen bleibt.

def api_cloud_sessions(_body, _query) -> dict:
    return cloud.sessions()


def api_cloud_account_name(body, _query) -> dict:
    return cloud.set_name(str(body.get("name", "")))


def api_cloud_session_revoke(body, _query) -> dict:
    return cloud.revoke_session(body.get("created_at", 0), str(body.get("note", "")))


def api_cloud_link_discord(body, _query) -> dict:
    """Adresse zum nachträglichen Verknüpfen – die Oberfläche öffnet sie im Browser."""
    return cloud.discord_link_start(str(body.get("ziel", "")))


def api_cloud_servers(_body, query) -> dict:
    force = _q(query, "force") == "1"
    servers = [cloud.remote_view(r) for r in cloud.remote_servers(force)]
    cloud.sync_links()
    return {"servers": servers}


def api_cloud_start(body, query) -> dict:
    return cloud.remote_start(_cloud_instance(body, query))


def api_cloud_stop(body, query) -> dict:
    try:
        announce = max(0, min(900, int(body.get("announce_seconds") or 0)))
    except (TypeError, ValueError):
        announce = 0
    return cloud.remote_stop(_cloud_instance(body, query), announce)


def api_cloud_command(body, query) -> dict:
    return cloud.remote_command(_cloud_instance(body, query), str(body.get("command", "")))


def api_cloud_console(_body, query) -> dict:
    try:
        since = int(_q(query, "since", "0"))
        tail = int(_q(query, "tail", "0"))
    except ValueError:
        since, tail = 0, 0
    return cloud.remote_console(_cloud_instance({}, query), since, tail)


def api_cloud_files(_body, query) -> dict:
    return cloud.remote_files(_cloud_instance({}, query), _q(query, "path"))


def api_cloud_file_get(_body, query) -> dict:
    return cloud.remote_file_read(_cloud_instance({}, query), _q(query, "path"))


def api_cloud_file_put(body, query) -> dict:
    content = body.get("content", body.get("text"))
    if not isinstance(content, str):
        raise ApiError("Es fehlt der Text, der gespeichert werden soll.")
    return cloud.remote_file_write(_cloud_instance(body, query), str(body.get("path", "")), content)


def api_cloud_file_delete(body, query) -> dict:
    """Eine Datei oder einen Ordner auf dem Root-Server löschen (z. B. ein Plugin)."""
    return cloud.remote_file_delete(_cloud_instance(body, query),
                                    str(body.get("path", _q(query, "path"))))


def api_cloud_file_mkdir(body, query) -> dict:
    return cloud.remote_file_mkdir(_cloud_instance(body, query), str(body.get("path", "")))


def api_cloud_file_rename(body, query) -> dict:
    return cloud.remote_file_rename(_cloud_instance(body, query), str(body.get("path", "")),
                                    str(body.get("new_path", body.get("neu", ""))))


#: So groß darf der Körper beim Hochladen einer einzelnen Datei werden (Base64 wiegt +33 %).
UPLOAD_BODY_MAX = 48 * 1024 * 1024
UPLOAD_FILE_MAX = 32 * 1024 * 1024


def api_cloud_file_upload(body, query) -> dict:
    """Eine einzelne Datei auf den Root-Server legen – z. B. ein Plugin.

    Zwei Wege: ``local`` ist ein Pfad auf diesem PC (so ruft es ein Skript auf), ``inhalt`` ist
    der Inhalt als Base64 (so ruft es die Oberfläche auf – der Browser gibt keinen Pfad heraus).
    """
    ziel = str(body.get("path", ""))
    quelle = str(body.get("local", body.get("datei", "")))
    inhalt = body.get("inhalt", body.get("content_b64"))
    if isinstance(inhalt, str) and inhalt:
        name = pathlib.Path(str(body.get("name", "") or ziel or "datei")).name
        if not name:
            raise ApiError("Es fehlt der Name der Datei.")
        try:
            roh = base64.b64decode(inhalt, validate=True)
        except (ValueError, binascii.Error):
            raise ApiError("Der Inhalt der Datei ist unlesbar.") from None
        if len(roh) > UPLOAD_FILE_MAX:
            raise ApiError(f"Die Datei ist zu groß (höchstens "
                           f"{UPLOAD_FILE_MAX // (1024 * 1024)} MB auf diesem Weg).")
        ablage = store.DATA_DIR / "cloud-stage"
        ablage.mkdir(parents=True, exist_ok=True)
        tmp = ablage / f"{secrets.token_hex(8)}-{name}"
        try:
            tmp.write_bytes(roh)
            return cloud.remote_file_upload(_cloud_instance(body, query), tmp, ziel or name)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    if not quelle:
        raise ApiError("Es fehlt die Datei, die hochgeladen werden soll.")
    return cloud.remote_file_upload(_cloud_instance(body, query), quelle, ziel)


def api_cloud_file_download(body, query) -> dict:
    """Eine einzelne Datei vom Root-Server holen – z. B. world/level.dat."""
    pfad = str(body.get("path", _q(query, "path")))
    if not pfad:
        raise ApiError("Es fehlt der Pfad der Datei, die geholt werden soll.")
    ziel = str(body.get("local", "")) or None
    return cloud.remote_file_download(_cloud_instance(body, query), pfad, ziel)


# -- Eine gehostete Instanz ganz normal bedienen (Übersicht, Einstellungen, Spieler)
# Die Oberfläche zeigt für einen Server auf dem Root-Server dieselbe Seite mit denselben Reitern
# wie für einen Server auf diesem PC – nur arbeiten sie gegen den Root. Starten, Stoppen, Konsole
# und Dateien gibt es dafür schon oben; hier stehen die drei Gegenstücke, die noch fehlten:
# der Zustand einer einzelnen Instanz, ihre Einstellungen (Ruhezustand, Arbeitsspeicher, Name),
# server.properties als Formular und die Spielerverwaltung über die Konsole des Root-Servers.
# Gesprochen wird weiter ausschließlich über core/cloud.py.

#: Antworten des Root-Servers, die „diesen Weg gibt es hier (noch) nicht“ bedeuten.
CLOUD_MISSING = (404, 405, 501)
#: Ruhezustand bei Leerstand – dieselben Standardwerte wie auf dem Root-Server.
HIBERNATION_DEFAULT = True
HIBERNATION_MINUTES_DEFAULT = 15
HIBERNATION_MINUTES_MIN = 5
HIBERNATION_MINUTES_MAX = 1440
#: So lange auf die Antwort des Konsolenbefehls „list“ vom Root-Server warten.
HOSTED_LIST_WAIT = 4.0
#: Kurz warten, bis der laufende Server seine Listen-Dateien neu geschrieben hat.
HOSTED_SETTLE = 1.0
#: Änderungen, die die Spielerverwaltung einer gehosteten Instanz kennt (wie lokal).
HOSTED_PLAYER_ACTIONS = ("whitelist_on", "whitelist_off", "whitelist_add", "whitelist_remove",
                         "ban", "unban", "kick")


def _cloud_api(method: str, path: str, *, body=None, query: dict | None = None) -> dict:
    """Aufruf an den Root-Server für Wege, die core/cloud.py noch nicht als Funktion anbietet.

    Bietet core/cloud.py dafür etwas Öffentliches an (``api_call``), wird das genommen – hier soll
    keine zweite Stelle mit Token, Zeitgrenzen und Zertifikaten entstehen.
    """
    call = getattr(cloud, "api_call", None) or getattr(cloud, "_api", None)
    if not callable(call):
        raise ApiError("Dieser Weg zum Root-Server steht in diesem Programm nicht zur Verfügung.", 501)
    return call(method, path, body=body, query=query)


def _cloud_frisch() -> None:
    """Zwischenspeicher von core/cloud.py verwerfen, damit die Oberfläche sofort das Neue sieht."""
    drop = getattr(cloud, "drop_cache", None) or getattr(cloud, "_drop_cache", None)
    if callable(drop):
        drop()


def _cloud_route(remote_id: str, rest: str) -> str:
    """Weg einer Instanz auf dem Root-Server, z. B. /api/servers/<id>/settings."""
    return f"/api/servers/{quote(remote_id, safe='')}{rest}"


def _zahl(value, default: int = 0) -> int:
    """Ganze Zahl aus einer Angabe des Root-Servers – fehlt sie oder passt sie nicht, der Standard."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _hosted_remote(remote_id: str) -> dict:
    """Datensatz einer Instanz vom Root-Server – frisch, ohne Zwischenspeicher."""
    remote = cloud.remote_detail(remote_id)
    if not remote:
        raise ApiError("Diesen Server gibt es auf dem Root-Server nicht (mehr).", 404)
    return remote


def _hosted_kind(remote: dict) -> str:
    """Art des Servers, wie core/manager.py und core/players.py sie kennen."""
    return "bedrock" if str(remote.get("type") or "") == "bedrock" else "java"


def _hosted_sleeping(remote: dict) -> bool:
    """Schläft die Instanz? Das sagt der Root-Server selbst („sleeping“).

    Fehlt die Angabe, wird sie nur dann abgeleitet, wenn der Root-Server den Ruhezustand
    überhaupt kennt – sonst sähe jeder gestoppte Server aus wie ein schlafender.
    """
    if "sleeping" in remote:
        return bool(remote.get("sleeping"))
    if "hibernation" not in remote:
        return False
    return (str(remote.get("state") or "") == "hosted" and not remote.get("running")
            and bool(remote.get("hibernation")))


def _hosted_settings(remote: dict) -> dict:
    """Einstellungen einer gehosteten Instanz für das Formular."""
    schlaf = remote.get("hibernation")
    minuten = remote.get("hibernation_minutes")
    return {
        "name": str(remote.get("name") or ""),
        "type": _hosted_kind(remote),
        "version": str(remote.get("version") or ""),
        "ram_mb": _zahl(remote.get("ram_mb")),
        "hibernation": HIBERNATION_DEFAULT if schlaf is None else bool(schlaf),
        "hibernation_minutes": (HIBERNATION_MINUTES_DEFAULT if minuten is None
                                else _zahl(minuten, HIBERNATION_MINUTES_DEFAULT)),
        # Kennt der Root-Server den Ruhezustand noch nicht, sagt die Oberfläche das ruhig statt
        # Werte anzuzeigen, die dort niemand liest.
        "hibernation_known": schlaf is not None or minuten is not None,
        "sleeping": _hosted_sleeping(remote),
        "minutes_min": HIBERNATION_MINUTES_MIN,
        "minutes_max": HIBERNATION_MINUTES_MAX,
    }


def api_cloud_instance(_body, query) -> dict:
    """Ein einzelner Server auf dem Root-Server: Zustand, Spielerzahl, Adresse, Einstellungen."""
    remote = _hosted_remote(_cloud_instance({}, query))
    return {"server": remote, "settings": _hosted_settings(remote)}


def api_cloud_settings(_body, query) -> dict:
    """Einstellungen einer gehosteten Instanz lesen (Name, Arbeitsspeicher, Ruhezustand)."""
    remote = _hosted_remote(_cloud_instance({}, query))
    return {"server": remote, "settings": _hosted_settings(remote)}


def api_cloud_settings_put(body, query) -> dict:
    """Einstellungen einer gehosteten Instanz ändern (Name, Arbeitsspeicher, Ruhezustand)."""
    remote_id = _cloud_instance(body, query)
    payload: dict = {}
    if "name" in body:
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()
        if not 2 <= len(name) <= 32 or any(ch < " " for ch in name):
            raise ApiError("Bitte einen Namen mit 2 bis 32 Zeichen eintragen.")
        payload["name"] = name
    if "ram_mb" in body:
        ram = _zahl(body.get("ram_mb"))
        if not 512 <= ram <= 32768:
            raise ApiError("Der Arbeitsspeicher muss zwischen 512 MB und 32 GB liegen.")
        payload["ram_mb"] = ram
    if "hibernation" in body:
        payload["hibernation"] = bool(body.get("hibernation"))
    if "hibernation_minutes" in body:
        minuten = _zahl(body.get("hibernation_minutes"))
        if not HIBERNATION_MINUTES_MIN <= minuten <= HIBERNATION_MINUTES_MAX:
            raise ApiError(f"Die Wartezeit des Ruhezustands muss zwischen {HIBERNATION_MINUTES_MIN} "
                           f"und {HIBERNATION_MINUTES_MAX} Minuten liegen.")
        payload["hibernation_minutes"] = minuten
    if not payload:
        raise ApiError("Es wurde keine Einstellung übergeben.")
    try:
        data = _cloud_api("POST", _cloud_route(remote_id, "/settings"), body=payload)
    except cloud.CloudError as exc:
        if exc.status in CLOUD_MISSING:
            return {"supported": False,
                    "hint": "Dieser Root-Server kann die Einstellungen einer gehosteten Instanz "
                            "noch nicht ändern. Bitte den Betreiber darum bitten."}
        raise
    _cloud_frisch()
    roh = data.get("server") or {}
    view = cloud.remote_view(roh) if roh else _hosted_remote(remote_id)
    # Was der Root-Server nicht kennt, fehlt in seiner Antwort – die Oberfläche sagt es ruhig.
    offen = [key for key in ("hibernation", "hibernation_minutes") if key in payload and key not in roh]
    return {"ok": True, "server": view, "settings": _hosted_settings(view), "ignored": offen}


# -- server.properties einer gehosteten Instanz (dasselbe Formular wie lokal)

def _properties_from_text(text: str) -> list[tuple[str, str]]:
    """server.properties aus Text lesen – dieselben Regeln wie manager.read_properties."""
    out: list[tuple[str, str]] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key, value = s.split("=", 1)
            out.append((key.strip(), value.strip()))
    return out


def _properties_patch(text: str, values: dict) -> str:
    """Werte ersetzen und alles andere (Kommentare, unbekannte Zeilen) unverändert stehen lassen."""
    rest = dict(values)
    out = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in rest:
                out.append(f"{key}={rest.pop(key)}")
                continue
        out.append(line)
    out.extend(f"{key}={value}" for key, value in rest.items())
    return "\n".join(out).rstrip("\n") + "\n"


def _props_fields(pairs: list[tuple[str, str]], kind: str) -> list[dict]:
    """Beschriebene Felder für das Formular – dieselbe Tabelle wie bei lokalen Servern."""
    meta = manager.PROPS_META.get(kind, {})
    fields = []
    for key, value in pairs:
        known = meta.get(key)
        if known:
            fields.append({"key": key, "value": value, "known": True, **known})
        else:
            art = ("bool" if value in ("true", "false")
                   else "int" if re.fullmatch(r"-?\d+", value) else "text")
            fields.append({"key": key, "value": value, "known": False,
                           "label": key, "desc": "", "type": art})
    return fields


def _hosted_read_text(remote_id: str, path: str) -> str | None:
    """Textdatei vom Root-Server – None, wenn es sie dort nicht gibt."""
    try:
        return str(cloud.remote_file_read(remote_id, path).get("text") or "")
    except cloud.CloudError as exc:
        if exc.status in (400, 404):
            return None
        raise


def api_cloud_props(_body, query) -> dict:
    """server.properties einer gehosteten Instanz als Formular."""
    remote_id = _cloud_instance({}, query)
    remote = _hosted_remote(remote_id)
    text = _hosted_read_text(remote_id, "server.properties")
    return {"exists": text is not None, "running": bool(remote.get("running")),
            "fields": _props_fields(_properties_from_text(text or ""), _hosted_kind(remote))}


def api_cloud_props_put(body, query) -> dict:
    """server.properties einer gehosteten Instanz speichern (nur bei gestopptem Server)."""
    remote_id = _cloud_instance(body, query)
    remote = _hosted_remote(remote_id)
    if remote.get("running"):
        raise ApiError("Bitte den Server auf dem Root-Server zuerst stoppen – ein laufender Server "
                       "schreibt server.properties gleich wieder um.", 409)
    values = body.get("values")
    if not isinstance(values, dict):
        raise ApiError("Ungültige Daten.")
    meta = manager.PROPS_META.get(_hosted_kind(remote), {})
    pruefen = getattr(manager, "validate_prop", None) or getattr(manager, "_validate_prop", None)
    clean: dict[str, str] = {}
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", str(key)):
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        text = str(value).replace("\r", "").replace("\n", " ").strip()[:512]
        known = meta.get(key)
        if known:
            if known.get("managed"):                  # setzt der Root-Server selbst (Port, Transport)
                continue
            if callable(pruefen):
                try:
                    pruefen(known, key, text)
                except ValueError as exc:
                    raise ApiError(str(exc)) from exc
        clean[key] = text
    if not clean:
        raise ApiError("Es wurde keine Einstellung übergeben.")
    alt = _hosted_read_text(remote_id, "server.properties")
    if alt is None:
        raise ApiError("Auf dem Root-Server gibt es noch keine server.properties – sie entsteht "
                       "beim ersten Start.", 404)
    neu = _properties_patch(alt, clean)
    cloud.remote_file_write(remote_id, "server.properties", neu)
    return {"ok": True, "fields": _props_fields(_properties_from_text(neu), _hosted_kind(remote))}


# -- Spielerverwaltung einer gehosteten Instanz: Listen als Dateien, Befehle über die Konsole

def _hosted_player_files(kind: str) -> tuple[str, str, str]:
    """Freigabeliste, Sperrliste und IP-Sperren – dieselben Dateinamen wie auf dem PC."""
    return (("allowlist.json" if kind == "bedrock" else "whitelist.json"),
            "banned-players.json", "banned-ips.json")


def _hosted_json_list(remote_id: str, name: str) -> tuple[list[dict], bool]:
    """Liste aus einer Minecraft-Datei auf dem Root-Server – (Einträge, lesbar?).

    Eine fehlende Datei gilt als leere Liste, eine unlesbare wird gemeldet: sonst sähen beide
    gleich aus und ein Schreiben würde die bisherigen Einträge still wegwerfen.
    """
    text = _hosted_read_text(remote_id, name)
    if text is None or not text.strip():
        return [], True
    try:
        data = json.loads(text)
    except ValueError:
        return [], False
    if not isinstance(data, list):
        return [], False
    return [e for e in data if isinstance(e, dict)], True


def _hosted_write_list(remote_id: str, name: str, entries: list[dict]) -> None:
    """Liste als JSON auf den Root-Server schreiben (Minecraft-Format, mit Zeilenumbruch am Ende)."""
    cloud.remote_file_write(remote_id, name,
                            json.dumps(entries, indent=2, ensure_ascii=False) + "\n")


def _hosted_cfg(remote: dict, remote_id: str) -> dict:
    """Behelfs-Konfiguration für die Prüfungen aus core/players.py.

    Nur Typ, Crossplay und – falls es sie gibt – die Kennung der lokalen Kopie: aus ihren Dateien
    (usercache.json) kommt die Spieler-Nummer, ohne Mojang zu fragen.
    """
    local = next((cfg for cfg in store.all_servers()
                  if str(cloud.link_of(cfg).get("instance") or "") == remote_id), None)
    return {"id": str((local or {}).get("id") or ""), "type": _hosted_kind(remote),
            "flavor": str(remote.get("flavor") or "paper"), "geyser": bool(remote.get("geyser")),
            "name": str(remote.get("name") or "")}


def _hosted_flag_key(kind: str) -> str:
    """Schalter in server.properties, der die Freigabeliste ein- und ausschaltet."""
    return "allow-list" if kind == "bedrock" else "white-list"


def _hosted_list_enabled(remote_id: str, kind: str) -> bool:
    """Ist die Freigabeliste auf dem Root-Server eingeschaltet?"""
    text = _hosted_read_text(remote_id, "server.properties") or ""
    values = dict(_properties_from_text(text))
    return str(values.get(_hosted_flag_key(kind), "")).strip().lower() == "true"


def _hosted_set_enabled(remote_id: str, kind: str, enabled: bool) -> None:
    """Freigabeliste in server.properties ein- oder ausschalten (nur bei gestopptem Server)."""
    text = _hosted_read_text(remote_id, "server.properties")
    if text is None:
        raise ApiError("Auf dem Root-Server gibt es noch keine server.properties – bitte den "
                       "Server einmal starten.", 404)
    cloud.remote_file_write(remote_id, "server.properties",
                            _properties_patch(text, {_hosted_flag_key(kind): "true" if enabled else "false"}))


def _hosted_online(remote_id: str, cfg: dict) -> tuple[list[str], bool]:
    """Wer spielt gerade? Der Konsolenbefehl „list“ auf dem Root-Server, Antwort aus der Konsole."""
    parse = getattr(players, "parse_list", None) or getattr(players, "_parse_list", None)
    if not callable(parse):
        return [], False
    try:
        marke = _zahl(cloud.remote_console(remote_id, 0, 1).get("next"))
        cloud.remote_command(remote_id, "list")
    except cloud.CloudError:
        return [], False
    ende = time.time() + HOSTED_LIST_WAIT
    while True:
        time.sleep(0.4)
        try:
            lines = [str(line) for line in (cloud.remote_console(remote_id, marke).get("lines") or [])]
        except cloud.CloudError:
            return [], False
        names = parse(lines, cfg)
        if names is not None:
            return names, True
        if time.time() >= ende:
            return [], False


def _ban_expired(raw: str) -> bool:
    """Ist eine Sperre auf Zeit schon abgelaufen? Dieselbe Regel wie bei lokalen Servern."""
    fn = getattr(players, "ban_expired", None) or getattr(players, "_expired", None)
    return bool(fn(raw)) if callable(fn) else False


def _hosted_players_view(remote_id: str, remote: dict, message: str = "", warn: bool = False,
                         *, online: bool = True) -> dict:
    """Gesamtbild der Spielerverwaltung – gleiche Felder wie players.overview für lokale Server."""
    kind = _hosted_kind(remote)
    bedrock = kind == "bedrock"
    allow_file, ban_file, ban_ip_file = _hosted_player_files(kind)
    running = bool(remote.get("running"))
    roh, lesbar = _hosted_json_list(remote_id, allow_file)
    broken = [] if lesbar else [allow_file]
    allowed = sorted(({"name": str(e.get("name") or "").strip(), "uuid": str(e.get("uuid") or "")}
                      for e in roh if str(e.get("name") or "").strip()),
                     key=lambda e: e["name"].lower())
    banned: list[dict] = []
    banned_ips: list[dict] = []
    if not bedrock:
        roh_ban, lesbar_ban = _hosted_json_list(remote_id, ban_file)
        if not lesbar_ban:
            broken.append(ban_file)
        for entry in roh_ban:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            expires = str(entry.get("expires") or "forever")
            banned.append({"name": name, "uuid": str(entry.get("uuid") or ""),
                           "reason": str(entry.get("reason") or ""),
                           "source": str(entry.get("source") or ""),
                           "created": str(entry.get("created") or ""),
                           "expires": expires, "expired": _ban_expired(expires)})
        banned.sort(key=lambda e: e["name"].lower())
        roh_ip, lesbar_ip = _hosted_json_list(remote_id, ban_ip_file)
        if not lesbar_ip:
            broken.append(ban_ip_file)
        banned_ips = sorted(({"ip": str(e.get("ip") or "").strip(),
                              "reason": str(e.get("reason") or ""),
                              "source": str(e.get("source") or ""),
                              "expires": str(e.get("expires") or "forever"),
                              "expired": _ban_expired(str(e.get("expires") or "forever"))}
                             for e in roh_ip if str(e.get("ip") or "").strip()),
                            key=lambda e: e["ip"])
    names: list[str] = []
    known = False
    if running and online:
        names, known = _hosted_online(remote_id, _hosted_cfg(remote, remote_id))
    return {"kind": kind, "hosted": True, "instance": remote_id, "running": running,
            "enabled": _hosted_list_enabled(remote_id, kind),
            "allowed": allowed, "banned": banned, "banned_ips": banned_ips, "bans": not bedrock,
            "broken": broken, "online": names, "online_known": known,
            "max_players": _zahl(remote.get("max_players")),
            "message": message, "warn": warn}


def api_cloud_players(_body, query) -> dict:
    remote_id = _cloud_instance({}, query)
    return _hosted_players_view(remote_id, _hosted_remote(remote_id))


def _cmd_arg(name: str) -> str:
    """Name als Befehlsargument – Gamertags mit Leerzeichen gehören in Anführungszeichen."""
    return f'"{name}"' if " " in name else name


def api_cloud_players_action(body, query) -> dict:
    """Freigabeliste, Sperren und Rauswürfe auf dem Root-Server.

    Läuft der Server, geht alles über seine Konsole; ist er gestoppt, schreibt das Programm die
    Listen-Dateien auf dem Root-Server – genau wie es das lokal mit den Dateien auf dem PC tut.
    """
    remote_id = _cloud_instance(body, query)
    remote = _hosted_remote(remote_id)
    action = str(body.get("action") or "")
    if action not in HOSTED_PLAYER_ACTIONS:
        raise ApiError("Unbekannte Aktion.")
    kind = _hosted_kind(remote)
    bedrock = kind == "bedrock"
    allow_file, ban_file, _ = _hosted_player_files(kind)
    running = bool(remote.get("running"))
    wort = "allowlist" if bedrock else "whitelist"
    label = "Erlaubnisliste" if bedrock else "Freigabeliste"
    cfg = _hosted_cfg(remote, remote_id)

    def bild(text: str, warnen: bool = False, *, frisch: bool = False) -> dict:
        return _hosted_players_view(remote_id, remote, text, warnen, online=frisch)

    if action in ("ban", "unban") and bedrock:
        raise ApiError("Der Bedrock-Server führt keine eigene Sperrliste. Nimm den Spieler aus der "
                       "Erlaubnisliste und schalte sie ein – dann kommt er nicht mehr herein.")

    if action in ("whitelist_on", "whitelist_off"):
        an = action == "whitelist_on"
        if running and bedrock:
            raise ApiError("Die Erlaubnisliste eines Bedrock-Servers steht in server.properties – "
                           "sie lässt sich nur bei gestopptem Server umschalten.", 409)
        if running:
            cloud.remote_command(remote_id, f"{wort} {'on' if an else 'off'}")
            cloud.remote_command(remote_id, f"{wort} reload")
            time.sleep(HOSTED_SETTLE)
        else:
            _hosted_set_enabled(remote_id, kind, an)
        return bild(f"{label} ist jetzt {'eingeschaltet' if an else 'ausgeschaltet'}."
                    + ("" if running else " Es gilt ab dem nächsten Start."))

    try:
        name = players.check_name(cfg, body.get("name", ""))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc

    if action == "whitelist_add":
        if running:
            cloud.remote_command(remote_id, f"{wort} add {_cmd_arg(name)}")
            time.sleep(HOSTED_SETTLE)
            out = bild(f"„{name}“ steht jetzt auf der {label}.")
            if not any(e["name"].lower() == name.lower() for e in out["allowed"]):
                out["message"] = (f"Der Server hat „{name}“ nicht aufgenommen – die Konsole zeigt, "
                                  f"woran es lag (meist ein Tippfehler im Namen).")
                out["warn"] = True
            return out
        entries, lesbar = _hosted_json_list(remote_id, allow_file)
        if not lesbar:
            raise ApiError(f"Die Datei {allow_file} auf dem Root-Server ist beschädigt. Bitte den "
                           f"Server einmal starten und wieder stoppen – er schreibt sie dann neu.", 409)
        if any(str(e.get("name") or "").lower() == name.lower() for e in entries):
            return bild(f"„{name}“ steht bereits auf der {label}.")
        if bedrock:
            entries.append({"ignoresPlayerLimit": False, "name": name})
        else:
            if not players.JAVA_NAME_RE.fullmatch(name):
                return bild(f"„{name}“ sieht nach einem Bedrock-Spieler aus (Crossplay). Dafür muss "
                            f"der Server laufen – bitte starten und es dann noch einmal versuchen.", True)
            try:
                uuid, name = players.profile_for(cfg, name)
            except ValueError as exc:
                raise ApiError(str(exc)) from exc
            entries.append({"uuid": uuid, "name": name})
        _hosted_write_list(remote_id, allow_file, entries)
        return bild(f"„{name}“ steht jetzt auf der {label}.")

    if action == "whitelist_remove":
        if running:
            cloud.remote_command(remote_id, f"{wort} remove {_cmd_arg(name)}")
            time.sleep(HOSTED_SETTLE)
        else:
            entries, lesbar = _hosted_json_list(remote_id, allow_file)
            if not lesbar:
                raise ApiError(f"Die Datei {allow_file} auf dem Root-Server ist beschädigt – bitte "
                               f"den Server einmal starten und wieder stoppen.", 409)
            bleibt = [e for e in entries if str(e.get("name") or "").lower() != name.lower()]
            if len(bleibt) == len(entries):
                return bild(f"„{name}“ stand nicht auf der {label}.")
            _hosted_write_list(remote_id, allow_file, bleibt)
        return bild(f"„{name}“ steht nicht mehr auf der {label}.")

    if action == "ban":
        grund = re.sub(r"[\x00-\x1f]", " ", str(body.get("reason") or "")).strip()[:120] \
            or "Vom Serverbesitzer gesperrt."
        dauer = str(body.get("duration") or "").strip().lower()
        sekunden = 0 if dauer in ("", "forever", "dauerhaft") else players.DURATIONS.get(dauer, -1)
        if sekunden < 0:
            raise ApiError("Unbekannte Dauer – möglich sind 1h, 6h, 1d, 7d, 30d oder leer für dauerhaft.")
        if running:
            if sekunden:
                raise ApiError("Eine Sperre auf Zeit kann der laufende Server nicht selbst setzen. "
                               "Sperre dauerhaft – oder stoppe den Server, dann trägt das Programm "
                               "die Frist ein.")
            cloud.remote_command(remote_id, f"ban {_cmd_arg(name)} {grund}")
            time.sleep(HOSTED_SETTLE)
            out = bild(f"„{name}“ ist gesperrt.", frisch=True)
            if not any(e["name"].lower() == name.lower() for e in out["banned"]):
                out["message"] = (f"Der Server hat „{name}“ nicht gesperrt – die Konsole zeigt, "
                                  f"woran es lag (der Name muss dem Server bekannt sein).")
                out["warn"] = True
            return out
        try:
            uuid, name = players.profile_for(cfg, name)
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
        entries, lesbar = _hosted_json_list(remote_id, ban_file)
        if not lesbar:
            raise ApiError(f"Die Datei {ban_file} auf dem Root-Server ist beschädigt – bitte den "
                           f"Server einmal starten und wieder stoppen.", 409)
        stempel = getattr(players, "stamp", None) or getattr(players, "_stamp", None)
        jetzt = time.time()
        entries = [e for e in entries if str(e.get("name") or "").lower() != name.lower()]
        entries.append({"uuid": uuid, "name": name,
                        "created": stempel(jetzt) if callable(stempel) else "",
                        "source": "Server Manager",
                        "expires": (stempel(jetzt + sekunden) if sekunden and callable(stempel)
                                    else "forever"),
                        "reason": grund})
        _hosted_write_list(remote_id, ban_file, entries)
        return bild(f"„{name}“ ist gesperrt." + (" Die Sperre endet von allein." if sekunden else ""))

    if action == "unban":
        if running:
            cloud.remote_command(remote_id, f"pardon {_cmd_arg(name)}")
            time.sleep(HOSTED_SETTLE)
        else:
            entries, lesbar = _hosted_json_list(remote_id, ban_file)
            if not lesbar:
                raise ApiError(f"Die Datei {ban_file} auf dem Root-Server ist beschädigt – bitte den "
                               f"Server einmal starten und wieder stoppen.", 409)
            bleibt = [e for e in entries if str(e.get("name") or "").lower() != name.lower()]
            if len(bleibt) == len(entries):
                return bild(f"„{name}“ war nicht gesperrt.")
            _hosted_write_list(remote_id, ban_file, bleibt)
        return bild(f"„{name}“ ist nicht mehr gesperrt.")

    # kick
    if not running:
        return bild("Der Server auf dem Root-Server läuft gerade nicht – es ist niemand online.", True)
    cloud.remote_command(remote_id, f"kick {_cmd_arg(name)}")
    time.sleep(HOSTED_SETTLE)
    return bild(f"„{name}“ wurde hinausgeworfen.", frisch=True)


def api_cloud_upload(body, _query) -> dict:
    """„Auf den Root-Server verschieben“: ganzen Serverordner hochladen (Job)."""
    cfg = _require(store.get(str(body.get("server", ""))))
    _no_job(cfg["id"])
    job = cloud.upload_async(cfg)
    log.info("Server „%s“ wird auf den Root-Server verschoben.", cfg["name"])
    return {"job_id": job["id"], "server_id": cfg["id"]}


def api_cloud_download(body, query) -> dict:
    """„Zurück auf diesen PC holen“: Instanz herunterladen, prüfen und den Root-Ordner freigeben."""
    job = cloud.download_async(_cloud_instance(body, query))
    log.info("Rückholung vom Root-Server gestartet.")
    return {"job_id": job["id"], "server_id": job["server_id"]}


def api_cloud_release(body, query) -> dict:
    """Nachträglich freigeben: prüft die lokalen Dateien erneut und löscht dann den Root-Ordner."""
    job = cloud.release_async(_cloud_instance(body, query))
    return {"job_id": job["id"], "server_id": job["server_id"]}


def api_cloud_abort(body, _query) -> dict:
    return cloud.abort_transfer(str(body.get("server", "")))


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

SHUTDOWN_BLOCK_TEXT = ("Minecraft-Server laufen noch! Bitte im Server Manager alle Server stoppen, "
                       "bevor du den PC herunterfährst – sonst gehen ungespeicherte Welt-Änderungen verloren.")


def shutdown_block_reason() -> str | None:
    """Grund für die Windows-Sperre gegen Herunterfahren – nur solange mindestens ein Server läuft."""
    n = sum(1 for c in store.all_servers() if manager.is_running(c["id"]))
    if not n:
        return None
    servers = "1 Minecraft-Server läuft noch" if n == 1 else f"{n} Minecraft-Server laufen noch"
    return servers + "! Bitte im Server Manager stoppen, bevor du den PC herunterfährst – sonst gehen ungespeicherte Welt-Änderungen verloren."


def _on_end_session() -> None:
    """Windows fährt trotzdem herunter: Server in den verbleibenden Sekunden sauber stoppen."""
    log.info("Windows beendet die Sitzung – Server werden gestoppt …")
    manager.emergency_stop_all(4.0)


def start_tray(url: str):
    """Symbol neben der Uhr: Klick öffnet die Oberfläche, Menü stoppt/beendet. Nie Pflicht."""
    if not tray.available or os.environ.get("MCSM_NO_TRAY"):
        return None
    try:
        icon = tray.Tray(BASE / "app.ico", APP_NAME, on_open=lambda: open_ui(url),
                         on_stop_all=manager.stop_all, on_quit=request_shutdown,
                         on_query_end=shutdown_block_reason, on_end_session=_on_end_session)
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
                icon.set_shutdown_block(SHUTDOWN_BLOCK_TEXT if n else None)
                last = text
            time.sleep(5)

    threading.Thread(target=tips, daemon=True).start()
    return icon


ROUTES = {
    ("GET", "ping"): api_ping,
    ("GET", "bootstrap"): api_bootstrap,
    ("GET", "versions"): api_versions,
    ("GET", "modpacks/search"): api_modpack_search,
    ("GET", "publicip"): api_public_ip,
    ("POST", "servers"): api_create,
    ("POST", "shutdown"): api_shutdown,
    ("GET", "update"): api_update,
    ("POST", "update/apply"): api_update_apply,
    # Cloud (Root-Server)
    ("GET", "cloud/status"): api_cloud_status,
    ("GET", "cloud/servers"): api_cloud_servers,
    ("GET", "cloud/instance"): api_cloud_instance,
    ("GET", "cloud/console"): api_cloud_console,
    ("GET", "cloud/files"): api_cloud_files,
    ("GET", "cloud/file"): api_cloud_file_get,
    ("GET", "cloud/settings"): api_cloud_settings,
    ("GET", "cloud/props"): api_cloud_props,
    ("GET", "cloud/players"): api_cloud_players,
    ("GET", "cloud/login/poll"): api_cloud_login_poll,
    ("GET", "cloud/sessions"): api_cloud_sessions,
    ("POST", "cloud/login"): api_cloud_login,
    ("POST", "cloud/login/register"): api_cloud_login_register,
    ("POST", "cloud/login/abort"): api_cloud_login_abort,
    ("POST", "cloud/login/invite"): api_cloud_login_invite,
    ("POST", "cloud/logout"): api_cloud_logout,
    ("POST", "cloud/account/name"): api_cloud_account_name,
    ("POST", "cloud/session/revoke"): api_cloud_session_revoke,
    ("POST", "cloud/link/discord"): api_cloud_link_discord,
    ("POST", "cloud/start"): api_cloud_start,
    ("POST", "cloud/stop"): api_cloud_stop,
    ("POST", "cloud/command"): api_cloud_command,
    ("POST", "cloud/settings"): api_cloud_settings_put,
    ("POST", "cloud/props"): api_cloud_props_put,
    ("POST", "cloud/players"): api_cloud_players_action,
    ("POST", "cloud/file"): api_cloud_file_put,
    ("POST", "cloud/file/delete"): api_cloud_file_delete,
    ("POST", "cloud/file/mkdir"): api_cloud_file_mkdir,
    ("POST", "cloud/file/rename"): api_cloud_file_rename,
    ("POST", "cloud/file/upload"): api_cloud_file_upload,
    ("POST", "cloud/file/download"): api_cloud_file_download,
    ("POST", "cloud/upload"): api_cloud_upload,
    ("POST", "cloud/download"): api_cloud_download,
    ("POST", "cloud/release"): api_cloud_release,
    ("POST", "cloud/abort"): api_cloud_abort,
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
    ("GET", "players"): api_players,
    ("POST", "players"): api_players_action,
    ("GET", "xbox"): api_xbox_status,
    ("POST", "xbox/setup"): api_xbox_setup,
    ("POST", "xbox/start"): api_xbox_start,
    ("POST", "xbox/stop"): api_xbox_stop,
    ("POST", "xbox/disable"): api_xbox_disable,
    ("POST", "portmap"): api_portmap,
    ("POST", "hardcore"): api_hardcore,
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
            # Eine einzelne Datei (Plugin) kommt als Base64 im Körper – dafür ist mehr Platz
            # nötig als für gewöhnliche Aufrufe. Alles andere bleibt eng begrenzt.
            grenze = UPLOAD_BODY_MAX if route == "cloud/file/upload" else 4_000_000
            if length > grenze:
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
            if len(parts) == 3 and parts[0] == "modpacks" and parts[2] == "versions" and method == "GET":
                self._json(200, api_modpack_versions(body, query, parts[1]))
                return
            handler = ROUTES.get((method, "/".join(parts))) or ROUTES.get((method, parts[0] if parts else ""))
            if handler is None:
                raise ApiError("Unbekannter Aufruf.", 404)
            self._json(200, handler(body, query))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except sources.SourceError as exc:
            self._json(502, {"error": str(exc)})
        except cloud.CloudError as exc:
            # Fehler des Root-Servers samt Status durchgeben (401 hat core/cloud.py schon abgemeldet).
            self._json(exc.status if 400 <= exc.status < 600 else 502, {"error": str(exc)})
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
