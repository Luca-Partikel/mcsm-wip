"""Betrieb der Minecraft-Server auf dem Root-Server (Linux).

Je Instanz ein `Runner`: starten, Konsolenausgabe puffern, Befehle senden, sauber stoppen
(Konsolenbefehl → Wartezeit → SIGTERM → SIGKILL), Laufzeit und Speicherverbrauch melden.

Die Vorlage ist `core/manager.py` des lokalen Programms; alles Windows-Eigene (Job-Objekt,
CREATE_NO_WINDOW, .exe) ist durch Linux-Mittel ersetzt:

* Der Server läuft in einer eigenen Sitzung (`setsid`), also in einer eigenen Prozessgruppe.
  Beim Stoppen bekommt die ganze Gruppe das Signal – Kindprozesse gehen sicher mit.
* Minecraft-Prozesse laufen **nie als root**. Läuft der Daemon versehentlich als root,
  verweigert der Runner den Start.
* Der Server erbt nicht die Umgebung des Daemons (keine Token, keine Pfade), sondern bekommt
  eine kleine, feste Umgebung mit `HOME` im Instanzordner.
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
from typing import Callable, Mapping, Sequence

from . import isolation, sources_linux

# ---------------------------------------------------------------- Grenzen und Texte

MAX_LOG_LINES = 4000            # Ringpuffer der Konsole je Instanz
MAX_LINE_CHARS = 2000           # überlange Zeilen werden gekürzt (Speicher und JSON-Größe)
MAX_COMMAND_CHARS = 512

RAM_MIN_MB = 512
RAM_MAX_MB = 12288              # mehr gibt die Maschine nicht her (13 GB gesamt)

START_READY_PATTERNS = (
    re.compile(r"Done \(\d"),                      # Paper/Vanilla
    re.compile(r"Server started\."),               # Bedrock Dedicated Server
)
# Farbcodes von log4j – im JSON wären das Zeichensalat.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Angekündigter Stopp über das Begleit-Plugin (gleiche Befehle wie im lokalen Programm).
STOP_COUNTDOWN = 10             # Sekunden, die das Plugin im Spiel herunterzählt
ANNOUNCE_ANSWER = 2.0           # so lange auf die Bestätigung des Plugins warten
ANNOUNCE_EXTRA = 5              # Zugabe nach dem Countdown, bevor „stop“ hinterhergeht
_ANNOUNCE_OK_RE = re.compile(r"Herunterfahren angefordert")

STOP_TIMEOUT = 60               # so lange darf „stop“ zum Speichern der Welt brauchen
TERM_WAIT = 15                  # nach SIGTERM
KILL_WAIT = 5                   # nach SIGKILL

BEDROCK_BINARY = "bedrock_server"
# Verwaltungsordner der Instanz. `.mcsm` ist in core/paths.py als Verwaltungsname gesperrt: der
# Besitzer sieht ihn nicht in der Dateiliste, und bei einer Übertragung bleibt er auf dem Server.
# Dort liegen die Prozesskennung und die Arbeitsordner des Servers (HOME, TMPDIR) – so bleibt der
# Instanzordner selbst sauber (sonst landete z. B. `hsperfdata_mcsm` zwischen den Welten).
MANAGE_DIR = isolation.MANAGE_DIR
PID_FILE = "mcsm.pid"

KIND_JAVA = "java"
KIND_BEDROCK = "bedrock"
VALID_KINDS = (KIND_JAVA, KIND_BEDROCK)

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
# JVM-Schalter: nur -XX:… und -D… ohne Leerzeichen und Sonderzeichen.
_FLAG_RE = re.compile(r"-(?:XX:|D)[A-Za-z0-9+\-:._=/,\[\]]{1,160}")
# Diese JVM-Schalter führen Programme aus oder schreiben beliebige Dateien – niemals übernehmen,
# auch nicht, wenn sie in einer gespeicherten Konfiguration stehen.
_FLAG_DENY = ("onerror", "onoutofmemoryerror", "abortvmonexception", "errorfile",
              "heapdumppath", "logfile", "flightrecorderoptions", "startflightrecording",
              "nativememorytracking=detail")

JAVA_ENCODING_FLAGS = ("-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8",
                       "-Dstderr.encoding=UTF-8", "-Dstdin.encoding=UTF-8")

#: Kennzeichen der Instanz in der Java-Kommandozeile. Ohne das stünde in ``/proc/<pid>/cmdline``
#: nur „java … -jar server.jar nogui“ – der Instanzordner käme darin nicht vor, und ein
#: weiterlaufender Server ließe sich nach einem Neustart des Dienstes nicht mehr zuordnen
#: (siehe `orphan_pid`). Der Wert ist die Instanzkennung, also nichts Geheimes.
INSTANZ_FLAG = "-Dmcsm.instanz="

#: So weit dürfen die Startzeit aus /proc und der Zeitstempel in .mcsm/mcsm.pid auseinanderliegen.
PID_START_TOLERANZ = 5


# ---------------------------------------------------------------- Wurzelordner der Instanzen

_instances_root: pathlib.Path | None = None


def instances_root() -> pathlib.Path:
    """Ordner, unter dem alle Instanzordner liegen müssen (Standard `/srv/mcsm/users`)."""
    return _instances_root if _instances_root is not None else sources_linux.users_dir()


def set_instances_root(path: str | os.PathLike) -> pathlib.Path:
    """Wurzelordner umstellen (Selbsttests, abweichende Installation)."""
    global _instances_root
    _instances_root = pathlib.Path(path).expanduser().resolve()
    return _instances_root


def check_instance_dir(directory: str | os.PathLike) -> pathlib.Path:
    """Prüft, dass der Instanzordner wirklich unter dem Wurzelordner liegt – kein `..`,
    keine Symlinks nach draußen."""
    root = instances_root()
    raw = pathlib.Path(directory)
    if not raw.is_absolute():
        raise ValueError("Der Instanzordner muss ein absoluter Pfad sein.")
    if ".." in raw.parts:
        raise ValueError("Der Instanzordner darf kein „..“ enthalten.")
    # strict=False: der Ordner darf noch fehlen (wird vor dem Start angelegt), vorhandene
    # Symlinks werden aber aufgelöst und damit ein Ausbruch erkannt.
    resolved = raw.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Der Instanzordner liegt außerhalb des erlaubten Bereichs.") from exc
    if resolved == root.resolve():
        raise ValueError("Der Instanzordner darf nicht der Wurzelordner selbst sein.")
    return resolved


# ---------------------------------------------------------------- Startbefehl

def safe_java_flags(flags: Sequence[str] | None) -> list[str]:
    """Aus einer Liste JVM-Schalter die unbedenklichen heraussuchen.

    Erlaubt sind nur `-XX:…` und `-D…` ohne Leerzeichen. Schalter, die Programme starten oder
    Dateien an beliebige Stellen schreiben (`-XX:OnOutOfMemoryError=…`, `-XX:ErrorFile=…` …),
    werden verworfen – sonst wäre eine gespeicherte Konfiguration ein Einfallstor.
    """
    out: list[str] = []
    for raw in flags or ():
        if not isinstance(raw, str):
            continue
        flag = raw.strip()
        if not _FLAG_RE.fullmatch(flag):
            continue
        name = flag.split("=", 1)[0].lower()
        if flag.startswith("-XX:"):
            name = name[4:].lstrip("+-")
        else:
            name = name[2:]
        if any(name.startswith(bad) or bad in flag.lower() for bad in _FLAG_DENY):
            continue
        if flag not in out:
            out.append(flag)
    return out


@dataclasses.dataclass(frozen=True)
class RunSpec:
    """Alles, was zum Starten einer Instanz gebraucht wird.

    Der Daemon baut sie aus seinem Instanz-Datensatz; der Runner liest hier nur und prüft.
    """

    instance_id: str
    directory: pathlib.Path
    kind: str = KIND_JAVA
    name: str = "Minecraft Server"
    version: str = ""
    ram_mb: int = 4096
    java: pathlib.Path | None = None
    java_major: int = 0
    java_flags: tuple[str, ...] = ()
    jar: str = "server.jar"
    companion: bool = True                       # Paper mit Begleit-Plugin (Befehl `mcsmstop`)
    after_ready_commands: tuple[str, ...] = ()   # z. B. Gamerules ab 1.21.9
    extra_env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    run_as: str = ""                             # Unix-Benutzer des Servers (core/isolation.py)

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(str(self.instance_id or "")):
            raise ValueError("Ungültige Instanzkennung.")
        if str(self.kind) not in VALID_KINDS:
            raise ValueError(f"Unbekannte Serverart: {self.kind!r} (erlaubt: java, bedrock)")
        object.__setattr__(self, "directory", check_instance_dir(self.directory))
        try:
            ram = int(self.ram_mb)
        except (TypeError, ValueError) as exc:
            raise ValueError("Der Arbeitsspeicher muss eine Zahl in MB sein.") from exc
        if not RAM_MIN_MB <= ram <= RAM_MAX_MB:
            raise ValueError(f"Der Arbeitsspeicher muss zwischen {RAM_MIN_MB} und "
                             f"{RAM_MAX_MB} MB liegen.")
        object.__setattr__(self, "ram_mb", ram)
        jar = str(self.jar or "server.jar")
        if "/" in jar or "\\" in jar or jar.startswith(".") or not jar.endswith(".jar"):
            raise ValueError("Der Name der Server-Datei muss im Instanzordner liegen "
                             "und auf .jar enden.")
        object.__setattr__(self, "jar", jar)
        object.__setattr__(self, "java_flags", tuple(safe_java_flags(self.java_flags)))
        object.__setattr__(self, "after_ready_commands",
                           tuple(check_command(c) for c in self.after_ready_commands or ()))
        object.__setattr__(self, "extra_env", _clean_env(self.extra_env))
        object.__setattr__(self, "name", re.sub(r"[\x00-\x1f]", "", str(self.name or ""))[:48]
                           or "Minecraft Server")
        if self.java is not None:
            object.__setattr__(self, "java", pathlib.Path(self.java))
        run_as = str(self.run_as or "").strip()
        if run_as and not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", run_as):
            raise ValueError("Ungültiger Benutzername für den Serverprozess.")
        object.__setattr__(self, "run_as", run_as)

    @property
    def is_java(self) -> bool:
        return self.kind == KIND_JAVA

    @classmethod
    def from_config(cls, cfg: Mapping, directory: str | os.PathLike, **over) -> "RunSpec":
        """Aus einer Konfiguration im Format des lokalen Programms (store.DEFAULTS)."""
        kind = KIND_BEDROCK if str(cfg.get("type") or "") == "bedrock" else KIND_JAVA
        flavor = str(cfg.get("flavor") or "paper")
        data = {
            "instance_id": str(cfg.get("id") or ""),
            "directory": directory,
            "kind": kind,
            "name": str(cfg.get("name") or ""),
            "version": str(cfg.get("version") or ""),
            "ram_mb": int(cfg.get("ram_mb") or 4096),
            "java_major": int(cfg.get("java_major") or 0),
            "java_flags": tuple(cfg.get("java_flags") or ()),
            "companion": kind == KIND_JAVA and flavor == "paper",
        }
        data.update(over)
        return cls(**data)


def check_command(command: str) -> str:
    """Einen Konsolenbefehl prüfen: genau eine Zeile, keine Steuerzeichen, begrenzte Länge.

    Ohne diese Prüfung könnte ein zusätzliches `\\n` im Text einen zweiten Befehl an die
    Server-Konsole schmuggeln.
    """
    text = str(command or "").strip()
    if not text:
        raise ValueError("Es wurde kein Befehl angegeben.")
    if len(text) > MAX_COMMAND_CHARS:
        raise ValueError(f"Der Befehl ist zu lang (höchstens {MAX_COMMAND_CHARS} Zeichen).")
    if re.search(r"[\x00-\x1f\x7f]", text):
        raise ValueError("Der Befehl darf nur eine Zeile ohne Steuerzeichen sein.")
    return text


def _clean_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (extra or {}).items():
        name = str(key)
        text = str(value)
        if not _ENV_KEY_RE.fullmatch(name) or re.search(r"[\x00-\x1f]", text) or len(text) > 512:
            raise ValueError(f"Ungültige Umgebungsvariable: {name!r}")
        out[name] = text
    return out


def resolve_java(spec: RunSpec) -> pathlib.Path:
    """Der Java-Start braucht eine wirklich vorhandene Laufzeit."""
    if spec.java is not None:
        java = spec.java
        if java.is_file():
            return java
        raise ValueError(f"Die Java-Laufzeit {java} ist nicht vorhanden.")
    major = int(spec.java_major or 0)
    found = sources_linux.java_path(major) if major else None
    if found is None:
        raise ValueError(f"Java {major or '?'} ist auf dem Server nicht eingerichtet – "
                         "die Laufzeit muss vor dem Start geladen werden.")
    return found


def instanz_kennzeichen(spec: RunSpec) -> str:
    """``-Dmcsm.instanz=<kennung>`` – daran erkennt der Dienst seine Server in ``/proc``."""
    return f"{INSTANZ_FLAG}{spec.instance_id}"


def build_command(spec: RunSpec) -> list[str]:
    """Der vollständige Startbefehl als Argumentliste (keine Shell, keine Zeichenketten-Bastelei)."""
    if spec.is_java:
        java = resolve_java(spec)
        xmx = spec.ram_mb
        xms = max(RAM_MIN_MB, xmx // 2)
        return [str(java), f"-Xms{xms}M", f"-Xmx{xmx}M", *JAVA_ENCODING_FLAGS,
                instanz_kennzeichen(spec), *spec.java_flags, "-jar", spec.jar, "nogui"]
    return [str(spec.directory / BEDROCK_BINARY)]


def manage_dir(spec: RunSpec) -> pathlib.Path:
    """Verwaltungsordner der Instanz (`.mcsm`) – gehört dem Manager, nicht dem Besitzer."""
    return spec.directory / MANAGE_DIR


def build_env(spec: RunSpec) -> dict[str, str]:
    """Kleine, feste Umgebung für den Server-Prozess – er erbt nichts vom Daemon."""
    manage = manage_dir(spec)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(manage / "home"),
        "TMPDIR": str(manage / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": os.environ.get("TZ") or "Europe/Berlin",
    }
    if not spec.is_java:
        # BDS lädt seine mitgelieferten Bibliotheken aus dem eigenen Ordner.
        env["LD_LIBRARY_PATH"] = str(spec.directory)
    env.update(spec.extra_env)
    return env


# ---------------------------------------------------------------- Prozesse und Signale

def _linux() -> bool:
    return hasattr(os, "killpg") and hasattr(os, "getpgid")


def _signal_group(proc: subprocess.Popen, sig: int, hard: bool = False, run_as: str = "") -> None:
    """Signal an die ganze Prozessgruppe – so gehen auch Kindprozesse mit.

    Läuft der Server unter einem eigenen Benutzer, darf der Daemon ihm selbst kein Signal
    schicken; dann geht es über denselben Weg zurück, über den er gestartet wurde.

    Klappt das nicht (die Gruppe ist schon weg), bekommt wenigstens der Hauptprozess sein
    Signal. `hard` unterscheidet dabei „beenden“ von „abschießen“.
    """
    if proc.poll() is not None:
        return
    try:
        if _linux():
            pgid = os.getpgid(proc.pid)
            if run_as:
                if isolation.signal_group(run_as, pgid, sig):
                    return
            else:
                os.killpg(pgid, sig)
                return
    except OSError:                        # ProcessLookupError ist ein OSError
        pass
    try:
        proc.kill() if hard else proc.terminate()
    except OSError:
        pass


def signal_gruppe(proc: subprocess.Popen, sig: int, *, hard: bool = False,
                  run_as: str = "") -> None:
    """Öffentlicher Zugang zu `_signal_group`.

    Nicht nur der Server läuft in eigener Sitzung unter dem Benutzer des Kontos: der Bot des
    Xbox-Freunde-Modus (`core/xbox.py`) tut es genauso und wird genauso beendet. Damit gibt es
    für „Signal an die ganze Prozessgruppe“ nur eine Stelle.
    """
    _signal_group(proc, sig, hard=hard, run_as=run_as)


def signal_pid_gruppe(pid: int, sig: int, *, run_as: str = "") -> bool:
    """Signal an die Prozessgruppe einer **bekannten** Kennung (vergessener Prozess aus einem
    früheren Lauf). ``False``, wenn es den Prozess nicht mehr gibt."""
    try:
        pgid = os.getpgid(int(pid)) if _linux() else int(pid)
    except OSError:                        # ProcessLookupError ist ein OSError
        return False
    try:
        if _linux():
            if run_as:
                return isolation.signal_group(run_as, pgid, sig)
            os.killpg(pgid, sig)
        else:                                                   # pragma: no cover - nur Linux
            os.kill(int(pid), sig)
        return True
    except OSError:
        return False


def prozess_cmdline(pid: int) -> str:
    """``/proc/<pid>/cmdline`` als Text (leer, wenn nicht lesbar). Darf jeder lesen."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def _proc_stat(pid: int) -> list[str] | None:
    """Felder ab „state“ aus /proc/<pid>/stat (der Prozessname kann Leerzeichen enthalten)."""
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError):
        return None
    cut = text.rfind(")")
    if cut < 0:
        return None
    return text[cut + 2:].split()


