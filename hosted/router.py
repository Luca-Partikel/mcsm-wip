#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcsm-router – der Verteiler vor den gehosteten Minecraft-Servern.

Ein Server, eine Adresse, kein Port: Auf **TCP 25565** lauscht dieser Verteiler. Ein Java-Client
schickt als erstes Paket den Handshake und schreibt dort die Adresse hinein, die der Spieler
eingetippt hat (z. B. ``mein-server.arcardia-nexus.de``). Der Verteiler liest diese Adresse,
schlägt in ``/srv/mcsm/data/routes.json`` den internen Port nach und reicht die Verbindung an
``127.0.0.1:<port>`` weiter. Die bereits gelesenen Bytes gehen dabei **unverändert** mit – der
Minecraft-Server bekommt genau den Datenstrom, den der Client geschickt hat.

Eigenschaften:

* Nur Standardbibliothek (Python 3.11), keine Fremdpakete – wie der übrige Hosted-Modus.
* Läuft als eigener Dienst (``mcsm-router.service``), also unabhängig vom Daemon ``mcsmd``.
  Fällt der Daemon aus, spielen die laufenden Server weiter.
* Kennt keine Konten, keine Tokens, keine Passwörter. Er kennt nur Adresse → Port.
* Datensparsam: im Protokoll steht die Spieler-IP nur bei Fehlern; sonst eine Kennung, die je
  Prozessstart neu gesalzen wird und sich nicht zurückrechnen lässt.

Einstellungen (alle über Umgebungsvariablen, Vorgaben in Klammern):

===========================  ========================================================
``MCSM_ROUTER_BIND``         Adresse zum Lauschen (``""`` = alle, IPv4 **und** IPv6)
``MCSM_ROUTER_PORT``         Port zum Lauschen (``25565``)
``MCSM_ROUTES``              Zuordnungstabelle (``/srv/mcsm/data/routes.json``)
``MCSM_ROUTER_DOMAIN``       Basisdomain (``arcardia-nexus.de``)
``MCSM_ROUTER_TARGET``       Zielrechner der Weiterleitung (``127.0.0.1``)
``MCSM_ROUTER_LIMIT``        Gleichzeitige Verbindungen je Ziel (``200``)
``MCSM_ROUTER_LIMIT_TOTAL``  Gleichzeitige Verbindungen insgesamt (``1000``)
``MCSM_ROUTER_TIMEOUT``      Zeitlimit für den Handshake in Sekunden (``5``)
``MCSM_ROUTER_LOG``          Protokolldatei (``/var/log/mcsm/router.log``)
``MCSM_ROUTER_LOG_STDOUT``   Zusätzlich auf die Ausgabe (``1``; für Tests ``0``)
``MCSM_ROUTER_STATS``        Abstand der Zählerzeile in Sekunden (``300``, ``0`` = aus)
===========================  ========================================================

Start von Hand (Selbsttest)::

    MCSM_ROUTER_PORT=25565 python3 router.py
    python3 router.py --pruefen        # nur Einstellungen und Tabelle prüfen
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import selectors
import signal
import socket
import sys
import threading
import time
from typing import Callable, NamedTuple

VERSION = "1.0.0"

# --------------------------------------------------------------------------- Vorgaben

STANDARD_PORT = 25565
STANDARD_DOMAIN = "arcardia-nexus.de"
STANDARD_ZIEL = "127.0.0.1"
STANDARD_ROUTEN = "/srv/mcsm/data/routes.json"
STANDARD_PROTOKOLL = "/var/log/mcsm/router.log"

GRENZE_JE_ZIEL = 200            # gleichzeitige Verbindungen je Ziel
GRENZE_GESAMT = 1000            # gleichzeitige Verbindungen über alle Ziele
HANDSHAKE_FRIST = 5.0           # so lange darf ein Client für sein erstes Paket brauchen
ZIEL_FRIST = 5.0                # so lange darf der Verbindungsaufbau zum Server dauern
ABKLINGZEIT = 10.0             # so lange wird nach dem Ende einer Richtung auf die andere gewartet
ANTWORT_FRIST = 0.5             # kurzes Nachlesen bei Statusabfragen und altem Ping
PUFFER_GROESSE = 64 * 1024      # Häppchen beim Durchreichen
ERSTES_PAKET_MAX = 8192         # mehr als das ist kein Handshake mehr
HANDSHAKE_MAX = 4096            # angekündigte Länge des Handshake-Pakets
STRING_MAX_BYTES = 1024         # Adressfeld: 255 Zeichen laut Protokoll, Proxys hängen mehr an
ADRESSE_MAX = 253               # längster gültiger Hostname

# Schlüssel in routes.json für „alles ohne eigene Unterdomain“ (freiwillig, siehe finde()).
STANDARD_SCHLUESSEL = "@standard"

# Marken vor der Basisdomain, die nie ein Server sind (nginx bedient sie bzw. sie sind allgemein).
RESERVIERTE_MARKEN = ("server", "www", "api", "admin", "mc", "play", "status", "panel")

# Sichtbare Meldungen (Deutsch, ohne Satzzeichen am Ende – sie stehen im Spiel für sich).
MELDUNG_UNBEKANNT = "Diesen Server gibt es hier nicht"
MELDUNG_AUS = "Dieser Server läuft gerade nicht"
MELDUNG_VOLL = "Dieser Server ist gerade voll"
MELDUNG_NAME = "Arcardia Nexus"

ALTER_PING = 0xFE               # erstes Byte des Pings vor 1.7
PAKET_HANDSHAKE = 0x00
PAKET_STATUS = 0x00
PAKET_PING = 0x01
PAKET_TRENNUNG = 0x00           # im Zustand „Anmeldung“: Disconnect
ZUSTAND_STATUS = 1
ZUSTAND_BEITRITT = 2
ZUSTAND_UEBERGABE = 3           # ab 1.20.5: „transfer“, verhält sich wie Beitritt


# --------------------------------------------------------------------------- Einstellungen

def _text(name: str, vorgabe: str) -> str:
    wert = os.environ.get(name)
    return vorgabe if wert is None else wert.strip()


def _zahl(name: str, vorgabe: int, *, kleinster: int = 0, groesster: int = 2 ** 31) -> int:
    rohwert = (os.environ.get(name) or "").strip()
    if not rohwert:
        return vorgabe
    try:
        wert = int(rohwert)
    except ValueError:
        return vorgabe
    return max(kleinster, min(groesster, wert))


def _kommazahl(name: str, vorgabe: float, *, kleinste: float = 0.1,
               groesste: float = 600.0) -> float:
    rohwert = (os.environ.get(name) or "").strip()
    if not rohwert:
        return vorgabe
    try:
        wert = float(rohwert.replace(",", "."))
    except ValueError:
        return vorgabe
    return max(kleinste, min(groesste, wert))


def _ja(name: str, vorgabe: bool = True) -> bool:
    rohwert = (os.environ.get(name) or "").strip().lower()
    if not rohwert:
        return vorgabe
    return rohwert not in ("0", "nein", "no", "false", "aus", "off")


