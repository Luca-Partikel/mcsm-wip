# -*- coding: utf-8 -*-
"""Xbox-Freunde-Modus (MCXboxBroadcast) für gehostete Server – die Linux-Fassung.

Vorlage ist der Abschnitt „Xbox“ in ``core/manager.py`` des Programms auf dem PC. Ein Bot-Konto
meldet sich bei Xbox Live an und zeigt den Server allen Freunden dieses Kontos als beitretbare
Welt – Konsolen (Xbox, PlayStation, Switch) treten dann über „Freunde → Beitreten“ bei, ohne
Adresse und ohne Port. Für viele Spieler ist das der **einzige** Weg auf einen fremden Server.

Unterschiede zur Fassung auf dem PC, und warum:

* **Beworben wird die öffentliche Adresse des Root-Servers**, nicht ``127.0.0.1``. Der Bot läuft
  hier auf derselben Maschine wie der Server – trotzdem muss in der Sitzung die Adresse stehen,
  die ein Spieler von außen erreicht, denn die Konsole verbindet sich selbst dorthin. Dazu der
  Bedrock-Port **dieser Instanz** (bei Java der Geyser-Port, bei BDS der eingetippte Port).
* Der Bot läuft unter **demselben Unix-Benutzer wie der Server** (``core/isolation.py``), also
  nicht als ``mcsm``. Er liest und schreibt nur im Ordner ``xbox`` der Instanz.
* Er läuft in einer **eigenen Sitzung** (``setsid``); gestoppt wird die ganze Prozessgruppe über
  denselben Weg, über den er gestartet wurde.
* Die gespeicherte Anmeldung liegt in ``<instanz>/xbox/cache/cache.json`` und bleibt dort. Sie
  wird bei einer Übertragung **mitgenommen** – sonst müsste sich der Betreiber nach jedem Umzug
  neu anmelden (siehe ``core/paths.py``, ``nachladbar`` prüft nur den **ersten** Pfadteil, und
  der ist hier ``xbox``; ein Selbsttest hält das fest).

Der Bot ist **kein** Minecraft-Server: er hält keine Welt, verliert beim Stoppen nichts und
lässt sich jederzeit neu starten. Deshalb wird er beim Beenden des Dienstes angehalten (anders
als die Server) und beim nächsten Start wieder hochgefahren – ein vergessener Bot würde sonst
dieselbe Sitzung ein zweites Mal ankündigen.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import signal
import subprocess
import threading
import time
from typing import Callable

from . import isolation, runner, sources_linux

# ---------------------------------------------------------------- Quelle der Jar

#: MCXboxBroadcast Standalone – dieselbe Quelle wie im Programm auf dem PC (``core/sources.py``).
BROADCAST_API = "https://api.github.com/repos/MCXboxBroadcast/Broadcaster/releases/latest"
BROADCAST_ASSET = "MCXboxBroadcastStandalone.jar"
#: GitHub liefert die Dateien über wechselnde Namen aus – nur diese sind erlaubt.
BROADCAST_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                   "release-assets.githubusercontent.com")
#: So heißt die Jar im Zwischenspeicher (``/srv/mcsm/cache``).
JAR_PREFIX = "MCXboxBroadcastStandalone-"

#: Java-Laufzeiten, die MCXboxBroadcast nimmt (es braucht keine bestimmte, nur mindestens 17).
JAVA_CANDIDATES = (25, 21, 17)

#: Unterordner der Instanz: Konfiguration, Protokolle und die gespeicherte Anmeldung des Bots.
XBOX_DIR = "xbox"
#: Darin liegt die Anmeldung. Der Bot legt ``player_history.db`` schon vor dem Login an – nur
#: ``cache/cache.json`` beweist, dass sich jemand angemeldet hat.
TOKEN_REL = "cache/cache.json"

#: Prozesskennzeichen in der Kommandozeile – daran findet der Dienst einen vergessenen Bot
#: in ``/proc`` wieder (wie ``runner.INSTANZ_FLAG`` beim Server). Nichts Geheimes.
XBOX_FLAG = "-Dmcsm.xbox="
#: Merkdatei mit der Prozesskennung, im Verwaltungsordner der Instanz (gehört dem Dienst).
PID_FILE = "xbox.pid"

RAM_START_MB = 96
RAM_MAX_MB = 512

#: Diese Adressen wären in einer Xbox-Sitzung sinnlos: Die Konsole des Freundes baut die
#: Verbindung **selbst** zu der beworbenen Adresse auf – mit ``127.0.0.1`` verbände sie sich mit
#: sich selbst. Beworben wird deshalb immer die öffentliche Adresse des Root-Servers.
LOOPBACK = ("127.0.0.1", "0.0.0.0", "::1", "localhost", "localhost.localdomain")

#: So lange wartet ein Stopp auf das ``exit`` des Bots, bevor Signale folgen.
STOP_TIMEOUT = 15
#: Ohne Anmeldung reagiert der Bot nicht auf ``exit`` – dann lohnt langes Warten nicht.
STOP_TIMEOUT_UNANGEMELDET = 3
TERM_WAIT = 5
KILL_WAIT = 5

MAX_LOG_LINES = 1500
MAX_LINE_CHARS = 2000

# ---------------------------------------------------------------- Ausgabe des Bots lesen

#: Der Geräte-Code: „… open https://microsoft.com/link and enter the code ABCD-1234 …“
CODE_RE = re.compile(r"(https://(?:www\.)?microsoft\.com/link)\S*.*?\bcode\s+([A-Z0-9]{6,12})", re.I)
#: Meldungen des Standalone, sobald die Sitzung bei Xbox Live steht.
ONLINE_RE = re.compile(r"Creation of Xbox LIVE session was successful|NetherNet Broadcaster started on ID"
                       r"|Session recreated after device token refresh|Updated session!", re.I)
USER_RE = re.compile(r"(?:authenticated|logged in|signed in)\s+as\s+([^\s(]+)", re.I)
#: Harmlos: der RakNet-Ping klappt gegen einen gestoppten Server (und nie gegen NetherNet-BDS).
IGNORE_RE = re.compile(r"Failed to ping server", re.I)
FEHLER_RE = re.compile(r"\bFailed to\b|Exception:")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: Deutscher Klartext zu den Zuständen (das Programm auf dem PC darf ihn unverändert anzeigen).
STATE_TEXTS = {
    "off": "Der Xbox-Freunde-Modus läuft nicht.",
    "starting": "Der Bot startet – einen Augenblick.",
    "login": "Der Bot wartet auf die Anmeldung: Seite öffnen und den Code eingeben.",
    "online": "Der Server steht in der Xbox-Freundesliste des Bot-Kontos.",
    "error": "Der Bot meldet einen Fehler.",
}


def state_text(state: str, *, code: str = "", url: str = "", gamertag: str = "",
               error: str = "") -> str:
    """Ein deutscher Satz zum Zustand – mit Code bzw. Gamertag, wenn beides bekannt ist."""
    schluessel = str(state or "off")
    if schluessel == "login" and code:
        ziel = url or "https://microsoft.com/link"
        return (f"Bitte {ziel} öffnen und den Code {code} eingeben. "
                f"Danach erscheint der Server in der Freundesliste des Bot-Kontos.")
    if schluessel == "online" and gamertag:
        return f"Der Server steht in der Xbox-Freundesliste von {gamertag}."
    if schluessel == "error" and error:
        return f"Der Bot meldet einen Fehler: {error}"
    return STATE_TEXTS.get(schluessel, STATE_TEXTS["off"])


# ---------------------------------------------------------------- Ordner und Dateien

def xbox_dir(directory) -> pathlib.Path:
    """``<instanzordner>/xbox`` – Arbeitsordner des Bots."""
    return pathlib.Path(str(directory)) / XBOX_DIR


def config_path(directory) -> pathlib.Path:
    return xbox_dir(directory) / "config.yml"


def token_path(directory) -> pathlib.Path:
    """Die gespeicherte Anmeldung. Liegt im Datenordner der Instanz und bleibt dort."""
    return xbox_dir(directory) / "cache" / "cache.json"


def token_cached(directory) -> bool:
    try:
        return token_path(directory).is_file()
    except OSError:
        return False


def pid_path(directory) -> pathlib.Path:
    """Merkdatei im Verwaltungsordner – nicht im Ordner des Bots, den der Besitzer sieht."""
    return pathlib.Path(str(directory)) / isolation.MANAGE_DIR / PID_FILE


# ---------------------------------------------------------------- Jar und Java

def cached_jar() -> pathlib.Path | None:
    """Die neueste im Zwischenspeicher liegende Jar (oder ``None``)."""
    try:
        jars = sorted(sources_linux.cache_dir().glob(JAR_PREFIX + "*.jar"),
                      key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return jars[-1] if jars else None


#: So lange gilt die einmal gefundene Java-Laufzeit als bekannt.
JAVA_MEMO_SECONDS = 300
_java_memo: dict = {"at": 0.0, "path": None}
_java_memo_lock = threading.Lock()


def java_path(*, force: bool = False) -> pathlib.Path | None:
    """Irgendeine vorhandene Java-Laufzeit ab 17 – MCXboxBroadcast braucht keine bestimmte.

    Das Ergebnis wird kurz gemerkt: ``sources_linux.java_path`` ruft zur Prüfung wirklich
    ``java -version`` auf, und der Zustand des Bots hängt an **jeder** Serveransicht. Ohne
    dieses Gedächtnis liefen bei einem Blick auf die Serverliste mehrere Unterprozesse je Server.
    """
    with _java_memo_lock:
        jetzt = time.time()
        gemerkt = _java_memo["path"]
        if not force and jetzt - float(_java_memo["at"] or 0) < JAVA_MEMO_SECONDS:
            if gemerkt is None or gemerkt.is_file():
                return gemerkt
        found = None
        for major in JAVA_CANDIDATES:
            found = sources_linux.java_path(major)
            if found is not None:
                break
        _java_memo.update(at=jetzt, path=found)
        return found


def broadcast_download() -> tuple[str, str]:
    """``(url, release_tag)`` der aktuellen Standalone-Fassung von MCXboxBroadcast."""
    data = sources_linux.fetch_json(BROADCAST_API, BROADCAST_HOSTS)
    if not isinstance(data, dict):
        raise sources_linux.SourceError("Die Antwort von GitHub war keine Freigabe-Auskunft.")
    tag = re.sub(r"[^0-9A-Za-z.\-_]", "", str(data.get("tag_name") or "latest")) or "latest"
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if asset.get("name") == BROADCAST_ASSET and asset.get("browser_download_url"):
            return str(asset["browser_download_url"]), tag
    raise sources_linux.SourceError(
        f"{BROADCAST_ASSET} wurde in der aktuellen Freigabe nicht gefunden.")


def freigeben(jar) -> None:
    """Die Jar für den Server-Benutzer lesbar machen – **er** startet den Bot, nicht der Dienst.

    Der Dienst schreibt mit ``UMask=0007``; die Jar wäre also ``0660 mcsm:mcsm``, und der
    Serverbenutzer (``mcsmsrv01`` …) käme nicht an sie heran: „Unable to access jarfile“. Auf dem
    Root-Server nachgemessen, genau daran scheiterte der erste Start.

    Freigegeben wird so wenig wie möglich:

    * Der Zwischenspeicher bekommt nur **durchlaufen** für andere (0711) – dasselbe Muster wie
      ``/srv/mcsm`` und ``/srv/mcsm/users`` (siehe ARCHITEKTUR.md). Auflisten kann ihn niemand,
      und alles andere darin bleibt ``0660``.
    * Weltlesbar wird allein diese eine Datei. Sie ist ein öffentliches Freigabe-Artefakt von
      GitHub, an ihr ist nichts geheim – und sie wird nur gelesen, nie geschrieben.

    So genügt **eine** Jar für alle Instanzen; eine Kopie je Instanz wären 40 MB, und die Platte
    ist auf dieser Maschine der engere Engpass.
    """
    pfad = pathlib.Path(str(jar))
    try:
        ordner = pfad.parent
        os.chmod(ordner, (ordner.stat().st_mode & 0o7777) | 0o011)
        os.chmod(pfad, 0o644)
    except OSError:
        pass                    # ein Fehlschlag zeigt sich beim Start als klare Meldung des Bots


def ensure_jar(progress: Callable[[int, int], None] | None = None,
               status: Callable[[str], None] | None = None) -> pathlib.Path:
    """Jar nach ``/srv/mcsm/cache`` holen (mit Prüfsumme) und den Pfad zurückgeben.

    ``cached_download`` prüft eine schon vorhandene Datei gegen die Prüfsumme neben ihr und lädt
    nur neu, wenn sie fehlt oder beschädigt ist.
    """
    url, tag = broadcast_download()
    ziel = sources_linux.cache_dir() / f"{JAR_PREFIX}{tag}.jar"
    ziel.parent.mkdir(parents=True, exist_ok=True)
    jar = sources_linux.cached_download(url, ziel, progress=progress, status=status,
                                        hosts=BROADCAST_HOSTS)
    freigeben(jar)
    return jar


def ensure_java(progress: Callable[[int, int], None] | None = None,
                status: Callable[[str], None] | None = None) -> pathlib.Path:
    """Java besorgen, falls noch keine Laufzeit da ist."""
    found = java_path()
    if found is not None:
        return found
    sources_linux.ensure_java(21, progress=progress, status=status)
    found = java_path(force=True)
    if found is None:
        raise sources_linux.SourceError(
            "Für den Xbox-Freunde-Modus wird Java gebraucht, es ließ sich aber keine Laufzeit "
            "einrichten.")
    return found


# ---------------------------------------------------------------- Angaben eines Bots

def _yaml_str(value) -> str:
    """Zeichenkette für Configurate (YAML). Rückstriche fallen weg, Anführungszeichen werden
    zu einfachen – so kann kein Wert die Datei aufbrechen."""
    text = re.sub(r"[\x00-\x1f]", " ", str(value))
    return '"' + text.replace("\\", "").replace('"', "'") + '"'


def _yaml_bool(value) -> str:
    return "true" if value else "false"


@dataclasses.dataclass(frozen=True)
class XboxSpec:
    """Alles, was ein Bot zum Laufen braucht. Der Daemon baut sie aus dem Instanz-Datensatz.

    ``address`` ist die **öffentliche** Adresse des Root-Servers und ``port`` der Bedrock-Port
    dieser Instanz – zusammen das, was eine Konsole aus der Freundesliste anwählt.
    """

    instance_id: str
    directory: pathlib.Path
    name: str = "Minecraft Server"
    motd: str = ""
    max_players: int = 10
    address: str = ""
    #: Der Bedrock-Port dieser Instanz. ``0`` heißt „noch nicht bekannt“ (der Port wird beim
    #: ersten Start vergeben) – ein Bot lässt sich damit nicht starten, aber der Zustand anzeigen.
    port: int = 0
    #: Java + Geyser antwortet auf den RakNet-Ping, ein NetherNet-BDS nie – dort also aus.
    query_server: bool = True
    run_as: str = ""

    def __post_init__(self) -> None:
        if not runner._ID_RE.fullmatch(str(self.instance_id or "")):
            raise ValueError("Ungültige Instanzkennung.")
        object.__setattr__(self, "directory",
                           runner.check_instance_dir(self.directory))
        name = re.sub(r"[\x00-\x1f]", "", str(self.name or "")).strip()[:32] or "Minecraft Server"
        object.__setattr__(self, "name", name)
        motd = re.sub(r"[\x00-\x1f]", "", str(self.motd or "")).strip()[:64] or name
        object.__setattr__(self, "motd", motd)
        try:
            plaetze = int(self.max_players)
        except (TypeError, ValueError) as exc:
            raise ValueError("Die Spielerzahl muss eine Zahl sein.") from exc
        object.__setattr__(self, "max_players", max(1, min(200, plaetze)))
        adresse = str(self.address or "").strip()
        if not adresse or not re.fullmatch(r"[A-Za-z0-9.\-:]{1,120}", adresse):
            raise ValueError("Die beworbene Adresse muss ein Hostname oder eine IP-Adresse sein.")
        if adresse.lower() in LOOPBACK:
            raise ValueError(
                "Die beworbene Adresse muss die öffentliche Adresse des Servers sein. Mit "
                f"„{adresse}“ verbände sich die Konsole des Freundes mit sich selbst.")
        object.__setattr__(self, "address", adresse)
        try:
            port = int(self.port)
        except (TypeError, ValueError) as exc:
            raise ValueError("Der Bedrock-Port muss eine Zahl sein.") from exc
        if port and not 1 <= port <= 65535:
            raise ValueError("Der Bedrock-Port liegt außerhalb des gültigen Bereichs.")
        port = max(0, port)
        object.__setattr__(self, "port", port)
        run_as = str(self.run_as or "").strip()
        if run_as and not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", run_as):
            raise ValueError("Ungültiger Benutzername für den Bot-Prozess.")
        object.__setattr__(self, "run_as", run_as)

    @property
    def signature(self) -> tuple:
        """Alles, was in ``config.yml`` landet. Ändert sich das, muss der Bot neu starten –
        MCXboxBroadcast liest die Datei nur beim Start und kennt keinen Reload-Befehl."""
        return (self.name, self.motd, self.max_players, self.address, self.port,
                self.query_server)


def write_config(spec: XboxSpec) -> pathlib.Path:
    """``config.yml`` für MCXboxBroadcast Standalone (Schlüssel laut CoreConfig, kebab-case)."""
    folder = xbox_dir(spec.directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "config.yml"
    text = (
        "# Von Minecraft Server Manager erzeugt. Änderungen hier werden beim nächsten Start\n"
        "# überschrieben – Adresse und Anzeigename gehören in die Oberfläche des Programms.\n"
        "session:\n"
        "  update-interval: 30\n"
        f"  query-server: {_yaml_bool(spec.query_server)}\n"
        "  web-query-fallback: false\n"
        "  config-fallback: true\n"
        "  session-info:\n"
        f"    host-name: {_yaml_str(spec.name)}\n"
        f"    world-name: {_yaml_str(spec.motd)}\n"
        "    players: 0\n"
        f"    max-players: {spec.max_players}\n"
        f"    ip: {_yaml_str(spec.address)}\n"
        f"    port: {spec.port}\n"
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
        "suppress-session-update-message: true\n"
    )
    tmp = path.with_name(path.name + ".neu")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def build_command(spec: XboxSpec, jar, java) -> list[str]:
    """Startbefehl als Argumentliste – keine Shell, keine Zeichenketten-Bastelei."""
    return [str(java), f"-Xms{RAM_START_MB}M", f"-Xmx{RAM_MAX_MB}M",
            *runner.JAVA_ENCODING_FLAGS, f"{XBOX_FLAG}{spec.instance_id}", "-jar", str(jar)]


def build_env(spec: XboxSpec) -> dict[str, str]:
    """Kleine, feste Umgebung – der Bot erbt nichts vom Dienst (keine Token, keine Pfade)."""
    manage = pathlib.Path(str(spec.directory)) / isolation.MANAGE_DIR
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(manage / "home"),
        "TMPDIR": str(manage / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": os.environ.get("TZ") or "Europe/Berlin",
    }


# ---------------------------------------------------------------- Vergessene Bots

def _erwartete_uid(spec: XboxSpec) -> int | None:
    if spec.run_as:
        return isolation.uid_of(spec.run_as)
    try:
        return int(os.getuid())                                 # pragma: no cover - nur Unix
    except AttributeError:                                      # pragma: no cover - Windows
        return None


def orphan_pid(spec: XboxSpec) -> int | None:
    """Läuft aus einem früheren Lauf des Dienstes noch ein Bot für diese Instanz?

    Gesucht wird in ``/proc`` nach Besitzer **und** Kennzeichen (``-Dmcsm.xbox=<kennung>``) –
    genau wie beim Server (``runner.server_pid_in_proc``). Ohne das kündigte nach einem Update
    ein vergessener Bot dieselbe Sitzung ein zweites Mal an.
    """
    erwartet = _erwartete_uid(spec)
    if erwartet is None:
        return None
    merkmal = f"{XBOX_FLAG}{spec.instance_id}"
    try:
        namen = os.listdir("/proc")
    except OSError:                                             # pragma: no cover - kein /proc
        return None
    for name in namen:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or runner.prozess_besitzer(pid) != erwartet:
            continue
        if merkmal in runner.prozess_cmdline(pid):
            return pid
    return None


def stop_orphan(spec: XboxSpec, timeout: float = TERM_WAIT) -> bool:
    """Einen vergessenen Bot beenden (SIGTERM, dann SIGKILL). ``True``, wenn danach Ruhe ist."""
    pid = orphan_pid(spec)
    if pid is None:
        return True
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        if not runner.signal_pid_gruppe(pid, sig, run_as=spec.run_as):
            return True                     # Prozess ist schon weg
        deadline = time.time() + (timeout if sig == signal.SIGTERM else KILL_WAIT)
        while time.time() < deadline:
            if not runner.prozess_laeuft(pid):
                try:
                    pid_path(spec.directory).unlink(missing_ok=True)
                except OSError:
                    pass
                return True
            time.sleep(0.2)
    return not runner.prozess_laeuft(pid)


# ---------------------------------------------------------------- Der Bot selbst

class Broadcaster:
    """Ein MCXboxBroadcast-Prozess. Der Zustand (Code, Gamertag, online) wird beim Lesen der
    Ausgabe mitgeführt und bleibt erhalten, egal wie viel danach noch protokolliert wird."""

    def __init__(self, spec: XboxSpec) -> None:
        self.spec = spec
        self.active_spec = spec
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.offset = 0
        self.started_at = 0.0
        self.stopping = False
        self.exit_code: int | None = None
        self.state = self._frisch()
        self.started_sig: tuple = ()
        self._buf_lock = threading.Lock()
        self._proc_lock = threading.RLock()

    # -- Zustand --------------------------------------------------------
    @staticmethod
    def _frisch() -> dict:
        return {"state": "starting", "code": "", "url": "", "gamertag": "", "error": "",
                "authed": False}

    @property
    def running(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    @property
    def restart_needed(self) -> bool:
        """Der Bot läuft, aber mit anderen Angaben als jetzt gelten."""
        return self.running and self.started_sig != self.spec.signature

    @property
    def uptime(self) -> int:
        return int(time.time() - self.started_at) if self.running and self.started_at else 0

    # -- Konsolenpuffer -------------------------------------------------
    def log(self, text: str) -> None:
        line = _ANSI_RE.sub("", str(text)).rstrip("\r\n")[:MAX_LINE_CHARS]
        with self._buf_lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                weg = len(self.lines) - MAX_LOG_LINES
                del self.lines[:weg]
                self.offset += weg

    def console(self, since: int = 0) -> dict:
        """``{"next": …, "lines": [...]}`` – gleiche Form wie beim Server."""
        with self._buf_lock:
            start = max(0, int(since) - self.offset)
            return {"next": self.offset + len(self.lines), "lines": list(self.lines[start:])}

    # -- Starten --------------------------------------------------------
    def _spawn(self, cmd: list[str], cwd: str, env: dict[str, str]) -> subprocess.Popen:
        """Prozess anlegen. Eigene Methode, damit Selbsttests sie ersetzen können."""
        if self.active_spec.run_as:
            cmd = isolation.wrap(cmd, self.active_spec.run_as, env)
        return subprocess.Popen(                                    # noqa: S603 - Argumentliste
            cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, close_fds=True,
            start_new_session=True)                                 # setsid: eigene Prozessgruppe

    def start(self) -> None:
        """Bot starten. Läuft er schon mit denselben Angaben, passiert nichts.

        Haben sich Name, Adresse oder Port geändert, wird er neu gestartet: MCXboxBroadcast liest
        ``config.yml`` nur beim Start.
        """
        with self._proc_lock:
            if self.running:
                if not self.restart_needed:
                    return
                self.stop()
            spec = self.spec
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                raise ValueError("Der Xbox-Bot darf nicht als root laufen – der Dienst muss als "
                                 "Benutzer mcsm gestartet werden.")
            jar = cached_jar()
            java = java_path()
            if jar is None or java is None:
                raise ValueError("Der Xbox-Freunde-Modus ist für diesen Server noch nicht "
                                 "eingerichtet. Bitte zuerst einrichten lassen.")
            if not spec.port:
                raise ValueError("Der Bedrock-Port dieses Servers steht noch nicht fest – "
                                 "der Bot kann erst danach eine Adresse ankündigen. Bitte den "
                                 "Server einmal starten.")
            if spec.run_as:
                # Der Bot läuft unter dem Benutzer des Servers und muss die Jar lesen können.
                freigeben(jar)
            folder = xbox_dir(spec.directory)
            folder.mkdir(parents=True, exist_ok=True)
            if spec.run_as:
                # Der Bot schreibt seine Anmeldung in diesen Ordner – er muss ihm gehören.
                isolation.apply_dir(folder, spec.run_as)
            write_config(spec)
            vergessen = orphan_pid(spec)
            if vergessen is not None and not stop_orphan(spec):
                raise ValueError("Für diesen Server läuft noch ein Xbox-Bot aus einem früheren "
                                 "Lauf, der sich nicht beenden ließ.")
            self.active_spec = spec
            with self._buf_lock:
                self.lines.clear()
                self.offset = 0
            self.stopping = False
            self.exit_code = None
            self.state = self._frisch()
            self.started_sig = spec.signature
            self.log("[Manager] Starte Xbox-Freunde-Modus (MCXboxBroadcast) …")
            cmd = build_command(spec, jar, java)
            env = build_env(spec)
            try:
                self.proc = self._spawn(cmd, str(folder), env)
            except OSError as exc:
                self.proc = None
                raise ValueError(f"Der Xbox-Bot konnte nicht gestartet werden: {exc}") from exc
            self.started_at = time.time()
            self._write_pid(self.proc.pid)
            threading.Thread(target=self._read_output, args=(self.proc,), daemon=True,
                             name=f"mcsm-xbox-{spec.instance_id}").start()

    def _write_pid(self, pid: int) -> None:
        path = pid_path(self.active_spec.directory)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(f"{int(pid)}\n{int(time.time())}\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _drop_pid(self) -> None:
        try:
            pid_path(self.active_spec.directory).unlink(missing_ok=True)
        except OSError:
            pass

    # -- Ausgabe --------------------------------------------------------
    def _track(self, line: str) -> None:
        """Zustand aus einer Ausgabezeile fortschreiben (starting → login → online)."""
        st = self.state
        found = CODE_RE.search(line)
        if found:
            st.update(state="login", url=found.group(1), code=found.group(2).upper(), error="")
            return
        found = USER_RE.search(line)
        if found:
            st.update(gamertag=found.group(1), code="", url="", authed=True, error="")
            if st["state"] == "login":
                st["state"] = "starting"
        if ONLINE_RE.search(line):
            st.update(state="online", code="", url="", authed=True, error="")
            return
        # Stacktrace-Zeilen und der harmlose Ping-Fehler sind keine Meldung für die Oberfläche.
        if IGNORE_RE.search(line) or line.startswith(("\tat ", "\t... ", "java.", "Caused by")):
            return
        if FEHLER_RE.search(line):
            st["error"] = line.strip()[-220:]

    def _read_output(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is not None:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                self.log(line)
                self._track(line)
        code = proc.wait()
        self.exit_code = code
        self.state.update(state="off", code="", url="")
        self.stopping = False
        self._drop_pid()
        self.log(f"[Manager] Xbox-Freunde-Modus beendet (Code {code}).")

    # -- Stoppen --------------------------------------------------------
    def _warte(self, seconds: float) -> bool:
        deadline = time.time() + max(0.0, float(seconds))
        while time.time() < deadline:
            if not self.running:
                return True
            time.sleep(0.2)
        return not self.running

    def stop(self, timeout: int = STOP_TIMEOUT) -> None:
        """Sauber stoppen: ``exit`` → SIGTERM an die Prozessgruppe → SIGKILL."""
        proc = self.proc
        if not self.running or proc is None:
            return
        with self._proc_lock:
            if not self.running:
                return
            erster = not self.stopping
            self.stopping = True
        if not erster:
            self._warte(timeout)
            return
        self.log("[Manager] Stoppe Xbox-Freunde-Modus …")
        if not self.state.get("authed"):
            timeout = min(int(timeout), STOP_TIMEOUT_UNANGEMELDET)
        try:
            if proc.stdin:
                proc.stdin.write(b"exit\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        if self._warte(timeout):
            return
        runner.signal_gruppe(proc, signal.SIGTERM, run_as=self.active_spec.run_as)
        if self._warte(TERM_WAIT):
            return
        self.log("[Manager] Der Bot hängt – die Prozessgruppe wird beendet.")
        runner.signal_gruppe(proc, getattr(signal, "SIGKILL", signal.SIGTERM), hard=True,
                             run_as=self.active_spec.run_as)
        self._warte(KILL_WAIT)

    # -- Auskunft -------------------------------------------------------
    def status(self) -> dict:
        """Zustand des Bots. Die Felder heißen wie im Programm auf dem PC."""
        st = dict(self.state)
        zustand = st["state"] if self.running else ("error" if st.get("error") else "off")
        if not self.running:
            st.update(code="", url="")
        with self._buf_lock:
            weiter = self.offset + len(self.lines)
        return {
            "running": self.running,
            "state": zustand,
            "code": st.get("code", ""),
            "url": st.get("url", ""),
            "gamertag": st.get("gamertag", ""),
            "error": st.get("error", ""),
            "uptime": self.uptime,
            "console_next": weiter,
            "state_text": state_text(zustand, code=st.get("code", ""), url=st.get("url", ""),
                                     gamertag=st.get("gamertag", ""), error=st.get("error", "")),
        }


# ---------------------------------------------------------------- Verwaltung aller Bots

class BroadcasterRegistry:
    """Alle Bots des Dienstes – höchstens einer je Instanz."""

    def __init__(self) -> None:
        self._bots: dict[str, Broadcaster] = {}
        self._lock = threading.Lock()

    def get(self, spec: XboxSpec) -> Broadcaster:
        """Bot holen oder anlegen. Läuft er, bleiben die laufenden Angaben gültig; die neuen
        gelten ab dem nächsten Start (``restart_needed`` sagt, dass es einen braucht)."""
        with self._lock:
            bot = self._bots.get(spec.instance_id)
            if bot is None:
                bot = Broadcaster(spec)
                self._bots[spec.instance_id] = bot
            else:
                bot.spec = spec
                if not bot.running:
                    bot.active_spec = spec
            return bot

    def find(self, instance_id: str) -> Broadcaster | None:
        with self._lock:
            return self._bots.get(str(instance_id))

    def all(self) -> list[Broadcaster]:
        with self._lock:
            return list(self._bots.values())

    def running_ids(self) -> list[str]:
        return sorted(b.spec.instance_id for b in self.all() if b.running)

    def stop(self, instance_id: str, timeout: int = STOP_TIMEOUT) -> bool:
        """Den Bot einer Instanz stoppen. ``True``, wenn überhaupt einer lief."""
        bot = self.find(instance_id)
        if bot is None or not bot.running:
            return False
        bot.stop(timeout=timeout)
        return True

    def drop(self, instance_id: str, timeout: int = STOP_TIMEOUT) -> None:
        """Bot stoppen und aus der Verwaltung nehmen (Instanz gelöscht oder zurückgeholt)."""
        with self._lock:
            bot = self._bots.pop(str(instance_id), None)
        if bot is not None:
            bot.stop(timeout=timeout)

    def stop_all(self, timeout: int = STOP_TIMEOUT) -> list[str]:
        """Alle Bots anhalten (Ende des Dienstes). Rückgabe: welche liefen.

        Anders als die Server werden die Bots wirklich gestoppt: sie halten keine Welt, und ein
        vergessener Bot kündigte nach dem Neustart dieselbe Sitzung doppelt an.
        """
        laufende = [b for b in self.all() if b.running]
        faeden = []
        for bot in laufende:
            faden = threading.Thread(target=bot.stop, kwargs={"timeout": timeout}, daemon=True,
                                     name=f"xbox-stop-{bot.spec.instance_id}")
            faden.start()
            faeden.append(faden)
        deadline = time.time() + timeout + TERM_WAIT + KILL_WAIT
        for faden in faeden:
            faden.join(max(0.1, deadline - time.time()))
        return sorted(b.spec.instance_id for b in laufende)


def status_for(spec: XboxSpec, bot: Broadcaster | None, *, enabled: bool, autostart: bool) -> dict:
    """Der vollständige Zustand für die API – auch wenn gar kein Bot angelegt ist.

    Die Felder heißen wie in ``app.py`` des Programms auf dem PC (``manager.xbox_status``), damit
    die Oberfläche kaum etwas umlernen muss. Dazu kommen ``state_text`` und ``autostart``.
    """
    jar = cached_jar()
    java = java_path()
    out = {
        "enabled": bool(enabled),
        "autostart": bool(autostart),
        "installed": jar is not None and java is not None,
        "running": False,
        "state": "off",
        "code": "", "url": "", "gamertag": "", "error": "",
        "address": spec.address,
        "port": spec.port,
        "host_name": spec.name,
        "token_cached": token_cached(spec.directory),
        "uptime": 0,
        "console_next": 0,
        "restart_needed": False,
    }
    if bot is not None:
        out.update(bot.status())
        out["restart_needed"] = bot.restart_needed
    out["state_text"] = state_text(out["state"], code=out["code"], url=out["url"],
                                   gamertag=out["gamertag"], error=out["error"])
    if not out["installed"]:
        out["state_text"] = ("Der Xbox-Freunde-Modus ist für diesen Server noch nicht "
                            "eingerichtet.")
    return out


def reset(spec: XboxSpec, bot: Broadcaster | None = None) -> None:
    """Anmeldung verwerfen: Bot stoppen und den Ordner mit dem Token löschen.

    Beim nächsten Start zeigt der Bot dann einen neuen Anmelde-Code. Gelöscht wird **nur** der
    Ordner ``xbox/cache`` – Welt und Serverdateien werden nie angefasst.
    """
    if bot is not None:
        bot.stop()
    folder = xbox_dir(spec.directory) / "cache"
    try:
        for kind in sorted(folder.rglob("*"), reverse=True):
            if kind.is_symlink() or kind.is_file():
                kind.unlink(missing_ok=True)
            elif kind.is_dir():
                kind.rmdir()
        folder.rmdir()
    except OSError:
        pass
