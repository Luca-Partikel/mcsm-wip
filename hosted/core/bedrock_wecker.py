# -*- coding: utf-8 -*-
"""Der Bedrock-Wecker: RakNet auf UDP für gehostete Server, die gerade schlafen.

Für **Java** gibt es das alles schon: Der Verteiler (``hosted/router.py``) sitzt auf TCP 25565,
beantwortet den Status-Ping eines schlafenden Servers wie ein echter Server und weckt ihn beim
Beitritt. Konsolenspieler kommen aber über **Bedrock** herein – und Bedrock ist UDP mit RakNet,
je Instanz auf einem eigenen Port (Geyser, z. B. 19132). Solange der Server schlief, lauschte
dort **nichts**: Der Eintrag in der Xbox-Freundesliste war zwar da, aber jeder Beitrittsversuch
lief ins Leere, und niemand konnte den Server anpingen oder starten.

Dieses Modul schließt die Lücke. Es kennt nur zwei Pakete:

* **Unconnected Ping** (erstes Byte ``0x01``) – wird mit einem **Unconnected Pong** (``0x1C``)
  beantwortet: gespiegelter Zeitstempel, Server-GUID, MAGIC und die Auskunft als
  längencodierte Zeichenkette. Der Spieler sieht in der Freundesliste einen gewöhnlichen
  Eintrag mit ``0/<max>`` und in Zeile 2 im Klartext, was los ist.
* **Open Connection Request 1** (erstes Byte ``0x05``) – jemand will wirklich verbinden. Dann
  wird die Instanz geweckt, genau wie beim Java-Verteiler: dieselbe Prüfung (Pass, Kontingent,
  Platz) und dieselbe Sperre gegen Mehrfachstarts. Geantwortet wird darauf **nicht**: Eine
  saubere Absage mit Begründung gibt es auf RakNet-Ebene nicht (``0x19`` würde die Konsole
  „veralteter Server“ melden – falsch und verwirrend). Die Auskunft im Ping ist der Weg.

**Der heikelste Punkt ist der Port.** Geyser bindet denselben UDP-Port, den der Wecker hält.
Zwei Programme können ihn nicht gleichzeitig haben, und Geyser gibt beim fehlgeschlagenen Bind
einfach auf – dann käme für die ganze Laufzeit des Servers kein Bedrock-Spieler mehr herein.
Deshalb gilt ohne Ausnahme:

1. **Erst schließen, dann starten.** `Wecker.freigeben` schließt den Socket, wartet, bis der
   Port wirklich wieder bindbar ist, und **sperrt** die Instanz für eine Weile gegen ein
   erneutes Binden. Der Daemon ruft das vor der Portvergabe auf – sonst hielte schon
   ``PortPool.allocate`` den eigenen Wecker-Socket für „belegt“ und gäbe Geyser einen anderen
   Port.
2. **Nach dem Stopp erst binden, wenn der Serverprozess wirklich weg ist.** Der Daemon hebt die
   Sperre im Ende-Rückruf des Runners auf (da ist der Prozess beendet); ein Bind, der trotzdem
   noch scheitert, wird beim nächsten Abgleich einfach wiederholt.
3. **Nie Wiederverwendung erlauben.** Der Socket bekommt weder ``SO_REUSEADDR`` noch
   ``SO_REUSEPORT``. Ein fehlgeschlagener Bind ist hier die gewünschte Schutzwirkung: er sagt,
   dass der Port jemand anderem gehört (Geyser), und dann hat der Wecker dort nichts zu suchen.

Zwischen Weckruf und dem Moment, in dem Geyser den Port übernimmt, antwortet also **niemand**
auf dem Bedrock-Port. Das ist Absicht und die einzige sichere Reihenfolge; die Konsole hat in
genau diesem Augenblick die Auskunft „Server startet gerade – bitte in etwa einer Minute noch
einmal verbinden“ bekommen.

**Nur RakNet, also nur Crossplay (Geyser).** Ein Bedrock-Dedicated-Server mit ``nethernet``
lauscht gar nicht auf RakNet – dort läuft die Verbindung über die Xbox-Sitzung (WebRTC). Für
solche Instanzen gibt es hier nichts zu tun, und der Daemon lässt sie aus.

Alles Standardbibliothek. Die Paketfunktionen sind reine Funktionen ohne Socket – die
Selbsttests prüfen sie mit echten Byte-Folgen.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import struct
import threading
import time
import traceback
from typing import Callable, NamedTuple

# --------------------------------------------------------------------------- Protokoll

#: RakNet-Erkennungszeichen („offline message data id“) – steht in jedem verbindungslosen Paket.
MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")

#: Unconnected Ping (Client fragt: „bist du da?“).
ID_PING = 0x01
#: Dasselbe, nur von älteren Clients (``ID_UNCONNECTED_PING_OPEN_CONNECTIONS``).
ID_PING_OFFEN = 0x02
#: Unconnected Pong (unsere Antwort mit der Auskunft).
ID_PONG = 0x1C
#: Open Connection Request 1 – der erste Schritt eines echten Verbindungsaufbaus.
ID_VERBINDUNG_1 = 0x05

#: So viel Byte liest der Wecker höchstens aus einem Paket (ein Ping ist 33 Byte; ein
#: Verbindungsversuch ist mit MTU-Füllung bis ~1500 Byte groß).
MAX_PAKET = 2048

#: Kürzester gültiger Ping: Kennziffer + Zeitstempel + MAGIC (die Client-GUID fehlt bei
#: manchen Clients).
PING_MIN = 1 + 8 + len(MAGIC)
#: Kürzester gültiger Verbindungsversuch: Kennziffer + MAGIC + Protokollfassung.
VERBINDUNG_MIN = 1 + len(MAGIC) + 1


class Protokollfehler(ValueError):
    """Das kann kein RakNet-Paket dieser Art sein – stillschweigend wegwerfen."""


class Ping(NamedTuple):
    """Ein Unconnected Ping, wie er auf dem Port ankommt."""

    zeit: int                   # Zeitstempel des Clients, wird gespiegelt
    klient_guid: int            # 0, wenn der Client keine mitschickt


class Pong(NamedTuple):
    """Ein Unconnected Pong – das, was ein echter Server (Geyser) antwortet."""

    zeit: int
    guid: int
    auskunft: str


class Auskunft(NamedTuple):
    """Die Felder der längencodierten Zeichenkette im Pong.

    Reihenfolge laut Bedrock-Protokoll::

        MCPE;<Zeile 1>;<Protokoll>;<Version>;<online>;<max>;<GUID>;<Zeile 2>;
        <Spielmodus>;1;<Port v4>;<Port v6>;
    """

    zeile1: str
    protokoll: int
    version: str
    online: int
    max_spieler: int
    guid: int
    zeile2: str
    spielmodus: str
    port_v4: int
    port_v6: int


def _feld(text) -> str:
    """Ein Feld der Auskunft säubern.

    Das Trennzeichen ist ``;`` – steht es im Servernamen, verrutschte die ganze Auskunft und die
    Konsole zeigte Unsinn an. Steuerzeichen fliegen mit heraus, mehrere Leerzeichen werden zu
    einem (ein Zeilenumbruch wird sonst zu einer Lücke), die Länge wird begrenzt.
    """
    roh = str(text if text is not None else "")
    return re.sub(r"[\x00-\x1f;\s]+", " ", roh).strip()[:96]


def baue_auskunft(*, zeile1: str, protokoll: int, version: str, online: int, max_spieler: int,
                  guid: int, zeile2: str, spielmodus: str, port_v4: int,
                  port_v6: int | None = None) -> str:
    """Die Auskunft für den Pong zusammensetzen (mit ``;`` am Ende, wie es echte Server tun)."""
    if port_v6 is None:
        port_v6 = int(port_v4)
    teile = [
        "MCPE",
        _feld(zeile1) or "Minecraft Server",
        str(max(0, int(protokoll))),
        _feld(version) or "1.0.0",
        str(max(0, int(online))),
        str(max(1, int(max_spieler))),
        str(int(guid) & 0xFFFFFFFFFFFFFFFF),
        _feld(zeile2),
        _feld(spielmodus) or "Survival",
        "1",
        str(int(port_v4)),
        str(int(port_v6)),
    ]
    return ";".join(teile) + ";"


def zerlege_auskunft(text: str) -> Auskunft:
    """Die Auskunft eines echten Servers zerlegen (so lernt der Daemon Fassung und Protokoll)."""
    teile = str(text or "").split(";")
    if len(teile) < 7 or teile[0].strip().upper() not in ("MCPE", "MCEE"):
        raise Protokollfehler("Das ist keine Bedrock-Auskunft (kein „MCPE“ am Anfang).")

    def zahl(pos: int, vorgabe: int = 0) -> int:
        try:
            return int(str(teile[pos]).strip())
        except (IndexError, ValueError):
            return vorgabe

    def wort(pos: int) -> str:
        try:
            return str(teile[pos]).strip()
        except IndexError:
            return ""

    return Auskunft(zeile1=wort(1), protokoll=zahl(2), version=wort(3), online=zahl(4),
                    max_spieler=zahl(5, 10), guid=zahl(6), zeile2=wort(7),
                    spielmodus=wort(8) or "Survival", port_v4=zahl(10), port_v6=zahl(11))


def zerlege_ping(daten: bytes) -> Ping:
    """Einen Unconnected Ping lesen. `Protokollfehler`, wenn es keiner ist."""
    if len(daten) < PING_MIN:
        raise Protokollfehler(f"Ein Ping ist mindestens {PING_MIN} Byte lang, hier sind es "
                              f"{len(daten)}.")
    if daten[0] not in (ID_PING, ID_PING_OFFEN):
        raise Protokollfehler(f"Das erste Byte ist 0x{daten[0]:02x}, erwartet war 0x01.")
    if daten[9:9 + len(MAGIC)] != MAGIC:
        raise Protokollfehler("Das RakNet-Erkennungszeichen (MAGIC) fehlt.")
    zeit = struct.unpack_from(">Q", daten, 1)[0]
    rest = daten[9 + len(MAGIC):]
    guid = struct.unpack_from(">Q", rest)[0] if len(rest) >= 8 else 0
    return Ping(zeit=int(zeit), klient_guid=int(guid))


def baue_ping(zeit: int, guid: int) -> bytes:
    """Einen Unconnected Ping bauen – der Daemon fragt damit den **laufenden** Server."""
    return (bytes([ID_PING]) + struct.pack(">Q", int(zeit) & 0xFFFFFFFFFFFFFFFF) + MAGIC
            + struct.pack(">Q", int(guid) & 0xFFFFFFFFFFFFFFFF))


def baue_pong(zeit: int, guid: int, auskunft: str) -> bytes:
    """Einen Unconnected Pong bauen: Zeitstempel gespiegelt, GUID, MAGIC, Auskunft (uint16)."""
    text = str(auskunft or "").encode("utf-8")[:0xFFFF]
    return (bytes([ID_PONG])
            + struct.pack(">Q", int(zeit) & 0xFFFFFFFFFFFFFFFF)
            + struct.pack(">Q", int(guid) & 0xFFFFFFFFFFFFFFFF)
            + MAGIC
            + struct.pack(">H", len(text)) + text)


def zerlege_pong(daten: bytes) -> Pong:
    """Einen Unconnected Pong lesen (Antwort des laufenden Servers)."""
    kopf = 1 + 8 + 8 + len(MAGIC) + 2
    if len(daten) < kopf:
        raise Protokollfehler(f"Ein Pong ist mindestens {kopf} Byte lang, hier sind es "
                              f"{len(daten)}.")
    if daten[0] != ID_PONG:
        raise Protokollfehler(f"Das erste Byte ist 0x{daten[0]:02x}, erwartet war 0x1c.")
    if daten[17:17 + len(MAGIC)] != MAGIC:
        raise Protokollfehler("Das RakNet-Erkennungszeichen (MAGIC) fehlt.")
    zeit, guid = struct.unpack_from(">QQ", daten, 1)
    laenge = struct.unpack_from(">H", daten, 17 + len(MAGIC))[0]
    text = daten[kopf:kopf + laenge].decode("utf-8", "replace")
    return Pong(zeit=int(zeit), guid=int(guid), auskunft=text)


def ist_verbindungsversuch(daten: bytes) -> bool:
    """Ist das ein „Open Connection Request 1“ – will hier wirklich jemand herein?"""
    return (len(daten) >= VERBINDUNG_MIN and daten[0] == ID_VERBINDUNG_1
            and daten[1:1 + len(MAGIC)] == MAGIC)


def guid_fuer(instanz_id: str) -> int:
    """Eine feste Server-GUID je Instanz.

    Sie muss über die Laufzeit **gleich** bleiben: Wechselt sie zwischen zwei Pings, hält die
    Konsole das für einen anderen Server und legt einen zweiten Eintrag an. Aus der
    Instanzkennung abgeleitet, damit sie auch einen Neustart des Dienstes übersteht. Das
    oberste Bit bleibt frei – die Auskunft nennt die GUID als Dezimalzahl, und manche Clients
    lesen sie vorzeichenbehaftet.
    """
    roh = hashlib.sha256(f"mcsm-bedrock-wecker:{instanz_id}".encode("utf-8")).digest()
    return struct.unpack_from(">Q", roh)[0] & 0x7FFFFFFFFFFFFFFF


# --------------------------------------------------------------------------- Sichtbare Texte

#: Genau wie beim Java-Verteiler: ein schlafender Server sieht normal aus, keine Fehlerfarbe.
ZEILE2_AUS = "Server ist ausgeschaltet – tritt bei, um ihn zu starten"
#: Nach einem Weckruf – damit der Spieler in der Freundesliste sofort sieht, was los ist.
ZEILE2_STARTET = "Server startet gerade – bitte in etwa einer Minute noch einmal verbinden"
#: Ruhezustand ist abgeschaltet: ein Beitritt weckt hier nichts (wie ``MELDUNG_AUS``).
ZEILE2_LAEUFT_NICHT = "Dieser Server läuft gerade nicht"

#: Anzeigename im Stil des Begleit-Plugins.
NAME_VORSATZ = "✦ MCSM ¦ "


def zeile1_fuer(name) -> str:
    """Zeile 1 der Auskunft: ``✦ MCSM ¦ <Name>`` (gesäubert und begrenzt)."""
    rein = _feld(name)
    return NAME_VORSATZ + (rein or "Minecraft Server")


# --------------------------------------------------------------------------- Vorgaben

#: Umgebungsvariablen für die Werte, die der Wecker nicht wissen kann, solange er noch keinen
#: laufenden Server gefragt hat (siehe `frage_server`).
PROTOKOLL_ENV = "MCSM_BEDROCK_PROTOKOLL"
VERSION_ENV = "MCSM_BEDROCK_VERSION"
BIND_ENV = "MCSM_BEDROCK_BIND"

#: Vorgaben, bis die echten Werte vom laufenden Server gelernt sind. Sie sind nur Kosmetik: Die
#: Konsole zeigt damit unter Umständen „veralteter Server“ am Eintrag an, der Eintrag selbst ist
#: aber da und der Beitritt weckt trotzdem.
PROTOKOLL_VORGABE = 800
VERSION_VORGABE = "1.21.100"
SPIELMODUS_VORGABE = "Survival"


def protokoll_vorgabe() -> int:
    try:
        wert = int((os.environ.get(PROTOKOLL_ENV) or "").strip() or PROTOKOLL_VORGABE)
    except ValueError:
        return PROTOKOLL_VORGABE
    return wert if 0 < wert < 1_000_000 else PROTOKOLL_VORGABE


def version_vorgabe() -> str:
    roh = (os.environ.get(VERSION_ENV) or "").strip()
    return _feld(roh) or VERSION_VORGABE


def bind_host() -> str:
    """Adresse, auf der gelauscht wird.

    IPv4 genügt: Die Basisdomain zeigt auf die feste IPv4 des Root-Servers, und die Konsole des
    Freundes baut die Verbindung dorthin auf. Ein Doppel-Socket (IPv6 mit IPv4-Annahme) würde
    hier nur die Bindprüfung gegen Geyser unzuverlässig machen.
    """
    return (os.environ.get(BIND_ENV) or "").strip() or "0.0.0.0"


# --------------------------------------------------------------------------- Zeiten

#: Zeitlimit eines ``recvfrom`` – so schnell merkt der Faden, dass er aufhören soll.
LESE_FRIST = 0.5
#: So lange steht „Server startet gerade“ in Zeile 2, und so lange wird kein zweiter Weckruf
#: ausgelöst. RakNet schickt den Verbindungsversuch mehrmals je Sekunde – ohne diese Sperre
#: liefen Dutzende Weckrufe los (die Sperre im Dienst fängt sie ab, aber jeder kostet einen Faden).
STARTET_TTL = 120.0
#: So lange bleibt der Grund einer abgelehnten Weckung in Zeile 2 stehen.
ABLEHNUNG_TTL = 60.0
#: So lange wartet `freigeben` darauf, dass der Port wirklich wieder bindbar ist.
FREI_FRIST = 5.0
#: So lange bleibt eine Instanz nach `freigeben` gegen erneutes Binden gesperrt (Sicherheitsnetz,
#: falls der Daemon das Entsperren einmal nicht schafft: dann bindet der Wecker eben später).
SPERRE_TTL = 180.0


# --------------------------------------------------------------------------- Ziele

class Ziel(NamedTuple):
    """Ein schlafender Server, für den der Wecker den Bedrock-Port hält."""

    instanz: str
    port: int
    zeile1: str
    zeile2: str
    max_spieler: int = 10
    protokoll: int = PROTOKOLL_VORGABE
    version: str = VERSION_VORGABE
    spielmodus: str = SPIELMODUS_VORGABE
    #: Darf ein Beitritt diesen Server starten? (Ruhezustand eingeschaltet)
    wecken: bool = True


def _warte_bis_frei(port: int, host: str, frist: float) -> bool:
    """Warten, bis sich der UDP-Port wieder binden lässt. ``True``, wenn er frei ist."""
    ende = time.monotonic() + max(0.0, float(frist))
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((host, int(port)))
            return True
        except OSError:
            pass
        finally:
            probe.close()
        if time.monotonic() >= ende:
            return False
        time.sleep(0.1)


class Horcher:
    """Ein UDP-Socket für **einen** schlafenden Server, mit eigenem Lesefaden."""

    def __init__(self, ziel: Ziel, *, wecken: Callable[[str], tuple] | None = None,
                 melder: Callable[[str], None] | None = None, host: str = "",
                 ablehnung_merken: Callable[[str, str], None] | None = None) -> None:
        self.ziel = ziel
        self.host = host or bind_host()
        self.guid = guid_fuer(ziel.instanz)
        self._wecken = wecken
        self._melder = melder or (lambda text: None)
        self._ablehnung_merken = ablehnung_merken or (lambda instanz, text: None)
        self._sock: socket.socket | None = None
        self._faden: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._startet_seit = 0.0
        self._ablehnung = ("", 0.0)
        #: Zähler fürs Protokoll und für die Selbsttests.
        self.pings = 0
        self.weckrufe = 0

    # ------------------------------------------------------------------ Leben

    @property
    def port(self) -> int:
        return int(self.ziel.port)

    @property
    def laeuft(self) -> bool:
        return self._sock is not None and not self._stop.is_set()

    def binde(self) -> str:
        """Socket öffnen und den Lesefaden starten. Rückgabe: leer = in Ordnung, sonst der Grund.

        **Ohne** ``SO_REUSEADDR``/``SO_REUSEPORT``: Scheitert der Bind, hält jemand anderes den
        Port – bei einem Bedrock-Port ist das Geyser, und dann hat der Wecker dort nichts zu
        suchen. Das ist die Schutzwirkung, nicht ein Hindernis.
        """
        if self.laeuft:
            return ""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            return str(exc)
        sock.settimeout(LESE_FRIST)
        self._stop.clear()
        self._sock = sock
        self._faden = threading.Thread(target=self._lauf, args=(sock,), daemon=True,
                                       name=f"bedrock-{self.ziel.instanz}-{self.port}")
        self._faden.start()
        return ""

    def aktualisiere(self, ziel: Ziel) -> None:
        """Neue Angaben übernehmen (Name, Spielerplätze, Zeile 2). Der Port bleibt."""
        if int(ziel.port) != self.port:
            raise ValueError("Ein Horcher wechselt nicht den Port – er wird neu angelegt.")
        with self._lock:
            self.ziel = ziel

    def schliesse(self, *, frist: float = 2.0) -> bool:
        """Socket zumachen und auf den Lesefaden warten. ``True``, wenn der Faden weg ist."""
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()            # weckt ein hängendes recvfrom sofort auf
            except OSError:
                pass
        faden, self._faden = self._faden, None
        if faden is not None and faden.is_alive():
            faden.join(max(0.1, float(frist)))
            return not faden.is_alive()
        return True

    # ------------------------------------------------------------------ Zeile 2

    def startet_jetzt(self) -> None:
        """Merken, dass gerade geweckt wurde – Zeile 2 sagt das, bis der Port weg ist."""
        with self._lock:
            self._startet_seit = time.monotonic()
            self._ablehnung = ("", 0.0)
        self._ablehnung_merken(self.ziel.instanz, "")

    def abgelehnt(self, meldung: str, *, seit: float | None = None, merken: bool = True) -> None:
        """Der Weckruf ist abgelehnt worden – der Grund gehört in Zeile 2.

        ``merken`` gibt den Grund an den `Wecker` weiter. Der braucht ihn, weil dieser Horcher
        kurz nach dem Weckruf verschwindet (der Port geht an Geyser) und der nächste den Grund
        sonst nicht kennt – der Spieler bekäme dann wieder „Server ist ausgeschaltet“ zu sehen,
        obwohl der Start gerade gescheitert ist.
        """
        with self._lock:
            self._startet_seit = 0.0
            self._ablehnung = (str(meldung or ""),
                               time.monotonic() if seit is None else float(seit))
        if merken:
            self._ablehnung_merken(self.ziel.instanz, str(meldung or ""))

    def zeile2(self) -> str:
        """Was gerade in Zeile 2 der Auskunft steht (der jüngste Grund gewinnt)."""
        jetzt = time.monotonic()
        with self._lock:
            if self._startet_seit and jetzt - self._startet_seit <= STARTET_TTL:
                return ZEILE2_STARTET
            text, seit = self._ablehnung
            if text and jetzt - seit <= ABLEHNUNG_TTL:
                return text
            return self.ziel.zeile2

    def auskunft(self) -> str:
        with self._lock:
            ziel = self.ziel
        return baue_auskunft(zeile1=ziel.zeile1, protokoll=ziel.protokoll, version=ziel.version,
                             online=0, max_spieler=ziel.max_spieler, guid=self.guid,
                             zeile2=self.zeile2(), spielmodus=ziel.spielmodus,
                             port_v4=self.port)

    # ------------------------------------------------------------------ Lesefaden

    def _lauf(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                daten, gegenstelle = sock.recvfrom(MAX_PAKET)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Eine frühere Antwort ist auf eine geschlossene Gegenstelle getroffen und das
                # System meldet das hier nach („Port nicht erreichbar“). Kein Grund aufzuhören –
                # sonst verstummte der Wecker, weil ein einzelner Spieler das Spiel zugemacht hat.
                continue
            except OSError:
                break                   # Socket zu – der Faden ist fertig
            try:
                self.behandle(daten, gegenstelle, sock)
            except Exception:                                       # noqa: BLE001
                self._melder(f"Im Bedrock-Wecker von {self.ziel.instanz} "
                             f"(Port {self.port}):\n" + traceback.format_exc())

    def behandle(self, daten: bytes, gegenstelle, sock=None) -> bytes | None:
        """Ein Paket beantworten. Rückgabe: die gesendeten Byte (für die Selbsttests)."""
        if not daten:
            return None
        if daten[0] in (ID_PING, ID_PING_OFFEN):
            try:
                ping = zerlege_ping(daten)
            except Protokollfehler:
                return None
            antwort = baue_pong(ping.zeit, self.guid, self.auskunft())
            with self._lock:
                self.pings += 1
            if sock is not None:
                try:
                    sock.sendto(antwort, gegenstelle)
                except OSError:
                    return None
            return antwort
        if ist_verbindungsversuch(daten):
            self._weckruf()
            return None
        return None

    def _weckruf(self) -> None:
        """Jemand will wirklich herein – die Instanz wecken (in eigenem Faden).

        Der Start dauert Sekunden bis Minuten; solange muss der Lesefaden weiter Pings
        beantworten. Deshalb läuft der Weckruf daneben, und Zeile 2 sagt ab sofort „startet
        gerade“ – auch wenn der Port gleich an Geyser übergeht.
        """
        jetzt = time.monotonic()
        with self._lock:
            if self._startet_seit and jetzt - self._startet_seit <= STARTET_TTL:
                return                  # läuft schon – RakNet fragt mehrmals je Sekunde
            if not self.ziel.wecken:
                return                  # Ruhezustand aus: ein Beitritt weckt hier nichts
            self._startet_seit = jetzt
            self._ablehnung = ("", 0.0)
            self.weckrufe += 1
        if self._wecken is None:
            return
        threading.Thread(target=self._wecke_jetzt, daemon=True,
                         name=f"bedrock-weckruf-{self.ziel.instanz}").start()

    def _wecke_jetzt(self) -> None:
        instanz = self.ziel.instanz
        try:
            ok, meldung = self._wecken(instanz)
        except Exception:                                           # noqa: BLE001
            self._melder(f"Der Weckruf für {instanz} über den Bedrock-Port ist gescheitert:\n"
                         + traceback.format_exc())
            self.abgelehnt(ZEILE2_AUS)
            return
        if ok:
            self._melder(f"„{self.ziel.zeile1}“ ({instanz}) wurde über den Bedrock-Port "
                         f"{self.port} geweckt – eine Konsole wollte beitreten.")
            return
        self.abgelehnt(str(meldung or ZEILE2_AUS))
        self._melder(f"Der Beitritt einer Konsole auf Port {self.port} konnte {instanz} nicht "
                     f"wecken: {meldung}")


# --------------------------------------------------------------------------- Der Wecker

class Wecker:
    """Alle Horcher des Dienstes – höchstens einer je Instanz.

    Der Daemon ruft `abgleichen` mit der Liste der Server, die gerade schlafen, und `freigeben`,
    bevor er einen Server startet. Alles andere macht dieses Objekt selbst.
    """

    def __init__(self, *, wecken: Callable[[str], tuple] | None = None,
                 melder: Callable[[str], None] | None = None, host: str = "") -> None:
        self._wecken = wecken
        self._melder = melder or (lambda text: None)
        self._host = host
        self._horcher: dict[str, Horcher] = {}
        self._gesperrt: dict[str, float] = {}
        #: Schon gemeldete Bindfehler – damit das Protokoll nicht alle 15 Sekunden zuläuft.
        self._klagen: dict[str, str] = {}
        #: Der letzte Grund, aus dem eine Weckung scheiterte – je Instanz, mit Zeitpunkt. Er
        #: überlebt den Socket: Beim Weckruf geht der Port weg (der Server soll ja starten), und
        #: der Horcher, der danach wieder bindet, soll denselben Grund weiterreichen.
        self._ablehnungen: dict[str, tuple] = {}
        self._lock = threading.RLock()

    def _ablehnung_merken(self, instanz: str, text: str) -> None:
        """Den Grund einer gescheiterten Weckung ablegen – und dem gerade offenen Horcher geben.

        Beides ist nötig: Der Horcher, der den Weckruf ausgelöst hat, ist in der Zwischenzeit
        vielleicht schon geschlossen (der Port ging an Geyser) und ein neuer schon gebunden. Der
        soll den Grund sofort nennen und nicht wieder „Server ist ausgeschaltet“ melden.
        """
        with self._lock:
            jetzt = time.monotonic()
            if text:
                self._ablehnungen[str(instanz)] = (str(text), jetzt)
            else:
                self._ablehnungen.pop(str(instanz), None)
            offen = self._horcher.get(str(instanz))
        if text and offen is not None:
            offen.abgelehnt(str(text), seit=jetzt, merken=False)

    # ------------------------------------------------------------------ Abgleich

    def abgleichen(self, ziele) -> dict:
        """Die Horcher auf die Liste der schlafenden Server bringen.

        Rückgabe ``{"neu": [...], "weg": [...], "fehler": {instanz: grund}}`` – der Daemon
        schreibt das ins Protokoll, die Selbsttests prüfen es.
        """
        soll = {}
        for ziel in ziele or []:
            if int(ziel.port) > 0 and str(ziel.instanz):
                soll[str(ziel.instanz)] = ziel
        neu: list[str] = []
        weg: list[str] = []
        fehler: dict[str, str] = {}
        with self._lock:
            self._sperren_aufraeumen()
            for instanz in list(self._horcher):
                horcher = self._horcher[instanz]
                ziel = soll.get(instanz)
                if ziel is None or int(ziel.port) != horcher.port:
                    # Weg oder auf einen anderen Port gewandert: der Socket muss neu entstehen.
                    horcher.schliesse()
                    self._horcher.pop(instanz, None)
                    weg.append(f"{instanz}:{horcher.port}")
                else:
                    horcher.aktualisiere(ziel)
            for instanz, ziel in soll.items():
                if instanz in self._horcher:
                    continue
                if instanz in self._gesperrt:
                    continue                    # der Server startet gerade – Finger weg vom Port
                horcher = Horcher(ziel, wecken=self._wecken, melder=self._melder,
                                  host=self._host,
                                  ablehnung_merken=self._ablehnung_merken)
                grund = horcher.binde()
                if grund:
                    fehler[instanz] = grund
                    continue
                # Ist gerade eine Weckung gescheitert, sagt schon der erste Ping den Grund.
                alt, seit = self._ablehnungen.get(instanz, ("", 0.0))
                if alt and time.monotonic() - seit <= ABLEHNUNG_TTL:
                    horcher.abgelehnt(alt, seit=seit, merken=False)
                self._horcher[instanz] = horcher
                self._klagen.pop(instanz, None)
                neu.append(f"{instanz}:{horcher.port}")
        for instanz, grund in fehler.items():
            merk = f"{soll[instanz].port}:{grund}"
            if self._klagen.get(instanz) != merk:
                self._klagen[instanz] = merk
                self._melder(f"Der Bedrock-Port {soll[instanz].port} von {instanz} lässt sich "
                             f"nicht belegen ({grund}) – Konsolen sehen diesen schlafenden Server "
                             f"deshalb nicht. Es wird weiter versucht.")
        return {"neu": sorted(neu), "weg": sorted(weg), "fehler": fehler}

    # ------------------------------------------------------------------ Port hergeben

    def freigeben(self, instanz: str, *, frist: float = FREI_FRIST) -> bool:
        """Den Port dieser Instanz **sofort** hergeben und gegen erneutes Binden sperren.

        Das ruft der Daemon **vor** der Portvergabe und vor dem Start des Servers auf. Rückgabe
        ``True``, wenn der Port danach wirklich frei ist (oder gar keiner gehalten wurde).
        """
        instanz = str(instanz)
        with self._lock:
            self._gesperrt[instanz] = time.monotonic() + SPERRE_TTL
            horcher = self._horcher.pop(instanz, None)
        if horcher is None:
            return True
        horcher.schliesse()
        frei = _warte_bis_frei(horcher.port, horcher.host, frist)
        if not frei:
            self._melder(f"Der Bedrock-Port {horcher.port} von {instanz} war {frist:.0f} s nach "
                         f"dem Schließen noch belegt. Geyser könnte ihn nicht binden – die "
                         f"Bedrock-Spieler kämen dann nicht herein.")
        return frei

    def entsperren(self, instanz: str) -> None:
        """Die Sperre aufheben: der Server ist wirklich beendet (oder sein Start ging schief)."""
        with self._lock:
            self._gesperrt.pop(str(instanz), None)

    def gesperrt(self, instanz: str) -> bool:
        with self._lock:
            self._sperren_aufraeumen()
            return str(instanz) in self._gesperrt

    def _sperren_aufraeumen(self) -> None:
        jetzt = time.monotonic()
        for instanz, bis in list(self._gesperrt.items()):
            if bis <= jetzt:
                self._gesperrt.pop(instanz, None)

    # ------------------------------------------------------------------ Auskunft

    def startet(self, instanz: str) -> None:
        """Zeile 2 dieser Instanz auf „Server startet gerade“ stellen (Weckruf von außen)."""
        with self._lock:
            horcher = self._horcher.get(str(instanz))
        if horcher is not None:
            horcher.startet_jetzt()

    def port_von(self, instanz: str) -> int:
        with self._lock:
            horcher = self._horcher.get(str(instanz))
        return horcher.port if horcher is not None else 0

    def stand(self) -> list[dict]:
        """Was der Wecker gerade hält – für das Protokoll und die Betreiberoberfläche."""
        with self._lock:
            horcher = list(self._horcher.values())
        return [{"instanz": h.ziel.instanz, "port": h.port, "name": h.ziel.zeile1,
                 "pings": h.pings, "weckrufe": h.weckrufe, "zeile2": h.zeile2(),
                 "wecken": bool(h.ziel.wecken)}
                for h in sorted(horcher, key=lambda x: x.port)]

    def schliessen(self) -> list[str]:
        """Alle Ports hergeben (Ende des Dienstes). Rückgabe: welche gehalten wurden."""
        with self._lock:
            horcher = list(self._horcher.items())
            self._horcher.clear()
        for _instanz, einer in horcher:
            einer.schliesse()
        return sorted(f"{instanz}:{einer.port}" for instanz, einer in horcher)


# --------------------------------------------------------------------------- Vom Server lernen

def frage_server(port: int, *, host: str = "127.0.0.1", frist: float = 1.5) -> Auskunft | None:
    """Einen **laufenden** Server (Geyser) nach seiner Auskunft fragen. ``None`` = keine Antwort.

    So lernt der Dienst Protokollfassung, Spielfassung und Spielmodus des echten Servers und
    kann sie später im Ruhezustand weiterreichen. Ohne das müsste der Wecker Zahlen raten, und
    die Konsole zeigte am Eintrag womöglich „veralteter Server“ an.
    """
    zeit = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
    guid = int.from_bytes(os.urandom(8), "big") & 0x7FFFFFFFFFFFFFFF
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(max(0.2, float(frist)))
        sock.sendto(baue_ping(zeit, guid), (str(host), int(port)))
        ende = time.monotonic() + max(0.2, float(frist))
        while time.monotonic() < ende:
            try:
                daten, _ = sock.recvfrom(MAX_PAKET)
            except (socket.timeout, OSError):
                return None
            try:
                return zerlege_auskunft(zerlege_pong(daten).auskunft)
            except Protokollfehler:
                continue                # fremdes Paket auf dem Port – weiterhorchen
        return None
    except OSError:
        return None
    finally:
        sock.close()
