"""Modpack-Server von Modrinth (.mrpack): Paket lesen, Mod-Loader installieren, Mods laden, Startbefehl.

Ein .mrpack ist ein ZIP mit `modrinth.index.json` (Abhängigkeiten = Minecraft- und Loader-Version,
`files` = herunterzuladende Mods mit Pfad, Download-URLs, Prüfsummen und env.client/env.server)
sowie den Ordnern `overrides/` und `server-overrides/`, die 1:1 in den Server-Ordner kopiert werden.

Der Server-Ordner sieht danach so aus:
  Fabric   : server.jar (Fabric-Server-Launcher, lädt Vanilla nach .fabric/server/)      -> java -jar server.jar
  Quilt    : quilt-server-launch.jar + server.jar (Vanilla, vom Quilt-Installer geladen)  -> java -jar quilt-server-launch.jar
  NeoForge : libraries/ + user_jvm_args.txt + libraries/net/neoforged/neoforge/<v>/win_args.txt
  Forge    : libraries/ + user_jvm_args.txt + libraries/net/minecraftforge/forge/<mc>-<v>/win_args.txt
Die installierten Pack-Dateien stehen in `modpack-index.json`, damit ein Versionswechsel die alten
Mods wieder entfernen kann, ohne Welt und eigene Konfiguration anzufassen.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import posixpath
import re
import shutil
import subprocess
import sys
import zipfile

from . import sources, store

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
INDEX_NAME = "modrinth.index.json"
INSTALLED_INDEX = "modpack-index.json"
INSTALLER_TIMEOUT = 1800                        # NeoForge/Forge laden Vanilla + Bibliotheken nach
LOADER_KEYS = {"fabric-loader": "fabric", "quilt-loader": "quilt", "neoforge": "neoforge", "forge": "forge"}
LOADER_NAMES = {"fabric": "Fabric", "quilt": "Quilt", "neoforge": "NeoForge", "forge": "Forge"}
FABRIC_LAUNCHER = "server.jar"
QUILT_LAUNCHER = "quilt-server-launch.jar"
# Technische Dateien der Loader – im Dateibrowser standardmäßig ausgeblendet.
TECH_FILES = {"user_jvm_args.txt", "run.bat", "run.sh", "fabric-server-launcher.properties",
              "quilt-server-launcher.properties", INSTALLED_INDEX, ".fabric", ".quilt", "installer.log",
              QUILT_LAUNCHER, "server.jar", "libraries", "versions", "mods-unsupported"}
# Dateien, die dem Nutzer gehören: Pack-Overrides überschreiben sie bei „Neu installieren“ / Versionswechsel
# nicht (bei einer frischen Installation existieren sie noch nicht, dann greifen die Vorgaben des Packs).
KEEP_IF_PRESENT = {"server.properties", "eula.txt", "whitelist.json", "ops.json", "banned-players.json",
                   "banned-ips.json", "usercache.json", "user_jvm_args.txt", INSTALLED_INDEX}


def is_modpack(cfg: dict) -> bool:
    return cfg.get("type") == "java" and str(cfg.get("flavor") or "paper") != "paper"


def pack_file(cfg: dict) -> pathlib.Path:
    mp = cfg.get("modpack") or {}
    name = f"{mp.get('project_id') or 'pack'}-{mp.get('version_id') or 'latest'}.mrpack"
    return store.CACHE_DIR / "modpacks" / re.sub(r"[^0-9A-Za-z.\-_]", "_", name)


# ---------------------------------------------------------------- .mrpack lesen

def _safe_rel(path: str) -> str:
    """Relativer POSIX-Pfad innerhalb des Server-Ordners – alles andere (.., absolut, Laufwerk) wird abgelehnt."""
    rel = str(path or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or ":" in rel or "\x00" in rel or ".." in rel.split("/"):
        raise sources.SourceError(f"Das Modpack enthält einen unzulässigen Pfad: {path!r}")
    norm = posixpath.normpath(rel)
    if norm.startswith("/") or ".." in norm.split("/") or norm in (".", ""):
        raise sources.SourceError(f"Das Modpack enthält einen unzulässigen Pfad: {path!r}")
    return norm


def read_index(mrpack: pathlib.Path) -> dict:
    """Liest modrinth.index.json und liefert {name, version, mc_version, loader, loader_version, files}."""
    try:
        with zipfile.ZipFile(mrpack) as zf:
            try:
                raw = json.loads(zf.read(INDEX_NAME).decode("utf-8"))
            except KeyError as exc:
                raise sources.SourceError("Die Datei ist kein Modrinth-Modpack (modrinth.index.json fehlt).") from exc
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise sources.SourceError(f"Das Modpack konnte nicht gelesen werden: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("game", "minecraft") != "minecraft":
        raise sources.SourceError("Das Modpack ist nicht für Minecraft.")
    if int(raw.get("formatVersion") or 1) != 1:
        raise sources.SourceError(f"Unbekanntes Modpack-Format (formatVersion {raw.get('formatVersion')}).")

    deps = raw.get("dependencies") or {}
    loader, loader_version = "", ""
    for key, name in LOADER_KEYS.items():
        if deps.get(key):
            loader, loader_version = name, str(deps[key])
            break
    mc_version = str(deps.get("minecraft") or "")
    if not mc_version:
        raise sources.SourceError("Das Modpack nennt keine Minecraft-Version.")
    if not loader:
        raise sources.SourceError("Das Modpack nennt keinen unterstützten Mod-Loader (Fabric, Quilt, NeoForge, Forge).")

    files = []
    for f in raw.get("files") or []:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        env = f.get("env") or {}
        downloads = [str(u) for u in (f.get("downloads") or []) if str(u).startswith("https://")]
        hashes = f.get("hashes") or {}
        files.append({
            "path": _safe_rel(f["path"]),
            "downloads": downloads,
            "server": str(env.get("server") or "required"),
            "size": int(f.get("fileSize") or 0),
            "sha1": str(hashes.get("sha1") or "").lower(),
            "sha512": str(hashes.get("sha512") or "").lower(),
        })
    return {"name": str(raw.get("name") or ""), "version": str(raw.get("versionId") or ""),
            "mc_version": mc_version, "loader": loader, "loader_version": loader_version, "files": files}


# ---------------------------------------------------------------- Prüfsummen

def _digest(path: pathlib.Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def _matches(path: pathlib.Path, sha1: str = "", sha512: str = "") -> bool:
    if not path.is_file():
        return False
    if sha512:
        return _digest(path, "sha512") == sha512.lower()
    if sha1:
        return _digest(path, "sha1") == sha1.lower()
    return True


def _verify(path: pathlib.Path, sha1: str = "", sha512: str = "") -> None:
    if (sha1 or sha512) and not _matches(path, sha1, sha512):
        path.unlink(missing_ok=True)
        raise sources.SourceError(f"Prüfsumme von {path.name} stimmt nicht – Datei ist beschädigt.")


def download_pack(version: dict, progress=None, status=None) -> pathlib.Path:
    """Lädt die .mrpack-Datei einer Modrinth-Version in den Cache (Prüfsumme wird kontrolliert)."""
    target = store.CACHE_DIR / "modpacks" / re.sub(r"[^0-9A-Za-z.\-_]", "_", f"{version['project_id']}-{version['id']}.mrpack")
    if _matches(target, version.get("sha1", ""), version.get("sha512", "")):
        return target
    sources.download(version["url"], target, progress, status)
    _verify(target, version.get("sha1", ""), version.get("sha512", ""))
    return target


# ---------------------------------------------------------------- Loader installieren

def _run_installer(cmd: list[str], cwd: pathlib.Path, what: str) -> None:
    log = cwd / "installer.log"
    try:
        with log.open("wb") as fh:
            res = subprocess.run(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                 timeout=INSTALLER_TIMEOUT, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise sources.SourceError(f"{what} hat zu lange gebraucht und wurde abgebrochen.") from exc
    except OSError as exc:
        raise sources.SourceError(f"{what} konnte nicht gestartet werden: {exc}") from exc
    if res.returncode != 0:
        tail = ""
        try:
            tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
            tail = " | ".join(t.strip() for t in tail)[-600:]
        except OSError:
            pass
        raise sources.SourceError(f"{what} ist fehlgeschlagen (Code {res.returncode}). {tail}")


def _args_file(sdir: pathlib.Path, loader: str, mc_version: str, loader_version: str) -> pathlib.Path:
    name = "win_args.txt" if sys.platform == "win32" else "unix_args.txt"
    if loader == "neoforge":
        return sdir / "libraries" / "net" / "neoforged" / "neoforge" / loader_version / name
    return sdir / "libraries" / "net" / "minecraftforge" / "forge" / f"{mc_version}-{loader_version}" / name


def _legacy_forge_jar(sdir: pathlib.Path) -> pathlib.Path | None:
    """Forge vor 1.17 legt ein direkt startbares forge-<mc>-<v>.jar an (ohne Argument-Dateien)."""
    jars = [p for p in sdir.glob("forge-*.jar") if "installer" not in p.name.lower()]
    return jars[0] if jars else None


def install_loader(sdir: pathlib.Path, index: dict, java: pathlib.Path, status=None, progress=None) -> str:
    """Installiert den Mod-Loader-Server in sdir; liefert die tatsächlich verwendete Loader-Version."""
    loader, mc, version = index["loader"], index["mc_version"], index.get("loader_version") or ""
    say = status or (lambda _t: None)

    if loader == "fabric":
        if not version:
            version = sources.fabric_loader_version(mc)
        say(f"Fabric-Loader {version} für Minecraft {mc} wird geladen …")
        url = sources.fabric_server_jar_url(mc, version)
        (sdir / "build.txt").unlink(missing_ok=True)          # sonst hielte Paper das JAR für seines
        sources.download(url, sdir / FABRIC_LAUNCHER, progress, status)
        # Der Launcher lädt den Vanilla-Server beim ersten Start selbst nach .fabric/server/.
        (sdir / "fabric-server-launcher.properties").write_text("serverJar=server.jar\n", encoding="utf-8")
        return version

    if loader == "quilt":
        if not version:
            version = sources.quilt_loader_version(mc)
        say("Quilt-Installer wird geladen …")
        url, inst_version, sha256 = sources.quilt_installer()
        installer = store.CACHE_DIR / f"quilt-installer-{inst_version}.jar"
        if not installer.exists():
            sources.download(url, installer, progress, status, sha256=sha256)
        say(f"Quilt {version} für Minecraft {mc} wird installiert (lädt den Vanilla-Server) …")
        (sdir / "build.txt").unlink(missing_ok=True)
        _run_installer([str(java), "-jar", str(installer), "install", "server", mc, version,
                        "--download-server", f"--install-dir={sdir}"], sdir, "Der Quilt-Installer")
        if not (sdir / QUILT_LAUNCHER).exists():
            raise sources.SourceError(f"{QUILT_LAUNCHER} wurde vom Quilt-Installer nicht angelegt.")
        return version

    if loader in ("neoforge", "forge"):
        if not version:
            raise sources.SourceError(f"Das Modpack nennt keine {LOADER_NAMES[loader]}-Version.")
        args = _args_file(sdir, loader, mc, version)
        if args.exists():
            say(f"{LOADER_NAMES[loader]} {version} ist bereits installiert")
            return version
        if loader == "neoforge":
            url = sources.neoforge_installer_url(version)
            installer = store.CACHE_DIR / f"neoforge-{version}-installer.jar"
        else:
            url = sources.forge_installer_url(mc, version)
            installer = store.CACHE_DIR / f"forge-{mc}-{version}-installer.jar"
        say(f"{LOADER_NAMES[loader]}-Installer {version} wird geladen …")
        if not installer.exists():
            sources.download(url, installer, progress, status)
        say(f"{LOADER_NAMES[loader]} {version} wird installiert (lädt Vanilla-Server und Bibliotheken – dauert einige Minuten) …")
        (sdir / "build.txt").unlink(missing_ok=True)
        _run_installer([str(java), "-jar", str(installer), "--installServer", str(sdir)], sdir,
                       f"Der {LOADER_NAMES[loader]}-Installer")
        if not args.exists() and not (loader == "forge" and _legacy_forge_jar(sdir)):
            raise sources.SourceError(f"{args.name} wurde vom {LOADER_NAMES[loader]}-Installer nicht angelegt.")
        # Der Installer schreibt ein eigenes Protokoll neben sich – in den Server-Ordner gehört es nicht.
        for p in sdir.glob("*-installer.jar.log"):
            p.unlink(missing_ok=True)
        return version

    raise sources.SourceError(f"Unbekannter Mod-Loader: {loader}")


# ---------------------------------------------------------------- Pack-Dateien

def installed_index(sdir: pathlib.Path) -> dict:
    try:
        data = json.loads((sdir / INSTALLED_INDEX).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remove_installed(sdir: pathlib.Path, keep: set[str] | None = None) -> int:
    """Entfernt die Dateien des zuvor installierten Packs: alle Einträge aus `files` sowie die per
    Overrides gelieferten Mods (mods/…). Pfade in `keep` liefert das neue Pack erneut – sie bleiben liegen
    und install_files prüft sie per Prüfsumme. Eigene Konfiguration (config/…) bleibt unangetastet.
    Liefert die Anzahl der entfernten Dateien."""
    old = installed_index(sdir)
    keep = keep or set()
    rels = list(old.get("files") or []) + [r for r in (old.get("overrides") or [])
                                            if str(r).replace("\\", "/").lower().startswith("mods/")]
    n = 0
    for rel in rels:
        try:
            rel = _safe_rel(rel)
        except sources.SourceError:
            continue
        if rel in keep:
            continue
        target = sdir / rel
        if target.is_file():
            target.unlink(missing_ok=True)
            n += 1
    return n


def _extract_overrides(zf: zipfile.ZipFile, folder: str, sdir: pathlib.Path) -> list[str]:
    root = sdir.resolve()
    prefix = folder.rstrip("/") + "/"
    written = []
    for member in zf.namelist():
        name = member.replace("\\", "/")            # manche Packer schreiben Backslashes ins ZIP
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        rel = _safe_rel(name[len(prefix):])
        dest = (sdir / rel).resolve()
        if root != dest and root not in dest.parents:               # Zip-Slip
            continue
        if rel in KEEP_IF_PRESENT and dest.exists():                # Nutzerdateien nicht überschreiben
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)
        written.append(rel)
    return written


def install_files(sdir: pathlib.Path, mrpack: pathlib.Path, index: dict, meta: dict,
                  progress=None, status=None) -> dict:
    """Lädt alle serverseitigen Dateien des Packs, kopiert die Overrides und schreibt modpack-index.json.
    progress(n, total, name) meldet den Fortschritt; meta (project_id, version_id …) landet im Index."""
    say = status or (lambda _t: None)
    root = sdir.resolve()
    wanted = [f for f in index["files"] if f["server"] != "unsupported"]
    # Vorläufigen Index schreiben, bevor geladen wird: bricht die Installation ab, kennt ein späterer
    # Versionswechsel auch die schon geladenen Dateien und räumt sie mit auf.
    prev = installed_index(sdir)
    provisional = dict(prev, files=sorted(set(prev.get("files") or []) | {f["path"] for f in wanted}))
    (sdir / INSTALLED_INDEX).write_text(json.dumps(provisional, indent=2, ensure_ascii=False), encoding="utf-8")
    done_paths: list[str] = []
    for i, f in enumerate(wanted, 1):
        dest = (sdir / f["path"]).resolve()
        if root not in dest.parents:
            continue
        name = pathlib.PurePosixPath(f["path"]).name
        if progress:
            progress(i, len(wanted), name)
        if _matches(dest, f["sha1"], f["sha512"]):
            done_paths.append(f["path"])
            continue
        if not f["downloads"]:
            raise sources.SourceError(f"Für {name} gibt es keine Download-Adresse.")
        last = ""
        for url in f["downloads"]:
            try:
                sources.download(url, dest, None, None)
                _verify(dest, f["sha1"], f["sha512"])
                break
            except sources.SourceError as exc:
                last = str(exc)
        else:
            raise sources.SourceError(f"{name} konnte nicht geladen werden: {last}")
        done_paths.append(f["path"])

    say("Konfigurationsdateien des Packs werden übernommen …")
    overrides: list[str] = []
    with zipfile.ZipFile(mrpack) as zf:
        overrides += _extract_overrides(zf, "overrides", sdir)
        overrides += _extract_overrides(zf, "server-overrides", sdir)

    record = {"project_id": meta.get("project_id", ""), "version_id": meta.get("version_id", ""),
              "version_name": meta.get("version_name") or index.get("version", ""),
              "mc_version": index["mc_version"], "loader": index["loader"],
              "loader_version": meta.get("loader_version") or index.get("loader_version", ""),
              "files": done_paths, "overrides": overrides}
    (sdir / INSTALLED_INDEX).write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ---------------------------------------------------------------- Startbefehl

def start_command(cfg: dict, java_exe: str | pathlib.Path, ram_mb: int) -> list[str]:
    """Startzeile für einen Modpack-Server (relativ zum Server-Ordner, dort wird der Prozess gestartet)."""
    sdir = store.server_dir(cfg["id"])
    flavor = str(cfg.get("flavor") or "paper")
    mp = cfg.get("modpack") or {}
    ram = int(ram_mb)
    cmd = [str(java_exe)]
    # Argument-Datei des Installers zuerst, dann die Werte des Managers – bei doppeltem -Xmx gewinnt der
    # letzte Wert, so kann ein user_jvm_args.txt (aus dem Pack oder per Dateibrowser) den RAM-Regler nicht aushebeln.
    if flavor in ("neoforge", "forge") and (sdir / "user_jvm_args.txt").exists():
        cmd.append("@user_jvm_args.txt")
    cmd += [f"-Xms{max(512, ram // 2)}M", f"-Xmx{ram}M",
            "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8", "-Dstdin.encoding=UTF-8"]

    if flavor == "fabric":
        if not (sdir / FABRIC_LAUNCHER).exists():
            raise RuntimeError("Der Fabric-Server ist nicht vollständig installiert – bitte „Neu installieren“.")
        return cmd + ["-jar", FABRIC_LAUNCHER, "nogui"]
    if flavor == "quilt":
        if not (sdir / QUILT_LAUNCHER).exists():
            raise RuntimeError("Der Quilt-Server ist nicht vollständig installiert – bitte „Neu installieren“.")
        return cmd + ["-jar", QUILT_LAUNCHER, "nogui"]
    if flavor in ("neoforge", "forge"):
        mc = str(mp.get("mc_version") or cfg.get("version") or "")
        args = _args_file(sdir, flavor, mc, str(mp.get("loader_version") or ""))
        if args.exists():
            return cmd + [f"@{args.relative_to(sdir).as_posix()}", "nogui"]
        legacy = _legacy_forge_jar(sdir) if flavor == "forge" else None
        if legacy:
            return cmd + ["-jar", legacy.name, "nogui"]
        raise RuntimeError(f"Der {LOADER_NAMES[flavor]}-Server ist nicht vollständig installiert – bitte „Neu installieren“.")
    raise RuntimeError(f"Unbekannter Mod-Loader: {flavor}")
