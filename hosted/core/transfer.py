"""Übertragung von Dateien und ganzen Serverordnern zwischen PC und Root-Server.

Eine Übertragung läuft in drei Schritten, damit auch eine 3-GB-Welt durchgeht und ein Abbruch
nicht alles wegwirft:

1. **beginnen** – der PC schickt ein Manifest (jede Datei mit Größe und SHA256). Der Daemon prüft
   den freien Platz, erkennt schon vorhandene, prüfsummengleiche Dateien und legt eine Sitzung an.
2. **Stücke** – je Datei werden Stücke von 8 MB angehängt. Jedes Stück nennt seinen Versatz; passt
   er nicht, sagt die Fehlermeldung, ab welchem Byte es weitergehen muss (Wiederaufnahme).
   Ist eine Datei vollständig, wird ihre Prüfsumme geprüft und die Teil-Datei umbenannt.
3. **abschließen** – alle Prüfsummen werden noch einmal gegen das Manifest verglichen.

Der Download läuft spiegelbildlich: dieselbe Sitzungslogik, nur holt die empfangende Seite die
Stücke über eine Rückruffunktion (``DownloadSession.run``).

Für die Rückholung („awaiting_pull“): ``verify(ordner, manifest)`` bestätigt, dass lokal alles
vollständig angekommen ist. Erst nach dieser Bestätigung darf ``delete_tree`` den Ordner auf dem
Root-Server löschen.

Alle Fehler sind ``ValueError`` mit einem verständlichen deutschen Satz.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import re
import secrets
import shutil
import tarfile
import threading
import time

from . import paths

# ---------------------------------------------------------------- Grenzen und Namen

CHUNK_SIZE = 8 * 1024 * 1024            # Standardgröße eines Stücks
MAX_CHUNK_BYTES = 16 * 1024 * 1024      # größere Stücke werden abgelehnt (Speicherschutz)
HASH_BLOCK = 1024 * 1024                # Blockgröße beim Prüfsummenlesen
MAX_FILES = 200_000                     # so viele Dateien hat keine gesunde Instanz
MAX_FILE_BYTES = 6 * 1024 ** 3          # Obergrenze für eine einzelne Datei
MAX_TOTAL_BYTES = 20 * 1024 ** 3        # Obergrenze für eine Übertragung (Platte hat 30 GB)
RESERVE_BYTES = 3 * 1024 ** 3           # so viel Platte bleibt immer frei (siehe ARCHITEKTUR.md)
SESSION_MAX_AGE = 7 * 24 * 3600         # ältere Sitzungen werden aufgeräumt
MAX_OPEN_SESSIONS_PER_USER = 8          # so viele offene Übertragungen darf ein Konto haben
MAX_CACHED_SESSIONS = 64                # so viele Sitzungen bleiben im Arbeitsspeicher (LRU)
SPACE_CHECK_EVERY = 8                   # alle so viele Stücke wird der freie Platz nachgeprüft

# Pakete: 92 % der Dateien einer Instanz sind kleiner als 1 MB. Einzeln kostet jede von ihnen
# eine eigene Anfrage – bei einer Leitung mit 20 ms Laufzeit mehr als ihr Inhalt. Sie gehen
# deshalb gebündelt als tar-Strom in einer Anfrage über die Leitung und werden hier ausgepackt
# (dieselben Pfadprüfungen wie bei einem einzelnen Stück, Größe und Prüfsumme je Datei).
BUNDLE_SMALL_BYTES = 1024 * 1024        # bis hierher gilt eine Datei als „klein“
BUNDLE_MAX_BYTES = 24 * 1024 * 1024     # so groß darf ein Paket höchstens sein (Arbeitsspeicher)
BUNDLE_MAX_FILES = 400                  # so viele Dateien stecken höchstens in einem Paket

MANIFEST_VERSION = 1
SESSION_FILE = "session.json"
PART_SUFFIX = paths.PART_SUFFIX
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_REPORT_CAP = 50                        # so viele Namen nennt ein Bericht höchstens


# ---------------------------------------------------------------- Kleinigkeiten

def new_id() -> str:
    """Zufällige Sitzungskennung (32 Hexzeichen)."""
    return secrets.token_hex(16)


def check_id(value) -> str:
    """Prüft eine Sitzungskennung, bevor sie zu einem Pfad wird."""
    text = str(value or "").strip().lower()
    if not _ID_RE.match(text):
        raise ValueError("Ungültige Kennung der Übertragung.")
    return text


def sha256_file(path, *, block: int = HASH_BLOCK) -> str:
    """SHA256 einer Datei als Hexzeichenkette (blockweise, auch bei 3 GB)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(block)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """SHA256 eines Speicherinhalts als Hexzeichenkette."""
    return hashlib.sha256(data).hexdigest()


def _cap(names: list[str]) -> list[str]:
    return names[:_REPORT_CAP]


def _now() -> int:
    return int(time.time())