def routen_pfad() -> pathlib.Path:
    return pathlib.Path(_text("MCSM_ROUTES", STANDARD_ROUTEN))


def protokoll_pfad() -> pathlib.Path:
    return pathlib.Path(_text("MCSM_ROUTER_LOG", STANDARD_PROTOKOLL))


# --------------------------------------------------------------------------- Protokoll

_log_lock = threading.Lock()
_log_stand: dict = {"pfad": None, "strom": None, "gewarnt": False}

# Salz je Prozessstart: die Kennung im Protokoll lässt sich damit nicht auf eine IP zurückrechnen.
_SALZ = secrets.token_bytes(16)


def kennung(ip: str) -> str:
    """Kurze, nicht zurückrechenbare Kennung einer Gegenstelle (Datensparsamkeit)."""
    roh = hashlib.sha256(_SALZ + str(ip or "").encode("utf-8")).hexdigest()
    return roh[:8]


def _schreibe(zeile: str) -> None:
    ziel = str(protokoll_pfad())
    with _log_lock:
        if _log_stand["pfad"] != ziel:
            alt = _log_stand.get("strom")
            if alt is not None:
                try:
                    alt.close()
                except OSError:
                    pass
            strom = None
            try:
                pathlib.Path(ziel).parent.mkdir(parents=True, exist_ok=True)
                strom = open(ziel, "a", encoding="utf-8")
            except OSError as exc:
                if not _log_stand["gewarnt"]:
                    print(f"[verteiler] Das Protokoll {ziel} ist nicht beschreibbar ({exc}) – "
                          f"es wird nur nach journalctl geschrieben.", file=sys.stderr, flush=True)
                    _log_stand["gewarnt"] = True
            _log_stand["pfad"] = ziel
            _log_stand["strom"] = strom
        strom = _log_stand.get("strom")
        if strom is not None:
            try:
                strom.write(zeile + "\n")
                strom.flush()
            except OSError:
                _log_stand["strom"] = None
    if _ja("MCSM_ROUTER_LOG_STDOUT", True):
        print(zeile, flush=True)


def schliesse_protokoll() -> None:
    """Protokolldatei schließen (Selbsttests, Neustart des Protokolls)."""
    with _log_lock:
        strom = _log_stand.get("strom")
        if strom is not None:
            try:
                strom.close()
            except OSError:
                pass
        _log_stand["strom"] = None
        _log_stand["pfad"] = None


def melde(bereich: str, text: str) -> None:
    """Eine Zeile ins Protokoll: ``2026-09-25 12:00:00 [bereich] Text``."""
    _schreibe(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{bereich}] {text}")


# --------------------------------------------------------------------------- Protokollfehler

class Unvollstaendig(Exception):
    """Die Bytes reichen noch nicht – es muss weitergelesen werden."""


class Protokollfehler(Exception):
    """Die Bytes ergeben kein gültiges Minecraft-Paket."""


# --------------------------------------------------------------------------- VarInt & Co.

def _als_int32(wert: int) -> int:
    """VarInts sind im Minecraft-Protokoll vorzeichenbehaftete 32-Bit-Zahlen."""
    wert &= 0xFFFFFFFF
    return wert - 0x1_0000_0000 if wert & 0x8000_0000 else wert


def lese_varint(daten: bytes, pos: int = 0) -> tuple[int, int]:
    """VarInt an Stelle ``pos`` lesen. Rückgabe: (Wert, Stelle danach)."""
    if pos < 0:
        raise Protokollfehler("Negative Leseposition.")
    wert = 0
    for schritt in range(5):
        stelle = pos + schritt
        if stelle >= len(daten):
            raise Unvollstaendig("Das VarInt ist noch nicht vollständig eingetroffen.")
        byte = daten[stelle]
        wert |= (byte & 0x7F) << (7 * schritt)
        if not byte & 0x80:
            return _als_int32(wert), stelle + 1
    raise Protokollfehler("Ein VarInt ist länger als 5 Byte – das ist kein Minecraft-Handshake.")


def schreibe_varint(wert: int) -> bytes:
    """Zahl als VarInt (wie im Minecraft-Protokoll)."""
    zahl = int(wert) & 0xFFFFFFFF
    aus = bytearray()
    while True:
        byte = zahl & 0x7F
        zahl >>= 7
        if zahl:
            aus.append(byte | 0x80)
        else:
            aus.append(byte)
            return bytes(aus)


def lese_string(daten: bytes, pos: int = 0, *, max_bytes: int = STRING_MAX_BYTES) -> tuple[str, int]:
    """Zeichenkette lesen: VarInt-Länge in Byte, danach UTF-8."""
    laenge, pos = lese_varint(daten, pos)
    if laenge < 0:
        raise Protokollfehler("Eine Zeichenkette mit negativer Länge ist nicht möglich.")
    if laenge > max_bytes:
        raise Protokollfehler(f"Eine Zeichenkette mit {laenge} Byte ist zu lang "
                              f"(höchstens {max_bytes}).")
    if pos + laenge > len(daten):
        raise Unvollstaendig("Die Zeichenkette ist noch nicht vollständig eingetroffen.")
    roh = bytes(daten[pos:pos + laenge])
    try:
        return roh.decode("utf-8"), pos + laenge
    except UnicodeDecodeError as exc:
        raise Protokollfehler("Die Zeichenkette ist nicht als UTF-8 lesbar.") from exc


def schreibe_string(text: str) -> bytes:
    roh = str(text).encode("utf-8")
    return schreibe_varint(len(roh)) + roh


def paket(kennziffer: int, *teile: bytes) -> bytes:
    """Paket bauen: VarInt-Gesamtlänge, VarInt-Kennziffer, Inhalt."""
    rumpf = schreibe_varint(kennziffer) + b"".join(teile)
    return schreibe_varint(len(rumpf)) + rumpf


# --------------------------------------------------------------------------- Handshake

class Handshake(NamedTuple):
    """Das erste Paket eines Java-Clients."""
    protokoll: int          # Protokollversion des Clients
    adresse: str            # Serveradresse, wie der Client sie geschickt hat (mit Zusätzen)
    port: int               # Port, den der Client angesprochen hat
    zustand: int            # 1 = Statusabfrage, 2 = Beitritt, 3 = Übergabe (ab 1.20.5)
    laenge: int             # so viele Byte des Puffers gehören zum Handshake-Paket

    @property
    def ist_status(self) -> bool:
        return self.zustand == ZUSTAND_STATUS

    @property
    def ist_beitritt(self) -> bool:
        return self.zustand in (ZUSTAND_BEITRITT, ZUSTAND_UEBERGABE)


