"""Serverinstanzen auf dem Root-Server: Datensatz, Zustände und die Prüfung „darf das jetzt starten?“.

Zustände (siehe ARCHITEKTUR.md)::

    local_only -> uploading -> hosted -> awaiting_pull -> downloading -> local_only
    Premium:                   hosted <-> suspended

Gespielt werden kann ein Server immer nur an einer Stelle: Solange er ``hosted`` ist, sperrt das
Programm auf dem PC den Start der lokalen Kopie.

Das **Anlegen** ist unbegrenzt. Das Kernstück dieses Moduls ist `check_start`: Sie sagt entweder „ja“
oder in einem verständlichen deutschen Satz, warum nicht.
"""
from __future__ import annotations

import re
import shutil
from typing import NamedTuple

from . import passes, ports as ports_mod, store_hosted, users

TYPES = ("java", "bedrock")
FLAVORS = ("paper", "vanilla", "fabric", "neoforge", "forge", "quilt")
ORIGINS = ("local", "premium")

STATES = ("local_only", "uploading", "hosted", "awaiting_pull", "downloading", "suspended")

# Erlaubte Zustandswechsel. Ein Wechsel auf denselben Zustand ist immer in Ordnung.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "local_only": ("uploading",),
    "uploading": ("hosted", "local_only"),              # local_only = Übertragung abgebrochen
    "hosted": ("awaiting_pull", "suspended"),
    "awaiting_pull": ("downloading", "hosted"),         # hosted = Pass wieder gültig, weiterspielen
    "downloading": ("local_only", "awaiting_pull"),     # awaiting_pull = Rückholung abgebrochen
    "suspended": ("hosted",),
}

NAME_MIN = 2
NAME_MAX = 48
_NAME_RE = re.compile(r"[A-Za-zÄÖÜäöüß0-9](?:[A-Za-zÄÖÜäöüß0-9 ._\-]*[A-Za-zÄÖÜäöüß0-9._\-])?")
_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.\-_]{0,31}")

RAM_MIN_MB = 1024
RAM_MAX_MB = passes.RAM_TOTAL_MAX_MB

