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
    "type": "bedrock",          # "bedrock" (BDS) oder "java" (Paper, optional + Geyser; oder Modpack)
    "flavor": "paper",          # nur Java: "paper" oder der Mod-Loader eines Modpacks (fabric/neoforge/forge/quilt)
    "modpack": {},              # nur Modpack: project_id, title, version_id, version_name, mc_version, loader,
                                #   loader_version, icon_url, url (alles von Modrinth)
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
    "auto_portmap": True,       # Portfreigabe beim Serverstart per UPnP anfordern (immer an, nicht in der Oberfläche)
    "companion_tips": True,   # nur Paper: rotierende Tipps des Begleit-Plugins im Chat
    "hardcore": False,          # nur Paper: MCSM-Hardcore des Companion-Plugins (1 Leben, Grab, Totem)
    "geyser": True,             # nur Java: Bedrock-Crossplay aktivieren
    "autostart": False,
    "installed": False,
    "eula_accepted": False,
    # Cloud (Root-Server): Verknüpfung dieser lokalen Kopie mit einer Instanz auf dem Root-Server.
    # Wird ausschließlich von core/cloud.py geschrieben, nie aus der Oberfläche (sanitize kennt es
    # nicht). Felder: instance, state (local_only|uploading|hosted|awaiting_pull|downloading|
    # suspended), running, name, address, session (laufende Übertragung), updated.
    "cloud": {},
    # Xbox-Freunde-Modus (MCXboxBroadcast): Server erscheint in der Freundesliste eines Bot-Kontos.
    "xbox_enabled": False,
    "xbox_address": "",         # Adresse, die Freunde erreichen (LAN-IP, öffentliche IP oder DynDNS-Name)
    "xbox_host_name": "",       # Anzeigename in der Freundesliste (leer = Servername)
    "xbox_autostart": True,     # Bot zusammen mit dem Server starten/stoppen
}

# Schlüssel früherer Fassungen, die beim Laden verworfen werden, damit sie nicht zurückkommen.
OBSOLETE = ("companion_admins",)

VALID_TYPES = ("bedrock", "java")
VALID_FLAVORS = ("paper", "fabric", "neoforge", "forge", "quilt")
VALID_GAMEMODES = ("survival", "creative", "adventure")
VALID_DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
MODPACK_RAM_MIN = 4096
MODPACK_RAM_DEFAULT = 6144
_VERSION_RE = r"[0-9][0-9A-Za-z.\-_]{0,31}"
_MODRINTH_ID_RE = r"[A-Za-z0-9]{1,32}"


def is_modpack(cfg: dict) -> bool:
    return cfg.get("type") == "java" and str(cfg.get("flavor") or "paper") != "paper"