def zerlege_handshake(puffer: bytes) -> Handshake:
    """Handshake aus dem Puffer lesen.

    Wirft ``Unvollstaendig``, solange noch Bytes fehlen, und ``Protokollfehler``, wenn die Bytes
    kein Handshake sein können. Der Puffer darf mehr enthalten als das Paket (der Rest bleibt
    unangetastet und wird später mit durchgereicht).
    """
    if not puffer:
        raise Unvollstaendig("Es ist noch kein Byte eingetroffen.")
    if puffer[0] == ALTER_PING:
        raise Protokollfehler("Das ist der alte Ping (0xFE), kein Handshake.")

    laenge, pos = lese_varint(puffer, 0)
    if laenge <= 0:
        raise Protokollfehler(f"Die angekündigte Paketlänge {laenge} ist ungültig.")
    if laenge > HANDSHAKE_MAX:
        raise Protokollfehler(f"Das erste Paket ist mit {laenge} Byte zu groß für einen Handshake.")
    ende = pos + laenge
    if ende > len(puffer):
        raise Unvollstaendig("Das Handshake-Paket ist noch nicht vollständig eingetroffen.")

    rumpf = bytes(puffer[pos:ende])
    try:
        kennziffer, p = lese_varint(rumpf, 0)
        if kennziffer != PAKET_HANDSHAKE:
            raise Protokollfehler(f"Erwartet wird Paket 0x00, gekommen ist 0x{kennziffer & 0xFF:02x}.")
        protokoll, p = lese_varint(rumpf, p)
        adresse, p = lese_string(rumpf, p)
        if p + 2 > len(rumpf):
            raise Unvollstaendig("Der Port fehlt noch.")
        port = int.from_bytes(rumpf[p:p + 2], "big")
        p += 2
        zustand, p = lese_varint(rumpf, p)
    except Unvollstaendig as exc:
        # Das Paket ist vollständig da, aber kürzer als sein Inhalt verspricht.
        raise Protokollfehler("Das Handshake-Paket ist kürzer als angekündigt.") from exc

    if zustand not in (ZUSTAND_STATUS, ZUSTAND_BEITRITT, ZUSTAND_UEBERGABE):
        raise Protokollfehler(f"Unbekannter nächster Zustand {zustand} "
                              f"(erwartet werden 1, 2 oder 3).")
    return Handshake(protokoll=protokoll, adresse=adresse, port=port,
                     zustand=zustand, laenge=ende)


def zerlege_alten_ping(puffer: bytes) -> tuple[str, int] | None:
    """Alten Ping (vor 1.7) lesen.

    Rückgabe ``None``, solange noch Bytes fehlen können, sonst ``(Adresse, Port)``. Der ganz alte
    Ping (nur ``0xFE``) und die Fassung 1.4/1.5 (``0xFE 0x01``) tragen keine Adresse: dann ist die
    Adresse leer. Die Fassung 1.6 hängt die eingetippte Adresse in einem ``MC|PingHost``-Paket an.
    """
    if not puffer or puffer[0] != ALTER_PING:
        raise Protokollfehler("Das ist kein alter Ping (erstes Byte ist nicht 0xFE).")
    if len(puffer) < 2:
        return None                                  # vielleicht kommt noch 0x01
    if puffer[1] != 0x01:
        return "", 0                                 # ganz alter Ping
    if len(puffer) < 3:
        return None
    if puffer[2] != 0xFA:
        return "", 0                                 # 1.4/1.5: keine Adresse dabei

    pos = 3

    def kurz(stelle: int) -> int | None:
        if len(puffer) < stelle + 2:
            return None
        return int.from_bytes(puffer[stelle:stelle + 2], "big")

    kanal_zeichen = kurz(pos)
    if kanal_zeichen is None:
        return None
    pos += 2
    if kanal_zeichen > 64:
        return "", 0
    if len(puffer) < pos + kanal_zeichen * 2:
        return None
    kanal = puffer[pos:pos + kanal_zeichen * 2].decode("utf-16-be", "replace")
    pos += kanal_zeichen * 2
    if kanal != "MC|PingHost":
        return "", 0
    if kurz(pos) is None:
        return None
    pos += 2                                         # Restlänge, wird nicht gebraucht
    if len(puffer) < pos + 1:
        return None
    pos += 1                                         # Protokollversion des Clients
    host_zeichen = kurz(pos)
    if host_zeichen is None:
        return None
    pos += 2
    if host_zeichen > ADRESSE_MAX:
        return "", 0
    if len(puffer) < pos + host_zeichen * 2:
        return None
    host = puffer[pos:pos + host_zeichen * 2].decode("utf-16-be", "replace")
    pos += host_zeichen * 2
    if len(puffer) < pos + 4:
        return None
    port = int.from_bytes(puffer[pos:pos + 4], "big", signed=True)
    return host, max(0, min(65535, port))


# --------------------------------------------------------------------------- Antworten

def status_json(meldung: str, protokoll: int) -> str:
    """Statusantwort als JSON. Die Protokollversion des Clients wird gespiegelt, damit der
    Client die Meldung als gewöhnliche Serverbeschreibung anzeigt und nicht „nicht passend“."""
    gespiegelt = protokoll if 0 <= protokoll <= 0x7FFF_FFFF else -1
    daten = {
        "version": {"name": MELDUNG_NAME, "protocol": gespiegelt},
        "players": {"max": 0, "online": 0, "sample": []},
        "description": {"text": meldung, "color": "red"},
    }
    return json.dumps(daten, ensure_ascii=False, separators=(",", ":"))


def status_paket(meldung: str, protokoll: int) -> bytes:
    """Statusantwort (Zustand 1) als fertiges Paket."""
    return paket(PAKET_STATUS, schreibe_string(status_json(meldung, protokoll)))


def ping_paket(nutzlast: bytes) -> bytes:
    """Antwort auf die Laufzeitmessung: dieselben 8 Byte zurück."""
    return paket(PAKET_PING, bytes(nutzlast[:8]))


def trenn_paket(meldung: str) -> bytes:
    """Trennmeldung (Zustand 2) als JSON-Chat-Komponente, korrekt längencodiert."""
    inhalt = json.dumps({"text": meldung, "color": "red"},
                        ensure_ascii=False, separators=(",", ":"))
    return paket(PAKET_TRENNUNG, schreibe_string(inhalt))


def alter_ping_paket(meldung: str, *, protokoll: int = 127, version: str = "1.6.4") -> bytes:
    """Antwort auf den alten Ping: Kick-Paket 0xFF mit UTF-16BE-Text."""
    text = "§" + "1\x00" + f"{protokoll}\x00{version}\x00{meldung}\x000\x000"
    zeichen = len(text)
    if zeichen > 0xFFFF:                             # kann nicht vorkommen, aber sicher ist sicher
        text = text[:0xFFFF]
        zeichen = len(text)
    return b"\xff" + zeichen.to_bytes(2, "big") + text.encode("utf-16-be")


# --------------------------------------------------------------------------- Adressen

_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
                          r"(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*")
