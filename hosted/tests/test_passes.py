"""Selbsttests für Ablage, Konten, Einladungen, Sitzungen und Pässe.

Läuft ohne Server und ohne Fremdpakete: alle Daten landen in einem Temp-Ordner (MCSM_DATA),
die Zeit wird über den Parameter ``now`` gesteuert.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys
import tempfile
import types
import unittest

HOSTED = pathlib.Path(__file__).resolve().parent.parent
PKG = "mcsm_hosted"


def _module(name: str):
    """Modul aus hosted/core laden.

    Bewusst unter eigenem Paketnamen: Im Projektwurzelverzeichnis liegt ein gleichnamiger Ordner
    core/ (das Programm für den PC), der sonst zuerst gefunden würde.
    """
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(HOSTED / "core")]        # type: ignore[attr-defined]
        pkg.__package__ = PKG
        sys.modules[PKG] = pkg
    return importlib.import_module(f"{PKG}.{name}")


store_hosted = _module("store_hosted")
users = _module("users")
passes = _module("passes")

T0 = 1_700_000_000          # feste Startzeit für alle Tests
TAG = 86400


class Basis(unittest.TestCase):
    """Legt für jeden Test einen frischen Datenordner an."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._alt = os.environ.get("MCSM_DATA")
        os.environ["MCSM_DATA"] = self._tmp.name
        store_hosted.ensure_dir()

    def tearDown(self) -> None:
        if self._alt is None:
            os.environ.pop("MCSM_DATA", None)
        else:
            os.environ["MCSM_DATA"] = self._alt
        self._tmp.cleanup()

    def konto(self, name: str = "Luca", **kw) -> dict:
        return users.create_user(name, now=T0, **kw)


# ----------------------------------------------------------------------- Ablage

class TestAblage(Basis):

    def test_speichern_und_laden(self):
        self.assertEqual(store_hosted.load("users"), [])
        store_hosted.save("users", [{"id": "a", "name": "Test"}])
        self.assertEqual(store_hosted.load("users"), [{"id": "a", "name": "Test"}])
        self.assertTrue((pathlib.Path(self._tmp.name) / "users.json").exists())

    def test_temp_datei_bleibt_nicht_liegen(self):
        store_hosted.save("passes", [{"id": "x"}])
        rest = list(pathlib.Path(self._tmp.name).glob("*.tmp"))
        self.assertEqual(rest, [])

    def test_unbekannte_datenart(self):
        with self.assertRaises(ValueError) as fehler:
            store_hosted.path_of("geheim")
        self.assertIn("Unbekannte Datenart", str(fehler.exception))

    def test_kaputte_datei_wird_nicht_verschwiegen(self):
        (pathlib.Path(self._tmp.name) / "users.json").write_text("{kaputt", encoding="utf-8")
        with self.assertRaises(ValueError) as fehler:
            store_hosted.load("users")
        self.assertIn("beschädigt", str(fehler.exception))

    def test_update_schreibt_bei_fehler_nichts(self):
        store_hosted.save("users", [{"id": "a"}])

        def kaputt(records):
            records.append({"id": "b"})
            raise ValueError("Absicht")

        with self.assertRaises(ValueError):
            store_hosted.update("users", kaputt)
        self.assertEqual([r["id"] for r in store_hosted.load("users")], ["a"])

    def test_datenordner_aus_umgebung(self):
        self.assertEqual(store_hosted.data_dir(), pathlib.Path(self._tmp.name))
        os.environ.pop("MCSM_DATA")
        self.assertEqual(store_hosted.data_dir(), pathlib.Path(store_hosted.DEFAULT_DATA_DIR))
        os.environ["MCSM_DATA"] = self._tmp.name


# ----------------------------------------------------------------------- Konten

