"""Jeder Kunden-Server läuft unter einem **eigenen** Unix-Benutzer.

Warum: Ein Kunde darf Plugins und ganze Server-Dateien hochladen – das ist ausdrücklich so
gewollt. Ein Plugin ist beliebiger Java-Code. Läuft es unter demselben Benutzer wie der Daemon
(``mcsm``), kann es ohne Umweg über die HTTP-API

* Welten und Plugins **fremder** Konten unter ``/srv/mcsm/users/...`` lesen und ändern,
* ``/srv/mcsm/data/users.json``, ``invites.json``, ``passes.json`` und ``daemon.json`` lesen und
  ändern (dort stehen gültige Einladungscodes und das Geheimnis des ersten Admin-Kontos),
* fremde Server-Prozesse abschießen.

Die Mandantentrennung in ``core/paths.py`` sichert nur die HTTP-API, nicht das Dateisystem.

Wie: ``deploy.sh`` legt als root einen Vorrat von Server-Benutzern an (``mcsmsrv01`` …
``mcsmsrvNN``, je mit gleichnamiger Gruppe), nimmt ``mcsm`` in **alle** diese Gruppen auf und
erlaubt ``mcsm`` in ``/etc/sudoers.d/mcsm``, Befehle als genau diese Benutzer zu starten. Der
Daemon bleibt unprivilegiert: er kann nur zu diesen unprivilegierten Benutzern wechseln, nie zu
root.

Jedes Konto bekommt beim ersten Start fest einen dieser Benutzer (gemerkt in ``users.json``).
Der Instanzordner gehört weiter ``mcsm`` (der Daemon muss Dateien annehmen und ausliefern),
bekommt aber die Gruppe des Server-Benutzers und den Modus ``2770`` – der Serverprozess darf
alles in seinem Ordner, fremde Ordner sieht er nicht einmal.

Fehlt die Einrichtung (Entwicklungsrechner, Windows, altes Deployment), läuft alles wie bisher
unter dem Dienstbenutzer; der Daemon schreibt dann beim Start eine deutliche Warnung ins
Protokoll. Mit ``MCSM_REQUIRE_ISOLATION=1`` wird der Start stattdessen abgelehnt.
"""
from __future__ import annotations

import os
import re
import shutil
import stat as _stat
import subprocess
import sys
import threading

try:                                   # nur auf Unix vorhanden – Windows-Tests laufen ohne
    import grp
    import pwd
except ImportError:                    # pragma: no cover - Windows
    grp = None                         # type: ignore[assignment]
    pwd = None                         # type: ignore[assignment]

from . import store_hosted, users

#: Namensschema der Server-Benutzer. deploy.sh legt sie an, hier werden sie nur gesucht.
USER_PREFIX = "mcsmsrv"
_USER_RE = re.compile(r"^mcsmsrv[0-9]{1,3}$")

#: Modus der Instanzordner: Gruppe darf alles, andere gar nichts, setgid vererbt die Gruppe.
DIR_MODE = 0o2770

#: Umask des Serverprozesses: seine eigenen Dateien bleiben für die Gruppe (und damit für den
#: Daemon, der in allen Server-Gruppen ist) schreibbar – sonst könnte der Daemon eine von Java
#: neu geschriebene ``server.properties`` nicht mehr anpassen.
SERVER_UMASK = "0002"

#: Verwaltungsordner der Instanz. Er gehört dem Dienst, nicht dem Besitzer des Servers.
MANAGE_DIR = ".mcsm"

#: Rechte des Verwaltungsordners: der Dienst darf alles, der Server-Benutzer darf ihn nur
#: **durchlaufen** (``--x``). Ohne das erbt ``.mcsm`` vom Instanzordner (setgid, 2770) die Rechte
#: der Server-Gruppe – ein hochgeladenes Plugin könnte dann ``mcsm.pid`` neu schreiben, die
#: Merkdatei löschen (und damit `fix_tree` erneut auslösen) und ``install.json`` austauschen.
MANAGE_MODE = 0o710

