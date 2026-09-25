"""Installation, Konfiguration, Dateien und Prozess-Steuerung der Server-Instanzen."""
from __future__ import annotations

import ctypes
import html
import ipaddress
import os
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

from . import companion, modpacks, sources, store

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
MAX_LOG_LINES = 4000

# Angekündigter Stopp über das Begleit-Plugin (Knopf „Stoppen“ auf Paper-Servern):
STOP_COUNTDOWN = 10       # Sekunden, die das Plugin im Spiel herunterzählt
ANNOUNCE_WAIT = 15        # so lange darauf warten; danach geht wie bisher „stop“ hinterher
ANNOUNCE_ANSWER = 2.0     # so lange auf die Bestätigung des Plugins warten
# Protokollzeile, mit der das Plugin den angekündigten Stopp bestätigt (ShutdownCommand/ShutdownService).
# Bewusst eine Zusage statt des Ausbleibens von „Unknown command“: eine Ablehnung (features.shutdown:
# false) schreibt ebenfalls kein „Unknown command“ und wäre sonst 15 Sekunden Leerlauf.
_ANNOUNCE_OK_RE = re.compile(r"Herunterfahren angefordert")

# ---------------------------------------------------------------- Job-Objekt (Windows)
# Alle Server-Prozesse hängen an einem Job-Objekt mit KILL_ON_JOB_CLOSE: Endet der Manager –
# egal wie (Fenster zu, Task-Manager, Abmelden) – beendet Windows die Server mit. Ohne das
# liefen java.exe/bedrock_server.exe unsichtbar weiter und hielten den Port belegt.

_JOB = None
if sys.platform == "win32":
    import ctypes.wintypes as wt

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        _k32 = ctypes.windll.kernel32
        _k32.CreateJobObjectW.restype = wt.HANDLE
        _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        _JOB = _k32.CreateJobObjectW(None, None)
        _limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        _limits.BasicLimitInformation.LimitFlags = 0x2000        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(_JOB, 9, ctypes.byref(_limits), ctypes.sizeof(_limits)):
            _JOB = None                                           # 9 = JobObjectExtendedLimitInformation
    except (OSError, AttributeError):
        _JOB = None


def _bind_to_job(proc: subprocess.Popen) -> None:
    """Server-Prozess stirbt mit dem Manager – egal wie der Manager endet."""
    if _JOB:
        try:
            _k32.AssignProcessToJobObject(_JOB, wt.HANDLE(int(proc._handle)))  # noqa: SLF001
        except (OSError, AttributeError, ValueError):
            pass