def _page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, AttributeError):
        return 4096


def group_memory_mb(pid: int) -> int:
    """Speicherverbrauch (RSS) aller Prozesse der Prozessgruppe in MB.

    Der Server ist Gruppenführer (`setsid`), deshalb reicht ein Blick über /proc: alles mit
    dieser Gruppenkennung gehört zu diesem Server (Java plus etwaige Hilfsprozesse).
    """
    pid = int(pid)
    own = _proc_stat(pid)
    if own is None:
        return 0
    page = _page_size()
    total = int(own[21]) * page if len(own) > 21 and own[21].isdigit() else 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return int(total / 1048576)
    for name in entries:
        if not name.isdigit() or int(name) == pid:
            continue
        stat = _proc_stat(int(name))
        if not stat or len(stat) < 22:
            continue
        try:
            if int(stat[2]) != pid:              # Feld „pgrp“
                continue
            total += int(stat[21]) * page
        except (ValueError, IndexError):
            continue
    return int(total / 1048576)


def _pid_file(spec: RunSpec) -> pathlib.Path:
    return manage_dir(spec) / PID_FILE


def _write_pid_file(spec: RunSpec, pid: int) -> None:
    path = _pid_file(spec)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{pid}\n{int(time.time())}\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def pid_file_info(spec: RunSpec) -> tuple:
    """``(pid, gestartet_am)`` aus ``.mcsm/mcsm.pid`` – ``(None, 0)``, wenn es sie nicht gibt."""
    try:
        zeilen = _pid_file(spec).read_text(encoding="utf-8").splitlines()
        pid = int(zeilen[0].strip())
    except (OSError, ValueError, IndexError):
        return None, 0
    try:
        seit = int(zeilen[1].strip())
    except (ValueError, IndexError):
        seit = 0
    return pid, seit


