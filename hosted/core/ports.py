"""Portvergabe für die gehosteten Minecraft-Server.

Freigegeben sind auf dem Root-Server nur zwei Bereiche (siehe hosted/ARCHITEKTUR.md):

* Java-Edition über **TCP 25565-25700**
* Bedrock/Geyser über **UDP 19132-19300**

Dieses Modul merkt sich, welche Instanz welche Ports belegt, findet freie Ports und schließt
Kollisionen aus – sowohl mit anderen Instanzen (eigene Buchführung) als auch mit fremden
Programmen auf der Maschine (Probe-Bind, abschaltbar).

Der Daemon hält genau einen `PortPool`, füllt ihn beim Start aus `instances.json`
(`restore`) und schreibt `snapshot()` nach jeder Änderung zurück.
"""
from __future__ import annotations

import re
import socket
import threading
from typing import Iterable, Mapping

TCP = "tcp"
UDP = "udp"

# Instanzkennungen, die der Daemon als Eigentümer übergibt (z. B. "a1b2c3d4" oder "user/instanz").
_OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")

KIND_JAVA = "java"
KIND_BEDROCK = "bedrock"
VALID_KINDS = (KIND_JAVA, KIND_BEDROCK)

JAVA_POOL = "java"
BEDROCK_POOL = "bedrock"

# BDS bindet pro Spieler-Sitzung einen eigenen UDP-Port aus `server-udp-ports` – deshalb wächst
# der Block mit max-players (wie im lokalen Programm, mindestens 16 Ports).
MIN_UDP_BLOCK = 16
UDP_BLOCK_EXTRA = 8
MAX_UDP_BLOCK = 64


class PortsExhausted(ValueError):
    """Im gewünschten Bereich ist kein passender Port (mehr) frei.

    Absichtlich von ValueError abgeleitet: der Daemon kann daraus einen eigenen HTTP-Status
    machen, ein allgemeiner ValueError-Handler fängt sie aber weiterhin mit.
    """


class PortRange:
    """Ein freigegebener Portbereich; beide Grenzen gehören dazu."""

    __slots__ = ("name", "proto", "first", "last")

    def __init__(self, name: str, proto: str, first: int, last: int) -> None:
        if proto not in (TCP, UDP):
            raise ValueError(f"Unbekanntes Protokoll: {proto!r}")
        if not (1024 <= int(first) <= int(last) <= 65535):
            raise ValueError("Der Portbereich muss zwischen 1024 und 65535 liegen.")
        self.name = str(name)
        self.proto = proto
        self.first = int(first)
        self.last = int(last)

    @property
    def size(self) -> int:
        return self.last - self.first + 1

    def __contains__(self, port: object) -> bool:
        try:
            return self.first <= int(port) <= self.last     # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def ports(self) -> range:
        return range(self.first, self.last + 1)

    def label(self) -> str:
        return f"{self.proto.upper()} {self.first}-{self.last}"

    def __repr__(self) -> str:                              # pragma: no cover - nur Fehlersuche
        return f"PortRange({self.name!r}, {self.label()!r})"


# Die freigegebenen Bereiche. Der Name ist gleichzeitig der Schlüssel im Pool.
POOLS: dict[str, PortRange] = {
    JAVA_POOL: PortRange(JAVA_POOL, TCP, 25565, 25700),
    BEDROCK_POOL: PortRange(BEDROCK_POOL, UDP, 19132, 19300),
}


def firewall_ranges() -> list[tuple[str, int, int]]:
    """[(proto, erster, letzter)] – für die Firewall-Regeln des Root-Servers."""
    return [(r.proto, r.first, r.last) for r in POOLS.values()]


def udp_block_size(max_players: int | None = 10) -> int:
    """Größe des UDP-Blocks eines Bedrock-Servers (NetherNet) – nach oben begrenzt, damit ein
    Server mit vielen Spielerplätzen nicht den ganzen Bereich aufbraucht."""
    try:
        players = int(max_players or 10)
    except (TypeError, ValueError):
        players = 10
    players = max(1, min(200, players))
    return max(MIN_UDP_BLOCK, min(MAX_UDP_BLOCK, players + UDP_BLOCK_EXTRA))


