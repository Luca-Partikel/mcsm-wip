"""MCSMCompanion – das Begleit-Plugin des Managers für Paper-Server.

Der Manager kopiert das Plugin vor jedem Start in plugins/ (auch wenn es gelöscht wurde), schreibt die
verwalteten Schlüssel in plugins/MCSMCompanion/config.yml (eigene Anpassungen bleiben erhalten) und liest
plugins/MCSMCompanion/status.json für das Dashboard. Modpacks (Fabric/NeoForge/…) und Bedrock haben keine
Bukkit-Plugin-API – dort ist das Plugin nicht verfügbar.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import time
import zipfile

from . import store
from .version import __version__

JAR_SRC = store.BASE / "assets" / "MCSMCompanion.jar"
SPONSOR_TEXT = "Sponsored by Novelnia"
MANAGED = ("server_name", "manager_version", "mode", "sponsor_text", "hardcore")
# Schlüssel früherer Versionen, die aus vorhandenen config.yml entfernt werden
REMOVED = ("admin_users", "admin_op", "admin_silent_join", "admin_vanish_gamemode")
SUSPICIOUS_HINT = ("Ein installiertes Plugin sieht nach Manipulation aus (gefälschte Spielerzahlen/Ping). "
                   "Auf lokalen Servern ist das erlaubt, auf Root-/Paid-Servern wird es gesperrt.")


def applies(cfg: dict) -> bool:
    """Nur Java-Server mit Paper (Bukkit-API) können das Plugin laden."""
    return cfg.get("type") == "java" and str(cfg.get("flavor") or "paper") == "paper"


def available() -> bool:
    return JAR_SRC.is_file()


def plugin_dir(cfg: dict) -> pathlib.Path:
    return store.server_dir(cfg["id"]) / "plugins" / "MCSMCompanion"


def jar_dest(cfg: dict) -> pathlib.Path:
    return store.server_dir(cfg["id"]) / "plugins" / "MCSMCompanion.jar"


def _sha(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def install_jar(cfg: dict) -> bool:
    """Plugin-Jar (neu) in plugins/ legen – Ersatz ist atomar, damit nie eine halbe Datei liegt."""
    if not applies(cfg) or not available():
        return False
    dest = jar_dest(cfg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and _sha(dest) == _sha(JAR_SRC):
        return True
    tmp = dest.with_name(dest.name + ".new")
    shutil.copy2(JAR_SRC, tmp)
    tmp.replace(dest)
    return True


# ---------------------------------------------------------------- config.yml

def _yaml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{text}"'


def managed_values(cfg: dict) -> dict[str, str]:
    return {
        "server_name": _yaml(cfg.get("name") or "Minecraft Server"),
        "manager_version": _yaml(__version__),
        "mode": _yaml("local"),
        "sponsor_text": _yaml(SPONSOR_TEXT),
        "hardcore": _yaml(bool(cfg.get("hardcore"))),
    }


def _template_lines() -> list[str]:
    """Vorlage aus dem Plugin-Jar (alle Schlüssel samt Erklärungen), damit Anpassungen leichtfallen."""
    try:
        with zipfile.ZipFile(JAR_SRC) as zf:
            return zf.read("config.yml").decode("utf-8", errors="replace").splitlines()
    except (OSError, KeyError, zipfile.BadZipFile):
        return [
            "# Von Minecraft Server Manager erzeugt. Verwaltete Schlüssel (server_name, manager_version, mode,",
            "# sponsor_text, hardcore) werden bei jedem Start neu gesetzt – alles andere",
            "# darf hier angepasst werden. Fehlende Schlüssel ergänzt das Plugin mit seinen Standardwerten.",
        ]


def write_config(cfg: dict) -> pathlib.Path:
    """Setzt die verwalteten Schlüssel auf oberster Ebene; alle anderen Zeilen bleiben unverändert.
    Ein Schlüssel mit Listen-/Block-Wert (mehrere eingerückte Folgezeilen) wird komplett ersetzt."""
    path = plugin_dir(cfg) / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = managed_values(cfg)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else _template_lines()
    out: list[str] = []
    skipping = False
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if m:
            skipping = False
            key = m.group(1)
            if key in values:
                out.append(f"{key}: {values.pop(key)}")
                skipping = True                       # Folgezeilen (Listeneinträge) des alten Werts überspringen
                continue
            if key in REMOVED:
                skipping = True
                continue
        elif skipping and (line.startswith((" ", "\t", "-")) or not line.strip()):
            if not line.strip():
                skipping = False
                out.append(line)
            continue
        else:
            skipping = False
        out.append(line)
    for key, value in values.items():
        out.append(f"{key}: {value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def prepare(cfg: dict) -> bool:
    """Vor jedem Start: Plugin sicherstellen und Konfiguration schreiben."""
    if not install_jar(cfg):
        return False
    write_config(cfg)
    return True


# ---------------------------------------------------------------- status.json

def status(cfg: dict) -> dict:
    info = {"applies": applies(cfg), "available": available(), "installed": False,
            "status": None, "age": None, "suspicious": [], "hint": ""}
    if not info["applies"]:
        return info
    info["installed"] = jar_dest(cfg).exists()
    path = plugin_dir(cfg) / "status.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            info["status"] = data
            info["age"] = max(0, int(time.time() - float(data.get("updated") or 0)))
            info["suspicious"] = list(data.get("suspicious") or [])
            if info["suspicious"]:
                info["hint"] = SUSPICIOUS_HINT
        except (OSError, ValueError):
            info["status"] = None
    return info


def sync_after_stop(cfg: dict) -> None:
    """/hardcore on|off im Spiel überlebt den Neustart: den Zustand in die Einstellungen übernehmen."""
    data = (status(cfg).get("status") or {})
    hc = data.get("hardcore")
    if not isinstance(hc, dict) or "enabled" not in hc:
        return
    enabled = bool(hc.get("enabled"))
    current = store.get(cfg["id"])
    if current is not None and bool(current.get("hardcore")) != enabled:
        current["hardcore"] = enabled
        store.save(current)