def _boot_time() -> int:
    """Zeitpunkt des Systemstarts in Unix-Sekunden (``btime`` aus /proc/stat)."""
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def proc_start_time(pid: int, felder: list[str] | None = None) -> int:
    """Startzeit eines Prozesses in Unix-Sekunden (Feld 22 aus /proc/<pid>/stat). 0 = unbekannt."""
    stat = felder if felder is not None else _proc_stat(pid)
    if not stat or len(stat) < 20:
        return 0
    try:
        ticks = int(stat[19])                    # Feld 22 („starttime“), Zählung ab „state“
        hz = int(os.sysconf("SC_CLK_TCK")) or 100
    except (ValueError, OSError, AttributeError):
        return 0
    boot = _boot_time()
    return (boot + int(ticks / hz)) if boot else 0


def _proc_uid(pid: int) -> int | None:
    """Besitzer eines Prozesses. ``/proc/<pid>`` darf jeder lesen – anders als ``cwd``."""
    try:
        return int(os.stat(f"/proc/{int(pid)}").st_uid)
    except OSError:
        return None


def prozess_besitzer(pid: int) -> int | None:
    """Besitzer eines Prozesses (öffentlich, für `core/xbox.py`)."""
    return _proc_uid(pid)


def prozess_laeuft(pid: int) -> bool:
    """Gibt es diesen Prozess noch? (öffentlich, für `core/xbox.py`)"""
    return _proc_stat(pid) is not None


