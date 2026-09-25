"""Selbsttests für den Ausbau: Unterdomänen, Verteiler-Tabelle, Begleit-Plugin, Ratenbremse.

Geprüft wird ohne Netz, ohne Minecraft-Server und ohne echte Discord-Zugangsdaten.

    python -m unittest discover -s hosted/tests
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
import zipfile

_HOSTED = pathlib.Path(__file__).resolve().parent.parent
if str(_HOSTED) not in sys.path:
    sys.path.insert(0, str(_HOSTED))

import mcsmd                                                        # noqa: E402

instances = mcsmd.instances
routen = mcsmd.routen
companion = mcsmd.companion
store_hosted = mcsmd.store_hosted
users = mcsmd.users
passes = mcsmd.passes


class Basis(unittest.TestCase):
    """Eigener Datenordner je Test – es wird nichts Echtes angefasst."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mcsm-ausbau-")
        root = pathlib.Path(self.tmp)
        self.env_backup = {k: os.environ.get(k) for k in
                           ("MCSM_DATA", "MCSM_DATA_ROOT", "MCSM_DOMAIN", "MCSM_ROUTES",
                            "MCSM_COMPANION_JAR")}
        os.environ["MCSM_DATA_ROOT"] = str(root)
        os.environ["MCSM_DATA"] = str(root / "data")
        os.environ["MCSM_ROUTES"] = str(root / "data" / "routes.json")
        os.environ["MCSM_DOMAIN"] = "arcardia-nexus.de"
        store_hosted.ensure_dir()
        self.user = users.create_user("Luca")

    def tearDown(self) -> None:
        for key, value in self.env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def instanz(self, name: str, *, art: str = "java", hosted: bool = True) -> dict:
        entry = instances.create_instance(self.user["id"], name, server_type=art,
                                          flavor="paper" if art == "java" else "paper")
        if hosted:
            entry = instances.set_state(entry["id"], "uploading")
            entry = instances.set_state(entry["id"], "hosted")
        return entry


# ----------------------------------------------------------------------- Marken

class TestMarken(Basis):

    def test_umlaute_und_sonderzeichen(self):
        self.assertEqual(routen.marke_aus_name("Lucas Welt Nr. 2"), "lucas-welt-nr-2")
        self.assertEqual(routen.marke_aus_name("Grüße aus Köln"), "gruesse-aus-koeln")
        self.assertEqual(routen.marke_aus_name("Straße"), "strasse"),
        self.assertEqual(routen.marke_aus_name("Über_Alles!!!"), "ueber-alles")
        self.assertEqual(routen.marke_aus_name("---"), "")
        self.assertEqual(routen.marke_aus_name("Café Amélie"), "cafe-amelie")

    def test_marke_ist_gueltige_dns_marke(self):
        marke = routen.marke_aus_name("  ...Test---Welt...  ")
        self.assertEqual(marke, "test-welt")
        self.assertEqual(instances.clean_marke(marke), marke)

    def test_reservierte_marken_bekommt_niemand(self):
        for name in ("admin", "API", "www", "play", "Panel"):
            entry = self.instanz(name)
            marke = routen.ensure_marke(entry)
            with self.subTest(name=name):
                self.assertNotIn(marke, routen.RESERVIERT)
                self.assertTrue(marke)

    def test_marke_ist_ueber_alle_konten_eindeutig(self):
        zweiter = users.create_user("Anna")
        eins = self.instanz("Meine Welt")
        zwei = instances.create_instance(zweiter["id"], "Meine Welt")
        m1 = routen.ensure_marke(eins)
        m2 = routen.ensure_marke(zwei)
        self.assertEqual(m1, "meine-welt")
        self.assertNotEqual(m1, m2)
        self.assertTrue(m2.startswith("meine-welt-"))

    def test_marke_bleibt_beim_umbenennen(self):
        entry = self.instanz("Alte Welt")
        marke = routen.ensure_marke(entry)
        instances.rename_instance(entry["id"], "Ganz neuer Name")
        frisch = instances.get_instance(entry["id"])
        self.assertEqual(routen.ensure_marke(frisch), marke)
        self.assertEqual(frisch["marke"], marke)

    def test_marke_aus_der_kennung_wenn_der_name_nichts_hergibt(self):
        """Bleibt vom Namen nichts übrig, muss die Instanzkennung herhalten – eine Instanz ohne
        Adresse wäre für den Verteiler unsichtbar."""
        self.assertEqual(routen.marke_fuer({"id": "a1b2c3d4e5f6", "name": "___"}),
                         "a1b2c3d4e5f6")
        self.assertEqual(routen.marke_fuer({"id": "a1b2c3d4e5f6", "name": "admin"},
                                           belegt={}), "admin-a1b2c3")

    def test_kaputte_marke_im_datensatz_wird_ersetzt(self):
        entry = self.instanz("Welt A")
        store_hosted.patch_by("instances", "id", entry["id"], {"marke": "NICHT Erlaubt!"})
        marke = routen.ensure_marke(instances.get_instance(entry["id"]))
        self.assertEqual(marke, "welt-a")