def _fsync_dir(path: pathlib.Path) -> None:
    """Verzeichniseintrag auf die Platte zwingen – sonst kann ``os.replace`` verloren gehen."""
    if not hasattr(os, "O_DIRECTORY"):                 # pragma: no cover - Windows kennt das nicht
        return
    try:
        fd = os.open(str(path), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_json(path: pathlib.Path, data: dict) -> None:
    """Atomar schreiben (.tmp + Umbenennen) – ein Abbruch darf keine halbe Datei hinterlassen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _read_json(path: pathlib.Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError("Diese Übertragung ist nicht (mehr) bekannt.") from exc
    except ValueError as exc:
        raise ValueError("Die Daten der Übertragung sind beschädigt.") from exc
    if not isinstance(data, dict):
        raise ValueError("Die Daten der Übertragung sind beschädigt.")
    return data


# ---------------------------------------------------------------- Platz

def dir_size(folder) -> int:
    """Summe der Dateigrößen unter ``folder`` in Bytes (Verknüpfungen zählen nicht)."""
    return dir_stats(folder)["bytes"]


def dir_stats(folder) -> dict:
    """Größe und Anzahl der Dateien unter ``folder``: ``{"bytes": int, "files": int}``."""
    total = 0
    count = 0
    root = str(folder)
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in names:
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            try:
                total += os.path.getsize(full)
                count += 1
            except OSError:
                continue
    return {"bytes": total, "files": count}


def free_space(path) -> int:
    """Freier Platz auf dem Datenträger von ``path`` (der nächste vorhandene Ordner zählt)."""
    probe = pathlib.Path(str(path)).absolute()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free
    except OSError as exc:
        raise ValueError("Der freie Platz auf der Platte lässt sich nicht bestimmen.") from exc


def check_space(path, needed: int, *, reserve: int = RESERVE_BYTES) -> int:
    """Prüft **vor** einer Übertragung, ob ``needed`` Bytes plus Reserve frei sind."""
    needed = max(0, int(needed))
    free = free_space(path)
    if free - needed < reserve:
        raise ValueError(
            f"Auf dem Server ist zu wenig Platz frei: benötigt werden {human(needed)}, "
            f"frei sind {human(free)}, und {human(reserve)} müssen als Reserve frei bleiben.")
    return free


def human(num: int) -> str:
    """Bytes als kurze deutsche Angabe, z. B. „2,4 GB“."""
    value = float(max(0, int(num)))
    for unit in ("Bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "Bytes":
                return f"{int(value)} Bytes"
            return f"{value:.1f}".replace(".", ",") + f" {unit}"
        value /= 1024
    return f"{int(value)} Bytes"


# ---------------------------------------------------------------- Manifest

def build_manifest(folder, *, rel: str = "", hashes: bool = True,
                   nachladbar_auslassen: bool = True) -> dict:
    """Liest einen Ordner ein und baut das Manifest (jede Datei mit Größe und SHA256).

    Bei ``hashes=False`` bleiben die Prüfsummen leer – nur für schnelle Übersichten, für eine
    Übertragung oder eine Bestätigung braucht es die echten Prüfsummen.

    ``nachladbar_auslassen`` (Standard **an**) lässt weg, was der Root-Server selbst beschaffen
    kann: ``libraries``, ``versions``, ``cache``, ``logs`` und den Serverkern
    (``paths.nachladbar``). Das gilt mit Absicht auch für das Manifest der **Rückholung** – so
    sehen beide Seiten dieselbe Liste, und was nie hochgeladen wurde, gilt beim Zurückholen
    nicht als fehlend.
    """
    root = pathlib.Path(str(folder))
    if not root.is_dir():
        raise ValueError("Der Serverordner ist nicht vorhanden.")
    files: list[dict] = []
    skipped: list[dict] = []
    total = 0
    for name in paths.walk_files(root, rel, skipped=skipped,
                                 nachladbar_auslassen=nachladbar_auslassen):
        full = paths.resolve(root, name)
        try:
            size = full.stat().st_size
            mtime = int(full.stat().st_mtime)
        except OSError as exc:
            skipped.append({"path": name, "reason": f"Die Datei lässt sich nicht lesen: {exc}"})
            continue
        if size > MAX_FILE_BYTES:
            raise ValueError(f"Die Datei „{name}“ ist mit {human(size)} zu groß für eine Übertragung.")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"Der Serverordner ist mit über {human(MAX_TOTAL_BYTES)} zu groß "
                             "für eine Übertragung.")
        if len(files) >= MAX_FILES:
            raise ValueError(f"Der Serverordner hat mehr als {MAX_FILES} Dateien – "
                             "bitte zuerst alte Sicherungen löschen.")
        entry = {"path": name, "size": size, "mtime": mtime}
        entry["sha256"] = sha256_file(full) if (hashes and size) else (EMPTY_SHA256 if not size else "")
        files.append(entry)
    files.sort(key=lambda e: e["path"])
    skipped.sort(key=lambda e: e["path"])
    # „skipped“ gehört nicht zur Übertragung, sondern ist die Warnliste für den Daemon:
    # Diese Einträge kann der PC nicht bekommen, also darf der Ordner nicht gelöscht werden.
    return {"version": MANIFEST_VERSION, "created_at": _now(), "file_count": len(files),
            "total_bytes": total, "files": files, "skipped": skipped}


def check_manifest(obj) -> dict:
    """Prüft ein von außen gekommenes Manifest streng und gibt eine aufgeräumte Kopie zurück."""
    if not isinstance(obj, dict):
        raise ValueError("Das Manifest fehlt oder hat das falsche Format.")
    raw = obj.get("files")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Das Manifest enthält keine Dateien.")
    if len(raw) > MAX_FILES:
        raise ValueError(f"Das Manifest enthält mehr als {MAX_FILES} Dateien.")
    files: list[dict] = []
    seen: set[str] = set()
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Ein Eintrag im Manifest hat das falsche Format.")
        name = paths.check_transfer(item.get("path"))
        lower = name.lower()
        if lower in seen:
            raise ValueError(f"Die Datei „{name}“ steht mehrfach im Manifest.")
        seen.add(lower)
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"Die Größe von „{name}“ fehlt oder ist ungültig.")
        if size > MAX_FILE_BYTES:
            raise ValueError(f"Die Datei „{name}“ ist mit {human(size)} zu groß für eine Übertragung.")
        sha = str(item.get("sha256") or "").strip().lower()
        if size == 0 and not sha:
            sha = EMPTY_SHA256
        if not _SHA_RE.match(sha):
            raise ValueError(f"Die Prüfsumme von „{name}“ fehlt oder ist ungültig.")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"Die Übertragung ist mit über {human(MAX_TOTAL_BYTES)} zu groß.")
        mtime = item.get("mtime")
        entry = {"path": name, "size": size, "sha256": sha}
        if isinstance(mtime, int) and not isinstance(mtime, bool) and 0 <= mtime <= 4_000_000_000:
            entry["mtime"] = mtime
        files.append(entry)
    files.sort(key=lambda e: e["path"])
    return {"version": MANIFEST_VERSION, "created_at": _now(), "file_count": len(files),
            "total_bytes": total, "files": files}


def manifest_index(manifest: dict) -> dict:
    """Manifest als Zuordnung Pfad → Eintrag."""
    return {entry["path"]: entry for entry in manifest.get("files", [])}


def compare_manifests(expected: dict, actual: dict) -> dict:
    """Vergleicht zwei Manifeste (z. B. Root gegen PC) ohne die Platte anzufassen."""
    want = manifest_index(expected)
    have = manifest_index(actual)
    missing = sorted(p for p in want if p not in have)
    wrong_size = sorted(p for p in want if p in have and have[p]["size"] != want[p]["size"])
    wrong_hash = sorted(p for p in want
                        if p in have and have[p]["size"] == want[p]["size"]
                        and have[p].get("sha256") != want[p].get("sha256"))
    extra = sorted(p for p in have if p not in want)
    return {
        "ok": not missing and not wrong_size and not wrong_hash,
        "files": len(want),
        "bytes": int(expected.get("total_bytes") or 0),
        "missing": _cap(missing), "missing_count": len(missing),
        "wrong_size": _cap(wrong_size), "wrong_size_count": len(wrong_size),
        "wrong_hash": _cap(wrong_hash), "wrong_hash_count": len(wrong_hash),
        "extra": _cap(extra), "extra_count": len(extra),
    }


def verify(folder, manifest: dict, *, hashes: bool = True) -> dict:
    """Prüft, ob in ``folder`` jede Datei des Manifests vollständig und richtig liegt.

    Das ist die Bestätigung für die Rückholung: Erst wenn ``ok`` wahr ist, darf der Ordner auf
    dem Root-Server gelöscht werden. Zusätzliche Dateien im Ordner sind erlaubt (sie tauchen
    unter ``extra`` auf, machen ``ok`` aber nicht falsch).
    """
    root = pathlib.Path(str(folder))
    if not root.is_dir():
        raise ValueError("Der Ordner ist nicht vorhanden.")
    missing: list[str] = []
    wrong_size: list[str] = []
    wrong_hash: list[str] = []
    checked = 0
    checked_bytes = 0
    for entry in manifest.get("files", []):
        name = entry["path"]
        try:
            full = paths.resolve(root, name)
        except ValueError:
            missing.append(name)
            continue
        if not full.is_file():
            missing.append(name)
            continue
        try:
            size = full.stat().st_size
        except OSError:
            missing.append(name)
            continue
        if size != entry["size"]:
            wrong_size.append(name)
            continue
        if hashes and entry.get("sha256"):
            try:
                if sha256_file(full) != entry["sha256"]:
                    wrong_hash.append(name)
                    continue
            except OSError:
                missing.append(name)
                continue
        checked += 1
        checked_bytes += size
    known = {e["path"] for e in manifest.get("files", [])}
    # Nachladbares zählt nicht als „zusätzlich“: `libraries` und `logs` entstehen auf dem Root von
    # selbst und gehören in kein Manifest – sonst meldete jeder Bericht hunderte Fundstücke.
    extra = sorted(name for name in paths.walk_files(root, nachladbar_auslassen=True)
                   if name not in known and not name.endswith(PART_SUFFIX))
    return {
        "ok": not missing and not wrong_size and not wrong_hash,
        "files": checked, "bytes": checked_bytes,
        "expected_files": len(known),
        "expected_bytes": int(manifest.get("total_bytes") or 0),
        "missing": _cap(missing), "missing_count": len(missing),
        "wrong_size": _cap(wrong_size), "wrong_size_count": len(wrong_size),
        "wrong_hash": _cap(wrong_hash), "wrong_hash_count": len(wrong_hash),
        "extra": _cap(extra), "extra_count": len(extra),
    }


def problem_text(report: dict) -> str:
    """Ein Satz, der sagt, was an einer Übertragung noch fehlt."""
    parts_out = []
    if report.get("missing_count"):
        parts_out.append(f"{report['missing_count']} Datei(en) fehlen")
    if report.get("wrong_size_count"):
        parts_out.append(f"{report['wrong_size_count']} Datei(en) sind unvollständig")
    if report.get("wrong_hash_count"):
        parts_out.append(f"{report['wrong_hash_count']} Datei(en) haben eine falsche Prüfsumme")
    example = (report.get("missing") or report.get("wrong_size") or report.get("wrong_hash") or [])
    text = ", ".join(parts_out) if parts_out else "die Übertragung ist unvollständig"
    if example:
        text += f" (z. B. „{example[0]}“)"
    return text


def confirm_pull(folder, manifest: dict) -> dict:
    """Bestätigt eine Rückholung: alles aus dem Manifest liegt lokal vollständig vor.

    Wirft einen Fehler, solange etwas fehlt – der Aufrufer darf den Ordner auf dem Root-Server
    nur löschen, wenn diese Funktion durchläuft.
    """
    report = verify(folder, manifest, hashes=True)
    if not report["ok"]:
        raise ValueError("Die Rückholung ist noch nicht vollständig: " + problem_text(report)
                         + ". Der Serverordner bleibt so lange auf dem Server liegen.")
    return report


def delete_tree(folder, allowed_root) -> dict:
    """Löscht einen Instanzordner – nur innerhalb von ``allowed_root`` und ohne Verknüpfungen.

    Wird nach einer bestätigten Rückholung (``confirm_pull``) aufgerufen.
    """
    target = pathlib.Path(str(folder))
    base = pathlib.Path(str(allowed_root))
    real_target = os.path.realpath(str(target))
    real_base = os.path.realpath(str(base))
    if real_target == real_base or not real_target.startswith(real_base + os.sep):
        raise ValueError("Dieser Ordner liegt nicht im Datenbereich des Servers und wird nicht gelöscht.")
    if os.path.islink(str(target)):
        raise ValueError("Der Ordner ist eine Verknüpfung und wird nicht gelöscht.")
    if not os.path.isdir(real_target):
        return {"deleted": False, "bytes": 0, "files": 0}
    stats = dir_stats(real_target)
    shutil.rmtree(real_target)
    return {"deleted": True, "bytes": stats["bytes"], "files": stats["files"]}


# ---------------------------------------------------------------- Stücke lesen (Download-Quelle)

def read_chunk(root, rel: str, offset: int, length: int = CHUNK_SIZE) -> bytes:
    """Liest ein Stück einer Datei im Instanzordner (für den Download zum PC)."""
    name = paths.check_transfer(rel)
    try:
        offset = int(offset)
        length = int(length)
    except (TypeError, ValueError) as exc:
        raise ValueError("Versatz oder Länge sind keine Zahlen.") from exc
    if offset < 0:
        raise ValueError("Der Versatz darf nicht negativ sein.")
    if length <= 0 or length > MAX_CHUNK_BYTES:
        raise ValueError(f"Die Länge eines Stücks muss zwischen 1 und {MAX_CHUNK_BYTES} Bytes liegen.")
    # Gelesen wird über einen Ordner-Deskriptor (paths.lies_stueck): zwischen Prüfung und
    # Zugriff darf der Serverbenutzer in seinem Ordner umbenennen – über den Pfad ließe sich
    # sonst im richtigen Augenblick eine Verknüpfung nach /srv/mcsm/data dazwischenschieben.
    try:
        return paths.lies_stueck(root, name, offset, length)
    except FileNotFoundError as exc:
        raise ValueError(f"Die Datei „{name}“ ist nicht vorhanden.") from exc
    except IsADirectoryError as exc:
        raise ValueError(f"„{name}“ ist ein Ordner.") from exc


def pack_files(root, names, *, max_bytes: int = BUNDLE_MAX_BYTES,
               max_files: int = BUNDLE_MAX_FILES,
               small_bytes: int = BUNDLE_SMALL_BYTES) -> tuple[bytes, list[dict]]:
    """Packt viele **kleine** Dateien des Instanzordners in einen tar-Strom (Rückholung).

    Das Gegenstück zu :meth:`_ChunkSession.write_bundle`: der PC holt hunderte kleine Dateien in
    einer Anfrage statt in hunderten. Gelesen wird über Ordner-Deskriptoren
    (``paths.lies_stueck``), es kommen also keine Verknüpfungen mit.

    Rückgabe ``(tar-Bytes, Liste)``. Die Liste nennt zu jeder enthaltenen Datei ``path``, ``size``
    und ``sha256``; der PC prüft damit jede Datei einzeln. Was nicht mehr hineinpasst, zu groß ist
    oder nicht (mehr) existiert, bleibt einfach weg – der Aufrufer holt es dann in Stücken.
    """
    grenze = max(1, min(int(max_bytes or BUNDLE_MAX_BYTES), BUNDLE_MAX_BYTES))
    hoechstzahl = max(1, min(int(max_files or BUNDLE_MAX_FILES), BUNDLE_MAX_FILES))
    klein = max(1, min(int(small_bytes or BUNDLE_SMALL_BYTES), BUNDLE_SMALL_BYTES))
    puffer = io.BytesIO()
    enthalten: list[dict] = []
    gesamt = 0
    with tarfile.open(fileobj=puffer, mode="w", format=tarfile.PAX_FORMAT,
                      encoding="utf-8") as archiv:
        for roh in list(names)[:hoechstzahl * 4]:
            if len(enthalten) >= hoechstzahl:
                break
            name = paths.check_transfer(roh)
            groesse = paths.groesse(root, name)
            if groesse < 0 or groesse > klein:
                continue
            if gesamt + groesse > grenze and enthalten:
                break
            try:
                inhalt = paths.lies_stueck(root, name, 0, groesse)
            except (OSError, ValueError):
                continue
            if len(inhalt) != groesse:
                continue                      # die Datei hat sich gerade geändert – später holen
            info = tarfile.TarInfo(name)
            info.size = len(inhalt)
            info.mtime = _now()
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            archiv.addfile(info, io.BytesIO(inhalt))
            enthalten.append({"path": name, "size": len(inhalt),
                              "sha256": sha256_bytes(inhalt)})
            gesamt += len(inhalt)
    return puffer.getvalue(), enthalten


# ---------------------------------------------------------------- Sitzungen

class _ChunkSession:
    """Gemeinsamer Teil von Upload und Download: Sitzung, Teil-Dateien, Wiederaufnahme."""

    kind = "upload"

    def __init__(self, store_dir, data: dict) -> None:
        self.store_dir = pathlib.Path(str(store_dir))
        self.id = check_id(data.get("id"))
        self.data = data
        self.target = pathlib.Path(str(data["target"]))
        self.manifest = data["manifest"]
        self.index = manifest_index(self.manifest)
        self.done = set(data.get("done") or [])
        self._lock = threading.RLock()
        self._chunks_since_check = 0

    # -------------------------------------------------- anlegen und laden

    @classmethod
    def begin(cls, store_dir, target, manifest: dict, *, user_id: str = "", instance_id: str = "",
              session_id: str | None = None, reserve: int = RESERVE_BYTES) -> "_ChunkSession":
        """Beginnt eine Übertragung: Manifest prüfen, Platz prüfen, Vorhandenes überspringen."""
        clean = check_manifest(manifest)
        root = pathlib.Path(str(target))
        if not root.is_absolute():
            raise ValueError("Der Zielordner muss ein vollständiger Pfad sein.")
        _check_session_budget(store_dir, user_id)
        sid = check_id(session_id) if session_id else new_id()
        done: list[str] = []
        skipped: list[str] = []
        needed = 0
        for entry in clean["files"]:
            full = paths.resolve(root, entry["path"])
            part = _part_path(root, entry["path"])
            if full.is_dir():
                raise ValueError(f"Am Ziel liegt schon ein Ordner namens „{entry['path']}“ – "
                                 "er müsste erst umbenannt oder gelöscht werden.")
            if _matches(full, entry):
                done.append(entry["path"])
                skipped.append(entry["path"])
                continue
            have = part.stat().st_size if part.is_file() else 0
            if have > entry["size"]:
                have = 0                       # unbrauchbare Teil-Datei: neu beginnen
            needed += entry["size"] - have
        # Was andere offene Sitzungen noch schreiben wollen, ist schon vergeben: sonst sehen drei
        # gleichzeitige 8-GB-Uploads jeder für sich genug Platz und füllen zusammen die Platte.
        check_space(root, needed + outstanding_bytes(), reserve=reserve)
        root.mkdir(parents=True, exist_ok=True)
        # Leere Dateien haben keine Stücke – gleich anlegen und als fertig führen.
        for entry in clean["files"]:
            if entry["size"] == 0 and entry["path"] not in done:
                full = paths.resolve(root, entry["path"])
                full.parent.mkdir(parents=True, exist_ok=True)
                with full.open("wb"):
                    pass
                done.append(entry["path"])
        now = _now()
        data = {
            "version": MANIFEST_VERSION, "id": sid, "kind": cls.kind, "state": "open",
            "user_id": str(user_id or ""), "instance_id": str(instance_id or ""),
            "target": str(root), "created_at": now, "updated_at": now,
            "manifest": clean, "done": sorted(done), "skipped": sorted(skipped),
        }
        session = cls(store_dir, data)
        with _registry_lock:
            session.save()
            _sessions[sid] = session
            _trim_cache()
        return session

    @classmethod
    def load(cls, store_dir, session_id: str) -> "_ChunkSession":
        """Lädt eine Sitzung von der Platte (ohne Zwischenspeicher – siehe ``open_session``)."""
        sid = check_id(session_id)
        data = _read_json(pathlib.Path(str(store_dir)) / sid / SESSION_FILE)
        if not isinstance(data.get("manifest"), dict) or not data.get("target"):
            raise ValueError("Die Daten der Übertragung sind beschädigt.")
        art = str(data.get("kind") or "upload")
        chosen = DownloadSession if art == "download" else UploadSession
        return chosen(store_dir, data)

    # -------------------------------------------------- Zustand

    @property
    def dir(self) -> pathlib.Path:
        return self.store_dir / self.id

    @property
    def state(self) -> str:
        return str(self.data.get("state") or "open")

    def save(self) -> None:
        self.data["done"] = sorted(self.done)
        self.data["updated_at"] = _now()
        _write_json(self.dir / SESSION_FILE, self.data)

    def remaining_bytes(self) -> int:
        """Was diese Sitzung noch auf die Platte schreiben will (0, wenn sie nicht mehr offen ist)."""
        if self.state != "open":
            return 0
        rest = 0
        for entry in self.manifest["files"]:
            if entry["path"] in self.done:
                continue
            part = _part_path(self.target, entry["path"])
            try:
                have = part.stat().st_size
            except OSError:
                have = 0
            rest += max(0, entry["size"] - have)
        return rest

    def received_bytes(self) -> int:
        """Bereits gesicherte Bytes (fertige Dateien plus angefangene Teil-Dateien)."""
        total = 0
        for entry in self.manifest["files"]:
            if entry["path"] in self.done:
                total += entry["size"]
                continue
            part = _part_path(self.target, entry["path"])
            try:
                total += part.stat().st_size
            except OSError:
                pass
        return total

    def missing(self, limit: int = _REPORT_CAP) -> list[dict]:
        """Die nächsten offenen Dateien mit dem Byte, ab dem es weitergeht."""
        out: list[dict] = []
        for entry in self.manifest["files"]:
            if entry["path"] in self.done:
                continue
            part = _part_path(self.target, entry["path"])
            try:
                offset = part.stat().st_size
            except OSError:
                offset = 0
            if offset > entry["size"]:
                offset = 0
            out.append({"path": entry["path"], "offset": offset, "size": entry["size"],
                        "sha256": entry["sha256"]})
            if len(out) >= limit:
                break
        return out

    def next_needed(self) -> dict | None:
        """Die nächste Datei, die Stücke braucht – oder ``None``, wenn alles da ist."""
        offen = self.missing(limit=1)
        return offen[0] if offen else None

    def complete(self) -> bool:
        return len(self.done) >= len(self.index)

    def status(self, *, missing_limit: int = _REPORT_CAP) -> dict:
        """Zustand für das Programm auf dem PC (Fortschrittsanzeige und Wiederaufnahme).

        ``missing_limit`` bestimmt, wie viele offene Dateien die Antwort nennt. Der PC schickt
        mehrere Dateien gleichzeitig und bündelt die kleinen – mit einer längeren Liste braucht er
        für hunderte kleine Dateien nicht hunderte Runden.
        """
        with self._lock:
            grenze = max(1, min(int(missing_limit or _REPORT_CAP), MAX_FILES))
            return {
                "id": self.id, "kind": self.kind, "state": self.state,
                "target": str(self.target),
                "instance_id": self.data.get("instance_id", ""),
                "chunk_size": CHUNK_SIZE,
                # Damit das Programm auf dem PC weiß, dass dieser Dienst Pakete annimmt
                # (ältere Stände kennen die Route nicht und lassen die Felder weg).
                "bundle": True,
                "bundle_small_bytes": BUNDLE_SMALL_BYTES,
                "bundle_max_bytes": BUNDLE_MAX_BYTES,
                "bundle_max_files": BUNDLE_MAX_FILES,
                "file_count": len(self.index),
                "total_bytes": int(self.manifest.get("total_bytes") or 0),
                "done_files": len(self.done),
                "received_bytes": self.received_bytes(),
                "skipped_files": len(self.data.get("skipped") or []),
                "complete": self.complete(),
                "missing": self.missing(limit=grenze),
                "next": self.next_needed(),
                "created_at": self.data.get("created_at", 0),
                "updated_at": self.data.get("updated_at", 0),
            }

    # -------------------------------------------------- Stücke

    def write_chunk(self, rel: str, offset: int, data: bytes) -> dict:
        """Hängt ein Stück an eine Datei an; die letzte Lieferung prüft die Prüfsumme."""
        with self._lock:
            if self.state != "open":
                raise ValueError("Diese Übertragung ist abgeschlossen oder abgebrochen.")
            name = paths.check_transfer(rel)
            entry = self.index.get(name)
            if entry is None:
                raise ValueError(f"Die Datei „{name}“ gehört nicht zu dieser Übertragung.")
            if name in self.done:
                raise ValueError(f"Die Datei „{name}“ ist bereits vollständig übertragen.")
            if not isinstance(data, (bytes, bytearray)):
                raise ValueError("Das Stück enthält keine Daten.")
            data = bytes(data)
            if not data:
                raise ValueError("Das Stück enthält keine Daten.")
            if len(data) > MAX_CHUNK_BYTES:
                raise ValueError(f"Ein Stück darf höchstens {human(MAX_CHUNK_BYTES)} groß sein.")
            try:
                offset = int(offset)
            except (TypeError, ValueError) as exc:
                raise ValueError("Der Versatz ist keine Zahl.") from exc
            part = _part_path(self.target, name)
            part_rel = name + PART_SUFFIX
            have = max(0, paths.groesse(self.target, part_rel))
            if have > entry["size"]:
                paths.entfernen(self.target, part_rel)
                have = 0
            if offset != have:
                raise ValueError(f"Die Übertragung von „{name}“ muss bei Byte {have} weitergehen "
                                 f"(gesendet wurde ab Byte {offset}).")
            if have + len(data) > entry["size"]:
                raise ValueError(f"Für „{name}“ wurden mehr Daten gesendet als angekündigt.")
            # Die Reserve wird auch **während** der Übertragung geprüft: die Platte teilen sich
            # laufende Server (Welt speichern) und andere Uploads. Lieber hier mit einem klaren
            # Satz anhalten als später mitten im Schreiben in einen OSError laufen.
            self._chunks_since_check += 1
            if self._chunks_since_check >= SPACE_CHECK_EVERY:
                self._chunks_since_check = 0
                try:
                    frei = free_space(self.target)
                except ValueError:
                    frei = None
                if frei is not None and frei - len(data) < RESERVE_BYTES:
                    raise ValueError(
                        f"Auf dem Server sind nur noch {human(frei)} frei; {human(RESERVE_BYTES)} "
                        f"müssen als Reserve frei bleiben, damit laufende Server ihre Welt "
                        f"speichern können. Die Übertragung von „{name}“ wurde angehalten – bitte "
                        f"beim Betreiber melden oder Dateien löschen und dann fortsetzen.")
            try:
                part.parent.mkdir(parents=True, exist_ok=True)
                # Angehängt wird über einen Ordner-Deskriptor: der Serverbenutzer darf in seinem
                # Instanzordner umbenennen und könnte sonst die Teil-Datei im richtigen Augenblick
                # gegen eine Verknüpfung nach /srv/mcsm/data tauschen.
                reached = paths.anhaengen(self.target, part_rel, data)
            except OSError as exc:
                raise ValueError(f"„{name}“ kann auf dem Server nicht geschrieben werden.") from exc
            if reached < entry["size"]:
                return {"path": name, "offset": reached, "size": entry["size"],
                        "done": False, "complete": False}
            got = paths.pruefsumme(self.target, part_rel)
            if got != entry["sha256"]:
                paths.entfernen(self.target, part_rel)
                raise ValueError(f"Die Prüfsumme von „{name}“ stimmt nicht – die Datei muss "
                                 "noch einmal von Anfang an übertragen werden.")
            full = paths.resolve(self.target, name)
            try:
                full.parent.mkdir(parents=True, exist_ok=True)
                paths.ersetzen(self.target, part_rel, name)
            except OSError as exc:
                raise ValueError(f"„{name}“ kann auf dem Server nicht abgelegt werden.") from exc
            if entry.get("mtime"):
                try:
                    os.utime(str(full), (entry["mtime"], entry["mtime"]))
                except OSError:
                    pass
            self.done.add(name)
            self.save()
            return {"path": name, "offset": reached, "size": entry["size"],
                    "done": True, "complete": self.complete()}

    # -------------------------------------------------- Paket mit kleinen Dateien

    def write_bundle(self, data: bytes) -> dict:
        """Nimmt ein **Paket** (tar-Strom) mit vielen kleinen Dateien in einer Anfrage an.

        Jede Datei im Paket wird einzeln geprüft, als wäre sie ein Stück: Pfad durch
        ``paths.check_transfer`` (kein Ausbruch, keine Verwaltungsdatei), sie muss zum Manifest
        dieser Übertragung gehören, Größe und SHA256 müssen stimmen, und geschrieben wird über
        Ordner-Deskriptoren (``paths.schreibe_atomar``) – eine Verknüpfung im Pfad kann den Inhalt
        also nicht aus dem Instanzordner hinausleiten.

        Dateien, die schon fertig sind, werden übergangen. Ein wiederholtes Paket nach einem
        Verbindungsabriss ist damit unschädlich.
        """
        with self._lock:
            if self.state != "open":
                raise ValueError("Diese Übertragung ist abgeschlossen oder abgebrochen.")
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise ValueError("Das Paket enthält keine Daten.")
            data = bytes(data)
            if len(data) > BUNDLE_MAX_BYTES:
                raise ValueError(f"Ein Paket darf höchstens {human(BUNDLE_MAX_BYTES)} groß sein.")
            check_space(self.target, len(data))
            geschrieben: list[str] = []
            uebergangen: list[str] = []
            geschriebene_bytes = 0
            try:
                strom = tarfile.open(fileobj=io.BytesIO(data), mode="r|", encoding="utf-8")
            except tarfile.TarError as exc:
                raise ValueError("Das Paket ist beschädigt und lässt sich nicht auspacken.") from exc
            try:
                for nummer, eintrag in enumerate(strom, start=1):
                    if nummer > BUNDLE_MAX_FILES:
                        raise ValueError(f"Ein Paket darf höchstens {BUNDLE_MAX_FILES} Dateien "
                                         f"enthalten.")
                    if not eintrag.isfile():
                        raise ValueError(f"„{eintrag.name}“ ist im Paket keine gewöhnliche Datei – "
                                         f"Ordner, Verknüpfungen und Gerätedateien nimmt der "
                                         f"Root-Server nicht an.")
                    name = paths.check_transfer(eintrag.name)
                    ziel = self.index.get(name)
                    if ziel is None:
                        raise ValueError(f"Die Datei „{name}“ gehört nicht zu dieser Übertragung.")
                    if name in self.done:
                        uebergangen.append(name)
                        continue
                    if int(eintrag.size) > BUNDLE_SMALL_BYTES:
                        raise ValueError(f"„{name}“ ist mit {human(eintrag.size)} zu groß für ein "
                                         f"Paket – größere Dateien gehen in Stücken.")
                    if int(eintrag.size) != int(ziel["size"]):
                        raise ValueError(f"„{name}“ ist im Paket {human(eintrag.size)} groß, das "
                                         f"Manifest nennt {human(ziel['size'])}.")
                    quelle = strom.extractfile(eintrag)
                    inhalt = quelle.read(int(ziel["size"])) if quelle is not None else b""
                    if len(inhalt) != int(ziel["size"]):
                        raise ValueError(f"„{name}“ kam im Paket unvollständig an.")
                    if sha256_bytes(inhalt) != ziel["sha256"]:
                        raise ValueError(f"Die Prüfsumme von „{name}“ stimmt nicht – das Paket "
                                         f"muss noch einmal geschickt werden.")
                    vollstaendig = paths.resolve(self.target, name)
                    try:
                        vollstaendig.parent.mkdir(parents=True, exist_ok=True)
                        paths.schreibe_atomar(self.target, name, inhalt)
                    except OSError as exc:
                        raise ValueError(f"„{name}“ kann auf dem Server nicht geschrieben "
                                         f"werden.") from exc
                    # Eine angefangene Teil-Datei aus einem früheren Anlauf ist jetzt überholt.
                    try:
                        paths.entfernen(self.target, name + PART_SUFFIX)
                    except (OSError, ValueError):
                        pass
                    if ziel.get("mtime"):
                        try:
                            os.utime(str(vollstaendig), (ziel["mtime"], ziel["mtime"]))
                        except OSError:
                            pass
                    self.done.add(name)
                    geschrieben.append(name)
                    geschriebene_bytes += len(inhalt)
            except tarfile.TarError as exc:
                raise ValueError(f"Das Paket lässt sich nicht auspacken: {exc}") from exc
            finally:
                try:
                    strom.close()
                except tarfile.TarError:
                    pass
                if geschrieben:
                    self.save()          # auch bei einem Fehler: das Geschriebene bleibt gültig
            return {"files": len(geschrieben), "bytes": geschriebene_bytes,
                    "skipped": _cap(uebergangen), "skipped_count": len(uebergangen),
                    "done_files": len(self.done), "complete": self.complete(),
                    "received_bytes": self.received_bytes()}

    # -------------------------------------------------- abschließen und abbrechen

    def finish(self, *, hashes: bool = True) -> dict:
        """Schließt die Übertragung ab: alle Prüfsummen noch einmal gegen das Manifest."""
        with self._lock:
            if self.state == "aborted":
                raise ValueError("Diese Übertragung wurde abgebrochen.")
            open_count = len(self.index) - len(self.done)
            if open_count > 0:
                nxt = self.next_needed()
                hint = f" (z. B. „{nxt['path']}“)" if nxt else ""
                raise ValueError(f"Die Übertragung ist noch nicht fertig: {open_count} Datei(en) "
                                 f"fehlen{hint}.")
            report = verify(self.target, self.manifest, hashes=hashes)
            if not report["ok"]:
                raise ValueError("Die Übertragung ist unvollständig: " + problem_text(report) + ".")
            self.data["state"] = "done"
            self.data["finished_at"] = _now()
            self.save()
            return {"id": self.id, "files": report["files"], "bytes": report["bytes"],
                    "skipped_files": len(self.data.get("skipped") or []), "ok": True}

    def abort(self) -> dict:
        """Bricht die Übertragung ab und räumt die Teil-Dateien weg (fertige bleiben liegen)."""
        with self._lock:
            removed = self._remove_parts()
            self.data["state"] = "aborted"
            self.save()
            return {"id": self.id, "removed_parts": removed}

    def discard(self) -> None:
        """Entfernt Teil-Dateien und die Sitzungsdaten vollständig."""
        with self._lock:
            self._remove_parts()
        with _registry_lock:
            _sessions.pop(self.id, None)
        shutil.rmtree(str(self.dir), ignore_errors=True)

    def _remove_parts(self) -> int:
        count = 0
        for entry in self.manifest["files"]:
            if entry["path"] in self.done:
                continue
            try:
                part = _part_path(self.target, entry["path"])
            except ValueError:
                continue
            try:
                if part.is_file():
                    part.unlink()
                    count += 1
            except OSError:
                continue
        return count


class UploadSession(_ChunkSession):
    """Upload vom PC auf den Root-Server (der Daemon nimmt die Stücke an)."""

    kind = "upload"


class DownloadSession(_ChunkSession):
    """Download vom Root-Server auf den PC (die empfangende Seite holt die Stücke)."""

    kind = "download"

    def run(self, fetch, *, chunk_size: int = CHUNK_SIZE, progress=None) -> dict:
        """Holt alle offenen Stücke über ``fetch(pfad, versatz, laenge) -> bytes`` ab.

        ``progress`` wird – wenn angegeben – nach jedem Stück mit dem Status aufgerufen.
        Bricht die Verbindung ab, kann ``run`` später einfach erneut aufgerufen werden.
        """
        if chunk_size <= 0 or chunk_size > MAX_CHUNK_BYTES:
            raise ValueError(f"Die Stückgröße muss zwischen 1 und {MAX_CHUNK_BYTES} Bytes liegen.")
        while True:
            todo = self.next_needed()
            if todo is None:
                break
            rest = todo["size"] - todo["offset"]
            data = fetch(todo["path"], todo["offset"], min(chunk_size, rest))
            if not data:
                raise ValueError(f"Der Server hat für „{todo['path']}“ keine Daten mehr geliefert.")
            result = self.write_chunk(todo["path"], todo["offset"], data)
            if progress is not None:
                progress(result)
        return self.finish()


# ---------------------------------------------------------------- Zwischenspeicher der Sitzungen

# Der Daemon bedient mehrere Anfragen gleichzeitig. Damit zwei Stücke derselben Übertragung nicht
# durcheinander laufen, gibt es je Kennung genau ein Sitzungsobjekt mit eigenem Schloss.
_registry_lock = threading.Lock()
_sessions: dict[str, _ChunkSession] = {}


def _trim_cache() -> None:
    """Den Zwischenspeicher auf `MAX_CACHED_SESSIONS` begrenzen (ältester Eintrag zuerst weg).

    Muss unter ``_registry_lock`` laufen. Ein Manifest kann bis zu einem MB groß sein – ohne
    Obergrenze wächst der Daemon bei vielen begonnenen Übertragungen unbegrenzt. Die Daten stehen
    auf der Platte, eine verdrängte Sitzung wird bei Bedarf einfach neu geladen.

    Verdrängt werden nur **abgeschlossene oder abgebrochene** Sitzungen: zu einer offenen muss es
    genau ein Objekt geben, sonst schrieben zwei Fäden mit zwei Schlössern in dieselbe Teil-Datei.
    Die Zahl der offenen Sitzungen begrenzt `_check_session_budget`.
    """
    if len(_sessions) <= MAX_CACHED_SESSIONS:
        return
    for sid, session in list(_sessions.items()):
        if len(_sessions) <= MAX_CACHED_SESSIONS:
            return
        try:
            offen = session.state == "open"
        except (AttributeError, KeyError):              # pragma: no cover - kaputter Eintrag
            offen = False
        if not offen:
            _sessions.pop(sid, None)


def outstanding_bytes() -> int:
    """Summe der Bytes, die alle offenen Sitzungen im Speicher noch schreiben wollen."""
    with _registry_lock:
        offen = list(_sessions.values())
    total = 0
    for session in offen:
        try:
            total += session.remaining_bytes()
        except (OSError, ValueError):
            continue
    return total


def _check_session_budget(store_dir, user_id: str) -> None:
    """Wie viele offene Übertragungen darf ein Konto gleichzeitig haben?

    Ohne diese Grenze hängt jedes ``upload/begin`` ein weiteres Manifest in den Speicher und legt
    einen weiteren Ordner an – eine Schleife aus ``begin`` und ``abort`` würde den Daemon
    aufbrauchen.
    """
    uid = str(user_id or "")
    if not uid:
        return
    try:
        alle = [s for s in list_sessions(store_dir) if str(s.get("state") or "") == "open"]
    except OSError:
        return
    eigene = [s for s in alle if str(s.get("user_id") or "") == uid]
    if len(eigene) >= MAX_OPEN_SESSIONS_PER_USER:
        raise ValueError(f"Es laufen schon {len(eigene)} Übertragungen für dein Konto – mehr als "
                         f"{MAX_OPEN_SESSIONS_PER_USER} gleichzeitig nimmt der Root-Server nicht "
                         f"an. Bitte warte, bis eine fertig ist, oder brich eine ab.")
    if len(alle) >= MAX_CACHED_SESSIONS:
        raise ValueError(f"Auf dem Root-Server laufen gerade {len(alle)} Übertragungen – das ist "
                         f"die Obergrenze. Bitte in ein paar Minuten noch einmal versuchen.")


def open_session(store_dir, session_id: str) -> _ChunkSession:
    """Sitzung aus dem Zwischenspeicher oder von der Platte holen (immer dasselbe Objekt)."""
    sid = check_id(session_id)
    with _registry_lock:
        found = _sessions.get(sid)
        if found is not None and str(found.store_dir) == str(store_dir):
            _sessions[sid] = _sessions.pop(sid)          # zuletzt benutzt = hinten (LRU)
            return found
        session = _ChunkSession.load(store_dir, sid)
        _sessions[sid] = session
        _trim_cache()
        return session


def forget_session(session_id: str) -> None:
    """Nimmt eine Sitzung aus dem Zwischenspeicher (die Daten auf der Platte bleiben)."""
    with _registry_lock:
        _sessions.pop(check_id(session_id), None)


def list_sessions(store_dir) -> list[dict]:
    """Kurzübersicht aller Sitzungen im Ablageordner (für Admin und Aufräumen)."""
    base = pathlib.Path(str(store_dir))
    out: list[dict] = []
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        if not child.is_dir() or not _ID_RE.match(child.name):
            continue
        try:
            data = _read_json(child / SESSION_FILE)
        except ValueError:
            continue
        out.append({"id": child.name, "kind": data.get("kind", "upload"),
                    "state": data.get("state", "open"),
                    "instance_id": data.get("instance_id", ""),
                    "user_id": data.get("user_id", ""),
                    "total_bytes": int((data.get("manifest") or {}).get("total_bytes") or 0),
                    "done_files": len(data.get("done") or []),
                    "created_at": data.get("created_at", 0),
                    "updated_at": data.get("updated_at", 0)})
    return out


def cleanup_old(store_dir, *, max_age: int = SESSION_MAX_AGE) -> list[str]:
    """Räumt liegengebliebene Übertragungen auf (Teil-Dateien und Sitzungsdaten)."""
    limit = _now() - max(60, int(max_age))
    removed: list[str] = []
    for info in list_sessions(store_dir):
        if int(info.get("updated_at") or 0) > limit:
            continue
        try:
            _ChunkSession.load(store_dir, info["id"]).discard()
        except ValueError:
            forget_session(info["id"])
            shutil.rmtree(str(pathlib.Path(str(store_dir)) / info["id"]), ignore_errors=True)
        removed.append(info["id"])
    return removed


# ---------------------------------------------------------------- Hilfen

def _part_path(root, rel: str) -> pathlib.Path:
    """Pfad der Teil-Datei zu einer Zieldatei (``welt/level.dat.mcsmpart``)."""
    name = paths.check_transfer(rel)
    return paths.resolve(root, name + PART_SUFFIX)


def _matches(full: pathlib.Path, entry: dict) -> bool:
    """Liegt die Datei schon vollständig und prüfsummengleich da? (Dann wird sie übersprungen.)"""
    try:
        if not full.is_file() or full.stat().st_size != entry["size"]:
            return False
        if entry["size"] == 0:
            return True
        return sha256_file(full) == entry["sha256"]
    except OSError:
        return False