_MARKE_RE = re.compile(r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?")


def bereinige_adresse(roh) -> str:
    """Die Adresse aus dem Handshake auf das Wesentliche kürzen.

    Forge (``\\0FML3\\0``), Geyser und Proxys hängen mit Nullbyte getrennte Zusätze an – die
    werden abgeschnitten. Groß-/Kleinschreibung ist egal, ein Punkt am Ende (absolute DNS-Namen)
    und Klammern um eine IPv6-Adresse fallen weg.
    """
    adresse = str(roh or "").split("\x00", 1)[0].strip().rstrip(".").lower()
    if adresse.startswith("[") and adresse.endswith("]"):
        adresse = adresse[1:-1]
    return adresse[:ADRESSE_MAX]


def ist_ip(adresse: str) -> bool:
    """Hat der Spieler statt eines Namens eine IP-Adresse eingetippt?"""
    try:
        ipaddress.ip_address(adresse)
    except ValueError:
        return False
    return True


def ist_hostname(adresse: str) -> bool:
    return bool(adresse) and len(adresse) <= ADRESSE_MAX and bool(_HOSTNAME_RE.fullmatch(adresse))


def pruefe_schluessel(roh) -> str:
    """Schlüssel aus routes.json prüfen: eine Marke (``mein-server``), ein ganzer Name
    (``mc.beispiel.de``) oder ``@standard``. Rückgabe: der bereinigte Schlüssel."""
    wert = str(roh or "").strip().rstrip(".").lower()
    if wert == STANDARD_SCHLUESSEL:
        return wert
    if not wert:
        raise ValueError("Ein leerer Schlüssel ist nicht erlaubt.")
    if not ist_hostname(wert):
        raise ValueError(f"„{wert}“ ist kein gültiger Name (erlaubt sind Kleinbuchstaben, "
                         f"Ziffern, Bindestrich und Punkt).")
    return wert


def pruefe_port(roh) -> int:
    """Port aus der Tabelle prüfen: eine ganze Zahl von 1 bis 65535."""
    if isinstance(roh, bool) or isinstance(roh, float):
        raise ValueError(f"„{roh}“ ist kein Port (es muss eine ganze Zahl sein).")
    if isinstance(roh, str):
        text = roh.strip()
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f"„{roh}“ ist kein Port.")
        port = int(text)
    elif isinstance(roh, int):
        port = roh
    else:
        raise ValueError(f"„{roh}“ ist kein Port.")
    if not 1 <= port <= 65535:
        raise ValueError(f"Der Port {port} liegt außerhalb von 1 bis 65535.")
    return port


# --------------------------------------------------------------------------- Zuordnungstabelle

class Route(NamedTuple):
    """Ein Eintrag der Zuordnungstabelle."""
    name: str               # Schlüssel, unter dem er gefunden wurde (auch der Zähl-Schlüssel)
    port: int               # interner Port auf 127.0.0.1
    instanz: str            # Kennung der Serverinstanz (nur fürs Protokoll)
    grenze: int             # gleichzeitige Verbindungen; 0 = Vorgabe des Verteilers


_NIE_GELESEN = object()             # Anfangswert der Dateimarke, siehe Routen.aktualisiere


class Routen:
    """Adresse → interner Port, gelesen aus ``routes.json``.

    Die Datei wird nur neu gelesen, wenn sich Zeitstempel, Größe oder Inode geändert haben, und
    dafür höchstens einmal je ``pruef_abstand`` Sekunden nachgesehen. Ist die Datei beschädigt
    oder nicht lesbar, bleibt die **letzte gültige** Tabelle in Kraft – ein Tippfehler des
    Betreibers wirft niemanden aus seinem Spiel.
    """

    def __init__(self, pfad=None, *, domain: str = STANDARD_DOMAIN,
                 pruef_abstand: float = 1.0, melder: Callable[[str, str], None] | None = None):
        self._pfad = pathlib.Path(pfad) if pfad else None
        self.domain = str(domain or "").strip().strip(".").lower()
        self._abstand = max(0.0, float(pruef_abstand))
        self._melder = melder or melde
        self._lock = threading.RLock()
        self._eintraege: dict[str, Route] = {}
        self._marke: object = _NIE_GELESEN          # unterscheidbar von „Datei fehlt“ (None)
        self._geprueft = 0.0
        self._klage = ""                             # letzte Klage, damit sie nur einmal im Protokoll steht

    # ------------------------------------------------------------------ lesen

    @property
    def pfad(self) -> pathlib.Path:
        return self._pfad if self._pfad is not None else routen_pfad()

    def _stempel(self) -> tuple | None:
        try:
            zustand = self.pfad.stat()
        except OSError:
            return None
        return (zustand.st_mtime_ns, zustand.st_size, zustand.st_ino)

    def aktualisiere(self, *, erzwingen: bool = False) -> bool:
        """Bei Bedarf neu einlesen. Rückgabe: ob die Tabelle neu gelesen wurde."""
        jetzt = time.monotonic()
        with self._lock:
            if not erzwingen and self._geprueft and jetzt - self._geprueft < self._abstand:
                return False
            self._geprueft = jetzt
            stempel = self._stempel()
            if not erzwingen and stempel == self._marke:
                return False
            self._marke = stempel
            eintraege, klagen, behalten = self._lese()
            for klage in klagen:
                if klage != self._klage:
                    self._melder("verteiler", klage)
                    self._klage = klage
            if behalten:
                return False
            self._klage = ""
            vorher = set(self._eintraege)
            self._eintraege = eintraege
            neu = sorted(set(eintraege) - vorher)
            weg = sorted(vorher - set(eintraege))
            teile = [f"{len(eintraege)} Einträge"]
            if neu:
                teile.append("neu: " + ", ".join(neu))
            if weg:
                teile.append("weg: " + ", ".join(weg))
            self._melder("verteiler", f"Zuordnungstabelle gelesen ({'; '.join(teile)}).")
            return True

    def _lese(self) -> tuple[dict[str, Route], list[str], bool]:
        """Datei einlesen. Rückgabe: (Einträge, Klagen, alte Tabelle behalten?)."""
        pfad = self.pfad
        try:
            text = pfad.read_text(encoding="utf-8")
        except FileNotFoundError:
            if self._eintraege:
                return {}, [f"Die Zuordnungstabelle {pfad} ist verschwunden – "
                            f"es wird niemand mehr weitergeleitet."], False
            return {}, [f"Die Zuordnungstabelle {pfad} gibt es noch nicht – "
                        f"es wird niemand weitergeleitet."], False
        except OSError as exc:
            return {}, [f"Die Zuordnungstabelle {pfad} ist nicht lesbar ({exc}) – "
                        f"die bisherige Tabelle bleibt in Kraft."], True
        try:
            rohdaten = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            return {}, [f"Die Zuordnungstabelle {pfad} enthält kein gültiges JSON ({exc}) – "
                        f"die bisherige Tabelle bleibt in Kraft."], True
        if isinstance(rohdaten, dict):
            for huelle in ("routen", "routes"):
                innen = rohdaten.get(huelle)
                if isinstance(innen, dict):
                    rohdaten = innen
                    break
        if not isinstance(rohdaten, dict):
            return {}, [f"Die Zuordnungstabelle {pfad} muss eine Zuordnung "
                        f"„Name → Port“ sein – die bisherige Tabelle bleibt in Kraft."], True

        eintraege: dict[str, Route] = {}
        klagen: list[str] = []
        for roher_schluessel, roher_wert in rohdaten.items():
            if str(roher_schluessel).startswith("_"):
                continue                             # Platz für Bemerkungen in der Datei
            try:
                schluessel = pruefe_schluessel(roher_schluessel)
                if isinstance(roher_wert, dict):
                    port = pruefe_port(roher_wert.get("port"))
                    instanz = str(roher_wert.get("instanz") or roher_wert.get("instance") or "")[:64]
                    grenze = roher_wert.get("grenze", roher_wert.get("limit", 0))
                    grenze = max(0, int(grenze)) if isinstance(grenze, int) and not isinstance(grenze, bool) else 0
                else:
                    port = pruefe_port(roher_wert)
                    instanz = ""
                    grenze = 0
            except ValueError as exc:
                klagen.append(f"Eintrag „{roher_schluessel}“ in {pfad} wird übersprungen: {exc}")
                continue
            if schluessel in eintraege:
                klagen.append(f"Der Eintrag „{schluessel}“ steht mehrfach in {pfad} – "
                              f"der erste gilt.")
                continue
            eintraege[schluessel] = Route(name=schluessel, port=port,
                                          instanz=instanz, grenze=grenze)
        return eintraege, klagen, False

    # ------------------------------------------------------------------ nachschlagen

    def eintraege(self) -> dict[str, Route]:
        with self._lock:
            return dict(self._eintraege)

    def ohne_marke(self, adresse: str) -> bool:
        """Hat der Spieler keine eigene Unterdomain angesprochen?

        Das ist der Fall bei der nackten Basisdomain (``arcardia-nexus.de``), bei einer
        eingetippten IP-Adresse, bei einer allgemeinen Marke (``server.``, ``www.``, ``mc.``)
        und bei jeder Adresse, die gar nicht zu uns gehört.
        """
        if not adresse or ist_ip(adresse):
            return True
        if not self.domain:
            return False
        if adresse == self.domain:
            return True
        if not adresse.endswith("." + self.domain):
            return True
        marke = adresse[: -(len(self.domain) + 1)]
        return marke.split(".")[-1] in RESERVIERTE_MARKEN or not marke

    def kandidaten(self, adresse: str) -> list[str]:
        """Schlüssel in der Reihenfolge, in der nachgeschlagen wird."""
        liste: list[str] = []
        if adresse:
            liste.append(adresse)                    # ganzer Name als Schlüssel
            if self.domain and adresse.endswith("." + self.domain):
                vorn = adresse[: -(len(self.domain) + 1)]
                if vorn:
                    liste.append(vorn)               # „mein-server“ – der Normalfall
                    if "." in vorn:
                        liste.append(vorn.split(".")[0])
        if self.ohne_marke(adresse):
            liste.append(STANDARD_SCHLUESSEL)        # nur freiwillig, siehe Klassendoku
        gesehen: set[str] = set()
        return [k for k in liste if not (k in gesehen or gesehen.add(k))]

    def finde(self, adresse_roh) -> Route | None:
        """Route zu einer Adresse aus dem Handshake – oder ``None``."""
        adresse = bereinige_adresse(adresse_roh)
        self.aktualisiere()
        with self._lock:
            for schluessel in self.kandidaten(adresse):
                route = self._eintraege.get(schluessel)
                if route is not None:
                    return route
        return None


