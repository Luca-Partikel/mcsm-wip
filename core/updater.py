"""Selbst-Update über GitHub Releases.

Ablauf: Release-API abfragen → Version vergleichen → ZIP laden (Prüfsumme aus SHA256SUMS.txt, falls
vorhanden) → alte Programmdateien sichern → Dateien ersetzen → Manager neu starten. Server-Daten
(servers/, data/, cache/, runtime/) werden nie angefasst.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import zipfile

from . import manager, sources, store
from .version import UPDATE_REPO, __version__

ZIP_NAME = "MinecraftServerManager.zip"
SUMS_NAME = "SHA256SUMS.txt"
CHECK_CACHE_SECONDS = 300
PROTECTED = {"data", "servers", "cache", "runtime", "dist", ".git"}   # nie überschreiben/sichern

_cache: dict = {}
_lock = threading.Lock()


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3]) or (0,)


def repo() -> str:
    return os.environ.get("MCSM_UPDATE_REPO") or UPDATE_REPO


def api_url() -> str:
    return os.environ.get("MCSM_UPDATE_API") or f"https://api.github.com/repos/{repo()}/releases/latest"


def dev_folder() -> bool:
    """Im Entwicklungsordner (git-Checkout) keine Updates – sonst würde der Quellcode überschrieben."""
    return (store.BASE / ".git").exists()


def check(force: bool = False) -> dict:
    """Aktueller Stand: {'enabled', 'current', 'latest', 'available', 'notes', 'zip_url', ...}."""
    with _lock:
        if not force and _cache and time.time() - _cache.get("checked", 0) < CHECK_CACHE_SECONDS:
            return dict(_cache)
    result = {"enabled": True, "current": __version__, "latest": "", "available": False,
              "notes": "", "zip_url": "", "sums_url": "", "html_url": "", "published": "",
              "checked": time.time(), "error": ""}
    if dev_folder():
        result.update(enabled=False, error="Entwicklungsordner (git) – Updates sind hier deaktiviert.")
    elif not repo() and not os.environ.get("MCSM_UPDATE_API"):
        result.update(enabled=False, error="Keine Update-Quelle konfiguriert.")
    else:
        try:
            data = sources.fetch_json(api_url())
            tag = str(data.get("tag_name") or data.get("name") or "")
            assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets") or []}
            result.update(
                latest=tag.lstrip("vV"),
                available=version_tuple(tag) > version_tuple(__version__),
                notes=str(data.get("body") or "")[:8000],
                zip_url=assets.get(ZIP_NAME) or "",
                sums_url=assets.get(SUMS_NAME) or "",
                html_url=str(data.get("html_url") or ""),
                published=str(data.get("published_at") or ""),
            )
            if result["available"] and not result["zip_url"]:
                result.update(available=False, error=f"Release {tag} enthält kein {ZIP_NAME}.")
        except sources.SourceError as exc:
            result["error"] = str(exc)
    with _lock:
        _cache.clear()
        _cache.update(result)
    return dict(result)


def last() -> dict:
    """Zuletzt geprüfter Stand ohne Netzwerkzugriff (für /api/bootstrap)."""
    with _lock:
        if _cache:
            return dict(_cache)
    return {"enabled": bool(repo() or os.environ.get("MCSM_UPDATE_API")) and not dev_folder(),
            "current": __version__, "latest": "", "available": False, "checked": 0, "error": ""}


# ---------------------------------------------------------------- Anwenden

def _program_files() -> list[pathlib.Path]:
    out = []
    for p in store.BASE.rglob("*"):
        rel = p.relative_to(store.BASE)
        if not p.is_file() or rel.parts[0] in PROTECTED or "__pycache__" in rel.parts:
            continue
        out.append(p)
    return out


def _backup(current: str) -> pathlib.Path:
    bdir = store.DATA_DIR / "backup"
    bdir.mkdir(parents=True, exist_ok=True)
    target = bdir / f"programm-v{current}-{time.strftime('%Y-%m-%d_%H-%M-%S')}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in _program_files():
            zf.write(p, p.relative_to(store.BASE).as_posix())
    for old in sorted(bdir.glob("programm-*.zip"))[:-3]:      # nur die letzten drei behalten
        old.unlink(missing_ok=True)
    return target


def _expected_sha(sums_url: str) -> str:
    if not sums_url:
        return ""
    try:
        text = sources.fetch_text(sums_url)
    except sources.SourceError:
        return ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == ZIP_NAME and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return ""


def _extract(archive: pathlib.Path, job: dict) -> int:
    root = store.BASE.resolve()
    count = 0
    with zipfile.ZipFile(archive) as zf:
        if zf.testzip() is not None:
            raise sources.SourceError("Das Update-Archiv ist beschädigt.")
        names = zf.namelist()
        if "app.py" not in names or "core/manager.py" not in names:
            raise sources.SourceError("Das Archiv ist kein Minecraft-Server-Manager-Paket.")
        for member in names:
            if member.endswith("/"):
                continue
            rel = pathlib.PurePosixPath(member)
            if rel.is_absolute() or ".." in rel.parts or (rel.parts and rel.parts[0] in PROTECTED):
                continue
            dest = (store.BASE / member).resolve()
            if root not in dest.parents:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(member)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            count += 1
            if count % 5 == 0:
                job["detail"] = f"Dateien werden ersetzt … {count}/{len(names)}"
    return count


def _schedule_restart() -> None:
    """Wartet (unsichtbar) auf das Ende dieses Prozesses und startet den Manager über Start.vbs neu.
    Bewusst eine kleine Batch-Datei statt PowerShell – keine Anführungszeichen-Akrobatik."""
    vbs = store.BASE / "Start.vbs"
    if sys.platform != "win32" or not vbs.exists():
        return
    pid = os.getpid()
    helper = store.DATA_DIR / "restart.cmd"
    # Absolute Pfade: im PATH könnte ein anderes `find` (z. B. aus Git) vor dem Windows-Werkzeug liegen.
    sys32 = "%SystemRoot%\\System32"
    lines = [
        "@echo off",
        ":warten",
        f'"{sys32}\\tasklist.exe" /FI "PID eq {pid}" 2>nul | "{sys32}\\findstr.exe" /C:" {pid} " >nul',
        "if not errorlevel 1 (",
        f'  "{sys32}\\ping.exe" -n 2 127.0.0.1 >nul',
        "  goto warten",
        ")",
        f'"{sys32}\\ping.exe" -n 2 127.0.0.1 >nul',
        f'start "" "{sys32}\\wscript.exe" //B "{vbs}"',
    ]
    helper.write_text("\r\n".join(lines) + "\r\n", encoding="cp1252")
    # Versteckte eigene Konsole (CREATE_NO_WINDOW), aber NICHT DETACHED_PROCESS: ohne Konsole scheitern
    # die Pipes in der Batch-Datei. Eigene Prozessgruppe, damit das Ende des Managers ihn nicht mitreißt.
    subprocess.Popen(["cmd.exe", "/c", str(helper)], cwd=str(store.BASE),
                     creationflags=manager.CREATE_NO_WINDOW | 0x00000200,                # CREATE_NEW_PROCESS_GROUP
                     close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_async(on_done) -> dict:
    """Update-Job: laden → sichern → ersetzen → Neustart planen. `on_done()` beendet den Manager."""
    info = check(force=True)
    if not info["available"]:
        raise sources.SourceError(info["error"] or "Es ist kein Update verfügbar.")
    job = manager.new_job("update", ["Update herunterladen", "Alte Version sichern", "Dateien ersetzen", "Neu starten"])

    def work() -> None:
        manager._step(job, 0, f"Version {info['latest']} wird geladen …")
        archive = store.CACHE_DIR / f"update-{info['latest']}.zip"
        sha = _expected_sha(info["sums_url"])
        sources.download(info["zip_url"], archive, manager._progress_cb(job, "Update"),
                         manager._status_cb(job), sha256=sha)
        manager._step(job, 1, "Aktuelle Programmdateien werden gesichert …")
        backup = _backup(info["current"])
        manager._step(job, 2, "Dateien werden ersetzt …")
        n = _extract(archive, job)
        (store.DATA_DIR / "last-update.json").write_text(json.dumps({
            "from": info["current"], "to": info["latest"], "files": n, "backup": backup.name,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        manager._step(job, 3, "Manager startet neu – das neue Fenster öffnet sich gleich …")
        _schedule_restart()
        manager._finish(job)
        threading.Timer(2.5, on_done).start()          # der GUI Zeit lassen, den Abschluss zu sehen

    def run() -> None:
        try:
            work()
        except Exception as exc:                      # noqa: BLE001 – in der GUI anzeigen
            manager._finish(job, str(exc))

    threading.Thread(target=run, daemon=True).start()
    return job
