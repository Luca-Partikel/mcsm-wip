"""Selbsttests des Verteilers (hosted/router.py).

Geprüft wird der Protokoll-Zerleger mit echten Byte-Folgen, die Zuordnungstabelle, die Zähler
und – über echte Sockets auf 127.0.0.1 – die Weiterleitung. Es wird kein Minecraft-Server
gebraucht: als Ziel dient ein einfacher TCP-Horcher, der alles zurückschickt.

    python -m unittest discover -s hosted/tests
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

_HOSTED = pathlib.Path(__file__).resolve().parent.parent
if str(_HOSTED) not in sys.path:
    sys.path.insert(0, str(_HOSTED))

import router                                                       # noqa: E402

DOMAIN = "arcardia-nexus.de"


# --------------------------------------------------------------------------- Hilfen

def handshake(adresse: str, *, protokoll: int = 767, port: int = 25565,
              zustand: int = 2) -> bytes:
    """Ein echtes Handshake-Paket bauen (wie ein Java-Client es schickt)."""
    rumpf = (router.schreibe_varint(0x00)
             + router.schreibe_varint(protokoll)
             + router.schreibe_string(adresse)
             + int(port).to_bytes(2, "big")
             + router.schreibe_varint(zustand))
    return router.schreibe_varint(len(rumpf)) + rumpf


STATUS_ANFRAGE = b"\x01\x00"                     # Länge 1, Paket 0x00


def ping_anfrage(nutzlast: bytes = b"\x01\x02\x03\x04\x05\x06\x07\x08") -> bytes:
    rumpf = router.schreibe_varint(0x01) + nutzlast
    return router.schreibe_varint(len(rumpf)) + rumpf


def pakete(daten: bytes) -> list[tuple[int, bytes]]:
    """Bytes in (Kennziffer, Rumpf) zerlegen."""
    aus: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(daten):
        laenge, p = router.lese_varint(daten, pos)
        rumpf = daten[p:p + laenge]
        kennziffer, q = router.lese_varint(rumpf, 0)
        aus.append((kennziffer, bytes(rumpf[q:])))
        pos = p + laenge
    return aus


def lies_bis_ende(verbindung: socket.socket, frist: float = 4.0) -> bytes:
    """Alles lesen, bis die Gegenstelle schließt."""
    verbindung.settimeout(frist)
    daten = b""
    while True:
        try:
            stueck = verbindung.recv(4096)
        except (socket.timeout, OSError):
            return daten
        if not stueck:
            return daten
        daten += stueck


def lies_genau(verbindung: socket.socket, anzahl: int, frist: float = 4.0) -> bytes:
    verbindung.settimeout(frist)
    daten = b""
    while len(daten) < anzahl:
        try:
            stueck = verbindung.recv(anzahl - len(daten))
        except (socket.timeout, OSError):
            return daten
        if not stueck:
            return daten
        daten += stueck
    return daten


class Echo:
    """Ersatz für einen Minecraft-Server: schickt alles zurück und merkt sich, was ankam."""

    def __init__(self) -> None:
        self.horch = socket.socket()
        self.horch.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.horch.bind(("127.0.0.1", 0))
        self.horch.listen(8)
        self.port = self.horch.getsockname()[1]
        self._lock = threading.Lock()
        self._empfangen = bytearray()
        self.verbindungen = 0
        threading.Thread(target=self._laufe, daemon=True).start()

    def _laufe(self) -> None:
        while True:
            try:
                klient, _ = self.horch.accept()
            except OSError:
                return
            with self._lock:
                self.verbindungen += 1
            threading.Thread(target=self._bediene, args=(klient,), daemon=True).start()

    def _bediene(self, klient: socket.socket) -> None:
        try:
            while True:
                stueck = klient.recv(4096)
                if not stueck:
                    return
                with self._lock:
                    self._empfangen += stueck
                klient.sendall(stueck)
        except OSError:
            return
        finally:
            try:
                klient.close()
            except OSError:
                pass

    def empfangen(self) -> bytes:
        with self._lock:
            return bytes(self._empfangen)

    def warte_auf(self, anzahl: int, frist: float = 3.0) -> bytes:
        ende = time.monotonic() + frist
        while time.monotonic() < ende:
            daten = self.empfangen()
            if len(daten) >= anzahl:
                return daten
            time.sleep(0.02)
        return self.empfangen()

    def stoppe(self) -> None:
        try:
            self.horch.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- VarInt & Strings

class VarIntTest(unittest.TestCase):

    def test_hin_und_zurueck(self) -> None:
        for wert in (0, 1, 2, 127, 128, 255, 256, 2097151, 2097152, 25565, 767, 2147483647):
            roh = router.schreibe_varint(wert)
            self.assertEqual(router.lese_varint(roh), (wert, len(roh)), f"Wert {wert}")

    def test_bekannte_byte_folgen(self) -> None:
        # Die Beispiele aus der Protokollbeschreibung.
        self.assertEqual(router.schreibe_varint(0), b"\x00")
        self.assertEqual(router.schreibe_varint(1), b"\x01")
        self.assertEqual(router.schreibe_varint(127), b"\x7f")
        self.assertEqual(router.schreibe_varint(128), b"\x80\x01")
        self.assertEqual(router.schreibe_varint(255), b"\xff\x01")
        self.assertEqual(router.schreibe_varint(2097151), b"\xff\xff\x7f")
        self.assertEqual(router.schreibe_varint(2147483647), b"\xff\xff\xff\xff\x07")
        self.assertEqual(router.schreibe_varint(-1), b"\xff\xff\xff\xff\x0f")

    def test_negative_zahlen(self) -> None:
        for wert in (-1, -2, -128, -2147483648):
            roh = router.schreibe_varint(wert)
            self.assertEqual(len(roh), 5)
            self.assertEqual(router.lese_varint(roh)[0], wert)

    def test_versatz(self) -> None:
        daten = b"\x99\x99" + router.schreibe_varint(300)
        self.assertEqual(router.lese_varint(daten, 2), (300, len(daten)))

    def test_abgeschnitten(self) -> None:
        with self.assertRaises(router.Unvollstaendig):
            router.lese_varint(b"")
        with self.assertRaises(router.Unvollstaendig):
            router.lese_varint(b"\x80")
        with self.assertRaises(router.Unvollstaendig):
            router.lese_varint(b"\xff\xff\xff")

    def test_zu_lang(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.lese_varint(b"\x80\x80\x80\x80\x80\x01")

    def test_fuenftes_byte_wird_beschnitten(self) -> None:
        # Bits über 32 hinaus dürfen das Ergebnis nicht verfälschen.
        self.assertEqual(router.lese_varint(b"\xff\xff\xff\xff\x7f")[0], -1)


class StringTest(unittest.TestCase):

    def test_hin_und_zurueck(self) -> None:
        for text in ("", "a", "mein-server.arcardia-nexus.de", "Ümläute und ß", "x" * 255):
            roh = router.schreibe_string(text)
            self.assertEqual(router.lese_string(roh), (text, len(roh)))

    def test_abgeschnitten(self) -> None:
        roh = router.schreibe_string("abcdef")
        with self.assertRaises(router.Unvollstaendig):
            router.lese_string(roh[:-1])

    def test_zu_lang(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.lese_string(router.schreibe_varint(99999) + b"x" * 10)

    def test_negative_laenge(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.lese_string(router.schreibe_varint(-1) + b"x")

    def test_kein_utf8(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.lese_string(b"\x02\xff\xfe")


# --------------------------------------------------------------------------- Handshake

class HandshakeTest(unittest.TestCase):

    def test_normalfall(self) -> None:
        roh = handshake("mein-server.arcardia-nexus.de", protokoll=767, port=25565, zustand=2)
        hs = router.zerlege_handshake(roh)
        self.assertEqual(hs.protokoll, 767)
        self.assertEqual(hs.adresse, "mein-server.arcardia-nexus.de")
        self.assertEqual(hs.port, 25565)
        self.assertEqual(hs.zustand, 2)
        self.assertEqual(hs.laenge, len(roh))
        self.assertTrue(hs.ist_beitritt)
        self.assertFalse(hs.ist_status)

    def test_verschiedene_adresslaengen(self) -> None:
        for laenge in (1, 2, 63, 100, 127, 128, 129, 200, 253):
            adresse = ("a" * (laenge - 1) + "b") if laenge > 1 else "a"
            roh = handshake(adresse, zustand=1)
            hs = router.zerlege_handshake(roh)
            self.assertEqual(hs.adresse, adresse, f"Länge {laenge}")
            self.assertEqual(hs.laenge, len(roh))
            self.assertTrue(hs.ist_status)

    def test_lange_adresse_mit_zwei_byte_laenge(self) -> None:
        # Ab 128 Byte ist die Längenangabe des Strings zwei Byte lang – der Grenzfall.
        adresse = "x" * 130 + "." + DOMAIN
        roh = handshake(adresse)
        self.assertEqual(router.zerlege_handshake(roh).adresse, adresse)

    def test_grosse_protokollversion_und_port(self) -> None:
        roh = handshake("a." + DOMAIN, protokoll=2097151, port=65535, zustand=1)
        hs = router.zerlege_handshake(roh)
        self.assertEqual(hs.protokoll, 2097151)
        self.assertEqual(hs.port, 65535)

    def test_uebergabe_zustand_drei(self) -> None:
        hs = router.zerlege_handshake(handshake("a." + DOMAIN, zustand=3))
        self.assertTrue(hs.ist_beitritt)

    def test_jedes_praefix_ist_unvollstaendig(self) -> None:
        roh = handshake("mein-server." + DOMAIN)
        for schnitt in range(0, len(roh)):
            with self.subTest(schnitt=schnitt):
                with self.assertRaises(router.Unvollstaendig):
                    router.zerlege_handshake(roh[:schnitt])
        self.assertEqual(router.zerlege_handshake(roh).laenge, len(roh))

    def test_zusaetzliche_bytes_bleiben_liegen(self) -> None:
        roh = handshake("mein-server." + DOMAIN, zustand=1)
        hs = router.zerlege_handshake(roh + STATUS_ANFRAGE + b"noch mehr")
        self.assertEqual(hs.laenge, len(roh))

    def test_nullbyte_zusaetze(self) -> None:
        for zusatz in ("\x00FML\x00", "\x00FML2\x00", "\x00FML3\x00",
                       "\x0072.0.0.1\x00c0ffee\x00[]"):
            adresse = "mein-server." + DOMAIN + zusatz
            hs = router.zerlege_handshake(handshake(adresse))
            self.assertEqual(hs.adresse, adresse)
            self.assertEqual(router.bereinige_adresse(hs.adresse), "mein-server." + DOMAIN)

    def test_falsche_kennziffer(self) -> None:
        rumpf = (router.schreibe_varint(0x02) + router.schreibe_varint(767)
                 + router.schreibe_string("a." + DOMAIN) + b"\x63\xdd"
                 + router.schreibe_varint(2))
        roh = router.schreibe_varint(len(rumpf)) + rumpf
        with self.assertRaises(router.Protokollfehler) as fall:
            router.zerlege_handshake(roh)
        self.assertIn("0x00", str(fall.exception))

    def test_falscher_zustand(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.zerlege_handshake(handshake("a." + DOMAIN, zustand=7))

    def test_laenge_null(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.zerlege_handshake(b"\x00\x00")

    def test_laenge_zu_gross(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.zerlege_handshake(router.schreibe_varint(999999) + b"\x00" * 10)

    def test_kuerzer_als_angekuendigt(self) -> None:
        # Das Paket ist vollständig da (Länge stimmt), aber Port und Zustand fehlen darin.
        rumpf = (router.schreibe_varint(0x00) + router.schreibe_varint(767)
                 + router.schreibe_string("mein-server." + DOMAIN))
        roh = router.schreibe_varint(len(rumpf)) + rumpf
        with self.assertRaises(router.Protokollfehler) as fall:
            router.zerlege_handshake(roh)
        self.assertIn("kürzer", str(fall.exception))
        # Auch ein Paket, dessen Adresse über das Paketende hinausreicht, ist ein Fehler.
        rumpf2 = (router.schreibe_varint(0x00) + router.schreibe_varint(767)
                  + router.schreibe_varint(200) + b"x" * 5)
        with self.assertRaises(router.Protokollfehler):
            router.zerlege_handshake(router.schreibe_varint(len(rumpf2)) + rumpf2)

    def test_alter_ping_ist_kein_handshake(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.zerlege_handshake(b"\xfe\x01")

    def test_muell(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.zerlege_handshake(b"\x02\x99\x01")


class AlterPingTest(unittest.TestCase):

    def test_ganz_alter_ping(self) -> None:
        self.assertEqual(router.zerlege_alten_ping(b"\xfe\x02"), ("", 0))
        self.assertIsNone(router.zerlege_alten_ping(b"\xfe"))

    def test_ohne_adresse(self) -> None:
        # Nach „FE 01“ könnte noch das Zusatzpaket folgen – erst das dritte Byte entscheidet.
        self.assertIsNone(router.zerlege_alten_ping(b"\xfe\x01"))
        self.assertEqual(router.zerlege_alten_ping(b"\xfe\x01\x00"), ("", 0))

    def test_mit_adresse(self) -> None:
        adresse = "mein-server." + DOMAIN
        kanal = "MC|PingHost".encode("utf-16-be")
        host = adresse.encode("utf-16-be")
        rest = 1 + 2 + len(host) + 4
        roh = (b"\xfe\x01\xfa" + len("MC|PingHost").to_bytes(2, "big") + kanal
               + rest.to_bytes(2, "big") + b"\x4a" + len(adresse).to_bytes(2, "big")
               + host + (25565).to_bytes(4, "big"))
        self.assertEqual(router.zerlege_alten_ping(roh), (adresse, 25565))
        for schnitt in range(3, len(roh)):
            self.assertIsNone(router.zerlege_alten_ping(roh[:schnitt]), f"Schnitt {schnitt}")

    def test_fremder_kanal(self) -> None:
        kanal = "MC|Quatsch!".encode("utf-16-be")
        roh = b"\xfe\x01\xfa" + (11).to_bytes(2, "big") + kanal
        self.assertEqual(router.zerlege_alten_ping(roh), ("", 0))

    def test_kein_alter_ping(self) -> None:
        with self.assertRaises(router.Protokollfehler):
            router.zerlege_alten_ping(b"\x10\x00")

    def test_antwort_ist_lesbar(self) -> None:
        roh = router.alter_ping_paket("Diesen Server gibt es hier nicht")
        self.assertEqual(roh[0], 0xFF)
        zeichen = int.from_bytes(roh[1:3], "big")
        text = roh[3:].decode("utf-16-be")
        self.assertEqual(zeichen, len(text))
        teile = text.split("\x00")
        self.assertEqual(teile[0], "§1")
        self.assertEqual(teile[3], "Diesen Server gibt es hier nicht")
        self.assertEqual(teile[4:], ["0", "0"])


# --------------------------------------------------------------------------- Antworten

class AntwortTest(unittest.TestCase):

    def test_statusantwort(self) -> None:
        roh = router.status_paket(router.MELDUNG_UNBEKANNT, 767)
        (kennziffer, rumpf), = pakete(roh)
        self.assertEqual(kennziffer, 0x00)
        text, gelesen = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(gelesen, len(rumpf))
        daten = json.loads(text)
        self.assertEqual(daten["description"]["text"], "Diesen Server gibt es hier nicht")
        self.assertEqual(daten["version"]["protocol"], 767)
        self.assertEqual(daten["players"], {"max": 0, "online": 0, "sample": []})

    def test_statusantwort_spiegelt_unsinn_nicht(self) -> None:
        daten = json.loads(router.status_json("x", -1))
        self.assertEqual(daten["version"]["protocol"], -1)

    def test_trennmeldung(self) -> None:
        roh = router.trenn_paket(router.MELDUNG_UNBEKANNT)
        (kennziffer, rumpf), = pakete(roh)
        self.assertEqual(kennziffer, 0x00)
        text, gelesen = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(gelesen, len(rumpf))
        self.assertEqual(json.loads(text)["text"], "Diesen Server gibt es hier nicht")

    def test_umlaute_werden_richtig_gezaehlt(self) -> None:
        # „korrekt längencodiert“ heißt: Byte zählen, nicht Zeichen.
        roh = router.trenn_paket("Grüße aus Österreich")
        (_kennziffer, rumpf), = pakete(roh)
        text, gelesen = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(gelesen, len(rumpf))
        self.assertIn("Grüße", text)

    def test_pingantwort(self) -> None:
        (kennziffer, rumpf), = pakete(router.ping_paket(b"12345678"))
        self.assertEqual((kennziffer, rumpf), (0x01, b"12345678"))


# --------------------------------------------------------------------------- Adressen

class AdresseTest(unittest.TestCase):

    def test_bereinigen(self) -> None:
        faelle = {
            "Mein-Server.Arcardia-Nexus.DE": "mein-server.arcardia-nexus.de",
            "mein-server.arcardia-nexus.de.": "mein-server.arcardia-nexus.de",
            "  mein-server.arcardia-nexus.de  ": "mein-server.arcardia-nexus.de",
            "mein-server.arcardia-nexus.de\x00FML3\x00": "mein-server.arcardia-nexus.de",
            "\x00nur-zusatz": "",
            "[::1]": "::1",
            "": "",
            None: "",
        }
        for roh, erwartet in faelle.items():
            self.assertEqual(router.bereinige_adresse(roh), erwartet, repr(roh))

    def test_laenge_wird_begrenzt(self) -> None:
        self.assertEqual(len(router.bereinige_adresse("a" * 500)), router.ADRESSE_MAX)

    def test_ip_erkennen(self) -> None:
        self.assertTrue(router.ist_ip("45.132.89.224"))
        self.assertTrue(router.ist_ip("::1"))
        self.assertFalse(router.ist_ip("mein-server." + DOMAIN))

    def test_hostname_pruefen(self) -> None:
        self.assertTrue(router.ist_hostname("mein-server." + DOMAIN))
        self.assertTrue(router.ist_hostname("a1"))
        self.assertFalse(router.ist_hostname("-nein." + DOMAIN))
        self.assertFalse(router.ist_hostname("mit leer." + DOMAIN))
        self.assertFalse(router.ist_hostname("Groß." + DOMAIN))
        self.assertFalse(router.ist_hostname(""))

    def test_schluessel_pruefen(self) -> None:
        self.assertEqual(router.pruefe_schluessel(" Mein-Server "), "mein-server")
        self.assertEqual(router.pruefe_schluessel("@standard"), "@standard")
        self.assertEqual(router.pruefe_schluessel("mc.beispiel.de."), "mc.beispiel.de")
        for schlecht in ("", "  ", "mit leer", "über", "-vorn", "hinten-", "../etc"):
            with self.assertRaises(ValueError, msg=schlecht):
                router.pruefe_schluessel(schlecht)

    def test_port_pruefen(self) -> None:
        self.assertEqual(router.pruefe_port("25567"), 25567)
        self.assertEqual(router.pruefe_port(25567), 25567)
        for schlecht in (0, -1, 65536, "abc", None, True, 1.5):
            with self.assertRaises(ValueError, msg=repr(schlecht)):
                router.pruefe_port(schlecht)


# --------------------------------------------------------------------------- Zuordnungstabelle

class TabellenBasis(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcsm-router-test-"))
        self.datei = self.tmp / "routes.json"
        self.zeilen: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def schreibe(self, inhalt) -> None:
        text = inhalt if isinstance(inhalt, str) else json.dumps(inhalt, ensure_ascii=False)
        self.datei.write_text(text, encoding="utf-8")

    def tabelle(self, **mehr) -> router.Routen:
        mehr.setdefault("pruef_abstand", 0.0)
        return router.Routen(self.datei, domain=DOMAIN,
                             melder=lambda bereich, text: self.zeilen.append((bereich, text)),
                             **mehr)

    @property
    def protokolltext(self) -> str:
        return "\n".join(text for _bereich, text in self.zeilen)


class RoutenTest(TabellenBasis):

    def test_nachschlagen(self) -> None:
        self.schreibe({"mein-server": {"port": 25567, "instanz": "a1b2c3"},
                       "zweiter": {"port": 25568, "instanz": "d4e5f6"}})
        routen = self.tabelle()
        route = routen.finde("mein-server." + DOMAIN)
        self.assertIsNotNone(route)
        self.assertEqual((route.name, route.port, route.instanz), ("mein-server", 25567, "a1b2c3"))
        self.assertEqual(routen.finde("ZWEITER." + DOMAIN + ".").port, 25568)
        self.assertEqual(routen.finde("mein-server." + DOMAIN + "\x00FML3\x00").port, 25567)
        self.assertIsNone(routen.finde("dritter." + DOMAIN))

    def test_port_als_zahl(self) -> None:
        self.schreibe({"mein-server": 25567})
        self.assertEqual(self.tabelle().finde("mein-server." + DOMAIN).port, 25567)

    def test_ganzer_name_als_schluessel(self) -> None:
        self.schreibe({"mc.beispiel.de": {"port": 25570}})
        routen = self.tabelle()
        self.assertEqual(routen.finde("MC.Beispiel.de").port, 25570)
        self.assertIsNone(routen.finde("mc.beispiel.com"))

    def test_ohne_unterdomain_ohne_standard(self) -> None:
        self.schreibe({"mein-server": {"port": 25567}})
        routen = self.tabelle()
        for adresse in (DOMAIN, "server." + DOMAIN, "www." + DOMAIN, "45.132.89.224", "", "::1"):
            self.assertIsNone(routen.finde(adresse), adresse)

    def test_ohne_unterdomain_mit_standard(self) -> None:
        self.schreibe({"@standard": {"port": 25599}, "mein-server": {"port": 25567}})
        routen = self.tabelle()
        for adresse in (DOMAIN, "server." + DOMAIN, "45.132.89.224", "", "fremd.example.com"):
            self.assertEqual(routen.finde(adresse).port, 25599, adresse)
        # Ein Tippfehler in der Unterdomain landet NICHT beim Standardserver.
        self.assertIsNone(routen.finde("tippfehler." + DOMAIN))
        self.assertEqual(routen.finde("mein-server." + DOMAIN).port, 25567)

    def test_eigener_eintrag_schlaegt_reservierte_marke(self) -> None:
        self.schreibe({"server": {"port": 25571}, "@standard": {"port": 25599}})
        self.assertEqual(self.tabelle().finde("server." + DOMAIN).port, 25571)

    def test_tiefere_unterdomain(self) -> None:
        """Bei „welt.gruppe.<domain>“ gilt die linkeste Marke – so bleibt eine tiefere
        Schreibweise nutzbar, falls jemand sie einträgt."""
        self.schreibe({"welt": {"port": 25572}, "welt.gruppe": {"port": 25573}})
        routen = self.tabelle()
        self.assertEqual(routen.finde("welt.gruppe." + DOMAIN).port, 25573)
        self.assertEqual(routen.finde("welt." + DOMAIN).port, 25572)
        self.assertEqual(routen.finde("welt.andere." + DOMAIN).port, 25572)

    def test_fehlende_datei(self) -> None:
        routen = self.tabelle()
        self.assertIsNone(routen.finde("mein-server." + DOMAIN))
        self.assertEqual(routen.eintraege(), {})
        self.assertIn("gibt es noch nicht", self.protokolltext)

    def test_leere_datei(self) -> None:
        self.datei.write_text("", encoding="utf-8")
        self.assertEqual(self.tabelle().eintraege(), {})

    def test_schlechte_eintraege_werden_uebersprungen(self) -> None:
        self.schreibe({"gut": {"port": 25567},
                       "ohne-port": {"instanz": "x"},
                       "port-null": {"port": 0},
                       "port-text": {"port": "abc"},
                       "mit leer": {"port": 25568},
                       "_bemerkung": "wird ignoriert",
                       "zu-gross": {"port": 70000}})
        routen = self.tabelle()
        routen.aktualisiere(erzwingen=True)
        self.assertEqual(sorted(routen.eintraege()), ["gut"])
        for name in ("ohne-port", "port-null", "port-text", "mit leer", "zu-gross"):
            self.assertIn(f"„{name}“", self.protokolltext)
        self.assertNotIn("_bemerkung", self.protokolltext)

    def test_kaputtes_json_behaelt_alte_tabelle(self) -> None:
        self.schreibe({"mein-server": {"port": 25567}})
        routen = self.tabelle()
        self.assertIsNotNone(routen.finde("mein-server." + DOMAIN))
        self.datei.write_text("{ das ist kein JSON", encoding="utf-8")
        self.assertEqual(routen.finde("mein-server." + DOMAIN).port, 25567)
        self.assertIn("kein gültiges JSON", self.protokolltext)
        # Und nach der Reparatur gilt wieder die Datei.
        self.schreibe({"mein-server": {"port": 25599}})
        self.assertEqual(routen.finde("mein-server." + DOMAIN).port, 25599)

    def test_falscher_aufbau_behaelt_alte_tabelle(self) -> None:
        self.schreibe({"mein-server": {"port": 25567}})
        routen = self.tabelle()
        routen.aktualisiere(erzwingen=True)
        self.schreibe([1, 2, 3])
        self.assertEqual(routen.finde("mein-server." + DOMAIN).port, 25567)
        self.assertIn("Name → Port", self.protokolltext)

    def test_huelle_routen(self) -> None:
        self.schreibe({"routen": {"mein-server": {"port": 25567}}})
        self.assertEqual(self.tabelle().finde("mein-server." + DOMAIN).port, 25567)

    def test_wird_bei_aenderung_neu_gelesen(self) -> None:
        self.schreibe({"eins": {"port": 25567}})
        routen = self.tabelle()
        self.assertIsNotNone(routen.finde("eins." + DOMAIN))
        self.assertIsNone(routen.finde("zwei." + DOMAIN))
        self.schreibe({"eins": {"port": 25567}, "zwei": {"port": 25568}})
        self.assertEqual(routen.finde("zwei." + DOMAIN).port, 25568)
        self.assertIn("neu: zwei", self.protokolltext)

    def test_wird_nicht_dauernd_gelesen(self) -> None:
        self.schreibe({"eins": {"port": 25567}})
        routen = self.tabelle(pruef_abstand=30.0)
        gelesen: list[int] = []
        echtes_lesen = routen._lese

        def zaehlend():
            gelesen.append(1)
            return echtes_lesen()

        routen._lese = zaehlend                                     # type: ignore[assignment]
        for _ in range(50):
            routen.finde("eins." + DOMAIN)
        self.assertEqual(len(gelesen), 1)
        # Auch ein zweites Mal nur einmal – der Zeitstempel bleibt ja gleich.
        routen.aktualisiere(erzwingen=True)
        self.assertEqual(len(gelesen), 2)

    def test_gleicher_zeitstempel_kein_neues_lesen(self) -> None:
        self.schreibe({"eins": {"port": 25567}})
        routen = self.tabelle()
        routen.aktualisiere(erzwingen=True)
        gelesen: list[int] = []
        echtes_lesen = routen._lese
        routen._lese = lambda: (gelesen.append(1), echtes_lesen())[1]   # type: ignore[assignment]
        for _ in range(5):
            routen.aktualisiere()
        self.assertEqual(gelesen, [])

    def test_eigene_grenze_im_eintrag(self) -> None:
        self.schreibe({"eins": {"port": 25567, "grenze": 3}})
        self.assertEqual(self.tabelle().finde("eins." + DOMAIN).grenze, 3)


# --------------------------------------------------------------------------- Zähler

class ZaehlerTest(unittest.TestCase):

    def test_grenze_je_ziel(self) -> None:
        zaehler = router.Zaehler()
        self.assertEqual(zaehler.belege("eins", 2), "")
        self.assertEqual(zaehler.belege("eins", 2), "")
        klage = zaehler.belege("eins", 2)
        self.assertIn("Grenze 2", klage)
        self.assertIn("eins", klage)
        self.assertEqual(zaehler.belege("zwei", 2), "")     # andere Ziele sind nicht betroffen
        zaehler.gib_frei("eins")
        self.assertEqual(zaehler.belege("eins", 2), "")
        self.assertEqual(zaehler.offen("eins"), 2)
        self.assertEqual(zaehler.gesamt(), 3)

    def test_gesamtgrenze(self) -> None:
        zaehler = router.Zaehler(gesamt_grenze=2)
        self.assertEqual(zaehler.belege("a", 100), "")
        self.assertEqual(zaehler.belege("b", 100), "")
        self.assertIn("Gesamtgrenze", zaehler.belege("c", 100))

    def test_freigeben_geht_nicht_unter_null(self) -> None:
        zaehler = router.Zaehler()
        zaehler.gib_frei("gibt-es-nicht")
        self.assertEqual(zaehler.gesamt(), 0)
        self.assertEqual(zaehler.stand(), {})


# --------------------------------------------------------------------------- Weiterleitung

class Dienst:
    """Ein Verteiler auf 127.0.0.1 mit eigenem Thread – nur für die Tests."""

    def __init__(self, datei: pathlib.Path, *, grenze: int = 200, frist: float = 5.0,
                 gesamt_grenze: int = 1000) -> None:
        self.zeilen: list[tuple[str, str]] = []
        melder = lambda bereich, text: self.zeilen.append((bereich, text))    # noqa: E731
        self.routen = router.Routen(datei, domain=DOMAIN, pruef_abstand=0.0, melder=melder)
        self.verteiler = router.Verteiler(self.routen, ziel_host="127.0.0.1", grenze=grenze,
                                          gesamt_grenze=gesamt_grenze, frist=frist,
                                          ziel_frist=2.0, melder=melder)
        self.sockets = self.verteiler.horcher("127.0.0.1", 0)
        self.port = int(self.sockets[0].getsockname()[1])
        self.thread = threading.Thread(target=self.verteiler.bediene, daemon=True)
        self.thread.start()

    def klient(self, frist: float = 4.0) -> socket.socket:
        verbindung = socket.create_connection(("127.0.0.1", self.port), timeout=frist)
        verbindung.settimeout(frist)
        return verbindung

    @property
    def protokolltext(self) -> str:
        return "\n".join(f"[{bereich}] {text}" for bereich, text in self.zeilen)

    def stoppe(self) -> None:
        self.verteiler.stoppe()
        self.verteiler.schliesse()
        self.thread.join(3.0)


class WeiterleitungTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcsm-router-test-"))
        self.datei = self.tmp / "routes.json"
        self.echo = Echo()
        self.dienste: list[Dienst] = []

    def tearDown(self) -> None:
        for dienst in self.dienste:
            dienst.stoppe()
        self.echo.stoppe()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tabelle(self, inhalt: dict) -> None:
        self.datei.write_text(json.dumps(inhalt, ensure_ascii=False), encoding="utf-8")

    def dienst(self, **mehr) -> Dienst:
        neu = Dienst(self.datei, **mehr)
        self.dienste.append(neu)
        return neu

    # ---------------------------------------------------------------- Weiterleiten

    def test_beitritt_wird_weitergereicht(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port, "instanz": "a1b2c3"}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=2)
        with dienst.klient() as klient:
            klient.sendall(roh)
            # Der Testserver schickt alles zurück: also müssen genau dieselben Bytes ankommen.
            self.assertEqual(lies_genau(klient, len(roh)), roh)
            nachschlag = b"\x10\x00\x0eSpielerinName"
            klient.sendall(nachschlag)
            self.assertEqual(lies_genau(klient, len(nachschlag)), nachschlag)
        self.assertEqual(self.echo.warte_auf(len(roh) + len(nachschlag)), roh + nachschlag)
        self.assertIn("instanz=a1b2c3", dienst.protokolltext)
        self.assertIn(f"127.0.0.1:{self.echo.port}", dienst.protokolltext)

    def test_gepufferte_bytes_gehen_vollstaendig_mit(self) -> None:
        """Handshake und Anmeldung kommen in einem Häppchen – beides muss ankommen."""
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=2) + b"\x10\x00\x0eSpielerinName"
        with dienst.klient() as klient:
            klient.sendall(roh)
            self.assertEqual(lies_genau(klient, len(roh)), roh)
        self.assertEqual(self.echo.warte_auf(len(roh)), roh)

    def test_handshake_haeppchenweise(self) -> None:
        """Ein Byte nach dem anderen – auch so muss der Verteiler zurechtkommen."""
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=2)
        with dienst.klient() as klient:
            for byte in roh:
                klient.sendall(bytes([byte]))
                time.sleep(0.002)
            self.assertEqual(lies_genau(klient, len(roh)), roh)

    def test_grosse_datenmenge(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=2)
        last = bytes(range(256)) * 400                              # ~100 KB
        with dienst.klient(frist=10.0) as klient:
            klient.sendall(roh + last)
            zurueck = lies_genau(klient, len(roh) + len(last), frist=10.0)
        self.assertEqual(zurueck, roh + last)

    def test_status_wird_weitergereicht(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=1) + STATUS_ANFRAGE
        with dienst.klient() as klient:
            klient.sendall(roh)
            self.assertEqual(lies_genau(klient, len(roh)), roh)

    def test_zusatz_hinter_nullbyte_stoert_nicht(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN + "\x00FML3\x00", zustand=2)
        with dienst.klient() as klient:
            klient.sendall(roh)
            self.assertEqual(lies_genau(klient, len(roh)), roh)     # unverändert, mit Zusatz

    def test_grosse_kleinschreibung(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("MEIN-SERVER." + DOMAIN.upper() + ".", zustand=2)
        with dienst.klient() as klient:
            klient.sendall(roh)
            self.assertEqual(lies_genau(klient, len(roh)), roh)

    def test_geaenderte_tabelle_wirkt_sofort(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        zweiter = Echo()
        try:
            self.tabelle({"mein-server": {"port": self.echo.port},
                          "zweiter": {"port": zweiter.port}})
            roh = handshake("zweiter." + DOMAIN, zustand=2)
            with dienst.klient() as klient:
                klient.sendall(roh)
                self.assertEqual(lies_genau(klient, len(roh)), roh)
            self.assertEqual(zweiter.warte_auf(len(roh)), roh)
        finally:
            zweiter.stoppe()

    # ---------------------------------------------------------------- Unbekannt

    def test_unbekannt_bei_beitritt(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("gibt-es-nicht." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        (kennziffer, rumpf), = pakete(antwort)
        self.assertEqual(kennziffer, 0x00)
        text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(json.loads(text)["text"], "Diesen Server gibt es hier nicht")
        self.assertEqual(self.echo.empfangen(), b"")
        self.assertIn("Unbekannte Adresse", dienst.protokolltext)

    def test_unbekannt_bei_statusabfrage(self) -> None:
        self.tabelle({})
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("gibt-es-nicht." + DOMAIN, zustand=1) + STATUS_ANFRAGE
                           + ping_anfrage())
            antwort = lies_bis_ende(klient)
        teile = pakete(antwort)
        self.assertEqual([k for k, _ in teile], [0x00, 0x01])
        text, _ = router.lese_string(teile[0][1], 0, max_bytes=65535)
        daten = json.loads(text)
        self.assertEqual(daten["description"]["text"], "Diesen Server gibt es hier nicht")
        self.assertEqual(daten["version"]["protocol"], 767)
        self.assertEqual(teile[1][1], b"\x01\x02\x03\x04\x05\x06\x07\x08")

    def test_status_ohne_anfrage_wird_trotzdem_beantwortet(self) -> None:
        self.tabelle({})
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("gibt-es-nicht." + DOMAIN, zustand=1))
            antwort = lies_bis_ende(klient)
        self.assertTrue(antwort)
        kennziffer, rumpf = pakete(antwort)[0]
        text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertIn("Diesen Server gibt es hier nicht", text)

    def test_nackte_domain_ohne_standard(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake(DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        _kennziffer, rumpf = pakete(antwort)[0]
        text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(json.loads(text)["text"], "Diesen Server gibt es hier nicht")

    def test_nackte_domain_mit_standard(self) -> None:
        self.tabelle({"@standard": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("server." + DOMAIN, zustand=2)
        with dienst.klient() as klient:
            klient.sendall(roh)
            self.assertEqual(lies_genau(klient, len(roh)), roh)

    # ---------------------------------------------------------------- Fehlerfälle

    def test_ziel_ist_aus(self) -> None:
        tot = socket.socket()
        tot.bind(("127.0.0.1", 0))
        port = tot.getsockname()[1]
        tot.close()                                                 # niemand lauscht dort
        self.tabelle({"mein-server": {"port": port}})
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("mein-server." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        _kennziffer, rumpf = pakete(antwort)[0]
        text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(json.loads(text)["text"], "Dieser Server läuft gerade nicht")
        self.assertIn("nimmt keine Verbindung an", dienst.protokolltext)

    def test_ziel_ist_aus_bei_statusabfrage(self) -> None:
        tot = socket.socket()
        tot.bind(("127.0.0.1", 0))
        port = tot.getsockname()[1]
        tot.close()
        self.tabelle({"mein-server": {"port": port}})
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("mein-server." + DOMAIN, zustand=1) + STATUS_ANFRAGE)
            antwort = lies_bis_ende(klient)
        _kennziffer, rumpf = pakete(antwort)[0]
        text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(json.loads(text)["description"]["text"], "Dieser Server läuft gerade nicht")

    def test_grenze_je_ziel(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst(grenze=1)
        roh = handshake("mein-server." + DOMAIN, zustand=2)
        erste = dienst.klient()
        try:
            erste.sendall(roh)
            self.assertEqual(lies_genau(erste, len(roh)), roh)       # die erste steht
            with dienst.klient() as zweite:
                zweite.sendall(roh)
                antwort = lies_bis_ende(zweite)
            _kennziffer, rumpf = pakete(antwort)[0]
            text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
            self.assertEqual(json.loads(text)["text"], "Dieser Server ist gerade voll")
        finally:
            erste.close()
        # Nach dem Schließen ist der Platz wieder frei.
        ende = time.monotonic() + 3.0
        while dienst.verteiler.zaehler.offen("mein-server") and time.monotonic() < ende:
            time.sleep(0.02)
        self.assertEqual(dienst.verteiler.zaehler.offen("mein-server"), 0)
        with dienst.klient() as dritte:
            dritte.sendall(roh)
            self.assertEqual(lies_genau(dritte, len(roh)), roh)

    def test_eintrag_zeigt_auf_den_verteiler(self) -> None:
        """Ein Eintrag mit dem eigenen Port wäre eine Schleife – er wird nicht weitergeleitet."""
        dienst = self.dienst()
        self.tabelle({"schleife": {"port": dienst.port}})
        with dienst.klient() as klient:
            klient.sendall(handshake("schleife." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        _kennziffer, rumpf = pakete(antwort)[0]
        text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
        self.assertEqual(json.loads(text)["text"], "Dieser Server läuft gerade nicht")
        self.assertIn("Schleife", dienst.protokolltext)
        self.assertEqual(dienst.verteiler.zaehler.gesamt(), 0)

    def test_zeitlimit_fuer_den_handshake(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst(frist=0.6)
        beginn = time.monotonic()
        with dienst.klient() as klient:
            klient.sendall(b"\x10")                                 # Länge 16, aber nichts folgt
            self.assertEqual(lies_bis_ende(klient, frist=3.0), b"")
        dauer = time.monotonic() - beginn
        self.assertLess(dauer, 3.0)
        self.assertGreaterEqual(dauer, 0.4)
        self.assertIn("Zeitlimit", dienst.protokolltext)

    def test_kein_minecraft(self) -> None:
        self.tabelle({})
        dienst = self.dienst(frist=3.0)
        with dienst.klient() as klient:
            klient.sendall(b"\x02\x99\x01")                         # gültige Länge, falsches Paket
            self.assertEqual(lies_bis_ende(klient, frist=2.0), b"")
        self.assertIn("spricht kein Minecraft", dienst.protokolltext)

    def test_leere_verbindung(self) -> None:
        """Eine Portprüfung (verbinden, sofort schließen) darf nichts kaputt machen."""
        self.tabelle({})
        dienst = self.dienst()
        for _ in range(5):
            klient = dienst.klient()
            klient.close()
        time.sleep(0.2)
        self.assertNotIn("Unerwarteter Fehler", dienst.protokolltext)
        # Der Verteiler bedient danach ganz normal weiter.
        with dienst.klient() as klient:
            klient.sendall(handshake("gibt-es-nicht." + DOMAIN, zustand=2))
            self.assertTrue(lies_bis_ende(klient))

    def test_alter_ping_wird_beantwortet(self) -> None:
        self.tabelle({})
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(b"\xfe\x01")
            antwort = lies_bis_ende(klient, frist=3.0)
        self.assertTrue(antwort.startswith(b"\xff"))
        text = antwort[3:].decode("utf-16-be")
        self.assertIn("Diesen Server gibt es hier nicht", text)
        self.assertIn("Alter Ping", dienst.protokolltext)

    def test_alter_ping_mit_bekannter_adresse_wird_weitergereicht(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        adresse = "mein-server." + DOMAIN
        host = adresse.encode("utf-16-be")
        kanal = "MC|PingHost".encode("utf-16-be")
        roh = (b"\xfe\x01\xfa" + (11).to_bytes(2, "big") + kanal
               + (1 + 2 + len(host) + 4).to_bytes(2, "big") + b"\x4a"
               + len(adresse).to_bytes(2, "big") + host + (25565).to_bytes(4, "big"))
        with dienst.klient() as klient:
            klient.sendall(roh)
            self.assertEqual(lies_genau(klient, len(roh)), roh)
        self.assertEqual(self.echo.warte_auf(len(roh)), roh)

    def test_zahlen_stimmen(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=2)
        with dienst.klient() as klient:
            klient.sendall(roh)
            lies_genau(klient, len(roh))
        with dienst.klient() as klient:
            klient.sendall(handshake("gibt-es-nicht." + DOMAIN, zustand=2))
            lies_bis_ende(klient)
        self.assertEqual(dienst.verteiler.zahlen["weitergeleitet"], 1)
        self.assertEqual(dienst.verteiler.zahlen["unbekannt"], 1)
        self.assertEqual(dienst.verteiler.zahlen["angenommen"], 2)
        self.assertIn("weitergeleitet=1", dienst.verteiler.stand_text())

    def test_protokoll_nennt_keine_ip(self) -> None:
        """Datensparsamkeit: bei einer gewöhnlichen Weiterleitung steht keine IP im Protokoll."""
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=2)
        with dienst.klient() as klient:
            klient.sendall(roh)
            lies_genau(klient, len(roh))
        weiter = "\n".join(text for bereich, text in dienst.zeilen if bereich == "weiter")
        self.assertTrue(weiter)
        self.assertNotIn("127.0.0.1:", weiter.replace(f"127.0.0.1:{self.echo.port}", ""))
        self.assertNotIn("klient=127.0.0.1", weiter)
        self.assertIn("klient=", weiter)

    def test_kennung_ist_stabil_aber_nicht_die_ip(self) -> None:
        eins = router.kennung("45.132.89.224")
        self.assertEqual(eins, router.kennung("45.132.89.224"))
        self.assertNotEqual(eins, router.kennung("45.132.89.225"))
        self.assertEqual(len(eins), 8)
        # Keine Klartextspur: die Kennung ist ein gesalzener Hash, also reines Hex und nirgends
        # ein Stück der IP. (Frueher stand hier `assertNotIn("45", eins[:2])` – das Salz ist
        # zufaellig, damit war der Test in einem von 256 Laeufen rot.)
        self.assertTrue(all(z in "0123456789abcdef" for z in eins), eins)
        for teil in ("45.132", "132.89", "89.224", "45.132.89.224"):
            self.assertNotIn(teil, eins)

    def test_viele_gleichzeitig(self) -> None:
        self.tabelle({"mein-server": {"port": self.echo.port}})
        dienst = self.dienst()
        roh = handshake("mein-server." + DOMAIN, zustand=2)
        fehler: list[str] = []

        def einer() -> None:
            try:
                with dienst.klient(frist=8.0) as klient:
                    klient.sendall(roh)
                    if lies_genau(klient, len(roh), frist=8.0) != roh:
                        fehler.append("Bytes kamen nicht zurück")
            except OSError as exc:
                fehler.append(str(exc))

        faeden = [threading.Thread(target=einer) for _ in range(25)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(15.0)
        self.assertEqual(fehler, [])
        self.assertEqual(dienst.verteiler.zahlen["weitergeleitet"], 25)
        # Keine Zombies: nach dem Schließen sind alle Plätze wieder frei.
        ende = time.monotonic() + 5.0
        while dienst.verteiler.zaehler.gesamt() and time.monotonic() < ende:
            time.sleep(0.05)
        self.assertEqual(dienst.verteiler.zaehler.gesamt(), 0)


# --------------------------------------------------------------------------- Einstellungen

class EinstellungTest(unittest.TestCase):

    def setUp(self) -> None:
        self.sicherung = {name: os.environ.get(name) for name in
                          ("MCSM_ROUTER_PORT", "MCSM_ROUTES", "MCSM_ROUTER_LOG",
                           "MCSM_ROUTER_LOG_STDOUT", "MCSM_ROUTER_TIMEOUT")}

    def tearDown(self) -> None:
        for name, wert in self.sicherung.items():
            if wert is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = wert

    def test_zahlen_werden_begrenzt(self) -> None:
        os.environ["MCSM_ROUTER_PORT"] = "99999"
        self.assertEqual(router._zahl("MCSM_ROUTER_PORT", 25565, kleinster=0, groesster=65535),
                         65535)
        os.environ["MCSM_ROUTER_PORT"] = "quatsch"
        self.assertEqual(router._zahl("MCSM_ROUTER_PORT", 25565, kleinster=0, groesster=65535),
                         25565)
        os.environ["MCSM_ROUTER_PORT"] = ""
        self.assertEqual(router._zahl("MCSM_ROUTER_PORT", 25565), 25565)

    def test_kommazahl(self) -> None:
        os.environ["MCSM_ROUTER_TIMEOUT"] = "2,5"
        self.assertAlmostEqual(router._kommazahl("MCSM_ROUTER_TIMEOUT", 5.0), 2.5)

    def test_pfade(self) -> None:
        os.environ["MCSM_ROUTES"] = "/tmp/pruefung.json"
        self.assertEqual(str(router.routen_pfad()), str(pathlib.Path("/tmp/pruefung.json")))
        os.environ.pop("MCSM_ROUTES")
        self.assertEqual(str(router.routen_pfad()), str(pathlib.Path(router.STANDARD_ROUTEN)))

    def test_pruefen_laeuft_ohne_netz(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcsm-router-test-"))
        try:
            (tmp / "routes.json").write_text(json.dumps({"eins": {"port": 25567}}),
                                             encoding="utf-8")
            os.environ["MCSM_ROUTES"] = str(tmp / "routes.json")
            os.environ["MCSM_ROUTER_LOG"] = str(tmp / "router.log")
            os.environ["MCSM_ROUTER_LOG_STDOUT"] = "0"
            self.assertEqual(router.main(["--pruefen"]), 0)
            self.assertEqual(router.main(["--version"]), 0)
        finally:
            router.schliesse_protokoll()
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- Schlafende Server

class Weckdienst:
    """Ersatz für den Dienst mcsmd: nimmt den Weckruf an und merkt sich, was ankam."""

    def __init__(self, antwort: dict | None = None, status: int = 200) -> None:
        self.antwort = dict(antwort or {"ok": True, "gestartet": True,
                                        "meldung": router.MELDUNG_STARTET})
        self.status = int(status)
        self.rufe: list[dict] = []
        self._lock = threading.Lock()
        aussen = self

        class Griff(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):                      # noqa: A003
                pass

            def do_POST(self):                                      # noqa: N802
                laenge = int(self.headers.get("Content-Length") or 0)
                rumpf = self.rfile.read(laenge) if laenge else b"{}"
                try:
                    daten = json.loads(rumpf.decode("utf-8"))
                except ValueError:
                    daten = {}
                with aussen._lock:
                    aussen.rufe.append({"pfad": self.path,
                                        "geheimnis": self.headers.get("X-MCSM-Router", ""),
                                        "daten": daten})
                aus = json.dumps(aussen.antwort, ensure_ascii=False).encode("utf-8")
                self.send_response(aussen.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(aus)))
                self.end_headers()
                self.wfile.write(aus)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Griff)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def anzahl(self) -> int:
        with self._lock:
            return len(self.rufe)

    def stoppe(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


class SchlafTest(unittest.TestCase):
    """Ein gehosteter Server, der gerade aus ist, aber geweckt werden darf."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcsm-router-schlaf-"))
        self.datei = self.tmp / "routes.json"
        self.geheimnis_datei = self.tmp / "router_secret"
        self.geheimnis_datei.write_text("g" * 64 + "\n", encoding="utf-8")
        self.env_backup = {k: os.environ.get(k) for k in
                           ("MCSM_ROUTER_API", "MCSM_ROUTER_SECRET")}
        os.environ["MCSM_ROUTER_SECRET"] = str(self.geheimnis_datei)
        self.dienste: list[Dienst] = []
        self.weckdienste: list[Weckdienst] = []
        # Ein Port, auf dem sicher niemand lauscht: der „schlafende Server“.
        tot = socket.socket()
        tot.bind(("127.0.0.1", 0))
        self.toter_port = tot.getsockname()[1]
        tot.close()

    def tearDown(self) -> None:
        for dienst in self.dienste:
            dienst.stoppe()
        for weck in self.weckdienste:
            weck.stoppe()
        for name, wert in self.env_backup.items():
            if wert is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = wert
        shutil.rmtree(self.tmp, ignore_errors=True)

    def weckdienst(self, **mehr) -> Weckdienst:
        neu = Weckdienst(**mehr)
        self.weckdienste.append(neu)
        os.environ["MCSM_ROUTER_API"] = f"http://127.0.0.1:{neu.port}"
        return neu

    def tabelle(self, inhalt: dict) -> None:
        self.datei.write_text(json.dumps(inhalt, ensure_ascii=False), encoding="utf-8")

    def dienst(self, **mehr) -> Dienst:
        neu = Dienst(self.datei, **mehr)
        self.dienste.append(neu)
        return neu

    def schlaefer(self, **mehr) -> dict:
        eintrag = {"port": self.toter_port, "instanz": "efc71eb9c547",
                   "name": "Eutopia", "max": 20, "wecken": True}
        eintrag.update(mehr)
        return {"eutopia": eintrag}

    @staticmethod
    def erstes_json(antwort: bytes) -> dict:
        _kennziffer, rumpf = pakete(antwort)[0]
        text, _ = router.lese_string(rumpf, 0, max_bytes=65535)
        return json.loads(text)

    # ---------------------------------------------------------------- Tabelle

    def test_tabelle_liest_die_neuen_felder(self) -> None:
        self.tabelle(self.schlaefer())
        tabelle = router.Routen(self.datei, domain=DOMAIN, pruef_abstand=0.0)
        treffer = tabelle.finde("eutopia." + DOMAIN)
        self.assertIsNotNone(treffer)
        self.assertEqual(treffer.anzeige, "Eutopia")
        self.assertEqual(treffer.max_spieler, 20)
        self.assertTrue(treffer.wecken)

    def test_alte_tabelle_ohne_neue_felder_bleibt_lesbar(self) -> None:
        self.tabelle({"eutopia": 25566})
        tabelle = router.Routen(self.datei, domain=DOMAIN, pruef_abstand=0.0)
        treffer = tabelle.finde("eutopia." + DOMAIN)
        self.assertEqual(treffer.port, 25566)
        self.assertEqual(treffer.anzeige, "")
        self.assertFalse(treffer.wecken)

    # ---------------------------------------------------------------- Ping

    def test_ping_auf_schlafenden_server_sieht_normal_aus(self) -> None:
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=1) + STATUS_ANFRAGE)
            antwort = lies_bis_ende(klient)
        daten = self.erstes_json(antwort)
        self.assertEqual(daten["version"]["name"], "Eutopia")
        self.assertEqual(daten["players"], {"max": 20, "online": 0, "sample": []})
        self.assertEqual(daten["description"]["text"],
                         "Server ist ausgeschaltet – tritt bei, um ihn zu starten")
        # Keine Fehlermeldung: also auch keine Fehlerfarbe.
        self.assertNotIn("color", daten["description"])
        self.assertEqual(dienst.verteiler.zahlen["schlaeft"], 1)

    def test_ping_beantwortet_auch_die_laufzeitmessung(self) -> None:
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        nutzlast = b"\x08\x07\x06\x05\x04\x03\x02\x01"
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=1)
                           + STATUS_ANFRAGE + ping_anfrage(nutzlast))
            antwort = lies_bis_ende(klient)
        teile = pakete(antwort)
        self.assertEqual(len(teile), 2, "Statusantwort und Laufzeitmessung erwartet")
        self.assertEqual(teile[1], (router.PAKET_PING, nutzlast))

    def test_ohne_ruhezustand_bleibt_es_bei_laeuft_nicht(self) -> None:
        """Wer den Ruhezustand abgeschaltet hat, soll keine Weck-Einladung sehen."""
        self.tabelle(self.schlaefer(wecken=False))
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=1) + STATUS_ANFRAGE)
            antwort = lies_bis_ende(klient)
        daten = self.erstes_json(antwort)
        self.assertEqual(daten["description"]["text"], "Dieser Server läuft gerade nicht")

    # ---------------------------------------------------------------- Beitritt weckt

    def test_beitritt_weckt_und_trennt_freundlich(self) -> None:
        weck = self.weckdienst()
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        daten = self.erstes_json(antwort)
        self.assertEqual(daten["text"], router.MELDUNG_STARTET)
        self.assertEqual(weck.anzahl(), 1)
        ruf = weck.rufe[0]
        self.assertEqual(ruf["pfad"], "/api/router/wake")
        self.assertEqual(ruf["geheimnis"], "g" * 64)
        self.assertEqual(ruf["daten"]["instanz"], "efc71eb9c547")
        self.assertEqual(ruf["daten"]["marke"], "eutopia")
        self.assertEqual(dienst.verteiler.zahlen["geweckt"], 1)

    def test_uebergabe_weckt_ebenso(self) -> None:
        """Zustand 3 („transfer“, ab 1.20.5) ist ein Beitritt."""
        weck = self.weckdienst()
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=3))
            lies_bis_ende(klient)
        self.assertEqual(weck.anzahl(), 1)

    def test_ping_weckt_nicht(self) -> None:
        weck = self.weckdienst()
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=1) + STATUS_ANFRAGE)
            lies_bis_ende(klient)
        self.assertEqual(weck.anzahl(), 0, "Ein Blick in die Serverliste darf nichts starten")

    def test_abgelehnter_weckruf_nennt_den_grund(self) -> None:
        grund = "Dein Pass erlaubt 1 gleichzeitig laufenden Server – es läuft bereits Welt-B."
        self.weckdienst(antwort={"ok": False, "gestartet": False, "meldung": grund})
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        self.assertEqual(self.erstes_json(antwort)["text"], grund)
        self.assertEqual(dienst.verteiler.zahlen["weckung_abgelehnt"], 1)

    def test_fehler_des_dienstes_wird_zur_hoeflichen_meldung(self) -> None:
        self.weckdienst(antwort={"error": "Bitte neu anmelden."}, status=404)
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        self.assertEqual(self.erstes_json(antwort)["text"], "Bitte neu anmelden.")

    def test_ohne_dienst_bleibt_der_spieler_nicht_ratlos(self) -> None:
        os.environ["MCSM_ROUTER_API"] = "http://127.0.0.1:1"        # dort lauscht niemand
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient(frist=8.0) as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient, frist=8.0)
        self.assertEqual(self.erstes_json(antwort)["text"], router.MELDUNG_WECKEN_FEHLT)

    def test_ohne_geheimnis_wird_gar_nicht_gerufen(self) -> None:
        weck = self.weckdienst()
        self.geheimnis_datei.unlink()
        self.tabelle(self.schlaefer())
        dienst = self.dienst()
        with dienst.klient() as klient:
            klient.sendall(handshake("eutopia." + DOMAIN, zustand=2))
            antwort = lies_bis_ende(klient)
        self.assertEqual(weck.anzahl(), 0)
        self.assertEqual(self.erstes_json(antwort)["text"], router.MELDUNG_WECKEN_FEHLT)

    def test_geheimnis_wird_frisch_gelesen(self) -> None:
        """Startet der Dienst neu, gilt sofort das neue Geheimnis – ohne Neustart des Verteilers."""
        self.assertEqual(router.geheimnis(), "g" * 64)
        self.geheimnis_datei.write_text("h" * 64 + "\n", encoding="utf-8")
        self.assertEqual(router.geheimnis(), "h" * 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
