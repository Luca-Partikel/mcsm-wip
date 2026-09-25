"""Selbsttests für Serverinstanzen – vor allem für die Frage „darf dieser Server jetzt starten?“.

Läuft ohne Server und ohne Fremdpakete. Der freie Platz wird immer ausdrücklich übergeben
(``free_bytes``), damit die Tests nicht von der Platte des Rechners abhängen.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest

HOSTED = pathlib.Path(__file__).resolve().parent.parent
PKG = "mcsm_hosted"


def _module(name: str):
    """Modul aus hosted/core laden (eigener Paketname wegen des gleichnamigen Ordners core/ im Projekt)."""
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(HOSTED / "core")]        # type: ignore[attr-defined]
        pkg.__package__ = PKG
        sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.{name}")


store_hosted = _module("store_hosted")
users = _module("users")
passes = _module("passes")
instances = _module("instances")

T0 = 1_700_000_000
TAG = 86400
VIEL_PLATZ = 20 * 1024 ** 3          # 20 GB frei – die Platte steht in diesen Tests nicht im Weg


class Basis(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._alt = os.environ.get("MCSM_DATA")
        os.environ["MCSM_DATA"] = self._tmp.name
        store_hosted.ensure_dir()
        self.konto = users.create_user("Luca", now=T0)

    def tearDown(self) -> None:
        if self._alt is None:
            os.environ.pop("MCSM_DATA", None)
        else:
            os.environ["MCSM_DATA"] = self._alt
        self._tmp.cleanup()

    # -------------------------------------------------------------- Hilfen
    def pass_lokal(self, anzahl: int = 2, ram: int = 8192, tage: int = 30, now: int = T0) -> dict:
        return passes.issue_pass(self.konto["id"], "local", tage, anzahl, ram, now=now)

    def pass_premium(self, anzahl: int = 2, ram: int = 8192, tage: int = 30, now: int = T0) -> dict:
        return passes.issue_pass(self.konto["id"], "premium", tage, anzahl, ram, now=now)

    def instanz(self, name: str, ram: int = 4096, *, origin: str = "local", hosted: bool = True,
                now: int = T0) -> dict:
        """Instanz anlegen und (wenn gewünscht) in den Zustand ``hosted`` bringen."""
        entry = instances.create_instance(self.konto["id"], name, ram_mb=ram, origin=origin, now=now)
        if hosted and entry["state"] != "hosted":
            instances.set_state(entry["id"], "uploading", now=now)
            entry = instances.set_state(entry["id"], "hosted", now=now)
        return entry

    def laeuft(self, name: str, ram: int = 4096, *, now: int = T0) -> dict:
        entry = self.instanz(name, ram, now=now)
        return instances.set_running(entry["id"], True, now=now)

    def pruefe(self, entry: dict, *, now: int = T0, free_bytes: int = VIEL_PLATZ):
        return instances.check_start(entry["id"], now=now, free_bytes=free_bytes)


# ----------------------------------------------------------------------- Anlegen

class TestAnlegen(Basis):

    def test_anlegen_ist_unbegrenzt(self):
        self.pass_lokal(anzahl=1, ram=4096)
        for i in range(5):
            self.instanz(f"Welt-{i}", 4096, hosted=False)
        self.assertEqual(len(instances.instances_of(self.konto["id"])), 5)

    def test_anlegen_ohne_pass_erlaubt(self):
        entry = self.instanz("Welt-A", hosted=False)
        self.assertEqual(entry["state"], "local_only")
        self.assertEqual(entry["origin"], "local")

    def test_name_doppelt(self):
        self.instanz("Welt-A", hosted=False)
        with self.assertRaises(ValueError) as fehler:
            self.instanz("welt-a", hosted=False)
        self.assertIn("schon einen Server", str(fehler.exception))

    def test_ungueltige_angaben(self):
        with self.assertRaises(ValueError):
            instances.create_instance(self.konto["id"], "x", now=T0)
        with self.assertRaises(ValueError):
            instances.create_instance(self.konto["id"], "Welt", server_type="switch", now=T0)
        with self.assertRaises(ValueError):
            instances.create_instance(self.konto["id"], "Welt", ram_mb=64, now=T0)
        with self.assertRaises(ValueError):
            instances.create_instance(self.konto["id"], "Welt", version="../böse", now=T0)
        with self.assertRaises(ValueError) as fehler:
            instances.create_instance("gibtsnicht", "Welt", now=T0)
        self.assertIn("kein Konto", str(fehler.exception))

    def test_premium_instanz_braucht_premium_pass(self):
        self.pass_lokal()
        with self.assertRaises(ValueError) as fehler:
            instances.create_instance(self.konto["id"], "Root-Welt", origin="premium", now=T0)
        self.assertIn("Premium-Pass", str(fehler.exception))
        self.pass_premium()
        entry = instances.create_instance(self.konto["id"], "Root-Welt", origin="premium", now=T0)
        self.assertEqual(entry["state"], "hosted")

    def test_anlegen_vergibt_keinen_port(self):
        """Beim Anlegen wird **kein** Port belegt – sonst sperrt ein Konto alle anderen aus.

        Der Bereich hat rund 100 Java-Ports. Würden sie beim Anlegen vergeben, könnte ein
        einziges Konto mit 100 (auch ausgeschalteten) Instanzen dafür sorgen, dass kein Konto
        mehr eine Java-Instanz anlegen kann. Den wirklichen Port vergibt der PortPool beim Start.
        """
        eins = self.instanz("Welt-A", hosted=False)
        zwei = instances.create_instance(self.konto["id"], "Bedrock-Welt", server_type="bedrock",
                                         now=T0)
        self.assertEqual(eins["ports"], {})
        self.assertEqual(zwei["ports"], {})
        self.assertEqual(instances.ports_in_use(), set())
        # 120 Instanzen sind kein Problem mehr (früher war bei 100 Schluss).
        for i in range(120):
            instances.create_instance(self.konto["id"], f"Masse-{i}", now=T0)
        self.assertEqual(len(instances.instances_of(self.konto["id"])), 122)

    def test_portbereiche_kommen_aus_ports_py(self):
        ports_mod = _module("ports")
        for key, (low, high) in instances.PORT_RANGES.items():
            self.assertEqual((low, high), (ports_mod.POOLS[key].first, ports_mod.POOLS[key].last))

    def test_port_doppelt_vergeben(self):
        eins = self.instanz("Welt-A", hosted=False)
        zwei = self.instanz("Welt-B", hosted=False)
        instances.set_ports(eins["id"], {"java": 25570})
        with self.assertRaises(ValueError) as fehler:
            instances.set_ports(zwei["id"], {"java": 25570})
        self.assertIn("schon an einen anderen Server vergeben", str(fehler.exception))
        with self.assertRaises(ValueError):
            instances.set_ports(zwei["id"], {"java": 80})

    def test_bedrock_hat_auch_einen_java_port(self):
        """BDS mit NetherNet braucht TCP **und** UDP – der TCP-Port ist der, den der Spieler
        eintippt. Früher lehnte clean_ports ihn ab; damit stand er nie im Datensatz."""
        self.assertEqual(instances.clean_ports({"java": 25565, "bedrock": 19132}, "bedrock"),
                         {"java": 25565, "bedrock": 19132})
        with self.assertRaises(ValueError):
            instances.clean_ports({"ftp": 21}, "java")


# ----------------------------------------------------------------------- Startprüfung

class TestStartpruefung(Basis):

    def test_start_erlaubt(self):
        self.pass_lokal()
        entry = self.instanz("Welt-A", 4096)
        ergebnis = self.pruefe(entry)
        self.assertTrue(ergebnis.ok)
        self.assertTrue(ergebnis)
        self.assertEqual(ergebnis.code, "ok")
        self.assertEqual(ergebnis.reason, "")

    def test_ohne_pass(self):
        entry = self.instanz("Welt-A")
        ergebnis = self.pruefe(entry)
        self.assertFalse(ergebnis.ok)
        self.assertEqual(ergebnis.code, "kein_pass")
        self.assertIn("keinen gültigen Pass", ergebnis.reason)

    def test_pass_abgelaufen(self):
        self.pass_lokal(tage=1)
        entry = self.instanz("Welt-A")
        self.assertTrue(self.pruefe(entry, now=T0 + TAG - 1).ok)
        self.assertEqual(self.pruefe(entry, now=T0 + TAG).code, "kein_pass")

    def test_pass_widerrufen(self):
        pas = self.pass_lokal()
        entry = self.instanz("Welt-A")
        passes.revoke_pass(pas["id"], now=T0)
        self.assertEqual(self.pruefe(entry).code, "kein_pass")

    def test_anzahl_erreicht(self):
        self.pass_lokal(anzahl=2, ram=10240)
        self.laeuft("Welt-A", 2048)
        self.laeuft("Welt-B", 2048)
        drei = self.instanz("Welt-C", 2048)
        ergebnis = self.pruefe(drei)
        self.assertEqual(ergebnis.code, "anzahl")
        self.assertEqual(ergebnis.reason,
                         "Dein Pass erlaubt 2 gleichzeitig laufende Server – es laufen bereits "
                         "Welt-A und Welt-B.")

    def test_anzahl_eins_im_singular(self):
        self.pass_lokal(anzahl=1, ram=10240)
        self.laeuft("Welt-A", 2048)
        zwei = self.instanz("Welt-B", 2048)
        ergebnis = self.pruefe(zwei)
        self.assertEqual(ergebnis.reason,
                         "Dein Pass erlaubt 1 gleichzeitig laufenden Server – es läuft bereits Welt-A.")

    def test_drei_namen_werden_aufgezaehlt(self):
        self.pass_lokal(anzahl=3, ram=10240)
        self.laeuft("Welt-A", 1024)
        self.laeuft("Welt-B", 1024)
        self.laeuft("Welt-C", 1024)
        vier = self.instanz("Welt-D", 1024)
        self.assertIn("Welt-A, Welt-B und Welt-C", self.pruefe(vier).reason)

    def test_ram_summe_erreicht(self):
        self.pass_lokal(anzahl=4, ram=8192)
        self.laeuft("Welt-A", 6144)
        zwei = self.instanz("Welt-B", 4096)
        ergebnis = self.pruefe(zwei)
        self.assertEqual(ergebnis.code, "ram")
        self.assertEqual(ergebnis.reason,
                         "Dein Pass erlaubt zusammen 8 GB; es laufen schon 6 GB (Welt-A), "
                         "dieser Server braucht 4 GB.")

    def test_ram_genau_auf_der_grenze_geht(self):
        self.pass_lokal(anzahl=4, ram=8192)
        self.laeuft("Welt-A", 4096)
        zwei = self.instanz("Welt-B", 4096)
        self.assertTrue(self.pruefe(zwei).ok)
        drei = self.instanz("Welt-C", 1024)
        instances.set_running(zwei["id"], True, now=T0)
        self.assertEqual(self.pruefe(drei).code, "ram")

    def test_ram_des_servers_zu_gross_fuer_den_pass(self):
        self.pass_lokal(anzahl=2, ram=4096)
        entry = self.instanz("Welt-A", 6144)
        ergebnis = self.pruefe(entry)
        self.assertEqual(ergebnis.code, "ram_zu_gross")
        self.assertIn("erlaubt zusammen 4 GB", ergebnis.reason)
        self.assertIn("mit 6 GB eingerichtet", ergebnis.reason)

    def test_ram_passt_aber_anzahl_nicht(self):
        self.pass_lokal(anzahl=1, ram=10240)
        self.laeuft("Welt-A", 1024)
        zwei = self.instanz("Welt-B", 1024)
        self.assertEqual(self.pruefe(zwei).code, "anzahl")

    def test_anzahl_passt_aber_ram_nicht(self):
        self.pass_lokal(anzahl=4, ram=4096)
        self.laeuft("Welt-A", 3072)
        zwei = self.instanz("Welt-B", 2048)
        self.assertEqual(self.pruefe(zwei).code, "ram")

    def test_laeuft_schon(self):
        self.pass_lokal()
        entry = self.laeuft("Welt-A", 2048)
        ergebnis = self.pruefe(entry)
        self.assertEqual(ergebnis.code, "laeuft_schon")
        self.assertIn("läuft schon", ergebnis.reason)

    def test_konto_gesperrt(self):
        self.pass_lokal()
        entry = self.instanz("Welt-A")
        users.set_blocked(self.konto["id"], True, "Zahlung offen")
        ergebnis = self.pruefe(entry)
        self.assertEqual(ergebnis.code, "konto_gesperrt")
        self.assertIn("gesperrt", ergebnis.reason)
        self.assertIn("Zahlung offen", ergebnis.reason)

    def test_platte_zu_voll(self):
        self.pass_lokal()
        entry = self.instanz("Welt-A")
        ergebnis = self.pruefe(entry, free_bytes=2 * 1024 ** 3)
        self.assertEqual(ergebnis.code, "platte")
        self.assertIn("nur noch 2 GB frei", ergebnis.reason)
        self.assertIn("unter 3 GB", ergebnis.reason)
        # Genau auf der Grenze ist noch erlaubt.
        self.assertTrue(self.pruefe(entry, free_bytes=instances.MIN_FREE_DISK_MB * 1024 * 1024).ok)

    def test_zustaende_verhindern_den_start(self):
        self.pass_lokal()
        entry = instances.create_instance(self.konto["id"], "Welt-A", now=T0)
        self.assertEqual(self.pruefe(entry).code, "zustand")
        self.assertIn("nur auf deinem PC", self.pruefe(entry).reason)

        instances.set_state(entry["id"], "uploading", now=T0)
        self.assertIn("wird gerade hochgeladen", self.pruefe(entry).reason)

        instances.set_state(entry["id"], "hosted", now=T0)
        self.assertTrue(self.pruefe(entry).ok)

        instances.set_state(entry["id"], "awaiting_pull", now=T0)
        self.assertIn("zurückgeholt zu werden", self.pruefe(entry).reason)

        instances.set_state(entry["id"], "downloading", now=T0)
        self.assertIn("wird gerade auf deinen PC zurückgeholt", self.pruefe(entry).reason)

    def test_premium_instanz_ohne_premium_pass(self):
        premium = self.pass_premium()
        entry = instances.create_instance(self.konto["id"], "Root-Welt", origin="premium", now=T0)
        self.assertTrue(self.pruefe(entry).ok)
        passes.revoke_pass(premium["id"], now=T0)
        self.pass_lokal()
        ergebnis = self.pruefe(entry)
        self.assertEqual(ergebnis.code, "kein_premium_pass")
        self.assertIn("reiner Root-Server", ergebnis.reason)

    def test_premium_pass_deckt_auch_lokale_instanz(self):
        self.pass_premium(anzahl=1, ram=4096)
        entry = self.instanz("Welt-A", 4096)
        self.assertTrue(self.pruefe(entry).ok)

    def test_mehrere_paesse_es_gilt_das_maximum(self):
        self.pass_lokal(anzahl=1, ram=4096)
        self.pass_lokal(anzahl=2, ram=6144)
        self.laeuft("Welt-A", 3072)
        zwei = self.instanz("Welt-B", 3072)
        self.assertTrue(self.pruefe(zwei).ok)
        instances.set_running(zwei["id"], True, now=T0)
        drei = self.instanz("Welt-C", 1024)
        # Anzahl (2) ist erreicht – die Summe der Pässe (3 Server) gilt ausdrücklich nicht.
        self.assertEqual(self.pruefe(drei).code, "anzahl")

    def test_unbekannte_instanz(self):
        with self.assertRaises(ValueError) as fehler:
            instances.check_start("gibtsnicht", now=T0, free_bytes=VIEL_PLATZ)
        self.assertIn("keinen Server", str(fehler.exception))

    def test_geloeschtes_konto(self):
        self.pass_lokal()
        entry = self.instanz("Welt-A")
        store_hosted.remove_by("users", "id", self.konto["id"])
        self.assertEqual(self.pruefe(entry).code, "kein_konto")


# ----------------------------------------------------------------------- Reservieren (Wettlauf)

class TestReservieren(Basis):
    """`reserve_start` prüft und trägt „läuft“ in einem Zug ein.

    Ohne das hier ließen sich die Pass-Grenzen durch zwei gleichzeitige Startaufrufe umgehen:
    beide sehen noch „nichts läuft“, beide bekommen ein „ja“, beide starten.
    """

    def test_zwei_gleichzeitige_starts_nur_einer_kommt_durch(self):
        import threading
        self.pass_lokal(anzahl=1, ram=4096)
        eins = self.instanz("Welt-A", ram=4096)
        zwei = self.instanz("Welt-B", ram=4096)
        ergebnis: list = []
        sperre = threading.Lock()

        def start(iid: str) -> None:
            check = instances.reserve_start(iid, now=T0, free_bytes=VIEL_PLATZ)
            time.sleep(0.05)               # hier liegen sonst Portvergabe und Prozessstart
            with sperre:
                ergebnis.append(check.ok)

        faeden = [threading.Thread(target=start, args=(e["id"],)) for e in (eins, zwei)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(20)
        self.assertEqual(sorted(ergebnis), [False, True], "Es darf genau einer durchkommen.")
        laufen = instances.running_of(self.konto["id"])
        self.assertEqual(len(laufen), 1)
        self.assertEqual(sum(int(i["ram_mb"]) for i in laufen), 4096)

    def test_ram_summe_haelt_bei_gleichzeitigen_starts(self):
        import threading
        self.pass_lokal(anzahl=4, ram=6144)
        drei = [self.instanz(f"Welt-{i}", ram=4096) for i in range(3)]
        ok: list = []
        sperre = threading.Lock()

        def start(iid: str) -> None:
            check = instances.reserve_start(iid, now=T0, free_bytes=VIEL_PLATZ)
            with sperre:
                ok.append(check.ok)

        faeden = [threading.Thread(target=start, args=(e["id"],)) for e in drei]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(20)
        self.assertEqual(ok.count(True), 1, "6 GB Budget reichen nur für einen 4-GB-Server.")
        self.assertLessEqual(instances.ram_in_use_mb(self.konto["id"]), 6144)

    def test_reservierung_zurueckgeben(self):
        self.pass_lokal(anzahl=1)
        entry = self.instanz("Welt-A")
        self.assertTrue(instances.reserve_start(entry["id"], now=T0, free_bytes=VIEL_PLATZ).ok)
        instances.release_start(entry["id"], now=T0)
        self.assertEqual(instances.running_of(self.konto["id"]), [])
        self.assertTrue(instances.reserve_start(entry["id"], now=T0, free_bytes=VIEL_PLATZ).ok)

    def test_update_schreibt_nur_bei_aenderung(self):
        """Ein Aufruf, der nichts ändert, darf die Datei nicht neu schreiben.

        Sonst legt ein unangemeldetes ``/api/auth/logout`` in einer Schleife dauerhaft
        Schreiblast auf dieselbe Platte, auf der die Welten liegen.
        """
        pfad = store_hosted.path_of("users")
        vorher = pfad.stat().st_mtime_ns, pfad.read_bytes()
        store_hosted.update("users", lambda records: False)
        self.assertEqual((pfad.stat().st_mtime_ns, pfad.read_bytes()), vorher)
        store_hosted.update("users", lambda records: records.append({"id": "x"}))
        self.assertNotEqual(pfad.read_bytes(), vorher[1])


# ----------------------------------------------------------------------- Zustände und Pflege

class TestZustaende(Basis):

    def test_unerlaubter_uebergang(self):
        entry = self.instanz("Welt-A", hosted=False)
        with self.assertRaises(ValueError) as fehler:
            instances.set_state(entry["id"], "hosted", now=T0)
        self.assertIn("nicht vorgesehen", str(fehler.exception))
        with self.assertRaises(ValueError):
            instances.set_state(entry["id"], "quatsch", now=T0)

    def test_laufender_server_wechselt_nicht(self):
        self.pass_lokal()
        entry = self.laeuft("Welt-A")
        with self.assertRaises(ValueError) as fehler:
            instances.set_state(entry["id"], "awaiting_pull", now=T0)
        self.assertIn("läuft noch", str(fehler.exception))

    def test_einschalten_nur_im_zustand_hosted(self):
        entry = self.instanz("Welt-A", hosted=False)
        with self.assertRaises(ValueError):
            instances.set_running(entry["id"], True, now=T0)

    def test_ram_aendern_nur_im_ausgeschalteten_zustand(self):
        self.pass_lokal()
        entry = self.laeuft("Welt-A", 2048)
        with self.assertRaises(ValueError) as fehler:
            instances.set_ram(entry["id"], 4096)
        self.assertIn("nur ändern, wenn der Server aus ist", str(fehler.exception))
        instances.set_running(entry["id"], False, now=T0)
        self.assertEqual(instances.set_ram(entry["id"], 4096)["ram_mb"], 4096)

    def test_umbenennen(self):
        self.instanz("Welt-A", hosted=False)
        zwei = self.instanz("Welt-B", hosted=False)
        with self.assertRaises(ValueError):
            instances.rename_instance(zwei["id"], "Welt-A")
        self.assertEqual(instances.rename_instance(zwei["id"], "Welt-C")["name"], "Welt-C")

    def test_groesse_fortschreiben(self):
        entry = self.instanz("Welt-A", hosted=False)
        instances.set_size(entry["id"], 3 * 1024 ** 3)
        self.assertEqual(instances.total_size_bytes(self.konto["id"]), 3 * 1024 ** 3)
        self.assertEqual(instances.size_text(3 * 1024 ** 3), "3 GB")

    def test_loeschen_erst_nach_stoppen(self):
        self.pass_lokal()
        entry = self.laeuft("Welt-A")
        with self.assertRaises(ValueError):
            instances.delete_instance(entry["id"])
        instances.set_running(entry["id"], False, now=T0)
        instances.delete_instance(entry["id"])
        self.assertIsNone(instances.get_instance(entry["id"]))

    def test_rueckholung_und_freigabe(self):
        self.pass_lokal()
        entry = self.instanz("Welt-A")
        instances.set_state(entry["id"], "awaiting_pull", now=T0)
        instances.set_state(entry["id"], "downloading", now=T0)
        frei = instances.release_instance(entry["id"], now=T0 + 60)
        self.assertEqual(frei["state"], "local_only")
        self.assertEqual(frei["size_bytes"], 0)
        self.assertEqual(frei["parked_at"], 0)

    def test_freigabe_nur_nach_rueckholung(self):
        self.pass_lokal()
        entry = self.instanz("Welt-A")
        with self.assertRaises(ValueError) as fehler:
            instances.release_instance(entry["id"], now=T0)
        self.assertIn("zurückgeholt", str(fehler.exception))


# ----------------------------------------------------------------------- Ablauf des Passes

class TestAblauf(Basis):

    def test_lokale_instanz_wartet_auf_rueckholung(self):
        pas = self.pass_lokal()
        entry = self.laeuft("Welt-A", 2048)
        passes.revoke_pass(pas["id"], now=T0)
        self.assertEqual([i["id"] for i in instances.uncovered(self.konto["id"], now=T0)],
                         [entry["id"]])
        geparkt = instances.park_uncovered(self.konto["id"], now=T0 + 600)
        self.assertEqual(geparkt[0]["state"], "awaiting_pull")
        self.assertFalse(geparkt[0]["running"])
        self.assertEqual(geparkt[0]["parked_at"], T0 + 600)
        self.assertIn("zurückgeholt zu werden", self.pruefe(entry).reason)

    def test_premium_instanz_ruht(self):
        premium = self.pass_premium()
        entry = instances.create_instance(self.konto["id"], "Root-Welt", origin="premium", now=T0)
        instances.set_running(entry["id"], True, now=T0)
        passes.revoke_pass(premium["id"], now=T0)
        geparkt = instances.park_uncovered(self.konto["id"], now=T0 + 60)
        self.assertEqual(geparkt[0]["state"], "suspended")
        self.assertIn("ruht", self.pruefe(entry).reason)

    def test_zielzustand_nach_ablauf(self):
        self.assertEqual(instances.target_state_after_expiry({"origin": "local"}), "awaiting_pull")
        self.assertEqual(instances.target_state_after_expiry({"origin": "premium"}), "suspended")

    def test_lokaler_pass_deckt_premium_instanz_nicht_mehr(self):
        premium = self.pass_premium()
        lokal = self.instanz("Welt-A", 2048)
        root = instances.create_instance(self.konto["id"], "Root-Welt", origin="premium", now=T0)
        passes.revoke_pass(premium["id"], now=T0)
        self.pass_lokal()
        offen = [i["id"] for i in instances.uncovered(self.konto["id"], now=T0)]
        self.assertEqual(offen, [root["id"]])
        self.assertTrue(self.pruefe(lokal).ok)

    def test_ruhende_premium_instanzen_duerfen_spaeter_weg(self):
        premium = self.pass_premium()
        entry = instances.create_instance(self.konto["id"], "Root-Welt", origin="premium", now=T0)
        passes.revoke_pass(premium["id"], now=T0)
        instances.park_uncovered(self.konto["id"], now=T0)
        self.assertEqual(instances.premium_delete_candidates(now=T0 + 13 * TAG), [])
        kandidaten = instances.premium_delete_candidates(now=T0 + 14 * TAG)
        self.assertEqual([i["id"] for i in kandidaten], [entry["id"]])
        # Mit neuem Premium-Pass bleibt sie liegen.
        self.pass_premium(now=T0 + 14 * TAG)
        self.assertEqual(instances.premium_delete_candidates(now=T0 + 14 * TAG), [])

    def test_zu_viel_laeuft_nach_widerruf(self):
        gross = self.pass_lokal(anzahl=3, ram=8192)
        eins = self.laeuft("Welt-A", 2048, now=T0)
        zwei = self.laeuft("Welt-B", 2048, now=T0 + 10)
        drei = self.laeuft("Welt-C", 2048, now=T0 + 20)
        self.assertEqual(instances.excess_running(self.konto["id"], now=T0 + 30), [])
        passes.revoke_pass(gross["id"], now=T0 + 30)
        self.pass_lokal(anzahl=1, ram=4096, now=T0 + 30)
        zuviel = [i["id"] for i in instances.excess_running(self.konto["id"], now=T0 + 40)]
        self.assertEqual(zuviel, [drei["id"], zwei["id"]])
        self.assertNotIn(eins["id"], zuviel)

    def test_zu_viel_laeuft_wegen_ram(self):
        gross = self.pass_lokal(anzahl=4, ram=8192)
        self.laeuft("Welt-A", 4096, now=T0)
        zwei = self.laeuft("Welt-B", 4096, now=T0 + 10)
        passes.revoke_pass(gross["id"], now=T0 + 20)
        self.pass_lokal(anzahl=4, ram=4096, now=T0 + 20)
        zuviel = [i["id"] for i in instances.excess_running(self.konto["id"], now=T0 + 30)]
        self.assertEqual(zuviel, [zwei["id"]])


# ----------------------------------------------------------------------- Übersicht und Texte

class TestUebersicht(Basis):

    def test_uebersicht_fuer_die_api(self):
        self.pass_lokal(anzahl=2, ram=8192)
        self.laeuft("Welt-A", 6144)
        self.instanz("Welt-B", 2048)
        blick = instances.overview(self.konto["id"], now=T0, free_bytes=VIEL_PLATZ)
        self.assertEqual(blick["slots_used"], 1)
        self.assertEqual(blick["slots_free"], 1)
        self.assertEqual(blick["ram_used_mb"], 6144)
        self.assertEqual(blick["ram_free_mb"], 2048)
        self.assertEqual(blick["instances"], 2)
        self.assertEqual(blick["running_names"], ["Welt-A"])
        self.assertTrue(blick["disk_ok"])
        self.assertEqual(blick["limits_text"], "2 gleichzeitig laufende Server, zusammen 8 GB")

    def test_uebersicht_ohne_pass(self):
        self.instanz("Welt-A", hosted=False)
        blick = instances.overview(self.konto["id"], now=T0, free_bytes=VIEL_PLATZ)
        self.assertEqual(blick["slots_free"], 0)
        self.assertEqual(blick["ram_total_mb"], 0)
        self.assertEqual(blick["limits_text"], "kein gültiger Pass")

    def test_uebersicht_meldet_volle_platte(self):
        self.pass_lokal()
        blick = instances.overview(self.konto["id"], now=T0, free_bytes=1024 ** 3)
        self.assertFalse(blick["disk_ok"])

    def test_namen_aufzaehlen(self):
        self.assertEqual(instances.name_list([]), "keiner")
        self.assertEqual(instances.name_list(["A"]), "A")
        self.assertEqual(instances.name_list(["A", "B"]), "A und B")
        self.assertEqual(instances.name_list(["A", "B", "C"]), "A, B und C")

    def test_instanz_fuer_die_api(self):
        self.pass_lokal()
        entry = self.laeuft("Welt-A", 4096)
        offen = instances.public_instance(entry)
        self.assertEqual(offen["ram_text"], "4 GB")
        self.assertEqual(offen["state_text"], "auf dem Root-Server")
        self.assertTrue(offen["running"])

    def test_zustandstexte(self):
        for zustand in instances.STATES:
            self.assertTrue(instances.state_text(zustand))


class ZwischenspeicherTest(Basis):
    """Der Zwischenspeicher in store_hosted spart die Dateisperre – darf aber nie alt liefern."""

    def test_aenderung_wird_bemerkt(self):
        users.create_user("Zweiter", now=T0)
        self.assertEqual(len(store_hosted.load("users")), 2)
        users.create_user("Dritter", now=T0)
        self.assertEqual(len(store_hosted.load("users")), 3)

    def test_jeder_aufruf_bekommt_eigene_objekte(self):
        """Zwei Aufrufe dürfen sich keine Listen teilen – sonst verändert ein Aufrufer den
        Zwischenspeicher aller anderen."""
        einladung = users.create_invite(self.konto["id"], uses_max=2, now=T0)
        eins = store_hosted.get_by("invites", "code", einladung["code"])
        eins["redeemed_by"].append({"user_id": "x", "at": T0})
        zwei = store_hosted.get_by("invites", "code", einladung["code"])
        self.assertEqual(zwei["redeemed_by"], [])

    def test_datei_von_hand_ausgetauscht(self):
        """Ein Pflegeskript des Betreibers schreibt die Datei – der Fingerabdruck merkt das."""
        import json
        self.assertEqual(len(store_hosted.load("users")), 1)
        store_hosted.path_of("users").write_text(json.dumps({"users": []}), encoding="utf-8")
        self.assertEqual(store_hosted.load("users"), [])

    def test_zwischenspeicher_laesst_sich_leeren(self):
        self.assertEqual(len(store_hosted.load("users")), 1)
        store_hosted.cache_leeren()
        self.assertEqual(len(store_hosted.load("users")), 1)

    def test_geloeschte_datei_gibt_leere_liste(self):
        store_hosted.path_of("users").unlink()
        self.assertEqual(store_hosted.load("users"), [])


if __name__ == "__main__":
    unittest.main()