def requirements(kind: str, *, geyser: bool = False, max_players: int = 10) -> list[tuple[str, int]]:
    """Welche Pools eine Instanz mit wie vielen Ports braucht: [(pool, anzahl), …].

    * Java ohne Crossplay: ein TCP-Port.
    * Java mit Geyser: ein TCP-Port + ein UDP-Port für Bedrock-Spieler.
    * Bedrock (BDS mit NetherNet): ein TCP-Port für den Verbindungsaufbau + ein UDP-Block
      für die Spieldaten.
    """
    kind = str(kind or "").lower()
    if kind == KIND_JAVA:
        needs = [(JAVA_POOL, 1)]
        if geyser:
            needs.append((BEDROCK_POOL, 1))
        return needs
    if kind == KIND_BEDROCK:
        return [(JAVA_POOL, 1), (BEDROCK_POOL, udp_block_size(max_players))]
    raise ValueError(f"Unbekannte Serverart: {kind!r} (erlaubt: java, bedrock)")


def _check_owner(owner: str) -> str:
    owner = str(owner or "").strip()
    if not _OWNER_RE.fullmatch(owner):
        raise ValueError("Ungültige Instanzkennung für die Portvergabe.")
    return owner


def _check_pool(pool: str) -> PortRange:
    rng = POOLS.get(str(pool or "").lower())
    if rng is None:
        raise ValueError(f"Unbekannter Portbereich: {pool!r}")
    return rng


def port_free_on_system(port: int, proto: str, *, host: str = "0.0.0.0") -> bool:
    """Ob sich der Port jetzt binden lässt – erkennt Ports, die ein fremdes Programm hält."""
    kind = socket.SOCK_DGRAM if proto == UDP else socket.SOCK_STREAM
    try:
        with socket.socket(socket.AF_INET, kind) as sock:
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False


class PortLease:
    """Ein zusammenhängender Portblock, der einer Instanz gehört."""

    __slots__ = ("owner", "pool", "first", "count")

    def __init__(self, owner: str, pool: str, first: int, count: int = 1) -> None:
        self.owner = _check_owner(owner)
        rng = _check_pool(pool)
        self.pool = rng.name
        first = int(first)
        count = int(count)
        if count < 1:
            raise ValueError("Ein Portblock braucht mindestens einen Port.")
        if first not in rng or (first + count - 1) not in rng:
            raise ValueError(f"Der Portblock {first}-{first + count - 1} liegt außerhalb von "
                             f"{rng.label()}.")
        self.first = first
        self.count = count

    @property
    def last(self) -> int:
        return self.first + self.count - 1

    @property
    def proto(self) -> str:
        return POOLS[self.pool].proto

    def ports(self) -> range:
        return range(self.first, self.last + 1)

    def overlaps(self, first: int, count: int) -> bool:
        return self.first <= int(first) + int(count) - 1 and int(first) <= self.last

    def as_dict(self) -> dict:
        return {"owner": self.owner, "pool": self.pool, "proto": self.proto,
                "first": self.first, "count": self.count}

    @classmethod
    def from_dict(cls, data: Mapping) -> "PortLease":
        if not isinstance(data, Mapping):
            raise ValueError("Ein Porteintrag muss ein Objekt sein.")
        return cls(str(data.get("owner") or ""), str(data.get("pool") or ""),
                   int(data.get("first") or 0), int(data.get("count") or 1))

    def __repr__(self) -> str:                              # pragma: no cover - nur Fehlersuche
        return f"PortLease({self.owner!r}, {self.pool!r}, {self.first}-{self.last})"


