"""Persistente Konfiguration aller Server-Instanzen."""
from __future__ import annotations

import json
import pathlib
import re
import secrets
import threading
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
SERVERS_DIR = BASE / "servers"
RUNTIME_DIR = BASE / "runtime"
CACHE_DIR = BASE / "cache"
WEB_DIR = BASE / "web"
CONFIG_FILE = DATA_DIR / "servers.json"

_lock = threading.RLock()


def ensure_dirs() -> None:
    for d in (DATA_DIR, SERVERS_DIR, RUNTIME_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def server_dir(server_id: str) -> pathlib.Path:
    return SERVERS_DIR / server_id


DEFAULTS = {
    "name": "Mein Server",
    "type": "bedrock",          # "bedrock" (BDS) oder "java" (Paper + Geyser)
    "version": "",
    "port": 19132,
    "bedrock_port": 19132,      # nur bei type=java (Geyser)
    "motd": "Ein Minecraft Server",
    "max_players": 10,
    "ram_mb": 4096,             # nur Java
    "gamemode": "survival",
    "difficulty": "easy",
    "online_mode": True,
    "allow_cheats": False,
    "pvp": True,
    "view_distance": 10,
    "level_seed": "",
    "public_ip": "",            # nur Bedrock: öffentliche IPv4 für NetherNet (leer = vom Router abfragen)
    "public_address": "",       # Anzeige für Freunde: MyFRITZ!-Name oder feste IP (leer = automatisch ermitteln)
    "auto_portmap": False,      # Portfreigabe beim Serverstart per UPnP in der FritzBox anfordern
    "geyser": True,             # nur Java: Bedrock-Crossplay aktivieren
    "autostart": False,
    "installed": False,
    "eula_accepted": False,
    # Xbox-Freunde-Modus (MCXboxBroadcast): Server erscheint in der Freundesliste eines Bot-Kontos.
    "xbox_enabled": False,
    "xbox_address": "",         # Adresse, die Freunde erreichen (LAN-IP, öffentliche IP oder DynDNS-Name)
    "xbox_host_name": "",       # Anzeigename in der Freundesliste (leer = Servername)
    "xbox_autostart": True,     # Bot zusammen mit dem Server starten/stoppen
}

VALID_TYPES = ("bedrock", "java")
VALID_GAMEMODES = ("survival", "creative", "adventure")
VALID_DIFFICULTIES = ("peaceful", "easy", "normal", "hard")


def new_id() -> str:
    return secrets.token_hex(4)


def _read() -> dict:
    if not CONFIG_FILE.exists():
        return {"servers": []}
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "servers" not in data:
            return {"servers": []}
        return data
    except (OSError, ValueError):
        return {"servers": []}


def _write(data: dict) -> None:
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(CONFIG_FILE)


def all_servers() -> list[dict]:
    with _lock:
        return [dict(DEFAULTS, **s) for s in _read()["servers"]]


def get(server_id: str) -> dict | None:
    for s in all_servers():
        if s["id"] == server_id:
            return s
    return None


def save(cfg: dict) -> dict:
    with _lock:
        data = _read()
        for i, s in enumerate(data["servers"]):
            if s["id"] == cfg["id"]:
                data["servers"][i] = cfg
                break
        else:
            data["servers"].append(cfg)
        _write(data)
    return cfg


def delete(server_id: str) -> None:
    with _lock:
        data = _read()
        data["servers"] = [s for s in data["servers"] if s["id"] != server_id]
        _write(data)


def _clamp(value, low: int, high: int, fallback: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, n))


def sanitize(raw: dict, existing: dict | None = None) -> dict:
    """Nimmt rohe GUI-Eingaben entgegen und macht eine gültige Konfiguration daraus."""
    cfg = dict(existing) if existing else dict(DEFAULTS)
    if existing is None:
        cfg["id"] = new_id()
        cfg["created"] = time.strftime("%Y-%m-%d %H:%M")

    name = str(raw.get("name", cfg.get("name", ""))).strip() or "Mein Server"
    cfg["name"] = name[:48]

    if existing is None:
        stype = str(raw.get("type", "bedrock")).lower()
        cfg["type"] = stype if stype in VALID_TYPES else "bedrock"

    version = str(raw.get("version", cfg.get("version", ""))).strip()
    if re.fullmatch(r"[0-9][0-9A-Za-z.\-_]{0,31}", version or ""):
        cfg["version"] = version

    cfg["port"] = _clamp(raw.get("port", cfg["port"]), 1024, 65535,
                         19132 if cfg["type"] == "bedrock" else 25565)
    cfg["bedrock_port"] = _clamp(raw.get("bedrock_port", cfg["bedrock_port"]), 1024, 65535, 19132)
    cfg["max_players"] = _clamp(raw.get("max_players", cfg["max_players"]), 1, 200, 10)
    cfg["ram_mb"] = _clamp(raw.get("ram_mb", cfg["ram_mb"]), 1024, 65536, 4096)
    cfg["view_distance"] = _clamp(raw.get("view_distance", cfg["view_distance"]), 4, 32, 10)

    motd = str(raw.get("motd", cfg["motd"])).strip() or "Ein Minecraft Server"
    # Zeilenumbrüche und '=' würden server.properties zerlegen.
    cfg["motd"] = motd.replace("\\", "").replace("=", " ").replace("\n", " ").replace("\r", " ")[:64]

    seed = str(raw.get("level_seed", cfg["level_seed"])).strip()
    cfg["level_seed"] = seed[:40] if re.fullmatch(r"[-0-9A-Za-z ]{0,40}", seed) else ""

    if "public_ip" in raw:
        ip = str(raw.get("public_ip") or "").strip()
        cfg["public_ip"] = ip if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) else ""
    if "public_address" in raw:
        addr = str(raw.get("public_address") or "").strip().lower()
        cfg["public_address"] = addr[:96] if re.fullmatch(r"[a-z0-9.\-]{1,96}", addr) else ""

    gm = str(raw.get("gamemode", cfg["gamemode"])).lower()
    cfg["gamemode"] = gm if gm in VALID_GAMEMODES else "survival"
    df = str(raw.get("difficulty", cfg["difficulty"])).lower()
    cfg["difficulty"] = df if df in VALID_DIFFICULTIES else "easy"

    for flag in ("online_mode", "allow_cheats", "pvp", "geyser", "autostart", "eula_accepted",
                 "xbox_enabled", "xbox_autostart", "auto_portmap"):
        if flag in raw:
            cfg[flag] = bool(raw[flag])

    # Xbox-Freunde-Modus: Adresse = IP oder Hostname, Anzeigename ohne YAML-Sonderzeichen.
    if "xbox_address" in raw:
        addr = str(raw.get("xbox_address") or "").strip()
        cfg["xbox_address"] = addr[:96] if re.fullmatch(r"[A-Za-z0-9.\-]{0,96}", addr) else ""
    if "xbox_host_name" in raw:
        host = str(raw.get("xbox_host_name") or "").strip()
        cfg["xbox_host_name"] = re.sub(r'["\\\n\r]', "", host)[:32]

    return cfg