#: Rechte der Arbeitsordner des Servers (HOME und TMPDIR liegen in ``.mcsm``): dort **muss** er
#: schreiben dürfen, sonst startet Java nicht.
WORK_MODE = 0o2770

#: Unterordner von ``.mcsm``, die dem Server-Benutzer offenstehen.
WORK_DIRS = ("home", "tmp")

#: Merkdatei im Instanzordner: unter dieser Gruppe wurden die Rechte zuletzt gerichtet.
MARKER = MANAGE_DIR + "/gruppe"

#: Grenzen für den einmaligen Durchlauf über den Instanzordner (`fix_tree`).
FIX_MAX_DEPTH = 40
FIX_MAX_ENTRIES = 400_000

_lock = threading.RLock()
_state: dict = {"pool": None, "sudo": None, "warned": False}


# ---------------------------------------------------------------- Vorrat der Server-Benutzer

def _unix() -> bool:
    return os.name == "posix" and pwd is not None


def sudo_path() -> str:
    """Pfad zu ``sudo`` – leer, wenn es das hier nicht gibt."""
    with _lock:
        if _state["sudo"] is None:
            _state["sudo"] = shutil.which("sudo") or ""
        return str(_state["sudo"])


def pool(refresh: bool = False) -> list[str]:
    """Die vorhandenen Server-Benutzer, nach Namen sortiert."""
    with _lock:
        if _state["pool"] is None or refresh:
            found: list[str] = []
            if _unix():
                try:
                    found = sorted(p.pw_name for p in pwd.getpwall()
                                   if _USER_RE.match(str(p.pw_name)))
                except OSError:                       # pragma: no cover - kaputte NSS
                    found = []
            _state["pool"] = found
        return list(_state["pool"])


def available() -> bool:
    """Ist die Trennung einsatzbereit (Unix, sudo, mindestens ein Server-Benutzer)?"""
    return bool(_unix() and sudo_path() and pool())


def required() -> bool:
    """Soll ein Start ohne Trennung abgelehnt werden? (``MCSM_REQUIRE_ISOLATION=1``)"""
    return str(os.environ.get("MCSM_REQUIRE_ISOLATION") or "").strip().lower() in (
        "1", "ja", "yes", "true")


def status() -> dict:
    """Kurzbericht für Protokoll und Admin-Übersicht."""
    return {"aktiv": available(), "benutzer": len(pool()), "sudo": sudo_path(),
            "erzwungen": required()}


def warn_once() -> str:
    """Einmalige Warnung fürs Protokoll – leer, wenn alles eingerichtet ist."""
    with _lock:
        if available() or _state["warned"]:
            return ""
        _state["warned"] = True
    return ("Achtung: Die Minecraft-Server laufen alle unter dem Dienstbenutzer, weil kein "
            "Vorrat an Server-Benutzern gefunden wurde. Ein hochgeladenes Plugin könnte damit "
            "fremde Instanzen und die Kontodaten lesen. Bitte deploy.sh erneut ausführen.")


# ---------------------------------------------------------------- Zuordnung Konto -> Benutzer

def user_for_owner(owner_id: str) -> str:
    """Fester Server-Benutzer eines Kontos. Leer, wenn die Trennung nicht eingerichtet ist.

    Die Zuordnung wird beim ersten Mal vergeben und in ``users.json`` gemerkt, damit die Dateien
    eines Kontos nach einem Neustart demselben Benutzer gehören. Vergeben wird der Benutzer mit
    den wenigsten Konten – bei genügend Vorrat bekommt jedes Konto einen eigenen.
    """
    owner = str(owner_id or "")
    if not owner or not available():
        return ""
    vorrat = pool()
    with store_hosted.lock():
        konto = users.get_user(owner)
        if not konto:
            return ""
        gemerkt = str(konto.get("server_user") or "")
        if gemerkt in vorrat:
            return gemerkt
        belegt: dict[str, int] = {name: 0 for name in vorrat}
        for other in users.all_users():
            name = str(other.get("server_user") or "")
            if name in belegt and str(other.get("id")) != owner:
                belegt[name] += 1
        gewaehlt = min(vorrat, key=lambda n: (belegt.get(n, 0), n))
        try:
            users.set_server_user(owner, gewaehlt)
        except ValueError:
            return ""
        return gewaehlt