class TestKonten(Basis):

    def test_anlegen_und_finden(self):
        konto = self.konto("Luca")
        self.assertEqual(konto["role"], "user")
        self.assertFalse(konto["blocked"])
        self.assertEqual(users.get_user(konto["id"])["name"], "Luca")
        self.assertEqual(users.find_by_name("luca")["id"], konto["id"])

    def test_name_doppelt(self):
        self.konto("Luca")
        with self.assertRaises(ValueError) as fehler:
            self.konto("luca")
        self.assertIn("gibt es schon", str(fehler.exception))

    def test_name_ungueltig(self):
        for schlecht in ("", "A", "x" * 33, "böse\x00Zeile", "Halt<script>", "Pfad/weg"):
            with self.assertRaises(ValueError):
                users.clean_name(schlecht)
        self.assertEqual(users.clean_name("  Jörg   Müller "), "Jörg Müller")
        # Zeilenumbrüche und Tabulatoren werden zu einem Leerzeichen, nicht abgelehnt.
        self.assertEqual(users.clean_name("Jörg\tMüller\n"), "Jörg Müller")

    def test_discord_kennung(self):
        self.assertEqual(users.clean_discord_id(" 123456789012345678 "), "123456789012345678")
        self.assertEqual(users.clean_discord_id(""), "")
        with self.assertRaises(ValueError):
            users.clean_discord_id("abc")

    def test_discord_doppelt(self):
        self.konto("Luca", discord_id="123456789012345678")
        with self.assertRaises(ValueError):
            self.konto("Tim", discord_id="123456789012345678")

    def test_letzter_admin_bleibt(self):
        admin = self.konto("Chef", role="admin")
        with self.assertRaises(ValueError):
            users.set_role(admin["id"], "user")
        with self.assertRaises(ValueError):
            users.set_blocked(admin["id"], True)
        with self.assertRaises(ValueError):
            users.delete_user(admin["id"])
        users.create_user("Zweiter", role="admin", now=T0)
        self.assertEqual(users.set_role(admin["id"], "user")["role"], "user")

    def test_sperren_beendet_sitzungen(self):
        konto = self.konto()
        token, _ = users.create_session(konto["id"], now=T0)
        self.assertIsNotNone(users.user_for_token(token, now=T0))
        users.set_blocked(konto["id"], True, "Zahlung offen")
        self.assertIsNone(users.user_for_token(token, now=T0))
        self.assertEqual(users.get_user(konto["id"])["blocked_reason"], "Zahlung offen")

    def test_oeffentliches_konto_zeigt_keine_discord_id(self):
        konto = self.konto("Luca", discord_id="123456789012345678")
        offen = users.public_user(konto)
        self.assertTrue(offen["discord_linked"])
        self.assertNotIn("discord_id", offen)


# ----------------------------------------------------------------------- Einladungen