def _erwartete_uid(spec: RunSpec) -> int | None:
    if spec.run_as:
        return isolation.uid_of(spec.run_as)
    try:
        return int(os.getuid())                                 # pragma: no cover - nur Unix
    except AttributeError:                                      # pragma: no cover - Windows
        return None


def _passt_zur_instanz(pid: int, spec: RunSpec) -> bool:
    """Gehört dieser Prozess zu dieser Instanz?

    Gewertet wird die Kommandozeile (``/proc/<pid>/cmdline`` darf jeder lesen): bei Java steht
    dort ``-Dmcsm.instanz=<kennung>``, bei Bedrock der Instanzpfad. Der Arbeitsordner zählt nur
    als zusätzliche Bestätigung – ``readlink /proc/<pid>/cwd`` verlangt ``PTRACE_MODE_READ`` und
    scheitert, sobald Daemon und Server unter verschiedenen Benutzern laufen.
    """
    cmdline = prozess_cmdline(pid)
    # Absichtlich nicht „Instanzordner kommt irgendwo vor“: Auch die kurzen Hilfsaufrufe des
    # Dienstes (``chmod -Rf g+rwX <instanzordner>`` aus isolation.grant_group) tragen ihn in der
    # Kommandozeile und würden sonst als laufender Server gelten.
    merkmal = instanz_kennzeichen(spec) if spec.is_java else str(spec.directory / BEDROCK_BINARY)
    if cmdline and merkmal in cmdline:
        return True
    try:
        return os.readlink(f"/proc/{int(pid)}/cwd") == str(spec.directory)
    except OSError:
        return False                # „Zugriff verweigert“ ist kein Beweis – aber auch keiner dagegen


def server_pid_in_proc(spec: RunSpec) -> int | None:
    """Den laufenden Serverprozess dieser Instanz in ``/proc`` suchen.

    Nötig, weil in ``.mcsm/mcsm.pid`` bei getrennten Benutzern **nicht** der Server steht:
    ``isolation.wrap`` packt den Startbefehl in ``sudo``, und sudo bleibt als eigener Prozess
    stehen, um Signale weiterzureichen – als **root**, denn sudo ist setuid. Der Server ist
    dessen Kind und bekommt eine eigene Prozesskennung. Auf dem Root-Server nachgesehen:
    ``77401 root /usr/bin/sudo -n -u mcsmsrv01 …`` und ``77403 mcsmsrv01 …/java -Xms…``.

    Gesucht wird deshalb nach Besitzer **und** Kennzeichen: der Prozess muss dem Benutzer dieser
    Instanz gehören (damit fällt der sudo-Aufruf als root heraus) und die Instanz in seiner
    Kommandozeile nennen.
    """
    erwartet = _erwartete_uid(spec)
    if erwartet is None:
        return None
    try:
        namen = os.listdir("/proc")
    except OSError:                                             # pragma: no cover - kein /proc
        return None
    for name in namen:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or _proc_uid(pid) != erwartet:
            continue
        if _passt_zur_instanz(pid, spec):
            return pid
    return None


def orphan_pid(spec: RunSpec) -> int | None:
    """Läuft aus einem früheren Daemon-Lauf noch ein Server für diese Instanz?

    Der Daemon fragt das beim Start: ein vergessener Prozess hält sonst Port und Welt fest.

    Entschieden wird **nicht** über ``/proc/<pid>/cwd`` und **nicht** allein über
    ``.mcsm/mcsm.pid``:

    * ``readlink("/proc/<pid>/cwd")`` verlangt ``PTRACE_MODE_READ``. Der Daemon läuft als
      ``mcsm``, der Serverprozess unter einem eigenen Benutzer – der Aufruf scheitert mit
      „Zugriff verweigert“.
    * In ``.mcsm/mcsm.pid`` steht die Kennung des **sudo**-Aufrufs, nicht die des Servers
      (siehe `server_pid_in_proc`). Nach einem Neustart des Dienstes kann sudo längst weg sein,
      während der Server weiterspielt.

    Würde eines von beidem als „läuft nicht mehr“ gewertet, verlöre der Dienst nach jedem
    ``systemctl restart mcsm`` jeden laufenden Server: Der Java-Prozess liefe weiter, wäre aber
    nicht mehr stoppbar, zählte nicht mehr gegen ``max_concurrent`` und ``ram_total_mb``, und ein
    „Start“ des Besitzers brächte eine zweite JVM auf dieselbe Welt (Weltkorruption).

    Deshalb: erst den Serverprozess in ``/proc`` suchen, dann hilfsweise den Eintrag aus
    ``.mcsm/mcsm.pid`` prüfen (Kennzeichen, sonst Besitzer **und** Startzeit).
    """
    gefunden = server_pid_in_proc(spec)
    if gefunden is not None:
        return gefunden
    pid, seit = pid_file_info(spec)
    if pid is None or pid <= 1:
        return None
    felder = _proc_stat(pid)
    if felder is None:
        return None
    if _passt_zur_instanz(pid, spec):
        return pid
    # Kein Kennzeichen in der Kommandozeile: Server aus einer älteren Fassung des Dienstes.
    # Dann müssen Besitzer **und** Startzeit stimmen – die Prozesskennung könnte neu vergeben sein.
    erwartet = _erwartete_uid(spec)
    if erwartet is None or _proc_uid(pid) != erwartet:
        return None
    start = proc_start_time(pid, felder)
    if seit and start and abs(start - int(seit)) <= PID_START_TOLERANZ:
        return pid
    return None


