"""Sichere Pfade innerhalb eines Instanzordners und die Freigabe-Liste für Benutzerzugriffe.

Der Daemon steht im Internet: jeder Pfad, der von außen kommt, läuft zuerst durch dieses Modul.
Verboten sind absolute Pfade, `..`, Laufwerksbuchstaben, Steuerzeichen und Verknüpfungen
(Symlinks), die aus dem Instanzordner hinausführen – geprüft mit ``os.path.realpath``.

Zwei Strenge-Stufen:

* ``check_transfer`` – für Übertragungen (Upload/Download ganzer Instanzen). Erlaubt auch
  technische Dateien (server.jar, libraries …), weil eine Instanz vollständig umzieht.
* ``check_user_read`` / ``check_user_write`` / ``check_user_delete`` – für die Dateiverwaltung
  im Programm. Hier gilt die Freigabe-Liste: Welten, Plugins/Mods, Konfiguration, Protokolle,
  Sicherungen und Einstellungen darf der Benutzer sehen und ändern, die Servertechnik nicht.

Alle Fehler sind ``ValueError`` mit einem verständlichen deutschen Satz – der Daemon gibt sie
unverändert als ``{"error": ...}`` heraus.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import pathlib
import stat as _stat

# ---------------------------------------------------------------- Grenzen

MAX_REL_LEN = 1024          # Länge des gesamten relativen Pfads
MAX_NAME_LEN = 200          # Länge eines einzelnen Namens (Linux erlaubt 255 Bytes)
MAX_DEPTH = 32              # so tief verschachtelt ist keine Instanz mehr gesund
MAX_EDIT_BYTES = 1_500_000  # größere Textdateien nicht im Editor öffnen

# ---------------------------------------------------------------- Namensregeln

# Zeichen, die Windows in Dateinamen nicht zulässt. Der Ordner muss am Ende wieder auf den PC
# zurückkommen – ein auf dem Root angelegter Name mit „?“ oder „:“ ließe sich dort nicht speichern.
_WINDOWS_BAD_CHARS = set('<>:"|?*\\')
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "com0", "com1", "com2", "com3", "com4", "com5",
                     "com6", "com7", "com8", "com9", "lpt0", "lpt1", "lpt2", "lpt3", "lpt4",
                     "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"}

# ---------------------------------------------------------------- Freigabe-Liste

#: Dateiendungen, die als Text gelten (lesen und im Editor bearbeiten).
#: (Skripte fehlen bewusst: im Serverordner soll nichts Ausführbares neu entstehen.)
TEXT_EXT = {".properties", ".yml", ".yaml", ".json", ".txt", ".toml", ".cfg", ".ini", ".conf",
            ".log", ".md", ".mcmeta", ".lang", ".csv", ".wlist"}

#: Ordner im Instanzordner, die dem Benutzer gehören (oberste Ebene).
USER_DIRS = {"plugins", "mods", "config", "datapacks", "logs", "backups", "worlds",
             "behavior_packs", "resource_packs", "world", "world_nether", "world_the_end"}

#: Dateien im Instanzordner, die dem Benutzer gehören (oberste Ebene).
USER_FILES = {"server.properties", "eula.txt", "whitelist.json", "allowlist.json", "ops.json",
              "permissions.json", "banned-players.json", "banned-ips.json", "bukkit.yml",
              "spigot.yml", "paper.yml", "paper-global.yml", "paper-world-defaults.yml",
              "commands.yml", "help.yml", "server-icon.png", "banned_ips.json",
              "known_valid_packs.json", "server.properties.bak"}

#: Verwaltungsdateien des Daemons: nie anzeigen, nie ändern, nie übertragen.
RESERVED_NAMES = {".mcsm", "mcsm.json", "mcsm-instanz.json"}

#: Endung der Teil-Dateien einer laufenden Übertragung (unsichtbar und gesperrt).
PART_SUFFIX = ".mcsmpart"

#: Servertechnik: wird übertragen, aber der Benutzer darf sie nicht bearbeiten oder löschen.
TECH_NAMES = {"libraries", "versions", "cache", ".paper-remapped", "bundler", "server.jar",
              "build.txt", "definitions", "minecraftpe", "treatments", "world_templates",
              "internalStorage", "development_behavior_packs", "development_resource_packs",
              "development_skin_packs", "bedrock_server", "bedrock_server.exe",
              "bedrock_server_how_to.html", "release-notes.txt", "profanity_filter.wlist",
              "valid_known_packs.json", "Dedicated_Server.txt", "version_history.json",
              # Technik der Mod-Loader (wie modpacks.TECH_FILES im lokalen Programm)
              "user_jvm_args.txt", "run.bat", "run.sh", "unix_args.txt",
              "fabric-server-launcher.properties", "quilt-server-launcher.properties",
              "quilt-server-launch.jar", "modpack-index.json", ".fabric", ".quilt",
              "installer.log", "mods-unsupported"}

#: Endungen, die in der Dateiliste zugeklappt bleiben (technisch, nicht interessant).
HIDDEN_EXT = {".exe", ".dll", ".pdb", ".lock", ".dat_old", ".bak", ".tmp", PART_SUFFIX}

#: Namen, die in der Dateiliste zugeklappt bleiben.
HIDDEN_NAMES = {"session.lock", "uid.dat", ".console_history", "logs.old", "usercache.json"}

_WORLD_DIRS = {"worlds", "world", "world_nether", "world_the_end"}


# ---------------------------------------------------------------- Namen und relative Pfade

def check_name(name: str) -> str:
    """Prüft einen einzelnen Datei- oder Ordnernamen (ohne Trennzeichen)."""
    if not name or name in (".", ".."):
        raise ValueError("Ungültiger Name.")
    if "/" in name or "\\" in name:
        raise ValueError("Ein Name darf kein Trennzeichen enthalten.")
    if len(name.encode("utf-8")) > MAX_NAME_LEN:
        raise ValueError(f"Der Name ist zu lang (höchstens {MAX_NAME_LEN} Zeichen).")
    for ch in name:
        if ord(ch) < 32 or ord(ch) == 127:
            raise ValueError("Der Name enthält Steuerzeichen.")
        if ch in _WINDOWS_BAD_CHARS:
            raise ValueError(f"Das Zeichen „{ch}“ ist in Namen nicht erlaubt, "
                             "weil die Datei später auf dem PC gespeichert werden muss.")
    if name != name.strip() or name.endswith("."):
        raise ValueError("Der Name darf nicht mit Leerzeichen oder einem Punkt enden.")
    if name.split(".")[0].lower() in _WINDOWS_RESERVED:
        raise ValueError(f"„{name}“ ist ein von Windows belegter Name und kann nicht verwendet werden.")
    return name


def normalize(rel: str) -> str:
    """Bringt einen relativen Pfad auf die Form ``ordner/datei`` (leer = Instanzordner selbst)."""
    if rel is None:
        return ""
    if not isinstance(rel, str):
        raise ValueError("Ungültiger Pfad.")
    if "\x00" in rel:
        raise ValueError("Ungültiger Pfad.")
    if len(rel) > MAX_REL_LEN:
        raise ValueError("Der Pfad ist zu lang.")
    # Nur die Trennzeichen vereinheitlichen. Ein `strip()` über den **ganzen** Pfad würde
    # „world/level.dat “ klammheimlich zu „world/level.dat“ machen – der Eintrag zeigte dann auf
    # eine andere Datei. Leerzeichen am Rand eines Namens lehnt `check_name` weiter unten ab.
    text = rel.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if text.startswith("/"):
        raise ValueError("Es sind nur Pfade innerhalb des Serverordners erlaubt.")
    text = text.strip("/")
    if text in ("", "."):
        return ""
    parts = [p for p in text.split("/") if p != "."]
    if not parts:
        return ""
    if len(parts) > MAX_DEPTH:
        raise ValueError("Der Pfad ist zu tief verschachtelt.")
    for part in parts:
        if part == "..":
            raise ValueError("Es sind nur Pfade innerhalb des Serverordners erlaubt.")
        check_name(part)
    return "/".join(parts)


def parts(rel: str) -> list[str]:
    """Die Namensbestandteile eines geprüften relativen Pfads."""
    norm = normalize(rel)
    return norm.split("/") if norm else []


# ---------------------------------------------------------------- Auflösen

def _real(path) -> str:
    return os.path.realpath(str(path))


def resolve(root, rel: str, *, allow_symlinks: bool = False) -> pathlib.Path:
    """Löst ``rel`` innerhalb von ``root`` auf; ein Ausbruch ist nicht möglich.

    Geprüft wird dreifach: die Namensregeln (kein ``..``, kein absoluter Pfad), jeder vorhandene
    Zwischenschritt auf Verknüpfungen und zum Schluss der echte Pfad (``realpath``) gegen den
    echten Wurzelpfad. Der Pfad muss noch nicht existieren (Ziel eines Uploads).
    """
    real_root = _real(root)
    norm = normalize(rel)
    if not norm:
        return pathlib.Path(real_root)
    target = os.path.join(real_root, *norm.split("/"))
    if not allow_symlinks:
        # Jeden vorhandenen Schritt einzeln ansehen: eine Verknüpfung mitten im Pfad wird sonst
        # von realpath aufgelöst und fällt nur auf, wenn sie zufällig hinausführt.
        walk = real_root
        for part in norm.split("/"):
            walk = os.path.join(walk, part)
            if os.path.islink(walk):
                raise ValueError("Verknüpfungen (Symlinks) sind im Serverordner nicht erlaubt.")
    real_target = _real(target)
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        raise ValueError("Es sind nur Pfade innerhalb des Serverordners erlaubt.")
    return pathlib.Path(target)


# ---------------------------------------------------------------- Öffnen ohne Zeitfenster

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_BINARY = getattr(os, "O_BINARY", 0)


def dirfd_moeglich() -> bool:
    """Kann dieses Betriebssystem Dateien über einen Ordner-Deskriptor öffnen? (Linux: ja)"""
    return bool(_NOFOLLOW) and os.open in getattr(os, "supports_dir_fd", set())


def _symlink_fehler(exc: OSError) -> bool:
    return exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1))


_SYMLINK_TEXT = "Verknüpfungen (Symlinks) sind im Serverordner nicht erlaubt."


@contextlib.contextmanager
def ordner_griff(root, rel: str):
    """Den Elternordner von ``rel`` Schritt für Schritt öffnen und als Deskriptor herausgeben.

    Warum nicht einfach ``resolve``: ``resolve`` prüft jeden Schritt auf Verknüpfungen und gibt
    danach einen **Pfad** zurück; geöffnet wird erst vom Aufrufer. Dazwischen darf der
    Serverbenutzer in seinem Instanzordner umbenennen – ein hochgeladenes Plugin kann in genau
    diesem Augenblick eine Verknüpfung auf ``/srv/mcsm/data/users.json`` dazwischenschieben, und
    der Daemon (der die Datei lesen darf) liefert sie aus. Über Deskriptoren gibt es dieses
    Zeitfenster nicht: was hier offen ist, bleibt dieselbe Datei, auch wenn der Name inzwischen
    auf etwas anderes zeigt.

    Rückgabe ``(fd, name)``: ``fd`` ist der Elternordner, ``name`` der letzte Namensteil. Auf
    Betriebssystemen ohne ``dir_fd`` (Windows im Selbsttest) ist ``fd`` ``None`` und ``name`` der
    vollständige Pfad – dort gibt es die getrennten Systembenutzer ohnehin nicht.
    """
    norm = normalize(rel)
    if not norm:
        raise ValueError("Es fehlt der Dateiname.")
    if not dirfd_moeglich():                       # pragma: no cover - Windows im Selbsttest
        yield None, str(resolve(root, norm))
        return
    stuecke = norm.split("/")
    offen: list[int] = []
    try:
        offen.append(os.open(_real(root), os.O_RDONLY | _DIRECTORY))
        for teil in stuecke[:-1]:
            try:
                offen.append(os.open(teil, os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                                     dir_fd=offen[-1]))
            except OSError as exc:
                if _symlink_fehler(exc):
                    raise ValueError(_SYMLINK_TEXT) from exc
                raise
        yield offen[-1], stuecke[-1]
    finally:
        for handle in reversed(offen):
            try:
                os.close(handle)
            except OSError:
                pass


def oeffnen(root, rel: str, flags: int, mode: int = 0o660) -> int:
    """Eine Datei im Instanzordner öffnen, ohne einem Symlink zu folgen. Rückgabe: Deskriptor."""
    with ordner_griff(root, rel) as (fd, name):
        try:
            if fd is None:                         # pragma: no cover - Windows im Selbsttest
                if os.path.islink(name):
                    raise ValueError(_SYMLINK_TEXT)
                return os.open(name, flags | _NOFOLLOW | _BINARY, mode)
            return os.open(name, flags | _NOFOLLOW | _BINARY, mode, dir_fd=fd)
        except OSError as exc:
            if _symlink_fehler(exc):
                raise ValueError(_SYMLINK_TEXT) from exc
            raise


def lies_stueck(root, rel: str, offset: int = 0, length: int = -1) -> bytes:
    """Ein Stück einer Datei im Instanzordner lesen (ohne Umweg über einen zweiten Pfadzugriff)."""
    fd = oeffnen(root, rel, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise ValueError(f"„{normalize(rel)}“ ist keine gewöhnliche Datei.")
        if offset:
            os.lseek(fd, int(offset), os.SEEK_SET)
        rest = int(length) if length is not None and int(length) >= 0 else max(
            0, info.st_size - int(offset))
        teile: list[bytes] = []
        while rest > 0:
            block = os.read(fd, min(1048576, rest))
            if not block:
                break
            teile.append(block)
            rest -= len(block)
        return b"".join(teile)
    finally:
        os.close(fd)


def pruefsumme(root, rel: str) -> str:
    """SHA256 einer Datei im Instanzordner – über denselben sicheren Weg wie ``lies_stueck``."""
    digest = hashlib.sha256()
    fd = oeffnen(root, rel, os.O_RDONLY)
    try:
        while True:
            block = os.read(fd, 1048576)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def groesse(root, rel: str) -> int:
    """Größe einer Datei im Instanzordner; ``-1``, wenn es sie nicht (mehr) gibt."""
    try:
        fd = oeffnen(root, rel, os.O_RDONLY)
    except (OSError, ValueError):
        return -1
    try:
        info = os.fstat(fd)
        return int(info.st_size) if _stat.S_ISREG(info.st_mode) else -1
    finally:
        os.close(fd)


def anhaengen(root, rel: str, data: bytes, *, mode: int = 0o660) -> int:
    """Daten an eine Datei im Instanzordner anhängen. Rückgabe: Größe danach."""
    fd = oeffnen(root, rel, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise ValueError(f"„{normalize(rel)}“ ist keine gewöhnliche Datei.")
        os.write(fd, bytes(data))
        try:
            os.fsync(fd)
        except OSError:
            pass
        return int(os.fstat(fd).st_size)
    finally:
        os.close(fd)


def entfernen(root, rel: str) -> bool:
    """Eine Datei im Instanzordner löschen, ohne einem Symlink zu folgen."""
    with ordner_griff(root, rel) as (fd, name):
        try:
            if fd is None:                         # pragma: no cover - Windows im Selbsttest
                os.unlink(name)
            else:
                os.unlink(name, dir_fd=fd)
            return True
        except FileNotFoundError:
            return False


def ersetzen(root, quelle_rel: str, ziel_rel: str) -> None:
    """``quelle`` im Instanzordner auf ``ziel`` umbenennen – beide Seiten über Deskriptoren.

    ``rename`` folgt zwar selbst keiner Verknüpfung am Ziel, wohl aber einer im **Ordnerteil**
    des Pfads. Deshalb kommen auch hier die Elternordner als Deskriptor.
    """
    with ordner_griff(root, quelle_rel) as (qfd, qname):
        with ordner_griff(root, ziel_rel) as (zfd, zname):
            if qfd is None or zfd is None:         # pragma: no cover - Windows im Selbsttest
                os.replace(qname, zname)
                return
            os.replace(qname, zname, src_dir_fd=qfd, dst_dir_fd=zfd)


def schreibe_atomar(root, rel: str, data: bytes, *, mode: int = 0o660) -> None:
    """Eine Datei im Instanzordner atomar ersetzen (Zwischendatei im selben Ordner + rename)."""
    norm = normalize(rel)
    tmp = norm + ".mcsmtmp"
    fd = oeffnen(root, tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, bytes(data))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    try:
        ersetzen(root, tmp, norm)
    except BaseException:
        try:
            entfernen(root, tmp)
        except (OSError, ValueError):
            pass
        raise


def relative(root, path) -> str:
    """Gibt ``path`` als geprüften relativen Pfad zum Instanzordner zurück (posix-Schreibweise)."""
    real_root = _real(root)
    real_path = _real(path)
    if real_path == real_root:
        return ""
    if not real_path.startswith(real_root + os.sep):
        raise ValueError("Der Pfad liegt außerhalb des Serverordners.")
    return real_path[len(real_root) + 1:].replace(os.sep, "/")


def inside(root, path) -> bool:
    """Liegt ``path`` (nach Auflösen aller Verknüpfungen) innerhalb von ``root``?"""
    real_root = _real(root)
    real_path = _real(path)
    return real_path == real_root or real_path.startswith(real_root + os.sep)


# ---------------------------------------------------------------- Einordnung und Freigabe

def classify(rel: str, is_dir: bool = False) -> str:
    """Grobe Art eines Eintrags: ``world``, ``plugins``, ``plugin``, ``config``, ``log``,
    ``backup``, ``folder``, ``tech`` oder ``other`` – für Anzeige und Freigabe."""
    piece = parts(rel)
    if not piece:
        return "folder"
    name = piece[-1]
    lower = name.lower()
    top = piece[0].lower()
    if name in RESERVED_NAMES or piece[0] in RESERVED_NAMES:
        return "reserved"
    if lower.endswith(PART_SUFFIX):
        return "reserved"
    if piece[0] in TECH_NAMES:
        return "tech"
    if is_dir:
        if lower in _WORLD_DIRS or top in _WORLD_DIRS:
            return "world"
        if lower in ("plugins", "mods"):
            return "plugins"
        if lower in ("config", "datapacks", "behavior_packs", "resource_packs"):
            return "config"
        if lower == "backups":
            return "backup"
        if lower == "logs":
            return "log"
        return "folder"
    ext = ("." + lower.rsplit(".", 1)[1]) if "." in lower else ""
    if top in _WORLD_DIRS:
        return "world"
    if lower.endswith(".log.gz") or ext == ".log" or top == "logs":
        return "log"
    if ext == ".zip" and top == "backups":
        return "backup"
    if ext == ".jar" and top in ("plugins", "mods"):
        return "plugin"
    if ext == ".jar":
        return "tech"
    if ext in TEXT_EXT or ext in (".png", ".properties"):
        return "config"
    return "other"


def is_hidden(rel: str, kind: str | None = None) -> bool:
    """Bleibt der Eintrag in der Dateiliste zugeklappt (Technik, nichts zum Anfassen)?"""
    piece = parts(rel)
    if not piece:
        return False
    name = piece[-1]
    art = kind or classify(rel)
    if art in ("reserved", "tech"):
        return True
    if art in ("world", "plugins", "plugin", "config", "backup", "log"):
        return False
    lower = name.lower()
    ext = ("." + lower.rsplit(".", 1)[1]) if "." in lower else ""
    return name in HIDDEN_NAMES or ext in HIDDEN_EXT or name.startswith(".")


def is_text(rel: str) -> bool:
    """Darf diese Datei im Editor angezeigt werden (nach Endung)?"""
    piece = parts(rel)
    if not piece:
        return False
    lower = piece[-1].lower()
    ext = ("." + lower.rsplit(".", 1)[1]) if "." in lower else ""
    return ext in TEXT_EXT


def check_transfer(rel: str) -> str:
    """Pfadprüfung für Übertragungen: alles außer Verwaltungsdateien und Teil-Dateien."""
    norm = normalize(rel)
    if not norm:
        raise ValueError("Es fehlt der Dateiname.")
    if classify(norm) == "reserved":
        raise ValueError(f"„{norm}“ gehört zur Verwaltung des Servers und wird nicht übertragen.")
    return norm


def check_user_read(rel: str) -> str:
    """Pfadprüfung für Anzeigen und Lesen: alles außer Verwaltungsdateien."""
    norm = normalize(rel)
    if norm and classify(norm) == "reserved":
        raise ValueError("Diese Datei gehört zur Verwaltung des Servers und ist nicht sichtbar.")
    return norm


def check_user_write(rel: str) -> str:
    """Pfadprüfung für Anlegen, Hochladen und Speichern durch den Benutzer."""
    norm = normalize(rel)
    if not norm:
        raise ValueError("Es fehlt der Dateiname.")
    art = classify(norm)
    if art == "reserved":
        raise ValueError("Diese Datei gehört zur Verwaltung des Servers und kann nicht geändert werden.")
    if art == "tech":
        raise ValueError("Diese Datei gehört zur Servertechnik und wird vom Programm verwaltet.")
    piece = norm.split("/")
    if len(piece) == 1 and piece[0] not in USER_FILES and piece[0] not in USER_DIRS:
        # Neue Dateien direkt im Serverordner sind die häufigste Verwechslung (Plugin ins
        # Wurzelverzeichnis geschoben) – deshalb ein Satz, der den richtigen Ort nennt.
        if is_text(norm):
            return norm
        raise ValueError("Bitte im passenden Ordner ablegen: Plugins nach „plugins“, "
                         "Mods nach „mods“, Einstellungen nach „config“.")
    return norm


def check_user_delete(rel: str) -> str:
    """Pfadprüfung fürs Löschen durch den Benutzer (Welten und Plugins dürfen weg)."""
    norm = normalize(rel)
    if not norm:
        raise ValueError("Der Serverordner selbst kann nicht gelöscht werden.")
    art = classify(norm)
    if art == "reserved":
        raise ValueError("Diese Datei gehört zur Verwaltung des Servers und kann nicht gelöscht werden.")
    if art == "tech":
        raise ValueError("Diese Datei gehört zur Servertechnik und kann nicht gelöscht werden.")
    if norm == "server.properties":
        raise ValueError("Die Servereinstellungen können nicht gelöscht werden.")
    return norm


# ---------------------------------------------------------------- Auflisten

def entry_info(root, rel: str) -> dict:
    """Angaben zu einem Eintrag: Art, Größe, Änderungszeit, sichtbar, bearbeitbar."""
    norm = check_user_read(rel)
    target = resolve(root, norm)
    st = target.stat()               # OSError trägt der Aufrufer
    is_dir = target.is_dir()
    kind = classify(norm, is_dir)
    return {
        "name": target.name,
        "path": norm,
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": int(st.st_mtime),
        "kind": kind,
        "hidden": is_hidden(norm, kind),
        "editable": (not is_dir) and is_text(norm) and st.st_size <= MAX_EDIT_BYTES,
    }


def list_dir(root, rel: str = "") -> dict:
    """Inhalt eines Ordners im Instanzordner – Verwaltungsdateien kommen nicht vor."""
    norm = check_user_read(rel)
    target = resolve(root, norm)
    if not target.is_dir():
        raise ValueError("Ordner nicht gefunden.")
    entries: list[dict] = []
    try:
        names = sorted(os.listdir(target), key=lambda n: n.lower())
    except OSError as exc:
        raise ValueError("Der Ordner kann nicht gelesen werden.") from exc
    for name in names:
        child = f"{norm}/{name}" if norm else name
        try:
            if classify(child) == "reserved":
                continue
            entries.append(entry_info(root, child))
        except (ValueError, OSError):
            continue                 # unlesbare oder nicht erlaubte Einträge auslassen
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"path": norm, "entries": entries}


def walk_files(root, rel: str = "", *, skip_reserved: bool = True, skipped: list | None = None):
    """Alle Dateien unter ``rel`` als relative Pfade (keine Verknüpfungen, sortiert).

    Verknüpfungen werden übersprungen statt zu einem Fehler zu führen: beim Einlesen eines
    ganzen Instanzordners soll eine einzelne Merkwürdigkeit die Übertragung nicht verhindern.

    Wird ``skipped`` (eine Liste) mitgegeben, landen dort alle ausgelassenen Einträge als
    ``{"path": …, "reason": …}``. Der Daemon braucht das vor dem Löschen eines Ordners: was nicht
    im Manifest steht, hat der PC nie heruntergeladen und darf deshalb nicht ungesehen weg.
    """
    def note(pfad: str, grund: str) -> None:
        if skipped is not None and len(skipped) < 200:
            skipped.append({"path": str(pfad), "reason": str(grund)})

    start = resolve(root, rel)
    base = normalize(rel)
    folders = [(base, str(start))]
    while folders:
        current, folder = folders.pop(0)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        subfolders: list[tuple[str, str]] = []
        for name in names:
            child = f"{current}/{name}" if current else name
            full = os.path.join(folder, name)
            if "\\" in name:
                # Linux erlaubt den Rückstrich im Namen, der PC nicht – normalize würde ihn als
                # Trennzeichen lesen und auf eine andere Datei zeigen. Solche Namen bleiben außen vor.
                note(child, "Der Name enthält einen Rückstrich, den der PC nicht speichern kann.")
                continue
            if os.path.islink(full):
                note(child, "Der Eintrag ist eine Verknüpfung (Symlink).")
                continue
            try:
                norm = normalize(child)
            except ValueError as exc:
                note(child, str(exc))
                continue
            if skip_reserved and classify(norm) == "reserved":
                continue
            if os.path.isdir(full):
                subfolders.append((norm, full))
            elif os.path.isfile(full):
                yield norm
        folders = subfolders + folders
