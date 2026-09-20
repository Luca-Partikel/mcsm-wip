"""Download-Quellen: Paper (Java), Bedrock Dedicated Server, Java-Runtime, Geyser."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

UA = "MinecraftServerManager/1.0 (lokales Setup-Tool)"
TIMEOUT = 45
DOWNLOAD_ATTEMPTS = 3

PAPER_API = "https://fill.papermc.io/v3/projects/paper"
BEDROCK_LINKS_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
BEDROCK_ZIP_TMPL = "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-{v}.zip"
ADOPTIUM_API = ("https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
                "?os=windows&architecture=x64&image_type=jre")
GEYSER_URL = "https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot"
FLOODGATE_URL = "https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot"
# Geyser spricht immer die neueste Java-Protokollversion; ViaVersion/ViaBackwards übersetzen
# zwischen dieser und der tatsächlichen Server-Version (Hangar-API von PaperMC).
HANGAR_LATEST = "https://hangar.papermc.io/api/v1/projects/{project}/latestrelease"
HANGAR_DOWNLOAD = "https://hangar.papermc.io/api/v1/projects/{project}/versions/{version}/PAPER/download"

EULA_URL = "https://aka.ms/MinecraftEULA"


class SourceError(RuntimeError):
    pass


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})


# ---------------------------------------------------------------- Zertifikats-Fallback
# Pythons OpenSSL prüft Zertifikate gegen den Windows-Speicher, lädt fehlende Stammzertifikate aber
# nicht nach (Windows tut das nur für eigene Programme) und kennt keine Antivirus-HTTPS-Filter.
# Schlägt die Prüfung fehl (CERTIFICATE_VERIFY_FAILED), übernimmt das in Windows enthaltene
# curl.exe (Schannel) – es nutzt denselben Speicher wie Edge, inklusive automatischem Nachladen.

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_ssl_broken = False            # nach dem ersten Zertifikatsfehler dauerhaft curl verwenden


def _windows_curl() -> str | None:
    if sys.platform != "win32":
        return None
    exe = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    return str(exe) if exe.exists() else shutil.which("curl")


def _is_cert_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


CERT_HINT = ("Die HTTPS-Zertifikatsprüfung ist auf diesem PC fehlgeschlagen. Meist helfen Windows-Updates "
             "(Stammzertifikate) oder das Abschalten der HTTPS-Prüfung im Antivirus-Programm.")


def _curl_args(url: str, extra: list[str]) -> list[str]:
    curl = _windows_curl()
    if not curl:
        raise SourceError(CERT_HINT)
    return [curl, "-sS", "-L", "--fail", "--max-time", str(TIMEOUT * 20), "-A", UA, *extra, url]


def _curl_bytes(url: str) -> bytes:
    try:
        res = subprocess.run(_curl_args(url, []), capture_output=True, timeout=TIMEOUT * 2,
                             creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceError(f"curl konnte {url} nicht laden: {exc}") from exc
    if res.returncode != 0:
        raise SourceError(f"curl konnte {url} nicht laden: {res.stderr.decode('utf-8', 'replace').strip()}")
    return res.stdout


def _get_bytes(url: str) -> bytes:
    """GET als Bytes – urllib, bei Zertifikatsfehler automatisch Windows-curl."""
    global _ssl_broken
    if not _ssl_broken:
        try:
            with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            if not (_is_cert_error(exc) and _windows_curl()):
                raise
            _ssl_broken = True
    return _curl_bytes(url)


def fetch_json(url: str):
    try:
        return json.loads(_get_bytes(url).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceError(f"Konnte {url} nicht laden: {exc}") from exc


def fetch_text(url: str) -> str:
    try:
        return _get_bytes(url).decode("utf-8").strip()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceError(f"Konnte {url} nicht laden: {exc}") from exc


def _urllib_to_file(url: str, tmp: pathlib.Path, progress) -> tuple[int, int, str]:
    """(gelesen, Content-Length, sha256) – wirft URLError/HTTPException/OSError."""
    with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        digest = hashlib.sha256()
        with tmp.open("wb") as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return done, total, digest.hexdigest()


def _curl_to_file(url: str, tmp: pathlib.Path, progress) -> tuple[int, int, str]:
    tmp.unlink(missing_ok=True)
    try:
        proc = subprocess.Popen(_curl_args(url, ["--retry", "2", "-o", str(tmp)]),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        raise SourceError(f"curl konnte nicht gestartet werden: {exc}") from exc
    while proc.poll() is None:
        time.sleep(0.4)
        if progress and tmp.exists():
            progress(tmp.stat().st_size, 0)
    if proc.returncode != 0:
        err = proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
        raise SourceError(f"curl-Download fehlgeschlagen: {err or proc.returncode}")
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise SourceError("curl lieferte keine Datei")
    digest = hashlib.sha256()
    with tmp.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            digest.update(chunk)
    size = tmp.stat().st_size
    if progress:
        progress(size, size)
    return size, size, digest.hexdigest()


def download(url: str, dest: pathlib.Path, progress=None, status=None,
             sha256: str = "") -> pathlib.Path:
    """Lädt eine Datei; progress(bytes_done, bytes_total) wird laufend aufgerufen.

    Bei Verbindungsabbrüchen wird bis zu DOWNLOAD_ATTEMPTS-mal neu begonnen (status(text)
    meldet den Versuch). Unvollständige Dateien (Content-Length) und falsche Prüfsummen
    werden nie übernommen. Bei Zertifikatsfehlern springt Windows-curl ein.
    """
    global _ssl_broken
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_error = ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            if _ssl_broken and _windows_curl():
                done, total, digest = _curl_to_file(url, tmp, progress)
            else:
                try:
                    done, total, digest = _urllib_to_file(url, tmp, progress)
                except urllib.error.URLError as exc:
                    if not (_is_cert_error(exc) and _windows_curl()):
                        raise
                    _ssl_broken = True
                    if status:
                        status("Zertifikatsprüfung fehlgeschlagen – Download läuft über Windows-curl …")
                    done, total, digest = _curl_to_file(url, tmp, progress)
            # Python meldet ein vorzeitiges Verbindungsende nicht als Fehler – selbst prüfen.
            if total and done != total:
                raise SourceError(f"Download unvollständig ({done}/{total} Bytes)")
            if sha256 and digest.lower() != sha256.lower():
                raise SourceError("Prüfsumme stimmt nicht – Datei ist beschädigt")
            tmp.replace(dest)
            return dest
        except (urllib.error.URLError, http.client.HTTPException, OSError, SourceError) as exc:
            tmp.unlink(missing_ok=True)
            last_error = str(exc)
            if _is_cert_error(exc) and not _windows_curl():
                last_error = CERT_HINT
                break
            if attempt < DOWNLOAD_ATTEMPTS:
                if status:
                    status(f"Verbindung unterbrochen – {attempt + 1}. Versuch …")
                time.sleep(2 * attempt)
    raise SourceError(f"Download fehlgeschlagen ({url}): {last_error}")


def hangar_download_url(project: str) -> tuple[str, str]:
    """(url, version) der neuesten Release-Version eines Hangar-Plugins (z. B. ViaVersion)."""
    version = fetch_text(HANGAR_LATEST.format(project=project))
    if not re.fullmatch(r"[0-9A-Za-z.\-+_]{1,40}", version):
        raise SourceError(f"Hangar lieferte keine gültige Version für {project}: {version!r}")
    return HANGAR_DOWNLOAD.format(project=project, version=version), version


# --------------------------------------------------------------------------- Paper

def _is_stable(version: str) -> bool:
    return not re.search(r"(pre|rc|snapshot)", version, re.I)


_stable_build_cache: dict[str, dict | None] = {}
_versions_cache: dict[str, object] = {}
VERSIONS_CACHE_SECONDS = 600


def paper_stable_build(version: str) -> dict | None:
    """Neuester Build im Kanal STABLE – ganz neue Versionen haben oft nur ALPHA/BETA-Builds,
    auf denen Geyser & Co. nicht laufen."""
    if version in _stable_build_cache:
        return _stable_build_cache[version]
    data = fetch_json(f"{PAPER_API}/versions/{version}/builds")
    builds = data if isinstance(data, list) else (data.get("builds") or [])
    stable = [b for b in builds if str(b.get("channel", "")).upper() == "STABLE"
              and (b.get("downloads") or {}).get("server:default", {}).get("url")]
    best = max(stable, key=lambda b: int(b.get("id") or 0)) if stable else None
    _stable_build_cache[version] = best
    return best


def paper_versions() -> list[str]:
    """Stabile Paper-Versionen, neueste zuerst. Die vordersten Kandidaten werden geprüft,
    ob sie überhaupt einen STABLE-Build haben (sonst würde die Liste mit einer Alpha beginnen)."""
    cached = _versions_cache.get("paper")
    if cached and time.time() - cached[0] < VERSIONS_CACHE_SECONDS:      # type: ignore[index]
        return list(cached[1])                                            # type: ignore[index]
    data = fetch_json(PAPER_API)
    versions: list[str] = []
    for group in data.get("versions", {}).values():
        versions.extend(v for v in group if _is_stable(v))
    checked: list[str] = []
    incomplete = False
    for i, v in enumerate(versions):
        if i < 6:
            try:
                if paper_stable_build(v) is None:
                    continue
            except SourceError:
                incomplete = True     # vorübergehender Fehler: Version lieber behalten als verschweigen
        checked.append(v)
    if not incomplete:                # unvollständig geprüfte Listen nicht 10 Minuten festhalten
        _versions_cache["paper"] = (time.time(), checked)
    return checked


def paper_download(version: str) -> tuple[str, str, str]:
    """Gibt (url, dateiname, sha256) des neuesten STABLE-Builds zurück. Nur wenn es wirklich keinen
    STABLE-Build gibt, wird der neueste Build genommen – ein Netzwerkfehler wird nach einem zweiten
    Versuch durchgereicht statt still auf einen ALPHA-Build auszuweichen."""
    try:
        data = paper_stable_build(version)
    except SourceError:
        data = paper_stable_build(version)
    if data is None:
        data = fetch_json(f"{PAPER_API}/versions/{version}/builds/latest")
    entry = (data.get("downloads") or {}).get("server:default")
    if not entry or not entry.get("url"):
        raise SourceError(f"Für Paper {version} gibt es keinen Download.")
    sha = str((entry.get("checksums") or {}).get("sha256") or "")
    return entry["url"], entry.get("name", f"paper-{version}.jar"), sha


def version_tuple(version: str) -> tuple[int, ...]:
    """'1.21.11' -> (1, 21, 11); '26.3' -> (26, 3); unbekannt -> ()."""
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", version or "")
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


def command_block_gamerule(version: str) -> str | None:
    """Seit 1.21.9 sind pvp/enable-command-block keine server.properties mehr, sondern Gamerules.
    None = Version schreibt die Werte noch in server.properties."""
    v = version_tuple(version)
    if not v:
        return None
    if v[0] != 1 or v >= (1, 21, 11):
        return "command_blocks_work"          # snake_case-Registry ab 1.21.11 / 26.x
    if v >= (1, 21, 9):
        return "commandBlocksEnabled"         # 1.21.9 / 1.21.10
    return None


AVAILABLE_JAVA = (8, 11, 17, 21, 25)      # LTS-Versionen, die Adoptium als JRE anbietet
_java_info_cache: dict[str, dict] = {}


def _pick_java(minimum: int) -> int:
    for major in AVAILABLE_JAVA:
        if major >= minimum:
            return major
    return AVAILABLE_JAVA[-1]


def _guess_java_major(version: str) -> int:
    """Schätzung, falls die PaperMC-API nicht erreichbar ist."""
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return 25
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if major != 1:          # neues Schema (26.x) -> Java 25
        return 25
    if minor >= 21 or (minor == 20 and patch >= 5):
        return 21
    if minor >= 17:
        return 17
    return 8


def paper_java_info(version: str) -> dict:
    """{'major': 25, 'flags': [...]} – benötigte Java-Version und empfohlene JVM-Flags laut PaperMC."""
    if version in _java_info_cache:
        return _java_info_cache[version]
    info = {"major": _guess_java_major(version), "flags": []}
    try:
        data = fetch_json(f"{PAPER_API}/versions/{version}")
        java = ((data.get("version") or data).get("java") or {})
        minimum = int((java.get("version") or {}).get("minimum"))
        info["major"] = _pick_java(minimum)
        flags = (java.get("flags") or {}).get("recommended") or []
        info["flags"] = [f for f in flags if isinstance(f, str) and f.startswith(("-XX:", "-D"))]
        _java_info_cache[version] = info
    except (SourceError, TypeError, ValueError, AttributeError):
        pass
    return info


def java_major_for(version: str) -> int:
    """Passende Java-Version für eine Minecraft-Version."""
    return paper_java_info(version)["major"]


# --------------------------------------------------------------------------- Bedrock

def bedrock_links() -> dict:
    data = fetch_json(BEDROCK_LINKS_API)
    out = {}
    for link in (data.get("result") or {}).get("links", []):
        out[link.get("downloadType")] = link.get("downloadUrl")
    return out


def _version_from_url(url: str) -> str:
    m = re.search(r"bedrock-server-([0-9.]+)\.zip", url or "")
    return m.group(1) if m else ""


def bedrock_versions() -> dict:
    """{'latest': '1.26.51.1', 'preview': '1.26.60.27'} – direkt von Mojang."""
    links = bedrock_links()
    return {
        "latest": _version_from_url(links.get("serverBedrockWindows", "")),
        "preview": _version_from_url(links.get("serverBedrockPreviewWindows", "")),
    }


def bedrock_download_url(version: str) -> str:
    if not re.fullmatch(r"[0-9]+(\.[0-9]+){2,3}", version or ""):
        raise SourceError(f"'{version}' ist keine gültige Bedrock-Version (z. B. 1.26.51.1).")
    return BEDROCK_ZIP_TMPL.format(v=version)


# --------------------------------------------------------------------------- Java-Runtime

def adoptium_jre(major: int) -> tuple[str, str]:
    """(url, release_name) des Temurin-JRE-ZIPs für Windows x64."""
    data = fetch_json(ADOPTIUM_API.format(major=major))
    for asset in data:
        package = (asset.get("binary") or {}).get("package") or {}
        link = package.get("link", "")
        if link.endswith(".zip"):
            return link, asset.get("release_name", f"jdk-{major}")
    raise SourceError(f"Kein Temurin-JRE {major} für Windows x64 gefunden.")


# --------------------------------------------------------------------------- Xbox-Freunde-Modus

# MCXboxBroadcast meldet sich mit einem Xbox-Konto an und zeigt den Server dessen Freunden
# als beitretbare „Welt“ – so wie ein Spieler, der eine Welt hostet.
XBOX_BROADCAST_API = "https://api.github.com/repos/MCXboxBroadcast/Broadcaster/releases/latest"
XBOX_BROADCAST_ASSET = "MCXboxBroadcastStandalone.jar"


def xbox_broadcast_download() -> tuple[str, str]:
    """(url, release_tag) der aktuellen Standalone-Version von MCXboxBroadcast."""
    data = fetch_json(XBOX_BROADCAST_API)
    tag = re.sub(r"[^0-9A-Za-z.\-_]", "", str(data.get("tag_name") or "latest")) or "latest"
    for asset in data.get("assets") or []:
        if asset.get("name") == XBOX_BROADCAST_ASSET and asset.get("browser_download_url"):
            return asset["browser_download_url"], tag
    raise SourceError(f"{XBOX_BROADCAST_ASSET} wurde im aktuellen Release nicht gefunden.")