# --------------------------------------------------------------------------- Zähler

class Zaehler:
    """Offene Verbindungen: insgesamt und je Ziel."""

    def __init__(self, *, gesamt_grenze: int = GRENZE_GESAMT):
        self.gesamt_grenze = max(1, int(gesamt_grenze))
        self._lock = threading.Lock()
        self._offen: dict[str, int] = {}
        self._gesamt = 0

    def belege(self, ziel: str, grenze: int) -> str:
        """Platz nehmen. Rückgabe: leer = in Ordnung, sonst ein deutscher Grund."""
        grenze = max(1, int(grenze))
        with self._lock:
            if self._gesamt >= self.gesamt_grenze:
                return (f"Der Verteiler ist an seiner Gesamtgrenze "
                        f"({self.gesamt_grenze} Verbindungen).")
            jetzt = self._offen.get(ziel, 0)
            if jetzt >= grenze:
                return f"Für „{ziel}“ sind schon {jetzt} Verbindungen offen (Grenze {grenze})."
            self._offen[ziel] = jetzt + 1
            self._gesamt += 1
            return ""

    def gib_frei(self, ziel: str) -> None:
        with self._lock:
            jetzt = self._offen.get(ziel, 0)
            if jetzt <= 1:
                self._offen.pop(ziel, None)
            else:
                self._offen[ziel] = jetzt - 1
            self._gesamt = max(0, self._gesamt - 1)

    def offen(self, ziel: str) -> int:
        with self._lock:
            return self._offen.get(ziel, 0)

    def gesamt(self) -> int:
        with self._lock:
            return self._gesamt

    def stand(self) -> dict[str, int]:
        with self._lock:
            return dict(self._offen)


# --------------------------------------------------------------------------- Durchreichen

def sende(verbindung: socket.socket, daten: bytes) -> bool:
    """Bytes wegschicken; ``False``, wenn die Gegenstelle schon weg ist."""
    try:
        verbindung.sendall(daten)
        return True
    except OSError:
        return False


def _pumpe(quelle: socket.socket, ziel: socket.socket) -> int:
    """Eine Richtung durchreichen, bis nichts mehr kommt. Rückgabe: übertragene Bytes."""
    menge = 0
    try:
        while True:
            stueck = quelle.recv(PUFFER_GROESSE)
            if not stueck:
                break
            ziel.sendall(stueck)
            menge += len(stueck)
    except OSError:
        pass
    finally:
        try:
            ziel.shutdown(socket.SHUT_WR)            # dem Gegenüber das Ende ansagen
        except OSError:
            pass
    return menge


def _zu(verbindung: socket.socket | None) -> None:
    if verbindung is None:
        return
    try:
        verbindung.close()
    except OSError:
        pass


# --------------------------------------------------------------------------- Verteiler