def stop_orphan(spec: RunSpec, timeout: float = TERM_WAIT) -> bool:
    """Einen vergessenen Server-Prozess beenden (SIGTERM, dann SIGKILL). True, wenn danach Ruhe ist."""
    pid = orphan_pid(spec)
    if pid is None:
        return True
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            if _linux():
                pgid = os.getpgid(pid)
                if spec.run_as:
                    isolation.signal_group(spec.run_as, pgid, sig)
                else:
                    os.killpg(pgid, sig)
            else:                                                   # pragma: no cover - nur Linux
                os.kill(pid, sig)
        except (OSError, ProcessLookupError):
            return True
        deadline = time.time() + (timeout if sig == signal.SIGTERM else KILL_WAIT)
        while time.time() < deadline:
            if _proc_stat(pid) is None:
                _pid_file(spec).unlink(missing_ok=True)
                return True
            time.sleep(0.2)
    return _proc_stat(pid) is None


class UebernommenerProzess:
    """Ein Serverprozess aus einem früheren Lauf des Dienstes – beobachtet, nicht selbst gestartet.

    Ein Update des Dienstes soll für die Spieler keine Zwangsunterbrechung sein: die Server
    laufen in eigener Sitzung weiter (``KillMode=process``), und der neue Dienst sammelt sie
    beim Start ein. An ihre Konsole kommt er nicht mehr heran – ``stdin`` und ``stdout`` gehörten
    dem alten Prozess –, aber er kennt ihre Prozesskennung. Zustand, Speicherverbrauch und ein
    sauberer Stopp (SIGTERM an die Gruppe, Paper speichert dabei) funktionieren weiter.

    Nach außen verhält sich das Objekt wie ein ``subprocess.Popen``, soweit der Runner es
    braucht: ``pid``, ``poll()``, ``wait()``, ``terminate()``, ``kill()``, ``stdin``/``stdout``.
    """

    __slots__ = ("pid", "returncode", "stdin", "stdout")

    def __init__(self, pid: int) -> None:
        self.pid = int(pid)
        self.returncode: int | None = None
        self.stdin = None                 # bewusst nicht vorhanden: der Kanal ist fremd
        self.stdout = None

    def poll(self) -> int | None:
        if self.returncode is None and _proc_stat(self.pid) is None:
            # Den echten Rückgabewert kennt nur der Elternprozess, und der ist weg. 0 heißt
            # hier „beendet, ohne dass wir den Grund kennen“ – als Fehler wird es nicht gewertet.
            self.returncode = 0
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.time() + float(timeout)
        while self.poll() is None:
            if deadline is not None and time.time() >= deadline:
                raise subprocess.TimeoutExpired("mcsm-uebernommen", timeout or 0)
            time.sleep(0.5)
        return int(self.returncode or 0)

    def _signal(self, sig: int) -> None:
        if self.poll() is not None:
            return
        try:
            os.kill(self.pid, sig)
        except OSError:
            pass

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def __repr__(self) -> str:                              # pragma: no cover - nur Fehlersuche
        return f"UebernommenerProzess(pid={self.pid}, returncode={self.returncode})"


# ---------------------------------------------------------------- Runner