def sanitize_modpack(raw) -> dict:
    """Modpack-Angaben aus der Oberfläche prüfen (alles stammt aus der Modrinth-API und wird bei der
    Installation ohnehin noch einmal von dort geholt). Leeres dict = kein gültiges Modpack."""
    if not isinstance(raw, dict):
        return {}
    pid = str(raw.get("project_id") or "").strip()
    vid = str(raw.get("version_id") or "").strip()
    if not (re.fullmatch(_MODRINTH_ID_RE, pid) and re.fullmatch(_MODRINTH_ID_RE, vid)):
        return {}
    loader = str(raw.get("loader") or "").lower()
    mc = str(raw.get("mc_version") or "").strip()
    loader_version = str(raw.get("loader_version") or "").strip()
    icon = str(raw.get("icon_url") or "").strip()
    url = str(raw.get("url") or "").strip()
    clean = lambda s, n: re.sub(r"[\x00-\x1f]", "", str(s or "")).strip()[:n]     # noqa: E731
    return {
        "project_id": pid,
        "version_id": vid,
        "title": clean(raw.get("title"), 80),
        "version_name": clean(raw.get("version_name"), 64),
        "mc_version": mc if re.fullmatch(_VERSION_RE, mc) else "",
        "loader": loader if loader in VALID_FLAVORS and loader != "paper" else "",
        "loader_version": loader_version if re.fullmatch(r"[0-9A-Za-z.\-+_]{1,40}", loader_version) else "",
        "icon_url": icon if icon.startswith("https://cdn.modrinth.com/") and len(icon) < 300 else "",
        "url": url if re.fullmatch(r"https://modrinth\.com/[A-Za-z0-9/_\-]{1,120}", url) else "",
    }


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
        out = []
        for s in _read()["servers"]:
            cfg = dict(DEFAULTS, **s)
            for key in OBSOLETE:
                cfg.pop(key, None)
            # eigene Kopien: sonst zeigten alle Server auf dieselben leeren Vorgabe-Objekte
            cfg["cloud"] = dict(cfg.get("cloud") or {})
            cfg["modpack"] = dict(cfg.get("modpack") or {})
            out.append(cfg)
        return out


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
        cfg["flavor"] = "paper"
        cfg["modpack"] = {}
        cfg["cloud"] = {}                              # nur core/cloud.py schreibt hier hinein

    # Modpack: beim Anlegen oder – bei einem bestehenden Modpack-Server – beim Versionswechsel.
    # Der Loader kommt aus der gewählten Pack-Version (ein Pack kann z. B. von Forge auf NeoForge wechseln);
    # Paper-/Bedrock-Server bleiben, was sie sind.
    if cfg["type"] == "java" and "modpack" in raw and (existing is None or is_modpack(cfg)):
        mp = sanitize_modpack(raw.get("modpack"))
        if mp and mp["loader"]:
            cfg["modpack"] = mp
            cfg["flavor"] = mp["loader"]
            cfg["geyser"] = False                      # Bukkit-Plugins laufen nicht auf Fabric/NeoForge
        elif existing is not None:
            raise ValueError("Ungültige Modpack-Angaben – bitte die Version erneut auswählen.")
    modpack = is_modpack(cfg)

    version = str(raw.get("version", cfg.get("version", ""))).strip()
    if modpack:
        version = cfg["modpack"].get("mc_version") or version
    if re.fullmatch(_VERSION_RE, version or ""):
        cfg["version"] = version

    cfg["port"] = _clamp(raw.get("port", cfg["port"]), 1024, 65535,
                         19132 if cfg["type"] == "bedrock" else 25565)
    cfg["bedrock_port"] = _clamp(raw.get("bedrock_port", cfg["bedrock_port"]), 1024, 65535, 19132)
    cfg["max_players"] = _clamp(raw.get("max_players", cfg["max_players"]), 1, 200, 10)
    if modpack:
        cfg["ram_mb"] = _clamp(raw.get("ram_mb", cfg["ram_mb"]), MODPACK_RAM_MIN, 65536, MODPACK_RAM_DEFAULT)
    else:
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
        # Typische Eingaben von der MyFRITZ!-Seite normalisieren (URL, Pfad, Port), dann laut ablehnen
        # statt stillschweigend zu leeren – die Oberfläche meldet sonst „gespeichert“ bei leerem Feld.
        addr = str(raw.get("public_address") or "").strip().lower()
        addr = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", addr).split("/", 1)[0]   # URL -> Host
        addr = re.sub(r":\d{1,5}$", "", addr)                                   # host:port -> host
        if addr and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.\-]{0,94}[a-z0-9])?", addr):
            raise ValueError("Öffentliche Adresse: nur Hostname oder IPv4 eintragen, z. B. abc123.myfritz.net")
        cfg["public_address"] = addr

    gm = str(raw.get("gamemode", cfg["gamemode"])).lower()
    cfg["gamemode"] = gm if gm in VALID_GAMEMODES else "survival"
    df = str(raw.get("difficulty", cfg["difficulty"])).lower()
    cfg["difficulty"] = df if df in VALID_DIFFICULTIES else "easy"

    for flag in ("online_mode", "allow_cheats", "pvp", "geyser", "autostart", "eula_accepted",
                 "xbox_enabled", "xbox_autostart", "auto_portmap", "hardcore",
                 "companion_tips"):
        if flag in raw:
            cfg[flag] = bool(raw[flag])
    if modpack:
        cfg["geyser"] = False                          # kein Crossplay auf Mod-Loadern (s. o.)


    # Xbox-Freunde-Modus: Adresse = IP oder Hostname, Anzeigename ohne YAML-Sonderzeichen.
    if "xbox_address" in raw:
        addr = str(raw.get("xbox_address") or "").strip()
        cfg["xbox_address"] = addr[:96] if re.fullmatch(r"[A-Za-z0-9.\-]{0,96}", addr) else ""
    if "xbox_host_name" in raw:
        host = str(raw.get("xbox_host_name") or "").strip()
        cfg["xbox_host_name"] = re.sub(r'["\\\n\r]', "", host)[:32]

    return cfg