class TestEinladungen(Basis):

    def test_code_form(self):
        code = users.new_code()
        self.assertEqual(len(code), 12)
        self.assertEqual(users.format_code(code).count("-"), 2)
        self.assertEqual(users.normalize_code(users.format_code(code).lower()), code)
        with self.assertRaises(ValueError):
            users.normalize_code("zu kurz")

    def test_einloesen(self):
        admin = self.konto("Chef", role="admin")
        einladung = users.create_invite(admin["id"], uses_max=2, days=7, now=T0)
        konto = users.redeem_invite(users.format_code(einladung["code"]), "Tim", now=T0)
        self.assertEqual(konto["name"], "Tim")
        self.assertEqual(konto["role"], "user")
        frisch = users.get_invite(einladung["code"])
        self.assertEqual(len(frisch["redeemed_by"]), 1)
        self.assertEqual(users.invite_state(frisch, now=T0), "open")
        users.redeem_invite(einladung["code"], "Anna", now=T0)
        self.assertEqual(users.invite_state_text(users.get_invite(einladung["code"]), now=T0),
                         "aufgebraucht")
        with self.assertRaises(ValueError) as fehler:
            users.redeem_invite(einladung["code"], "Noch Einer", now=T0)
        self.assertIn("aufgebraucht", str(fehler.exception))

    def test_abgelaufen_und_widerrufen(self):
        admin = self.konto("Chef", role="admin")
        alt = users.create_invite(admin["id"], days=1, now=T0)
        with self.assertRaises(ValueError) as fehler:
            users.redeem_invite(alt["code"], "Tim", now=T0 + TAG)
        self.assertIn("abgelaufen", str(fehler.exception))

        weg = users.create_invite(admin["id"], days=7, now=T0)
        users.revoke_invite(weg["code"], now=T0)
        with self.assertRaises(ValueError) as fehler:
            users.redeem_invite(weg["code"], "Tim", now=T0)
        self.assertIn("zurückgezogen", str(fehler.exception))

    def test_unbekannter_code(self):
        with self.assertRaises(ValueError) as fehler:
            users.redeem_invite("ABCD-EFGH-JKLM", "Tim", now=T0)
        self.assertIn("unbekannt", str(fehler.exception))

    def test_kein_halbes_konto_bei_schlechtem_namen(self):
        admin = self.konto("Chef", role="admin")
        einladung = users.create_invite(admin["id"], now=T0)
        with self.assertRaises(ValueError):
            users.redeem_invite(einladung["code"], "x", now=T0)
        self.assertEqual(len(users.all_users()), 1)
        self.assertEqual(users.get_invite(einladung["code"])["redeemed_by"], [])

    def test_discord_ohne_einladung(self):
        with self.assertRaises(ValueError) as fehler:
            users.user_for_discord("123456789012345678", "Tim", now=T0)
        self.assertIn("Einladungscode", str(fehler.exception))

    def test_discord_mit_einladung_und_danach_wiedererkannt(self):
        admin = self.konto("Chef", role="admin")
        einladung = users.create_invite(admin["id"], now=T0)
        konto = users.user_for_discord("123456789012345678", "Tim",
                                       invite_code=einladung["code"], now=T0)
        wieder = users.user_for_discord("123456789012345678", "Tim", now=T0)
        self.assertEqual(wieder["id"], konto["id"])


# ----------------------------------------------------------------------- Sitzungen

class TestSitzungen(Basis):

    def test_token_wird_nur_als_hash_gespeichert(self):
        konto = self.konto()
        token, sitzung = users.create_session(konto["id"], now=T0)
        self.assertNotIn("token", sitzung)
        inhalt = (pathlib.Path(self._tmp.name) / "sessions.json").read_text(encoding="utf-8")
        self.assertNotIn(token, inhalt)
        self.assertIn(users.token_hash(token), inhalt)

    def test_gueltigkeit(self):
        konto = self.konto()
        token, _ = users.create_session(konto["id"], days=2, now=T0)
        self.assertEqual(users.user_for_token(token, now=T0)["id"], konto["id"])
        self.assertIsNone(users.user_for_token(token, now=T0 + 2 * TAG))
        self.assertIsNone(users.user_for_token("falsches-token", now=T0))
        self.assertIsNone(users.user_for_token("", now=T0))

    def test_abmelden(self):
        konto = self.konto()
        token, _ = users.create_session(konto["id"], now=T0)
        self.assertTrue(users.revoke_session(token, now=T0))
        self.assertIsNone(users.user_for_token(token, now=T0))
        self.assertFalse(users.revoke_session("nichts", now=T0))

    def test_sitzungen_auflisten_ohne_hash(self):
        konto = self.konto()
        users.create_session(konto["id"], note="PC von Luca", now=T0)
        liste = users.sessions_of(konto["id"], now=T0)
        self.assertEqual(len(liste), 1)
        self.assertNotIn("token_hash", liste[0])
        self.assertEqual(liste[0]["note"], "PC von Luca")

    def test_aufraeumen(self):
        konto = self.konto()
        users.create_session(konto["id"], days=1, now=T0)
        self.assertEqual(users.purge_sessions(now=T0 + TAG, keep_days=0), 1)
        self.assertEqual(users.all_sessions(), [])

    def test_gesperrtes_konto_bekommt_keine_sitzung(self):
        konto = self.konto()
        users.set_blocked(konto["id"], True)
        with self.assertRaises(ValueError):
            users.create_session(konto["id"], now=T0)


# ----------------------------------------------------------------------- Pässe