# ---------------------------------------------------------------- Rechte der Ordner

def group_of(user: str) -> int | None:
    """Gruppenkennung eines Server-Benutzers (die gleichnamige Gruppe)."""
    if not _unix() or not user:
        return None
    try:
        return int(grp.getgrnam(str(user)).gr_gid)
    except (KeyError, OSError):
        pass
    try:
        return int(pwd.getpwnam(str(user)).pw_gid)
    except (KeyError, OSError):
        return None


def uid_of(user: str) -> int | None:
    """Benutzerkennung eines Server-Benutzers (für den Abgleich mit ``/proc/<pid>``)."""
    if not _unix() or not user:
        return None
    try:
        return int(pwd.getpwnam(str(user)).pw_uid)
    except (KeyError, OSError):
        return None


def apply_dir(path, user: str, mode: int = DIR_MODE) -> bool:
    """Einen Ordner dem Server-Benutzer als Gruppe geben und den Modus setzen.

    ``chown`` auf eine fremde Gruppe darf nur, wer selbst Mitglied ist – deshalb nimmt deploy.sh
    ``mcsm`` in alle Server-Gruppen auf. Klappt es nicht, bleibt es beim bisherigen Modus.
    """
    if not _unix() or not user:
        return False
    gid = group_of(user)
    if gid is None:
        return False
    try:
        os.chown(str(path), -1, gid)
        os.chmod(str(path), mode)
        return True
    except OSError:
        return False


def secure_manage(folder, user: str) -> bool:
    """Den Verwaltungsordner ``.mcsm`` gegen den Serverprozess abschließen.

    Der Instanzordner hat setgid und den Modus ``2770``; alles, was der Dienst darin anlegt,
    erbt damit die Gruppe des Server-Benutzers und wird für ihn beschreibbar – auch ``.mcsm``.
    Dort liegen aber die Prozesskennung, die Merkdatei für `fix_tree` und ``install.json``
    (Quelle für die Java-Schalter). Deshalb wird ``.mcsm`` hier ausdrücklich auf ``0710``
    gesetzt: der Server-Benutzer darf den Ordner nur **durchlaufen**, um an seine Arbeitsordner
    ``home`` und ``tmp`` zu kommen. Vererben genügt dafür nicht – das muss jeder Start richten.
    """
    if not _unix():
        return False
    manage = os.path.join(str(folder), MANAGE_DIR)
    gid = group_of(user) if user else None
    try:
        os.makedirs(manage, exist_ok=True)
        if gid is not None:
            os.chown(manage, -1, gid)
        os.chmod(manage, MANAGE_MODE if gid is not None else 0o700)
    except OSError:
        return False
    ok = True
    for name in WORK_DIRS:
        ziel = os.path.join(manage, name)
        try:
            os.makedirs(ziel, exist_ok=True)
            if gid is not None:
                os.chown(ziel, -1, gid)
                os.chmod(ziel, WORK_MODE)
            else:
                os.chmod(ziel, 0o700)
        except OSError:
            ok = False
    return ok


