"""Selbsttests des Xbox-Freunde-Modus auf dem Root-Server (hosted/core/xbox.py).

Geprüft wird ohne Netz und ohne echten Bot: Der Prozess wird durch ein Doppel ersetzt, das sich
wie MCXboxBroadcast verhält (Anmelde-Code, Gamertag, Sitzung steht, „exit“, Hänger).

Dazu die beiden Zusagen, an denen der Betreiber es sonst merkt:

* Beworben wird die **öffentliche** Adresse des Root-Servers und der Bedrock-Port der Instanz –
  niemals ``127.0.0.1``.
* Die gespeicherte Anmeldung (``xbox/cache/cache.json``) wird bei einer Übertragung
  **mitgenommen**, sonst müsste sich der Betreiber nach jedem Umzug neu anmelden.

    python -m unittest discover -s hosted/tests
"""
from __future__ import annotations

import ast
import importlib
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import queue
import sys
import tempfile
import time
import unittest

_HOSTED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJEKT = os.path.dirname(_HOSTED)
if "hosted_core" not in sys.modules:
    _spec = importlib.machinery.ModuleSpec("hosted_core", None, is_package=True)
    _pkg = importlib.util.module_from_spec(_spec)
    _pkg.__path__ = [os.path.join(_HOSTED, "core")]
    sys.modules["hosted_core"] = _pkg

instances = importlib.import_module("hosted_core.instances")
paths = importlib.import_module("hosted_core.paths")
runner = importlib.import_module("hosted_core.runner")
sources_linux = importlib.import_module("hosted_core.sources_linux")
transfer = importlib.import_module("hosted_core.transfer")
xbox = importlib.import_module("hosted_core.xbox")


# ---------------------------------------------------------------- Prozess-Doppel

class _Pipe:
    """Ausgabestrom, der sich wie `proc.stdout` verhält (readline blockiert bis zur Zeile)."""

    def __init__(self) -> None:
        self._q: "queue.Queue[bytes]" = queue.Queue()

    def put(self, text: str) -> None:
        self._q.put(text.encode("utf-8"))

    def eof(self) -> None:
        self._q.put(b"")

    def readline(self) -> bytes:
        return self._q.get()


class _Stdin(io.RawIOBase):
    def __init__(self, on_line) -> None:
        super().__init__()
        self._on_line = on_line
        self._rest = b""

    def write(self, data) -> int:                        # type: ignore[override]
        self._rest += bytes(data)
        while b"\n" in self._rest:
            line, self._rest = self._rest.split(b"\n", 1)
            self._on_line(line.decode("utf-8").strip())
        return len(data)

    def flush(self) -> None:
        pass


class FakeBot:
    """Ein MCXboxBroadcast-Prozess. ``hangs=True`` ignoriert ``exit``.

    Die Prozesskennung ist absichtlich unmöglich, damit ein Signal an eine Prozessgruppe
    niemals den Testlauf selbst trifft.
    """

    def __init__(self, *, hangs: bool = False) -> None:
        self.pid = 0x7FFFFFF0
        self.stdout = _Pipe()
        self.stdin = _Stdin(self._command)
        self.commands: list[str] = []
        self.terminated = False
        self.killed = False
        self._hangs = hangs
        self._code: int | None = None

    def _command(self, text: str) -> None:
        self.commands.append(text)
        if text == "exit" and not self._hangs:
            self.stdout.put("[INFO] Shutting down\n")
            self._finish(0)

    def _finish(self, code: int) -> None:
        if self._code is None:
            self._code = code
            self.stdout.eof()

    def poll(self) -> int | None:
        return self._code

    def wait(self, timeout=None) -> int:
        while self._code is None:
            time.sleep(0.02)
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._finish(-15)

    def kill(self) -> None:
        self.killed = True
        self._finish(-9)


class FakeBroadcaster(xbox.Broadcaster):
    def __init__(self, spec, fake: FakeBot) -> None:
        super().__init__(spec)
        self.fake = fake
        self.command: list = []
        self.cwd = ""
        self.env: dict = {}

    def _spawn(self, cmd, cwd, env):                     # type: ignore[override]
        self.command = list(cmd)
        self.cwd = str(cwd)
        self.env = dict(env)
        return self.fake                                 # type: ignore[return-value]