class TestPaesse(Basis):

    def test_ausstellen_und_lesen(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 30, 2, 8192, issued_by="admin", now=T0)
        self.assertEqual(pas["expires_at"], T0 + 30 * TAG)
        self.assertEqual(pas["revoked_at"], 0)
        self.assertTrue(passes.is_active(pas, now=T0))
        self.assertEqual(passes.pass_state(pas, now=T0), "active")
        self.assertEqual([p["id"] for p in passes.passes_of(konto["id"])], [pas["id"]])

    def test_grenzen_werden_geprueft(self):
        konto = self.konto()
        for art, tage, anzahl, ram in (
            ("gold", 30, 2, 8192),          # unbekannte Art
            ("local", 0, 2, 8192),          # Laufzeit zu klein
            ("local", 4000, 2, 8192),       # Laufzeit zu groß
            ("local", 30, 0, 8192),         # keine Server
            ("local", 30, 99, 8192),        # zu viele Server
            ("local", 30, 2, 512),          # zu wenig RAM
            ("local", 30, 2, 999999),       # mehr RAM als die Maschine hat
            ("local", 30, 2, "viel"),       # keine Zahl
        ):
            with self.assertRaises(ValueError, msg=f"{art}/{tage}/{anzahl}/{ram}"):
                passes.issue_pass(konto["id"], art, tage, anzahl, ram, now=T0)

    def test_pass_fuer_unbekanntes_konto(self):
        with self.assertRaises(ValueError) as fehler:
            passes.issue_pass("gibtsnicht", "local", 7, 1, 4096, now=T0)
        self.assertIn("kein Konto", str(fehler.exception))

    def test_ablauf_genau_auf_der_grenze(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 1, 1, 4096, now=T0)
        self.assertTrue(passes.is_active(pas, now=T0 + TAG - 1))
        self.assertFalse(passes.is_active(pas, now=T0 + TAG))
        self.assertEqual(passes.pass_state(pas, now=T0 + TAG), "expired")

    def test_widerrufen(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 30, 2, 8192, now=T0)
        passes.revoke_pass(pas["id"], now=T0 + 60)
        frisch = passes.get_pass(pas["id"])
        self.assertEqual(passes.pass_state(frisch, now=T0 + 60), "revoked")
        self.assertEqual(passes.remaining_seconds(frisch, now=T0 + 60), 0)
        self.assertEqual(passes.remaining_text(frisch, now=T0 + 60), "zurückgezogen")
        self.assertEqual(passes.active_passes(konto["id"], now=T0 + 60), [])

    def test_verlaengern(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 7, 1, 4096, now=T0)
        laenger = passes.extend_pass(pas["id"], 7, now=T0 + TAG)
        self.assertEqual(laenger["expires_at"], T0 + 14 * TAG)
        self.assertEqual(laenger["days"], 14)
        # Ein abgelaufener Pass läuft ab jetzt weiter, nicht ab dem alten Ende.
        spaeter = passes.extend_pass(pas["id"], 1, now=T0 + 30 * TAG)
        self.assertEqual(spaeter["expires_at"], T0 + 31 * TAG)

    def test_widerrufener_pass_nicht_verlaengerbar(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 7, 1, 4096, now=T0)
        passes.revoke_pass(pas["id"], now=T0)
        with self.assertRaises(ValueError) as fehler:
            passes.extend_pass(pas["id"], 7, now=T0)
        self.assertIn("zurückgezogen", str(fehler.exception))

    def test_grenzen_nachtraeglich_aendern(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 7, 1, 4096, now=T0)
        neu = passes.set_limits(pas["id"], max_concurrent=3, ram_total_mb=6144)
        self.assertEqual((neu["max_concurrent"], neu["ram_total_mb"]), (3, 6144))
        with self.assertRaises(ValueError):
            passes.set_limits(pas["id"], ram_total_mb=99999)

    def test_wirksame_grenzen_sind_das_maximum_nicht_die_summe(self):
        konto = self.konto()
        passes.issue_pass(konto["id"], "local", 30, 1, 4096, now=T0)
        passes.issue_pass(konto["id"], "local", 30, 2, 6144, now=T0)
        grenzen = passes.effective_limits(konto["id"], now=T0)
        self.assertEqual(grenzen["max_concurrent"], 2)
        self.assertEqual(grenzen["ram_total_mb"], 6144)
        self.assertEqual(grenzen["pass_count"], 2)
        self.assertFalse(grenzen["premium"])

    def test_wirksame_grenzen_ignorieren_abgelaufene_und_widerrufene(self):
        konto = self.konto()
        kurz = passes.issue_pass(konto["id"], "local", 1, 4, 10240, now=T0)
        passes.issue_pass(konto["id"], "local", 30, 1, 4096, now=T0)
        weg = passes.issue_pass(konto["id"], "premium", 30, 3, 8192, now=T0)
        passes.revoke_pass(weg["id"], now=T0)
        grenzen = passes.effective_limits(konto["id"], now=T0 + 2 * TAG)
        self.assertEqual(grenzen["max_concurrent"], 1)
        self.assertEqual(grenzen["ram_total_mb"], 4096)
        self.assertFalse(grenzen["premium"])
        self.assertEqual(passes.pass_state(passes.get_pass(kurz["id"]), now=T0 + 2 * TAG), "expired")

    def test_ohne_pass_sind_alle_grenzen_null(self):
        konto = self.konto()
        grenzen = passes.effective_limits(konto["id"], now=T0)
        self.assertEqual(grenzen["max_concurrent"], 0)
        self.assertEqual(grenzen["ram_total_mb"], 0)
        self.assertEqual(grenzen["pass_ids"], [])
        self.assertEqual(passes.limits_text(grenzen), "kein gültiger Pass")

    def test_grenzen_zeiten(self):
        konto = self.konto()
        passes.issue_pass(konto["id"], "local", 1, 1, 4096, now=T0)
        passes.issue_pass(konto["id"], "local", 30, 2, 8192, now=T0)
        grenzen = passes.effective_limits(konto["id"], now=T0)
        self.assertEqual(grenzen["next_expiry"], T0 + TAG)
        self.assertEqual(grenzen["expires_at"], T0 + 30 * TAG)

    def test_premium_deckt_beides_ab(self):
        lokal = {"kind": "local"}
        premium = {"kind": "premium"}
        self.assertTrue(passes.allows_origin(lokal, "local"))
        self.assertFalse(passes.allows_origin(lokal, "premium"))
        self.assertTrue(passes.allows_origin(premium, "local"))
        self.assertTrue(passes.allows_origin(premium, "premium"))

    def test_premium_erkennen(self):
        konto = self.konto()
        passes.issue_pass(konto["id"], "local", 30, 1, 4096, now=T0)
        self.assertFalse(passes.has_premium(konto["id"], now=T0))
        passes.issue_pass(konto["id"], "premium", 30, 1, 4096, now=T0)
        self.assertTrue(passes.has_premium(konto["id"], now=T0))
        self.assertTrue(passes.effective_limits(konto["id"], now=T0)["premium"])
        self.assertEqual(len(passes.active_passes(konto["id"], now=T0, kind="premium")), 1)

    def test_bald_ablaufende_paesse(self):
        konto = self.konto()
        gleich = passes.issue_pass(konto["id"], "local", 1, 1, 4096, now=T0)
        spaeter = passes.issue_pass(konto["id"], "local", 30, 1, 4096, now=T0)
        jetzt = T0 + TAG - 600                            # genau 10 Minuten vor dem Ende
        ids = [p["id"] for p in passes.expiring_soon(10, now=jetzt)]
        self.assertEqual(ids, [gleich["id"]])
        self.assertEqual(passes.expiring_soon(9, now=jetzt), [])
        # Bereits abgelaufen zählt nicht mehr (die Warnung kommt vorher).
        self.assertEqual(passes.expiring_soon(10, now=T0 + TAG), [])
        # Widerrufene Pässe brauchen keine Vorwarnung.
        passes.revoke_pass(spaeter["id"], now=T0)
        self.assertEqual(passes.expiring_soon(60, now=T0 + 30 * TAG - 60), [])

    def test_bald_ablaufende_sortierung_und_pruefung(self):
        konto = self.konto()
        spaet = passes.issue_pass(konto["id"], "local", 2, 1, 4096, now=T0)
        frueh = passes.issue_pass(konto["id"], "local", 1, 1, 4096, now=T0)
        ids = [p["id"] for p in passes.expiring_soon(2 * 1440, now=T0)]
        self.assertEqual(ids, [frueh["id"], spaet["id"]])
        with self.assertRaises(ValueError):
            passes.expiring_soon(-1, now=T0)

    def test_gerade_abgelaufene(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 1, 1, 4096, now=T0)
        self.assertEqual(passes.newly_expired(T0, now=T0 + TAG - 1), [])
        self.assertEqual([p["id"] for p in passes.newly_expired(T0, now=T0 + TAG)], [pas["id"]])
        weg = passes.issue_pass(konto["id"], "local", 30, 1, 4096, now=T0)
        passes.revoke_pass(weg["id"], now=T0 + 100)
        ids = [p["id"] for p in passes.newly_expired(T0 + 99, now=T0 + 101)]
        self.assertEqual(ids, [weg["id"]])

    def test_restzeit_in_deutsch(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 30, 2, 8192, now=T0)
        self.assertEqual(passes.remaining_text(pas, now=T0), "läuft in 30 Tagen ab")
        self.assertEqual(passes.remaining_text(pas, now=T0 + 29 * TAG), "läuft in 1 Tag ab")
        self.assertEqual(passes.remaining_text(pas, now=T0 + 30 * TAG - 3600),
                         "läuft in 1 Stunde ab")
        self.assertEqual(passes.remaining_text(pas, now=T0 + 30 * TAG - 600),
                         "läuft in 10 Minuten ab")
        self.assertEqual(passes.remaining_text(pas, now=T0 + 30 * TAG - 30),
                         "läuft in weniger als einer Minute ab")
        self.assertEqual(passes.remaining_text(pas, now=T0 + 30 * TAG), "abgelaufen")

    def test_dauer_und_gb_texte(self):
        self.assertEqual(passes.duration_text(2 * TAG + 3 * 3600), "2 Tagen 3 Stunden")
        self.assertEqual(passes.duration_text(3660), "1 Stunde 1 Minute")
        self.assertEqual(passes.duration_text(0), "weniger als einer Minute")
        self.assertEqual(passes.gb_text(8192), "8 GB")
        self.assertEqual(passes.gb_text(1536), "1,5 GB")
        self.assertEqual(passes.gb_text(512), "512 MB")
        self.assertEqual(passes.gb_text(None), "0 MB")

    def test_pass_fuer_die_api(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "premium", 7, 2, 8192, now=T0)
        offen = passes.public_pass(pas, now=T0)
        self.assertEqual(offen["kind_text"], "Premium-Pass")
        self.assertEqual(offen["ram_total_text"], "8 GB")
        self.assertEqual(offen["state"], "active")
        self.assertEqual(offen["remaining_text"], "läuft in 7 Tagen ab")

    def test_grenzen_als_text(self):
        konto = self.konto()
        passes.issue_pass(konto["id"], "local", 7, 1, 4096, now=T0)
        self.assertEqual(passes.limits_text(passes.effective_limits(konto["id"], now=T0)),
                         "1 gleichzeitig laufender Server, zusammen 4 GB")
        passes.issue_pass(konto["id"], "local", 7, 3, 9216, now=T0)
        self.assertEqual(passes.limits_text(passes.effective_limits(konto["id"], now=T0)),
                         "3 gleichzeitig laufende Server, zusammen 9 GB")

    def test_pass_loeschen(self):
        konto = self.konto()
        pas = passes.issue_pass(konto["id"], "local", 7, 1, 4096, now=T0)
        passes.delete_pass(pas["id"])
        self.assertIsNone(passes.get_pass(pas["id"]))
        with self.assertRaises(ValueError):
            passes.delete_pass(pas["id"])


if __name__ == "__main__":
    unittest.main()