def _richte_eintrag(dir_fd: int, name: str, gid: int, eigen: int) -> int | None:
    """Einen Eintrag richten. Rückgabe: Deskriptor eines Ordners zum Absteigen, sonst ``None``.

    Geöffnet wird mit ``O_NOFOLLOW``: eine Verknüpfung lässt sich damit gar nicht erst öffnen.
    Geändert wird nur über den Deskriptor (``fchown``/``fchmod``) – zwischen Prüfung und
    Änderung kann der Name also nicht mehr auf etwas anderes zeigen. ``O_NONBLOCK`` ist
    wichtig, damit ein vom Kunden angelegtes Rohr (FIFO) den Dienst nicht anhält.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                     dir_fd=dir_fd)
    except OSError:
        return None                       # Verknüpfung, fremde 0600-Datei, Gerät … – nichts tun
    ordner = False
    try:
        info = os.fstat(fd)
        ordner = _stat.S_ISDIR(info.st_mode)
        if not ordner and not _stat.S_ISREG(info.st_mode):
            return None                   # nur gewöhnliche Dateien und Ordner
        # Nur eigene Dateien anfassen: was dem Server-Benutzer gehört, darf er selbst richten
        # (`grant_group`), und ein zusätzlicher Verweis auf dieselben Daten (`st_nlink > 1`)
        # könnte auf eine Datei außerhalb des Instanzordners zeigen.
        if info.st_uid != eigen or (not ordner and info.st_nlink != 1):
            return fd if ordner else None
        if info.st_gid != gid:
            os.fchown(fd, -1, gid)
        wanted = (info.st_mode & 0o7777) | (WORK_MODE if ordner else 0o660)
        if wanted != info.st_mode & 0o7777:
            os.fchmod(fd, wanted)
        return fd if ordner else None
    except OSError:
        return fd if ordner else None
    finally:
        if not ordner:
            try:
                os.close(fd)
            except OSError:
                pass


def _richte_baum(dir_fd: int, gid: int, eigen: int, tiefe: int, zaehler: list) -> None:
    """Einen geöffneten Ordner und alles darunter richten (Deskriptor statt Pfad)."""
    if tiefe > FIX_MAX_DEPTH or zaehler[0] >= FIX_MAX_ENTRIES:
        return
    try:
        namen = sorted(os.listdir(dir_fd))
    except OSError:
        return
    for name in namen:
        if zaehler[0] >= FIX_MAX_ENTRIES:
            return
        zaehler[0] += 1
        unter = _richte_eintrag(dir_fd, name, gid, eigen)
        if unter is None:
            continue
        try:
            _richte_baum(unter, gid, eigen, tiefe + 1, zaehler)
        finally:
            try:
                os.close(unter)
            except OSError:
                pass


def fix_tree(folder, user: str) -> int:
    """Einmalig alte Dateien im Instanzordner auf die richtige Gruppe und Rechte bringen.

    Nötig für Ordner, die vor der Umstellung mit ``umask 0077`` angelegt wurden: sie gehören
    ``mcsm`` und sind für die Gruppe gesperrt – der Serverprozess käme nicht an seine eigene
    ``server.jar``. Die Merkdatei sorgt dafür, dass der Durchlauf genau einmal je Gruppe
    passiert und nicht bei jedem Start; sie liegt in ``.mcsm`` und ist damit für den
    Serverprozess unerreichbar (siehe `secure_manage`) – er kann den Durchlauf also nicht
    beliebig oft erzwingen.

    Gelaufen wird über **Deskriptoren**, nicht über Pfade: der Serverbenutzer darf in seinem
    Instanzordner umbenennen und könnte sonst zwischen der Prüfung auf eine Verknüpfung und dem
    ``chown``/``chmod`` eine Verknüpfung nach ``/srv/mcsm/data`` dazwischenschieben – der Dienst
    würde dann fremde Dateien seiner Gruppe geben. ``.mcsm`` selbst bleibt außen vor.
    """
    if not _unix() or not user:
        return 0
    gid = group_of(user)
    if gid is None:
        return 0
    marke = os.path.join(str(folder), *MARKER.split("/"))
    try:
        if open(marke, "r", encoding="utf-8").read().strip() == str(user):
            return 0
    except OSError:
        pass
    try:
        eigen = int(os.getuid())
    except AttributeError:                                # pragma: no cover - Windows
        return 0
    zaehler = [0]
    try:
        wurzel = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return 0
    try:
        for name in sorted(os.listdir(wurzel)):
            if name == MANAGE_DIR:                        # gehört dem Dienst, nicht der Gruppe
                continue
            zaehler[0] += 1
            unter = _richte_eintrag(wurzel, name, gid, eigen)
            if unter is None:
                continue
            try:
                _richte_baum(unter, gid, eigen, 1, zaehler)
            finally:
                try:
                    os.close(unter)
                except OSError:
                    pass
    except OSError:
        pass
    finally:
        try:
            os.close(wurzel)
        except OSError:
            pass
    try:
        os.makedirs(os.path.dirname(marke), exist_ok=True)
        with open(marke, "w", encoding="utf-8") as fh:
            fh.write(str(user))
        os.chmod(marke, 0o600)
    except OSError:
        pass
    return zaehler[0]


# ---------------------------------------------------------------- Befehle unter fremdem Benutzer

def wrap(cmd: list[str], user: str, env: dict | None = None) -> list[str]:
    """Einen Befehl so einpacken, dass er als ``user`` mit genau ``env`` läuft.

    ``env -i`` setzt die Umgebung selbst zusammen – so hängt nichts davon ab, was sudo aus der
    Umgebung durchlässt. Die kleine Schleife über ``sh -c`` setzt nur die Umask und ersetzt sich
    dann durch den Server (``exec``), die Prozesskennung bleibt also die des Servers.
    """
    if not user:
        return list(cmd)
    tool = sudo_path()
    if not tool:
        raise ValueError("Für getrennte Server-Benutzer wird sudo gebraucht, es ist aber nicht "
                         "vorhanden. Bitte deploy.sh auf dem Root-Server erneut ausführen.")
    umgebung = [f"{k}={v}" for k, v in sorted((env or {}).items())]
    return ([tool, "-n", "-u", str(user), "--", "/usr/bin/env", "-i"] + umgebung
            + ["/bin/sh", "-c", f"umask {SERVER_UMASK}; exec \"$0\" \"$@\""] + list(cmd))


def grant_group(folder, user: str) -> bool:
    """Der Gruppe (und damit dem Daemon) Zugriff auf die Dateien des Servers geben.

    Nötig, weil Minecraft manche Dateien über eine Zwischendatei schreibt: ``level.dat`` landet
    dadurch als ``0600`` und gehört dem Server-Benutzer. Der Daemon kann sie dann weder lesen
    (Rückholung auf den PC!) noch selbst umstellen – ``chmod`` darf nur der Eigentümer. Also
    lässt er es den Server-Benutzer selbst tun, über denselben Weg, über den er gestartet wurde.

    ``chmod -Rf`` meldet keine Fehler für Dateien, die dem Daemon gehören; die sind schon richtig.
    """
    if not user:
        return False
    tool = sudo_path()
    chmod = shutil.which("chmod") or "/bin/chmod"
    if not tool:
        return False
    try:
        done = subprocess.run(                                   # noqa: S603 - feste Argumente
            [tool, "-n", "-u", str(user), "--", chmod, "-Rf", "g+rwX", str(folder)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=300)
        return done.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def signal_group(user: str, pgid: int, sig: int) -> bool:
    """Signal an eine Prozessgruppe, die einem Server-Benutzer gehört.

    Der Daemon darf fremden Prozessen kein Signal schicken – das geht nur über denselben Weg
    zurück, über den sie gestartet wurden. Gerufen wird Python statt ``kill``, weil sich die
    negative Prozessgruppenkennung dort nicht mit einer Option verwechseln lässt.
    """
    if not user:
        return False
    tool = sudo_path()
    if not tool:
        return False
    py = sys.executable or "/usr/bin/python3"
    code = "import os,sys;os.killpg(int(sys.argv[1]),int(sys.argv[2]))"
    try:
        done = subprocess.run(                                   # noqa: S603 - feste Argumente
            [tool, "-n", "-u", str(user), "--", py, "-c", code, str(int(pgid)), str(int(sig))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15)
        return done.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