class Runner:
    """Ein Minecraft-Server auf dem Root-Server."""

    def __init__(self, spec: RunSpec, *, on_exit: Callable[["Runner", int], None] | None = None,
                 on_line: Callable[["Runner", str], None] | None = None) -> None:
        self.spec = spec
        self.active_spec = spec          # die Angaben, mit denen wirklich gestartet wurde
        self.on_exit = on_exit
        self.on_line = on_line
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.offset = 0                  # Zahl der bereits verworfenen Zeilen (fortlaufende Nummer)
        self.started_at = 0.0
        self.ready_at = 0.0
        self.stopping = False
        self.exit_code: int | None = None
        self.last_error = ""
        self._buf_lock = threading.Lock()
        self._proc_lock = threading.RLock()
        self._pump: threading.Thread | None = None

    # -- Konsolenpuffer -------------------------------------------------
    def log(self, text: str) -> None:
        """Eine Zeile in den Ringpuffer legen (eigene Meldungen des Managers eingeschlossen)."""
        line = _ANSI_RE.sub("", str(text)).rstrip("\r\n")[:MAX_LINE_CHARS]
        with self._buf_lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                drop = len(self.lines) - MAX_LOG_LINES
                del self.lines[:drop]
                self.offset += drop
        if self.on_line is not None:
            try:
                self.on_line(self, line)
            except Exception:                        # noqa: BLE001 - Hänger im Daemon vermeiden
                pass

    def console(self, since: int = 0, tail: int | None = None) -> dict:
        """{"next": nächste Zeilennummer, "lines": [...]} – wie im lokalen Programm."""
        with self._buf_lock:
            if tail is not None:
                lines = self.lines[-int(tail):] if int(tail) > 0 else []
            else:
                start = max(0, int(since) - self.offset)
                lines = self.lines[start:]
            return {"next": self.offset + len(self.lines), "lines": list(lines)}

    # -- Zustand --------------------------------------------------------
    @property
    def running(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    @property
    def ready(self) -> bool:
        """Der Server hat die Welt geladen und nimmt Spieler an."""
        return self.running and self.ready_at > 0

    @property
    def pid(self) -> int | None:
        proc = self.proc
        return proc.pid if proc is not None and proc.poll() is None else None

    @property
    def uptime(self) -> int:
        return int(time.time() - self.started_at) if self.running and self.started_at else 0

    def memory_mb(self) -> int:
        pid = self.pid
        return group_memory_mb(pid) if pid else 0

    def status(self) -> dict:
        """Zustand für die API (deutsche Anzeigetexte macht das Programm auf dem PC)."""
        with self._buf_lock:
            next_line = self.offset + len(self.lines)
        return {
            "id": self.spec.instance_id,
            "running": self.running,
            "ready": self.ready,
            "stopping": self.stopping,
            "pid": self.pid,
            "uptime": self.uptime,
            "ram_mb": self.active_spec.ram_mb if self.running else 0,
            "memory_mb": self.memory_mb(),
            "exit_code": self.exit_code,
            "error": self.last_error,
            # Übernommen = lief über einen Neustart des Dienstes hinweg weiter. Das Programm auf
            # dem PC kann daraus den Hinweis „Konsole erst ab jetzt“ machen.
            "uebernommen": self.running and self.adopted,
            "console_next": next_line,
        }

    # -- Starten --------------------------------------------------------
    def _spawn(self, cmd: list[str], cwd: str, env: dict[str, str]) -> subprocess.Popen:
        """Prozess anlegen. Eigene Methode, damit Selbsttests sie ersetzen können.

        Läuft der Server unter einem eigenen Unix-Benutzer, wird der Befehl über ``sudo``
        eingepackt (siehe core/isolation.py) und bringt seine Umgebung selbst mit.
        """
        run_as = self.active_spec.run_as
        if run_as:
            cmd = isolation.wrap(cmd, run_as, env)
        return subprocess.Popen(                                    # noqa: S603 - Argumentliste
            cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, close_fds=True,
            start_new_session=True)                                 # setsid: eigene Prozessgruppe

    def _prepare(self, spec: RunSpec) -> list[str]:
        """Vor dem Start prüfen und den Befehl bauen; wirft ValueError mit klarem Satz."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise ValueError("Minecraft-Server dürfen nicht als root laufen – der Dienst muss "
                             "als Benutzer mcsm gestartet werden.")
        directory = check_instance_dir(spec.directory)
        if not directory.is_dir():
            raise ValueError("Der Ordner dieses Servers ist nicht vorhanden.")
        if not spec.run_as and isolation.required():
            raise ValueError("Für diesen Server ist kein eigener Unix-Benutzer eingerichtet, und "
                             "MCSM_REQUIRE_ISOLATION verlangt einen. Bitte deploy.sh erneut "
                             "ausführen.")
        try:
            for folder in (manage_dir(spec) / "home", manage_dir(spec) / "tmp"):
                folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Der Verwaltungsordner {MANAGE_DIR} konnte nicht angelegt "
                             f"werden: {exc}") from exc
        if spec.run_as:
            # Der Serverprozess läuft unter einem eigenen Benutzer: sein Ordner bekommt dessen
            # Gruppe, und alte Dateien aus der Zeit davor werden einmalig nachgezogen.
            isolation.apply_dir(directory, spec.run_as)
        # **Vor** dem Durchlauf: `.mcsm` gehört dem Dienst und wird für den Serverprozess
        # geschlossen. Sonst könnte ein Plugin die Merkdatei löschen und `fix_tree` bei jedem
        # Start erneut auslösen – und darin liegt ein Zeitfenster, das es auszunutzen gilt.
        isolation.secure_manage(directory, spec.run_as)
        if spec.run_as:
            isolation.fix_tree(directory, spec.run_as)
        if spec.is_java:
            jar = directory / spec.jar
            if not jar.is_file():
                raise ValueError("Der Server ist nicht vollständig installiert "
                                 f"({spec.jar} fehlt).")
        else:
            exe = directory / BEDROCK_BINARY
            if not exe.is_file():
                raise ValueError("Der Server ist nicht vollständig installiert "
                                 f"({BEDROCK_BINARY} fehlt).")
            try:
                if not os.access(exe, os.X_OK):
                    exe.chmod(0o750)
            except OSError as exc:
                raise ValueError(f"{BEDROCK_BINARY} konnte nicht ausführbar gemacht "
                                 f"werden: {exc}") from exc
        return build_command(spec)

    def start(self) -> None:
        """Server starten. Läuft er schon, passiert nichts."""
        with self._proc_lock:
            if self.running:
                return
            spec = self.spec
            cmd = self._prepare(spec)
            env = build_env(spec)
            leftover = orphan_pid(spec)
            if leftover is not None:
                raise ValueError("Für diesen Server läuft noch ein Prozess aus einem früheren "
                                 "Lauf. Er muss zuerst beendet werden.")
            with self._buf_lock:
                self.lines.clear()
                self.offset = 0
            self.stopping = False
            self.exit_code = None
            self.last_error = ""
            self.ready_at = 0.0
            self.active_spec = spec
            self.log(f"[Manager] Starte {spec.name} …")
            try:
                self.proc = self._spawn(cmd, str(spec.directory), env)
            except OSError as exc:
                self.proc = None
                raise ValueError(f"Der Server konnte nicht gestartet werden: {exc}") from exc
            self.started_at = time.time()
            _write_pid_file(spec, self.proc.pid)
            self._pump = threading.Thread(target=self._read_output, args=(self.proc,),
                                          name=f"mcsm-console-{spec.instance_id}", daemon=True)
            self._pump.start()

    # -- Übernehmen -----------------------------------------------------
    @property
    def adopted(self) -> bool:
        """Läuft hier ein übernommener Server aus einem früheren Lauf des Dienstes?"""
        return isinstance(self.proc, UebernommenerProzess)

    def adopt(self, pid: int, *, started_at: int = 0) -> None:
        """Einen weiterlaufenden Server aus einem früheren Lauf übernehmen.

        Der Server wird **nicht** angefasst: er spielt weiter. Der Dienst führt ihn ab jetzt
        wieder als laufend, kann ihn sauber stoppen und – bei Paper – seine Konsole aus
        ``logs/latest.log`` mitlesen. Befehle nimmt er erst nach dem nächsten eigenen Start an.
        """
        with self._proc_lock:
            if self.running:
                return
            spec = self.spec
            self.active_spec = spec
            with self._buf_lock:
                self.lines.clear()
                self.offset = 0
            self.stopping = False
            self.exit_code = None
            self.last_error = ""
            self.proc = UebernommenerProzess(pid)
            seit = int(started_at or 0)
            self.started_at = float(seit) if 0 < seit <= time.time() else time.time()
            # Der Server hat die Welt längst geladen – sonst liefe er nicht mehr.
            self.ready_at = self.started_at
            self.log(f"[Manager] „{spec.name}“ lief über den Neustart des Dienstes hinweg weiter "
                     f"(Prozess {pid}) und wird wieder übernommen. Die Konsole zeigt erst ab "
                     f"jetzt Zeilen; Konsolenbefehle nimmt der Server erst nach dem nächsten "
                     f"Start wieder an. Stoppen geht jederzeit – die Welt wird dabei gespeichert.")
            self._pump = threading.Thread(target=self._watch_adopted, args=(self.proc,),
                                          name=f"mcsm-uebernommen-{spec.instance_id}", daemon=True)
            self._pump.start()
            if spec.is_java:
                threading.Thread(target=self._tail_log, args=(self.proc,),
                                 name=f"mcsm-log-{spec.instance_id}", daemon=True).start()

    def _watch_adopted(self, proc: "UebernommenerProzess") -> None:
        """Auf das Ende eines übernommenen Prozesses warten (eigener Thread)."""
        while proc.poll() is None:
            time.sleep(1.0)
        code = int(proc.returncode or 0)
        self.exit_code = code
        self.ready_at = 0.0
        self.log("[Manager] Server beendet (übernommener Prozess).")
        self.stopping = False
        try:
            _pid_file(self.active_spec).unlink(missing_ok=True)
        except OSError:
            pass
        if self.on_exit is not None:
            try:
                self.on_exit(self, code)
            except Exception:                        # noqa: BLE001 - Daemon nicht mitreißen
                pass

    def _tail_log(self, proc: "UebernommenerProzess") -> None:
        """Konsole eines übernommenen Paper-Servers aus ``logs/latest.log`` mitlesen.

        Mehr geht nicht: die Ausgabe des fremden Prozesses ist für uns verloren. Gelesen wird ab
        dem Ende der Datei – der Besitzer sieht also alles, was **ab jetzt** passiert.
        """
        path = self.active_spec.directory / "logs" / "latest.log"
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return
        try:
            handle.seek(0, os.SEEK_END)
            while proc.poll() is None:
                line = handle.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                self.log(line)
        except OSError:
            return
        finally:
            try:
                handle.close()
            except OSError:
                pass

    def _read_output(self, proc: subprocess.Popen) -> None:
        """Ausgabe zeilenweise lesen, bis der Prozess endet (eigener Thread)."""
        stream = proc.stdout
        ready_done = False
        if stream is not None:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                self.log(line)
                if not ready_done and any(p.search(line) for p in START_READY_PATTERNS):
                    ready_done = True
                    self.ready_at = time.time()
                    self._after_ready()
        code = proc.wait()
        self.exit_code = code
        self.ready_at = 0.0
        self.log(f"[Manager] Server beendet (Code {code}).")
        if not self.stopping and code != 0:
            self.last_error = (f"Der Server hat sich unerwartet beendet (Code {code}). "
                               "Die letzten Konsolenzeilen nennen meist den Grund.")
        self.stopping = False
        _pid_file(self.active_spec).unlink(missing_ok=True)
        if self.on_exit is not None:
            try:
                self.on_exit(self, code)
            except Exception:                        # noqa: BLE001 - Daemon nicht mitreißen
                pass

    def _after_ready(self) -> None:
        """Befehle, die erst nach dem Laden der Welt gehen (z. B. Gamerules ab 1.21.9)."""
        for command in self.active_spec.after_ready_commands:
            try:
                self.send(command)
            except (ValueError, OSError):
                return

    # -- Befehle --------------------------------------------------------
    def send(self, command: str) -> None:
        """Einen Befehl in die Server-Konsole schreiben."""
        text = check_command(command)
        proc = self.proc
        if self.running and self.adopted:
            raise ValueError("Dieser Server lief über einen Neustart des Dienstes hinweg weiter – "
                             "seine Konsole gehört noch dem alten Vorgang und nimmt keine Befehle "
                             "an. Bitte den Server einmal stoppen und neu starten.")
        if not self.running or proc is None or proc.stdin is None:
            raise ValueError("Der Server läuft nicht.")
        try:
            proc.stdin.write((text + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ValueError(f"Der Befehl konnte nicht gesendet werden: {exc}") from exc
        self.log(f"> {text}")

    def announce(self, text: str) -> bool:
        """Eine Nachricht an alle Spieler (`say`). True, wenn sie abgeschickt wurde."""
        message = re.sub(r"[\x00-\x1f]", " ", str(text or "")).strip()[:200]
        if not message:
            return False
        try:
            self.send(f"say {message}")
            return True
        except ValueError:
            return False

    def _announce_stop(self, seconds: int) -> bool:
        """Begleit-Plugin: `mcsmstop <sekunden>` sagt den Stopp im Spiel an und stoppt danach selbst.

        Gewartet wird auf die Bestätigung im Protokoll. Bleibt sie aus (Plugin fehlt oder lehnt
        ab), wird mit `say` angekündigt und normal gestoppt.
        """
        seconds = max(0, min(600, int(seconds)))
        if not self.active_spec.companion:
            return False
        mark = self.console(0)["next"]
        try:
            self.send(f"mcsmstop {seconds}")
        except ValueError:
            return False
        deadline = time.time() + ANNOUNCE_ANSWER
        while self.running:
            for line in self.console(mark)["lines"]:
                if _ANNOUNCE_OK_RE.search(line):
                    self.log(f"[Manager] Stopp wird im Spiel angekündigt ({seconds} Sekunden) …")
                    return True
            if time.time() >= deadline:
                break
            time.sleep(0.2)
        self.log("[Manager] Der Server hat die Ankündigung nicht bestätigt – "
                 "er wird mit „say“ angekündigt und dann gestoppt.")
        return False

    def _wait_stopped(self, seconds: float) -> bool:
        deadline = time.time() + max(0.0, float(seconds))
        while time.time() < deadline:
            if not self.running:
                return True
            time.sleep(0.25)
        return not self.running

    def stop(self, timeout: int = STOP_TIMEOUT, announce_seconds: int = 0) -> None:
        """Sauber stoppen: Ankündigung (optional) → `stop` → SIGTERM → SIGKILL.

        `announce_seconds` > 0 kündigt den Stopp im Spiel an (Begleit-Plugin, sonst `say`) und
        wartet diese Zeit ab – so sieht ein Spieler den Ablauf seines Passes kommen.
        """
        proc = self.proc
        if not self.running or proc is None:
            return
        with self._proc_lock:
            if not self.running:
                return
            first = not self.stopping
            self.stopping = True
        if not first:
            self._wait_stopped(timeout)
            return
        seconds = max(0, min(900, int(announce_seconds or 0)))
        if seconds > 0:
            if self._announce_stop(seconds):
                # Das Plugin zählt herunter und stoppt selbst; danach geht „stop“ hinterher.
                self._wait_stopped(seconds + ANNOUNCE_EXTRA)
            else:
                self.announce(f"Dieser Server wird in {seconds} Sekunden gestoppt – "
                              "die Welt wird dabei gespeichert.")
                self._wait_stopped(seconds)
        if not self.running:
            return
        if self.adopted:
            # Ohne Konsole gibt es kein „stop“. SIGTERM an die Prozessgruppe ist hier der
            # richtige Weg: Paper und Vanilla speichern die Welt dabei ordentlich (im Versuch
            # auf dem Root-Server 1,6 s, Code 143), BDS beendet sich ebenfalls sauber.
            self.log("[Manager] Stoppe übernommenen Server mit SIGTERM – die Welt wird dabei "
                     "gespeichert.")
            _signal_group(proc, signal.SIGTERM, run_as=self.active_spec.run_as)
            if self._wait_stopped(max(TERM_WAIT, min(timeout, 120))):
                return
            self.log("[Manager] Der Server hängt – die Prozessgruppe wird mit SIGKILL beendet.")
            _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM), hard=True,
                          run_as=self.active_spec.run_as)
            self._wait_stopped(KILL_WAIT)
            return
        self.log("[Manager] Stoppe Server – Welt wird gespeichert …")
        try:
            if proc.stdin:
                proc.stdin.write(b"stop\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        if self._wait_stopped(timeout):
            return
        self.log("[Manager] Der Server reagiert nicht – die Prozessgruppe bekommt SIGTERM.")
        _signal_group(proc, signal.SIGTERM, run_as=self.active_spec.run_as)
        if self._wait_stopped(TERM_WAIT):
            return
        self.log("[Manager] Der Server hängt – die Prozessgruppe wird mit SIGKILL beendet.")
        _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM), hard=True,
                      run_as=self.active_spec.run_as)
        self._wait_stopped(KILL_WAIT)

    def kill(self) -> None:
        """Sofort beenden (nur für Notfälle – die Welt wird nicht gespeichert)."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self.stopping = True
        _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM), hard=True,
                      run_as=self.active_spec.run_as)
        self._wait_stopped(KILL_WAIT)