class Verteiler:
    """Nimmt Verbindungen an, liest den Handshake und reicht weiter."""

    def __init__(self, routen: Routen, *, ziel_host: str = STANDARD_ZIEL,
                 grenze: int = GRENZE_JE_ZIEL, gesamt_grenze: int = GRENZE_GESAMT,
                 frist: float = HANDSHAKE_FRIST, ziel_frist: float = ZIEL_FRIST,
                 eigener_port: int = 0,
                 melder: Callable[[str, str], None] | None = None):
        self.routen = routen
        self.ziel_host = str(ziel_host or STANDARD_ZIEL)
        # Eigener Port: ein Eintrag, der darauf zeigt, wäre eine Schleife (siehe _reiche_weiter).
        self.eigener_port = int(eigener_port or 0)
        self.grenze = max(1, int(grenze))
        self.frist = max(0.5, float(frist))
        self.ziel_frist = max(0.5, float(ziel_frist))
        self.zaehler = Zaehler(gesamt_grenze=gesamt_grenze)
        self.melder = melder or melde
        self.zahlen = {"angenommen": 0, "weitergeleitet": 0, "unbekannt": 0,
                       "abgewiesen": 0, "aus": 0, "alter_ping": 0, "fehler": 0}
        self._zahlen_lock = threading.Lock()
        self._halt = threading.Event()
        self._horcher: list[socket.socket] = []

    # ------------------------------------------------------------------ Zahlen

    def _zaehl(self, name: str, mehr: int = 1) -> None:
        with self._zahlen_lock:
            self.zahlen[name] = self.zahlen.get(name, 0) + mehr

    def stand_text(self) -> str:
        with self._zahlen_lock:
            zahlen = dict(self.zahlen)
        offen = self.zaehler.stand()
        je_ziel = ", ".join(f"{name}={anzahl}" for name, anzahl in sorted(offen.items())) or "keine"
        return (f"angenommen={zahlen['angenommen']} weitergeleitet={zahlen['weitergeleitet']} "
                f"unbekannt={zahlen['unbekannt']} abgewiesen={zahlen['abgewiesen']} "
                f"aus={zahlen['aus']} alter-ping={zahlen['alter_ping']} "
                f"fehler={zahlen['fehler']} offen={self.zaehler.gesamt()} ({je_ziel})")

    # ------------------------------------------------------------------ eine Verbindung

    def bearbeite(self, klient: socket.socket, gegenstelle) -> None:
        """Eine angenommene Verbindung von Anfang bis Ende bedienen."""
        ip = gegenstelle[0] if isinstance(gegenstelle, (tuple, list)) and gegenstelle else "?"
        kurz = kennung(ip)
        self._zaehl("angenommen")
        try:
            klient.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self._erstes_paket(klient, ip, kurz)
        except Exception as exc:                                     # noqa: BLE001
            self._zaehl("fehler")
            self.melder("fehler", f"Unerwarteter Fehler bei {ip}: {exc.__class__.__name__}: {exc}")
        finally:
            _zu(klient)

    def _erstes_paket(self, klient: socket.socket, ip: str, kurz: str) -> None:
        """Erstes Paket lesen (Handshake oder alter Ping) und entscheiden."""
        frist_ende = time.monotonic() + self.frist
        puffer = b""
        while True:
            rest = frist_ende - time.monotonic()
            if rest <= 0:
                self._zaehl("fehler")
                self.melder("fehler", f"Zeitlimit: {ip} hat in {self.frist:.0f} s kein "
                                      f"vollständiges erstes Paket geschickt ({len(puffer)} Byte).")
                return
            try:
                klient.settimeout(min(rest, 1.0))
                stueck = klient.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self.melder("fehler", f"Verbindung von {ip} abgebrochen: {exc}")
                return
            if not stueck:
                if puffer:
                    self.melder("fehler", f"{ip} hat die Verbindung nach {len(puffer)} Byte "
                                          f"beendet, ohne einen Handshake zu schicken.")
                return                               # leere Verbindung: Portprüfung, kein Fehler
            puffer += stueck
            if len(puffer) > ERSTES_PAKET_MAX:
                self._zaehl("fehler")
                self.melder("fehler", f"{ip} schickt mehr als {ERSTES_PAKET_MAX} Byte, "
                                      f"ohne dass ein Handshake erkennbar ist.")
                return
            if puffer[0] == ALTER_PING:
                self._alter_ping(klient, puffer, ip, kurz, frist_ende)
                return
            try:
                handshake = zerlege_handshake(puffer)
            except Unvollstaendig:
                continue
            except Protokollfehler as exc:
                self._zaehl("fehler")
                self.melder("fehler", f"{ip} spricht kein Minecraft: {exc}")
                return
            self._verteile(klient, puffer, handshake, ip, kurz)
            return

    def _alter_ping(self, klient: socket.socket, puffer: bytes, ip: str, kurz: str,
                    frist_ende: float) -> None:
        """Ping vor 1.7: höflich mit derselben Meldung antworten – oder weiterleiten, wenn die
        Adresse bekannt ist (die Fassung 1.6 schickt sie mit)."""
        ende = min(frist_ende, time.monotonic() + ANTWORT_FRIST)
        ergebnis: tuple[str, int] | None = None
        while True:
            try:
                ergebnis = zerlege_alten_ping(puffer)
            except Protokollfehler:
                ergebnis = ("", 0)
            if ergebnis is not None:
                break
            rest = ende - time.monotonic()
            if rest <= 0:
                ergebnis = ("", 0)
                break
            try:
                klient.settimeout(rest)
                stueck = klient.recv(4096)
            except socket.timeout:
                ergebnis = ("", 0)
                break
            except OSError:
                return
            if not stueck:
                ergebnis = ("", 0)
                break
            puffer += stueck
            if len(puffer) > ERSTES_PAKET_MAX:
                ergebnis = ("", 0)
                break

        adresse_roh, _port = ergebnis
        adresse = bereinige_adresse(adresse_roh)
        route = self.routen.finde(adresse_roh)
        if route is not None:
            # Der Server selbst kann den alten Ping besser beantworten (Spielerzahl, MOTD).
            self._reiche_weiter(klient, puffer, route, adresse, ip, kurz, zustand=ZUSTAND_STATUS,
                                protokoll=-1, alter_ping=True)
            return
        self._zaehl("alter_ping")
        self.melder("verteiler", f"Alter Ping (vor 1.7) von {kurz}"
                                 f"{f' für „{adresse}“' if adresse else ' ohne Adresse'} – "
                                 f"höflich abgewiesen.")
        sende(klient, alter_ping_paket(MELDUNG_UNBEKANNT))
        self._beende_sanft(klient)

    # ------------------------------------------------------------------ verteilen

    def _verteile(self, klient: socket.socket, puffer: bytes, handshake: Handshake,
                  ip: str, kurz: str) -> None:
        adresse = bereinige_adresse(handshake.adresse)
        zusatz = "\x00" in str(handshake.adresse)
        route = self.routen.finde(handshake.adresse)
        if route is None:
            self._zaehl("unbekannt")
            self.melder("abgewiesen",
                        f"Unbekannte Adresse „{adresse or '(leer)'}“ von {kurz} "
                        f"(zustand={handshake.zustand}, protokoll={handshake.protokoll}"
                        f"{', mit Zusatz' if zusatz else ''}).")
            self._antworte_unbekannt(klient, puffer, handshake)
            return
        self._reiche_weiter(klient, puffer, route, adresse, ip, kurz,
                           zustand=handshake.zustand, protokoll=handshake.protokoll)

    def _antworte_unbekannt(self, klient: socket.socket, puffer: bytes,
                            handshake: Handshake) -> None:
        """Deutsche Meldung statt einer Weiterleitung."""
        if handshake.ist_status:
            self._antworte_status(klient, puffer, handshake, MELDUNG_UNBEKANNT)
        else:
            sende(klient, trenn_paket(MELDUNG_UNBEKANNT))
            self._beende_sanft(klient)

    def _antworte_status(self, klient: socket.socket, puffer: bytes, handshake: Handshake,
                         meldung: str) -> None:
        """Statusabfrage selbst beantworten: Statusantwort, danach die Laufzeitmessung."""
        rest = bytes(puffer[handshake.laenge:])
        # Die Statusanfrage (0x00, leer) kommt oft im selben Häppchen; sonst kurz nachlesen.
        if not rest:
            rest = self._lies_kurz(klient)
        sende(klient, status_paket(meldung, handshake.protokoll))
        nutzlast = self._finde_ping(rest)
        if nutzlast is None:
            nutzlast = self._finde_ping(self._lies_kurz(klient))
        if nutzlast is not None:
            sende(klient, ping_paket(nutzlast))
        self._beende_sanft(klient)

    def _lies_kurz(self, klient: socket.socket) -> bytes:
        """Kurz nachlesen, was der Client noch geschickt hat (blockiert höchstens ANTWORT_FRIST)."""
        try:
            klient.settimeout(ANTWORT_FRIST)
            return klient.recv(1024)
        except (socket.timeout, OSError):
            return b""

    @staticmethod
    def _finde_ping(daten: bytes) -> bytes | None:
        """Aus den Bytes die 8 Byte der Laufzeitmessung (Paket 0x01) holen, wenn sie da sind."""
        pos = 0
        while pos < len(daten):
            try:
                laenge, p = lese_varint(daten, pos)
            except (Unvollstaendig, Protokollfehler):
                return None
            if laenge <= 0 or p + laenge > len(daten):
                return None
            rumpf = daten[p:p + laenge]
            try:
                kennziffer, q = lese_varint(rumpf, 0)
            except (Unvollstaendig, Protokollfehler):
                return None
            if kennziffer == PAKET_PING and len(rumpf) - q >= 8:
                return bytes(rumpf[q:q + 8])
            pos = p + laenge
        return None

    def _beende_sanft(self, klient: socket.socket) -> None:
        """Erst das Senden beenden, damit der Client die Antwort noch liest, dann schließen."""
        try:
            klient.shutdown(socket.SHUT_WR)
        except OSError:
            return
        try:
            klient.settimeout(ANTWORT_FRIST)
            klient.recv(256)
        except (socket.timeout, OSError):
            pass

    # ------------------------------------------------------------------ weiterreichen

    def _reiche_weiter(self, klient: socket.socket, puffer: bytes, route: Route, adresse: str,
                       ip: str, kurz: str, *, zustand: int, protokoll: int,
                       alter_ping: bool = False) -> None:
        if route.port and route.port == self.eigener_port:
            # Sonst würde der Verteiler sich selbst anrufen, bis keine Sockets mehr übrig sind.
            self._zaehl("fehler")
            self.melder("fehler", f"Der Eintrag „{route.name}“ zeigt mit Port {route.port} auf "
                                  f"den Verteiler selbst – das wäre eine Schleife. Er wird nicht "
                                  f"weitergeleitet.")
            self._sage_ab(klient, puffer, zustand, protokoll, MELDUNG_AUS, alter_ping)
            return
        grenze = route.grenze or self.grenze
        klage = self.zaehler.belege(route.name, grenze)
        if klage:
            self._zaehl("abgewiesen")
            self.melder("abgewiesen", f"{kurz} für „{adresse}“ abgewiesen: {klage}")
            self._sage_ab(klient, puffer, zustand, protokoll, MELDUNG_VOLL, alter_ping)
            return

        ziel = None
        try:
            try:
                ziel = socket.create_connection((self.ziel_host, route.port),
                                                timeout=self.ziel_frist)
            except OSError as exc:
                self._zaehl("aus")
                self.melder("abgewiesen", f"„{route.name}“ ({self.ziel_host}:{route.port}) "
                                          f"nimmt keine Verbindung an ({exc}) – {kurz} bekommt "
                                          f"eine Meldung.")
                self._sage_ab(klient, puffer, zustand, protokoll, MELDUNG_AUS, alter_ping)
                return
            try:
                ziel.settimeout(None)
                ziel.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            if not sende(ziel, puffer):              # gelesene Bytes unverändert zuerst
                self._zaehl("fehler")
                self.melder("fehler", f"„{route.name}“ hat die Verbindung von {ip} sofort "
                                      f"wieder geschlossen.")
                return
            self._zaehl("weitergeleitet")
            was = "alter ping" if alter_ping else ("status" if zustand == ZUSTAND_STATUS else "beitritt")
            self.melder("weiter", f"„{adresse or route.name}“ -> {self.ziel_host}:{route.port} "
                                  f"[{was}] klient={kurz}"
                                  f"{f' instanz={route.instanz}' if route.instanz else ''} "
                                  f"offen={self.zaehler.offen(route.name)}")
            try:
                klient.settimeout(None)
            except OSError:
                pass
            rueck = threading.Thread(target=_pumpe, args=(ziel, klient),
                                     name=f"zurueck-{route.name}", daemon=True)
            rueck.start()
            _pumpe(klient, ziel)
            rueck.join(ABKLINGZEIT)
        finally:
            _zu(ziel)                                # schließt auch einen hängenden Rückweg auf
            self.zaehler.gib_frei(route.name)

    def _sage_ab(self, klient: socket.socket, puffer: bytes, zustand: int, protokoll: int,
                 meldung: str, alter_ping: bool) -> None:
        """Absage mit deutscher Meldung, passend zum Zustand."""
        if alter_ping:
            sende(klient, alter_ping_paket(meldung))
        elif zustand == ZUSTAND_STATUS:
            ersatz = Handshake(protokoll=protokoll, adresse="", port=0,
                               zustand=ZUSTAND_STATUS, laenge=len(puffer))
            self._antworte_status(klient, puffer, ersatz, meldung)
            return
        else:
            sende(klient, trenn_paket(meldung))
        self._beende_sanft(klient)

    # ------------------------------------------------------------------ Horchen

    def _binde(self, familie: int, wohin: str, port: int, *, doppelt: bool) -> socket.socket | None:
        """Einen lauschenden Socket anlegen. ``None``, wenn das nicht geht (Grund kommt ins
        Protokoll). ``doppelt`` = ein IPv6-Socket, der auch IPv4-Verbindungen annimmt."""
        horch = None
        try:
            horch = socket.socket(familie, socket.SOCK_STREAM)
            horch.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if familie == socket.AF_INET6:
                try:
                    horch.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if doppelt else 1)
                except OSError as exc:
                    if doppelt:
                        raise OSError(f"IPv4 über IPv6 ist hier nicht möglich: {exc}") from exc
            horch.bind((wohin, int(port)))
            horch.listen(256)
            horch.setblocking(False)
        except OSError as exc:
            _zu(horch)
            self.melder("verteiler", f"Auf {wohin}:{port} kann nicht gelauscht werden ({exc}).")
            return None
        echt = horch.getsockname()
        art = "IPv4 und IPv6" if doppelt else ("IPv6" if familie == socket.AF_INET6 else "IPv4")
        self.melder("verteiler", f"Es wird auf {echt[0]}:{echt[1]} gelauscht ({art}).")
        return horch

    def horcher(self, bind: str, port: int) -> list[socket.socket]:
        """Lauschende Sockets anlegen: möglichst einer für IPv4 **und** IPv6."""
        offen: list[socket.socket] = []
        if not bind or bind in ("*", "::", "0.0.0.0"):
            # Ein IPv6-Socket ohne V6ONLY nimmt unter Linux auch IPv4-Verbindungen an.
            doppelstapel = self._binde(socket.AF_INET6, "::", port, doppelt=True)
            if doppelstapel is not None:
                offen.append(doppelstapel)
            else:
                # Getrennte Sockets. Bei Port 0 gilt der Port des ersten auch für den zweiten.
                for familie, wohin in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
                    einzeln = self._binde(familie, wohin, port, doppelt=False)
                    if einzeln is not None:
                        offen.append(einzeln)
                        port = int(einzeln.getsockname()[1])
        else:
            try:
                familie = socket.AF_INET6 if ipaddress.ip_address(bind).version == 6 \
                    else socket.AF_INET
            except ValueError:
                familie = socket.AF_INET
            einzeln = self._binde(familie, bind, port, doppelt=False)
            if einzeln is not None:
                offen.append(einzeln)
        if not offen:
            raise OSError(f"Auf {bind or 'alle Adressen'}:{port} kann nicht gelauscht werden.")
        self.eigener_port = int(offen[0].getsockname()[1])
        self._horcher = offen
        return offen

    def bediene(self, horcher: list[socket.socket] | None = None) -> None:
        """Annahmeschleife, bis ``stoppe()`` gerufen wird."""
        sockets = horcher if horcher is not None else self._horcher
        if not sockets:
            raise RuntimeError("Es wurde noch kein lauschender Socket angelegt.")
        with selectors.DefaultSelector() as waehler:
            for horch in sockets:
                waehler.register(horch, selectors.EVENT_READ)
            while not self._halt.is_set():
                for schluessel, _ereignis in waehler.select(timeout=0.5):
                    horch = schluessel.fileobj
                    try:
                        klient, gegenstelle = horch.accept()            # type: ignore[union-attr]
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError as exc:
                        if self._halt.is_set():
                            return
                        self.melder("fehler", f"Eine Verbindung konnte nicht angenommen "
                                              f"werden: {exc}")
                        continue
                    threading.Thread(target=self.bearbeite, args=(klient, gegenstelle),
                                     name="verbindung", daemon=True).start()

    def stoppe(self) -> None:
        self._halt.set()

    def schliesse(self) -> None:
        for horch in self._horcher:
            _zu(horch)
        self._horcher = []