# ----------------------------------------------------------------------- Adressen

class TestAdressen(Basis):

    def test_java_adresse_ohne_port(self):
        entry = self.instanz("Mein Server")
        routen.ensure_marke(entry)
        entry = instances.get_instance(entry["id"])
        self.assertEqual(routen.subdomain(entry), "mein-server.arcardia-nexus.de")
        self.assertEqual(routen.adresse(entry, ports_live={"port": 25570}),
                         "mein-server.arcardia-nexus.de")

    def test_bedrock_adresse_mit_port(self):
        entry = self.instanz("Handy Welt", art="bedrock")
        routen.ensure_marke(entry)
        entry = instances.get_instance(entry["id"])
        self.assertEqual(routen.adresse(entry, ports_live={"port": 25571, "bedrock_port": 19132}),
                         "arcardia-nexus.de:25571")

    def test_bedrock_ohne_port_nennt_nur_die_domain(self):
        entry = self.instanz("Handy Welt", art="bedrock")
        self.assertEqual(routen.adresse(instances.get_instance(entry["id"])),
                         "arcardia-nexus.de")

    def test_domain_kommt_aus_der_umgebung(self):
        os.environ["MCSM_DOMAIN"] = "Beispiel.DE."
        self.assertEqual(routen.domain(), "beispiel.de")


# ----------------------------------------------------------------------- Zuordnungstabelle

class TestTabelle(Basis):

    def test_nur_gehostete_java_instanzen(self):
        java = self.instanz("Java Welt")
        bedrock = self.instanz("Bedrock Welt", art="bedrock")
        lokal = self.instanz("Nur lokal", hosted=False)
        for entry in (java, bedrock, lokal):
            routen.ensure_marke(entry)
        instances.set_ports(java["id"], {"java": 25570})
        instances.set_ports(bedrock["id"], {"java": 25571, "bedrock": 19132})
        tabelle = routen.eintraege(routen.tabelle())
        self.assertEqual(sorted(tabelle), ["java-welt"])
        self.assertEqual(tabelle["java-welt"], {"port": 25570, "instanz": java["id"]})

    def test_gestoppte_bleiben_in_der_tabelle(self):
        """Der Verteiler meldet dann „läuft gerade nicht“ – das ist die bessere Auskunft."""
        entry = self.instanz("Welt A")
        routen.ensure_marke(entry)
        instances.set_ports(entry["id"], {"java": 25570})
        self.assertIn("welt-a", routen.eintraege(routen.tabelle()))

    def test_port_des_verteilers_kommt_nie_in_die_tabelle(self):
        entry = self.instanz("Welt A")
        routen.ensure_marke(entry)
        instances.set_ports(entry["id"], {"java": routen.ROUTER_PORT})
        self.assertEqual(routen.eintraege(routen.tabelle()), {})

    def test_live_ports_schlagen_den_datensatz(self):
        entry = self.instanz("Welt A")
        routen.ensure_marke(entry)
        instances.set_ports(entry["id"], {"java": 25570})
        tabelle = routen.eintraege(routen.tabelle(ports_live=lambda iid: {"port": 25599}))
        self.assertEqual(tabelle["welt-a"]["port"], 25599)

    def test_schreiben_ist_atomar_und_nur_fuer_den_dienst_lesbar(self):
        entry = self.instanz("Welt A")
        routen.ensure_marke(entry)
        instances.set_ports(entry["id"], {"java": 25570})
        routen.schreibe_routen()
        pfad = routen.routes_path()
        self.assertTrue(pfad.is_file())
        data = json.loads(pfad.read_text(encoding="utf-8"))
        self.assertEqual(data["welt-a"]["port"], 25570)
        self.assertIn("_bemerkung", data)
        self.assertFalse(list(pfad.parent.glob("*.tmp")), "Zwischendatei blieb liegen")
        if hasattr(os, "geteuid"):                          # Rechte gibt es nur unter Linux
            self.assertEqual(pfad.stat().st_mode & 0o777, 0o600)

    def test_tabelle_ist_fuer_den_verteiler_lesbar(self):
        """Gegenprobe mit dem echten Verteiler: er muss den Eintrag finden."""
        entry = self.instanz("Welt A")
        routen.ensure_marke(entry)
        instances.set_ports(entry["id"], {"java": 25570})
        routen.schreibe_routen()
        import router                                       # noqa: PLC0415 - nur hier gebraucht
        tabelle = router.Routen(str(routen.routes_path()), domain="arcardia-nexus.de")
        treffer = tabelle.finde("welt-a.arcardia-nexus.de")
        self.assertIsNotNone(treffer, "Der Verteiler findet den geschriebenen Eintrag nicht")
        self.assertEqual(treffer.port, 25570)
        self.assertEqual(treffer.instanz, entry["id"])
        self.assertIsNone(tabelle.finde("gibt-es-nicht.arcardia-nexus.de"))


