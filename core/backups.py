"""Weltensicherungen: packen, herunterladen und wieder aufspielen.

Gilt für Server auf diesem PC **und** für gehostete Server auf dem Root-Server (Pass-Inhaber).
Eine Sicherung liegt immer **auf diesem PC**, im Programmordner unter ``backups/<Servername>/`` –
bei einem gehosteten Server holt das Programm die Welten dafür vom Root-Server herunter.

Der Dateiname sagt, woher die Sicherung kommt:

    welten_2026-09-28_23-55-01.zip                  von Hand angelegt
    taeglich_2026-09-28_04-10-00.zip                die tägliche Sicherung des Programms
    vor-wiederherstellung_2026-09-28_23-58-12.zip   Pflicht-Sicherung vor dem Aufspielen

Aufgeräumt wird nur bei den täglichen (die letzten :data:`KEEP_DAILY` bleiben). Von Hand angelegte
Sicherungen und die Pflicht-Sicherungen vor einer Wiederherstellung löscht das Programm nie von
selbst – nur der Benutzer.

Im Archiv liegen die Welten so, wie sie im Serverordner liegen (``world/level.dat``,
``worlds/<Name>/level.dat``). Damit passt jede Sicherung auf beide Seiten: eine vom Root-Server
geholte Welt lässt sich in die Kopie auf diesem PC aufspielen und umgekehrt.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import shutil
import subprocess
import threading
import time
import zipfile

from . import cloud, manager, store

log = logging.getLogger("mcsm.sicherung")

#: Alle Sicherungen liegen unter diesem Ordner im Programmordner.
ROOT = store.BASE / "backups"
#: Beschreibung der Sicherung im Archiv selbst.
META_NAME = "mcsm-sicherung.json"
#: Kennzeichnet den Ordner eines Servers, damit er eine Umbenennung des Servers übersteht.
MARKER_NAME = ".mcsm-server.json"
#: So viele tägliche Sicherungen bleiben liegen; ältere räumt das Programm weg.
KEEP_DAILY = 7
#: Dateianfang je Art.
PREFIX = {"manuell": "welten", "taeglich": "taeglich", "vorher": "vor-wiederherstellung"}
LABEL = {"manuell": "von Hand", "taeglich": "täglich", "vorher": "vor einer Wiederherstellung"}
_STAMP = r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"
#: Der Wächter schaut in diesem Takt nach, ob heute schon gesichert wurde.
DAILY_CHECK_SECONDS = 15 * 60
#: Vorlauf nach dem Start des Programms, damit der erste Blick nicht in den Start hineinfällt.
DAILY_FIRST_DELAY = 90
#: So lange wird auf „Saved the game“ gewartet, bevor trotzdem gepackt wird.
SAVE_WAIT = 15.0
#: Vorwarnzeit im Spiel, bevor für eine Wiederherstellung gestoppt wird.
STOP_ANNOUNCE = 15
#: Grenzen beim Einlesen eines fremden Ordners (Root-Server), damit nichts ausufert.
MAX_REMOTE_FILES = 60_000
MAX_REMOTE_DEPTH = 24

_lock = threading.RLock()
_active: dict[str, str] = {}                 # Server-Kennung -> Job-Kennung


class BackupError(Exception):
    """Fehler, der so wie er ist in der Oberfläche stehen darf."""


# --------------------------------------------------------------------- Ordner und Namen

def _safe_name(name: str) -> str:
    """Servername als Ordnername: ohne Zeichen, die Windows nicht mag."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name or "")).strip(" .")
    clean = re.sub(r"\s+", " ", clean)[:48].strip()
    if not clean or clean.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)\d", clean.upper()):
        clean = "Server"
    return clean


