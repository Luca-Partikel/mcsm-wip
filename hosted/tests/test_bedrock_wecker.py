"""Selbsttests des Bedrock-Weckers (hosted/core/bedrock_wecker.py).

Geprüft wird in drei Stufen:

1. **Pakete** – Bau und Zerlegung mit echten Byte-Folgen (RakNet Unconnected Ping/Pong und
   Open Connection Request 1). Kein Socket, keine Zeit, keine Zufälle.
2. **Ein Horcher** – was auf ein Paket geantwortet wird, und dass ein Verbindungsversuch genau
   **einen** Weckruf auslöst, auch wenn RakNet ihn zehnmal schickt.
3. **Der Wecker** – echte UDP-Sockets auf der Rückschleife: binden, Port wechseln, Port
   hergeben, Sperre gegen erneutes Binden, belegter Port.

    python -m unittest discover -s hosted/tests
"""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import socket
import struct
import sys
import threading
import time
import unittest

_HOSTED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "hosted_core" not in sys.modules:
    _spec = importlib.machinery.ModuleSpec("hosted_core", None, is_package=True)
    _pkg = importlib.util.module_from_spec(_spec)
    _pkg.__path__ = [os.path.join(_HOSTED, "core")]
    sys.modules["hosted_core"] = _pkg

bw = importlib.import_module("hosted_core.bedrock_wecker")