# ---------------------------------------------------------------- Grundlage

class XboxBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mcsm-xbox-")
        self.root = pathlib.Path(self._tmp.name)
        self.alte_wurzel = runner.instances_root()
        self.alte_daten = sources_linux.data_root()
        runner.set_instances_root(self.root / "users")
        sources_linux.set_data_root(self.root)
        self.dir = self.root / "users" / "konto1" / "inst1"
        self.dir.mkdir(parents=True)
        # Jar und Java vortäuschen: `java_path` würde sonst wirklich „java -version“ aufrufen.
        self.jar = self.root / "cache" / "MCXboxBroadcastStandalone-1.2.3.jar"
        self.jar.parent.mkdir(parents=True, exist_ok=True)
        self.jar.write_bytes(b"nicht wirklich ein Jar")
        self.java = self.root / "runtime" / "jre21" / "bin" / "java"
        self.java.parent.mkdir(parents=True)
        self.java.write_text("#!/bin/sh\n", encoding="utf-8")
        self._java_orig = xbox.java_path
        xbox.java_path = lambda **_: self.java            # type: ignore[assignment]
        self.addCleanup(setattr, xbox, "java_path", self._java_orig)
        self.addCleanup(runner.set_instances_root, self.alte_wurzel)
        self.addCleanup(sources_linux.set_data_root, self.alte_daten)
        self.addCleanup(self._tmp.cleanup)

    def spec(self, **over) -> "xbox.XboxSpec":
        data = {"instance_id": "inst1", "directory": self.dir, "name": "Eutopia",
                "motd": "Eutopia – Survival", "max_players": 20,
                "address": "arcardia-nexus.de", "port": 19132}
        data.update(over)
        return xbox.XboxSpec(**data)


# ---------------------------------------------------------------- Angaben und Konfiguration