class PortPool:
    """Buchführung über die vergebenen Ports aller Instanzen.

    `probe=True` prüft zusätzlich mit einem Bind-Versuch, ob der Port wirklich frei ist
    (fremde Dienste, hängengebliebene Server). Für Selbsttests `probe=False`, damit die
    Vergabe unabhängig von der Maschine gleich ausfällt.
    """

    def __init__(self, *, probe: bool = True) -> None:
        self._probe = bool(probe)
        self._leases: dict[tuple[str, str], PortLease] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- Auskunft
    def leases(self, owner: str | None = None) -> list[PortLease]:
        """Alle Buchungen, optional nur die einer Instanz."""
        with self._lock:
            if owner is None:
                return list(self._leases.values())
            key = _check_owner(owner)
            return [ls for (own, _pool), ls in self._leases.items() if own == key]

    def lease(self, owner: str, pool: str) -> PortLease | None:
        with self._lock:
            return self._leases.get((_check_owner(owner), _check_pool(pool).name))

    def used_ports(self, pool: str) -> set[int]:
        rng = _check_pool(pool)
        with self._lock:
            used: set[int] = set()
            for (_own, name), ls in self._leases.items():
                if name == rng.name:
                    used.update(ls.ports())
            return used

    def free_ports(self, pool: str) -> int:
        """Wie viele Ports in diesem Bereich rechnerisch noch frei sind."""
        rng = _check_pool(pool)
        return rng.size - len(self.used_ports(rng.name))

    def owner_of(self, pool: str, port: int) -> str | None:
        """Wem gehört dieser Port? Nur für Protokoll und Admin-Ansicht – Fehlermeldungen an
        Benutzer nennen fremde Instanzen nicht."""
        rng = _check_pool(pool)
        with self._lock:
            for (own, name), ls in self._leases.items():
                if name == rng.name and int(port) in ls.ports():
                    return own
        return None

    def is_free(self, pool: str, first: int, count: int = 1, *, owner: str | None = None,
                probe: bool | None = None) -> bool:
        """Ob der Block frei ist. Eigene Buchungen der Instanz `owner` stören dabei nicht.

        ``probe=False`` lässt den Bind-Versuch weg. Das braucht der Daemon für Ports, die
        absichtlich schon von einem anderen Dienst gehalten werden – der Verteiler sitzt auf
        25565, und genau deshalb soll dieser Port hier als **belegt gebucht** werden, damit ihn
        keine Instanz bekommt. Mit Bind-Versuch wäre das unmöglich: der Port ist ja besetzt.
        """
        rng = _check_pool(pool)
        first, count = int(first), int(count)
        if count < 1 or first not in rng or (first + count - 1) not in rng:
            return False
        mine = _check_owner(owner) if owner else None
        with self._lock:
            for (own, name), ls in self._leases.items():
                if name != rng.name or (mine and own == mine):
                    continue
                if ls.overlaps(first, count):
                    return False
        if self._probe if probe is None else bool(probe):
            return all(port_free_on_system(p, rng.proto) for p in range(first, first + count))
        return True

    # ---------------------------------------------------------------- Vergabe
    def reserve(self, owner: str, pool: str, first: int, count: int = 1, *,
                probe: bool | None = None) -> PortLease:
        """Einen bestimmten Block buchen (Wiederherstellen aus instances.json, feste Wünsche).

        ``probe=False`` überspringt den Bind-Versuch – siehe `is_free`.
        """
        lease = PortLease(owner, pool, first, count)
        with self._lock:
            if not self.is_free(lease.pool, lease.first, lease.count, owner=lease.owner,
                                probe=probe):
                where = str(lease.first) if lease.count == 1 else f"{lease.first}-{lease.last}"
                raise PortsExhausted(f"Port {where} ({lease.proto.upper()}) ist schon belegt.")
            self._leases[(lease.owner, lease.pool)] = lease
            return lease

    def allocate(self, owner: str, pool: str, count: int = 1) -> PortLease:
        """Den ersten freien Block im Bereich buchen."""
        owner = _check_owner(owner)
        rng = _check_pool(pool)
        count = int(count)
        if count < 1:
            raise ValueError("Ein Portblock braucht mindestens einen Port.")
        if count > rng.size:
            raise ValueError(f"{count} Ports passen nicht in den Bereich {rng.label()}.")
        with self._lock:
            existing = self._leases.get((owner, rng.name))
            if existing is not None and existing.count == count:
                return existing                     # schon gebucht – gleiche Vergabe behalten
            for first in range(rng.first, rng.last - count + 2):
                if self.is_free(rng.name, first, count, owner=owner):
                    lease = PortLease(owner, rng.name, first, count)
                    self._leases[(owner, rng.name)] = lease
                    return lease
        raise PortsExhausted(
            f"Im Bereich {rng.label()} ist kein freier Platz für {count} Port(s) mehr – "
            "es laufen zu viele Server auf dieser Maschine.")

    def allocate_for(self, owner: str, kind: str, *, geyser: bool = False,
                     max_players: int = 10) -> dict[str, PortLease]:
        """Alle Ports einer Instanz in einem Schritt buchen (alles oder nichts)."""
        owner = _check_owner(owner)
        needs = requirements(kind, geyser=geyser, max_players=max_players)
        keep = {ls.pool: ls for ls in self.leases(owner)}
        with self._lock:
            done: dict[str, PortLease] = {}
            try:
                for pool, count in needs:
                    done[pool] = self.allocate(owner, pool, count)
            except ValueError:
                for pool in done:
                    if pool not in keep:
                        self._leases.pop((owner, pool), None)
                    else:
                        self._leases[(owner, pool)] = keep[pool]
                raise
            # Ports, die diese Instanz nicht mehr braucht (Crossplay abgeschaltet), freigeben.
            for pool in list(keep):
                if pool not in done:
                    self._leases.pop((owner, pool), None)
            return done

    def release(self, owner: str, pool: str | None = None) -> int:
        """Ports einer Instanz freigeben; gibt die Anzahl der gelösten Buchungen zurück."""
        owner = _check_owner(owner)
        with self._lock:
            if pool is not None:
                return 1 if self._leases.pop((owner, _check_pool(pool).name), None) else 0
            keys = [k for k in self._leases if k[0] == owner]
            for key in keys:
                del self._leases[key]
            return len(keys)

    # ---------------------------------------------------------------- Für die Konfiguration
    def assignment(self, owner: str) -> dict:
        """Die Ports einer Instanz so, wie sie in server.properties bzw. die Geyser-Konfiguration
        gehören: `port` (TCP), `bedrock_port` (UDP, nur mit Geyser/Bedrock) und bei Bedrock
        zusätzlich `udp_first`/`udp_last` für `server-udp-ports`."""
        out: dict[str, int] = {}
        java = self.lease(owner, JAVA_POOL)
        bedrock = self.lease(owner, BEDROCK_POOL)
        if java is not None:
            out["port"] = java.first
        if bedrock is not None:
            out["bedrock_port"] = bedrock.first
            if bedrock.count > 1:
                out["udp_first"] = bedrock.first
                out["udp_last"] = bedrock.last
        return out

    # ---------------------------------------------------------------- Speichern und Laden
    def snapshot(self) -> list[dict]:
        """Alle Buchungen als JSON-taugliche Liste (stabil sortiert)."""
        with self._lock:
            return [ls.as_dict() for ls in sorted(self._leases.values(),
                                                  key=lambda l: (l.pool, l.first))]

    def restore(self, entries: Iterable[Mapping]) -> list[str]:
        """Buchungen aus der gespeicherten Liste übernehmen. Unbrauchbare oder doppelte Einträge
        werden übersprungen; die Rückgabe nennt sie in verständlichen Sätzen fürs Protokoll."""
        notes: list[str] = []
        with self._lock:
            for raw in entries or []:
                try:
                    lease = PortLease.from_dict(raw)
                except (ValueError, TypeError, KeyError) as exc:
                    notes.append(f"Porteintrag übersprungen: {exc}")
                    continue
                key = (lease.owner, lease.pool)
                if key in self._leases:
                    notes.append(f"Porteintrag für {lease.owner} im Bereich {lease.pool} "
                                 "war doppelt vorhanden und wurde übersprungen.")
                    continue
                clash = None
                for (own, name), other in self._leases.items():
                    if name == lease.pool and other.overlaps(lease.first, lease.count):
                        clash = own
                        break
                if clash is not None:
                    notes.append(f"Port {lease.first} war doppelt vergeben – die Buchung von "
                                 f"{lease.owner} wurde verworfen und wird neu vergeben.")
                    continue
                self._leases[key] = lease
        return notes