# ---------------------------------------------------------------- Verwaltung aller Runner

class RunnerRegistry:
    """Alle Runner des Daemons – eine Instanz je Server."""

    def __init__(self, *, on_exit: Callable[[Runner, int], None] | None = None,
                 on_line: Callable[[Runner, str], None] | None = None) -> None:
        self._runners: dict[str, Runner] = {}
        self._lock = threading.Lock()
        self._on_exit = on_exit
        self._on_line = on_line

    def get(self, spec: RunSpec) -> Runner:
        """Runner holen oder anlegen. Läuft der Server, bleiben die laufenden Angaben gültig;
        die neuen gelten ab dem nächsten Start."""
        with self._lock:
            runner = self._runners.get(spec.instance_id)
            if runner is None:
                runner = Runner(spec, on_exit=self._on_exit, on_line=self._on_line)
                self._runners[spec.instance_id] = runner
            else:
                runner.spec = spec
                if not runner.running:
                    runner.active_spec = spec
            return runner

    def adopt(self, spec: RunSpec, pid: int, *, started_at: int = 0) -> Runner:
        """Einen weiterlaufenden Server aus einem früheren Lauf des Dienstes übernehmen."""
        runner = self.get(spec)
        runner.adopt(pid, started_at=started_at)
        return runner

    def adopted_ids(self) -> list:
        """Instanzen, die über einen Neustart des Dienstes hinweg weiterlaufen."""
        return sorted(r.spec.instance_id for r in self.all() if r.running and r.adopted)

    def find(self, instance_id: str) -> Runner | None:
        with self._lock:
            return self._runners.get(str(instance_id))

    def all(self) -> list[Runner]:
        with self._lock:
            return list(self._runners.values())

    def running_ids(self) -> list[str]:
        return sorted(r.spec.instance_id for r in self.all() if r.running)

    def running_ram_mb(self) -> int:
        """Summe des zugesagten Arbeitsspeichers aller laufenden Server – Grundlage für die
        Pass-Prüfung (`ram_total_mb`). Gezählt wird, womit wirklich gestartet wurde."""
        return sum(r.active_spec.ram_mb for r in self.all() if r.running)

    def statuses(self) -> list[dict]:
        return [r.status() for r in self.all()]

    def drop(self, instance_id: str, timeout: int = 20) -> None:
        """Server stoppen und aus der Verwaltung nehmen (z. B. beim Löschen der Instanz)."""
        with self._lock:
            runner = self._runners.pop(str(instance_id), None)
        if runner is not None:
            runner.stop(timeout=timeout)

    def stop_all(self, timeout: int = STOP_TIMEOUT, announce_seconds: int = 0) -> None:
        """Alle Server stoppen – beim Beenden des Daemons, damit keine Waisen zurückbleiben.
        Die Server werden gleichzeitig angestoßen und dann gemeinsam abgewartet."""
        runners = [r for r in self.all() if r.running]
        threads = []
        for runner in runners:
            thread = threading.Thread(target=runner.stop,
                                      kwargs={"timeout": timeout,
                                              "announce_seconds": announce_seconds},
                                      daemon=True)
            thread.start()
            threads.append(thread)
        limit = timeout + ANNOUNCE_EXTRA + TERM_WAIT + KILL_WAIT + max(0, announce_seconds)
        deadline = time.time() + limit
        for thread in threads:
            thread.join(max(0.1, deadline - time.time()))
