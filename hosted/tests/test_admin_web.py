"""Selbsttests für die Betreiberoberfläche (hosted/web/admin.*).

Die Seite ist reines HTML/CSS/JS ohne Bauschritt – geprüft wird deshalb der Zusammenhalt der drei
Dateien: Werden alle gesuchten Kennungen und Klassen wirklich beschrieben? Holt die Seite nichts von
fremden Adressen? Steht jede benutzte API-Route auch in der Spezifikation?

Aufruf: python -m unittest discover -s hosted/tests
"""

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
SPEC = Path(__file__).resolve().parents[1] / "spec-admin.md"

# Kennungen, die die Seite selbst erzeugt (Rückfragekasten) – die stehen nicht im HTML.
ERZEUGTE_IDS = {"askBack", "askInput", "askNo", "askYes"}
# „view“ ist nur der Anfang von $('#view' + Name) und keine eigene Kennung.
BERECHNETE_IDS = {"view"}
# Klassen, die nur als Markierung für das Skript dienen und absichtlich kein Aussehen haben.
MARKIERUNGS_KLASSEN = {"view"}
# Adressen, die die Seite ansprechen darf.
ERLAUBTE_ADRESSEN = ("https://discord.com/", "https://discordapp.com/",
                     "https://cdn.discordapp.com/")


def lies(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


class TestDateien(unittest.TestCase):
    """Sind die drei Dateien da und sauber lesbar?"""

    def test_dateien_vorhanden(self):
        for name in ("admin.html", "admin.css", "admin.js"):
            with self.subTest(datei=name):
                pfad = WEB / name
                self.assertTrue(pfad.is_file(), f"{name} fehlt in hosted/web/")
                self.assertGreater(pfad.stat().st_size, 500, f"{name} ist verdächtig klein")

    def test_utf8_und_keine_steuerzeichen(self):
        for name in ("admin.html", "admin.css", "admin.js"):
            with self.subTest(datei=name):
                text = lies(name)            # wirft bei kaputtem UTF-8
                verboten = set(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text))
                self.assertFalse(verboten, f"{name} enthält Steuerzeichen")

    def test_html_bindet_css_und_js_ein(self):
        html = lies("admin.html")
        self.assertIn('href="admin.css"', html)
        self.assertIn('src="admin.js"', html)
        self.assertIn('<html lang="de">', html)
        self.assertIn('charset="utf-8"', html)


