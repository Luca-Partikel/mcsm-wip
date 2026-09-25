"""Selbsttests für die Discord-Anmeldung (hosted/core/oauth.py).

Läuft ohne echte Discord-Zugangsdaten und ohne Netzverbindung:

* Die Zugangsdaten liegen in einem Temp-Ordner (``MCSM_DISCORD_DIR``), die Konten ebenfalls
  (``MCSM_DATA``).
* Statt ins Netz zu gehen, ersetzen die Tests ``oauth._urlopen`` – die einzige Stelle, an der
  das Modul urllib wirklich benutzt.
* Die Zeit wird über den Parameter ``now`` gesteuert.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.parse

HOSTED = pathlib.Path(__file__).resolve().parent.parent
PKG = "mcsm_hosted"


def _module(name: str):
    """Modul aus hosted/core laden (eigener Paketname, siehe test_passes.py)."""
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(HOSTED / "core")]        # type: ignore[attr-defined]
        pkg.__package__ = PKG
        sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.{name}")


store_hosted = _module("store_hosted")
users = _module("users")
oauth = _module("oauth")

T0 = 1_700_000_000
CLIENT_ID = "1234567890123456789"
SECRET = "abcdefghij0123456789ABCDEFGHIJ_-"
DISCORD_ID = "223344556677889900"
AVATAR = "0123456789abcdef0123456789abcdef"


# --------------------------------------------------------------------------- nachgebautes Discord

class FakeAntwort:
    """Antwort, wie urllib sie liefern würde (Kontextmanager mit read())."""

    def __init__(self, inhalt, status: int = 200) -> None:
        if isinstance(inhalt, bytes):
            self._daten = inhalt
        else:
            self._daten = json.dumps(inhalt).encode("utf-8")
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._daten if n is None or n < 0 else self._daten[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Netz:
    """Ersatz für ``oauth._urlopen``: liefert vorbereitete Antworten und merkt sich die Anfragen."""

    def __init__(self) -> None:
        self.antworten: dict[str, object] = {}
        self.aufrufe: list[dict] = []

    def setzen(self, url: str, antwort) -> None:
        self.antworten[url] = antwort

    def __call__(self, request, timeout):
        felder = None
        if request.data:
            felder = dict(urllib.parse.parse_qsl(request.data.decode("utf-8")))
        self.aufrufe.append({"url": request.full_url, "felder": felder,
                             "kopf": dict(request.headers), "methode": request.get_method(),
                             "timeout": timeout})
        antwort = self.antworten.get(request.full_url)
        if antwort is None:
            raise AssertionError(f"Unerwartete Anfrage an {request.full_url}")
        if callable(antwort) and not isinstance(antwort, FakeAntwort):
            antwort = antwort()
        if isinstance(antwort, BaseException):
            raise antwort
        return antwort

    def urls(self) -> list[str]:
        return [a["url"] for a in self.aufrufe]

    def felder_von(self, url: str) -> dict:
        for aufruf in self.aufrufe:
            if aufruf["url"] == url:
                return aufruf["felder"] or {}
        raise AssertionError(f"{url} wurde nicht aufgerufen.")

    def kopf_von(self, url: str) -> dict:
        for aufruf in self.aufrufe:
            if aufruf["url"] == url:
                return aufruf["kopf"]
        raise AssertionError(f"{url} wurde nicht aufgerufen.")


def http_fehler(status: int, inhalt) -> urllib.error.HTTPError:
    rohdaten = inhalt if isinstance(inhalt, bytes) else json.dumps(inhalt).encode("utf-8")
    return urllib.error.HTTPError("https://discord.com/", status, "Fehler", {},
                                  io.BytesIO(rohdaten))


def profil(**kw) -> dict:
    """Nachgebaute Antwort von /users/@me."""
    daten = {"id": DISCORD_ID, "username": "lucap", "global_name": "Luca", "avatar": AVATAR,
             "discriminator": "0"}
    daten.update(kw)
    return daten


# --------------------------------------------------------------------------- Grundgerüst

class Basis(unittest.TestCase):
    """Frischer Datenordner, frische Zugangsdaten, kein Netz."""

    zugangsdaten_anlegen = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ordner = pathlib.Path(self._tmp.name)
        self._alt = {name: os.environ.get(name) for name in ("MCSM_DATA", "MCSM_DISCORD_DIR")}
        os.environ["MCSM_DATA"] = str(self.ordner)
        os.environ["MCSM_DISCORD_DIR"] = str(self.ordner)
        store_hosted.ensure_dir()
        if self.zugangsdaten_anlegen:
            (self.ordner / oauth.CLIENT_ID_FILE).write_text(CLIENT_ID + "\n", encoding="utf-8")
            (self.ordner / oauth.SECRET_FILE).write_text(SECRET + "\n", encoding="utf-8")
        oauth.vergessen()
        oauth.alles_leeren()
        self.netz = Netz()
        self.netz.setzen(oauth.TOKEN_URL, FakeAntwort({"access_token": "discord-zugriff-123",
                                                       "token_type": "Bearer", "scope": "identify",
                                                       "expires_in": 604800}))
        self.netz.setzen(oauth.IDENTITY_URL, FakeAntwort(profil()))
        self.netz.setzen(oauth.REVOKE_URL, FakeAntwort({}))
        self._echtes_urlopen = oauth._urlopen
        oauth._urlopen = self.netz

    def tearDown(self) -> None:
        oauth._urlopen = self._echtes_urlopen
        oauth.vergessen()
        oauth.alles_leeren()
        for name, wert in self._alt.items():
            if wert is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = wert
        self._tmp.cleanup()

    # -- Hilfen ------------------------------------------------------------
    def anmeldung_durchlaufen(self, flow: str = oauth.FLOW_ADMIN, **kw):
        """start() + complete() in einem Zug, mit dem nachgebauten Discord."""
        begonnen = oauth.start(flow, now=T0, **kw)
        ergebnis = oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 5)
        return begonnen, ergebnis

    def erstzugang(self, *, now: int = T0) -> str:
        """Den Einladungscode für das erste Betreiberkonto anlegen und hinterlegen.

        Im Betrieb macht das der Dienst beim Start (`mcsmd.ensure_first_admin`) und schreibt den
        Code nach ERSTER-ADMIN.txt. Ohne ihn wird über Discord **kein** Betreiberkonto angelegt.
        """
        einladung = users.create_invite("system", uses_max=1, days=0, now=now)
        oauth.erstzugang_setzen(einladung["code"])
        return users.format_code(einladung["code"])

    def betreiber(self, identitaet=None, *, now: int = T0):
        """Das erste Konto anlegen – mit dem Erstzugangscode, wie der Betreiber es tut."""
        return oauth.konto_fuer(identitaet if identitaet is not None else self.identitaet(),
                                invite_code=self.erstzugang(now=now), now=now)

    def identitaet(self, **kw) -> "oauth.Identitaet":
        daten = {"discord_id": DISCORD_ID, "benutzername": "lucap", "anzeigename": "Luca",
                 "avatar_url": "https://example.invalid/a.png"}
        daten.update(kw)
        return oauth.Identitaet(**daten)


# --------------------------------------------------------------------------- 1. Zugangsdaten

class TestZugangsdaten(Basis):

    def test_gelesen_und_gemerkt(self):
        daten = oauth.zugangsdaten()
        self.assertEqual(daten.client_id, CLIENT_ID)
        self.assertEqual(daten.secret, SECRET)
        self.assertTrue(oauth.eingerichtet())
        # Nach dem ersten Lesen wird nicht erneut in die Datei geschaut.
        (self.ordner / oauth.SECRET_FILE).unlink()
        self.assertEqual(oauth.zugangsdaten().secret, SECRET)

    def test_geheimnis_steht_in_keiner_textausgabe(self):
        daten = oauth.zugangsdaten()
        self.assertNotIn(SECRET, repr(daten))
        self.assertNotIn(SECRET, str(daten))
        self.assertIn("<geheim>", repr(daten))
        meldung = oauth.startmeldung()
        self.assertNotIn(SECRET, meldung)
        self.assertIn(CLIENT_ID, meldung)
        self.assertIn("identify", meldung)

    def test_fehlende_datei_meldet_und_bremst_nur_die_anmeldung(self):
        (self.ordner / oauth.CLIENT_ID_FILE).unlink()
        oauth.vergessen()
        self.assertFalse(oauth.eingerichtet())
        with self.assertRaises(oauth.OAuthNichtEingerichtet) as fall:
            oauth.start(oauth.FLOW_ADMIN, now=T0)
        self.assertIn("fehlt", fall.exception.message)
        self.assertEqual(fall.exception.status, 501)
        meldung = oauth.startmeldung()
        self.assertIn("nicht aktiv", meldung)
        self.assertIn("läuft trotzdem", meldung)

    def test_leere_datei(self):
        (self.ordner / oauth.SECRET_FILE).write_text("\n\n", encoding="utf-8")
        oauth.vergessen()
        with self.assertRaises(oauth.OAuthNichtEingerichtet) as fall:
            oauth.zugangsdaten()
        self.assertIn("leer", fall.exception.message)

    def test_unsinnige_client_id(self):
        (self.ordner / oauth.CLIENT_ID_FILE).write_text("keine-zahl\n", encoding="utf-8")
        oauth.vergessen()
        with self.assertRaises(oauth.OAuthNichtEingerichtet) as fall:
            oauth.zugangsdaten()
        self.assertIn("Ziffern", fall.exception.message)

    def test_unsinniges_geheimnis_wird_nicht_ausgegeben(self):
        kaputt = "viel zu kurz mit Leerzeichen"
        (self.ordner / oauth.SECRET_FILE).write_text(kaputt + "\n", encoding="utf-8")
        oauth.vergessen()
        with self.assertRaises(oauth.OAuthNichtEingerichtet) as fall:
            oauth.zugangsdaten()
        self.assertIn("Format", fall.exception.message)
        self.assertNotIn(kaputt, fall.exception.message)

    def test_fehler_wird_gemerkt_und_ordnerwechsel_liest_neu(self):
        (self.ordner / oauth.CLIENT_ID_FILE).unlink()
        oauth.vergessen()
        with self.assertRaises(oauth.OAuthNichtEingerichtet):
            oauth.zugangsdaten()
        (self.ordner / oauth.CLIENT_ID_FILE).write_text(CLIENT_ID, encoding="utf-8")
        # Gemerkter Fehler bleibt, bis jemand „vergessen“ ruft …
        with self.assertRaises(oauth.OAuthNichtEingerichtet):
            oauth.zugangsdaten()
        oauth.vergessen()
        self.assertEqual(oauth.zugangsdaten().client_id, CLIENT_ID)

    def test_ordner_faellt_auf_datenordner_zurueck(self):
        os.environ.pop("MCSM_DISCORD_DIR", None)
        oauth.vergessen()
        self.assertEqual(oauth.credentials_dir(), store_hosted.data_dir())
        self.assertTrue(oauth.eingerichtet())


# --------------------------------------------------------------------------- 2. Rückleitungen

class TestRueckleitungen(Basis):

    def test_feste_adressen(self):
        self.assertEqual(oauth.redirect_uri(oauth.FLOW_ADMIN),
                         "https://admin.arcardia-nexus.de/auth/discord/callback")
        self.assertEqual(oauth.redirect_uri(oauth.FLOW_APP),
                         "https://api.arcardia-nexus.de/auth/discord/callback")

    def test_lokale_rueckleitung(self):
        self.assertEqual(oauth.redirect_uri(oauth.FLOW_LOKAL, 52345),
                         "http://127.0.0.1:52345/cloud-callback")
        self.assertEqual(oauth.lokale_rueckleitung("41000"),
                         "http://127.0.0.1:41000/cloud-callback")

    def test_port_wird_geprueft(self):
        for schlecht in (0, 80, 70000, -1, "abc", None):
            with self.assertRaises(oauth.OAuthFehler):
                oauth.lokale_rueckleitung(schlecht)

    def test_unbekannter_weg(self):
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.redirect_uri("irgendwas")
        self.assertIn("Anmeldeweg", fall.exception.message)

    def test_lokaler_weg_ohne_port(self):
        with self.assertRaises(oauth.OAuthFehler):
            oauth.start(oauth.FLOW_LOKAL, now=T0)


# --------------------------------------------------------------------------- 3. Anmelde-URL

class TestStart(Basis):

    def query(self, url: str) -> dict:
        teile = urllib.parse.urlsplit(url)
        self.assertEqual(teile.scheme, "https")
        self.assertEqual(teile.netloc, "discord.com")
        self.assertEqual(teile.path, "/oauth2/authorize")
        return dict(urllib.parse.parse_qsl(teile.query))

    def test_aufbau_der_url(self):
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        felder = self.query(begonnen["url"])
        self.assertEqual(felder["response_type"], "code")
        self.assertEqual(felder["client_id"], CLIENT_ID)
        self.assertEqual(felder["scope"], "identify")
        self.assertEqual(felder["redirect_uri"],
                         "https://admin.arcardia-nexus.de/auth/discord/callback")
        self.assertEqual(felder["state"], begonnen["state"])
        self.assertNotIn("prompt", felder)
        self.assertEqual(begonnen["expires_at"], T0 + oauth.STATE_TTL_SECONDS)
        self.assertEqual(begonnen["flow"], "admin")

    def test_kein_zusaetzlicher_umfang(self):
        begonnen = oauth.start(oauth.FLOW_APP, now=T0)
        felder = self.query(begonnen["url"])
        self.assertEqual(felder["scope"].split(), ["identify"])
        self.assertNotIn("email", begonnen["url"])
        self.assertNotIn(SECRET, begonnen["url"])

    def test_zustandswert_ist_zufaellig_und_lang(self):
        eins = oauth.start(oauth.FLOW_ADMIN, now=T0)["state"]
        zwei = oauth.start(oauth.FLOW_ADMIN, now=T0)["state"]
        self.assertNotEqual(eins, zwei)
        self.assertGreaterEqual(len(eins), 32)
        self.assertEqual(oauth.zustaende_offen(now=T0), 2)

    def test_lokaler_weg_traegt_den_port(self):
        begonnen = oauth.start(oauth.FLOW_LOKAL, port=52345, now=T0)
        felder = self.query(begonnen["url"])
        self.assertEqual(felder["redirect_uri"], "http://127.0.0.1:52345/cloud-callback")
        self.assertEqual(begonnen["redirect_uri"], "http://127.0.0.1:52345/cloud-callback")

    def test_einladungscode_wird_gemerkt(self):
        begonnen = oauth.start(oauth.FLOW_APP, invite_code="abcd-efgh-jklm", now=T0)
        ergebnis = oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 3)
        self.assertEqual(ergebnis.invite_code, "ABCDEFGHJKLM")
        self.assertEqual(ergebnis.flow, "app")

    def test_unsinniger_einladungscode_faellt_sofort_auf(self):
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.start(oauth.FLOW_APP, invite_code="ABC", now=T0)
        self.assertIn("Einladungscode", fall.exception.message)
        self.assertEqual(oauth.zustaende_offen(now=T0), 0)

    def test_prompt_nur_erlaubte_werte(self):
        felder = self.query(oauth.start(oauth.FLOW_ADMIN, prompt="consent", now=T0)["url"])
        self.assertEqual(felder["prompt"], "consent")
        with self.assertRaises(oauth.OAuthFehler):
            oauth.start(oauth.FLOW_ADMIN, prompt="immer", now=T0)

    def test_abholmerkmal_muss_sha256_sein(self):
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.start(oauth.FLOW_LOKAL, port=52345, abhol_hash="kurz", now=T0)
        self.assertIn("SHA256", fall.exception.message)


# --------------------------------------------------------------------------- 4. Zustandswerte

class TestZustand(Basis):

    def test_genau_einmal_verwendbar(self):
        state = oauth.start(oauth.FLOW_ADMIN, now=T0)["state"]
        self.assertEqual(oauth.zustand_einloesen(state, now=T0 + 1).flow, "admin")
        with self.assertRaises(oauth.OAuthZustandFehler) as fall:
            oauth.zustand_einloesen(state, now=T0 + 2)
        self.assertIn("abgelaufen", fall.exception.message)

    def test_unbekannter_zustand(self):
        with self.assertRaises(oauth.OAuthZustandFehler):
            oauth.zustand_einloesen("A" * 40, now=T0)

    def test_abgelaufener_zustand(self):
        state = oauth.start(oauth.FLOW_ADMIN, now=T0)["state"]
        with self.assertRaises(oauth.OAuthZustandFehler) as fall:
            oauth.zustand_einloesen(state, now=T0 + oauth.STATE_TTL_SECONDS + 1)
        self.assertIn("erneut", fall.exception.message)

    def test_leerer_oder_kaputter_zustand(self):
        for schlecht in ("", "   ", "kurz", "A" * 200, "mit leerzeichen drin"):
            with self.assertRaises(oauth.OAuthZustandFehler) as fall:
                oauth.zustand_einloesen(schlecht, now=T0)
            self.assertEqual(fall.exception.status, 400)

    def test_aufraeumen(self):
        oauth.start(oauth.FLOW_ADMIN, now=T0)
        oauth.start(oauth.FLOW_ADMIN, now=T0)
        self.assertEqual(oauth.zustaende_offen(now=T0), 2)
        self.assertEqual(oauth.zustaende_aufraeumen(now=T0 + oauth.STATE_TTL_SECONDS + 1), 2)
        self.assertEqual(oauth.zustaende_offen(now=T0), 0)

    def test_obergrenze(self):
        for _ in range(oauth.STATES_MAX + 20):
            oauth.start(oauth.FLOW_ADMIN, now=T0)
        self.assertLessEqual(oauth.zustaende_offen(now=T0), oauth.STATES_MAX)

    def test_complete_verlangt_gueltigen_zustand(self):
        oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.OAuthZustandFehler):
            oauth.complete("B" * 40, "anmeldecode123", now=T0 + 1)
        self.assertEqual(self.netz.aufrufe, [])          # kein Netzverkehr bei falschem Zustand


# --------------------------------------------------------------------------- 5. Rückleitung auswerten

class TestComplete(Basis):

    def test_glatter_durchlauf(self):
        begonnen, ergebnis = self.anmeldung_durchlaufen()
        person = ergebnis.identitaet
        self.assertEqual(person.discord_id, DISCORD_ID)
        self.assertEqual(person.benutzername, "lucap")
        self.assertEqual(person.anzeigename, "Luca")
        self.assertEqual(person.avatar_url,
                         f"https://cdn.discordapp.com/avatars/{DISCORD_ID}/{AVATAR}.png?size=128")
        self.assertEqual(person.as_dict()["discord_id"], DISCORD_ID)

        felder = self.netz.felder_von(oauth.TOKEN_URL)
        self.assertEqual(felder["grant_type"], "authorization_code")
        self.assertEqual(felder["code"], "anmeldecode123")
        self.assertEqual(felder["client_id"], CLIENT_ID)
        self.assertEqual(felder["client_secret"], SECRET)
        self.assertEqual(felder["redirect_uri"],
                         "https://admin.arcardia-nexus.de/auth/discord/callback")
        self.assertEqual(self.netz.kopf_von(oauth.IDENTITY_URL)["Authorization"],
                         "Bearer discord-zugriff-123")
        # Das Token wird sofort wieder weggeworfen.
        self.assertIn(oauth.REVOKE_URL, self.netz.urls())
        self.assertEqual(self.netz.felder_von(oauth.REVOKE_URL)["token"], "discord-zugriff-123")

    def test_lokaler_weg_nimmt_die_lokale_rueckleitung(self):
        begonnen = oauth.start(oauth.FLOW_LOKAL, port=52345, now=T0)
        ergebnis = oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertEqual(ergebnis.flow, "lokal")
        self.assertEqual(ergebnis.port, 52345)
        self.assertEqual(self.netz.felder_von(oauth.TOKEN_URL)["redirect_uri"],
                         "http://127.0.0.1:52345/cloud-callback")

    def test_bewegtes_bild_und_standardbilder(self):
        self.netz.setzen(oauth.IDENTITY_URL, FakeAntwort(profil(avatar="a_" + AVATAR)))
        _, ergebnis = self.anmeldung_durchlaufen()
        self.assertTrue(ergebnis.identitaet.avatar_url.endswith(".gif?size=128"))

        oauth.alles_leeren()
        self.netz.setzen(oauth.IDENTITY_URL, FakeAntwort(profil(avatar=None)))
        _, ergebnis = self.anmeldung_durchlaufen()
        erwartet = (int(DISCORD_ID) >> 22) % 6
        self.assertEqual(ergebnis.identitaet.avatar_url,
                         f"https://cdn.discordapp.com/embed/avatars/{erwartet}.png")

        oauth.alles_leeren()
        self.netz.setzen(oauth.IDENTITY_URL,
                         FakeAntwort(profil(avatar="", discriminator="1234")))
        _, ergebnis = self.anmeldung_durchlaufen()
        self.assertEqual(ergebnis.identitaet.avatar_url,
                         f"https://cdn.discordapp.com/embed/avatars/{1234 % 5}.png")

    def test_gefaelschter_avatarwert_wird_nicht_uebernommen(self):
        self.netz.setzen(oauth.IDENTITY_URL,
                         FakeAntwort(profil(avatar="../../../boese.png?x=y")))
        _, ergebnis = self.anmeldung_durchlaufen()
        self.assertIn("/embed/avatars/", ergebnis.identitaet.avatar_url)

    def test_anzeigename_faellt_auf_benutzernamen_zurueck(self):
        self.netz.setzen(oauth.IDENTITY_URL, FakeAntwort(profil(global_name=None)))
        _, ergebnis = self.anmeldung_durchlaufen()
        self.assertEqual(ergebnis.identitaet.anzeigename, "lucap")

    def test_nutzer_bricht_ab(self):
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.OAuthAbgebrochen) as fall:
            oauth.complete(begonnen["state"], "", error="access_denied", now=T0 + 1)
        self.assertIn("abgebrochen", fall.exception.message)
        self.assertEqual(self.netz.aufrufe, [])
        # Der Vorgang ist danach zu, der Zustandswert taugt nicht mehr.
        with self.assertRaises(oauth.OAuthZustandFehler):
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 2)

    def test_anderer_fehler_von_discord(self):
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.complete(begonnen["state"], "", error="server_error",
                           error_description="Etwas\nging schief", now=T0 + 1)
        self.assertIn("Etwas ging schief", fall.exception.message)

    def test_fehlender_anmeldecode(self):
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.complete(begonnen["state"], "", now=T0 + 1)
        self.assertIn("kein", fall.exception.message.lower())

    def test_unsinniger_anmeldecode_geht_nicht_ins_netz(self):
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.OAuthFehler):
            oauth.complete(begonnen["state"], "zu kurz & krumm", now=T0 + 1)
        self.assertEqual(self.netz.aufrufe, [])

    def test_abgelaufener_code(self):
        self.netz.setzen(oauth.TOKEN_URL, http_fehler(400, {"error": "invalid_grant"}))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertIn("nicht mehr gültig", fall.exception.message)
        self.assertEqual(fall.exception.status, 400)

    def test_falsche_zugangsdaten_melden_serverfehler(self):
        self.netz.setzen(oauth.TOKEN_URL, http_fehler(401, {"error": "invalid_client"}))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertEqual(fall.exception.status, 500)
        self.assertIn("Zugangsdaten", fall.exception.message)

    def test_discord_nicht_erreichbar(self):
        self.netz.setzen(oauth.TOKEN_URL, urllib.error.URLError("Name kann nicht aufgelöst werden"))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertIn("nicht erreichbar", fall.exception.message)
        self.assertEqual(fall.exception.status, 502)

    def test_zeitueberschreitung(self):
        self.netz.setzen(oauth.IDENTITY_URL, TimeoutError("zu langsam"))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler):
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        # Auch wenn es schiefgeht: das Zugriffstoken wird widerrufen.
        self.assertIn(oauth.REVOKE_URL, self.netz.urls())

    def test_bremse_von_discord(self):
        self.netz.setzen(oauth.TOKEN_URL, http_fehler(429, {"retry_after": 3}))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertEqual(fall.exception.status, 503)

    def test_unlesbare_antwort(self):
        self.netz.setzen(oauth.TOKEN_URL, FakeAntwort(b"<html>kaputt</html>"))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertIn("nicht lesbar", fall.exception.message)

    def test_antwort_ohne_token(self):
        self.netz.setzen(oauth.TOKEN_URL, FakeAntwort({"token_type": "Bearer"}))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertIn("Zugriffsrecht", fall.exception.message)

    def test_falscher_umfang(self):
        self.netz.setzen(oauth.TOKEN_URL, FakeAntwort({"access_token": "abcdefghij",
                                                       "token_type": "Bearer", "scope": "email"}))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertIn("identify", fall.exception.message)

    def test_profil_ohne_kennung(self):
        self.netz.setzen(oauth.IDENTITY_URL, FakeAntwort({"username": "ohne-id"}))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertIn("Kennung", fall.exception.message)

    def test_widerruf_darf_scheitern(self):
        self.netz.setzen(oauth.REVOKE_URL, http_fehler(500, {"error": "boom"}))
        _, ergebnis = self.anmeldung_durchlaufen()
        self.assertEqual(ergebnis.identitaet.discord_id, DISCORD_ID)

    def test_geheimnis_erscheint_nicht_in_fehlermeldungen(self):
        self.netz.setzen(oauth.TOKEN_URL, http_fehler(
            400, {"error": "seltsam", "error_description": f"secret={SECRET} war falsch"}))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertNotIn(SECRET, fall.exception.message)
        self.assertIn("<geheim>", fall.exception.message)

    def test_riesige_antwort_wird_abgelehnt(self):
        gross = b'{"id": "' + b"1" * (oauth.MAX_ANTWORT_BYTES + 10) + b'"}'
        self.netz.setzen(oauth.IDENTITY_URL, FakeAntwort(gross))
        begonnen = oauth.start(oauth.FLOW_ADMIN, now=T0)
        with self.assertRaises(oauth.DiscordFehler) as fall:
            oauth.complete(begonnen["state"], "anmeldecode123", now=T0 + 1)
        self.assertIn("viele Daten", fall.exception.message)


# --------------------------------------------------------------------------- 6. Konten

class TestKonto(Basis):

    def test_erster_login_wird_betreiber(self):
        konto = self.betreiber()
        self.assertTrue(konto.neu)
        self.assertTrue(konto.admin_vergeben)
        self.assertEqual(konto.user["role"], "admin")
        self.assertEqual(konto.user["name"], "Luca")
        self.assertEqual(konto.user["discord_id"], DISCORD_ID)

    def test_zweiter_braucht_einladungscode(self):
        self.betreiber()
        fremd = self.identitaet(discord_id="998877665544332211", benutzername="fremd",
                                anzeigename="Fremd")
        with self.assertRaises(oauth.EinladungNoetig) as fall:
            oauth.konto_fuer(fremd, now=T0 + 10)
        self.assertIn("Einladungscode", fall.exception.message)
        self.assertEqual(fall.exception.status, 403)
        self.assertEqual(len(users.all_users()), 1)

    def test_zweiter_mit_einladungscode(self):
        chef = self.betreiber()
        einladung = users.create_invite(chef.user["id"], now=T0)
        fremd = self.identitaet(discord_id="998877665544332211", benutzername="fremd",
                                anzeigename="Fremd")
        konto = oauth.konto_fuer(fremd, invite_code=users.format_code(einladung["code"]),
                                 now=T0 + 10)
        self.assertTrue(konto.neu)
        self.assertFalse(konto.admin_vergeben)
        self.assertEqual(konto.user["role"], "user")
        self.assertEqual(konto.user["name"], "Fremd")
        self.assertEqual(users.invite_state(users.get_invite(einladung["code"]), now=T0 + 11),
                         "used")

    def test_verbrauchter_code_meldet_verstaendlich(self):
        chef = self.betreiber()
        einladung = users.create_invite(chef.user["id"], days=1, now=T0)
        fremd = self.identitaet(discord_id="998877665544332211", anzeigename="Fremd")
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.konto_fuer(fremd, invite_code=einladung["code"], now=T0 + 3 * 86400)
        self.assertIn("abgelaufen", fall.exception.message)

    def test_bekanntes_discord_konto_wird_wiedererkannt(self):
        erst = self.betreiber()
        users.rename_user(erst.user["id"], "Luca P")
        wieder = oauth.konto_fuer(self.identitaet(anzeigename="Neuer Name"), now=T0 + 100)
        self.assertFalse(wieder.neu)
        self.assertEqual(wieder.user["id"], erst.user["id"])
        self.assertEqual(wieder.user["name"], "Luca P")        # wir überschreiben nichts
        self.assertEqual(len(users.all_users()), 1)

    def test_gesperrtes_konto(self):
        konto = self.betreiber()
        users.create_user("Zweiter Admin", role="admin", now=T0)
        users.set_blocked(konto.user["id"], True, "Zahlung offen")
        with self.assertRaises(oauth.KontoGesperrt) as fall:
            oauth.konto_fuer(self.identitaet(), now=T0 + 10)
        self.assertIn("gesperrt", fall.exception.message)
        self.assertIn("Zahlung offen", fall.exception.message)

    def test_kein_admin_aber_fremde_konten_vorhanden(self):
        users.create_user("Schon da", role="user", now=T0)
        fremd = self.identitaet()
        with self.assertRaises(oauth.EinladungNoetig):
            oauth.konto_fuer(fremd, now=T0 + 5)
        self.assertFalse(oauth.erster_admin_faellig())

    def test_namen_kollision_und_seltsame_zeichen(self):
        chef = self.betreiber(self.identitaet(discord_id="111000111000111000",
                              anzeigename="Chef", benutzername="chef"))
        users.create_user("Luca", now=T0)                      # Name schon belegt
        einladung = users.create_invite(chef.user["id"], uses_max=5, now=T0)

        konto = oauth.konto_fuer(self.identitaet(), invite_code=einladung["code"], now=T0 + 1)
        self.assertNotEqual(konto.user["name"], "Luca")
        self.assertEqual(konto.user["name"], "lucap")           # zweiter Vorschlag

        # Nur Zeichen, die im Anzeigenamen nicht erlaubt sind: dann ein sprechender Ersatz.
        drei = self.identitaet(discord_id="111222333444555666",
                               anzeigename="🔥🔥🔥", benutzername="🔥")
        konto3 = oauth.konto_fuer(drei, invite_code=einladung["code"], now=T0 + 2)
        self.assertEqual(konto3.user["name"], "Spieler 5666")

        # Gleicher Discord-Anzeigename, aber schon zweimal belegt: durchnummerieren.
        vier = self.identitaet(discord_id="111222333444555777", anzeigename="Luca",
                               benutzername="Luca")
        konto4 = oauth.konto_fuer(vier, invite_code=einladung["code"], now=T0 + 3)
        self.assertEqual(konto4.user["name"], "Luca 2")

    def test_erstanmeldung_als_admin_holt_rolle_nach(self):
        konto = self.betreiber()
        users.set_role(konto.user["id"], "admin")
        # Rolle künstlich verlieren (z. B. von Hand in der Datei geändert)
        store_hosted.patch_by("users", "id", konto.user["id"], {"role": "user"})
        user, vergeben = oauth.erstanmeldung_als_admin(konto.user["id"], now=T0 + 1)
        self.assertTrue(vergeben)
        self.assertEqual(user["role"], "admin")
        user, vergeben = oauth.erstanmeldung_als_admin(konto.user["id"], now=T0 + 2)
        self.assertFalse(vergeben)

    def test_erstanmeldung_unbekanntes_konto(self):
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.erstanmeldung_als_admin("gibtsnicht", now=T0)
        self.assertEqual(fall.exception.status, 404)

    def test_anmelden_liefert_gueltiges_sitzungstoken(self):
        _, ergebnis = self.anmeldung_durchlaufen(invite_code=self.erstzugang())
        anmeldung = oauth.anmelden(ergebnis, note="Programm auf dem PC", now=T0 + 6)
        self.assertTrue(anmeldung.admin_vergeben)
        self.assertEqual(users.user_for_token(anmeldung.token, now=T0 + 7)["id"],
                         anmeldung.user["id"])
        self.assertEqual(anmeldung.expires_at, T0 + 6 + users.SESSION_DAYS_DEFAULT * 86400)
        antwort = anmeldung.as_dict()
        self.assertEqual(antwort["user"]["role"], "admin")
        self.assertNotIn("discord_id", antwort["user"])
        self.assertTrue(antwort["user"]["discord_linked"])

    def test_anmelden_mit_identitaet_und_code(self):
        chef = self.betreiber()
        einladung = users.create_invite(chef.user["id"], now=T0)
        fremd = self.identitaet(discord_id="998877665544332211", anzeigename="Fremd")
        anmeldung = oauth.anmelden(fremd, invite_code=einladung["code"], now=T0 + 5)
        self.assertFalse(anmeldung.admin_vergeben)
        self.assertEqual(anmeldung.user["role"], "user")
        self.assertTrue(anmeldung.neu)

    def test_verknuepfen(self):
        chef = self.betreiber()
        zweiter = users.create_user("Ohne Discord", now=T0)
        with self.assertRaises(oauth.OAuthFehler) as fall:
            oauth.verknuepfen(zweiter["id"], self.identitaet())
        self.assertEqual(fall.exception.status, 409)
        frisch = self.identitaet(discord_id="998877665544332211")
        user = oauth.verknuepfen(zweiter["id"], frisch)
        self.assertEqual(user["discord_id"], "998877665544332211")
        self.assertEqual(chef.user["discord_id"], DISCORD_ID)

    def test_kennung_wird_geprueft(self):
        with self.assertRaises(oauth.DiscordFehler):
            oauth.konto_fuer(self.identitaet(discord_id="keine-zahl"), now=T0)


# --------------------------------------------------------------------------- 7. Abholcodes

class TestAbholcodes(Basis):

    def test_anlegen_und_genau_einmal_einloesen(self):
        code, ablauf = oauth.abholcode_anlegen({"token": "geheim", "user": "abc"}, now=T0)
        self.assertEqual(len(code), oauth.ABHOL_LEN)
        self.assertEqual(ablauf, T0 + oauth.ABHOL_TTL_SECONDS)
        self.assertEqual(oauth.abholungen_offen(now=T0), 1)
        inhalt = oauth.abholcode_einloesen(oauth.abholcode_formatieren(code), now=T0 + 5)
        self.assertEqual(inhalt["token"], "geheim")
        with self.assertRaises(oauth.OAuthZustandFehler):
            oauth.abholcode_einloesen(code, now=T0 + 6)

    def test_kleinschreibung_und_trennzeichen(self):
        code, _ = oauth.abholcode_anlegen({"token": "x"}, now=T0)
        bunt = " " + oauth.abholcode_formatieren(code).lower().replace("-", " ") + " "
        self.assertEqual(oauth.abholcode_einloesen(bunt, now=T0 + 1)["token"], "x")

    def test_abgelaufen(self):
        code, _ = oauth.abholcode_anlegen({"token": "x"}, now=T0)
        with self.assertRaises(oauth.OAuthZustandFehler) as fall:
            oauth.abholcode_einloesen(code, now=T0 + oauth.ABHOL_TTL_SECONDS + 1)
        self.assertIn("abgelaufen", fall.exception.message)

    def test_unsinniger_code(self):
        for schlecht in ("", "ABC", "ABCD-EFGH-JKL0", "x" * 40):
            with self.assertRaises(oauth.OAuthFehler):
                oauth.abholcode_einloesen(schlecht, now=T0)

    def test_leerer_inhalt(self):
        with self.assertRaises(oauth.OAuthFehler):
            oauth.abholcode_anlegen({}, now=T0)

    def test_geheimnis_des_programms(self):
        import hashlib
        geheimnis = "abholgeheimnis-des-programms"
        merkmal = hashlib.sha256(geheimnis.encode("utf-8")).hexdigest()
        code, _ = oauth.abholcode_anlegen({"token": "x"}, claim_hash=merkmal, now=T0)
        with self.assertRaises(oauth.OAuthZustandFehler) as fall:
            oauth.abholcode_einloesen(code, geheimnis="falsch", now=T0 + 1)
        self.assertEqual(fall.exception.status, 403)
        self.assertEqual(oauth.abholcode_einloesen(code, geheimnis=geheimnis,
                                                   now=T0 + 2)["token"], "x")

    def test_zu_viele_fehlversuche_werfen_alles_weg(self):
        code, _ = oauth.abholcode_anlegen({"token": "x"}, now=T0)
        for i in range(oauth.ABHOL_FEHLVERSUCHE_MAX):
            with self.assertRaises(oauth.OAuthZustandFehler):
                oauth.abholcode_einloesen("QQQQ-QQQQ-QQQQ", now=T0 + i)
        with self.assertRaises(oauth.OAuthZustandFehler) as fall:
            oauth.abholcode_einloesen(code, now=T0 + 20)
        self.assertEqual(fall.exception.status, 429)
        self.assertIn("zu viele", fall.exception.message.lower())
        self.assertEqual(oauth.abholungen_offen(now=T0 + 20), 0)

    def test_aufraeumen_und_obergrenze(self):
        for _ in range(oauth.ABHOL_MAX + 5):
            oauth.abholcode_anlegen({"token": "x"}, now=T0)
        self.assertLessEqual(oauth.abholungen_offen(now=T0), oauth.ABHOL_MAX)
        self.assertGreaterEqual(oauth.abholcodes_aufraeumen(now=T0 + oauth.ABHOL_TTL_SECONDS + 1), 1)
        self.assertEqual(oauth.abholungen_offen(now=T0), 0)

    def test_alles_leeren(self):
        oauth.start(oauth.FLOW_ADMIN, now=T0)
        oauth.abholcode_anlegen({"token": "x"}, now=T0)
        oauth.alles_leeren()
        self.assertEqual(oauth.zustaende_offen(now=T0), 0)
        self.assertEqual(oauth.abholungen_offen(now=T0), 0)


if __name__ == "__main__":
    unittest.main()