def freier_udp_port() -> int:
    """Einen gerade freien UDP-Port auf der Rückschleife besorgen."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ist_frei(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def echter_ping(zeit: int = 0x0123456789ABCDEF, guid: int = 0x1122334455667788) -> bytes:
    """Ein Unconnected Ping, Byte für Byte so, wie ihn eine Konsole schickt (33 Byte)."""
    return (b"\x01" + struct.pack(">Q", zeit)
            + bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
            + struct.pack(">Q", guid))


def echter_verbindungsversuch(mtu_fuellung: int = 1464) -> bytes:
    """Ein Open Connection Request 1: Kennziffer, MAGIC, Protokollfassung, MTU-Füllung."""
    return (b"\x05" + bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
            + b"\x0b" + b"\x00" * mtu_fuellung)


# --------------------------------------------------------------------------- 1. Pakete

class Pakete(unittest.TestCase):

    def test_magic_ist_die_raknet_folge(self):
        self.assertEqual(bw.MAGIC.hex(), "00ffff00fefefefefdfdfdfd12345678")
        self.assertEqual(len(bw.MAGIC), 16)

    def test_ping_wird_zerlegt(self):
        ping = bw.zerlege_ping(echter_ping())
        self.assertEqual(ping.zeit, 0x0123456789ABCDEF)
        self.assertEqual(ping.klient_guid, 0x1122334455667788)

    def test_ping_ohne_klient_guid_geht_auch(self):
        """Manche Clients lassen die GUID weg – das ist kein Fehler."""
        kurz = echter_ping()[:25]
        self.assertEqual(len(kurz), 25)
        self.assertEqual(bw.zerlege_ping(kurz).klient_guid, 0)

    def test_offener_ping_zaehlt_als_ping(self):
        """``0x02`` ist derselbe Ping von einem älteren Client."""
        daten = b"\x02" + echter_ping()[1:]
        self.assertEqual(bw.zerlege_ping(daten).zeit, 0x0123456789ABCDEF)

    def test_falsche_kennziffer_wird_abgelehnt(self):
        daten = b"\x09" + echter_ping()[1:]
        with self.assertRaises(bw.Protokollfehler):
            bw.zerlege_ping(daten)

    def test_fehlendes_magic_wird_abgelehnt(self):
        daten = bytearray(echter_ping())
        daten[10] = 0x00                        # ein Byte mitten im MAGIC verbiegen (0xff -> 0x00)
        with self.assertRaises(bw.Protokollfehler):
            bw.zerlege_ping(bytes(daten))

    def test_zu_kurz_wird_abgelehnt(self):
        with self.assertRaises(bw.Protokollfehler):
            bw.zerlege_ping(echter_ping()[:20])
        with self.assertRaises(bw.Protokollfehler):
            bw.zerlege_ping(b"")

    def test_pong_hat_den_richtigen_aufbau(self):
        auskunft = "MCPE;Test;800;1.21.100;0;10;42;zwei;Survival;1;19132;19132;"
        daten = bw.baue_pong(0x0123456789ABCDEF, 42, auskunft)
        self.assertEqual(daten[0], 0x1C)
        self.assertEqual(struct.unpack_from(">Q", daten, 1)[0], 0x0123456789ABCDEF)
        self.assertEqual(struct.unpack_from(">Q", daten, 9)[0], 42)
        self.assertEqual(daten[17:33], bw.MAGIC)
        self.assertEqual(struct.unpack_from(">H", daten, 33)[0], len(auskunft.encode("utf-8")))
        self.assertEqual(daten[35:].decode("utf-8"), auskunft)
        self.assertEqual(len(daten), 35 + len(auskunft.encode("utf-8")))

    def test_pong_spiegelt_den_zeitstempel(self):
        """Daran misst der Client die Laufzeit – eine andere Zahl und der Eintrag bleibt leer."""
        for zeit in (0, 1, 0xFFFFFFFFFFFFFFFF):
            zurueck = bw.zerlege_pong(bw.baue_pong(zeit, 7, "MCPE;x;800;1;0;10;7;;Survival;1;1;1;"))
            self.assertEqual(zurueck.zeit, zeit)

    def test_pong_hin_und_zurueck(self):
        text = "MCPE;✦ MCSM ¦ Eutopia;800;1.21.100;0;10;99;schläft;Survival;1;19132;19132;"
        zurueck = bw.zerlege_pong(bw.baue_pong(5, 99, text))
        self.assertEqual(zurueck.auskunft, text)
        self.assertEqual(zurueck.guid, 99)

    def test_pong_mit_umlauten_wird_in_byte_gezaehlt(self):
        """Die Länge im Pong ist eine **Byte**-Länge, keine Zeichenzahl."""
        text = "MCPE;ÄÖÜ;800;1;0;10;1;;Survival;1;1;1;"
        daten = bw.baue_pong(0, 1, text)
        self.assertEqual(struct.unpack_from(">H", daten, 33)[0], len(text.encode("utf-8")))
        self.assertGreater(len(text.encode("utf-8")), len(text))
        self.assertEqual(bw.zerlege_pong(daten).auskunft, text)

    def test_ping_bauen_und_selbst_zerlegen(self):
        roh = bw.baue_ping(12345, 678)
        self.assertEqual(len(roh), 33)
        self.assertEqual(roh, echter_ping(12345, 678))
        self.assertEqual(bw.zerlege_ping(roh), bw.Ping(zeit=12345, klient_guid=678))

    def test_verbindungsversuch_wird_erkannt(self):
        self.assertTrue(bw.ist_verbindungsversuch(echter_verbindungsversuch()))
        self.assertFalse(bw.ist_verbindungsversuch(echter_ping()))
        # 0x05 ohne MAGIC ist kein RakNet-Verbindungsversuch.
        self.assertFalse(bw.ist_verbindungsversuch(b"\x05" + b"\x00" * 30))
        self.assertFalse(bw.ist_verbindungsversuch(b"\x05"))

    def test_auskunft_hat_die_felder_des_protokolls(self):
        text = bw.baue_auskunft(zeile1="✦ MCSM ¦ Eutopia", protokoll=800, version="1.21.100",
                                online=0, max_spieler=10, guid=4711,
                                zeile2=bw.ZEILE2_AUS, spielmodus="Survival", port_v4=19132)
        self.assertTrue(text.endswith(";"))
        teile = text.split(";")
        self.assertEqual(teile[0], "MCPE")
        self.assertEqual(teile[1], "✦ MCSM ¦ Eutopia")
        self.assertEqual(teile[2], "800")
        self.assertEqual(teile[3], "1.21.100")
        self.assertEqual(teile[4], "0")
        self.assertEqual(teile[5], "10")
        self.assertEqual(teile[6], "4711")
        self.assertEqual(teile[7], bw.ZEILE2_AUS)
        self.assertEqual(teile[8], "Survival")
        self.assertEqual(teile[9], "1")
        self.assertEqual(teile[10], "19132")
        self.assertEqual(teile[11], "19132")

    def test_semikolon_im_namen_verrutscht_die_auskunft_nicht(self):
        text = bw.baue_auskunft(zeile1="Lucas; Welt", protokoll=800, version="1.2", online=0,
                                max_spieler=10, guid=1, zeile2="a;b", spielmodus="Survival",
                                port_v4=19132)
        self.assertEqual(len(text.split(";")), 13)      # 12 Felder + leerer Rest nach dem Ende
        self.assertNotIn(";", text.split(";")[1])
        self.assertEqual(text.split(";")[1], "Lucas Welt")

    def test_auskunft_eines_echten_servers_wird_zerlegt(self):
        """So antwortet Geyser (Feldreihenfolge wie im Bedrock-Protokoll)."""
        echt = ("MCPE;Dedicated Server;800;1.21.100;3;10;13253860892328930865;Bedrock level;"
                "Survival;1;19132;19133;")
        aus = bw.zerlege_auskunft(echt)
        self.assertEqual(aus.zeile1, "Dedicated Server")
        self.assertEqual(aus.protokoll, 800)
        self.assertEqual(aus.version, "1.21.100")
        self.assertEqual(aus.online, 3)
        self.assertEqual(aus.max_spieler, 10)
        self.assertEqual(aus.zeile2, "Bedrock level")
        self.assertEqual(aus.spielmodus, "Survival")
        self.assertEqual(aus.port_v4, 19132)
        self.assertEqual(aus.port_v6, 19133)

    def test_fremde_auskunft_wird_abgelehnt(self):
        with self.assertRaises(bw.Protokollfehler):
            bw.zerlege_auskunft("irgendwas;anderes")

    def test_kurze_auskunft_geht_trotzdem(self):
        """Ältere Server nennen weniger Felder – der Rest bleibt auf der Vorgabe."""
        aus = bw.zerlege_auskunft("MCPE;Welt;800;1.20.0;0;10;7;")
        self.assertEqual(aus.zeile1, "Welt")
        self.assertEqual(aus.spielmodus, "Survival")
        self.assertEqual(aus.port_v4, 0)

    def test_guid_bleibt_gleich_und_ist_positiv(self):
        erste = bw.guid_fuer("efc71eb9c547")
        self.assertEqual(erste, bw.guid_fuer("efc71eb9c547"))
        self.assertNotEqual(erste, bw.guid_fuer("04378eabb963"))
        self.assertGreater(erste, 0)
        self.assertLess(erste, 2 ** 63)

    def test_zeile1_im_stil_des_plugins(self):
        self.assertEqual(bw.zeile1_fuer("Eutopia"), "✦ MCSM ¦ Eutopia")
        self.assertEqual(bw.zeile1_fuer(""), "✦ MCSM ¦ Minecraft Server")
        self.assertNotIn("\n", bw.zeile1_fuer("A\nB"))

    def test_texte_sind_deutsch_und_ohne_fehlerfarbe(self):
        self.assertIn("ausgeschaltet", bw.ZEILE2_AUS)
        self.assertIn("tritt bei", bw.ZEILE2_AUS)
        self.assertIn("startet gerade", bw.ZEILE2_STARTET)
        self.assertIn("einer Minute", bw.ZEILE2_STARTET)


# --------------------------------------------------------------------------- 2. Ein Horcher

class EinHorcher(unittest.TestCase):

    def bau(self, **aenderung):
        felder = {"instanz": "abc123", "port": 19132, "zeile1": bw.zeile1_fuer("Eutopia"),
                  "zeile2": bw.ZEILE2_AUS, "max_spieler": 12, "protokoll": 800,
                  "version": "1.21.100", "spielmodus": "Survival", "wecken": True}
        felder.update(aenderung)
        return bw.Ziel(**felder)

    def test_ping_bekommt_die_auskunft_des_schlafenden_servers(self):
        horcher = bw.Horcher(self.bau())
        antwort = horcher.behandle(echter_ping(), ("203.0.113.9", 55555))
        pong = bw.zerlege_pong(antwort)
        self.assertEqual(pong.zeit, 0x0123456789ABCDEF)
        self.assertEqual(pong.guid, bw.guid_fuer("abc123"))
        aus = bw.zerlege_auskunft(pong.auskunft)
        self.assertEqual(aus.zeile1, "✦ MCSM ¦ Eutopia")
        self.assertEqual(aus.online, 0)
        self.assertEqual(aus.max_spieler, 12)
        self.assertEqual(aus.zeile2, bw.ZEILE2_AUS)
        self.assertEqual(aus.port_v4, 19132)
        self.assertEqual(horcher.pings, 1)

    def test_kaputtes_paket_bekommt_keine_antwort(self):
        horcher = bw.Horcher(self.bau())
        self.assertIsNone(horcher.behandle(b"\x01\x02\x03", ("203.0.113.9", 1)))
        self.assertIsNone(horcher.behandle(b"", ("203.0.113.9", 1)))
        self.assertEqual(horcher.pings, 0)

    def test_nach_dem_weckruf_steht_startet_gerade_in_zeile_zwei(self):
        horcher = bw.Horcher(self.bau())
        horcher.startet_jetzt()
        aus = bw.zerlege_auskunft(
            bw.zerlege_pong(horcher.behandle(echter_ping(), ("203.0.113.9", 1))).auskunft)
        self.assertEqual(aus.zeile2, bw.ZEILE2_STARTET)

    def test_abgelehnte_weckung_nennt_den_grund_in_zeile_zwei(self):
        horcher = bw.Horcher(self.bau())
        horcher.abgelehnt("Dein Pass erlaubt 1 gleichzeitig laufenden Server.")
        self.assertEqual(horcher.zeile2(), "Dein Pass erlaubt 1 gleichzeitig laufenden Server.")

    def test_verbindungsversuch_weckt_genau_einmal(self):
        """RakNet schickt den Versuch mehrmals je Sekunde – ein Start genügt."""
        rufe: list = []
        sperre = threading.Event()

        def wecken(iid):
            rufe.append(iid)
            sperre.set()
            return True, bw.ZEILE2_STARTET

        horcher = bw.Horcher(self.bau(), wecken=wecken)
        for _ in range(10):
            horcher.behandle(echter_verbindungsversuch(), ("203.0.113.9", 1))
        self.assertTrue(sperre.wait(3), "Der Weckruf ist nicht angekommen")
        time.sleep(0.2)
        self.assertEqual(rufe, ["abc123"])
        self.assertEqual(horcher.weckrufe, 1)
        self.assertEqual(horcher.zeile2(), bw.ZEILE2_STARTET)

    def test_ohne_ruhezustand_weckt_ein_beitritt_nichts(self):
        rufe: list = []
        horcher = bw.Horcher(self.bau(wecken=False, zeile2=bw.ZEILE2_LAEUFT_NICHT),
                             wecken=lambda iid: (rufe.append(iid), (True, ""))[1])
        horcher.behandle(echter_verbindungsversuch(), ("203.0.113.9", 1))
        time.sleep(0.2)
        self.assertEqual(rufe, [])
        self.assertEqual(horcher.zeile2(), bw.ZEILE2_LAEUFT_NICHT)

    def test_abgelehnte_weckung_wird_gemeldet_und_gemerkt(self):
        meldungen: list = []
        horcher = bw.Horcher(
            self.bau(), wecken=lambda iid: (False, "Du hast gerade keinen gültigen Pass."),
            melder=meldungen.append)
        horcher.behandle(echter_verbindungsversuch(), ("203.0.113.9", 1))
        ende = time.time() + 3
        while time.time() < ende and horcher.zeile2() == bw.ZEILE2_STARTET:
            time.sleep(0.05)
        self.assertEqual(horcher.zeile2(), "Du hast gerade keinen gültigen Pass.")
        self.assertTrue(any("keinen gültigen Pass" in m for m in meldungen), meldungen)

    def test_der_port_wechselt_nicht_unter_der_hand(self):
        horcher = bw.Horcher(self.bau())
        with self.assertRaises(ValueError):
            horcher.aktualisiere(self.bau(port=19140))
        horcher.aktualisiere(self.bau(max_spieler=30))
        self.assertEqual(horcher.ziel.max_spieler, 30)


# --------------------------------------------------------------------------- 3. Der Wecker

class DerWecker(unittest.TestCase):

    def setUp(self) -> None:
        self.rufe: list = []
        self.meldungen: list = []
        self.wecker = bw.Wecker(wecken=self._wecken, melder=self.meldungen.append,
                                host="127.0.0.1")

    def tearDown(self) -> None:
        self.wecker.schliessen()

    def _wecken(self, iid):
        self.rufe.append(iid)
        return True, bw.ZEILE2_STARTET

    def ziel(self, instanz: str, port: int, **aenderung):
        felder = {"instanz": instanz, "port": port, "zeile1": bw.zeile1_fuer(instanz),
                  "zeile2": bw.ZEILE2_AUS}
        felder.update(aenderung)
        return bw.Ziel(**felder)

    def frage(self, port: int, *, frist: float = 2.0):
        """Wie eine Konsole: Ping schicken und den Pong lesen."""
        return bw.frage_server(port, host="127.0.0.1", frist=frist)

    def test_ein_schlafender_server_antwortet_auf_dem_bedrock_port(self):
        port = freier_udp_port()
        stand = self.wecker.abgleichen([self.ziel("eutopia1", port)])
        self.assertEqual(stand["fehler"], {})
        self.assertEqual(stand["neu"], [f"eutopia1:{port}"])
        aus = self.frage(port)
        self.assertIsNotNone(aus, "Der Wecker hat nicht geantwortet")
        self.assertEqual(aus.zeile1, "✦ MCSM ¦ eutopia1")
        self.assertEqual(aus.online, 0)
        self.assertEqual(aus.zeile2, bw.ZEILE2_AUS)
        self.assertEqual(self.wecker.port_von("eutopia1"), port)

    def test_beitritt_weckt_ueber_den_udp_port(self):
        port = freier_udp_port()
        self.wecker.abgleichen([self.ziel("eutopia2", port)])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(echter_verbindungsversuch(), ("127.0.0.1", port))
        ende = time.time() + 3
        while time.time() < ende and not self.rufe:
            time.sleep(0.05)
        self.assertEqual(self.rufe, ["eutopia2"])
        # Und der nächste Ping sagt dem Spieler, was los ist.
        aus = self.frage(port)
        self.assertIsNotNone(aus)
        self.assertEqual(aus.zeile2, bw.ZEILE2_STARTET)

    def test_zwei_instanzen_zwei_ports(self):
        p1, p2 = freier_udp_port(), freier_udp_port()
        self.assertNotEqual(p1, p2)
        self.wecker.abgleichen([self.ziel("eins", p1), self.ziel("zwei", p2)])
        self.assertEqual(self.frage(p1).zeile1, "✦ MCSM ¦ eins")
        self.assertEqual(self.frage(p2).zeile1, "✦ MCSM ¦ zwei")
        self.assertEqual(len(self.wecker.stand()), 2)

    def test_geaenderte_angaben_wirken_ohne_neuen_socket(self):
        port = freier_udp_port()
        self.wecker.abgleichen([self.ziel("drei", port)])
        stand = self.wecker.abgleichen([self.ziel("drei", port, zeile2="Kein Platz mehr.",
                                                 max_spieler=44)])
        self.assertEqual(stand["neu"], [])
        self.assertEqual(stand["weg"], [])
        aus = self.frage(port)
        self.assertEqual(aus.zeile2, "Kein Platz mehr.")
        self.assertEqual(aus.max_spieler, 44)

    def test_portwechsel_legt_einen_neuen_socket_an(self):
        alt, neu = freier_udp_port(), freier_udp_port()
        self.wecker.abgleichen([self.ziel("vier", alt)])
        stand = self.wecker.abgleichen([self.ziel("vier", neu)])
        self.assertEqual(stand["weg"], [f"vier:{alt}"])
        self.assertEqual(stand["neu"], [f"vier:{neu}"])
        self.assertTrue(ist_frei(alt), "Der alte Port wurde nicht hergegeben")
        self.assertIsNotNone(self.frage(neu))

    def test_verschwundenes_ziel_gibt_den_port_zurueck(self):
        port = freier_udp_port()
        self.wecker.abgleichen([self.ziel("fuenf", port)])
        stand = self.wecker.abgleichen([])
        self.assertEqual(stand["weg"], [f"fuenf:{port}"])
        self.assertTrue(ist_frei(port))
        self.assertEqual(self.wecker.port_von("fuenf"), 0)

    def test_freigeben_macht_den_port_sofort_frei(self):
        """Der heikelste Punkt: Geyser muss ihn gleich danach binden können."""
        port = freier_udp_port()
        self.wecker.abgleichen([self.ziel("sechs", port)])
        self.assertFalse(ist_frei(port))
        self.assertTrue(self.wecker.freigeben("sechs"))
        self.assertTrue(ist_frei(port), "Der Port war nach dem Freigeben noch belegt")

    def test_nach_dem_freigeben_bindet_der_abgleich_nicht_wieder(self):
        """Solange der Server startet, hat der Wecker auf dem Port nichts zu suchen."""
        port = freier_udp_port()
        self.wecker.abgleichen([self.ziel("sieben", port)])
        self.wecker.freigeben("sieben")
        stand = self.wecker.abgleichen([self.ziel("sieben", port)])
        self.assertEqual(stand["neu"], [])
        self.assertTrue(ist_frei(port))
        self.assertTrue(self.wecker.gesperrt("sieben"))
        # Erst wenn der Serverprozess wirklich weg ist, wird die Sperre aufgehoben.
        self.wecker.entsperren("sieben")
        self.assertFalse(self.wecker.gesperrt("sieben"))
        stand = self.wecker.abgleichen([self.ziel("sieben", port)])
        self.assertEqual(stand["neu"], [f"sieben:{port}"])
        self.assertIsNotNone(self.frage(port))

    def test_freigeben_ohne_horcher_ist_in_ordnung(self):
        self.assertTrue(self.wecker.freigeben("gibt-es-nicht"))

    def test_belegter_port_wird_gemeldet_und_weiter_versucht(self):
        port = freier_udp_port()
        fremd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fremd.bind(("127.0.0.1", port))
        try:
            stand = self.wecker.abgleichen([self.ziel("acht", port)])
            self.assertIn("acht", stand["fehler"])
            self.assertEqual(stand["neu"], [])
            self.assertTrue(any("lässt sich nicht belegen" in m for m in self.meldungen),
                            self.meldungen)
            # Zweiter Anlauf: dieselbe Klage wird nicht noch einmal protokolliert.
            vorher = len(self.meldungen)
            self.wecker.abgleichen([self.ziel("acht", port)])
            self.assertEqual(len(self.meldungen), vorher)
        finally:
            fremd.close()
        # Ist der Port frei, klappt es beim nächsten Abgleich von selbst.
        stand = self.wecker.abgleichen([self.ziel("acht", port)])
        self.assertEqual(stand["neu"], [f"acht:{port}"])

    def test_schliessen_gibt_alle_ports_her(self):
        p1, p2 = freier_udp_port(), freier_udp_port()
        self.wecker.abgleichen([self.ziel("neun", p1), self.ziel("zehn", p2)])
        gehalten = self.wecker.schliessen()
        self.assertEqual(gehalten, sorted([f"neun:{p1}", f"zehn:{p2}"]))
        self.assertTrue(ist_frei(p1))
        self.assertTrue(ist_frei(p2))
        self.assertEqual(self.wecker.stand(), [])

    def test_ziel_ohne_port_wird_uebersprungen(self):
        stand = self.wecker.abgleichen([self.ziel("ohne", 0)])
        self.assertEqual(stand["neu"], [])
        self.assertEqual(self.wecker.stand(), [])


# --------------------------------------------------------------------------- 4. Vom Server lernen

class VomServerLernen(unittest.TestCase):
    """`frage_server` gegen einen Socket, der sich wie Geyser verhält."""

    def setUp(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = int(self.sock.getsockname()[1])
        self.antwort = ("MCPE;Eutopia;844;1.21.130;2;20;7788;Bedrock level;Creative;1;"
                        f"{self.port};{self.port};")
        self.faden = threading.Thread(target=self._dienen, daemon=True)
        self.faden.start()

    def tearDown(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        self.faden.join(2)

    def _dienen(self) -> None:
        try:
            daten, gegenstelle = self.sock.recvfrom(2048)
        except OSError:
            return
        try:
            ping = bw.zerlege_ping(daten)
        except bw.Protokollfehler:
            return
        try:
            self.sock.sendto(bw.baue_pong(ping.zeit, 7788, self.antwort), gegenstelle)
        except OSError:
            pass

    def test_auskunft_des_laufenden_servers_wird_gelesen(self):
        aus = bw.frage_server(self.port, host="127.0.0.1", frist=3.0)
        self.assertIsNotNone(aus, "Keine Antwort vom Doppel")
        self.assertEqual(aus.protokoll, 844)
        self.assertEqual(aus.version, "1.21.130")
        self.assertEqual(aus.spielmodus, "Creative")

    def test_ohne_antwort_gibt_es_none(self):
        self.sock.close()
        self.assertIsNone(bw.frage_server(freier_udp_port(), host="127.0.0.1", frist=0.3))


if __name__ == "__main__":                                          # pragma: no cover
    unittest.main()