class TestZusammenhalt(unittest.TestCase):
    """Passen HTML, CSS und JS zueinander?"""

    def test_gesuchte_kennungen_gibt_es_im_html(self):
        html = lies("admin.html")
        js = lies("admin.js")
        vorhanden = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        gesucht = set(re.findall(r"\$\$?\('#([A-Za-z0-9_-]+)'", js))
        fehlend = gesucht - vorhanden - ERZEUGTE_IDS - BERECHNETE_IDS
        self.assertFalse(fehlend, f"Das Skript sucht Kennungen, die es im HTML nicht gibt: {sorted(fehlend)}")

    def test_reiter_haben_ihren_bereich(self):
        html = lies("admin.html")
        reiter = re.findall(r'class="tab[^"]*" data-view="([a-z]+)"', html)
        self.assertEqual(len(reiter), 5, "Es sollten fünf Reiter sein")
        for name in reiter:
            kennung = "view" + name[0].upper() + name[1:]
            with self.subTest(reiter=name):
                self.assertIn(f'id="{kennung}"', html,
                              f"Zum Reiter „{name}“ fehlt der Bereich {kennung}")

    def test_benutzte_klassen_sind_beschrieben(self):
        html = lies("admin.html")
        css = lies("admin.css")
        beschrieben = set(re.findall(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)", css))
        benutzt = set()
        for wert in re.findall(r'class="([^"]+)"', html):
            benutzt.update(w for w in wert.split() if w)
        fehlend = benutzt - beschrieben - MARKIERUNGS_KLASSEN
        self.assertFalse(fehlend, f"Im HTML benutzte Klassen ohne Aussehen: {sorted(fehlend)}")

    def test_farben_wie_im_programm_auf_dem_pc(self):
        """Die Oberfläche soll aussehen wie das lokale Programm – gleiche Grundfarben."""
        css = lies("admin.css")
        for farbe in ("#0d1117", "#161c26", "#3ddc84", "#f0b429", "#f2626b", "#54a0ff"):
            with self.subTest(farbe=farbe):
                self.assertIn(farbe, css)


class TestSorgfalt(unittest.TestCase):
    """Nichts von fremden Adressen, keine Reste, deutsche Texte."""

    def test_keine_fremden_adressen(self):
        for name in ("admin.html", "admin.css", "admin.js"):
            with self.subTest(datei=name):
                for treffer in re.findall(r"https?://[^\s\"'()]+", lies(name)):
                    if treffer.startswith("http://127.0.0.1"):
                        continue
                    self.assertTrue(treffer.startswith(ERLAUBTE_ADRESSEN),
                                    f"{name} spricht eine fremde Adresse an: {treffer}")

    def test_keine_eingebundenen_fremddateien(self):
        html = lies("admin.html")
        for quelle in re.findall(r'(?:src|href)="([^"]+)"', html):
            with self.subTest(quelle=quelle):
                self.assertFalse(quelle.startswith(("http://", "https://", "//")),
                                 "Die Seite darf nichts von außen nachladen")

    def test_keine_reste_und_keine_werbung(self):
        for name in ("admin.html", "admin.css", "admin.js"):
            text = lies(name).lower()
            with self.subTest(datei=name):
                for wort in ("todo", "fixme", "lorem ipsum"):
                    self.assertNotIn(wort, text, f"„{wort}“ hat in {name} nichts zu suchen")

    def test_ausgaben_werden_entschaerft(self):
        js = lies("admin.js")
        self.assertIn("const esc =", js, "Es fehlt die Funktion zum Entschärfen von Text")
        for gefaehrlich in ("eval(", "document.write", "new Function("):
            with self.subTest(stelle=gefaehrlich):
                self.assertNotIn(gefaehrlich, js)

    def test_token_geht_nur_in_den_kopf(self):
        """Das Sitzungstoken darf nie in einer Adresse stehen."""
        js = lies("admin.js")
        self.assertIn("'Bearer ' + state.token", js)
        self.assertNotIn("token=' + ", js)
        self.assertNotIn("?token=", js)

    def test_kennungen_werden_kodiert(self):
        """Fremde Kennungen kommen kodiert in den Pfad, nie roh zusammengeklebt."""
        js = lies("admin.js")
        pfade = re.findall(r"api\('(/api/[^']*)'\s*\+\s*([A-Za-z]+)", js)
        for pfad, ausdruck in pfade:
            with self.subTest(pfad=pfad):
                self.assertEqual(ausdruck, "encodeURIComponent",
                                 f"Bei {pfad} wird eine Kennung ungeprüft angehängt")


class TestSpezifikation(unittest.TestCase):
    """Jede benutzte Route muss in spec-admin.md stehen – sonst weiß die Daemon-Gruppe nichts davon."""

    def test_spec_vorhanden(self):
        self.assertTrue(SPEC.is_file(), "hosted/spec-admin.md fehlt")

    def test_alle_routen_sind_beschrieben(self):
        js = lies("admin.js")
        spec = SPEC.read_text(encoding="utf-8")
        routen = set()
        for treffer in re.findall(r"'(/api/[A-Za-z0-9/_-]*)", js):
            routen.add(treffer.rstrip("/"))
        self.assertTrue(routen, "Im Skript wurde keine einzige Route gefunden")
        for route in sorted(routen):
            with self.subTest(route=route):
                self.assertIn(route, spec, f"Route {route} steht nicht in spec-admin.md")

    def test_auffrischung_alle_fuenf_sekunden(self):
        js = lies("admin.js")
        self.assertIn("const REFRESH_MS = 5000;", js)
        self.assertIn("setInterval(tick, REFRESH_MS)", js)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
