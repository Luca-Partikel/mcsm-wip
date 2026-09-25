"""Download-Quellen für den Root-Server (Linux x64).

Gleiche Quellen wie im lokalen Programm (`core/sources.py`), aber die Linux-Varianten:

* **Paper** – Build aus der Fill-API v3 (mit SHA256 aus der API)
* **Java** – Temurin-JRE von Adoptium als `tar.gz` für `linux/x64` (SHA256 aus der API)
* **Bedrock** – `bedrock-server-<version>.zip` aus `bin-linux` (Mojang nennt keine Prüfsumme;
  die berechnete wird neben der Datei abgelegt und beim nächsten Griff in den Zwischenspeicher
  geprüft)
* **Geyser/Floodgate** und **ViaVersion/ViaBackwards** – jeweils mit SHA256 aus der API

Ablage: Downloads in `/srv/mcsm/cache`, Java-Laufzeiten in `/srv/mcsm/runtime`.

Alle Fehler kommen als `SourceError` (eine Art von `ValueError`) mit einem verständlichen
deutschen Satz. Es werden nur die Standardbibliothek und keine Shell-Aufrufe benutzt.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Iterable, Sequence

UA = "MinecraftServerManager/1.0 (Root-Server)"
TIMEOUT = 45
DOWNLOAD_ATTEMPTS = 3
CHUNK = 262144

# Platte nicht vollschreiben: unter diesem Rest wird kein Download begonnen.
MIN_FREE_MB = 2048

PAPER_API = "https://fill.papermc.io/v3/projects/paper"
BEDROCK_LINKS_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
ADOPTIUM_API = ("https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
                "?os=linux&architecture=x64&image_type=jre")
GEYSER_BUILD_API = "https://download.geysermc.org/v2/projects/{project}/versions/latest/builds/latest"
GEYSER_DOWNLOAD = ("https://download.geysermc.org/v2/projects/{project}/versions/{version}"
                   "/builds/{build}/downloads/{flavor}")
HANGAR_LATEST = "https://hangar.papermc.io/api/v1/projects/{project}/latestrelease"
HANGAR_VERSION = "https://hangar.papermc.io/api/v1/projects/{project}/versions/{version}"

# Nur von diesen Hosts wird geladen. Eine manipulierte API-Antwort kann den Daemon so nicht
# dazu bringen, irgendetwas aus dem Internet zu holen.
_PAPER_HOSTS = ("fill.papermc.io", "fill-data.papermc.io", "api.papermc.io")
_ADOPTIUM_HOSTS = ("api.adoptium.net", "github.com", "objects.githubusercontent.com")
_BEDROCK_HOSTS = ("www.minecraft.net", "net-secondary.web.minecraft-services.net")
_GEYSER_HOSTS = ("download.geysermc.org",)
_HANGAR_HOSTS = ("hangar.papermc.io", "hangarcdn.papermc.io")

CROSSPLAY_JARS = ("Geyser-Spigot.jar", "floodgate-spigot.jar", "ViaVersion.jar", "ViaBackwards.jar")

AVAILABLE_JAVA = (8, 11, 17, 21, 25)            # LTS-Versionen, die Adoptium als JRE anbietet

_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.\-_]{0,31}")
_BEDROCK_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){2,3}")
_HANGAR_PROJECT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class SourceError(ValueError):
    """Eine Quelle war nicht erreichbar oder hat etwas Unbrauchbares geliefert."""


class DiskFull(SourceError):
    """Auf der Platte ist zu wenig Platz. Ein zweiter Versuch hilft hier nicht."""


# ---------------------------------------------------------------- Ordner

_root: pathlib.Path = pathlib.Path(os.environ.get("MCSM_DATA_ROOT") or "/srv/mcsm")


def data_root() -> pathlib.Path:
    return _root


def set_data_root(path: str | os.PathLike) -> pathlib.Path:
    """Datenwurzel umstellen (Selbsttests, abweichende Installation)."""
    global _root
    _root = pathlib.Path(path).expanduser().resolve()
    return _root


def cache_dir() -> pathlib.Path:
    return _root / "cache"


def runtime_dir() -> pathlib.Path:
    return _root / "runtime"


def users_dir() -> pathlib.Path:
    return _root / "users"


def transfer_dir() -> pathlib.Path:
    return _root / "transfer"


def ensure_dirs() -> None:
    for folder in (cache_dir(), runtime_dir(), users_dir(), transfer_dir()):
        folder.mkdir(parents=True, exist_ok=True)


def free_disk_mb(path: str | os.PathLike | None = None) -> int:
    """Freier Platz in MB auf der Platte, auf der dieser Pfad liegt."""
    target = pathlib.Path(path) if path is not None else _root
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return int(shutil.disk_usage(str(target)).free / 1048576)
    except OSError:
        return 0


def require_free_disk(need_mb: int = 0, path: str | os.PathLike | None = None) -> None:
    """Vor größeren Downloads prüfen, dass die Platte nicht vollläuft."""
    free = free_disk_mb(path)
    need = max(int(need_mb or 0), 0) + MIN_FREE_MB
    if free < need:
        raise DiskFull(f"Auf dem Server sind nur noch {free} MB frei – dafür werden "
                       f"mindestens {need} MB gebraucht. Bitte alte Sicherungen entfernen.")


# ---------------------------------------------------------------- HTTP

def _check_url(url: str, hosts: Sequence[str]) -> str:
    """Nur HTTPS und nur die erwarteten Hosts zulassen."""
    text = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise SourceError(f"Unbrauchbare Adresse: {text!r}") from exc
    if parts.scheme != "https" or not parts.hostname:
        raise SourceError(f"Nur HTTPS-Adressen sind erlaubt: {text!r}")
    if hosts and parts.hostname.lower() not in [h.lower() for h in hosts]:
        raise SourceError(f"Die Adresse {parts.hostname} gehört nicht zu den erwarteten "
                          "Download-Quellen und wird nicht geladen.")
    return text


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})


def _get_bytes(url: str, hosts: Sequence[str] = (), limit: int = 8_000_000) -> bytes:
    checked = _check_url(url, hosts)
    try:
        with urllib.request.urlopen(_request(checked), timeout=TIMEOUT) as resp:
            return resp.read(limit)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise SourceError(f"Konnte {checked} nicht laden: {exc}") from exc


def fetch_json(url: str, hosts: Sequence[str] = ()):
    raw = _get_bytes(url, hosts)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceError(f"Die Antwort von {url} war kein gültiges JSON.") from exc


def fetch_text(url: str, hosts: Sequence[str] = ()) -> str:
    try:
        return _get_bytes(url, hosts, limit=4096).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SourceError(f"Die Antwort von {url} war kein Text.") from exc


def sha256_of(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar(dest: pathlib.Path) -> pathlib.Path:
    return dest.with_name(dest.name + ".sha256")


def _in_cache(dest: pathlib.Path) -> bool:
    try:
        dest.resolve().relative_to(cache_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def _write_sidecar(dest: pathlib.Path, digest: str) -> None:
    """Prüfsumme neben die Datei legen – damit lässt sich ein beschädigter Zwischenspeicher
    auch bei Quellen ohne veröffentlichte Prüfsumme erkennen.

    Nur im Zwischenspeicher: in einem Instanzordner hätte der Besitzer sonst lauter
    `.sha256`-Dateien zwischen seinen Plugins.
    """
    if not _in_cache(dest):
        return
    try:
        tmp = _sidecar(dest).with_suffix(".tmp")
        tmp.write_text(digest + "\n", encoding="utf-8")
        os.replace(tmp, _sidecar(dest))
    except OSError:
        pass


def _read_sidecar(dest: pathlib.Path) -> str:
    try:
        text = _sidecar(dest).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text.lower() if _SHA256_RE.fullmatch(text) else ""


def download(url: str, dest: str | os.PathLike, progress: Callable[[int, int], None] | None = None,
             status: Callable[[str], None] | None = None, sha256: str = "",
             hosts: Sequence[str] = ()) -> str:
    """Lädt eine Datei und gibt ihre SHA256 zurück.

    Die Datei wird erst als `.part` geschrieben und dann mit `os.replace` an ihren Platz
    gelegt – es liegt also nie eine halbe Datei herum. Unvollständige Downloads
    (Content-Length) und falsche Prüfsummen werden nie übernommen; bei Abbrüchen wird bis zu
    `DOWNLOAD_ATTEMPTS`-mal neu begonnen.

    Der Name der Zwischendatei enthält einen Zufallsanteil. Bei einem festen Namen schrieben zwei
    gleichzeitige Einrichtungen in dieselbe `.part`-Datei; jede prüfte die Prüfsumme über ihren
    *eigenen* Datenstrom, und am Ende läge eine aus beiden verschränkte Datei am Ziel.
    """
    checked = _check_url(url, hosts)
    expected = str(sha256 or "").lower()
    if expected and not _SHA256_RE.fullmatch(expected):
        raise SourceError("Die erwartete Prüfsumme ist keine SHA256.")
    target = pathlib.Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{secrets.token_hex(4)}.part")
    last = ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(_request(checked), timeout=TIMEOUT) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                require_free_disk(int(total / 1048576) if total else 0, target.parent)
                digest = hashlib.sha256()
                done = 0
                with tmp.open("wb") as fh:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            # Ein vorzeitiges Verbindungsende meldet Python nicht als Fehler – selbst prüfen.
            if total and done != total:
                raise SourceError(f"Download unvollständig ({done}/{total} Bytes)")
            if done == 0:
                raise SourceError("Die Gegenstelle hat keine Daten geliefert.")
            got = digest.hexdigest()
            if expected and got != expected:
                raise SourceError("Die Prüfsumme stimmt nicht – die Datei ist beschädigt "
                                  "oder wurde unterwegs verändert.")
            os.replace(tmp, target)
            _write_sidecar(target, got)
            return got
        except (urllib.error.URLError, http.client.HTTPException, OSError, SourceError) as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            last = str(exc)
            if isinstance(exc, DiskFull):
                break                                     # Platte voll: erneute Versuche helfen nicht
            if attempt < DOWNLOAD_ATTEMPTS:
                if status:
                    status(f"Verbindung unterbrochen – {attempt + 1}. Versuch …")
                time.sleep(2 * attempt)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise SourceError(f"Download fehlgeschlagen ({checked}): {last}")


def cached_download(url: str, dest: str | os.PathLike, sha256: str = "",
                    progress: Callable[[int, int], None] | None = None,
                    status: Callable[[str], None] | None = None,
                    hosts: Sequence[str] = ()) -> pathlib.Path:
    """Wie `download`, nutzt aber eine schon vorhandene, unbeschädigte Datei weiter."""
    target = pathlib.Path(dest)
    if target.is_file() and target.stat().st_size > 0:
        expected = str(sha256 or "").lower() or _read_sidecar(target)
        if expected:
            try:
                if sha256_of(target) == expected:
                    if status:
                        status(f"{target.name} liegt schon bereit")
                    return target
            except OSError:
                pass
        try:
            target.unlink()
        except OSError:
            pass
    download(url, target, progress, status, sha256, hosts)
    return target


# ---------------------------------------------------------------- Archive sicher entpacken

def _inside(root: pathlib.Path, name: str) -> pathlib.Path | None:
    """Zielpfad eines Archiveintrags – None, wenn der Eintrag nicht sauber im Zielordner landet.

    Abgelehnt werden absolute Pfade, Laufwerksangaben und alles mit `..`. Ein Archiv mit
    solchen Einträgen ist nichts, was wir auspacken wollen (Zip-Slip).
    """
    raw = str(name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw.split("/")[0]:
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    dest = root.joinpath(*parts)
    try:
        dest.relative_to(root)
    except ValueError:
        return None
    return dest


def extract_zip(archive: str | os.PathLike, target: str | os.PathLike,
                keep: Iterable[str] = (), keep_dirs: Iterable[str] = (),
                status: Callable[[str], None] | None = None) -> int:
    """ZIP entpacken (Zip-Slip ausgeschlossen). `keep`/`keep_dirs` bleiben unangetastet, wenn sie
    schon vorhanden sind – so überschreibt ein Update keine Welt und keine Konfiguration."""
    root = pathlib.Path(target).resolve()
    root.mkdir(parents=True, exist_ok=True)
    keep_files = set(keep)
    keep_tops = set(keep_dirs)
    written = 0
    try:
        with zipfile.ZipFile(str(archive)) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            for index, member in enumerate(members):
                dest = _inside(root, member.filename)
                if dest is None:
                    continue
                top = member.filename.replace("\\", "/").strip("/").split("/", 1)[0]
                if dest.exists() and (top in keep_tops or member.filename in keep_files):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, CHUNK)
                written += 1
                if status and index % 40 == 0:
                    status(f"Entpacke … {index}/{len(members)}")
    except (zipfile.BadZipFile, OSError) as exc:
        raise SourceError(f"Das Archiv {pathlib.Path(archive).name} ließ sich nicht entpacken "
                          f"({exc}). Bitte erneut versuchen.") from exc
    return written


def extract_tar(archive: str | os.PathLike, target: str | os.PathLike) -> int:
    """tar.gz entpacken – ohne `..`, ohne absolute Pfade, ohne Symlinks nach draußen und ohne
    Sonderdateien. (Python 3.11.2 kennt `tarfile.extractall(filter=…)` noch nicht, deshalb
    prüfen wir jeden Eintrag selbst.)"""
    root = pathlib.Path(target).resolve()
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with tarfile.open(str(archive), "r:*") as tf:
            for member in tf:
                dest = _inside(root, member.name)
                if dest is None:
                    continue
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym():
                    link = str(member.linkname or "")
                    if link.startswith("/"):
                        continue
                    joined = os.path.normpath(os.path.join(os.path.dirname(member.name), link))
                    if joined.startswith("..") or os.path.isabs(joined):
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.is_symlink() or dest.exists():
                        continue
                    try:
                        os.symlink(link, dest)
                    except (OSError, NotImplementedError):
                        continue
                    written += 1
                    continue
                if not member.isreg():
                    continue                     # harte Links, Geräte, FIFOs: nicht übernehmen
                src = tf.extractfile(member)
                if src is None:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, CHUNK)
                # Nur die harmlosen Rechte übernehmen (kein setuid/setgid, kein Schreibrecht
                # für Gruppe und andere).
                try:
                    dest.chmod(member.mode & 0o755)
                except OSError:
                    pass
                written += 1
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SourceError(f"Das Archiv {pathlib.Path(archive).name} ließ sich nicht entpacken "
                          f"({exc}). Bitte erneut versuchen.") from exc
    return written


def _safe_name(text: str, fallback: str = "datei") -> str:
    """Aus einer Angabe der API einen unbedenklichen Dateinamen machen."""
    clean = re.sub(r"[^0-9A-Za-z._+-]", "_", str(text or "")).strip("._")
    return clean[:80] or fallback


# ---------------------------------------------------------------- Paper

def _is_stable(version: str) -> bool:
    return not re.search(r"(pre|rc|snapshot)", version, re.I)


def check_version(version: str) -> str:
    text = str(version or "").strip()
    if not _VERSION_RE.fullmatch(text):
        raise SourceError(f"„{version}“ ist keine gültige Minecraft-Version.")
    return text


_stable_build_cache: dict[str, dict | None] = {}
_versions_cache: dict[str, tuple[float, list[str]]] = {}
_java_info_cache: dict[str, dict] = {}
VERSIONS_CACHE_SECONDS = 600


def paper_stable_build(version: str) -> dict | None:
    """Neuester Build im Kanal STABLE – ganz neue Versionen haben oft nur ALPHA/BETA-Builds,
    auf denen Geyser & Co. nicht laufen."""
    version = check_version(version)
    if version in _stable_build_cache:
        return _stable_build_cache[version]
    data = fetch_json(f"{PAPER_API}/versions/{urllib.parse.quote(version)}/builds", _PAPER_HOSTS)
    builds = data if isinstance(data, list) else (data.get("builds") or [])
    stable = [b for b in builds if isinstance(b, dict)
              and str(b.get("channel", "")).upper() == "STABLE"
              and ((b.get("downloads") or {}).get("server:default") or {}).get("url")]
    best = max(stable, key=lambda b: int(b.get("id") or 0)) if stable else None
    _stable_build_cache[version] = best
    return best


def paper_versions() -> list[str]:
    """Stabile Paper-Versionen, neueste zuerst."""
    cached = _versions_cache.get("paper")
    if cached and time.time() - cached[0] < VERSIONS_CACHE_SECONDS:
        return list(cached[1])
    data = fetch_json(PAPER_API, _PAPER_HOSTS)
    versions: list[str] = []
    for group in (data.get("versions") or {}).values():
        versions.extend(str(v) for v in group if _is_stable(str(v)))
    checked: list[str] = []
    incomplete = False
    for index, version in enumerate(versions):
        if index < 6:
            try:
                if paper_stable_build(version) is None:
                    continue
            except SourceError:
                incomplete = True        # vorübergehender Fehler: Version lieber behalten
        checked.append(version)
    if not incomplete:
        _versions_cache["paper"] = (time.time(), checked)
    return checked


def paper_download(version: str) -> tuple[str, str, str]:
    """(url, dateiname, sha256) des neuesten STABLE-Builds."""
    version = check_version(version)
    try:
        data = paper_stable_build(version)
    except SourceError:
        data = paper_stable_build(version)
    if data is None:
        data = fetch_json(f"{PAPER_API}/versions/{urllib.parse.quote(version)}/builds/latest",
                          _PAPER_HOSTS)
    entry = ((data or {}).get("downloads") or {}).get("server:default") or {}
    url = str(entry.get("url") or "")
    if not url:
        raise SourceError(f"Für Paper {version} gibt es keinen Download.")
    sha = str((entry.get("checksums") or {}).get("sha256") or "")
    name = _safe_name(entry.get("name") or f"paper-{version}.jar", f"paper-{version}.jar")
    return _check_url(url, _PAPER_HOSTS), name, sha


def version_tuple(version: str) -> tuple[int, ...]:
    """'1.21.11' -> (1, 21, 11); '26.3' -> (26, 3); unbekannt -> ()."""
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(version or ""))
    if not match:
        return ()
    return tuple(int(g) for g in match.groups() if g is not None)


def command_block_gamerule(version: str) -> str | None:
    """Seit 1.21.9 sind pvp/enable-command-block keine server.properties, sondern Gamerules.
    None = die Version schreibt die Werte noch in server.properties."""
    value = version_tuple(version)
    if not value:
        return None
    if value[0] != 1 or value >= (1, 21, 11):
        return "command_blocks_work"
    if value >= (1, 21, 9):
        return "commandBlocksEnabled"
    return None


def _pick_java(minimum: int) -> int:
    for major in AVAILABLE_JAVA:
        if major >= minimum:
            return major
    return AVAILABLE_JAVA[-1]


def _guess_java_major(version: str) -> int:
    """Schätzung, falls die PaperMC-API nicht erreichbar ist."""
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(version or ""))
    if not match:
        return 25
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    if major != 1:
        return 25
    if minor >= 21 or (minor == 20 and patch >= 5):
        return 21
    if minor >= 17:
        return 17
    return 8


def paper_java_info(version: str) -> dict:
    """{'major': 21, 'flags': [...]} – nötige Java-Version und empfohlene JVM-Schalter."""
    version = check_version(version)
    if version in _java_info_cache:
        return dict(_java_info_cache[version])
    info = {"major": _guess_java_major(version), "flags": []}
    try:
        data = fetch_json(f"{PAPER_API}/versions/{urllib.parse.quote(version)}", _PAPER_HOSTS)
        java = ((data.get("version") or data).get("java") or {})
        info["major"] = _pick_java(int((java.get("version") or {}).get("minimum")))
        flags = (java.get("flags") or {}).get("recommended") or []
        info["flags"] = [f for f in flags if isinstance(f, str) and f.startswith(("-XX:", "-D"))]
        _java_info_cache[version] = dict(info)
    except (SourceError, TypeError, ValueError, AttributeError):
        pass
    return info


def java_major_for(version: str) -> int:
    return int(paper_java_info(version)["major"])


# ---------------------------------------------------------------- Java-Laufzeit (Adoptium)

def adoptium_jre(major: int) -> tuple[str, str, str]:
    """(url, release_name, sha256) des Temurin-JRE als tar.gz für Linux x64."""
    try:
        major = int(major)
    except (TypeError, ValueError) as exc:
        raise SourceError("Die Java-Version muss eine Zahl sein.") from exc
    if not 8 <= major <= 99:
        raise SourceError(f"Java {major} gibt es bei Adoptium nicht.")
    data = fetch_json(ADOPTIUM_API.format(major=major), _ADOPTIUM_HOSTS)
    for asset in data if isinstance(data, list) else []:
        binary = asset.get("binary") or {}
        package = binary.get("package") or {}
        link = str(package.get("link") or "")
        if (binary.get("os") == "linux" and binary.get("architecture") == "x64"
                and link.endswith(".tar.gz")):
            checksum = str(package.get("checksum") or "")
            return (_check_url(link, _ADOPTIUM_HOSTS),
                    str(asset.get("release_name") or f"jdk-{major}"),
                    checksum if _SHA256_RE.fullmatch(checksum) else "")
    raise SourceError(f"Kein Temurin-JRE {major} für Linux x64 gefunden.")


def _find_java_in(root: pathlib.Path) -> pathlib.Path | None:
    if not root.is_dir():
        return None
    direct = root / "bin" / "java"
    if direct.is_file():
        return direct
    try:
        children = sorted(root.iterdir())
    except OSError:
        return None
    for child in children:
        candidate = child / "bin" / "java"
        if child.is_dir() and candidate.is_file():
            return candidate
    return None


def java_major_of(exe: str | os.PathLike) -> int | None:
    """Major-Version einer Java-Datei (startet sie wirklich?). Kein Shell-Aufruf."""
    try:
        res = subprocess.run([str(exe), "-version"], capture_output=True, text=True,  # noqa: S603
                             timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'version "(\d+)(?:\.(\d+))?', (res.stderr or "") + (res.stdout or ""))
    if not match:
        return None
    major = int(match.group(1))
    return int(match.group(2) or 0) if major == 1 else major


def java_path(major: int) -> pathlib.Path | None:
    """Vorhandene, funktionsfähige Java-Laufzeit der geforderten Version."""
    try:
        major = int(major)
    except (TypeError, ValueError):
        return None
    bundled = _find_java_in(runtime_dir() / f"jre{major}")
    if bundled and java_major_of(bundled) == major:
        return bundled
    system = shutil.which("java")
    if system and java_major_of(system) == major:
        return pathlib.Path(system)
    return None


_java_locks_guard = threading.Lock()
_java_locks: dict[int, threading.Lock] = {}


def _java_lock(major: int) -> threading.Lock:
    """Je Java-Version eine Sperre: zwei Einrichtungen dürfen nicht dieselbe Laufzeit auspacken."""
    with _java_locks_guard:
        return _java_locks.setdefault(int(major), threading.Lock())


def ensure_java(major: int, progress: Callable[[int, int], None] | None = None,
                status: Callable[[str], None] | None = None) -> pathlib.Path:
    """Java-Laufzeit bereitstellen und den Pfad zur `java`-Datei zurückgeben.

    Alles ab dem Herunterladen läuft unter einer Sperre je Version: sonst laden zwei
    Einrichtungen dieselbe Datei in denselben Zwischenspeicher und tauschen gleichzeitig den
    Ordner ``runtime/jre<n>`` aus – der zweite zieht dem ersten den Boden weg.
    """
    major = int(major)
    existing = java_path(major)
    if existing is not None:
        return existing
    with _java_lock(major):
        existing = java_path(major)              # inzwischen von einem anderen Auftrag erledigt?
        if existing is not None:
            return existing
        if status:
            status(f"Java {major} wird von Adoptium geladen …")
        url, release, sha = adoptium_jre(major)
        archive = cache_dir() / f"jre{major}-{_safe_name(release, f'jdk-{major}')}.tar.gz"
        cached_download(url, archive, sha, progress, status, _ADOPTIUM_HOSTS)

        target = runtime_dir() / f"jre{major}"
        if status:
            status(f"Java {major} wird entpackt …")
        # Erst in einen Nebenordner entpacken und prüfen, dann tauschen – so bleibt nie eine halb
        # entpackte Laufzeit liegen, die zwar `java` hat, aber nicht startet.
        tmp_dir = target.with_name(f"{target.name}.{secrets.token_hex(4)}.tmp")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            extract_tar(archive, tmp_dir)
            java = _find_java_in(tmp_dir)
            if java is not None:
                try:
                    # a+rx: der Serverprozess laeuft unter einem eigenen Unix-Benutzer und muss
                    # diese Java-Laufzeit ausfuehren koennen (core/isolation.py).
                    java.chmod(0o755)
                except OSError:
                    pass
            if java is None or java_major_of(java) != major:
                raise SourceError(f"Die Java-Laufzeit {major} ließ sich nicht einrichten – "
                                  "bitte erneut versuchen.")
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            os.replace(tmp_dir, target)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        found = _find_java_in(target)
        if found is None:
            raise SourceError("Die Java-Laufzeit konnte nicht abgelegt werden.")
        return found


# ---------------------------------------------------------------- Bedrock (Linux)

def bedrock_links() -> dict:
    data = fetch_json(BEDROCK_LINKS_API, _BEDROCK_HOSTS)
    out: dict[str, str] = {}
    for link in ((data.get("result") or {}).get("links") or []):
        if isinstance(link, dict):
            out[str(link.get("downloadType") or "")] = str(link.get("downloadUrl") or "")
    return out


def _version_from_url(url: str) -> str:
    match = re.search(r"bedrock-server-([0-9.]+)\.zip", str(url or ""))
    return match.group(1) if match else ""


def bedrock_versions() -> dict:
    """{'latest': '1.26.51.1', 'preview': '1.26.60.28'} – die Linux-Ausgaben von Mojang."""
    links = bedrock_links()
    return {"latest": _version_from_url(links.get("serverBedrockLinux", "")),
            "preview": _version_from_url(links.get("serverBedrockPreviewLinux", ""))}


def bedrock_download_url(version: str, preview: bool = False) -> str:
    """Die Linux-ZIP einer Bedrock-Version (`bin-linux`, nicht `bin-win`)."""
    text = str(version or "").strip()
    if not _BEDROCK_VERSION_RE.fullmatch(text):
        raise SourceError(f"„{version}“ ist keine gültige Bedrock-Version (z. B. 1.26.51.1).")
    folder = "bin-linux-preview" if preview else "bin-linux"
    return _check_url(f"https://www.minecraft.net/bedrockdedicatedserver/{folder}/"
                      f"bedrock-server-{text}.zip", _BEDROCK_HOSTS)


# Diese Dateien gehören nach der ersten Installation dem Benutzer und werden bei einem
# Update nicht überschrieben (wie im lokalen Programm).
BEDROCK_KEEP = ("server.properties", "permissions.json", "allowlist.json", "whitelist.json")
BEDROCK_KEEP_DIRS = ("worlds",)
BEDROCK_BINARY = "bedrock_server"


def install_bedrock(version: str, target: str | os.PathLike, preview: bool = False,
                    progress: Callable[[int, int], None] | None = None,
                    status: Callable[[str], None] | None = None) -> dict:
    """Bedrock-Server in den Instanzordner legen. Welt und Konfiguration bleiben erhalten."""
    url = bedrock_download_url(version, preview)
    archive = cache_dir() / f"bedrock-server-{_safe_name(version)}.zip"
    if status:
        status(f"Bedrock Dedicated Server {version} wird geladen …")
    cached_download(url, archive, "", progress, status, _BEDROCK_HOSTS)
    folder = pathlib.Path(target)
    require_free_disk(1024, folder)
    if status:
        status("Server wird entpackt …")
    extract_zip(archive, folder, BEDROCK_KEEP, BEDROCK_KEEP_DIRS, status=status)
    binary = folder / BEDROCK_BINARY
    if not binary.is_file():
        raise SourceError(f"{BEDROCK_BINARY} wurde im Archiv nicht gefunden.")
    try:
        binary.chmod(0o750)
    except OSError as exc:
        raise SourceError(f"{BEDROCK_BINARY} konnte nicht ausführbar gemacht werden: "
                          f"{exc}") from exc
    return {"version": str(version), "binary": str(binary)}


# ---------------------------------------------------------------- Paper installieren

EULA_TEXT = ("# Die Mojang-EULA wurde im Minecraft Server Manager bestätigt.\n"
             "# https://aka.ms/MinecraftEULA\neula=true\n")


def install_paper(version: str, target: str | os.PathLike, eula_accepted: bool,
                  progress: Callable[[int, int], None] | None = None,
                  status: Callable[[str], None] | None = None, jar: str = "server.jar") -> dict:
    """Paper-Jar in den Instanzordner legen und die nötige Java-Version melden.

    `eula_accepted` muss der Bestätigung des Benutzers im Programm entsprechen – ohne sie
    wird nichts eingerichtet.
    """
    if not eula_accepted:
        raise SourceError("Die Mojang-EULA muss im Programm bestätigt werden, "
                          "bevor ein Server eingerichtet werden kann.")
    if "/" in jar or "\\" in jar or not jar.endswith(".jar"):
        raise ValueError("Der Name der Server-Datei muss auf .jar enden.")
    folder = pathlib.Path(target)
    folder.mkdir(parents=True, exist_ok=True)
    require_free_disk(512, folder)
    info = paper_java_info(version)
    if status:
        status(f"Paper {version} wird geladen …")
    url, filename, sha = paper_download(version)
    server_jar = folder / jar
    build_file = folder / "build.txt"
    have = ""
    try:
        have = build_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if server_jar.is_file() and have == filename and (not sha or sha256_of(server_jar) == sha):
        if status:
            status(f"{filename} liegt schon bereit")
    else:
        download(url, server_jar, progress, status, sha, _PAPER_HOSTS)
        tmp = build_file.with_suffix(".tmp")
        tmp.write_text(filename + "\n", encoding="utf-8")
        os.replace(tmp, build_file)
    eula = folder / "eula.txt"
    tmp_eula = eula.with_suffix(".tmp")
    tmp_eula.write_text(EULA_TEXT, encoding="utf-8")
    os.replace(tmp_eula, eula)
    return {"version": str(version), "build": filename, "jar": jar,
            "java_major": int(info["major"]), "java_flags": list(info["flags"])}


# ---------------------------------------------------------------- Geyser, Floodgate, Via*

def geyser_download(project: str = "geyser", flavor: str = "spigot") -> tuple[str, str, str]:
    """(url, dateiname, sha256) des neuesten Builds von Geyser bzw. Floodgate.

    Erst wird der Build nachgeschlagen und dann genau dieser geladen – sonst könnte zwischen
    Prüfsumme und Download ein neuer Build erscheinen.
    """
    if project not in ("geyser", "floodgate"):
        raise SourceError(f"Unbekanntes Projekt: {project!r}")
    if not re.fullmatch(r"[a-z]{3,20}", str(flavor or "")):
        raise SourceError(f"Unbekannte Ausgabe: {flavor!r}")
    data = fetch_json(GEYSER_BUILD_API.format(project=project), _GEYSER_HOSTS)
    version = str(data.get("version") or "")
    build = data.get("build")
    entry = ((data.get("downloads") or {}).get(flavor) or {})
    name = _safe_name(entry.get("name") or f"{project}-{flavor}.jar")
    sha = str(entry.get("sha256") or "")
    if not re.fullmatch(r"[0-9A-Za-z.\-+_]{1,40}", version) or not isinstance(build, int):
        raise SourceError(f"Die Build-Angaben von {project} waren unbrauchbar.")
    url = GEYSER_DOWNLOAD.format(project=project, version=urllib.parse.quote(version),
                                 build=int(build), flavor=flavor)
    return _check_url(url, _GEYSER_HOSTS), name, (sha if _SHA256_RE.fullmatch(sha) else "")


def hangar_download(project: str) -> tuple[str, str, str]:
    """(url, version, sha256) der neuesten Release-Version eines Hangar-Plugins (ViaVersion …)."""
    if not _HANGAR_PROJECT_RE.fullmatch(str(project or "")):
        raise SourceError(f"Unbekanntes Hangar-Projekt: {project!r}")
    version = fetch_text(HANGAR_LATEST.format(project=project), _HANGAR_HOSTS)
    if not re.fullmatch(r"[0-9A-Za-z.\-+_]{1,40}", version):
        raise SourceError(f"Hangar nannte keine gültige Version für {project}: {version!r}")
    data = fetch_json(HANGAR_VERSION.format(project=project,
                                            version=urllib.parse.quote(version)), _HANGAR_HOSTS)
    entry = (data.get("downloads") or {}).get("PAPER") or {}
    url = str(entry.get("downloadUrl") or "")
    sha = str((entry.get("fileInfo") or {}).get("sha256Hash") or "")
    if not url:
        raise SourceError(f"Für {project} {version} gibt es keinen Paper-Download.")
    return _check_url(url, _HANGAR_HOSTS), version, (sha if _SHA256_RE.fullmatch(sha) else "")


def install_crossplay(target: str | os.PathLike,
                      progress: Callable[[int, int], None] | None = None,
                      status: Callable[[str], None] | None = None) -> dict:
    """Geyser + Floodgate (Bedrock-Crossplay) und ViaVersion + ViaBackwards in `plugins/` legen.

    Geyser spricht immer die neueste Java-Protokollversion; Via* übersetzen zwischen ihr und
    der tatsächlichen Server-Version.
    """
    plugins = pathlib.Path(target) / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    require_free_disk(256, plugins)
    out: dict[str, str] = {}
    for project, flavor, name in (("geyser", "spigot", "Geyser-Spigot.jar"),
                                  ("floodgate", "spigot", "floodgate-spigot.jar")):
        if status:
            status(f"{project} wird geladen …")
        url, _name, sha = geyser_download(project, flavor)
        download(url, plugins / name, progress, status, sha, _GEYSER_HOSTS)
        out[name] = sha
    for project in ("ViaVersion", "ViaBackwards"):
        if status:
            status(f"{project} wird geladen …")
        url, version, sha = hangar_download(project)
        download(url, plugins / f"{project}.jar", progress, status, sha, _HANGAR_HOSTS)
        out[f"{project}.jar"] = version
    return out


def remove_crossplay(target: str | os.PathLike) -> None:
    """Crossplay aus: die Plugin-Dateien entfernen, sonst liefe Geyser weiter auf dem UDP-Port."""
    plugins = pathlib.Path(target) / "plugins"
    for name in CROSSPLAY_JARS:
        for path in (plugins / name, plugins / f"{name}.sha256"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
