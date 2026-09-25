"""Selbsttests für die Betreiberoberfläche (hosted/web/admin.*).

Die Seite ist reines HTML/CSS/JS ohne Bauschritt – geprüft wird deshalb der Zusammenhalt der drei
Dateien: Werden alle gesuchten Kennungen und Klassen wirklich beschrieben? Holt die Seite nichts von
fremden Adressen? Steht jede benutzte API-Route auch in der Spezifikation? Dazu die beiden Dinge,
auf die es bei der Bedienung ankommt: der Anmeldeweg (nur Discord, danach einmalig der
Einladungscode) und die Barrierefreiheit.

Aufruf: python -m unittest discover -s hosted/tests
"""

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
SPEC = Path(__file__).resolve().parents[1] / "spec-admin.md"

# Kennungen, die die Seite selbst erzeugt (Rückfragekasten, Serverfenster) – nicht im HTML.
ERZEUGTE_IDS = {"askBack", "askNo", "askYes",
                "srvBack", "srvZu", "srvKonsole", "srvPill"}
# „view“ ist nur der Anfang von $('#view' + Name) und keine eigene Kennung.
BERECHNETE_IDS = {"view"}
# Klassen, die nur als Markierung für das Skript dienen und absichtlich kein Aussehen haben.
MARKIERUNGS_KLASSEN = {"view"}
# Adressen, die die Seite ansprechen darf.
ERLAUBTE_ADRESSEN = ("https://discord.com/", "https://discordapp.com/",
                     "https://cdn.discordapp.com/")
# Die Reiter, die es geben soll – Reihenfolge wie in der Kopfzeile.
REITER = ["uebersicht", "users", "invites", "passes", "servers", "machine"]


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
        # setText('#…') sucht genauso eine Kennung – nur über eine kleine Hilfe.
        gesucht |= set(re.findall(r"setText\('#([A-Za-z0-9_-]+)'", js))
        fehlend = gesucht - vorhanden - ERZEUGTE_IDS - BERECHNETE_IDS
        self.assertFalse(fehlend, f"Das Skript sucht Kennungen, die es im HTML nicht gibt: {sorted(fehlend)}")

    def test_reiter_haben_ihren_bereich(self):
        html = lies("admin.html")
        reiter = re.findall(r'class="tab[^"]*" data-view="([a-z]+)"', html)
        self.assertEqual(reiter, REITER, "Die Reiter stimmen nicht mit der Verabredung überein")
        for name in reiter:
            kennung = "view" + name[0].upper() + name[1:]
            with self.subTest(reiter=name):
                self.assertIn(f'id="{kennung}"', html,
                              f"Zum Reiter „{name}“ fehlt der Bereich {kennung}")

    def test_skript_kennt_dieselben_reiter(self):
        js = lies("admin.js")
        treffer = re.search(r"const VIEWS = \[([^\]]+)\]", js)
        self.assertTrue(treffer, "Im Skript fehlt die Liste VIEWS der Reiter")
        namen = re.findall(r"'([a-z]+)'", treffer.group(1))
        self.assertEqual(namen, REITER, "VIEWS im Skript passt nicht zu den Reitern im HTML")

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


class TestAussehen(unittest.TestCase):
    """Die Seite soll ruhig wirken und die Breite des Fensters nutzen."""

    def test_seite_nutzt_die_breite(self):
        css = lies("admin.css")
        treffer = re.search(r"--page-max:\s*(\d+)px", css)
        self.assertTrue(treffer, "In admin.css fehlt --page-max (Breite des Inhalts)")
        self.assertGreaterEqual(int(treffer.group(1)), 1400,
                                "Der Inhalt soll im breiten Fenster keine schmale Spalte sein")
        self.assertIn("max-width: var(--page-max)", css,
                      "Die Seite benutzt --page-max nicht")

    def test_keine_leeren_abstandshalter(self):
        html = lies("admin.html")
        self.assertNotIn('class="spacer"', html,
                         "Luft kommt aus den Karten, nicht aus leeren Kästchen")
        leer = re.findall(r"<div[^>]*>\s*</div>", html)
        erlaubt = ("id=", "class=\"head-live\"")
        for stelle in leer:
            # Leere Kästen sind nur erlaubt, wenn das Skript sie füllt (sie tragen eine Kennung).
            with self.subTest(stelle=stelle[:60]):
                self.assertTrue(any(w in stelle for w in erlaubt),
                                f"Leerer Kasten ohne Zweck: {stelle[:60]}")

    def test_balken_pillen_und_leere_zustaende(self):
        css = lies("admin.css")
        js = lies("admin.js")
        for klasse in (".bar", ".pill-green", ".pill-amber", ".empty", ".meter", ".stat"):
            with self.subTest(klasse=klasse):
                self.assertIn(klasse, css, f"{klasse} fehlt im Aussehen")
        self.assertIn("function leer(", js, "Es fehlt der gemeinsame leere Zustand")
        self.assertIn("function meter(", js, "Es fehlen die Balken für die Auslastung")