# ----------------------------------------------------------------------- Begleit-Plugin

class TestBegleitPlugin(Basis):

    def setUp(self) -> None:
        super().setUp()
        self.jar = pathlib.Path(self.tmp) / "MCSMCompanion.jar"
        with zipfile.ZipFile(self.jar, "w") as zf:
            zf.writestr("config.yml", "# Vorlage\nserver_name: \"alt\"\neigenes: 5\n")
            zf.writestr("plugin.yml", "name: MCSMCompanion\n")
        os.environ["MCSM_COMPANION_JAR"] = str(self.jar)
        self.folder = pathlib.Path(self.tmp) / "instanz"
        self.folder.mkdir()

    def test_jar_wird_bereitgelegt(self):
        entry = self.instanz("Welt A")
        self.assertTrue(companion.prepare(self.folder, entry, version="1.0.0"))
        self.assertTrue((self.folder / "plugins" / "MCSMCompanion.jar").is_file())
        self.assertFalse(list((self.folder / "plugins").glob("*.neu")))

    def test_jar_wird_nach_dem_loeschen_wieder_hingelegt(self):
        entry = self.instanz("Welt A")
        companion.prepare(self.folder, entry)
        (self.folder / "plugins" / "MCSMCompanion.jar").unlink()
        companion.prepare(self.folder, entry)
        self.assertTrue(companion.installed(self.folder))

    def test_eigene_einstellungen_bleiben(self):
        entry = self.instanz("Welt A")
        companion.prepare(self.folder, entry, version="1.0.0")
        pfad = self.folder / "plugins" / "MCSMCompanion" / "config.yml"
        pfad.write_text(pfad.read_text(encoding="utf-8") + "meins: 42\n", encoding="utf-8")
        companion.prepare(self.folder, entry, version="1.0.0")
        text = pfad.read_text(encoding="utf-8")
        self.assertIn("meins: 42", text)
        self.assertIn('server_name: "Welt A"', text)
        self.assertIn('mode: "hosted"', text)
        self.assertIn("eigenes: 5", text)

    def test_nur_paper_bekommt_das_plugin(self):
        bedrock = self.instanz("Handy Welt", art="bedrock")
        self.assertFalse(companion.applies(bedrock))
        self.assertFalse(companion.prepare(self.folder, bedrock))
        self.assertFalse(companion.installed(self.folder))

    def test_fehlende_jar_bricht_nichts_ab(self):
        os.environ["MCSM_COMPANION_JAR"] = str(pathlib.Path(self.tmp) / "gibt-es-nicht.jar")
        entry = self.instanz("Welt A")
        self.assertFalse(companion.available())
        self.assertFalse(companion.prepare(self.folder, entry))

    def test_kennzahlen_werden_gelesen(self):
        entry = self.instanz("Welt A")
        companion.prepare(self.folder, entry)
        ordner = self.folder / "plugins" / "MCSMCompanion"
        ordner.mkdir(parents=True, exist_ok=True)
        (ordner / "status.json").write_text(json.dumps({"updated": 1, "players": 3}),
                                            encoding="utf-8")
        info = companion.status(self.folder, entry)
        self.assertTrue(info["installed"])
        self.assertEqual(info["status"]["players"], 3)

    def test_mitgelieferte_jar_ist_im_projekt(self):
        """Ohne die Jar im Ordner assets/ rollt deploy.sh kein Begleit-Plugin aus."""
        os.environ.pop("MCSM_COMPANION_JAR", None)
        self.assertTrue(companion.jar_source().is_file(),
                        f"{companion.jar_source()} fehlt – siehe hosted/assets/HINWEIS.txt")


# ----------------------------------------------------------------------- Ratenbremse