# DNS-Marke (Unterdomäne) einer Instanz: eine gültige Marke im Sinne von RFC 1035.
MARKE_MAX = 63
_MARKE_RE = re.compile(r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?")

PORT_KEYS = ("java", "bedrock")
# Es gibt nur **eine** Quelle für die freigegebenen Bereiche: ports.POOLS. Stünden hier eigene
# Zahlen, würde der PortPool Ports vergeben, die `set_ports` wieder ablehnt – im Datensatz und
# damit in der Anzeige stünde dann ein falscher Port.
PORT_RANGES = {key: (pool.first, pool.last) for key, pool in ports_mod.POOLS.items()}

#: Technische Obergrenze je Konto. Der Pass begrenzt das **Einschalten**, nicht das Anlegen –
#: diese Zahl ist nur eine Bremse gegen ein durchgelaufenes Skript, nicht Teil der Pass-Rechte.
MAX_INSTANCES_PER_USER = 500

# Weniger als so viel freier Platz -> kein Start mehr (Welten und Sicherungen brauchen Luft).
MIN_FREE_DISK_MB = 3072
# So lange bleiben Premium-Instanzen ohne gültigen Pass liegen, bevor der Betreiber sie löschen darf.
PREMIUM_KEEP_DAYS = 14


class StartCheck(NamedTuple):
    """Ergebnis der Startprüfung: ``ok`` oder ein deutscher Satz mit dem Grund."""
    ok: bool
    code: str            # maschinenlesbar, z. B. "anzahl", "ram", "platte"
    reason: str          # deutscher Satz; leer, wenn ok

    def __bool__(self) -> bool:
        return self.ok


OK = StartCheck(True, "ok", "")


# ----------------------------------------------------------------------- Textbausteine

def name_list(names) -> str:
    """Namen aufzählen: „Welt-A“, „Welt-A und Welt-B“, „Welt-A, Welt-B und Welt-C“."""
    items = [str(n) for n in names if str(n)]
    if not items:
        return "keiner"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " und " + items[-1]


def state_text(state: str) -> str:
    """Zustand als deutsche Anzeige."""
    return {
        "local_only": "nur auf deinem PC",
        "uploading": "wird hochgeladen",
        "hosted": "auf dem Root-Server",
        "awaiting_pull": "wartet auf Rückholung",
        "downloading": "wird zurückgeholt",
        "suspended": "ruht",
    }.get(state, state)


def size_text(size_bytes) -> str:
    """Belegter Platz in deutscher Schreibweise."""
    try:
        value = int(size_bytes)
    except (TypeError, ValueError):
        value = 0
    if value < 1024 * 1024:
        return f"{max(0, value) // 1024} KB"
    return passes.gb_text(value // (1024 * 1024))


# ----------------------------------------------------------------------- Prüfungen

def clean_name(raw) -> str:
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if len(name) < NAME_MIN or len(name) > NAME_MAX:
        raise ValueError(f"Der Servername muss zwischen {NAME_MIN} und {NAME_MAX} Zeichen lang sein.")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("Im Servernamen sind nur Buchstaben, Ziffern, Leerzeichen, Punkt, "
                         "Unterstrich und Bindestrich erlaubt.")
    return name


def clean_type(raw) -> str:
    value = str(raw or "").strip().lower()
    if value not in TYPES:
        raise ValueError("Die Serverart muss „java“ oder „bedrock“ sein.")
    return value


def clean_flavor(raw, server_type: str) -> str:
    value = str(raw or "").strip().lower()
    if server_type == "bedrock":
        return "vanilla"
    if value not in FLAVORS:
        raise ValueError("Unbekannte Server-Grundlage – erlaubt sind: " + ", ".join(FLAVORS) + ".")
    return value


def clean_origin(raw) -> str:
    value = str(raw or "").strip().lower()
    if value not in ORIGINS:
        raise ValueError("Die Herkunft muss „local“ oder „premium“ sein.")
    return value


def clean_state(raw) -> str:
    value = str(raw or "").strip().lower()
    if value not in STATES:
        raise ValueError("Unbekannter Zustand – erlaubt sind: " + ", ".join(STATES) + ".")
    return value


def clean_ram(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Der Arbeitsspeicher muss eine ganze Zahl in MB sein.") from None
    if not RAM_MIN_MB <= value <= RAM_MAX_MB:
        raise ValueError(f"Der Arbeitsspeicher muss zwischen {passes.gb_text(RAM_MIN_MB)} und "
                         f"{passes.gb_text(RAM_MAX_MB)} liegen.")
    return value


def clean_version(raw) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if not _VERSION_RE.fullmatch(value):
        raise ValueError("Die Versionsangabe enthält unerlaubte Zeichen.")
    return value


def clean_ports(raw, server_type: str) -> dict:
    """Ports prüfen. Erlaubt sind die Schlüssel „java“ und „bedrock“ in ihren Bereichen."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Die Ports müssen als Zuordnung „java“/„bedrock“ zu Portnummer kommen.")
    out: dict[str, int] = {}
    for key, value in raw.items():
        if key not in PORT_KEYS:
            raise ValueError(f"Unbekannter Port-Schlüssel „{key}“.")
        # Ein Bedrock-Server (BDS mit NetherNet) braucht **beides**: einen TCP-Port aus dem
        # Java-Bereich für den Verbindungsaufbau – das ist der Port, den der Spieler eintippt –
        # und einen UDP-Block für die Spieldaten. Früher stand hier „ein Bedrock-Server hat
        # keinen Java-Port“; damit landete der eingetippte Port nie im Datensatz.
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError("Portnummern müssen ganze Zahlen sein.") from None
        low, high = PORT_RANGES[key]
        if not low <= port <= high:
            raise ValueError(f"Der {key}-Port muss zwischen {low} und {high} liegen.")
        out[key] = port
    return out


# ----------------------------------------------------------------------- Lesen

def all_instances() -> list[dict]:
    return store_hosted.load("instances")


def get_instance(instance_id: str) -> dict | None:
    return store_hosted.get_by("instances", "id", str(instance_id or ""))


def instances_of(owner: str) -> list[dict]:
    """Instanzen eines Kontos, nach Namen sortiert (damit Meldungen immer gleich lauten)."""
    found = [i for i in all_instances() if i.get("owner") == owner]
    found.sort(key=lambda i: str(i.get("name", "")).casefold())
    return found


def is_running(instance: dict) -> bool:
    return bool(instance.get("running")) and instance.get("state") == "hosted"


def running_of(owner: str) -> list[dict]:
    """Laufende Instanzen eines Kontos."""
    return [i for i in instances_of(owner) if is_running(i)]


def ram_in_use_mb(owner: str) -> int:
    """Arbeitsspeicher, den die laufenden Server dieses Kontos zusammen belegen."""
    return sum(int(i.get("ram_mb") or 0) for i in running_of(owner))


def ports_in_use() -> set[int]:
    """Alle Ports, die bereits an eine Instanz vergeben sind."""
    used: set[int] = set()
    for instance in all_instances():
        for port in (instance.get("ports") or {}).values():
            try:
                used.add(int(port))
            except (TypeError, ValueError):
                continue
    return used


def free_ports(server_type: str, *, geyser: bool = False) -> dict:
    """Freie Ports aussuchen.

    Beim **Anlegen** wird das nicht mehr benutzt: der wirkliche Port kommt erst beim Start aus
    ``ports.PortPool``. Sonst hätten 100 angelegte (auch ausgeschaltete) Java-Instanzen eines
    einzigen Kontos den Bereich für alle Konten aufgebraucht.
    """
    server_type = clean_type(server_type)
    used = ports_in_use()
    wanted = ["bedrock"] if server_type == "bedrock" else ["java"] + (["bedrock"] if geyser else [])
    out: dict[str, int] = {}
    for key in wanted:
        low, high = PORT_RANGES[key]
        for port in range(low, high + 1):
            if port not in used:
                out[key] = port
                used.add(port)
                break
        else:
            raise ValueError(f"Es ist kein freier {key}-Port mehr übrig – bitte beim Betreiber melden.")
    return out


def free_disk_bytes() -> int | None:
    """Freier Platz auf der Platte des Datenordners. None, wenn er sich nicht ermitteln lässt."""
    try:
        return int(shutil.disk_usage(store_hosted.ensure_dir()).free)
    except OSError:
        return None


# ----------------------------------------------------------------------- Anlegen und Ändern

def create_instance(owner: str, name: str, *, server_type: str = "java", flavor: str = "paper",
                    version: str = "", ram_mb: int = 4096, origin: str = "local",
                    ports: dict | None = None, state: str = "", geyser: bool = False,
                    now: int | None = None) -> dict:
    """Serverinstanz anlegen. Das ist unbegrenzt erlaubt – begrenzt ist nur das Einschalten.

    Premium-Instanzen entstehen direkt auf dem Root (Zustand ``hosted``), lokale Instanzen beginnen
    als ``local_only`` und werden danach hochgeladen.

    Ein Port wird hier **nicht** vergeben: den holt sich der Daemon beim Start aus dem PortPool.
    Sonst würden angelegte Server den knappen Portbereich für alle Konten blockieren.
    """
    name = clean_name(name)
    server_type = clean_type(server_type)
    flavor = clean_flavor(flavor, server_type)
    origin = clean_origin(origin)
    ram_mb = clean_ram(ram_mb)
    version = clean_version(version)
    stamp = store_hosted.now() if now is None else int(now)
    start_state = clean_state(state) if state else ("hosted" if origin == "premium" else "local_only")

    with store_hosted.lock():
        if not users.get_user(owner):
            raise ValueError(f"Es gibt kein Konto mit der Kennung „{owner}“.")
        if origin == "premium" and not passes.has_premium(owner, now=stamp):
            raise ValueError("Für einen reinen Root-Server brauchst du einen gültigen Premium-Pass.")
        eigene = instances_of(owner)
        for existing in eigene:
            if str(existing.get("name", "")).casefold() == name.casefold():
                raise ValueError(f"Du hast schon einen Server mit dem Namen „{name}“.")
        if len(eigene) >= MAX_INSTANCES_PER_USER:
            raise ValueError(f"Du hast schon {len(eigene)} Server angelegt – mehr als "
                             f"{MAX_INSTANCES_PER_USER} nimmt der Root-Server aus technischen "
                             f"Gründen nicht an. Bitte lösche zuerst einen alten Server.")
        use_ports = clean_ports(ports, server_type) if ports else {}

        def apply(records: list[dict]) -> dict:
            entry = {
                "id": store_hosted.new_id(),
                "owner": owner,
                "name": name,
                "type": server_type,
                "flavor": flavor,
                "version": version,
                "ram_mb": ram_mb,
                "ports": use_ports,
                "state": start_state,
                "origin": origin,
                "running": False,
                "created_at": stamp,
                "updated_at": stamp,
                "started_at": 0,
                "parked_at": 0,
                "size_bytes": 0,
                # Die DNS-Marke (Unterdomäne) vergibt core/routes.py, sobald der Daemon die
                # Instanz kennt. Sie bleibt danach gleich, auch wenn der Server umbenannt wird –
                # die Spieler sollen ihre Adresse behalten.
                "marke": "",
            }
            records.append(entry)
            return dict(entry)

        return store_hosted.update("instances", apply)


def _patch(instance_id: str, changes: dict, *, now: int | None = None) -> dict:
    stamp = store_hosted.now() if now is None else int(now)
    changes = dict(changes)
    changes["updated_at"] = stamp
    return store_hosted.patch_by("instances", "id", instance_id, changes, _missing(instance_id))


def _missing(instance_id: str) -> str:
    return f"Es gibt keinen Server mit der Kennung „{instance_id}“."


def rename_instance(instance_id: str, name: str, *, now: int | None = None) -> dict:
    name = clean_name(name)
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        for other in instances_of(instance["owner"]):
            if other["id"] != instance_id and str(other.get("name", "")).casefold() == name.casefold():
                raise ValueError(f"Du hast schon einen Server mit dem Namen „{name}“.")
        return _patch(instance_id, {"name": name}, now=now)


def set_ram(instance_id: str, ram_mb: int, *, now: int | None = None) -> dict:
    """Arbeitsspeicher ändern – nur im ausgeschalteten Zustand."""
    ram_mb = clean_ram(ram_mb)
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        if is_running(instance):
            raise ValueError("Der Arbeitsspeicher lässt sich nur ändern, wenn der Server aus ist.")
        return _patch(instance_id, {"ram_mb": ram_mb}, now=now)


def set_version(instance_id: str, version: str, *, now: int | None = None) -> dict:
    return _patch(instance_id, {"version": clean_version(version)}, now=now)


def set_ports(instance_id: str, ports: dict, *, now: int | None = None) -> dict:
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        wanted = clean_ports(ports, instance.get("type", "java"))
        taken = ports_in_use() - {int(p) for p in (instance.get("ports") or {}).values()}
        for key, port in wanted.items():
            if port in taken:
                raise ValueError(f"Der Port {port} ist schon an einen anderen Server vergeben.")
        return _patch(instance_id, {"ports": wanted}, now=now)


def clean_marke(raw) -> str:
    """DNS-Marke prüfen: klein, nur a-z0-9 und Bindestrich, nicht am Rand, höchstens 63 Zeichen."""
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if len(value) > MARKE_MAX or not _MARKE_RE.fullmatch(value):
        raise ValueError("Die Unterdomäne darf nur Kleinbuchstaben, Ziffern und Bindestriche "
                         "enthalten und nicht mit einem Bindestrich beginnen oder enden.")
    return value


def set_marke(instance_id: str, marke: str, *, now: int | None = None) -> dict:
    """DNS-Marke festschreiben. Sie muss über **alle** Konten eindeutig sein, sonst landet ein
    Spieler beim fremden Server – geprüft wird deshalb gegen alle Instanzen."""
    wanted = clean_marke(marke)
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        if wanted:
            for other in all_instances():
                if str(other.get("id")) == str(instance_id):
                    continue
                if str(other.get("marke") or "").lower() == wanted:
                    raise ValueError(f"Die Unterdomäne „{wanted}“ ist schon vergeben.")
        return _patch(instance_id, {"marke": wanted}, now=now)


def set_size(instance_id: str, size_bytes: int, *, now: int | None = None) -> dict:
    """Belegten Platz fortschreiben (der Daemon rechnet ihn periodisch aus)."""
    try:
        value = max(0, int(size_bytes))
    except (TypeError, ValueError):
        raise ValueError("Die Größe muss eine ganze Zahl in Byte sein.") from None
    return _patch(instance_id, {"size_bytes": value}, now=now)


def set_state(instance_id: str, state: str, *, force: bool = False, now: int | None = None) -> dict:
    """Zustand wechseln. Erlaubt sind nur die Wege aus TRANSITIONS (``force`` nur für den Betreiber)."""
    state = clean_state(state)
    stamp = store_hosted.now() if now is None else int(now)
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        current = str(instance.get("state") or "local_only")
        if current == state:
            return instance
        if not force and state not in TRANSITIONS.get(current, ()):
            raise ValueError(f"Ein Wechsel von „{state_text(current)}“ nach „{state_text(state)}“ "
                             f"ist nicht vorgesehen.")
        if is_running(instance) and state != "hosted" and not force:
            raise ValueError(f"„{instance.get('name')}“ läuft noch – bitte zuerst stoppen.")
        changes: dict = {"state": state}
        if state != "hosted":
            changes["running"] = False
        if state in ("awaiting_pull", "suspended"):
            changes["parked_at"] = stamp
        if state in ("hosted", "local_only"):
            changes["parked_at"] = 0
        if state == "local_only":
            changes["size_bytes"] = 0
        return _patch(instance_id, changes, now=stamp)


def set_running(instance_id: str, running: bool, *, now: int | None = None) -> dict:
    """Merken, ob der Server läuft. Einschalten geht nur im Zustand ``hosted``."""
    stamp = store_hosted.now() if now is None else int(now)
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        if running:
            if instance.get("state") != "hosted":
                raise ValueError(f"„{instance.get('name')}“ ist gerade {state_text(instance.get('state'))} "
                                 f"und kann nicht laufen.")
            return _patch(instance_id, {"running": True, "started_at": stamp}, now=stamp)
        return _patch(instance_id, {"running": False}, now=stamp)


def delete_instance(instance_id: str) -> None:
    """Datensatz löschen. Den Ordner auf der Platte räumt der Daemon ab."""
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        if is_running(instance):
            raise ValueError(f"„{instance.get('name')}“ läuft noch – bitte zuerst stoppen.")
        store_hosted.remove_by("instances", "id", instance_id)


def release_instance(instance_id: str, *, now: int | None = None) -> dict:
    """Nach geprüfter Rückholung: Instanz gilt wieder als „nur auf dem PC“.

    Der Daemon löscht danach den Ordner auf dem Root-Server (und darf den Datensatz anschließend mit
    `delete_instance` entfernen, wenn er ihn nicht als Verlauf behalten will).
    """
    with store_hosted.lock():
        instance = get_instance(instance_id)
        if not instance:
            raise ValueError(_missing(instance_id))
        if instance.get("state") not in ("downloading", "awaiting_pull"):
            raise ValueError("Freigeben ist erst möglich, wenn der Server zurückgeholt wurde.")
        return set_state(instance_id, "local_only", force=True, now=now)


def public_instance(instance: dict) -> dict:
    """Instanz für die API: Datensatz plus Anzeigetexte."""
    return {
        "id": instance.get("id", ""),
        "owner": instance.get("owner", ""),
        "name": instance.get("name", ""),
        "type": instance.get("type", "java"),
        "flavor": instance.get("flavor", "paper"),
        "version": instance.get("version", "") or "",
        "ram_mb": int(instance.get("ram_mb") or 0),
        "ram_text": passes.gb_text(instance.get("ram_mb")),
        "ports": dict(instance.get("ports") or {}),
        "marke": str(instance.get("marke") or ""),
        "state": instance.get("state", "local_only"),
        "state_text": state_text(instance.get("state", "local_only")),
        "origin": instance.get("origin", "local"),
        "running": is_running(instance),
        "created_at": int(instance.get("created_at") or 0),
        "updated_at": int(instance.get("updated_at") or 0),
        "started_at": int(instance.get("started_at") or 0),
        "parked_at": int(instance.get("parked_at") or 0),
        "size_bytes": int(instance.get("size_bytes") or 0),
        "size_text": size_text(instance.get("size_bytes")),
    }


# ----------------------------------------------------------------------- Startprüfung

def check_start(instance_id: str, *, now: int | None = None,
                free_bytes: int | None = None) -> StartCheck:
    """Darf dieser Server **jetzt** eingeschaltet werden?

    Rückgabe ist `OK` oder ein `StartCheck` mit einem deutschen Satz, der genau sagt, was im Weg
    steht. Geprüft wird in dieser Reihenfolge: Konto, Zustand, läuft schon, Pass vorhanden und
    passend zur Herkunft, freier Platz, Arbeitsspeicher des Servers, Anzahl gleichzeitiger Server,
    Summe des Arbeitsspeichers.

    ``free_bytes`` kann den gemessenen freien Platz ersetzen (für Tests und für den Daemon, der ihn
    ohnehin schon kennt).
    """
    stamp = store_hosted.now() if now is None else int(now)
    instance = get_instance(instance_id)
    if not instance:
        raise ValueError(_missing(instance_id))

    owner = users.get_user(instance.get("owner", ""))
    if not owner:
        return StartCheck(False, "kein_konto", "Das Konto zu diesem Server gibt es nicht mehr – "
                                              "bitte beim Betreiber melden.")
    if owner.get("blocked"):
        grund = str(owner.get("blocked_reason") or "").strip()
        return StartCheck(False, "konto_gesperrt",
                          "Dein Konto ist gesperrt, deshalb startet kein Server."
                          + (f" Grund: {grund}" if grund else " Bitte beim Betreiber melden."))

    name = str(instance.get("name") or "Dieser Server")
    state = str(instance.get("state") or "local_only")
    if state != "hosted":
        return StartCheck(False, "zustand", _state_reason(name, state))
    if instance.get("running"):
        return StartCheck(False, "laeuft_schon", f"„{name}“ läuft schon.")

    origin = str(instance.get("origin") or "local")
    active = passes.active_passes(owner["id"], now=stamp)
    if not active:
        return StartCheck(False, "kein_pass",
                          "Du hast gerade keinen gültigen Pass – ohne Pass läuft auf dem "
                          "Root-Server kein Server. Bitte beim Betreiber einen neuen holen.")
    if not any(passes.allows_origin(p, origin) for p in active):
        return StartCheck(False, "kein_premium_pass",
                          f"„{name}“ ist ein reiner Root-Server – dafür brauchst du einen gültigen "
                          f"Premium-Pass. Dein Pass gilt nur für Server, die du von deinem PC "
                          f"hochlädst.")

    if free_bytes is None:
        free_bytes = free_disk_bytes()
    if free_bytes is not None and free_bytes < MIN_FREE_DISK_MB * 1024 * 1024:
        return StartCheck(False, "platte",
                          f"Auf dem Root-Server sind nur noch {size_text(free_bytes)} frei – unter "
                          f"{passes.gb_text(MIN_FREE_DISK_MB)} wird kein Server gestartet, damit "
                          f"Welten und Sicherungen nicht abbrechen. Bitte Dateien aufräumen oder "
                          f"beim Betreiber melden.")

    limits = passes.effective_limits(owner["id"], now=stamp)
    need = int(instance.get("ram_mb") or 0)
    total = int(limits.get("ram_total_mb") or 0)
    if need > total:
        return StartCheck(False, "ram_zu_gross",
                          f"Dein Pass erlaubt zusammen {passes.gb_text(total)}, „{name}“ ist aber mit "
                          f"{passes.gb_text(need)} eingerichtet – stelle den Arbeitsspeicher kleiner "
                          f"ein oder hole dir einen größeren Pass.")

    running = running_of(owner["id"])
    allowed = int(limits.get("max_concurrent") or 0)
    if len(running) >= allowed:
        namen = name_list([i.get("name") for i in running])
        if allowed == 1:
            return StartCheck(False, "anzahl", f"Dein Pass erlaubt 1 gleichzeitig laufenden Server – "
                                               f"es läuft bereits {namen}.")
        return StartCheck(False, "anzahl", f"Dein Pass erlaubt {allowed} gleichzeitig laufende Server "
                                           f"– es laufen bereits {namen}.")

    used = sum(int(i.get("ram_mb") or 0) for i in running)
    if used + need > total:
        return StartCheck(False, "ram",
                          f"Dein Pass erlaubt zusammen {passes.gb_text(total)}; es laufen schon "
                          f"{passes.gb_text(used)} ({name_list([i.get('name') for i in running])}), "
                          f"dieser Server braucht {passes.gb_text(need)}.")

    return OK


def reserve_start(instance_id: str, *, now: int | None = None,
                  free_bytes: int | None = None) -> StartCheck:
    """Startprüfung und Eintrag „läuft“ in **einem** Zug unter der gemeinsamen Sperre.

    Ohne das hier ließen sich die Pass-Grenzen einfach umgehen: zwei gleichzeitige Aufrufe von
    ``POST /api/servers/<id>/start`` sehen beide noch „nichts läuft“, beide bekommen ein „ja“ und
    beide starten. Zwischen `check_start` und `set_running` liegen sonst Portvergabe, Konfiguration
    und Prozessstart – viele Millisekunden.

    Der Eintrag wird **vor** dem Prozessstart gesetzt; scheitert der Start, nimmt ihn der Daemon
    mit `release_start` wieder zurück.
    """
    with store_hosted.lock():
        check = check_start(instance_id, now=now, free_bytes=free_bytes)
        if not check.ok:
            return check
        set_running(instance_id, True, now=now)
        return OK


def release_start(instance_id: str, *, now: int | None = None) -> None:
    """Eine Reservierung aus `reserve_start` zurücknehmen (der Start ist fehlgeschlagen)."""
    try:
        set_running(instance_id, False, now=now)
    except ValueError:
        pass                     # Datensatz inzwischen weg – dann gibt es auch nichts zurückzunehmen


def _state_reason(name: str, state: str) -> str:
    """Warum ein Server in diesem Zustand nicht startet."""
    return {
        "local_only": f"„{name}“ liegt nur auf deinem PC – lade ihn erst hoch, dann kann er hier laufen.",
        "uploading": f"„{name}“ wird gerade hochgeladen – bitte warten, bis die Übertragung fertig ist.",
        "awaiting_pull": f"„{name}“ wartet darauf, auf deinen PC zurückgeholt zu werden. Erst danach "
                         f"(oder mit einem neuen Pass) läuft er wieder.",
        "downloading": f"„{name}“ wird gerade auf deinen PC zurückgeholt.",
        "suspended": f"„{name}“ ruht, weil kein gültiger Premium-Pass vorliegt. Mit einem neuen "
                     f"Premium-Pass geht er wieder an.",
    }.get(state, f"„{name}“ ist gerade {state_text(state)} und kann nicht starten.")


def overview(user_id: str, *, now: int | None = None, free_bytes: int | None = None) -> dict:
    """Übersicht für ``GET /api/me``: Grenzen, was läuft, was noch frei ist."""
    stamp = store_hosted.now() if now is None else int(now)
    limits = passes.effective_limits(user_id, now=stamp)
    running = running_of(user_id)
    used = sum(int(i.get("ram_mb") or 0) for i in running)
    if free_bytes is None:
        free_bytes = free_disk_bytes()
    total_ram = int(limits.get("ram_total_mb") or 0)
    slots = int(limits.get("max_concurrent") or 0)
    return {
        "limits": limits,
        "limits_text": passes.limits_text(limits),
        "running": [public_instance(i) for i in running],
        "running_names": [i.get("name", "") for i in running],
        "instances": len(instances_of(user_id)),
        "ram_used_mb": used,
        "ram_free_mb": max(0, total_ram - used),
        "ram_total_mb": total_ram,
        "slots_used": len(running),
        "slots_free": max(0, slots - len(running)),
        "disk_free_bytes": int(free_bytes or 0),
        "disk_ok": free_bytes is None or free_bytes >= MIN_FREE_DISK_MB * 1024 * 1024,
    }


# ----------------------------------------------------------------------- Ablauf und Aufräumen

def target_state_after_expiry(instance: dict) -> str:
    """Wohin eine Instanz nach Ablauf des Passes wandert.

    Lokale Instanzen warten auf die Rückholung, Premium-Instanzen ruhen (sie haben keine lokale Heimat).
    """
    return "suspended" if str(instance.get("origin") or "local") == "premium" else "awaiting_pull"


def uncovered(user_id: str, *, now: int | None = None) -> list[dict]:
    """Instanzen dieses Kontos im Zustand ``hosted``, die von keinem gültigen Pass mehr gedeckt sind."""
    stamp = store_hosted.now() if now is None else int(now)
    active = passes.active_passes(user_id, now=stamp)
    out = []
    for instance in instances_of(user_id):
        if instance.get("state") != "hosted":
            continue
        origin = str(instance.get("origin") or "local")
        if not any(passes.allows_origin(p, origin) for p in active):
            out.append(instance)
    return out


def park_uncovered(user_id: str, *, now: int | None = None) -> list[dict]:
    """Nicht mehr gedeckte Instanzen parken: lokal -> ``awaiting_pull``, Premium -> ``suspended``.

    Der Daemon stoppt die Server vorher sauber (10-Minuten-Warnung, dann ``mcsmstop 0`` bzw. ``stop``).
    """
    stamp = store_hosted.now() if now is None else int(now)
    parked = []
    with store_hosted.lock():
        for instance in uncovered(user_id, now=stamp):
            parked.append(set_state(instance["id"], target_state_after_expiry(instance),
                                    force=True, now=stamp))
    return parked


def excess_running(user_id: str, *, now: int | None = None) -> list[dict]:
    """Laufende Server, die über die aktuellen Grenzen hinausgehen (z. B. nach einem Widerruf).

    Vorschlag in der Reihenfolge, in der gestoppt werden sollte: der zuletzt gestartete zuerst,
    damit die lange laufenden Welten weiterspielen können.
    """
    stamp = store_hosted.now() if now is None else int(now)
    limits = passes.effective_limits(user_id, now=stamp)
    running = sorted(running_of(user_id), key=lambda i: int(i.get("started_at") or 0), reverse=True)
    allowed = int(limits.get("max_concurrent") or 0)
    total = int(limits.get("ram_total_mb") or 0)
    keep: list[dict] = []
    stop: list[dict] = []
    for instance in reversed(running):                  # ältester zuerst behalten
        ram = sum(int(i.get("ram_mb") or 0) for i in keep) + int(instance.get("ram_mb") or 0)
        if len(keep) < allowed and ram <= total:
            keep.append(instance)
        else:
            stop.append(instance)
    stop.sort(key=lambda i: int(i.get("started_at") or 0), reverse=True)
    return stop


def premium_delete_candidates(*, keep_days: int = PREMIUM_KEEP_DAYS, now: int | None = None) -> list[dict]:
    """Ruhende Premium-Instanzen, die lange genug ohne gültigen Pass liegen – der Betreiber darf sie löschen."""
    stamp = store_hosted.now() if now is None else int(now)
    cutoff = max(0, int(keep_days)) * 86400
    out = []
    for instance in all_instances():
        if instance.get("state") != "suspended" or str(instance.get("origin")) != "premium":
            continue
        parked = int(instance.get("parked_at") or 0)
        if parked and stamp - parked < cutoff:
            continue
        if passes.has_premium(instance.get("owner", ""), now=stamp):
            continue
        out.append(instance)
    return out


def total_size_bytes(user_id: str = "") -> int:
    """Belegter Platz eines Kontos (oder aller Konten, wenn kein Konto angegeben ist)."""
    source = instances_of(user_id) if user_id else all_instances()
    return sum(int(i.get("size_bytes") or 0) for i in source)