class TestBarrierearm(unittest.TestCase):
    """Tastaturbedienung, sichtbarer Fokus, sprechende Beschriftungen."""

    def test_fokus_ist_sichtbar(self):
        css = lies("admin.css")
        self.assertIn(":focus-visible", css, "Ohne sichtbaren Fokus ist die Seite nicht bedienbar")

    def test_sprunglink_und_rollen(self):
        html = lies("admin.html")
        self.assertIn('class="skip"', html, "Der Sprunglink zum Inhalt fehlt")
        self.assertIn('role="tablist"', html)
        self.assertIn('aria-selected=', html)
        self.assertIn('aria-label=', html)

    def test_kaesten_sind_richtig_ausgezeichnet(self):
        js = lies("admin.js")
        self.assertEqual(js.count('role="dialog"'), 2,
                         "Rückfrage und Serverfenster sollen beide ein Dialog sein")
        self.assertEqual(js.count('aria-modal="true"'), 2)
        self.assertIn("aria-labelledby=", js)
        self.assertIn("function fangeFokus(", js,
                      "Die Tabulatortaste muss im offenen Kasten bleiben")

    def test_keine_fenster_des_browsers(self):
        """Rückfragen gehören in den eigenen Kasten, nicht in das graue Fenster des Browsers."""
        js = lies("admin.js")
        for gefaehrlich in ("confirm(", "alert(", "prompt("):
            with self.subTest(stelle=gefaehrlich):
                self.assertNotIn(gefaehrlich, js)
        self.assertIn("function ask(", js)


class TestAnmeldung(unittest.TestCase):
    """Es gibt nur einen Weg herein: Discord. Der Einladungscode kommt erst in Schritt 2."""

    def test_kein_codefeld_auf_der_anmeldeseite(self):
        html = lies("admin.html")
        js = lies("admin.js")
        self.assertIn('id="btnDiscord"', html, "Der Discord-Knopf fehlt")
        for weg in ('id="inviteCode"', 'id="inviteName"', 'id="btnCodeToggle"', 'id="codeBox"'):
            with self.subTest(stelle=weg):
                self.assertNotIn(weg, html, "Das Einladungscode-Feld gehört nicht mehr auf die "
                                            "Anmeldeseite")
        self.assertNotIn("/api/auth/invite", js,
                         "Die Verwaltung meldet sich nicht mehr mit einem Einladungscode an")

    def test_schritt_zwei_ist_vorhanden(self):
        html = lies("admin.html")
        for kennung in ('id="regBox"', 'id="regCode"', 'id="btnReg"', 'id="regHello"',
                        'id="regTimer"', 'id="regFirst"', 'id="btnRegBack"'):
            with self.subTest(stelle=kennung):
                self.assertIn(kennung, html, f"Für Schritt 2 fehlt {kennung}")
        self.assertIn("ABCD-EFGH-IJKL", html, "Das Beispielformat des Codes fehlt")

    def test_rueckleitung_wird_gelesen(self):
        js = lies("admin.js")
        self.assertIn("q.get('registrierung')", js, "Der Merkzettel aus der Raute wird nicht gelesen")
        self.assertIn("q.get('name')", js, "Der Anzeigename aus der Raute wird nicht gelesen")
        self.assertIn("q.get('verknuepft')", js, "Die Rückkehr vom Verknüpfen wird nicht erkannt")
        self.assertIn("q.get('token')", js)
        self.assertIn("q.get('fehler')", js)

    def test_registrierung_wird_geschickt(self):
        js = lies("admin.js")
        self.assertIn("/api/auth/discord/register", js)
        self.assertIn("zettel: state.reg.zettel", js,
                      "Der Merkzettel muss mit dem Code zusammen geschickt werden")

    def test_abgelaufener_merkzettel_fuehrt_zurueck(self):
        js = lies("admin.js")
        stelle = js[js.index("async function registrieren("):]
        stelle = stelle[:stelle.index("\n}\n")]
        self.assertIn("showLogin(", stelle,
                      "Ein abgelaufener Merkzettel muss zurück auf „Mit Discord anmelden“ führen")

    def test_erstzugang_wird_erklaert(self):
        js = lies("admin.js")
        self.assertIn("ERSTER-ADMIN.txt", js,
                      "Der Fall „noch kein Betreiberkonto“ muss erklärt werden")
        self.assertIn("erstzugang", js)

    def test_discord_nachtraeglich_verknuepfen(self):
        html = lies("admin.html")
        js = lies("admin.js")
        self.assertIn('id="btnLink"', html)
        self.assertIn("discord/start?link=1", js)


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

    def test_neuer_anmeldeweg_steht_in_der_spec(self):
        spec = SPEC.read_text(encoding="utf-8")
        for stelle in ("/api/auth/discord/register", "registrierung=", "ERSTER-ADMIN.txt",
                       "verknuepft=1"):
            with self.subTest(stelle=stelle):
                self.assertIn(stelle, spec, f"„{stelle}“ fehlt in spec-admin.md")

    def test_auffrischung_alle_fuenf_sekunden(self):
        js = lies("admin.js")
        self.assertIn("const REFRESH_MS = 5000;", js)
        self.assertIn("setInterval(tick, REFRESH_MS)", js)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