# ---------------------------------------------------------------- Job-Verwaltung

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def new_job(server_id: str, steps: list[str]) -> dict:
    job = {
        "id": secrets.token_hex(4),
        "server_id": server_id,
        "status": "running",
        "steps": [{"text": t, "state": "todo"} for t in steps],
        "current": 0,
        "pct": 0,
        "detail": "",
        "error": "",
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
    return job


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def job_running(server_id: str) -> bool:
    """True, solange für diesen Server eine Installation läuft."""
    with _jobs_lock:
        return any(j["server_id"] == server_id and j["status"] == "running" for j in _jobs.values())


def _step(job: dict, index: int, detail: str = "") -> None:
    for i, s in enumerate(job["steps"]):
        if i < index:
            s["state"] = "done"
        elif i == index:
            s["state"] = "active"
    job["current"] = index
    job["detail"] = detail
    job["pct"] = int(index / max(1, len(job["steps"])) * 100)


def _finish(job: dict, error: str = "") -> None:
    if error:
        job["status"] = "error"
        job["error"] = error
        for s in job["steps"]:
            if s["state"] == "active":
                s["state"] = "failed"
    else:
        job["status"] = "done"
        job["pct"] = 100
        job["detail"] = "Fertig"
        for s in job["steps"]:
            s["state"] = "done"


def _progress_cb(job: dict, label: str):
    state = {"last": 0.0}

    def cb(done: int, total: int) -> None:
        now = time.time()
        if now - state["last"] < 0.25 and done != total:
            return
        state["last"] = now
        if total:
            job["detail"] = f"{label}: {done / 1048576:.1f} / {total / 1048576:.1f} MB"
        else:
            job["detail"] = f"{label}: {done / 1048576:.1f} MB"

    return cb


def _run_job(job: dict, work) -> dict:
    def run() -> None:
        try:
            work()
            _finish(job)
        except Exception as exc:                      # noqa: BLE001 - in der GUI anzeigen
            _finish(job, str(exc))

    threading.Thread(target=run, daemon=True).start()
    return job


# ---------------------------------------------------------------- server.properties

def patch_properties(path: pathlib.Path, updates: dict[str, str]) -> None:
    """Setzt Werte in einer .properties-Datei; Kommentare und Reihenfolge bleiben erhalten."""
    remaining = dict(updates)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

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
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def read_properties(path: pathlib.Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out.append((k.strip(), v.strip()))
    return out


def _bool(value) -> str:
    return "true" if value else "false"


def bedrock_udp_range(cfg: dict) -> tuple[int, int]:
    """UDP-Bereich für die NetherNet-Spieldaten eines Bedrock-Servers, beginnend 10 Ports über dem TCP-Port.
    BDS bindet pro Spieler-Sitzung einen eigenen UDP-Port aus diesem Fenster – deshalb wächst es mit max-players
    (mindestens 16 Ports)."""
    size = max(16, int(cfg.get("max_players") or 10) + 8)
    start = min(65535 - size + 1, int(cfg["port"]) + 10)
    return start, start + size - 1


def bedrock_udp_ports_value(cfg: dict, public_ip: str = "") -> str:
    """Wert für `server-udp-ports`. Ohne öffentliche IP kennt BDS nur seine eigenen (privaten) Adressen und
    bietet Internet-Spielern trotz Portfreigabe nur 192.168.x.x an – erst der Eintrag
    `<öffentliche IP>:extern:intern` veröffentlicht die Freigabe an die Clients."""
    lo, hi = bedrock_udp_range(cfg)
    value = f"{lo}-{hi}"
    if public_ip:
        value += f",{public_ip}:{lo}-{hi}:{lo}-{hi}"
    return value


_UPNP_BODY = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
              's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
              '<u:GetExternalIPAddress xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1"/>'
              '</s:Body></s:Envelope>').encode("utf-8")


def fritz_external_ip(timeout: float = 1.5) -> str:
    """Öffentliche IPv4 direkt vom Router (UPnP-IGD, ohne Fremddienst). Leer, wenn der Router nicht antwortet
    oder keine öffentliche IPv4 hat (DS-Lite, doppeltes NAT)."""
    for host in ("fritz.box", "192.168.178.1"):
        try:
            req = urllib.request.Request(
                f"http://{host}:49000/igdupnp/control/WANIPConn1", data=_UPNP_BODY,
                headers={"Content-Type": 'text/xml; charset="utf-8"',
                         "SOAPAction": '"urn:schemas-upnp-org:service:WANIPConnection:1#GetExternalIPAddress"'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read(20000).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        m = re.search(r"<NewExternalIPAddress>\s*([0-9.]+)\s*<", text)
        if m and is_public_ipv4(m.group(1)):
            return m.group(1)
    return ""


# ---------------------------------------------------------------- UPnP-Portfreigaben (FritzBox)
# Voraussetzung in der FritzBox: Heimnetz → Netzwerk → Netzwerkverbindungen → (dieser PC) →
# „Selbstständige Portfreigaben für dieses Gerät erlauben“. Dann darf der Manager Freigaben per
# UPnP-IGD (AddPortMapping/DeletePortMapping) selbst anlegen und entfernen.

_UPNP_SERVICE = "urn:schemas-upnp-org:service:WANIPConnection:1"
_UPNP_HINTS = {
    "606": "Nicht erlaubt – in der FritzBox unter Heimnetz → Netzwerk → Netzwerkverbindungen → diesen PC bearbeiten → "
           "„Selbstständige Portfreigaben für dieses Gerät erlauben“ aktivieren.",
    "718": "Port ist bereits für ein anderes Gerät freigegeben (Konflikt) – Freigabe in der FritzBox prüfen.",
    "729": "Konflikt mit einer bestehenden Freigabe in der FritzBox.",
    "725": "Router erlaubt nur dauerhafte Freigaben.",
    "402": "Ungültige Angaben (Port/Protokoll).",
    "403": "Die FritzBox lehnt die Freigabe ab (403). Meist fehlt eine öffentliche IPv4-Adresse (DS-Lite/CGNAT) – "
           "oder „Selbstständige Portfreigaben“ ist für diesen PC nicht erlaubt.",
}
NO_IPV4_HINT = ("Die FritzBox meldet keine öffentliche IPv4-Adresse (DS-Lite/CGNAT). IPv4-Portfreigaben sind an diesem "
                "Anschluss wirkungslos – Freunde aus dem Internet erreichen den Server nur per IPv6 (beide Seiten), über "
                "eine vom Anbieter freigeschaltete IPv4 oder über einen Tunnel-Dienst wie playit.gg.")


def _upnp_hosts() -> list[str]:
    parts = local_ip().split(".")
    guess = ".".join(parts[:3]) + ".1" if len(parts) == 4 else ""
    return [h for h in dict.fromkeys(["fritz.box", guess, "192.168.178.1"]) if h]


def upnp_call(action: str, args: dict, timeout: float = 3.0) -> tuple[bool, str]:
    """SOAP-Aufruf an den Internet-Gateway-Dienst des Routers → (ok, Antwort bzw. Fehlertext)."""
    inner = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in args.items())
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{_UPNP_SERVICE}">{inner}</u:{action}></s:Body></s:Envelope>').encode("utf-8")
    last = "Router antwortet nicht (UPnP aus oder kein FritzBox-Netz)."
    for host in _upnp_hosts():
        try:
            req = urllib.request.Request(
                f"http://{host}:49000/igdupnp/control/WANIPConn1", data=body,
                headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{_UPNP_SERVICE}#{action}"'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, resp.read(20000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            text = exc.read(20000).decode("utf-8", errors="replace")
            m = re.search(r"<errorCode>\s*(\d+)\s*<.*?<errorDescription>\s*(.*?)\s*<", text, re.S)
            code = m.group(1) if m else str(exc.code)
            return False, _UPNP_HINTS.get(code, f"UPnP-Fehler {code}: {m.group(2) if m else exc.reason}")
        except (OSError, ValueError) as exc:
            last = f"Router antwortet nicht: {exc}"
    return False, last


def portmap_entries(cfg: dict) -> list[tuple[str, int, str]]:
    """(Protokoll, Port, Bezeichnung) für jede benötigte Freigabe – Bereiche werden aufgelöst."""
    out = []
    label = re.sub(r"[^\w .-]", "", cfg["name"])[:20] or cfg["id"]
    for p in port_list(cfg):
        for port in range(int(p["from"]), int(p["to"]) + 1):
            out.append((p["proto"], port, f"MCSM {label} {p['proto']} {port}"))
    return out


def request_port_mappings(cfg: dict) -> dict:
    """Legt alle Freigaben des Servers per UPnP an (dauerhaft, auf die lokale IP dieses PCs)."""
    ip = local_ip()
    results = []
    for proto, port, desc in portmap_entries(cfg):
        ok, text = upnp_call("AddPortMapping", {
            "NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": proto, "NewInternalPort": port,
            "NewInternalClient": ip, "NewEnabled": "1", "NewPortMappingDescription": desc, "NewLeaseDuration": "0"})
        results.append({"proto": proto, "port": port, "ok": ok, "error": "" if ok else text})
        if not ok and "Nicht erlaubt" in text:
            break                                     # weitere Versuche wären sinnlos
    ext = fritz_external_ip()
    hint = "" if ext else NO_IPV4_HINT
    return {"ok": bool(results) and all(r["ok"] for r in results), "results": results,
            "internal_ip": ip, "external_ip": ext, "hint": hint}


def remove_port_mappings(cfg: dict) -> dict:
    results = []
    for proto, port, _desc in portmap_entries(cfg):
        ok, text = upnp_call("DeletePortMapping", {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": proto})
        results.append({"proto": proto, "port": port, "ok": ok, "error": "" if ok else text})
    return {"ok": all(r["ok"] for r in results), "results": results}


def is_public_ipv4(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global


def public_ipv4(cfg: dict) -> str:
    """Manuell eingetragene öffentliche IPv4, sonst die vom Router gemeldete (kann leer sein)."""
    manual = str(cfg.get("public_ip") or "")
    return manual if is_public_ipv4(manual) else fritz_external_ip()


def port_list(cfg: dict) -> list[dict]:
    """Alle Ports, die Spieler erreichen müssen – für Anzeige, Firewall und FritzBox-Anleitung."""
    if cfg["type"] == "bedrock":
        lo, hi = bedrock_udp_range(cfg)
        return [
            {"label": "Bedrock – Verbindung (Konsole, Handy, Windows-App)", "proto": "TCP",
             "from": int(cfg["port"]), "to": int(cfg["port"])},
            {"label": "Bedrock – Spieldaten (NetherNet)", "proto": "UDP", "from": lo, "to": hi},
        ]
    ports = [{"label": "Java Edition", "proto": "TCP", "from": int(cfg["port"]), "to": int(cfg["port"])}]
    if cfg.get("geyser"):
        ports.append({"label": "Bedrock (Konsole, Handy, Windows-App)", "proto": "UDP",
                      "from": int(cfg["bedrock_port"]), "to": int(cfg["bedrock_port"])})
    return ports


# Startwerte, die nur bei einer frischen Installation gesetzt werden. Danach gehören sie dem Nutzer
# (Dateien → server.properties) und werden beim Speichern der Einstellungen nicht mehr angefasst.
_INITIAL_PROPS = {
    "bedrock": {"allow-list": "false", "enable-lan-visibility": "true", "tick-distance": "4",
                "level-name": "Bedrock level", "default-player-permission-level": "member"},
    "java": {"white-list": "false", "spawn-protection": "0"},
}


def apply_config(cfg: dict, initial: bool = False) -> None:
    """Schreibt die GUI-Einstellungen in die Konfigurationsdateien des Servers.
    initial=True (frische Installation) setzt zusätzlich die Startwerte aus _INITIAL_PROPS."""
    sdir = store.server_dir(cfg["id"])
    if not sdir.exists():
        return
    props = sdir / "server.properties"

    if cfg["type"] == "bedrock":
        updates = {
            "server-name": cfg["motd"],
            "gamemode": cfg["gamemode"],
            "difficulty": cfg["difficulty"],
            "allow-cheats": _bool(cfg["allow_cheats"]),
            "max-players": str(cfg["max_players"]),
            "online-mode": _bool(cfg["online_mode"]),
            "server-port": str(cfg["port"]),
            "server-portv6": str(min(65535, cfg["port"] + 1)),
            # BDS 1.26.51+ unterstützt nur noch NetherNet: Verbindungsaufbau über TCP `server-port`
            # (Dual-Stack), Spieldaten über einen festen UDP-Bereich. Der Bereich wird hier gesetzt,
            # damit Firewall-Regel und FritzBox-Freigabe konkrete Ports nennen können; die öffentliche
            # Adresse wird bei jedem Start ergänzt (Instance.start).
            "transport": "nethernet",
            "server-udp-ports": bedrock_udp_ports_value(cfg, str(cfg.get("public_ip") or "")),
            "view-distance": str(cfg["view_distance"]),
            "level-seed": cfg["level_seed"],
        }
    else:
        updates = {
            "server-port": str(cfg["port"]),
            "motd": cfg["motd"],
            "max-players": str(cfg["max_players"]),
            "gamemode": cfg["gamemode"],
            "difficulty": cfg["difficulty"],
            "online-mode": _bool(cfg["online_mode"]),
            "view-distance": str(cfg["view_distance"]),
            "level-seed": cfg["level_seed"],
        }
        if sources.command_block_gamerule(cfg["version"]) is None:
            # Ab 1.21.9 sind das Gamerules (werden nach dem Start gesetzt, s. Instance._pump).
            updates["pvp"] = _bool(cfg["pvp"])
            updates["enable-command-block"] = _bool(cfg["allow_cheats"])
        if cfg.get("geyser") and not modpacks.is_modpack(cfg):
            # Bedrock-Spieler kommen über Floodgate herein und haben keine Mojang-Chatsignatur.
            # Steht „enforce-secure-profile" auf true, verwirft der Server ihre Chatnachrichten –
            # sie können dann zwar spielen, aber nichts schreiben. Deshalb bei Crossplay aus.
            updates["enforce-secure-profile"] = "false"
    if initial:
        updates.update(_INITIAL_PROPS[cfg["type"]])
    patch_properties(props, updates)
    if cfg["type"] == "java" and not modpacks.is_modpack(cfg):
        # Modpacks (Fabric/NeoForge …) bekommen weder Geyser-Konfiguration noch Plugin-Ordner-Pflege.
        if cfg.get("geyser"):
            write_geyser_config(cfg)
        else:
            remove_crossplay_plugins(sdir)
        if companion.applies(cfg) and (sdir / "plugins").exists():
            companion.write_config(cfg)
        ensure_server_icon(sdir)


SERVER_ICON = store.BASE / "assets" / "server-icon.png"


def ensure_server_icon(sdir: pathlib.Path) -> None:
    """Standardbild für die Serverliste setzen, solange der Besitzer keins hinterlegt hat.

    Minecraft zeigt `server-icon.png` (64x64) in der Mehrspieler-Liste. Ohne Datei bleibt dort das
    graue Standardbild. Ein eigenes Bild des Besitzers wird nie überschrieben – nur wenn gar keins
    da ist, legt der Manager seins hin.
    """
    ziel = sdir / "server-icon.png"
    if ziel.exists() or not SERVER_ICON.is_file():
        return
    try:
        shutil.copy2(SERVER_ICON, ziel)
    except OSError as exc:
        log.info("Serverbild konnte nicht gesetzt werden: %s", exc)


CROSSPLAY_JARS = ("Geyser-Spigot.jar", "floodgate-spigot.jar", "ViaVersion.jar", "ViaBackwards.jar")


def remove_crossplay_plugins(sdir: pathlib.Path) -> None:
    """Crossplay aus: die Plugin-JARs entfernen, sonst liefe Geyser weiter auf dem UDP-Port."""
    for name in CROSSPLAY_JARS:
        (sdir / "plugins" / name).unlink(missing_ok=True)


def _yaml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "").replace('"', "'") + '"'


def write_geyser_config(cfg: dict) -> None:
    """Minimale Geyser-Konfiguration; fehlende Schlüssel ergänzt Geyser selbst."""
    path = store.server_dir(cfg["id"]) / "plugins" / "Geyser-Spigot" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Von Minecraft Server Manager erzeugt - hier sind die Bedrock-Einstellungen.\n"
        "bedrock:\n"
        "  address: 0.0.0.0\n"
        f"  port: {cfg['bedrock_port']}\n"
        "  clone-remote-port: false\n"
        f"  motd1: {_yaml_str(cfg['motd'])}\n"
        f"  motd2: {_yaml_str(cfg['name'])}\n"
        f"  server-name: {_yaml_str(cfg['name'])}\n"
        "  compression-level: 6\n"
        "remote:\n"
        "  address: 127.0.0.1\n"
        f"  port: {cfg['port']}\n"
        "  auth-type: floodgate\n"
        "  use-proxy-protocol: false\n"
        f"max-players: {cfg['max_players']}\n"
        "passthrough-motd: false\n"
        "passthrough-player-counts: true\n"
        "debug-mode: false\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- Vereinfachter Config-Editor

# Beschreibungen der wichtigsten server.properties-Schlüssel (für das Formular in der GUI).
_YESNO = {"type": "bool"}
PROPS_META = {
    "bedrock": {
        "server-name": {"label": "Servername / MOTD", "desc": "Text, der in der Serverliste der Spieler steht.", "type": "text"},
        "gamemode": {"label": "Spielmodus", "desc": "Standard-Spielmodus für neue Spieler.", "type": "enum", "options": ["survival", "creative", "adventure"]},
        "force-gamemode": {"label": "Spielmodus erzwingen", "desc": "Setzt Spieler beim Beitritt immer auf den Standard-Spielmodus.", **_YESNO},
        "difficulty": {"label": "Schwierigkeit", "desc": "", "type": "enum", "options": ["peaceful", "easy", "normal", "hard"]},
        "allow-cheats": {"label": "Cheats erlauben", "desc": "Operatoren dürfen Befehle wie /gamemode oder /give nutzen.", **_YESNO},
        "max-players": {"label": "Maximale Spieler", "desc": "", "type": "int", "min": 1, "max": 200},
        "online-mode": {"label": "Xbox-Konto erforderlich", "desc": "Spieler werden bei Xbox Live geprüft. Empfohlen: ja.", **_YESNO},
        "allow-list": {"label": "Nur eingetragene Spieler", "desc": "Wenn ja, dürfen nur Spieler aus allowlist.json beitreten.", **_YESNO},
        "server-port": {"label": "Port (TCP, Verbindungsaufbau)", "desc": "Standard 19132.", "type": "int", "min": 1024, "max": 65535},
        "server-portv6": {"label": "Port (IPv6)", "desc": "Wird vom Manager gesetzt (Port + 1); bei NetherNet ohne Bedeutung.", "type": "int", "min": 1024, "max": 65535, "managed": True},
        "enable-lan-visibility": {"label": "Im LAN sichtbar", "desc": "Server erscheint bei Spielern im selben Netz unter „Freunde“ / LAN.", **_YESNO},
        "transport": {"label": "Transport", "desc": "Wird vom Manager gesetzt – ab 1.26.51 wird nur noch nethernet unterstützt.", "type": "enum", "options": ["nethernet", "raknet"], "managed": True},
        "server-udp-ports": {"label": "UDP-Bereich für Spieldaten", "desc": "Wird vom Manager aus Port, Spieleranzahl und öffentlicher IP gesetzt (Einstellungen). Diese Ports müssen in Firewall und Router freigegeben sein.", "type": "text", "managed": True},
        "view-distance": {"label": "Sichtweite (Chunks)", "desc": "Höher = mehr Last für den PC.", "type": "int", "min": 4, "max": 32},
        "tick-distance": {"label": "Tick-Distanz", "desc": "Wie weit um Spieler herum die Welt „lebt“ (4–12).", "type": "int", "min": 4, "max": 12},
        "player-idle-timeout": {"label": "Kick bei Inaktivität (Min.)", "desc": "0 = nie.", "type": "int", "min": 0, "max": 1440},
        "max-threads": {"label": "Max. Threads", "desc": "0 = so viele wie möglich.", "type": "int", "min": 0, "max": 64},
        "level-name": {"label": "Weltname", "desc": "Ordnername unter worlds/. Anderer Name = andere Welt.", "type": "text"},
        "level-seed": {"label": "Seed", "desc": "Nur für neue Welten wirksam. Buchstaben, Ziffern, Leerzeichen und Bindestrich (max. 40 Zeichen).", "type": "text"},
        "default-player-permission-level": {"label": "Rechte neuer Spieler", "desc": "", "type": "enum", "options": ["visitor", "member", "operator"]},
        "texturepack-required": {"label": "Texturenpaket erzwingen", "desc": "", **_YESNO},
        "content-log-file-enabled": {"label": "Content-Log schreiben", "desc": "Für Fehlersuche bei Add-ons.", **_YESNO},
        "server-authoritative-movement": {"label": "Bewegungs-Prüfung", "desc": "Anti-Cheat für Bewegung (Standard: server-auth).", "type": "text"},
        "chat-restriction": {"label": "Chat-Einschränkung", "desc": "", "type": "enum", "options": ["None", "Dropped", "Disabled"]},
        "disable-player-interaction": {"label": "Spieler-Interaktion aus", "desc": "", **_YESNO},
        "client-side-chunk-generation-enabled": {"label": "Client-Chunk-Generierung", "desc": "Entlastet den Server.", **_YESNO},
        "emit-server-telemetry": {"label": "Telemetrie an Mojang", "desc": "", **_YESNO},
    },
    "java": {
        "motd": {"label": "MOTD", "desc": "Text in der Serverliste. Farben mit §-Codes möglich.", "type": "text"},
        "server-port": {"label": "Port (TCP)", "desc": "Standard 25565.", "type": "int", "min": 1024, "max": 65535},
        "max-players": {"label": "Maximale Spieler", "desc": "", "type": "int", "min": 1, "max": 200},
        "gamemode": {"label": "Spielmodus", "desc": "", "type": "enum", "options": ["survival", "creative", "adventure"]},
        "force-gamemode": {"label": "Spielmodus erzwingen", "desc": "", **_YESNO},
        "difficulty": {"label": "Schwierigkeit", "desc": "", "type": "enum", "options": ["peaceful", "easy", "normal", "hard"]},
        "hardcore": {"label": "Hardcore", "desc": "Nach dem Tod nur noch Zuschauer.", **_YESNO},
        "online-mode": {"label": "Konto-Prüfung (online-mode)", "desc": "Java-Spieler werden bei Mojang geprüft. Für Crossplay auf ja lassen.", **_YESNO},
        "pvp": {"label": "PvP", "desc": "", **_YESNO},
        "view-distance": {"label": "Sichtweite (Chunks)", "desc": "", "type": "int", "min": 4, "max": 32},
        "simulation-distance": {"label": "Simulationsweite", "desc": "Wie weit Mobs/Farmen aktiv bleiben.", "type": "int", "min": 2, "max": 32},
        "level-name": {"label": "Weltordner", "desc": "Anderer Name = andere Welt.", "type": "text"},
        "level-seed": {"label": "Seed", "desc": "Nur für neue Welten. Buchstaben, Ziffern, Leerzeichen und Bindestrich (max. 40 Zeichen).", "type": "text"},
        "level-type": {"label": "Welttyp", "desc": "", "type": "enum", "options": ["minecraft:normal", "minecraft:flat", "minecraft:large_biomes", "minecraft:amplified"]},
        "spawn-protection": {"label": "Spawn-Schutz (Blöcke)", "desc": "0 = aus.", "type": "int", "min": 0, "max": 100},
        "white-list": {"label": "Whitelist aktiv", "desc": "Nur Spieler aus whitelist.json dürfen rein.", **_YESNO},
        "enforce-whitelist": {"label": "Whitelist erzwingen", "desc": "Kickt Spieler, die nicht mehr auf der Liste stehen.", **_YESNO},
        "enable-command-block": {"label": "Befehlsblöcke", "desc": "", **_YESNO},
        "allow-flight": {"label": "Fliegen erlauben", "desc": "Verhindert Kicks bei Mods/Elytra-Bugs.", **_YESNO},
        "allow-nether": {"label": "Nether erlauben", "desc": "", **_YESNO},
        "spawn-monsters": {"label": "Monster spawnen", "desc": "", **_YESNO},
        "spawn-animals": {"label": "Tiere spawnen", "desc": "", **_YESNO},
        "spawn-npcs": {"label": "Dorfbewohner spawnen", "desc": "", **_YESNO},
        "generate-structures": {"label": "Strukturen generieren", "desc": "Dörfer, Tempel usw.", **_YESNO},
        "max-world-size": {"label": "Max. Weltgröße (Radius)", "desc": "", "type": "int", "min": 1, "max": 29999984},
        "player-idle-timeout": {"label": "Kick bei Inaktivität (Min.)", "desc": "0 = nie.", "type": "int", "min": 0, "max": 1440},
        "op-permission-level": {"label": "OP-Rechte-Stufe", "desc": "1–4, 4 = alles.", "type": "int", "min": 1, "max": 4},
        "hide-online-players": {"label": "Spielerliste verbergen", "desc": "", **_YESNO},
        "enforce-secure-profile": {"label": "Signierte Chats erzwingen",
                              "desc": "Muss bei Crossplay auf nein stehen – sonst können Bedrock-Spieler nicht im Chat schreiben. Der Manager setzt das automatisch.", **_YESNO},
        "enable-query": {"label": "Query-Protokoll", "desc": "Für Server-Listen-Abfragen.", **_YESNO},
        "enable-rcon": {"label": "RCON (Fernsteuerung)", "desc": "Nur aktivieren, wenn du es brauchst.", **_YESNO},
        "network-compression-threshold": {"label": "Netzwerk-Kompression ab (Bytes)", "desc": "", "type": "int", "min": -1, "max": 65535},
        "resource-pack": {"label": "Ressourcenpaket-URL", "desc": "", "type": "text"},
        "require-resource-pack": {"label": "Ressourcenpaket erzwingen", "desc": "", **_YESNO},
        "pause-when-empty-seconds": {"label": "Pause bei leerem Server (Sek.)", "desc": "Spart CPU, wenn niemand da ist.", "type": "int", "min": -1, "max": 86400},
        "accepts-transfers": {"label": "Transfers annehmen", "desc": "", **_YESNO},
    },
}

# Schlüssel, die auch in der Manager-Konfiguration gespiegelt werden.
_PROPS_TO_CFG = {
    "bedrock": {"server-name": "motd", "server-port": "port", "max-players": "max_players", "gamemode": "gamemode",
                "difficulty": "difficulty", "online-mode": "online_mode", "view-distance": "view_distance",
                "level-seed": "level_seed", "allow-cheats": "allow_cheats"},
    "java": {"motd": "motd", "server-port": "port", "max-players": "max_players", "gamemode": "gamemode",
             "difficulty": "difficulty", "online-mode": "online_mode", "view-distance": "view_distance",
             "level-seed": "level_seed", "enable-command-block": "allow_cheats", "pvp": "pvp"},
}


def read_props(cfg: dict) -> dict:
    """server.properties als Liste beschriebener Felder für das Formular."""
    path = store.server_dir(cfg["id"]) / "server.properties"
    meta = PROPS_META.get(cfg["type"], {})
    fields = []
    for key, value in read_properties(path):
        m = meta.get(key)
        if m:
            field = {"key": key, "value": value, "known": True, **m}
        else:
            t = "bool" if value in ("true", "false") else ("int" if re.fullmatch(r"-?\d+", value) else "text")
            field = {"key": key, "value": value, "known": False, "label": key, "desc": "", "type": t}
        fields.append(field)
    return {"exists": path.exists(), "fields": fields}


def _validate_prop(meta: dict, key: str, text: str) -> None:
    """Prüft einen Formularwert gegen PROPS_META (Zahlenbereich, Auswahl, Ja/Nein)."""
    label = meta.get("label") or key
    kind = meta.get("type")
    if kind == "int":
        if not re.fullmatch(r"-?\d+", text):
            raise ValueError(f"„{label}“: Bitte eine ganze Zahl eingeben.")
        lo, hi = meta.get("min"), meta.get("max")
        if (lo is not None and int(text) < lo) or (hi is not None and int(text) > hi):
            raise ValueError(f"„{label}“: Erlaubt sind Werte von {lo} bis {hi}.")
    elif kind == "enum" and text not in (meta.get("options") or []):
        raise ValueError(f"„{label}“: Erlaubt ist nur {', '.join(meta.get('options') or [])}.")
    elif kind == "bool" and text not in ("true", "false"):
        raise ValueError(f"„{label}“: Bitte Ja oder Nein wählen.")


def _mirror_to_cfg(cfg: dict, values: dict[str, str]) -> tuple[dict, dict]:
    """Spiegelt bekannte server.properties-Werte in die Manager-Konfiguration.
    Liefert (neue Konfiguration, Rohwerte) – gespeichert wird nur, wenn Rohwerte dabei waren."""
    mirror = _PROPS_TO_CFG.get(cfg["type"], {})
    raw = {}
    for pkey, ckey in mirror.items():
        if pkey in values:
            v = values[pkey]
            raw[ckey] = (v == "true") if ckey in ("online_mode", "allow_cheats", "pvp") else v
    if not raw:
        return cfg, raw
    return store.sanitize(raw, existing=cfg), raw


def write_props(cfg: dict, values: dict) -> dict:
    """Schreibt Formularwerte in server.properties und spiegelt bekannte Werte in die Manager-Konfiguration.
    Ungültige Werte werden mit einer Meldung abgewiesen, bevor etwas geschrieben wird."""
    if not isinstance(values, dict):
        raise ValueError("Ungültige Daten.")
    meta = PROPS_META.get(cfg["type"], {})
    clean: dict[str, str] = {}
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", str(key)):
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        text = str(value).replace("\r", "").replace("\n", " ").strip()[:512]
        m = meta.get(key)
        if m:
            if m.get("managed"):                       # setzt der Manager selbst (Port, Transport, UDP-Bereich)
                continue
            _validate_prop(m, key, text)
        clean[key] = text

    # Spiegelung prüfen: Was die Manager-Konfiguration nicht 1:1 übernehmen kann (z. B. '=' im MOTD,
    # Sonderzeichen im Seed), wird abgewiesen – sonst zeigten Übersicht und Einstellungen andere Werte
    # als die Datei und der nächste „Einstellungen speichern“ überschriebe die Datei still.
    new_cfg, raw = _mirror_to_cfg(cfg, clean)
    for pkey, ckey in _PROPS_TO_CFG.get(cfg["type"], {}).items():
        if pkey in clean:
            got = new_cfg[ckey]
            got_text = _bool(got) if isinstance(got, bool) else str(got)
            if got_text != clean[pkey]:
                label = meta.get(pkey, {}).get("label") or pkey
                hint = f" Möglich wäre z. B. „{got_text}“." if got_text else ""
                raise ValueError(f"„{label}“: Dieser Wert wird vom Manager nicht unterstützt.{hint}")

    patch_properties(store.server_dir(cfg["id"]) / "server.properties", clean)
    if raw:
        store.save(new_cfg)
    return new_cfg


# ---------------------------------------------------------------- Dateien

TEXT_EXT = {".properties", ".yml", ".yaml", ".json", ".txt", ".toml", ".cfg", ".ini", ".conf", ".log",
            ".md", ".mcmeta", ".lang", ".csv", ".wlist"}
MAX_EDIT_BYTES = 1_500_000

_HIDDEN_NAMES = {
    "libraries", "versions", "cache", ".paper-remapped", "bundler", "server.jar", "build.txt",
    "definitions", "minecraftpe", "treatments", "world_templates", "internalStorage",
    "development_behavior_packs", "development_resource_packs", "development_skin_packs",
    "bedrock_server.exe", "bedrock_server_how_to.html", "release-notes.txt", "profanity_filter.wlist",
    "valid_known_packs.json", "Dedicated_Server.txt", "usercache.json", "version_history.json",
    "session.lock", "uid.dat", ".console_history", "logs.old",
}
_HIDDEN_EXT = {".exe", ".dll", ".pdb", ".jar", ".lock", ".dat_old", ".bak"}
_WORLD_DIRS = {"worlds", "world", "world_nether", "world_the_end"}
# Technik der Mod-Loader (Startskripte, Argument-Dateien, Launcher-Einstellungen) – nur mit „Alle Dateien“.
_TECH_NAMES = set(modpacks.TECH_FILES)


def safe_path(cfg: dict, rel: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Löst einen relativen Pfad innerhalb des Server-Ordners auf (kein Ausbruch möglich)."""
    root = store.server_dir(cfg["id"]).resolve()
    rel = (rel or "").replace("\\", "/").strip("/")
    if rel in ("", "."):
        return root, root
    if ".." in rel.split("/") or ":" in rel:
        raise ValueError("Ungültiger Pfad.")
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Ungültiger Pfad.") from exc
    return root, target


def _kind(entry: pathlib.Path, rel: str) -> str:
    name = entry.name.lower()
    parts = rel.replace("\\", "/").split("/")
    if entry.is_dir():
        if name in _WORLD_DIRS or (entry / "level.dat").exists() or (entry / "levelname.txt").exists():
            return "world"
        if name in ("plugins", "behavior_packs", "resource_packs", "config", "datapacks", "mods"):
            return "plugins" if name in ("plugins", "mods") else "config"
        if name == "backups":
            return "backup"
        if name == "logs":
            return "log"
        return "folder"
    ext = entry.suffix.lower()
    if name.endswith(".log.gz") or ext == ".log":
        return "log"
    if ext == ".zip" and parts[0] == "backups":
        return "backup"
    if ext == ".jar" and ("plugins" in parts or "mods" in parts):
        return "plugin"                                # Mods eines Modpacks zählen wie Plugins
    if entry.name in _TECH_NAMES and len(parts) == 1:
        return "other"                                 # Loader-Technik im Server-Ordner (run.bat, user_jvm_args.txt …)
    if ext in TEXT_EXT:
        return "config"
    return "other"


def _hidden(entry: pathlib.Path, kind: str) -> bool:
    if kind in ("world", "plugins", "config", "backup", "log", "plugin"):
        return False
    return (entry.name in _HIDDEN_NAMES or entry.name in _TECH_NAMES or entry.suffix.lower() in _HIDDEN_EXT
            or entry.name.startswith("."))


def _dir_size(path: pathlib.Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def list_dir(cfg: dict, rel: str) -> dict:
    root, target = safe_path(cfg, rel)
    if not target.is_dir():
        raise ValueError("Ordner nicht gefunden.")
    entries = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rel_path = entry.relative_to(root).as_posix()
        kind = _kind(entry, rel_path)
        try:
            st = entry.stat()
        except OSError:
            continue
        entries.append({
            "name": entry.name,
            "path": rel_path,
            "is_dir": entry.is_dir(),
            "size": st.st_size if entry.is_file() else None,
            "mtime": int(st.st_mtime),
            "kind": kind,
            "hidden": _hidden(entry, kind),
            "editable": entry.is_file() and entry.suffix.lower() in TEXT_EXT and st.st_size <= MAX_EDIT_BYTES,
        })
    return {"path": target.relative_to(root).as_posix() if target != root else "", "entries": entries}


def read_file(cfg: dict, rel: str) -> dict:
    root, target = safe_path(cfg, rel)
    if not target.is_file():
        raise ValueError("Datei nicht gefunden.")
    if target.suffix.lower() not in TEXT_EXT:
        raise ValueError("Diese Datei ist keine Textdatei und kann hier nicht angezeigt werden.")
    if target.stat().st_size > MAX_EDIT_BYTES:
        raise ValueError("Die Datei ist zu groß für den Editor (max. 1,5 MB). Bitte im Explorer öffnen.")
    data = target.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError("Diese Datei ist binär und kann hier nicht angezeigt werden.")
    return {"path": target.relative_to(root).as_posix(), "content": data.decode("utf-8", errors="replace"),
            "size": len(data), "name": target.name}


def write_file(cfg: dict, rel: str, content: str) -> dict:
    root, target = safe_path(cfg, rel)
    if target.suffix.lower() not in TEXT_EXT:
        raise ValueError("Nur Textdateien (.properties, .yml, .json, .txt …) können gespeichert werden.")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise ValueError("Inhalt fehlt oder ist zu groß.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.copy2(target, target.with_name(target.name + ".bak"))
    target.write_text(content.replace("\r\n", "\n"), encoding="utf-8")
    if target.parent == root and target.name == "server.properties":
        # Roh bearbeitete server.properties: Port, MOTD usw. in die Manager-Konfiguration übernehmen,
        # damit Übersicht, Firewall-Befehle und Port-Prüfung zur Datei passen.
        new_cfg, raw = _mirror_to_cfg(cfg, dict(read_properties(target)))
        if raw:
            store.save(new_cfg)
    return {"path": target.relative_to(root).as_posix(), "size": target.stat().st_size}


def worlds(cfg: dict) -> list[dict]:
    root = store.server_dir(cfg["id"])
    found: list[pathlib.Path] = []
    if cfg["type"] == "bedrock":
        wdir = root / "worlds"
        if wdir.is_dir():
            found = [p for p in sorted(wdir.iterdir()) if p.is_dir()]
    else:
        found = [p for p in sorted(root.iterdir()) if p.is_dir() and (p / "level.dat").exists()]
    out = []
    for p in found:
        try:
            mtime = int(p.stat().st_mtime)
        except OSError:
            mtime = 0
        out.append({"name": p.name, "path": p.relative_to(root).as_posix(), "size": _dir_size(p), "mtime": mtime})
    return out


def backup_worlds(cfg: dict) -> dict:
    if is_running(cfg["id"]):
        raise ValueError("Bitte den Server zuerst stoppen, damit die Welt vollständig gespeichert ist.")
    root = store.server_dir(cfg["id"])
    ws = worlds(cfg)
    if not ws:
        raise ValueError("Es gibt noch keine Welt – der Server muss mindestens einmal gestartet worden sein.")
    bdir = root / "backups"
    bdir.mkdir(exist_ok=True)
    target = bdir / f"welten-{time.strftime('%Y-%m-%d_%H-%M-%S')}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for w in ws:
            wpath = root / w["path"]
            for p in wpath.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(root).as_posix())
    return {"file": target.name, "size": target.stat().st_size}


def backups(cfg: dict) -> list[dict]:
    bdir = store.server_dir(cfg["id"]) / "backups"
    if not bdir.is_dir():
        return []
    out = []
    for p in sorted(bdir.glob("*.zip"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def open_in_explorer(cfg: dict, rel: str) -> None:
    _, target = safe_path(cfg, rel)
    if target.is_dir():
        subprocess.Popen(["explorer", str(target)])
    elif target.exists():
        subprocess.Popen(["explorer", "/select,", str(target)])


# ---------------------------------------------------------------- Java-Runtime

def _find_java_in(root: pathlib.Path) -> pathlib.Path | None:
    if not root.exists():
        return None
    direct = root / "bin" / "java.exe"
    if direct.exists():
        return direct
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "bin" / "java.exe").exists():
            return child / "bin" / "java.exe"
    return None


def java_path(major: int) -> pathlib.Path | None:
    """Bereits vorhandene, funktionsfähige Java-Runtime für die geforderte Major-Version."""
    bundled = _find_java_in(store.RUNTIME_DIR / f"jre{major}")
    # Nur akzeptieren, wenn java.exe wirklich startet (unvollständig entpackte Runtime erkennen).
    if bundled and _java_major_of(bundled) == major:
        return bundled
    system = shutil.which("java")
    if system and _java_major_of(pathlib.Path(system)) == major:
        return pathlib.Path(system)
    return None


def _java_major_of(exe: pathlib.Path) -> int | None:
    try:
        res = subprocess.run([str(exe), "-version"], capture_output=True, text=True,
                             timeout=15, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', (res.stderr or "") + (res.stdout or ""))
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def _status_cb(job: dict):
    def cb(text: str) -> None:
        job["detail"] = text
    return cb


def _open_zip(archive: pathlib.Path) -> zipfile.ZipFile:
    """Öffnet ein Archiv aus dem Cache; ein beschädigtes (z. B. abgebrochener Download)
    wird gelöscht, damit der nächste Versuch es neu lädt."""
    try:
        zf = zipfile.ZipFile(archive)
        if zf.testzip() is not None:
            zf.close()
            raise zipfile.BadZipFile("CRC-Fehler")
        return zf
    except (zipfile.BadZipFile, OSError) as exc:
        archive.unlink(missing_ok=True)
        raise sources.SourceError(
            f"Das Archiv {archive.name} war beschädigt und wurde entfernt – bitte erneut versuchen.") from exc


def ensure_java(major: int, job: dict, step_index: int) -> pathlib.Path:
    existing = java_path(major)
    if existing:
        _step(job, step_index, f"Java {major} ist bereits vorhanden")
        return existing

    _step(job, step_index, f"Java {major} wird von Adoptium geladen …")
    url, release = sources.adoptium_jre(major)
    archive = store.CACHE_DIR / f"jre{major}-{release.replace('+', '_')}.zip"
    if not archive.exists():
        sources.download(url, archive, _progress_cb(job, f"Java {major}"), _status_cb(job))

    target = store.RUNTIME_DIR / f"jre{major}"
    job["detail"] = f"Java {major} wird entpackt …"
    # Erst in einen Temp-Ordner entpacken und prüfen, dann tauschen – so bleibt nie eine
    # halb entpackte Runtime liegen, die zwar java.exe hat, aber nicht startet.
    tmp_dir = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    with _open_zip(archive) as zf:
        zf.extractall(tmp_dir)

    java = _find_java_in(tmp_dir)
    if not java or _java_major_of(java) != major:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise sources.SourceError("Die Java-Runtime konnte nicht entpackt werden – bitte erneut versuchen.")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp_dir, target)
    java = _find_java_in(target)
    if not java:
        raise sources.SourceError("Die Java-Runtime konnte nicht abgelegt werden.")
    return java


def required_java(cfg: dict) -> int:
    return int(cfg.get("java_major") or sources.java_major_for(cfg["version"]))


# ---------------------------------------------------------------- Visual C++ Runtime (Bedrock)

VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def vc_redist_present() -> bool:
    sysdir = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return all((sysdir / n).exists() for n in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"))


def ensure_vc_redist(job: dict, step_index: int) -> None:
    """Der Bedrock-Server braucht die Visual-C++-Laufzeit; fehlt sie, wird sie installiert (UAC-Abfrage)."""
    if sys.platform != "win32" or vc_redist_present():
        _step(job, step_index, "Visual C++ Laufzeit ist vorhanden")
        return
    _step(job, step_index, "Visual C++ Laufzeit wird geladen …")
    installer = store.CACHE_DIR / "vc_redist.x64.exe"
    if not installer.exists():
        sources.download(VC_REDIST_URL, installer, _progress_cb(job, "VC++ Laufzeit"))
    job["detail"] = "Bitte die Windows-Abfrage (UAC) für die VC++-Installation bestätigen …"
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Start-Process -FilePath '{installer}' -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait"],
            timeout=600, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise sources.SourceError(f"VC++-Laufzeit konnte nicht installiert werden: {exc}") from exc
    if not vc_redist_present():
        raise sources.SourceError(
            "Die Visual C++ Laufzeit fehlt weiterhin. Bitte manuell installieren: " + VC_REDIST_URL)


# ---------------------------------------------------------------- Xbox-Freunde-Modus (MCXboxBroadcast)
# Ein Bot-Konto meldet sich bei Xbox Live an und zeigt den Server allen Freunden dieses Kontos
# als beitretbare Welt – Konsolen treten dann einfach über „Freunde → Beitreten“ bei.

_XBOX_CODE_RE = re.compile(r"(https://(?:www\.)?microsoft\.com/link)\S*.*?\bcode\s+([A-Z0-9]{6,12})", re.I)
# Meldungen des Standalone (SessionManagerCore/StandaloneMain), sobald die Session bei Xbox Live steht.
_XBOX_ONLINE_RE = re.compile(r"Creation of Xbox LIVE session was successful|NetherNet Broadcaster started on ID"
                             r"|Session recreated after device token refresh|Updated session!", re.I)
_XBOX_USER_RE = re.compile(r"(?:authenticated|logged in|signed in)\s+as\s+([^\s(]+)", re.I)
# Harmlos: der RakNet-Ping klappt gegen einen gestoppten Server (und nie gegen NetherNet-BDS).
_XBOX_IGNORE_RE = re.compile(r"Failed to ping server", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def xbox_dir(cfg: dict) -> pathlib.Path:
    return store.server_dir(cfg["id"]) / "xbox"


def xbox_jar() -> pathlib.Path | None:
    jars = sorted(store.CACHE_DIR.glob("MCXboxBroadcastStandalone-*.jar"), key=lambda p: p.stat().st_mtime)
    return jars[-1] if jars else None


def xbox_java() -> pathlib.Path | None:
    """Irgendeine vorhandene Java-Runtime ab 17 (MCXboxBroadcast braucht kein bestimmtes Java)."""
    for major in (25, 21, 17):
        path = java_path(major)
        if path:
            return path
    return None


def xbox_bedrock_port(cfg: dict) -> int:
    return int(cfg["port"] if cfg["type"] == "bedrock" else cfg["bedrock_port"])


def xbox_address(cfg: dict) -> str:
    return cfg.get("xbox_address") or local_ip()


def write_xbox_config(cfg: dict) -> pathlib.Path:
    """config.yml für MCXboxBroadcast Standalone (Schlüssel laut CoreConfig des Projekts)."""
    xdir = xbox_dir(cfg)
    xdir.mkdir(parents=True, exist_ok=True)
    host = cfg.get("xbox_host_name") or cfg["name"]
    path = xdir / "config.yml"
    # Der Ping des Bots ist ein RakNet-Ping (UDP) – ein NetherNet-BDS antwortet darauf nie, der Bot
    # protokollierte alle 30 s einen Fehler samt Stacktrace. Bei Bedrock also nur die Werte aus dieser
    # Datei nutzen; Geyser (Java) beantwortet den Ping.
    path.write_text(
        "# Von Minecraft Server Manager erzeugt. Änderungen hier werden beim nächsten Start überschrieben –\n"
        "# Adresse und Anzeigename bitte in der Oberfläche unter „Xbox-Freunde-Modus“ ändern.\n"
        "session:\n"
        "  update-interval: 30\n"
        f"  query-server: {_bool(cfg['type'] != 'bedrock')}\n"
        "  web-query-fallback: false\n"
        "  config-fallback: true\n"
        "  session-info:\n"
        f"    host-name: {_yaml_str(host)}\n"
        f"    world-name: {_yaml_str(cfg['motd'])}\n"
        "    players: 0\n"
        f"    max-players: {int(cfg['max_players'])}\n"
        f"    ip: {_yaml_str(xbox_address(cfg))}\n"
        f"    port: {xbox_bedrock_port(cfg)}\n"
        "  ice-port-range:\n"
        "    min: 0\n"
        "    max: 0\n"
        "friend-sync:\n"
        "  update-interval: 60\n"
        "  auto-follow: true\n"
        "  auto-unfollow: false\n"
        "  initial-invite: true\n"
        "  expiry:\n"
        "    enabled: false\n"
        "    days: 15\n"
        "    check: 1800\n"
        "notifications:\n"
        "  enabled: false\n"
        "  webhook-url: \"\"\n"
        "debug-mode: false\n"
        "suppress-session-update-message: true\n",
        encoding="utf-8",
    )
    return path


def xbox_setup_async(cfg: dict) -> dict:
    """Lädt Java (falls nötig) und MCXboxBroadcast, schreibt die Konfiguration und startet den Bot."""
    job = new_job(cfg["id"], ["Java prüfen", "MCXboxBroadcast laden", "Konfiguration schreiben", "Bot starten"])

    def work() -> None:
        if xbox_java():
            _step(job, 0, "Java ist vorhanden")
        else:
            ensure_java(21, job, 0)
        _step(job, 1, "MCXboxBroadcast wird geladen …")
        url, tag = sources.xbox_broadcast_download()
        jar = store.CACHE_DIR / f"MCXboxBroadcastStandalone-{tag}.jar"
        if not jar.exists():
            sources.download(url, jar, _progress_cb(job, "MCXboxBroadcast"), _status_cb(job))
        _step(job, 2, "Konfiguration wird geschrieben …")
        write_xbox_config(cfg)
        bot = broadcaster(cfg)
        # MCXboxBroadcast liest config.yml nur beim Start – ein laufender Bot wird für neue Werte neu gestartet.
        _step(job, 3, "Bot wird neu gestartet …" if bot.restart_needed else "Bot wird gestartet …")
        bot.start()

    return _run_job(job, work)


def xbox_status(cfg: dict) -> dict:
    """Zustand des Bots: off | starting | login | online (vom Bot-Prozess aus seiner Ausgabe mitgeführt)."""
    with _inst_lock:
        inst = _instances.get(f"{cfg['id']}:xbox")
    running = bool(inst and inst.running)
    status = {
        "enabled": bool(cfg.get("xbox_enabled")),
        "installed": xbox_jar() is not None and xbox_java() is not None,
        "running": running,
        "state": "off",
        "code": "", "url": "", "gamertag": "", "error": "",
        "address": xbox_address(cfg), "port": xbox_bedrock_port(cfg),
        "host_name": cfg.get("xbox_host_name") or cfg["name"],
        # Die Anmeldung liegt in cache.json; player_history.db legt der Bot schon vor dem Login an.
        "token_cached": (xbox_dir(cfg) / "cache" / "cache.json").is_file(),
    }
    if running and isinstance(inst, Broadcaster):
        st = inst.status
        status.update(state=st["state"], code=st["code"], url=st["url"], gamertag=st["gamertag"], error=st["error"])
    return status


def xbox_reset(cfg: dict) -> None:
    """Anmeldung verwerfen (Token-Cache löschen) – beim nächsten Start wird ein neuer Code angezeigt."""
    broadcaster(cfg).stop()
    shutil.rmtree(xbox_dir(cfg) / "cache", ignore_errors=True)


def start_broadcaster_if_enabled(cfg: dict) -> None:
    if not (cfg.get("xbox_enabled") and cfg.get("xbox_autostart")):
        return
    try:
        broadcaster(cfg).start()
    except RuntimeError:
        pass                                          # noch nicht eingerichtet – Status zeigt es an


def stop_broadcaster(cfg: dict) -> None:
    with _inst_lock:
        inst = _instances.get(f"{cfg['id']}:xbox")
    if inst:
        inst.stop(timeout=15)


# ---------------------------------------------------------------- Installation

KEEP_ON_UPDATE = ("server.properties", "permissions.json", "allowlist.json", "whitelist.json")


def _extract_bds(archive: pathlib.Path, target: pathlib.Path, job: dict) -> None:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with _open_zip(archive) as zf:
        members = zf.namelist()
        for i, member in enumerate(members):
            if member.endswith("/"):
                continue
            dest = (target / member).resolve()
            if root not in dest.parents:              # Zip-Slip abfangen
                continue
            # Vorhandene Welten und Konfiguration bei einem Update nicht überschreiben.
            top = member.split("/", 1)[0]
            if top == "worlds" and dest.exists():
                continue
            if member in KEEP_ON_UPDATE and dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
            if i % 40 == 0:
                job["detail"] = f"Entpacke … {i}/{len(members)}"


def install(cfg: dict, job: dict) -> None:
    sdir = store.server_dir(cfg["id"])
    sdir.mkdir(parents=True, exist_ok=True)
    # Frische Installation (keine server.properties): Startwerte setzen. Bei „Neu installieren“ bleiben
    # die vom Nutzer bearbeiteten Werte erhalten.
    fresh = not (sdir / "server.properties").exists()

    if cfg["type"] == "bedrock":
        ensure_vc_redist(job, 0)

        _step(job, 1, "Bedrock Dedicated Server wird geladen …")
        url = sources.bedrock_download_url(cfg["version"])
        archive = store.CACHE_DIR / f"bedrock-server-{cfg['version']}.zip"
        if not archive.exists():
            sources.download(url, archive, _progress_cb(job, "Bedrock Server"), _status_cb(job))

        _step(job, 2, "Server wird entpackt …")
        _extract_bds(archive, sdir, job)
        if not (sdir / "bedrock_server.exe").exists():
            raise sources.SourceError("bedrock_server.exe wurde im Archiv nicht gefunden.")

        _step(job, 3, "Einstellungen werden geschrieben …")
        apply_config(cfg, initial=fresh)
    elif modpacks.is_modpack(cfg):
        # Versionswechsel: bis zum Erfolg als „nicht installiert“ führen – schlägt der Loader fehl, darf
        # kein Start mit der halb umgestellten Installation möglich sein.
        prev_vid = modpacks.installed_index(sdir).get("version_id")
        if prev_vid and prev_vid != (cfg.get("modpack") or {}).get("version_id"):
            current = store.get(cfg["id"])
            if current:
                current["installed"] = False
                store.save(current)
        _install_modpack(cfg, job, fresh)
    else:
        info = sources.paper_java_info(cfg["version"])
        cfg["java_major"] = info["major"]
        cfg["java_flags"] = info["flags"]
        if cfg.get("geyser") and info["major"] < 21:
            raise sources.SourceError(
                "Bedrock-Crossplay (Geyser) funktioniert erst ab Paper 1.20.5 mit Java 21. "
                "Bitte unter Einstellungen eine neuere Version wählen oder Crossplay abschalten "
                "und dann „Neu installieren“.")
        ensure_java(info["major"], job, 0)

        _step(job, 1, f"Paper {cfg['version']} wird geladen …")
        url, filename, sha256 = sources.paper_download(cfg["version"])
        jar = sdir / "server.jar"
        build_file = sdir / "build.txt"
        if jar.exists() and build_file.exists() and build_file.read_text(encoding="utf-8").strip() == filename:
            job["detail"] = f"{filename} ist bereits vorhanden"
        else:
            sources.download(url, jar, _progress_cb(job, "Paper"), _status_cb(job), sha256=sha256)
            build_file.write_text(filename, encoding="utf-8")

        _step(job, 2, "Bedrock-Unterstützung (Geyser + Floodgate) wird eingerichtet …")
        plugins = sdir / "plugins"
        if cfg.get("geyser"):
            plugins.mkdir(exist_ok=True)
            sources.download(sources.GEYSER_URL, plugins / "Geyser-Spigot.jar",
                             _progress_cb(job, "Geyser"), _status_cb(job))
            sources.download(sources.FLOODGATE_URL, plugins / "floodgate-spigot.jar",
                             _progress_cb(job, "Floodgate"), _status_cb(job))
            # Geyser spricht nur die allerneueste Java-Protokollversion. ViaVersion + ViaBackwards
            # lassen Bedrock-Spieler auch auf jeder anderen Paper-Version beitreten.
            for project in ("ViaVersion", "ViaBackwards"):
                via_url, _via_version = sources.hangar_download_url(project)
                sources.download(via_url, plugins / f"{project}.jar",
                                 _progress_cb(job, project), _status_cb(job))
        else:
            remove_crossplay_plugins(sdir)

        _step(job, 3, "Einstellungen werden geschrieben …")
        # Die Mojang-EULA wurde in der GUI ausdrücklich bestätigt.
        (sdir / "eula.txt").write_text(
            "# Mojang EULA wurde im Minecraft Server Manager bestätigt.\neula=true\n",
            encoding="utf-8")
        apply_config(cfg, initial=fresh)
        if companion.applies(cfg):
            companion.prepare(cfg)

    # Gespeicherte Konfiguration neu lesen: wurde der Server inzwischen gelöscht, nicht wiederbeleben;
    # zwischenzeitlich gespeicherte Einstellungen nicht überschreiben.
    current = store.get(cfg["id"])
    if current is None:
        return
    current["installed"] = True
    keys = ("java_major", "java_flags")
    if modpacks.is_modpack(cfg):
        keys += ("modpack", "flavor", "version")      # aus dem Pack gelesen (Loader, Minecraft-Version)
    for key in keys:
        if key in cfg:
            current[key] = cfg[key]
    cfg.update(current)
    store.save(current)


def _install_modpack(cfg: dict, job: dict, fresh: bool) -> None:
    """Modpack von Modrinth: Pack laden, Mod-Loader-Server einrichten, Mods laden, konfigurieren.
    Schritte: Java bereitstellen · Modpack laden · Loader installieren · Mods laden · Konfigurieren."""
    sdir = store.server_dir(cfg["id"])
    mp = dict(cfg.get("modpack") or {})
    if not mp.get("version_id"):
        raise sources.SourceError("Für diesen Server ist keine Modpack-Version hinterlegt.")

    _step(job, 0, "Modpack-Version wird bei Modrinth nachgeschlagen …")
    version = sources.modrinth_version(mp["version_id"])
    if mp.get("project_id") and version["project_id"] and version["project_id"] != mp["project_id"]:
        raise sources.SourceError("Die Modpack-Version gehört zu einem anderen Projekt.")
    if not mp.get("title") or not mp.get("url"):
        try:
            mp.update({k: v for k, v in sources.modrinth_project(version["project_id"]).items() if v})
        except sources.SourceError:
            pass
    # Java-Version richtet sich nach der Minecraft-Version des Packs (1.20.5+ → 21, 26.x → 25, 1.17–1.20.4 → 17).
    mc_guess = version["mc_version"] or mp.get("mc_version") or cfg.get("version") or ""
    major = sources.java_major_for(mc_guess)
    java = ensure_java(major, job, 0)

    _step(job, 1, "Modpack wird geladen …")
    mrpack = modpacks.download_pack(version, _progress_cb(job, "Modpack"), _status_cb(job))
    index = modpacks.read_index(mrpack)
    mp.update({"project_id": version["project_id"] or mp.get("project_id", ""), "version_id": version["id"],
               "version_name": version["name"] or index["version"], "mc_version": index["mc_version"],
               "loader": index["loader"], "loader_version": index["loader_version"]})
    cfg["modpack"] = mp
    cfg["flavor"] = index["loader"]
    cfg["version"] = index["mc_version"]
    cfg["geyser"] = False
    if index["mc_version"] != mc_guess:               # Pack nennt eine andere Minecraft-Version als Modrinth
        major = sources.java_major_for(index["mc_version"])
        java = ensure_java(major, job, 1)
    cfg["java_major"] = major
    cfg["java_flags"] = []

    # Loader zuerst – schlägt er fehl, bleibt die bisherige Installation samt Mods unangetastet.
    _step(job, 2, f"{modpacks.LOADER_NAMES.get(index['loader'], index['loader'])} wird installiert …")
    mp["loader_version"] = modpacks.install_loader(sdir, index, java, _status_cb(job), _progress_cb(job, "Loader"))

    _step(job, 3, "Mods werden geladen …")
    # Vorherige Pack-Version: nur entfernen, was das neue Pack nicht mehr liefert (Welt, eigene Konfiguration bleiben);
    # weiterhin gelieferte Dateien prüft install_files per Prüfsumme und lädt sie nur bei Bedarf neu.
    wanted = {f["path"] for f in index["files"] if f["server"] != "unsupported"}
    removed = modpacks.remove_installed(sdir, keep=wanted)
    if removed:
        job["detail"] = f"{removed} Dateien der vorherigen Pack-Version entfernt"
    total = len(wanted)

    def mod_progress(n: int, m: int, name: str) -> None:
        job["detail"] = f"Mods laden {n}/{m} – {name}"
        job["pct"] = int((3 + (n / max(1, m))) / len(job["steps"]) * 100)

    modpacks.install_files(sdir, mrpack, index, mp, mod_progress, _status_cb(job))
    job["detail"] = f"{total} Dateien des Packs sind vorhanden"

    _step(job, 4, "Einstellungen werden geschrieben …")
    # Die Mojang-EULA wurde in der GUI ausdrücklich bestätigt.
    (sdir / "eula.txt").write_text(
        "# Mojang EULA wurde im Minecraft Server Manager bestätigt.\neula=true\n", encoding="utf-8")
    apply_config(cfg, initial=fresh)


def install_async(cfg: dict) -> dict:
    if cfg["type"] == "bedrock":
        steps = ["Systemvoraussetzungen prüfen", "Bedrock Server herunterladen", "Entpacken", "Konfigurieren"]
    elif modpacks.is_modpack(cfg):
        steps = ["Java bereitstellen", "Modpack laden", "Loader installieren", "Mods laden", "Konfigurieren"]
    else:
        steps = ["Java-Runtime bereitstellen", "Paper herunterladen",
                 "Geyser + Floodgate installieren", "Konfigurieren"]
    job = new_job(cfg["id"], steps)
    return _run_job(job, lambda: install(cfg, job))


def needs_preparation(cfg: dict) -> bool:
    """True, wenn vor dem Start noch etwas nachgeladen werden muss (Java, VC++-Laufzeit)."""
    if cfg["type"] == "java":
        return java_path(required_java(cfg)) is None
    return sys.platform == "win32" and not vc_redist_present()


def prepare_and_start_async(cfg: dict) -> dict:
    """Lädt fehlende Abhängigkeiten nach und startet den Server danach automatisch."""
    if cfg["type"] == "java":
        steps = [f"Java {required_java(cfg)} bereitstellen", "Server starten"]
    else:
        steps = ["Visual C++ Laufzeit bereitstellen", "Server starten"]
    job = new_job(cfg["id"], steps)

    def work() -> None:
        if cfg["type"] == "java":
            ensure_java(required_java(cfg), job, 0)
        else:
            ensure_vc_redist(job, 0)
        _step(job, 1, "Server wird gestartet …")
        instance(cfg).start()
        start_broadcaster_if_enabled(cfg)

    return _run_job(job, work)


# ---------------------------------------------------------------- Prozesse

class Instance:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.offset = 0                # Anzahl bereits verworfener Zeilen
        self.lock = threading.Lock()
        self.started_at = 0.0
        self.stopping = False

    # -- Log
    def log(self, text: str) -> None:
        with self.lock:
            self.lines.append(text.rstrip("\r\n"))
            if len(self.lines) > MAX_LOG_LINES:
                drop = len(self.lines) - MAX_LOG_LINES
                del self.lines[:drop]
                self.offset += drop

    def console(self, since: int, tail: int | None = None) -> dict:
        with self.lock:
            if tail is not None:
                lines = self.lines[-tail:] if tail > 0 else []
            else:
                start = max(0, since - self.offset)
                lines = self.lines[start:]
            return {"next": self.offset + len(self.lines), "lines": lines}

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- Steuerung
    def start(self) -> None:
        if self.running:
            return
        cfg = self.cfg
        sdir = store.server_dir(cfg["id"])
        notes: list[str] = []
        if cfg["type"] == "bedrock":
            exe = sdir / "bedrock_server.exe"
            if not exe.exists():
                raise RuntimeError("Der Server ist nicht vollständig installiert.")
            cmd = [str(exe)]
            # NetherNet bietet Clients nur die Adressen an, die in server-udp-ports stehen. Ohne öffentliche
            # IPv4 bekämen Freunde aus dem Internet trotz Portfreigabe nur die LAN-Adresse – deshalb wird
            # sie bei jedem Start ermittelt (sie wechselt bei vielen Anschlüssen täglich).
            lo, hi = bedrock_udp_range(cfg)
            pub = public_ipv4(cfg)
            patch_properties(sdir / "server.properties", {"server-udp-ports": bedrock_udp_ports_value(cfg, pub)})
            if pub:
                notes.append(f"[Manager] Internet-Beitritt: Spielern wird {pub} mit UDP {lo}–{hi} angeboten "
                             "(Portfreigabe in Router und Firewall vorausgesetzt).")
            else:
                notes.append("[Manager] Keine öffentliche IPv4 gefunden (Router-UPnP aus oder DS-Lite) – Beitritt nur "
                             "im Heimnetz. Für Freunde aus dem Internet die öffentliche IP unter Einstellungen eintragen.")
        else:
            major = required_java(cfg)
            java = java_path(major)
            if not java:
                raise RuntimeError(f"Java {major} wurde nicht gefunden. Bitte den Server erneut starten – "
                                   "die Laufzeit wird dann automatisch geladen.")
            ram = cfg["ram_mb"]
            if modpacks.is_modpack(cfg):
                # Fabric/Quilt: Launcher-JAR; NeoForge/Forge: Argument-Dateien des Installers.
                cmd = modpacks.start_command(cfg, java, ram)
                mp = cfg.get("modpack") or {}
                notes.append(f"[Manager] Modpack „{mp.get('title') or '?'}“ {mp.get('version_name') or ''} · "
                             f"{modpacks.LOADER_NAMES.get(cfg.get('flavor'), cfg.get('flavor'))} "
                             f"{mp.get('loader_version') or ''} · Minecraft {cfg['version']}. "
                             "Spieler brauchen dasselbe Modpack im Launcher (Modrinth App / Prism).")
            else:
                flags = [f for f in cfg.get("java_flags") or [] if isinstance(f, str) and f.startswith(("-XX:", "-D"))]
                # Seit JDK 19 gelten für System.out/err eigene Encoding-Eigenschaften – ohne sie
                # kämen Umlaute in der Konsole als Cp1252 an. Ältere JDKs ignorieren unbekannte -D.
                cmd = [str(java), f"-Xms{max(512, ram // 2)}M", f"-Xmx{ram}M",
                       "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",
                       "-Dstdin.encoding=UTF-8", *flags, "-jar", "server.jar", "nogui"]
                ensure_server_icon(sdir)
                if cfg.get("geyser"):
                    # Sicherstellen, dass Bedrock-Spieler im Chat schreiben dürfen (siehe apply_config).
                    patch_properties(sdir / "server.properties", {"enforce-secure-profile": "false"})
                if companion.applies(cfg):
                    # Begleit-Plugin vor jedem Start neu bereitstellen – auch wenn es gelöscht wurde.
                    try:
                        if companion.prepare(cfg):
                            notes.append("[Manager] Begleit-Plugin MCSMCompanion bereitgestellt.")
                    except OSError as exc:
                        notes.append(f"[Manager] Begleit-Plugin konnte nicht kopiert werden: {exc}")

        self.lines.clear()
        self.offset = 0
        self.stopping = False
        self.log(f"[Manager] Starte {cfg['name']} …")
        for note in notes:
            self.log(note)
        self.proc = subprocess.Popen(
            cmd, cwd=str(sdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, creationflags=CREATE_NO_WINDOW)
        _bind_to_job(self.proc)
        self.started_at = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _apply_gamerules(self) -> None:
        """Ab 1.21.9 sind PvP und Befehlsblöcke Gamerules statt server.properties."""
        cfg = self.cfg
        rule = sources.command_block_gamerule(cfg["version"])
        if cfg["type"] != "java" or rule is None:
            return
        try:
            self.send(f"gamerule pvp {_bool(cfg['pvp'])}")
            self.send(f"gamerule {rule} {_bool(cfg['allow_cheats'])}")
        except (RuntimeError, OSError):
            pass

    def _pump(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        gamerules_done = False
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            self.log(line)
            if not gamerules_done and "Done (" in line:
                gamerules_done = True
                self._apply_gamerules()
        code = proc.wait()
        self.log(f"[Manager] Server beendet (Code {code}).")
        # /hardcore on|off im Spiel in die Einstellungen übernehmen.
        threading.Thread(target=companion.sync_after_stop, args=(self.cfg,), daemon=True).start()
        # Ohne Server gibt es nichts mehr zu bewerben – Bot mit beenden.
        threading.Thread(target=stop_broadcaster, args=(self.cfg,), daemon=True).start()

    def send(self, command: str) -> None:
        if not self.running or not self.proc or not self.proc.stdin:
            raise RuntimeError("Der Server läuft nicht.")
        self.proc.stdin.write((command.strip() + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        self.log(f"> {command.strip()}")

    def _announce_stop(self) -> bool:
        """Paper-Server mit Begleit-Plugin: „mcsmstop 10“ sagt den Stopp im Spiel an und stoppt danach
        selbst (ohne Spieler online überspringt das Plugin den Countdown).

        Gewartet wird auf die Bestätigung des Plugins im Protokoll („Herunterfahren angefordert“).
        Bleibt sie aus – weil das Plugin fehlt („Unknown command“) oder weil features.shutdown auf
        false steht und der Befehl abgelehnt wird –, wird sofort normal gestoppt."""
        if not companion.applies(self.cfg):
            return False
        mark = self.console(0)["next"]
        try:
            self.send(f"mcsmstop {STOP_COUNTDOWN}")
        except (RuntimeError, OSError):
            return False
        deadline = time.time() + ANNOUNCE_ANSWER
        while self.running:
            for line in self.console(mark)["lines"]:
                if _ANNOUNCE_OK_RE.search(line):
                    self.log(f"[Manager] Stopp wird im Spiel angekündigt ({STOP_COUNTDOWN} Sekunden) …")
                    return True
            if time.time() >= deadline:
                break
            time.sleep(0.2)
        self.log("[Manager] Der Server hat die Ankündigung nicht bestätigt – er wird direkt gestoppt.")
        return False

    def stop(self, timeout: int = 45, announce: bool = False) -> None:
        if not self.running or not self.proc:
            return
        self.stopping = True
        if announce and self._announce_stop():
            # Dem Plugin Zeit für Countdown und Stopp lassen; danach wie bisher „stop“ hinterherschicken.
            waiting = time.time() + ANNOUNCE_WAIT
            while time.time() < waiting and self.running:
                time.sleep(0.4)
        if not self.running:
            return
        # Erst jetzt läuft die Frist: sie gilt dem eigentlichen Stopp (Welt speichern), nicht der
        # Ankündigung davor – sonst bliebe zum Speichern bis zu ANNOUNCE_WAIT weniger Zeit.
        deadline = time.time() + timeout
        self.log("[Manager] Stoppe Server – Welt wird gespeichert …")
        try:
            if self.proc.stdin:
                self.proc.stdin.write(b"stop\n")
                self.proc.stdin.flush()
        except OSError:
            pass
        while time.time() < deadline and self.running:
            time.sleep(0.4)
        if self.running:
            self.log("[Manager] Server reagiert nicht – wird beendet.")
            self.proc.terminate()


class Broadcaster(Instance):
    """MCXboxBroadcast-Prozess (Xbox-Freunde-Modus) eines Servers. Der Zustand (Anmelde-Code, Gamertag,
    online) wird beim Lesen der Ausgabe mitgeführt und bleibt erhalten, egal wie viel danach geloggt wird."""

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.status = self._fresh_status()
        self.started_sig: tuple = ()

    @staticmethod
    def _fresh_status() -> dict:
        return {"state": "starting", "code": "", "url": "", "gamertag": "", "error": "", "authed": False}

    def _config_sig(self) -> tuple:
        """Alles, was in config.yml landet – ändert sich das, muss der Bot neu gestartet werden."""
        cfg = self.cfg
        return (cfg.get("xbox_host_name") or cfg["name"], cfg["motd"], int(cfg["max_players"]),
                xbox_address(cfg), xbox_bedrock_port(cfg), cfg["type"])

    @property
    def restart_needed(self) -> bool:
        return self.running and self.started_sig != self._config_sig()

    def start(self) -> None:
        if self.running:
            if not self.restart_needed:
                return
            # MCXboxBroadcast liest config.yml nur beim Start und kennt keinen Reload-Befehl.
            self.stop(timeout=15)
        cfg = self.cfg
        jar, java = xbox_jar(), xbox_java()
        if not jar or not java:
            raise RuntimeError("Der Xbox-Freunde-Modus ist noch nicht eingerichtet.")
        write_xbox_config(cfg)
        cmd = [str(java), "-Xms96M", "-Xmx512M", "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8",
               "-Dstderr.encoding=UTF-8", "-jar", str(jar)]
        self.lines.clear()
        self.offset = 0
        self.stopping = False
        self.status = self._fresh_status()
        self.started_sig = self._config_sig()
        self.log("[Manager] Starte Xbox-Freunde-Modus (MCXboxBroadcast) …")
        self.proc = subprocess.Popen(
            cmd, cwd=str(xbox_dir(cfg)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, creationflags=CREATE_NO_WINDOW)
        _bind_to_job(self.proc)
        self.started_at = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _track(self, line: str) -> None:
        """Zustand aus einer Ausgabezeile fortschreiben (starting → login → online)."""
        st = self.status
        m = _XBOX_CODE_RE.search(line)
        if m:
            st.update(state="login", url=m.group(1), code=m.group(2).upper(), error="")
            return
        m = _XBOX_USER_RE.search(line)
        if m:
            st.update(gamertag=m.group(1), code="", url="", authed=True, error="")
            if st["state"] == "login":
                st["state"] = "starting"
        if _XBOX_ONLINE_RE.search(line):
            st.update(state="online", code="", url="", authed=True, error="")
            return
        # Stacktrace-Zeilen und der harmlose Ping-Fehler sind keine Fehlermeldung für die Oberfläche.
        if _XBOX_IGNORE_RE.search(line) or line.startswith(("\tat ", "\t... ", "java.", "Caused by")):
            return
        if re.search(r"\bFailed to\b|Exception:", line):
            st["error"] = line.strip()[-220:]

    def _pump(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        for raw in iter(proc.stdout.readline, b""):
            # log4j gibt Farbcodes aus – in der Oberfläche wären das Zeichensalat.
            line = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
            self.log(line)
            self._track(line)
        code = proc.wait()
        self.log(f"[Manager] Xbox-Freunde-Modus beendet (Code {code}).")

    def stop(self, timeout: int = 15, announce: bool = False) -> None:
        # announce: beim Bot ohne Bedeutung – es gibt niemanden, dem ein Stopp angekündigt werden müsste.
        with self.lock:
            if not self.running or not self.proc:
                return
            first = not self.stopping
            self.stopping = True
        if not first:
            # Server-Stopp und Prozessende rufen beide stop() – der zweite Aufruf wartet nur.
            deadline = time.time() + timeout
            while time.time() < deadline and self.running:
                time.sleep(0.3)
            return
        self.log("[Manager] Stoppe Xbox-Freunde-Modus …")
        if not self.status.get("authed"):
            timeout = min(timeout, 3)              # ohne Anmeldung reagiert der Bot nicht auf „exit“
        try:
            if self.proc.stdin:
                self.proc.stdin.write(b"exit\n")
                self.proc.stdin.flush()
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline and self.running:
            time.sleep(0.3)
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.SubprocessError:
                pass


_instances: dict[str, Instance] = {}
_inst_lock = threading.Lock()


def instance(cfg: dict) -> Instance:
    with _inst_lock:
        inst = _instances.get(cfg["id"])
        if inst is None:
            inst = Instance(cfg)
            _instances[cfg["id"]] = inst
        inst.cfg = cfg
        return inst


def broadcaster(cfg: dict) -> Broadcaster:
    key = f"{cfg['id']}:xbox"
    with _inst_lock:
        inst = _instances.get(key)
        if inst is None:
            inst = Broadcaster(cfg)
            _instances[key] = inst
        inst.cfg = cfg
        return inst  # type: ignore[return-value]


def is_running(server_id: str) -> bool:
    with _inst_lock:
        inst = _instances.get(server_id)
    return bool(inst and inst.running)


def uptime(server_id: str) -> int:
    with _inst_lock:
        inst = _instances.get(server_id)
    if inst and inst.running:
        return int(time.time() - inst.started_at)
    return 0


def drop(server_id: str) -> None:
    with _inst_lock:
        inst = _instances.pop(server_id, None)
        bot = _instances.pop(f"{server_id}:xbox", None)
    if bot:
        bot.stop(timeout=10)
    if inst:
        inst.stop(timeout=20)


def stop_all() -> None:
    with _inst_lock:
        instances = list(_instances.values())
    for inst in instances:
        try:
            inst.stop(timeout=30)
        except Exception:                             # noqa: BLE001
            pass


def emergency_stop_all(budget: float = 4.0) -> None:
    """Für Fenster-Schließen/Abmelden: 'stop' an alle Server, gemeinsam höchstens `budget`
    Sekunden warten (Windows gewährt ~5 s). Was dann noch läuft, beendet das Job-Objekt."""
    with _inst_lock:
        instances = [i for i in _instances.values() if i.running]
    for inst in instances:
        try:
            inst.stopping = True
            if inst.proc and inst.proc.stdin:
                inst.proc.stdin.write(b"stop\n")
                inst.proc.stdin.flush()
        except OSError:
            pass
    deadline = time.time() + budget
    while time.time() < deadline and any(i.running for i in instances):
        time.sleep(0.1)


# ---------------------------------------------------------------- Netzwerk

def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(("192.0.2.1", 9))               # TEST-NET, es fließen keine Daten
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def port_in_use(port: int, udp: bool, ipv6: bool = False) -> bool:
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    family = socket.AF_INET6 if ipv6 else socket.AF_INET
    try:
        with socket.socket(family, kind) as s:
            s.bind(("::" if ipv6 else "0.0.0.0", port))
            return False
    except OSError:
        return True


def _rule_name(cfg: dict) -> str:
    # Anführungszeichen und cmd-Sonderzeichen entfernen; Umlaute bleiben (\w ist Unicode).
    clean = re.sub(r"[^\w .()\-]", "", cfg["name"]).strip() or cfg["id"]
    return f"Minecraft Server Manager - {clean}"


def firewall_command(cfg: dict) -> list[str]:
    """netsh-Befehle, die die benötigten Ports in der Windows-Firewall freigeben."""
    name = _rule_name(cfg)
    cmds = []
    for p in port_list(cfg):
        ports = str(p["from"]) if p["from"] == p["to"] else f'{p["from"]}-{p["to"]}'
        cmds.append(f'netsh advfirewall firewall add rule name="{name} ({p["proto"]} {ports})" '
                    f'dir=in action=allow protocol={p["proto"]} localport={ports}')
    if cfg["type"] == "bedrock":
        # LAN-Suche der Bedrock-Clients (Server erscheint unter „Freunde“ → LAN)
        cmds.append(f'netsh advfirewall firewall add rule name="{name} (LAN-Suche UDP 7551)" '
                    f'dir=in action=allow protocol=UDP localport=7551')
    return cmds


if sys.platform == "win32":
    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", wt.ULONG), ("hwnd", wt.HWND),
                    ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                    ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
                    ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]


def open_firewall(cfg: dict) -> str:
    """Ein UAC-Dialog: cmd.exe wird erhöht und unsichtbar gestartet und führt alle netsh-Zeilen aus.
    Blockiert, bis die UAC-Abfrage beantwortet und netsh fertig ist (nur diese Anfrage wartet)."""
    if sys.platform != "win32":
        raise RuntimeError("Firewall-Regeln lassen sich nur unter Windows anlegen.")
    params = "/d /c " + " && ".join(firewall_command(cfg))
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000400           # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = params
    info.nShow = 0                                 # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        raise RuntimeError("Firewall-Regel wurde nicht angelegt – UAC-Abfrage abgelehnt oder Start fehlgeschlagen.")
    k32 = ctypes.windll.kernel32
    code = wt.DWORD(1)
    try:
        k32.WaitForSingleObject(wt.HANDLE(info.hProcess), 60000)
        k32.GetExitCodeProcess(wt.HANDLE(info.hProcess), ctypes.byref(code))
    finally:
        k32.CloseHandle(wt.HANDLE(info.hProcess))
    if code.value != 0:
        raise RuntimeError(f"netsh meldete einen Fehler (Code {code.value}). "
                           "Bitte die Befehle unter „Manuell“ als Administrator ausführen.")
    return "Firewall-Regel(n) wurden angelegt."


def delete_server(cfg: dict) -> None:
    drop(cfg["id"])
    time.sleep(0.5)
    path = store.server_dir(cfg["id"])

    def _retry(func, target, exc_info):
        # Windows gibt Dateien oft erst kurz nach Prozessende frei – einmal kurz warten.
        time.sleep(0.8)
        func(target)

    if path.exists():
        try:
            shutil.rmtree(path, onexc=_retry)
        except OSError as exc:
            raise RuntimeError(
                f"Ordner konnte nicht gelöscht werden: {exc}. Ist noch eine Datei daraus geöffnet?") from exc
    if path.exists():
        raise RuntimeError("Ordner konnte nicht vollständig gelöscht werden. Bitte erneut versuchen.")
    store.delete(cfg["id"])