# --------------------------------------------------------------------------- Dienst

def zahlen_faden(verteiler: Verteiler, abstand: int, halt: threading.Event) -> None:
    """Alle ``abstand`` Sekunden eine Zeile mit den Zählern (hilft beim Nachsehen im Protokoll)."""
    while not halt.wait(abstand):
        verteiler.melder("zahlen", verteiler.stand_text())


def main(argv: list[str] | None = None) -> int:
    argumente = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argumente:
        print(f"mcsm-router {VERSION}")
        return 0
    if "--hilfe" in argumente or "--help" in argumente:
        print(__doc__ or "")
        return 0
    nur_pruefen = "--pruefen" in argumente or "--check" in argumente

    bind = _text("MCSM_ROUTER_BIND", "")
    port = _zahl("MCSM_ROUTER_PORT", STANDARD_PORT, kleinster=0, groesster=65535)
    domain = _text("MCSM_ROUTER_DOMAIN", STANDARD_DOMAIN)
    ziel_host = _text("MCSM_ROUTER_TARGET", STANDARD_ZIEL)
    grenze = _zahl("MCSM_ROUTER_LIMIT", GRENZE_JE_ZIEL, kleinster=1, groesster=100000)
    gesamt = _zahl("MCSM_ROUTER_LIMIT_TOTAL", GRENZE_GESAMT, kleinster=1, groesster=1000000)
    frist = _kommazahl("MCSM_ROUTER_TIMEOUT", HANDSHAKE_FRIST)
    stats = _zahl("MCSM_ROUTER_STATS", 300, kleinster=0, groesster=86400)

    routen = Routen(routen_pfad(), domain=domain)
    verteiler = Verteiler(routen, ziel_host=ziel_host, grenze=grenze,
                          gesamt_grenze=gesamt, frist=frist, eigener_port=port)

    if nur_pruefen:
        routen.aktualisiere(erzwingen=True)
        eintraege = routen.eintraege()
        print(f"mcsm-router {VERSION}: Tabelle {routen.pfad} – {len(eintraege)} Einträge, "
              f"Domain {domain or '(keine)'}, Ziel {ziel_host}, Grenze {grenze} je Server.")
        for name in sorted(eintraege):
            eintrag = eintraege[name]
            print(f"  {name} -> {ziel_host}:{eintrag.port}"
                  f"{f' (Instanz {eintrag.instanz})' if eintrag.instanz else ''}")
        return 0

    melde("verteiler", f"mcsm-router {VERSION} startet (Domain {domain or '(keine)'}, "
                       f"Tabelle {routen.pfad}, Ziel {ziel_host}, Grenze {grenze} je Server, "
                       f"{gesamt} insgesamt, Handshake-Frist {frist:.0f} s).")
    routen.aktualisiere(erzwingen=True)

    halt = threading.Event()

    def beenden(nummer, _rahmen) -> None:
        melde("verteiler", f"Signal {nummer} – der Verteiler hört auf. {verteiler.stand_text()}")
        halt.set()
        verteiler.stoppe()
        verteiler.schliesse()

    for signalnummer in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signalnummer, beenden)
        except (ValueError, OSError):
            pass

    try:
        verteiler.horcher(bind, port)
    except OSError as exc:
        melde("fehler", f"Start nicht möglich: {exc}")
        return 1

    if stats:
        threading.Thread(target=zahlen_faden, args=(verteiler, stats, halt),
                         name="zahlen", daemon=True).start()
    try:
        verteiler.bediene()
    except KeyboardInterrupt:
        pass
    finally:
        halt.set()
        verteiler.stoppe()
        verteiler.schliesse()
        melde("verteiler", f"Der Verteiler ist beendet. {verteiler.stand_text()}")
        schliesse_protokoll()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
