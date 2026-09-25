# -*- coding: utf-8 -*-
"""Begleit-Plugin MCSMCompanion für gehostete Paper-Server.

Gegenstück zum ``core/companion.py`` des Programms auf dem PC. Vor **jedem** Start legt der
Daemon die Jar nach ``plugins/MCSMCompanion.jar`` (auch wenn sie gelöscht wurde) und schreibt
die verwalteten Schlüssel in ``plugins/MCSMCompanion/config.yml``. Eigene Anpassungen des
Besitzers bleiben dabei erhalten.

Wozu das gut ist:

* **Ankündigungen:** Mit dem Plugin versteht der Server ``mcsmstop <sekunden>`` – der Stopp wird
  im Spiel als Titel und im Chat heruntergezählt, und der Server speichert die Welt selbst.
  Ohne Plugin bleibt nur ``say``, und der Spieler steht ohne Vorwarnung draußen.
* **Kennzahlen:** Das Plugin schreibt ``plugins/MCSMCompanion/status.json`` (Spieler, TPS …) –
  daraus füttert der Daemon die Anzeige, ohne den Server nach jedem Wert zu fragen.

Modpacks (Fabric/NeoForge/…) und Bedrock haben keine Bukkit-Plugin-API – dort gibt es das
Plugin nicht, und alles funktioniert wie bisher.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import time
import zipfile

JAR_NAME = "MCSMCompanion.jar"
PLUGIN_DIR = "MCSMCompanion"
#: Woher die Jar kommt (deploy.sh legt sie nach /opt/mcsm/assets/).
JAR_ENV = "MCSM_COMPANION_JAR"
SPONSOR_TEXT = "Sponsored by Novelnia"
#: Diese Schlüssel setzt der Dienst bei jedem Start neu; alles andere gehört dem Besitzer.
MANAGED = ("server_name", "manager_version", "mode", "sponsor_text", "hardcore")
#: Schlüssel früherer Fassungen, die aus vorhandenen config.yml verschwinden.
REMOVED = ("admin_users", "admin_op", "admin_silent_join", "admin_vanish_gamemode")

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


def jar_source() -> pathlib.Path:
    """Pfad der mitgelieferten Jar (``/opt/mcsm/assets/MCSMCompanion.jar``)."""
    raw = (os.environ.get(JAR_ENV) or "").strip()
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path(__file__).resolve().parent.parent / "assets" / JAR_NAME


def available() -> bool:
    try:
        return jar_source().is_file()
    except OSError:
        return False


def applies(instance: dict) -> bool:
    """Nur Java-Server mit Paper (Bukkit-API) können das Plugin laden."""
    return (str(instance.get("type") or "java") == "java"
            and str(instance.get("flavor") or "paper") == "paper")


def plugin_dir(folder: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(folder) / "plugins" / PLUGIN_DIR


def jar_dest(folder: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(folder) / "plugins" / JAR_NAME


def installed(folder: pathlib.Path) -> bool:
    return jar_dest(folder).is_file()


def _sha(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def install_jar(folder: pathlib.Path) -> bool:
    """Jar bereitstellen. Der Ersatz ist atomar – es liegt nie eine halbe Datei in plugins/."""
    quelle = jar_source()
    if not quelle.is_file():
        return False
    ziel = jar_dest(folder)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    if ziel.is_file():
        try:
            if _sha(ziel) == _sha(quelle):
                return True
        except OSError:
            pass
    tmp = ziel.with_name(ziel.name + ".neu")
    shutil.copyfile(quelle, tmp)
    # Der Serverprozess läuft unter einem eigenen Unix-Benutzer und muss die Jar lesen können;
    # geschrieben wird sie nur vom Dienst.
    try:
        os.chmod(tmp, 0o640)
    except OSError:
        pass
    os.replace(tmp, ziel)
    return True


# ---------------------------------------------------------------- config.yml

def _yaml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{text}"'


def managed_values(instance: dict, *, version: str = "") -> dict:
    return {
        "server_name": _yaml(instance.get("name") or "Minecraft Server"),
        "manager_version": _yaml(str(version or "")),
        "mode": _yaml("hosted"),
        "sponsor_text": _yaml(SPONSOR_TEXT),
        "hardcore": _yaml(bool(instance.get("hardcore"))),
    }


def _template_lines() -> list:
    """Vorlage aus der Jar (alle Schlüssel samt Erklärungen), damit Anpassungen leichtfallen."""
    try:
        with zipfile.ZipFile(jar_source()) as zf:
            return zf.read("config.yml").decode("utf-8", errors="replace").splitlines()
    except (OSError, KeyError, zipfile.BadZipFile):
        return [
            "# Vom Minecraft Server Manager erzeugt. Die verwalteten Schlüssel (server_name,",
            "# manager_version, mode, sponsor_text, hardcore) werden bei jedem Start neu gesetzt –",
            "# alles andere darf hier angepasst werden. Fehlende Schlüssel ergänzt das Plugin",
            "# mit seinen Standardwerten.",
        ]


def write_config(folder: pathlib.Path, instance: dict, *, version: str = "") -> pathlib.Path:
    """Verwaltete Schlüssel setzen; alle anderen Zeilen bleiben unverändert."""
    path = plugin_dir(folder) / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = managed_values(instance, version=version)
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = _template_lines()
    out: list = []
    skipping = False
    for line in lines:
        found = _KEY_RE.match(line)
        if found:
            skipping = False
            key = found.group(1)
            if key in values:
                out.append(f"{key}: {values.pop(key)}")
                skipping = True                 # Folgezeilen des alten Werts überspringen
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
    tmp = path.with_name(path.name + ".neu")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def prepare(folder: pathlib.Path, instance: dict, *, version: str = "") -> bool:
    """Vor jedem Start: Jar bereitstellen und Konfiguration schreiben.

    Rückgabe ``True``, wenn der Server danach ``mcsmstop`` versteht. Ein Fehlschlag ist kein
    Grund, den Start abzulehnen – dann wird eben mit ``say`` angekündigt.
    """
    if not applies(instance):
        return False
    if not install_jar(folder):
        return False
    try:
        write_config(folder, instance, version=version)
    except OSError:
        return True                             # Jar liegt – das genügt für mcsmstop
    return True


# ---------------------------------------------------------------- status.json

def status(folder: pathlib.Path, instance: dict | None = None) -> dict:
    """Was das Plugin gemeldet hat (Spieler, TPS …) – für die Anzeige im Programm."""
    info = {"applies": applies(instance or {}) if instance is not None else True,
            "available": available(), "installed": installed(folder),
            "status": None, "age": None}
    path = plugin_dir(folder) / "status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    if isinstance(data, dict):
        info["status"] = data
        try:
            info["age"] = max(0, int(time.time() - float(data.get("updated") or 0)))
        except (TypeError, ValueError):
            info["age"] = None
    return info