def _marker(folder: pathlib.Path) -> str:
    try:
        return str(json.loads((folder / MARKER_NAME).read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def folder(cfg: dict, *, create: bool = True) -> pathlib.Path:
    """Sicherungsordner dieses Servers – er zieht mit, wenn der Server umbenannt wird."""
    sid = str(cfg["id"])
    wunsch = _safe_name(cfg.get("name"))
    with _lock:
        vorhanden: pathlib.Path | None = None
        belegt: set[str] = set()
        if ROOT.is_dir():
            for kind in sorted(ROOT.iterdir()):
                if not kind.is_dir():
                    continue
                besitzer = _marker(kind)
                if besitzer == sid:
                    vorhanden = kind
                elif besitzer:
                    belegt.add(kind.name.lower())
        if wunsch.lower() in belegt:
            wunsch = f"{wunsch} ({sid})"           # zwei Server mit demselben Namen
        ziel = ROOT / wunsch
        if vorhanden is not None:
            if vorhanden.name != wunsch:
                try:
                    vorhanden.rename(ziel)         # Server umbenannt: Ordner zieht mit
                    vorhanden = ziel
                except OSError:
                    pass                           # gerade in Benutzung – Name bleibt eben
            return vorhanden
        if not create:
            return ziel
        if ziel.exists() and _marker(ziel) not in ("", sid):
            ziel = ROOT / f"{wunsch} ({sid})"
        ziel.mkdir(parents=True, exist_ok=True)
        try:
            (ziel / MARKER_NAME).write_text(json.dumps({"id": sid, "name": cfg.get("name", "")},
                                                       ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return ziel


def _legacy_dir(cfg: dict) -> pathlib.Path:
    """Der alte Platz im Serverordner – Sicherungen von früher sollen nicht verschwinden."""
    return store.server_dir(cfg["id"]) / "backups"


def _stamp() -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S")


def kind_of(name: str) -> str:
    """Art einer Sicherung am Dateinamen erkennen."""
    for art, prefix in PREFIX.items():
        if re.fullmatch(rf"{re.escape(prefix)}_{_STAMP}\.zip", name):
            return art
    if re.fullmatch(rf"welten-{_STAMP}\.zip", name):
        return "manuell"                           # Namensform früherer Fassungen
    return "manuell"


def _date_of(name: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})_\d{2}-\d{2}-\d{2}", name)
    return m.group(1) if m else ""


def _when(name: str, mtime: int) -> int:
    """Zeitpunkt der Sicherung – aus dem Dateinamen, denn eine Kopie hat eine falsche Dateizeit."""
    m = re.search(rf"({_STAMP})", name)
    if m:
        try:
            return int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")))
        except (ValueError, OverflowError):
            pass
    return int(mtime)


def _entry(path: pathlib.Path, source: str) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    art = kind_of(path.name)
    return {"name": path.name, "size": st.st_size, "mtime": _when(path.name, int(st.st_mtime)),
            "kind": art, "kind_text": LABEL[art], "source": source}


def list_backups(cfg: dict) -> list[dict]:
    """Alle Sicherungen dieses Servers, neueste zuerst (auch die aus dem alten Ordner)."""
    out: list[dict] = []
    ziel = folder(cfg, create=False)
    if ziel.is_dir():
        out += [e for e in (_entry(p, "programm") for p in ziel.glob("*.zip")) if e]
    alt = _legacy_dir(cfg)
    if alt.is_dir():
        bekannt = {e["name"] for e in out}
        out += [e for e in (_entry(p, "server") for p in alt.glob("*.zip"))
                if e and e["name"] not in bekannt]
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


def overview(cfg: dict) -> dict:
    """Was die Oberfläche für den Reiter „Sicherungen“ braucht."""
    return {
        "folder": str(folder(cfg, create=False)),
        "entries": list_backups(cfg),
        "daily": bool(cfg.get("backup_daily", True)),
        "keep_daily": KEEP_DAILY,
        "busy": busy(cfg["id"]),
        "hosted": bool(_instance(cfg)),
    }


def _find(cfg: dict, name: str) -> pathlib.Path:
    """Dateinamen einer Sicherung auf einen Pfad abbilden – nur Namen aus der eigenen Liste."""
    name = str(name or "")
    if not re.fullmatch(r"[^\\/:*?\"<>|\x00-\x1f]{1,120}\.zip", name):
        raise BackupError("Das ist kein Name einer Sicherung.")
    for eintrag in list_backups(cfg):
        if eintrag["name"] == name:
            basis = folder(cfg, create=False) if eintrag["source"] == "programm" else _legacy_dir(cfg)
            pfad = basis / name
            if pfad.is_file():
                return pfad
    raise BackupError(f"Die Sicherung „{name}“ gibt es nicht (mehr).")


def delete_backup(cfg: dict, name: str) -> dict:
    """Eine Sicherung löschen – nur auf ausdrücklichen Wunsch des Benutzers."""
    pfad = _find(cfg, name)
    try:
        pfad.unlink()
    except OSError as exc:
        raise BackupError(f"Die Sicherung lässt sich nicht löschen: {exc}") from exc
    log.info("Sicherung gelöscht: %s", pfad.name)
    return {"ok": True, "name": name}


def open_folder(cfg: dict) -> dict:
    """Den Sicherungsordner im Explorer zeigen."""
    ziel = folder(cfg)
    subprocess.Popen(["explorer", str(ziel)])
    return {"ok": True, "folder": str(ziel)}


# --------------------------------------------------------------------- Gemeinsames

def _instance(cfg: dict) -> str:
    """Kennung auf dem Root-Server – leer, wenn der Server auf diesem PC liegt."""
    link = cloud.link_of(cfg)
    if str(link.get("state") or "") not in ("hosted", "suspended"):
        return ""
    rid = str(link.get("instance") or "").strip()
    return rid if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rid) else ""


def busy(server_id: str) -> bool:
    with _lock:
        return str(server_id) in _active


def ensure_idle(server_id: str) -> None:
    if busy(server_id):
        raise BackupError("Für diesen Server läuft gerade eine Sicherung oder eine "
                          "Wiederherstellung – bitte warten, bis sie durch ist.")


def _claim(server_id: str, job_id: str) -> None:
    with _lock:
        if server_id in _active:
            raise BackupError("Für diesen Server läuft gerade eine Sicherung oder eine "
                              "Wiederherstellung – bitte warten, bis sie durch ist.")
        _active[server_id] = job_id


def _release(server_id: str) -> None:
    with _lock:
        _active.pop(server_id, None)


def _start(cfg: dict, steps: list[str], work) -> dict:
    """Job anlegen und im Hintergrund abarbeiten.

    Der Job hängt an einer eigenen Kennung (``sicherung:<id>``): so sperrt er nicht die
    Einrichtung des Servers, ist aber über ``job/<id>`` abfragbar wie jeder andere Vorgang.
    """
    sid = str(cfg["id"])
    job = manager.new_job(f"sicherung:{sid}", steps)
    _claim(sid, job["id"])

    def run() -> None:
        try:
            work(job)
            manager.job_finish(job)
        except BackupError as exc:
            manager.job_finish(job, str(exc))
        except cloud.CloudError as exc:
            manager.job_finish(job, str(exc))
        except Exception as exc:                      # noqa: BLE001
            log.exception("Sicherung fehlgeschlagen (%s)", cfg.get("name"))
            manager.job_finish(job, f"Unerwarteter Fehler: {exc}")
        finally:
            _release(sid)

    threading.Thread(target=run, daemon=True).start()
    return job


def _meta(cfg: dict, art: str, welten: list[str], herkunft: str, heiss: bool):
    """Baut die Beschreibung, sobald Datei- und Byte-Zahl feststehen."""
    def bauen(dateien: int, bytes_: int) -> bytes:
        return json.dumps({
            "mcsm": "sicherung", "version": 1,
            "server": cfg.get("name", ""), "server_id": cfg["id"],
            "type": cfg.get("type", "java"), "flavor": cfg.get("flavor", "paper"),
            "kind": art, "origin": herkunft,
            "created": int(time.time()), "created_text": time.strftime("%d.%m.%Y %H:%M:%S"),
            "worlds": welten, "file_count": dateien, "bytes": bytes_,
            "hot": heiss,
        }, ensure_ascii=False, indent=2).encode("utf-8")
    return bauen


def _target(cfg: dict, art: str) -> pathlib.Path:
    ziel = folder(cfg) / f"{PREFIX[art]}_{_stamp()}.zip"
    n = 1
    while ziel.exists():                              # zweimal in derselben Sekunde
        ziel = ziel.with_name(f"{PREFIX[art]}_{_stamp()}-{n}.zip")
        n += 1
    return ziel


def _prune(cfg: dict) -> int:
    """Alte **tägliche** Sicherungen wegräumen; alles andere bleibt liegen."""
    taeglich = [e for e in list_backups(cfg) if e["kind"] == "taeglich" and e["source"] == "programm"]
    weg = taeglich[KEEP_DAILY:]
    ziel = folder(cfg, create=False)
    gezaehlt = 0
    for eintrag in weg:
        try:
            (ziel / eintrag["name"]).unlink()
            gezaehlt += 1
        except OSError:
            pass
    if gezaehlt:
        log.info("%d alte tägliche Sicherung(en) von „%s“ weggeräumt.", gezaehlt, cfg.get("name"))
    return gezaehlt


def _zip_folder(quelle: pathlib.Path, ziel: pathlib.Path, unterordner: list[str], meta,
                melde=None) -> tuple[int, int]:
    """Die genannten Unterordner von ``quelle`` in ein Archiv packen (erst .teil, dann umbenennen)."""
    dateien: list[tuple[pathlib.Path, str]] = []
    gesamt = 0
    for rel in unterordner:
        basis = quelle / pathlib.PurePosixPath(rel)
        for p in sorted(basis.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            try:
                gesamt += p.stat().st_size
            except OSError:
                continue
            dateien.append((p, p.relative_to(quelle).as_posix()))
    if not dateien:
        raise BackupError("In der Welt liegt keine einzige Datei – da ist nichts zu sichern.")
    teil = ziel.with_name(ziel.name + ".teil")
    teil.unlink(missing_ok=True)
    fertig = 0
    try:
        with zipfile.ZipFile(teil, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(META_NAME, meta(len(dateien), gesamt))
            for i, (p, rel) in enumerate(dateien):
                try:
                    zf.write(p, rel)
                    fertig += p.stat().st_size
                except (OSError, ValueError):
                    continue                          # eine gesperrte Datei bricht nicht alles ab
                if melde and (i % 25 == 0 or i + 1 == len(dateien)):
                    melde(i + 1, len(dateien), fertig, gesamt)
        teil.replace(ziel)
    except BaseException:
        teil.unlink(missing_ok=True)
        raise
    return len(dateien), gesamt


# --------------------------------------------------------------------- Welt auf Platte bringen

_SAVED_RE = re.compile(r"Saved the (game|world)", re.I)


def _hold_saves(cfg: dict):
    """Vor dem Packen die Welt auf die Platte schreiben lassen (Server läuft weiter).

    Gibt eine Funktion zurück, die das Speichern danach wieder freigibt – oder ``None``,
    wenn der Server gar nicht läuft.
    """
    if not manager.is_running(cfg["id"]):
        return None
    inst = manager.instance(cfg)
    try:
        if cfg.get("type") == "bedrock":
            inst.send("save hold")
            time.sleep(4.0)

            def frei_bedrock() -> None:
                try:
                    inst.send("save resume")
                except (RuntimeError, OSError):
                    pass
            return frei_bedrock
        mark = inst.console(0)["next"]
        inst.send("save-off")
        inst.send("save-all flush")
        ende = time.time() + SAVE_WAIT
        while time.time() < ende and inst.running:
            if any(_SAVED_RE.search(z) for z in inst.console(mark)["lines"]):
                break
            time.sleep(0.3)

        def frei_java() -> None:
            try:
                inst.send("save-on")
            except (RuntimeError, OSError):
                pass
        return frei_java
    except (RuntimeError, OSError):
        return None


# --------------------------------------------------------------------- Sichern

def start_backup(cfg: dict, art: str = "manuell") -> dict:
    """Sicherung anlegen – auf diesem PC oder vom Root-Server geholt."""
    if art not in PREFIX:
        art = "manuell"
    rid = _instance(cfg)
    if rid:
        steps = ["Verbindung zum Root-Server prüfen", "Welten suchen",
                 "Welten herunterladen", "Sicherung packen", "Aufräumen"]
        return _start(cfg, steps, lambda job: _backup_hosted(job, cfg, rid, art))
    steps = ["Welten suchen", "Welten packen", "Aufräumen"]
    return _start(cfg, steps, lambda job: _backup_local(job, cfg, art))


def _pack_local(cfg: dict, art: str, melde=None) -> pathlib.Path:
    """Die Welten dieses PCs in ein Archiv packen – läuft der Server, wird vorher gespeichert."""
    welten = [w["path"] for w in manager.worlds(cfg)]
    if not welten:
        raise BackupError("Es gibt noch keine Welt – der Server muss mindestens einmal "
                          "gestartet worden sein.")
    root = store.server_dir(cfg["id"])
    laeuft = manager.is_running(cfg["id"])
    frei = _hold_saves(cfg) if laeuft else None
    try:
        ziel = _target(cfg, art)
        _zip_folder(root, ziel, welten, _meta(cfg, art, welten, "lokal", laeuft), melde)
    finally:
        if frei:
            frei()
    log.info("Sicherung angelegt: %s (%s)", ziel, cloud.human(ziel.stat().st_size))
    return ziel


def _backup_local(job: dict, cfg: dict, art: str) -> dict:
    manager.job_step(job, 0)
    if manager.is_running(cfg["id"]):
        manager.job_step(job, 0, "Der Server läuft – die Welt wird zuerst auf die Platte "
                                 "geschrieben …")

    def melde(i: int, n: int, fertig: int, gesamt: int) -> None:
        manager.job_detail(job, f"Datei {i} von {n} · {cloud.human(fertig)} von {cloud.human(gesamt)}",
                           pct=33 + int(33 * (fertig / gesamt if gesamt else 1)))

    manager.job_step(job, 1)
    ziel = _pack_local(cfg, art, melde)
    manager.job_step(job, 2)
    _prune(cfg)
    manager.job_detail(job, f"{ziel.name} · {cloud.human(ziel.stat().st_size)}")
    return {"file": ziel.name, "size": ziel.stat().st_size}


# --------------------------------------------------------------------- Sichern: Root-Server

def _remote_worlds(cfg: dict, rid: str) -> list[str]:
    """Welten-Ordner auf dem Root-Server finden (wie ``manager.worlds``, nur über die Ferne)."""
    wurzel = cloud.remote_files(rid, "").get("entries") or []
    if cfg.get("type") == "bedrock":
        if not any(e.get("is_dir") and e.get("name", "").lower() == "worlds" for e in wurzel):
            return []
        unter = cloud.remote_files(rid, "worlds").get("entries") or []
        return [str(e["path"]) for e in unter if e.get("is_dir")]
    uninteressant = {"plugins", "mods", "libraries", "versions", "cache", "logs", "config",
                     "backups", "datapacks", "xbox", "crash-reports"}
    out = []
    for e in wurzel:
        if not e.get("is_dir") or str(e.get("name", "")).lower() in uninteressant:
            continue
        try:
            info = cloud.remote_file_info(rid, f"{e['path']}/level.dat")
        except cloud.CloudError:
            continue
        if info and not info.get("is_dir"):
            out.append(str(e["path"]))
    return out


def _remote_walk(rid: str, rel: str, out: list[dict], tiefe: int = 0) -> None:
    if tiefe > MAX_REMOTE_DEPTH or len(out) > MAX_REMOTE_FILES:
        return
    for e in cloud.remote_files(rid, rel).get("entries") or []:
        pfad = str(e.get("path") or "")
        if not pfad:
            continue
        if e.get("is_dir"):
            _remote_walk(rid, pfad, out, tiefe + 1)
        else:
            out.append({"path": pfad, "size": int(e.get("size") or 0)})


def _remote_running(rid: str) -> bool:
    try:
        return bool((cloud.remote_detail(rid) or {}).get("running"))
    except cloud.CloudError:
        return False


def _remote_hold(cfg: dict, rid: str):
    """Wie :func:`_hold_saves`, nur über die Konsole des Root-Servers."""
    if not _remote_running(rid):
        return None
    try:
        if cfg.get("type") == "bedrock":
            cloud.remote_command(rid, "save hold")
            time.sleep(4.0)
            return lambda: cloud.remote_command(rid, "save resume")
        cloud.remote_command(rid, "save-off")
        cloud.remote_command(rid, "save-all flush")
        time.sleep(5.0)
        return lambda: cloud.remote_command(rid, "save-on")
    except cloud.CloudError:
        return None


def _download_worlds(job: dict, cfg: dict, rid: str, welten: list[str], schritt: int,
                     ziel: pathlib.Path) -> tuple[int, int]:
    """Alle Dateien der Welten vom Root-Server in einen Ordner auf diesem PC holen."""
    dateien: list[dict] = []
    for w in welten:
        _remote_walk(rid, w, dateien)
    if not dateien:
        raise BackupError("Auf dem Root-Server liegt in den Welten keine einzige Datei.")
    gesamt = sum(d["size"] for d in dateien)
    manager.job_step(job, schritt, f"{len(dateien)} Dateien · {cloud.human(gesamt)}")
    frei = _remote_hold(cfg, rid)
    fertig = 0
    try:
        for i, d in enumerate(dateien):
            lokal = ziel / pathlib.PurePosixPath(d["path"])
            lokal.parent.mkdir(parents=True, exist_ok=True)
            cloud.remote_file_download(rid, d["path"], lokal)
            fertig += d["size"]
            if i % 5 == 0 or i + 1 == len(dateien):
                manager.job_detail(job, f"Datei {i + 1} von {len(dateien)} · "
                                        f"{cloud.human(fertig)} von {cloud.human(gesamt)}",
                                   pct=int(20 + 55 * (fertig / gesamt if gesamt else 1)))
    finally:
        if frei:
            try:
                frei()
            except cloud.CloudError:
                pass
    return len(dateien), gesamt


def _temp_dir(cfg: dict, zweck: str) -> pathlib.Path:
    ziel = store.CACHE_DIR / "sicherungen" / f"{cfg['id']}-{zweck}"
    shutil.rmtree(ziel, ignore_errors=True)
    ziel.mkdir(parents=True, exist_ok=True)
    return ziel


def _backup_hosted(job: dict, cfg: dict, rid: str, art: str) -> dict:
    manager.job_step(job, 0)
    if not cloud.logged_in():
        raise BackupError("Bitte zuerst am Root-Server anmelden – ohne Anmeldung kommt das "
                          "Programm nicht an die Welten.")
    cloud.remote_detail(rid)                          # wirft, wenn es die Instanz nicht (mehr) gibt
    manager.job_step(job, 1)
    welten = _remote_worlds(cfg, rid)
    if not welten:
        raise BackupError("Auf dem Root-Server gibt es noch keine Welt – der Server muss "
                          "mindestens einmal gestartet worden sein.")
    tmp = _temp_dir(cfg, "hol")
    try:
        laeuft = _remote_running(rid)
        _download_worlds(job, cfg, rid, welten, 2, tmp)
        manager.job_step(job, 3)
        ziel = _target(cfg, art)

        def melde(i: int, n: int, fertig: int, gesamt: int) -> None:
            manager.job_detail(job, f"Datei {i} von {n} · {cloud.human(fertig)} von {cloud.human(gesamt)}",
                               pct=int(75 + 20 * (fertig / gesamt if gesamt else 1)))
        _zip_folder(tmp, ziel, welten, _meta(cfg, art, welten, "root", laeuft), melde)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    manager.job_step(job, 4)
    _prune(cfg)
    manager.job_detail(job, f"{ziel.name} · {cloud.human(ziel.stat().st_size)}")
    log.info("Sicherung vom Root-Server geholt: %s (%s)", ziel, cloud.human(ziel.stat().st_size))
    return {"file": ziel.name, "size": ziel.stat().st_size}


# --------------------------------------------------------------------- Aufspielen

def _safe_member(name: str) -> str:
    """Eintrag aus dem Archiv prüfen – kein Ausbruch aus dem Serverordner."""
    rel = str(name or "").replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
        raise BackupError(f"Die Sicherung enthält einen unerlaubten Eintrag: {name}")
    return rel


def _worlds_in_zip(zf: zipfile.ZipFile) -> tuple[list[str], dict]:
    """Welten und Beschreibung aus dem Archiv lesen.

    Eine Welt ist ein Ordner mit ``level.dat`` (Java und Bedrock gleichermaßen); so lassen sich
    auch Archive früherer Fassungen ohne Beschreibung aufspielen.
    """
    meta: dict = {}
    try:
        meta = json.loads(zf.read(META_NAME).decode("utf-8"))
    except (KeyError, ValueError, OSError):
        meta = {}
    welten: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = _safe_member(info.filename)
        teile = rel.split("/")
        if teile[-1].lower() in ("level.dat", "levelname.txt") and len(teile) > 1:
            ordner = "/".join(teile[:-1])
            if ordner not in welten:
                welten.append(ordner)
    if not welten and isinstance(meta.get("worlds"), list):
        welten = [_safe_member(w) for w in meta["worlds"] if w]
    return welten, meta


def _extract(zf: zipfile.ZipFile, welten: list[str], ziel: pathlib.Path) -> int:
    """Nur die Dateien der Welten auspacken – alles andere im Archiv bleibt liegen."""
    anzahl = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = _safe_member(info.filename)
        if not any(rel == w or rel.startswith(w + "/") for w in welten):
            continue
        out = (ziel / rel).resolve()
        try:
            out.relative_to(ziel.resolve())
        except ValueError as exc:
            raise BackupError(f"Die Sicherung enthält einen unerlaubten Eintrag: {rel}") from exc
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as quelle, out.open("wb") as fh:
            shutil.copyfileobj(quelle, fh, 1024 * 256)
        anzahl += 1
    if not anzahl:
        raise BackupError("In der Sicherung liegt keine Welt – aufgespielt wird nichts.")
    return anzahl


def start_restore(cfg: dict, name: str) -> dict:
    """Eine Sicherung aufspielen. Vorher wird **immer** der jetzige Stand gesichert."""
    quelle = _find(cfg, name)
    rid = _instance(cfg)
    if rid:
        steps = ["Sicherung prüfen", "Server auf dem Root-Server stoppen",
                 "Jetzigen Stand sichern", "Alte Welten entfernen", "Welten hochladen",
                 "Server wieder starten"]
        return _start(cfg, steps, lambda job: _restore_hosted(job, cfg, rid, quelle))
    steps = ["Sicherung prüfen", "Server stoppen", "Jetzigen Stand sichern",
             "Welten ersetzen", "Server wieder starten"]
    return _start(cfg, steps, lambda job: _restore_local(job, cfg, quelle))


def _stop_local(job: dict, cfg: dict, schritt: int) -> bool:
    """Server stoppen, wenn er läuft – mit Ansage im Spiel. Gibt zurück, ob er lief."""
    if not manager.is_running(cfg["id"]):
        manager.job_step(job, schritt, "Der Server ist gestoppt.")
        return False
    manager.job_step(job, schritt, "Der Stopp wird im Spiel angekündigt …")
    manager.instance(cfg).stop(announce=True, seconds=STOP_ANNOUNCE,
                               reason="Eine Weltensicherung wird aufgespielt")
    ende = time.time() + 90
    while time.time() < ende and manager.is_running(cfg["id"]):
        time.sleep(0.5)
    if manager.is_running(cfg["id"]):
        raise BackupError("Der Server lässt sich nicht stoppen – bitte von Hand stoppen und "
                          "es noch einmal versuchen.")
    time.sleep(1.0)                                   # Dateien freigeben lassen
    return True


def _restore_local(job: dict, cfg: dict, quelle: pathlib.Path) -> dict:
    manager.job_step(job, 0, quelle.name)
    tmp = _temp_dir(cfg, "auf")
    try:
        with zipfile.ZipFile(quelle, "r") as zf:
            welten, _beschreibung = _worlds_in_zip(zf)
            if not welten:
                raise BackupError("In dieser Sicherung findet sich keine Welt.")
            neu = tmp / "neu"
            neu.mkdir(parents=True, exist_ok=True)
            anzahl = _extract(zf, welten, neu)
        alt = tmp / "alt"
        alt.mkdir(parents=True, exist_ok=True)
        root = store.server_dir(cfg["id"])
        lief = _stop_local(job, cfg, 1)

        manager.job_step(job, 2, "Damit ein Fehlgriff nie den jetzigen Stand kostet …")
        try:
            sicherung = _pack_local(cfg, "vorher")
            manager.job_detail(job, f"Jetziger Stand gesichert: {sicherung.name}")
        except BackupError as exc:
            # Gibt es noch gar keine Welt, ist auch nichts zu verlieren – dann darf es weitergehen.
            if "keine Welt" not in str(exc):
                raise BackupError(f"Der jetzige Stand lässt sich nicht sichern – deshalb wird "
                                  f"nichts ersetzt. ({exc})") from exc
        manager.job_step(job, 3, f"{anzahl} Dateien")
        verschoben: list[tuple[pathlib.Path, pathlib.Path]] = []
        try:
            for w in welten:
                ziel = root / pathlib.PurePosixPath(w)
                if ziel.exists():
                    weg = alt / pathlib.PurePosixPath(w)
                    weg.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(ziel), str(weg))
                    verschoben.append((weg, ziel))
                ziel.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(neu / pathlib.PurePosixPath(w)), str(ziel))
        except OSError as exc:
            for weg, ziel in verschoben:              # zurück auf den Stand von vorher
                shutil.rmtree(ziel, ignore_errors=True)
                try:
                    shutil.move(str(weg), str(ziel))
                except OSError:
                    pass
            raise BackupError(f"Die Welten lassen sich nicht ersetzen: {exc}. Der alte Stand "
                              f"steht wieder da.") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    manager.job_step(job, 4)
    if lief:
        try:
            manager.instance(cfg).start()
            manager.start_broadcaster_if_enabled(cfg)
            manager.job_detail(job, "Der Server läuft wieder.")
        except RuntimeError as exc:
            manager.job_detail(job, f"Aufgespielt – der Server ließ sich aber nicht wieder "
                                    f"starten: {exc}")
    else:
        manager.job_detail(job, "Fertig – der Server war gestoppt und bleibt es.")
    log.info("Sicherung aufgespielt: %s -> %s", quelle.name, cfg.get("name"))
    return {"ok": True, "worlds": welten, "files": anzahl}


def _restore_hosted(job: dict, cfg: dict, rid: str, quelle: pathlib.Path) -> dict:
    manager.job_step(job, 0, quelle.name)
    if not cloud.logged_in():
        raise BackupError("Bitte zuerst am Root-Server anmelden.")
    tmp = _temp_dir(cfg, "auf")
    try:
        with zipfile.ZipFile(quelle, "r") as zf:
            welten, _beschreibung = _worlds_in_zip(zf)
            if not welten:
                raise BackupError("In dieser Sicherung findet sich keine Welt.")
            neu = tmp / "neu"
            neu.mkdir(parents=True, exist_ok=True)
            anzahl = _extract(zf, welten, neu)
        lief = _remote_running(rid)
        manager.job_step(job, 1, "Der Stopp wird im Spiel angekündigt …" if lief
                         else "Der Server ist gestoppt.")
        if lief:
            cloud.remote_stop(rid, announce_seconds=STOP_ANNOUNCE,
                              reason="Eine Weltensicherung wird aufgespielt")
            ende = time.time() + 180
            while time.time() < ende and _remote_running(rid):
                time.sleep(2.0)
            if _remote_running(rid):
                raise BackupError("Der Server auf dem Root-Server lässt sich nicht stoppen – "
                                  "bitte von Hand stoppen und es noch einmal versuchen.")

        manager.job_step(job, 2, "Damit ein Fehlgriff nie den jetzigen Stand kostet …")
        sicher = _temp_dir(cfg, "vorher")
        try:
            jetzige = _remote_worlds(cfg, rid)
            if jetzige:
                _download_worlds(job, cfg, rid, jetzige, 2, sicher)
                ziel = _target(cfg, "vorher")
                _zip_folder(sicher, ziel, jetzige, _meta(cfg, "vorher", jetzige, "root", False))
                manager.job_detail(job, f"Jetziger Stand gesichert: {ziel.name}")
        finally:
            shutil.rmtree(sicher, ignore_errors=True)

        manager.job_step(job, 3)
        for w in welten:
            try:
                cloud.remote_file_delete(rid, w)
            except cloud.CloudError as exc:
                if exc.status != 404:
                    raise

        manager.job_step(job, 4, f"{anzahl} Dateien")
        hoch = sorted(p for p in neu.rglob("*") if p.is_file())
        gesamt = sum(p.stat().st_size for p in hoch) or 1
        fertig = 0
        for i, p in enumerate(hoch):
            rel = p.relative_to(neu).as_posix()
            cloud.remote_file_upload(rid, p, rel)
            fertig += p.stat().st_size
            if i % 5 == 0 or i + 1 == len(hoch):
                manager.job_detail(job, f"Datei {i + 1} von {len(hoch)} · "
                                        f"{cloud.human(fertig)} von {cloud.human(gesamt)}",
                                   pct=int(60 + 30 * (fertig / gesamt)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    manager.job_step(job, 5)
    if lief:
        try:
            cloud.remote_start(rid)
            manager.job_detail(job, "Der Server läuft wieder.")
        except cloud.CloudError as exc:
            manager.job_detail(job, f"Aufgespielt – der Server ließ sich aber nicht wieder "
                                    f"starten: {exc}")
    else:
        manager.job_detail(job, "Fertig – der Server war gestoppt und bleibt es.")
    log.info("Sicherung auf den Root-Server aufgespielt: %s -> %s", quelle.name, cfg.get("name"))
    return {"ok": True, "worlds": welten, "files": anzahl}


# --------------------------------------------------------------------- Täglich, von selbst

def due_today(cfg: dict) -> bool:
    """Steht für heute noch eine Sicherung aus?"""
    heute = time.strftime("%Y-%m-%d")
    for eintrag in list_backups(cfg):
        if _date_of(eintrag["name"]) == heute:
            return False
        if time.strftime("%Y-%m-%d", time.localtime(eintrag["mtime"])) == heute:
            return False
    return True


def _daily_candidates() -> list[dict]:
    out = []
    for cfg in store.all_servers():
        if not cfg.get("backup_daily", True) or not cfg.get("installed"):
            continue
        rid = _instance(cfg)
        if rid and not cloud.logged_in():
            continue                                   # ohne Anmeldung kommen wir nicht heran
        if not rid and cloud.locked(cfg):
            continue                                   # liegt gerade zwischen den Seiten
        if busy(cfg["id"]) or manager.job_running(cfg["id"]):
            continue
        if not rid and not store.server_dir(cfg["id"]).is_dir():
            continue
        if due_today(cfg):
            out.append(cfg)
    return out


def daily_round() -> int:
    """Einmal für alle Server nachsehen und fehlende Sicherungen anlegen (nacheinander)."""
    gemacht = 0
    for cfg in _daily_candidates():
        try:
            job = start_backup(cfg, "taeglich")
        except BackupError:
            continue
        log.info("Tägliche Sicherung für „%s“ gestartet.", cfg.get("name"))
        ende = time.time() + 6 * 3600
        while time.time() < ende:
            stand = manager.get_job(job["id"])
            if not stand or stand.get("status") != "running":
                if stand and stand.get("status") == "error":
                    log.warning("Tägliche Sicherung für „%s“ fehlgeschlagen: %s",
                                cfg.get("name"), stand.get("error"))
                else:
                    gemacht += 1
                break
            time.sleep(2.0)
    return gemacht


def start_daily_watch() -> None:
    """Wächter im Hintergrund: solange das Programm läuft, einmal am Tag je Server sichern."""
    def schleife() -> None:
        time.sleep(DAILY_FIRST_DELAY)
        while True:
            try:
                daily_round()
            except Exception:                          # noqa: BLE001
                log.exception("Tägliche Sicherung: unerwarteter Fehler")
            time.sleep(DAILY_CHECK_SECONDS)

    threading.Thread(target=schleife, daemon=True, name="sicherung-taeglich").start()