class TestBremse(unittest.TestCase):

    def setUp(self) -> None:
        self.bremse = mcsmd.Bremse()

    def test_unter_der_grenze_geht_alles_durch(self):
        grenze = mcsmd.RATE_LIMITS["anmeldung"][0]
        for nummer in range(grenze):
            wartezeit, _ = self.bremse.pruefen("1.2.3.4", "anmeldung")
            self.assertEqual(wartezeit, 0, f"Versuch {nummer + 1} wurde zu früh gebremst")

    def test_ueber_der_grenze_gibt_es_eine_sperre(self):
        grenze, _fenster, sperre = mcsmd.RATE_LIMITS["anmeldung"]
        for _ in range(grenze):
            self.bremse.pruefen("1.2.3.4", "anmeldung")
        wartezeit, neu = self.bremse.pruefen("1.2.3.4", "anmeldung")
        self.assertEqual(wartezeit, sperre)
        self.assertTrue(neu)
        wartezeit, neu = self.bremse.pruefen("1.2.3.4", "anmeldung")
        self.assertGreater(wartezeit, 0)
        self.assertFalse(neu, "Die Sperre darf nur einmal gemeldet werden")

    def test_andere_adressen_sind_nicht_betroffen(self):
        for _ in range(mcsmd.RATE_LIMITS["anmeldung"][0] + 3):
            self.bremse.pruefen("1.2.3.4", "anmeldung")
        self.assertEqual(self.bremse.pruefen("5.6.7.8", "anmeldung")[0], 0)

    def test_gruppen_zaehlen_getrennt(self):
        for _ in range(mcsmd.RATE_LIMITS["anmeldung"][0] + 3):
            self.bremse.pruefen("1.2.3.4", "anmeldung")
        self.assertEqual(self.bremse.pruefen("1.2.3.4", "angemeldet")[0], 0)

    def test_das_fenster_laeuft_weiter(self):
        grenze, fenster, _ = mcsmd.RATE_LIMITS["anmeldung"]
        start = 1_000_000.0
        for nummer in range(grenze):
            self.bremse.pruefen("1.2.3.4", "anmeldung", now=start + nummer * 0.1)
        # Nach dem Fenster zählt von vorn.
        self.assertEqual(self.bremse.pruefen("1.2.3.4", "anmeldung",
                                             now=start + fenster + 1)[0], 0)

    def test_nach_erfolgreicher_anmeldung_wird_freigegeben(self):
        for _ in range(mcsmd.RATE_LIMITS["anmeldung"][0]):
            self.bremse.pruefen("1.2.3.4", "anmeldung")
        self.bremse.freigeben("1.2.3.4", "anmeldung")
        self.assertEqual(self.bremse.pruefen("1.2.3.4", "anmeldung")[0], 0)

    def test_anmeldung_ist_strenger_als_angemeldete_aufrufe(self):
        self.assertLess(mcsmd.RATE_LIMITS["anmeldung"][0],
                        mcsmd.RATE_LIMITS["angemeldet"][0])

    def test_abholen_reicht_fuer_zehn_minuten_fragen(self):
        """Das Programm fragt alle 2 Sekunden – 10 Minuten lang. Das darf nicht bremsen."""
        grenze, fenster, _ = mcsmd.RATE_LIMITS["abholen"]
        self.assertGreaterEqual(grenze, fenster / 2)


class TestHerkunft(unittest.TestCase):
    """Die Herkunftsadresse hinter nginx."""

    class FalscherHandler:
        def __init__(self, peer, headers):
            self.client_address = (peer, 1234)
            self.headers = headers

    def handler(self, peer, **headers):
        return self.FalscherHandler(peer, headers)

    def test_direkte_verbindung(self):
        self.assertEqual(mcsmd.client_ip(self.handler("203.0.113.9")), "203.0.113.9")

    def test_fremde_kopfzeile_wird_nicht_geglaubt(self):
        """Nur die Rückschleife darf eine Herkunft behaupten – sonst sucht sich jeder eine aus."""
        handler = self.handler("203.0.113.9")
        handler.headers = {"X-Forwarded-For": "1.1.1.1"}
        self.assertEqual(mcsmd.client_ip(handler), "203.0.113.9")

    def test_nginx_gibt_die_adresse_weiter(self):
        handler = self.handler("127.0.0.1")
        handler.headers = {"X-Forwarded-For": "1.1.1.1, 198.51.100.7"}
        self.assertEqual(mcsmd.client_ip(handler), "198.51.100.7")

    def test_x_real_ip_als_rueckfall(self):
        handler = self.handler("127.0.0.1")
        handler.headers = {"X-Real-IP": "198.51.100.7"}
        self.assertEqual(mcsmd.client_ip(handler), "198.51.100.7")

    def test_ohne_kopfzeile_faellt_es_auf(self):
        handler = self.handler("127.0.0.1")
        handler.headers = {}
        self.assertEqual(mcsmd.client_ip(handler), "@proxy")


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