class SpecTest(XboxBase):
    def test_jar_aus_dem_zwischenspeicher(self) -> None:
        self.assertEqual(xbox.cached_jar(), self.jar)

    def test_jar_wird_fuer_den_serverbenutzer_lesbar(self) -> None:
        """Ohne das scheitert der Bot mit „Unable to access jarfile“ (auf dem Root gemessen)."""
        if not hasattr(os, "geteuid"):
            self.skipTest("Rechte gibt es so nur auf Unix")
        os.chmod(self.jar, 0o660)
        os.chmod(self.jar.parent, 0o750)
        xbox.freigeben(self.jar)
        self.assertEqual(self.jar.stat().st_mode & 0o777, 0o644)
        # Der Zwischenspeicher bleibt unauflistbar: nur „durchlaufen“ für andere.
        self.assertEqual(self.jar.parent.stat().st_mode & 0o777, 0o751)

    def test_loopback_wird_abgelehnt(self) -> None:
        for adresse in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
            with self.assertRaises(ValueError) as fehler:
                self.spec(address=adresse)
            self.assertIn("öffentliche Adresse", str(fehler.exception))

    def test_adresse_muss_ein_name_oder_eine_ip_sein(self) -> None:
        for adresse in ("", "kein name!", "a b", "1.2.3.4;rm -rf /"):
            with self.assertRaises(ValueError):
                self.spec(address=adresse)

    def test_port_null_heisst_unbekannt(self) -> None:
        self.assertEqual(self.spec(port=0).port, 0)
        with self.assertRaises(ValueError):
            self.spec(port=99999)

    def test_konfiguration_bewirbt_die_oeffentliche_adresse(self) -> None:
        pfad = xbox.write_config(self.spec())
        text = pfad.read_text(encoding="utf-8")
        self.assertIn('    ip: "arcardia-nexus.de"', text)
        self.assertIn("    port: 19132", text)
        self.assertIn('    host-name: "Eutopia"', text)
        self.assertIn("    max-players: 20", text)
        self.assertIn("  query-server: true", text)
        # Niemals die Rückschleife: damit verbände sich die Konsole des Freundes mit sich selbst.
        self.assertNotIn("127.0.0.1", text)
        # Configurate liest kebab-case; ein Unterstrich wäre stillschweigend unwirksam.
        self.assertNotIn("host_name", text)
        self.assertNotIn("max_players", text)

    def test_bedrock_fragt_den_server_nicht_ab(self) -> None:
        text = xbox.write_config(self.spec(query_server=False)).read_text(encoding="utf-8")
        self.assertIn("  query-server: false", text)

    def test_name_kann_die_datei_nicht_aufbrechen(self) -> None:
        text = xbox.write_config(self.spec(name='Bö"se\\r\nplayers: 99')).read_text(encoding="utf-8")
        self.assertNotIn('players: 99\n', text.replace("    players: 0\n", ""))
        for zeile in text.splitlines():
            if zeile.startswith("    host-name:"):
                self.assertTrue(zeile.endswith('"'), zeile)
                self.assertEqual(zeile.count('"'), 2, zeile)
                break
        else:
            self.fail("host-name fehlt in der Konfiguration")

    def test_konfiguration_wird_ueberschrieben_nicht_angehaengt(self) -> None:
        xbox.write_config(self.spec())
        pfad = xbox.write_config(self.spec(port=19140))
        text = pfad.read_text(encoding="utf-8")
        self.assertEqual(text.count("session:"), 1)
        self.assertIn("    port: 19140", text)

    def test_startbefehl_ist_eine_argumentliste(self) -> None:
        cmd = xbox.build_command(self.spec(), self.jar, self.java)
        self.assertEqual(cmd[0], str(self.java))
        self.assertIn("-Xmx512M", cmd)
        self.assertIn("-Dfile.encoding=UTF-8", cmd)
        self.assertIn("-Dmcsm.xbox=inst1", cmd)
        self.assertEqual(cmd[-2:], ["-jar", str(self.jar)])

    def test_umgebung_erbt_nichts_vom_dienst(self) -> None:
        env = xbox.build_env(self.spec())
        self.assertEqual(set(env), {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ"})
        self.assertTrue(env["HOME"].endswith(os.path.join(".mcsm", "home")))

    def test_signatur_erkennt_geaenderte_angaben(self) -> None:
        self.assertEqual(self.spec().signature, self.spec().signature)
        self.assertNotEqual(self.spec().signature, self.spec(port=19140).signature)
        self.assertNotEqual(self.spec().signature, self.spec(name="Anders").signature)


# ---------------------------------------------------------------- Zustand aus der Ausgabe

class ZustandTest(XboxBase):
    def _bot(self, **over) -> "xbox.Broadcaster":
        return xbox.Broadcaster(self.spec(**over))

    def test_anmeldecode_wird_erkannt(self) -> None:
        bot = self._bot()
        bot._track("[INFO] To sign in, use a web browser to open the page "
                   "https://microsoft.com/link and enter the code A1B2C3D4 to authenticate.")
        self.assertEqual(bot.state["state"], "login")
        self.assertEqual(bot.state["code"], "A1B2C3D4")
        self.assertEqual(bot.state["url"], "https://microsoft.com/link")

    def test_gamertag_und_sitzung(self) -> None:
        bot = self._bot()
        bot._track("[INFO] Sign in using the code XYZ123 at https://microsoft.com/link")
        bot._track("[INFO] Authenticated as EutopiaBot")
        self.assertEqual(bot.state["gamertag"], "EutopiaBot")
        self.assertTrue(bot.state["authed"])
        self.assertEqual(bot.state["code"], "")          # der Code ist eingelöst
        bot._track("[INFO] Creation of Xbox LIVE session was successful")
        self.assertEqual(bot.state["state"], "online")

    def test_harmlose_zeilen_sind_kein_fehler(self) -> None:
        bot = self._bot()
        bot._track("[WARN] Failed to ping server, using config values instead")
        bot._track("\tat com.example.Foo.bar(Foo.java:1)")
        bot._track("java.net.SocketTimeoutException")
        self.assertEqual(bot.state["error"], "")

    def test_echter_fehler_landet_im_zustand(self) -> None:
        bot = self._bot()
        bot._track("[ERROR] Failed to create Xbox LIVE session: 401 Unauthorized")
        self.assertIn("401", bot.state["error"])

    def test_klartext_nennt_den_code(self) -> None:
        text = xbox.state_text("login", code="A1B2C3D4", url="https://microsoft.com/link")
        self.assertIn("A1B2C3D4", text)
        self.assertIn("microsoft.com/link", text)
        self.assertIn("Freundesliste", xbox.state_text("online", gamertag="EutopiaBot"))
        self.assertIn("läuft nicht", xbox.state_text("off"))


# ---------------------------------------------------------------- Starten und Stoppen

class ProzessTest(XboxBase):
    def setUp(self) -> None:
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("Als root wird absichtlich nicht gestartet")

    def _warte(self, bedingung, grenze: float = 8.0) -> None:
        ende = time.time() + grenze
        while time.time() < ende:
            if bedingung():
                return
            time.sleep(0.05)
        self.fail("Der Bot hat nicht rechtzeitig reagiert")

    def _start(self, fake: FakeBot, **over) -> FakeBroadcaster:
        bot = FakeBroadcaster(self.spec(**over), fake)
        bot.start()
        return bot

    def test_start_schreibt_konfiguration_und_merkdatei(self) -> None:
        fake = FakeBot()
        bot = self._start(fake)
        self.assertTrue(bot.running)
        self.assertEqual(bot.cwd, str(xbox.xbox_dir(self.dir)))
        self.assertTrue(xbox.config_path(self.dir).is_file())
        self.assertTrue(xbox.pid_path(self.dir).is_file())
        bot.stop(timeout=3)
        # Die Merkdatei verschwindet, sobald der Faden das Prozessende verarbeitet hat.
        self._warte(lambda: not xbox.pid_path(self.dir).exists())

    def test_ohne_port_kein_start(self) -> None:
        bot = FakeBroadcaster(self.spec(port=0), FakeBot())
        with self.assertRaises(ValueError) as fehler:
            bot.start()
        self.assertIn("Bedrock-Port", str(fehler.exception))

    def test_ohne_jar_kein_start(self) -> None:
        self.jar.unlink()
        bot = FakeBroadcaster(self.spec(), FakeBot())
        with self.assertRaises(ValueError) as fehler:
            bot.start()
        self.assertIn("noch nicht eingerichtet", str(fehler.exception))

    def test_konsole_zaehlt_fortlaufend(self) -> None:
        fake = FakeBot()
        bot = self._start(fake)
        erste = bot.console(0)
        self.assertEqual(erste["lines"][0], "[Manager] Starte Xbox-Freunde-Modus (MCXboxBroadcast) …")
        fake.stdout.put("[INFO] Sign in at https://microsoft.com/link with the code AB12CD34\n")
        self._warte(lambda: bot.console(erste["next"])["lines"])
        self.assertEqual(bot.status()["code"], "AB12CD34")
        self.assertEqual(bot.status()["state"], "login")
        bot.stop(timeout=3)
        self._warte(lambda: not bot.running)
        self.assertEqual(fake.commands, ["exit"])

    def test_farbcodes_werden_entfernt(self) -> None:
        fake = FakeBot()
        bot = self._start(fake)
        fake.stdout.put("\x1b[32m[INFO]\x1b[0m Hallo\n")
        self._warte(lambda: any("Hallo" in z for z in bot.console(0)["lines"]))
        self.assertTrue(any(z == "[INFO] Hallo" for z in bot.console(0)["lines"]))

    def test_haenger_wird_beendet(self) -> None:
        fake = FakeBot(hangs=True)
        bot = self._start(fake)
        fake.stdout.put("[INFO] Authenticated as EutopiaBot\n")
        self._warte(lambda: bot.state.get("authed"))
        bot.stop(timeout=1)
        self._warte(lambda: not bot.running)
        self.assertTrue(fake.terminated or fake.killed)

    def test_neustart_nur_bei_geaenderten_angaben(self) -> None:
        fake = FakeBot()
        bot = self._start(fake)
        self.assertFalse(bot.restart_needed)
        bot.start()                                       # zweiter Aufruf: nichts passiert
        self.assertEqual(fake.commands, [])
        bot.spec = self.spec(port=19140)
        self.assertTrue(bot.restart_needed)
        bot.stop(timeout=3)
        self._warte(lambda: not bot.running)

    def test_status_ohne_bot(self) -> None:
        stand = xbox.status_for(self.spec(), None, enabled=True, autostart=True)
        self.assertTrue(stand["installed"])
        self.assertFalse(stand["running"])
        self.assertEqual(stand["state"], "off")
        self.assertEqual(stand["address"], "arcardia-nexus.de")
        self.assertEqual(stand["port"], 19132)
        self.assertEqual(stand["host_name"], "Eutopia")
        self.assertFalse(stand["token_cached"])
        # Dieselben Feldnamen wie im Programm auf dem PC (manager.xbox_status).
        for feld in ("enabled", "installed", "running", "state", "code", "url", "gamertag",
                     "error", "address", "port", "host_name", "token_cached"):
            self.assertIn(feld, stand)

    def test_status_nicht_eingerichtet(self) -> None:
        self.jar.unlink()
        stand = xbox.status_for(self.spec(), None, enabled=False, autostart=True)
        self.assertFalse(stand["installed"])
        self.assertIn("nicht eingerichtet", stand["state_text"])

    def test_registry_haelt_einen_bot_je_instanz(self) -> None:
        reg = xbox.BroadcasterRegistry()
        eins = reg.get(self.spec())
        zwei = reg.get(self.spec(port=19140))
        self.assertIs(eins, zwei)
        self.assertEqual(zwei.spec.port, 19140)
        self.assertEqual(reg.running_ids(), [])
        self.assertFalse(reg.stop("inst1"))


# ---------------------------------------------------------------- Anmeldung verwerfen

class ResetTest(XboxBase):
    def test_reset_loescht_nur_den_anmeldeordner(self) -> None:
        spec = self.spec()
        token = xbox.token_path(self.dir)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("{}", encoding="utf-8")
        (xbox.xbox_dir(self.dir) / "cache" / "unter").mkdir(parents=True, exist_ok=True)
        (xbox.xbox_dir(self.dir) / "cache" / "unter" / "x.bin").write_bytes(b"x")
        xbox.write_config(spec)
        (self.dir / "server.properties").write_text("motd=Eutopia\n", encoding="utf-8")
        self.assertTrue(xbox.token_cached(self.dir))
        xbox.reset(spec, None)
        self.assertFalse(xbox.token_cached(self.dir))
        self.assertFalse((xbox.xbox_dir(self.dir) / "cache").exists())
        # Konfiguration und Serverdateien bleiben unangetastet.
        self.assertTrue(xbox.config_path(self.dir).is_file())
        self.assertTrue((self.dir / "server.properties").is_file())


# ---------------------------------------------------------------- Der Umzug nimmt die Anmeldung mit

class UmzugTest(unittest.TestCase):
    """Die wichtigste Zusage: ``xbox/cache/cache.json`` wird übertragen.

    Der Stolperstein ist ``cache`` in ``paths.NACHLADBAR_DIRS`` – das gilt nur für den **ersten**
    Pfadteil. Fiele die Anmeldung doch unter die Auslassungsliste, müsste sich der Betreiber nach
    jedem Umzug neu bei Xbox Live anmelden, und seine Freunde verlören den Server aus der Liste.
    """

    def test_anmeldung_ist_nicht_nachladbar(self) -> None:
        self.assertTrue(transfer.xbox_anmeldung_dabei())
        self.assertFalse(paths.nachladbar("xbox"))
        self.assertFalse(paths.nachladbar("xbox/config.yml"))
        self.assertFalse(paths.nachladbar("xbox/cache/cache.json"))
        self.assertFalse(paths.nur_lokal("xbox/cache/cache.json"))
        # Zur Gegenprobe: der Zwischenspeicher von Paper im Wurzelverzeichnis fällt weiterhin weg.
        self.assertTrue(paths.nachladbar("cache/mojang_1.21.1.jar"))
        self.assertNotIn("xbox", paths.NACHLADBAR_DIRS)
        self.assertNotIn("xbox", paths.NUR_LOKAL_DIRS)

    def test_manifest_nimmt_die_anmeldung_mit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcsm-umzug-") as tmp:
            ordner = pathlib.Path(tmp)
            (ordner / "xbox" / "cache").mkdir(parents=True)
            (ordner / "xbox" / "cache" / "cache.json").write_text("{}", encoding="utf-8")
            (ordner / "xbox" / "config.yml").write_text("session:\n", encoding="utf-8")
            (ordner / "xbox" / "player_history.db").write_bytes(b"db")
            (ordner / "cache").mkdir()
            (ordner / "cache" / "gross.jar").write_bytes(b"nachladbar")
            (ordner / "server.properties").write_text("motd=x\n", encoding="utf-8")
            manifest = transfer.build_manifest(ordner)
            dabei = {e["path"] for e in manifest["files"]}
            self.assertIn("xbox/cache/cache.json", dabei)
            self.assertIn("xbox/config.yml", dabei)
            self.assertIn("xbox/player_history.db", dabei)
            self.assertNotIn("cache/gross.jar", dabei)
            self.assertEqual(manifest["skipped"], [])

    def test_auslassungsliste_des_pc_programms_kennt_xbox_nicht(self) -> None:
        """Die Zweitschrift in ``core/cloud.py`` (das Programm auf dem PC) wird mitgeprüft.

        Sie ist nötig, weil das Programm auf dem PC die Module des Root-Servers nicht mitbringt.
        Stünde ``xbox`` dort in einer der Listen, käme die Anmeldung beim **Hochladen** nicht mit.
        """
        quelle = pathlib.Path(_PROJEKT) / "core" / "cloud.py"
        if not quelle.is_file():                          # pragma: no cover - nur im Repo vorhanden
            self.skipTest("core/cloud.py liegt hier nicht")
        baum = ast.parse(quelle.read_text(encoding="utf-8"))
        gefunden: dict = {}
        for knoten in baum.body:
            if not isinstance(knoten, ast.Assign):
                continue
            for ziel in knoten.targets:
                if isinstance(ziel, ast.Name) and ziel.id in ("NACHLADBAR_DIRS",
                                                              "NACHLADBAR_FILES",
                                                              "SKIP_TOP_DIRS", "RESERVED_NAMES"):
                    gefunden[ziel.id] = ast.literal_eval(knoten.value)
        self.assertEqual(gefunden.get("NACHLADBAR_DIRS"), paths.NACHLADBAR_DIRS)
        self.assertEqual(gefunden.get("SKIP_TOP_DIRS"), paths.NUR_LOKAL_DIRS)
        for name, liste in gefunden.items():
            self.assertNotIn("xbox", liste, f"„xbox“ darf nicht in {name} stehen.")


# ---------------------------------------------------------------- Einstellungen der Instanz

class EinstellungenTest(unittest.TestCase):
    def test_vorgaben(self) -> None:
        self.assertFalse(instances.xbox_enabled({}))
        self.assertTrue(instances.xbox_autostart({}))
        self.assertFalse(instances.xbox_enabled({"xbox_enabled": "unbrauchbar"}))
        self.assertTrue(instances.xbox_autostart({"xbox_autostart": "unbrauchbar"}))

    def test_wortschatz_wie_beim_ruhezustand(self) -> None:
        for wert in (True, 1, "ja", "an", "true", "ON"):
            self.assertTrue(instances.clean_xbox_enabled(wert), wert)
        for wert in (False, 0, "nein", "aus", "false", "OFF"):
            self.assertFalse(instances.clean_xbox_enabled(wert), wert)
        with self.assertRaises(ValueError):
            instances.clean_xbox_enabled("vielleicht")

    def test_anzeige_enthaelt_die_schalter(self) -> None:
        ansicht = instances.public_instance({"id": "i1", "name": "Eutopia",
                                             "xbox_enabled": True, "xbox_autostart": False})
        self.assertTrue(ansicht["xbox_enabled"])
        self.assertFalse(ansicht["xbox_autostart"])


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
