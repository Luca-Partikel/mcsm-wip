"""Selbsttests für hosted/core/paths.py – Pfadprüfung und Freigabe-Liste."""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

# hosted/core unter einem eigenen Paketnamen laden. Im Projektordner gibt es schon ein Paket
# „core“ (das lokale Programm); ein schlichtes sys.path.insert würde das falsche erwischen.
_HOSTED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "hosted_core" not in sys.modules:
    _spec = importlib.machinery.ModuleSpec("hosted_core", None, is_package=True)
    _pkg = importlib.util.module_from_spec(_spec)
    _pkg.__path__ = [os.path.join(_HOSTED, "core")]
    sys.modules["hosted_core"] = _pkg

paths = importlib.import_module("hosted_core.paths")


def make_symlink(link: pathlib.Path, target: pathlib.Path) -> bool:
    """Legt eine Verknüpfung an; gibt False zurück, wenn das System das nicht zulässt."""
    try:
        os.symlink(str(target), str(link), target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


class TestNormalize(unittest.TestCase):
    def test_saubere_pfade(self):
        self.assertEqual(paths.normalize("plugins/EssentialsX.jar"), "plugins/EssentialsX.jar")
        self.assertEqual(paths.normalize("./plugins//config/"), "plugins/config")
        self.assertEqual(paths.normalize("plugins\\config\\x.yml"), "plugins/config/x.yml")
        self.assertEqual(paths.normalize(""), "")
        self.assertEqual(paths.normalize("."), "")
        self.assertEqual(paths.normalize(None), "")

    def test_ausbruch_abgewehrt(self):
        for bad in ("..", "../etc/passwd", "world/../../root/.ssh/id_rsa", "a/b/../../..",
                    "/etc/passwd", "/", "//etc/passwd"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.normalize(bad)

    def test_windows_und_steuerzeichen(self):
        for bad in ("C:/Windows", "world:stream", "datei\x00.txt", "datei\n.txt", "a/b|c",
                    "frage?.txt", "stern*.yml", "ende /datei.txt", "punkt.", "con.txt", "NUL",
                    "x" * 300, "a/" * 40 + "b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.normalize(bad)

    def test_leerzeichen_am_rand_werden_abgelehnt(self):
        """Ein `strip()` über den ganzen Pfad würde auf eine **andere** Datei zeigen.

        Unter Linux sind „level.dat“ und „level.dat “ zwei verschiedene Dateien. Würde der Rand
        stillschweigend abgeschnitten, käme die eine unter dem Namen der anderen ins Manifest –
        und beim Löschen des Ordners wäre sie weg. Deshalb: ablehnen statt umdeuten.
        """
        for bad in ("  plugins/x.jar  ", "plugins/x.jar ", " plugins/x.jar",
                    "world/level.dat ", "plugins /x.jar"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.normalize(bad)
        # Trennzeichen werden weiter vereinheitlicht.
        self.assertEqual(paths.normalize("plugins\\\\x.jar"), "plugins/x.jar")
        self.assertEqual(paths.normalize("plugins//x.jar/"), "plugins/x.jar")

    def test_zu_langer_pfad(self):
        with self.assertRaises(ValueError):
            paths.normalize("a/" * 600 + "b")


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcsm-paths-"))
        self.root = self.tmp / "instanz"
        (self.root / "plugins").mkdir(parents=True)
        (self.root / "plugins" / "x.jar").write_bytes(b"jar")
        (self.tmp / "geheim.txt").write_text("nicht anfassen", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_innerhalb(self):
        ziel = paths.resolve(self.root, "plugins/x.jar")
        self.assertTrue(ziel.is_file())
        self.assertEqual(paths.resolve(self.root, ""), pathlib.Path(os.path.realpath(self.root)))

    def test_noch_nicht_vorhanden(self):
        ziel = paths.resolve(self.root, "welt/region/r.0.0.mca")
        self.assertFalse(ziel.exists())
        self.assertTrue(paths.inside(self.root, ziel))

    def test_ausbruch(self):
        for bad in ("../geheim.txt", "plugins/../../geheim.txt", "/etc/passwd"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.resolve(self.root, bad)

    def test_symlink_nach_draussen(self):
        link = self.root / "raus"
        if not make_symlink(link, self.tmp / "geheim.txt"):
            self.skipTest("Dieses System erlaubt keine Verknüpfungen ohne Sonderrechte.")
        with self.assertRaises(ValueError):
            paths.resolve(self.root, "raus")
        with self.assertRaises(ValueError):
            paths.resolve(self.root, "raus/tiefer")

    def test_symlink_als_ordner_mitten_im_pfad(self):
        aussen = self.tmp / "aussen"
        aussen.mkdir()
        (aussen / "beute.txt").write_text("x", encoding="utf-8")
        link = self.root / "plugins" / "weg"
        if not make_symlink(link, aussen):
            self.skipTest("Dieses System erlaubt keine Verknüpfungen ohne Sonderrechte.")
        with self.assertRaises(ValueError):
            paths.resolve(self.root, "plugins/weg/beute.txt")

    def test_relative_und_inside(self):
        ziel = self.root / "plugins" / "x.jar"
        self.assertEqual(paths.relative(self.root, ziel), "plugins/x.jar")
        self.assertEqual(paths.relative(self.root, self.root), "")
        self.assertFalse(paths.inside(self.root, self.tmp / "geheim.txt"))
        with self.assertRaises(ValueError):
            paths.relative(self.root, self.tmp / "geheim.txt")


class TestFreigabe(unittest.TestCase):
    def test_einordnung(self):
        faelle = {
            "plugins/EssentialsX.jar": "plugin",
            "mods/sodium.jar": "plugin",
            "world/level.dat": "world",
            "logs/latest.log": "log",
            "backups/welten.zip": "backup",
            "server.properties": "config",
            "server.jar": "tech",
            "libraries/net/x.jar": "tech",
            ".mcsm/notizen": "reserved",
            "world/level.dat.mcsmpart": "reserved",
        }
        for rel, art in faelle.items():
            with self.subTest(rel=rel):
                self.assertEqual(paths.classify(rel), art)
        self.assertEqual(paths.classify("plugins", True), "plugins")
        self.assertEqual(paths.classify("worlds", True), "world")

    def test_benutzer_darf_schreiben(self):
        for gut in ("plugins/EssentialsX.jar", "server.properties", "config/paper.yml",
                    "worlds/Bikini Bottom/level.dat", "notizen.txt"):
            with self.subTest(gut=gut):
                self.assertEqual(paths.check_user_write(gut), paths.normalize(gut))

    def test_benutzer_darf_nicht_schreiben(self):
        for bad in ("server.jar", "libraries/x.jar", ".mcsm/config.json", "run.sh",
                    "irgendwas.bin", "start.exe", "", "../plugins/x.jar"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.check_user_write(bad)

    def test_loeschen(self):
        self.assertEqual(paths.check_user_delete("worlds/Alt"), "worlds/Alt")
        self.assertEqual(paths.check_user_delete("plugins/x.jar"), "plugins/x.jar")
        for bad in ("", "server.properties", "server.jar", ".mcsm/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.check_user_delete(bad)

    def test_uebertragung_darf_technik(self):
        self.assertEqual(paths.check_transfer("server.jar"), "server.jar")
        self.assertEqual(paths.check_transfer("libraries/a/b.jar"), "libraries/a/b.jar")
        for bad in ("", ".mcsm/x", "welt/level.dat.mcsmpart", "../x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    paths.check_transfer(bad)

    def test_sichtbarkeit_und_text(self):
        self.assertTrue(paths.is_hidden("server.jar"))
        self.assertTrue(paths.is_hidden("session.lock"))
        self.assertFalse(paths.is_hidden("plugins/EssentialsX.jar"))
        self.assertFalse(paths.is_hidden("logs/latest.log"))
        self.assertTrue(paths.is_text("server.properties"))
        self.assertFalse(paths.is_text("plugins/x.jar"))


class TestListeUndWalk(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcsm-liste-"))
        self.root = self.tmp / "instanz"
        (self.root / "plugins").mkdir(parents=True)
        (self.root / "logs").mkdir()
        (self.root / ".mcsm").mkdir()
        (self.root / "plugins" / "x.jar").write_bytes(b"jar")
        (self.root / "logs" / "latest.log").write_text("hallo", encoding="utf-8")
        (self.root / "server.properties").write_text("level-name=welt\n", encoding="utf-8")
        (self.root / "server.jar").write_bytes(b"MZ")
        (self.root / ".mcsm" / "intern.json").write_text("{}", encoding="utf-8")
        (self.root / "plugins" / "x.jar.mcsmpart").write_bytes(b"halb")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_liste_verbirgt_verwaltung(self):
        listing = paths.list_dir(self.root, "")
        namen = [e["name"] for e in listing["entries"]]
        self.assertIn("plugins", namen)
        self.assertIn("server.properties", namen)
        self.assertNotIn(".mcsm", namen)
        self.assertTrue(all(e["is_dir"] for e in listing["entries"][:2]))
        technik = [e for e in listing["entries"] if e["name"] == "server.jar"][0]
        self.assertTrue(technik["hidden"])
        eintrag = [e for e in listing["entries"] if e["name"] == "server.properties"][0]
        self.assertTrue(eintrag["editable"])

    def test_liste_verbirgt_teildateien(self):
        namen = [e["name"] for e in paths.list_dir(self.root, "plugins")["entries"]]
        self.assertEqual(namen, ["x.jar"])

    def test_walk_files(self):
        gefunden = sorted(paths.walk_files(self.root))
        self.assertIn("plugins/x.jar", gefunden)
        self.assertIn("logs/latest.log", gefunden)
        self.assertIn("server.jar", gefunden)
        self.assertNotIn(".mcsm/intern.json", gefunden)
        self.assertNotIn("plugins/x.jar.mcsmpart", gefunden)

    def test_walk_ueberspringt_symlinks(self):
        aussen = self.tmp / "aussen"
        aussen.mkdir()
        (aussen / "beute.txt").write_text("x", encoding="utf-8")
        if not make_symlink(self.root / "weg", aussen):
            self.skipTest("Dieses System erlaubt keine Verknüpfungen ohne Sonderrechte.")
        gefunden = list(paths.walk_files(self.root))
        self.assertFalse([g for g in gefunden if g.startswith("weg")])

    def test_ordner_fehlt(self):
        with self.assertRaises(ValueError):
            paths.list_dir(self.root, "gibtsnicht")


class TestSicheresOeffnen(unittest.TestCase):
    """Lesen und Schreiben über Ordner-Deskriptoren: kein Zeitfenster für eine Verknüpfung."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcsm-paths-")
        self.root = pathlib.Path(self.tmp) / "instanz"
        (self.root / "plugins").mkdir(parents=True)
        (self.root / "plugins" / "notizen.yml").write_text("hallo", encoding="utf-8")
        self.geheim = pathlib.Path(self.tmp) / "users.json"
        self.geheim.write_text('{"users": []}', encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lesen_und_schreiben(self):
        self.assertEqual(paths.lies_stueck(self.root, "plugins/notizen.yml"), b"hallo")
        self.assertEqual(paths.lies_stueck(self.root, "plugins/notizen.yml", 1, 3), b"all")
        paths.schreibe_atomar(self.root, "plugins/notizen.yml", b"neu")
        self.assertEqual((self.root / "plugins" / "notizen.yml").read_bytes(), b"neu")
        self.assertEqual(paths.groesse(self.root, "plugins/notizen.yml"), 3)
        self.assertEqual(paths.groesse(self.root, "plugins/gibtsnicht.yml"), -1)

    def test_anhaengen_und_ersetzen(self):
        self.assertEqual(paths.anhaengen(self.root, "plugins/teil.mcsmpart", b"abc"), 3)
        self.assertEqual(paths.anhaengen(self.root, "plugins/teil.mcsmpart", b"de"), 5)
        paths.ersetzen(self.root, "plugins/teil.mcsmpart", "plugins/fertig.txt")
        self.assertEqual((self.root / "plugins" / "fertig.txt").read_bytes(), b"abcde")
        self.assertFalse((self.root / "plugins" / "teil.mcsmpart").exists())
        self.assertTrue(paths.entfernen(self.root, "plugins/fertig.txt"))
        self.assertFalse(paths.entfernen(self.root, "plugins/fertig.txt"))

    def test_pruefsumme(self):
        import hashlib
        self.assertEqual(paths.pruefsumme(self.root, "plugins/notizen.yml"),
                         hashlib.sha256(b"hallo").hexdigest())

    def test_verknuepfung_am_ende_wird_abgelehnt(self):
        link = self.root / "plugins" / "klau.yml"
        if not make_symlink(link, self.geheim):
            self.skipTest("Dieses System legt keine Verknüpfungen an.")
        with self.assertRaises((ValueError, OSError)):
            paths.lies_stueck(self.root, "plugins/klau.yml")

    def test_verknuepfung_im_ordnerteil_wird_abgelehnt(self):
        link = self.root / "fremd"
        if not make_symlink(link, pathlib.Path(self.tmp)):
            self.skipTest("Dieses System legt keine Verknüpfungen an.")
        if not paths.dirfd_moeglich():
            self.skipTest("Ohne dir_fd (Windows) greift der Rückfall über resolve().")
        with self.assertRaises((ValueError, OSError)):
            paths.lies_stueck(self.root, "fremd/users.json")
        with self.assertRaises((ValueError, OSError)):
            paths.schreibe_atomar(self.root, "fremd/users.json", b"boese")
        self.assertEqual(self.geheim.read_text(encoding="utf-8"), '{"users": []}')


if __name__ == "__main__":
    unittest.main()
