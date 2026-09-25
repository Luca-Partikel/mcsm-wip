"""Selbsttests der Gruppe „runner“: Portvergabe, Startbefehle, Konsolenpuffer, Archive.

Alles läuft ohne Netz und ohne echten Minecraft-Server. Der Prozess wird durch ein
Doppel ersetzt, das sich wie ein Server verhält (Ausgabe, `stop`, `mcsmstop`, Hänger).

    python -m unittest discover -s hosted/tests
"""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import queue
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

# hosted/core unter einem eigenen Paketnamen laden. Im Projektordner gibt es schon ein Paket
# „core“ (das lokale Programm); ein schlichtes sys.path.insert würde das falsche erwischen.
_HOSTED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "hosted_core" not in sys.modules:
    _spec = importlib.machinery.ModuleSpec("hosted_core", None, is_package=True)
    _pkg = importlib.util.module_from_spec(_spec)
    _pkg.__path__ = [os.path.join(_HOSTED, "core")]
    sys.modules["hosted_core"] = _pkg

ports = importlib.import_module("hosted_core.ports")
isolation = importlib.import_module("hosted_core.isolation")
runner = importlib.import_module("hosted_core.runner")
sources_linux = importlib.import_module("hosted_core.sources_linux")


# ---------------------------------------------------------------- Portvergabe

class PortPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = ports.PortPool(probe=False)

    def test_bereiche_wie_vereinbart(self) -> None:
        java = ports.POOLS[ports.JAVA_POOL]
        bedrock = ports.POOLS[ports.BEDROCK_POOL]
        self.assertEqual((java.proto, java.first, java.last), (ports.TCP, 25565, 25700))
        self.assertEqual((bedrock.proto, bedrock.first, bedrock.last), (ports.UDP, 19132, 19300))
        self.assertIn((ports.TCP, 25565, 25700), ports.firewall_ranges())

    def test_erste_ports_der_reihe_nach(self) -> None:
        self.assertEqual(self.pool.allocate("inst-a", ports.JAVA_POOL).first, 25565)
        self.assertEqual(self.pool.allocate("inst-b", ports.JAVA_POOL).first, 25566)
        self.assertEqual(self.pool.allocate("inst-c", ports.JAVA_POOL).first, 25567)

    def test_gleicher_besitzer_behaelt_seinen_port(self) -> None:
        first = self.pool.allocate("inst-a", ports.JAVA_POOL)
        again = self.pool.allocate("inst-a", ports.JAVA_POOL)
        self.assertEqual(first.first, again.first)
        self.assertEqual(len(self.pool.leases()), 1)

    def test_luecke_wird_wiederverwendet(self) -> None:
        self.pool.allocate("inst-a", ports.JAVA_POOL)
        self.pool.allocate("inst-b", ports.JAVA_POOL)
        self.pool.release("inst-a")
        self.assertEqual(self.pool.allocate("inst-c", ports.JAVA_POOL).first, 25565)

    def test_fester_port_kollidiert(self) -> None:
        self.pool.reserve("inst-a", ports.JAVA_POOL, 25600)
        with self.assertRaises(ports.PortsExhausted):
            self.pool.reserve("inst-b", ports.JAVA_POOL, 25600)
        # Die Meldung nennt keine fremde Instanz.
        try:
            self.pool.reserve("inst-b", ports.JAVA_POOL, 25600)
        except ports.PortsExhausted as exc:
            self.assertNotIn("inst-a", str(exc))
        self.assertEqual(self.pool.owner_of(ports.JAVA_POOL, 25600), "inst-a")

    def test_port_ausserhalb_des_bereichs(self) -> None:
        with self.assertRaises(ValueError):
            self.pool.reserve("inst-a", ports.JAVA_POOL, 25564)
        with self.assertRaises(ValueError):
            self.pool.reserve("inst-a", ports.JAVA_POOL, 25700, 2)
        with self.assertRaises(ValueError):
            self.pool.reserve("inst-a", "quatsch", 25565)

    def test_ungueltige_kennung(self) -> None:
        for bad in ("", " ", "-start", "mit leerzeichen", "x" * 70, "../weg"):
            with self.assertRaises(ValueError):
                self.pool.allocate(bad, ports.JAVA_POOL)

    def test_udp_blockgroesse(self) -> None:
        self.assertEqual(ports.udp_block_size(10), 18)
        self.assertEqual(ports.udp_block_size(1), ports.MIN_UDP_BLOCK)
        self.assertEqual(ports.udp_block_size(None), 18)
        self.assertEqual(ports.udp_block_size(200), ports.MAX_UDP_BLOCK)

    def test_bedarf_je_serverart(self) -> None:
        self.assertEqual(ports.requirements("java"), [(ports.JAVA_POOL, 1)])
        self.assertEqual(ports.requirements("java", geyser=True),
                         [(ports.JAVA_POOL, 1), (ports.BEDROCK_POOL, 1)])
        self.assertEqual(ports.requirements("bedrock", max_players=10),
                         [(ports.JAVA_POOL, 1), (ports.BEDROCK_POOL, 18)])
        with self.assertRaises(ValueError):
            ports.requirements("bedwars")

    def test_java_mit_geyser_bekommt_beide_ports(self) -> None:
        leases = self.pool.allocate_for("inst-a", "java", geyser=True)
        self.assertEqual(leases[ports.JAVA_POOL].first, 25565)
        self.assertEqual(leases[ports.BEDROCK_POOL].first, 19132)
        self.assertEqual(self.pool.assignment("inst-a"), {"port": 25565, "bedrock_port": 19132})

    def test_bedrock_bekommt_block_und_handshake_port(self) -> None:
        self.pool.allocate_for("inst-a", "bedrock", max_players=10)
        self.assertEqual(self.pool.assignment("inst-a"),
                         {"port": 25565, "bedrock_port": 19132,
                          "udp_first": 19132, "udp_last": 19149})
        # Der nächste Bedrock-Server muss hinter dem Block anfangen.
        self.pool.allocate_for("inst-b", "bedrock", max_players=10)
        self.assertEqual(self.pool.assignment("inst-b")["udp_first"], 19150)

    def test_crossplay_abgeschaltet_gibt_udp_port_frei(self) -> None:
        self.pool.allocate_for("inst-a", "java", geyser=True)
        self.pool.allocate_for("inst-a", "java", geyser=False)
        self.assertIsNone(self.pool.lease("inst-a", ports.BEDROCK_POOL))
        self.assertEqual(self.pool.assignment("inst-a"), {"port": 25565})

    def test_voller_bereich_meldet_sich_verstaendlich(self) -> None:
        # Bei 200 Spielerplätzen braucht jeder Bedrock-Server 64 UDP-Ports: in 19132-19300
        # passen zwei, der dritte darf nicht mehr durchkommen.
        self.pool.allocate_for("inst-a", "bedrock", max_players=200)
        self.pool.allocate_for("inst-b", "bedrock", max_players=200)
        with self.assertRaises(ports.PortsExhausted) as ctx:
            self.pool.allocate_for("inst-c", "bedrock", max_players=200)
        self.assertIn("zu viele Server", str(ctx.exception))
        # Alles oder nichts: der TCP-Port des dritten Servers darf nicht gebucht bleiben.
        self.assertEqual(self.pool.leases("inst-c"), [])
        self.assertIsNone(self.pool.owner_of(ports.JAVA_POOL, 25567))

    def test_freigabe_und_zaehlung(self) -> None:
        self.pool.allocate_for("inst-a", "bedrock", max_players=10)
        self.assertEqual(self.pool.free_ports(ports.BEDROCK_POOL), 169 - 18)
        self.assertEqual(self.pool.release("inst-a"), 2)
        self.assertEqual(self.pool.free_ports(ports.BEDROCK_POOL), 169)
        self.assertEqual(self.pool.free_ports(ports.JAVA_POOL), 136)
        self.assertEqual(self.pool.release("inst-a"), 0)

    def test_speichern_und_wiederherstellen(self) -> None:
        self.pool.allocate_for("inst-a", "java", geyser=True)
        self.pool.allocate_for("inst-b", "bedrock", max_players=12)
        saved = self.pool.snapshot()
        self.assertEqual(saved[0]["proto"], ports.UDP)
        fresh = ports.PortPool(probe=False)
        self.assertEqual(fresh.restore(saved), [])
        self.assertEqual(fresh.assignment("inst-a"), self.pool.assignment("inst-a"))
        self.assertEqual(fresh.assignment("inst-b"), self.pool.assignment("inst-b"))
        # Nach dem Wiederherstellen bekommt eine neue Instanz einen freien Port.
        self.assertEqual(fresh.allocate("inst-c", ports.JAVA_POOL).first, 25567)

    def test_wiederherstellen_meldet_kaputte_eintraege(self) -> None:
        pool = ports.PortPool(probe=False)
        notes = pool.restore([
            {"owner": "inst-a", "pool": "java", "first": 25565, "count": 1},
            {"owner": "inst-b", "pool": "java", "first": 25565, "count": 1},   # doppelt vergeben
            {"owner": "inst-a", "pool": "java", "first": 25570, "count": 1},   # zweiter Eintrag
            {"owner": "inst-c", "pool": "java", "first": 99999, "count": 1},   # außerhalb
            "keindict",
        ])
        self.assertEqual(len(notes), 4)
        self.assertEqual(len(pool.leases()), 1)
        self.assertEqual(pool.assignment("inst-a"), {"port": 25565})

    def test_probe_erkennt_fremden_dienst(self) -> None:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("0.0.0.0", 25565))
                sock.listen(1)
            except OSError:
                self.skipTest("Port 25565 ist auf diesem Rechner nicht zu haben")
            pool = ports.PortPool(probe=True)
            self.assertFalse(pool.is_free(ports.JAVA_POOL, 25565))
            self.assertNotEqual(pool.allocate("inst-a", ports.JAVA_POOL).first, 25565)


# ---------------------------------------------------------------- Startbefehle

class SpecBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mcsm-test-")
        self.root = pathlib.Path(self._tmp.name)
        self.old_root = runner.instances_root()
        runner.set_instances_root(self.root)
        self.dir = self.root / "u1" / "inst1"
        self.dir.mkdir(parents=True)
        self.java = self.root / "jre21" / "bin" / "java"
        self.java.parent.mkdir(parents=True)
        self.java.write_text("#!/bin/sh\n", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(runner.set_instances_root, self.old_root)

    def spec(self, **over) -> runner.RunSpec:
        data = {"instance_id": "inst1", "directory": self.dir, "kind": "java",
                "name": "Testserver", "version": "1.21.11", "ram_mb": 4096, "java": self.java}
        data.update(over)
        return runner.RunSpec(**data)


class BuildCommandTest(SpecBase):
    def test_java_befehl(self) -> None:
        cmd = runner.build_command(self.spec(ram_mb=4096))
        self.assertEqual(cmd[0], str(self.java))
        self.assertIn("-Xms2048M", cmd)
        self.assertIn("-Xmx4096M", cmd)
        self.assertIn("-Dfile.encoding=UTF-8", cmd)
        self.assertEqual(cmd[-3:], ["-jar", "server.jar", "nogui"])
        # Keine Windows-Reste, keine Shell.
        self.assertNotIn("nogui.exe", cmd)
        self.assertTrue(all(isinstance(part, str) for part in cmd))

    def test_kommandozeile_nennt_die_instanz(self) -> None:
        """Ohne dieses Kennzeichen stünde der Instanzordner nirgends in /proc/<pid>/cmdline –
        ein weiterlaufender Server ließe sich nach einem Neustart des Dienstes nicht mehr
        zuordnen (siehe `runner.orphan_pid`)."""
        spec = self.spec()
        cmd = runner.build_command(spec)
        kennzeichen = runner.instanz_kennzeichen(spec)
        self.assertIn(kennzeichen, cmd)
        self.assertIn(spec.instance_id, kennzeichen)
        self.assertLess(cmd.index(kennzeichen), cmd.index("-jar"))

    def test_kleiner_speicher_haelt_mindestwert(self) -> None:
        cmd = runner.build_command(self.spec(ram_mb=512))
        self.assertIn("-Xms512M", cmd)
        self.assertIn("-Xmx512M", cmd)

    def test_empfohlene_flags_kommen_vor_jar(self) -> None:
        cmd = runner.build_command(self.spec(java_flags=("-XX:+UseG1GC", "-XX:MaxGCPauseMillis=200")))
        self.assertLess(cmd.index("-XX:+UseG1GC"), cmd.index("-jar"))
        self.assertIn("-XX:MaxGCPauseMillis=200", cmd)

    def test_gefaehrliche_flags_werden_verworfen(self) -> None:
        # -XX:OnError & Co. führen Programme aus, -XX:ErrorFile/-XX:HeapDumpPath schreiben
        # Dateien an beliebige Stellen: nichts davon darf aus einer Konfiguration kommen.
        böse = ("-XX:OnOutOfMemoryError=rm -rf /", "-XX:OnError=/bin/sh", "-XX:ErrorFile=/etc/passwd",
                "-XX:HeapDumpPath=/root/dump", "-XX:+StartFlightRecording=filename=/etc/x",
                "-javaagent:/tmp/x.jar", "-agentpath:/tmp/x.so", "/bin/sh", "-XX:+UseG1GC ; rm -rf /",
                "--add-opens=java.base/java.lang=ALL-UNNAMED", "-Dx=$(whoami)", "-D a=b")
        spec = self.spec(java_flags=("-XX:+UseG1GC",) + böse)
        self.assertEqual(spec.java_flags, ("-XX:+UseG1GC",))
        cmd = runner.build_command(spec)
        for flag in böse:
            self.assertNotIn(flag, cmd)
        # Die einzige Stelle mit „-jar“ ist die vom Manager selbst gebaute.
        self.assertEqual(cmd.count("-jar"), 1)

    def test_flags_ohne_doppelte(self) -> None:
        self.assertEqual(runner.safe_java_flags(["-XX:+UseG1GC", "-XX:+UseG1GC"]),
                         ["-XX:+UseG1GC"])
        self.assertEqual(runner.safe_java_flags(None), [])
        self.assertEqual(runner.safe_java_flags([1, None, "-XX:+UseG1GC"]), ["-XX:+UseG1GC"])

    def test_bedrock_befehl(self) -> None:
        cmd = runner.build_command(self.spec(kind="bedrock", java=None))
        self.assertEqual(cmd, [str(self.dir / "bedrock_server")])

    def test_umgebung_ist_klein_und_eigen(self) -> None:
        os.environ["MCSM_GEHEIM"] = "nicht weitergeben"
        self.addCleanup(os.environ.pop, "MCSM_GEHEIM", None)
        env = runner.build_env(self.spec())
        self.assertEqual(env["HOME"], str(self.dir / runner.MANAGE_DIR / "home"))
        self.assertEqual(env["TMPDIR"], str(self.dir / runner.MANAGE_DIR / "tmp"))
        self.assertNotIn("MCSM_GEHEIM", env)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        bedrock = runner.build_env(self.spec(kind="bedrock", java=None))
        self.assertEqual(bedrock["LD_LIBRARY_PATH"], str(self.dir))

    def test_eigene_umgebungsvariablen_werden_geprueft(self) -> None:
        self.assertEqual(runner.build_env(self.spec(extra_env={"MCSM_MODE": "hosted"}))["MCSM_MODE"],
                         "hosted")
        for bad in ({"kleines": "x"}, {"MIT LEER": "x"}, {"OK": "zwei\nzeilen"}):
            with self.assertRaises(ValueError):
                self.spec(extra_env=bad)

    def test_fehlende_java_laufzeit_wird_gemeldet(self) -> None:
        spec = self.spec(java=self.root / "gibtsnicht" / "java")
        with self.assertRaises(ValueError) as ctx:
            runner.build_command(spec)
        self.assertIn("nicht vorhanden", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            runner.build_command(self.spec(java=None, java_major=0))
        self.assertIn("nicht eingerichtet", str(ctx.exception))


class RunSpecTest(SpecBase):
    def test_pflichtangaben(self) -> None:
        with self.assertRaises(ValueError):
            self.spec(instance_id="")
        with self.assertRaises(ValueError):
            self.spec(instance_id="../weg")
        with self.assertRaises(ValueError):
            self.spec(kind="modpack")
        with self.assertRaises(ValueError):
            self.spec(ram_mb=128)
        with self.assertRaises(ValueError):
            self.spec(ram_mb=99999)
        with self.assertRaises(ValueError):
            self.spec(ram_mb="viel")

    def test_jar_bleibt_im_ordner(self) -> None:
        for bad in ("../server.jar", "unter/server.jar", "..\\server.jar", "server.txt",
                    ".versteckt.jar", "/srv/mcsm/server.jar"):
            with self.assertRaises(ValueError):
                self.spec(jar=bad)
        self.assertEqual(self.spec(jar="paper.jar").jar, "paper.jar")
        self.assertEqual(self.spec(jar="").jar, "server.jar")        # leer = Standardname

    def test_ordner_muss_unter_der_wurzel_liegen(self) -> None:
        outside = pathlib.Path(tempfile.gettempdir()).resolve() / "mcsm-fremd"
        with self.assertRaises(ValueError):
            self.spec(directory=outside)
        with self.assertRaises(ValueError):
            self.spec(directory=self.root / "u1" / ".." / ".." / "weg")
        with self.assertRaises(ValueError):
            self.spec(directory="relativ/dazu")
        with self.assertRaises(ValueError):
            self.spec(directory=self.root)

    def test_symlink_aus_dem_bereich_wird_erkannt(self) -> None:
        ziel = pathlib.Path(self._tmp.name).parent / "mcsm-ziel-test"
        ziel.mkdir(exist_ok=True)
        self.addCleanup(lambda: ziel.rmdir() if ziel.is_dir() else None)
        link = self.root / "u1" / "verlinkt"
        try:
            link.symlink_to(ziel, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks sind auf diesem System nicht erlaubt")
        with self.assertRaises(ValueError):
            self.spec(directory=link)

    def test_name_wird_entschaerft(self) -> None:
        self.assertEqual(self.spec(name="Mein\nServer\x00").name, "MeinServer")
        self.assertEqual(self.spec(name="").name, "Minecraft Server")
        self.assertEqual(len(self.spec(name="N" * 80).name), 48)

    def test_aus_konfiguration_des_lokalen_programms(self) -> None:
        cfg = {"id": "abcd1234", "name": "Paper", "type": "java", "flavor": "paper",
               "version": "1.21.11", "ram_mb": 6144, "java_major": 21,
               "java_flags": ["-XX:+UseG1GC", "-XX:OnError=/bin/sh"]}
        spec = runner.RunSpec.from_config(cfg, self.dir)
        self.assertEqual((spec.instance_id, spec.ram_mb, spec.java_major), ("abcd1234", 6144, 21))
        self.assertTrue(spec.companion)
        self.assertEqual(spec.java_flags, ("-XX:+UseG1GC",))
        bedrock = runner.RunSpec.from_config({**cfg, "type": "bedrock"}, self.dir)
        self.assertEqual(bedrock.kind, "bedrock")
        self.assertFalse(bedrock.companion)
        modpack = runner.RunSpec.from_config({**cfg, "flavor": "fabric"}, self.dir)
        self.assertFalse(modpack.companion)

    def test_befehle_muessen_einzeilig_sein(self) -> None:
        self.assertEqual(runner.check_command("  list  "), "list")
        for bad in ("", "   ", "list\nop boese", "list\r\nstop", "say \x07", "x" * 600):
            with self.assertRaises(ValueError):
                runner.check_command(bad)
        with self.assertRaises(ValueError):
            self.spec(after_ready_commands=("gamerule pvp true\nop boese",))
        self.assertEqual(self.spec(after_ready_commands=("gamerule pvp true",)).after_ready_commands,
                         ("gamerule pvp true",))


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

    def write(self, data) -> int:                    # type: ignore[override]
        self._rest += bytes(data)
        while b"\n" in self._rest:
            line, self._rest = self._rest.split(b"\n", 1)
            self._on_line(line.decode("utf-8").strip())
        return len(data)

    def flush(self) -> None:
        pass


class FakeServer:
    """Ein Server-Prozess zum Prüfen von Puffer und Stopp-Ablauf.

    `hangs=True` ignoriert `stop` – damit lässt sich die Eskalation auf SIGTERM prüfen.
    Die Prozesskennung ist absichtlich unmöglich, damit ein Signal an eine Prozessgruppe
    niemals den Testlauf selbst trifft.
    """

    def __init__(self, *, hangs: bool = False, companion: bool = False) -> None:
        self.pid = 0x7FFFFFF0
        self.stdout = _Pipe()
        self.stdin = _Stdin(self._command)
        self.commands: list[str] = []
        self.terminated = False
        self.killed = False
        self._hangs = hangs
        self._companion = companion
        self._code: int | None = None

    def _command(self, text: str) -> None:
        self.commands.append(text)
        if text.startswith("mcsmstop") and self._companion:
            self.stdout.put("[15:00:00 INFO]: [MCSMCompanion] Herunterfahren angefordert (10 s)\n")
            self._later(0.2, 0)
        elif text == "stop" and not self._hangs:
            self.stdout.put("[15:00:01 INFO]: Stopping server\n")
            self._finish(0)

    def _later(self, delay: float, code: int) -> None:
        import threading
        threading.Timer(delay, self._finish, args=(code,)).start()

    def _finish(self, code: int) -> None:
        if self._code is None:
            self._code = code
            self.stdout.eof()

    def poll(self) -> int | None:
        return self._code

    def wait(self) -> int:
        while self._code is None:
            time.sleep(0.02)
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._finish(-15)

    def kill(self) -> None:
        self.killed = True
        self._finish(-9)


class FakeRunner(runner.Runner):
    def __init__(self, spec, fake: FakeServer, **kw) -> None:
        super().__init__(spec, **kw)
        self.fake = fake

    def _spawn(self, cmd, cwd, env):                 # type: ignore[override]
        self.command = cmd
        self.cwd = cwd
        self.env = env
        return self.fake                             # type: ignore[return-value]


class RunnerProcessTest(SpecBase):
    def setUp(self) -> None:
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("Als root wird absichtlich nicht gestartet")
        (self.dir / "server.jar").write_bytes(b"nicht wirklich ein Jar")

    def _start(self, fake: FakeServer, **over) -> FakeRunner:
        run = FakeRunner(self.spec(**over), fake)
        run.start()
        return run

    def _warte(self, bedingung, grenze: float = 8.0) -> None:
        ende = time.time() + grenze
        while time.time() < ende:
            if bedingung():
                return
            time.sleep(0.05)
        self.fail("Der Server hat nicht rechtzeitig reagiert")

    def test_konsole_zaehlt_fortlaufend(self) -> None:
        fake = FakeServer()
        run = self._start(fake)
        fake.stdout.put("[15:00:00 INFO]: Starting minecraft server\n")
        fake.stdout.put('[15:00:05 INFO]: Done (5.2s)! For help, type "help"\n')
        self._warte(lambda: run.ready)
        erste = run.console(0)
        self.assertEqual(erste["lines"][0], "[Manager] Starte Testserver …")
        self.assertEqual(erste["next"], len(erste["lines"]))
        weiter = run.console(erste["next"])
        self.assertEqual(weiter["lines"], [])
        fake.stdout.put("[15:00:06 INFO]: Noch eine Zeile\n")
        self._warte(lambda: run.console(erste["next"])["lines"])
        self.assertEqual(run.console(erste["next"])["lines"], ["[15:00:06 INFO]: Noch eine Zeile"])
        self.assertEqual(run.console(0, tail=1)["lines"], ["[15:00:06 INFO]: Noch eine Zeile"])
        run.stop(timeout=3)
        self._warte(lambda: run.exit_code == 0)
        self.assertIn("[Manager] Server beendet (Code 0).", run.console(0)["lines"])
        self.assertFalse((self.dir / runner.MANAGE_DIR / runner.PID_FILE).exists())

    def test_ringpuffer_verwirft_alte_zeilen(self) -> None:
        run = FakeRunner(self.spec(), FakeServer())
        for i in range(runner.MAX_LOG_LINES + 50):
            run.log(f"Zeile {i}")
        stand = run.console(0)
        self.assertEqual(len(stand["lines"]), runner.MAX_LOG_LINES)
        self.assertEqual(stand["next"], runner.MAX_LOG_LINES + 50)
        self.assertEqual(stand["lines"][0], "Zeile 50")
        # Eine Nummer, die längst verworfen wurde, liefert einfach den ältesten Rest.
        self.assertEqual(len(run.console(5)["lines"]), runner.MAX_LOG_LINES)

    def test_farbcodes_und_ueberlange_zeilen(self) -> None:
        run = FakeRunner(self.spec(), FakeServer())
        run.log("\x1b[32m[INFO] Bunt\x1b[0m")
        run.log("x" * (runner.MAX_LINE_CHARS + 500))
        lines = run.console(0)["lines"]
        self.assertEqual(lines[0], "[INFO] Bunt")
        self.assertEqual(len(lines[1]), runner.MAX_LINE_CHARS)

    def test_startbefehl_und_arbeitsverzeichnis(self) -> None:
        run = self._start(FakeServer())
        self.assertEqual(run.cwd, str(self.dir))
        self.assertEqual(run.command[0], str(self.java))
        self.assertEqual(run.env["HOME"], str(self.dir / runner.MANAGE_DIR / "home"))
        self.assertTrue((self.dir / runner.MANAGE_DIR / runner.PID_FILE).is_file())
        # Der Instanzordner selbst bleibt sauber: nur .mcsm kommt vom Manager dazu.
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         [runner.MANAGE_DIR, "server.jar"])
        self.assertTrue(run.running)
        run.start()                                  # zweiter Start ändert nichts
        self.assertIs(run.proc, run.fake)
        run.stop(timeout=3)

    def test_befehl_senden_und_ablehnen(self) -> None:
        fake = FakeServer()
        run = self._start(fake)
        run.send(" list ")
        self._warte(lambda: "list" in fake.commands)
        self.assertIn("> list", run.console(0)["lines"])
        with self.assertRaises(ValueError):
            run.send("say hallo\nop boese")
        self.assertNotIn("op boese", fake.commands)
        run.stop(timeout=3)
        self._warte(lambda: not run.running)
        with self.assertRaises(ValueError) as ctx:
            run.send("list")
        self.assertIn("läuft nicht", str(ctx.exception))

    def test_stopp_speichert_und_endet(self) -> None:
        fake = FakeServer()
        run = self._start(fake)
        run.stop(timeout=5)
        self.assertIn("stop", fake.commands)
        self.assertFalse(run.running)
        self.assertFalse(fake.terminated)
        self.assertIn("[Manager] Stoppe Server – Welt wird gespeichert …", run.console(0)["lines"])

    def test_ankuendigung_ueber_begleit_plugin(self) -> None:
        fake = FakeServer(companion=True)
        run = self._start(fake)
        run.stop(timeout=5, announce_seconds=10)
        self.assertEqual(fake.commands[0], "mcsmstop 10")
        self.assertTrue(any("angekündigt" in l for l in run.console(0)["lines"]))

    def test_ankuendigung_per_say_ohne_plugin(self) -> None:
        fake = FakeServer(companion=False)
        run = self._start(fake, companion=False)
        run.stop(timeout=5, announce_seconds=1)
        self.assertTrue(fake.commands[0].startswith("say "))
        self.assertIn("stop", fake.commands)

    def test_haenger_bekommt_sigterm(self) -> None:
        fake = FakeServer(hangs=True)
        run = self._start(fake)
        run.stop(timeout=1)
        self._warte(lambda: fake.terminated or fake.killed)
        self.assertFalse(run.running)
        self.assertTrue(any("SIGTERM" in l for l in run.console(0)["lines"]))

    def test_unerwartetes_ende_wird_gemeldet(self) -> None:
        fake = FakeServer()
        run = self._start(fake)
        fake.stdout.put("[15:00:00 ERROR]: Port schon belegt\n")
        fake._finish(1)
        self._warte(lambda: run.exit_code == 1)
        self.assertIn("unerwartet beendet", run.last_error)
        self.assertFalse(run.running)
        self.assertEqual(run.status()["ram_mb"], 0)

    def test_fehlendes_jar_wird_gemeldet(self) -> None:
        (self.dir / "server.jar").unlink()
        run = FakeRunner(self.spec(), FakeServer())
        with self.assertRaises(ValueError) as ctx:
            run.start()
        self.assertIn("nicht vollständig installiert", str(ctx.exception))

    def test_fehlende_bedrock_datei_wird_gemeldet(self) -> None:
        run = FakeRunner(self.spec(kind="bedrock", java=None), FakeServer())
        with self.assertRaises(ValueError) as ctx:
            run.start()
        self.assertIn("bedrock_server", str(ctx.exception))

    def test_zustand_fuer_die_api(self) -> None:
        fake = FakeServer()
        run = self._start(fake)
        status = run.status()
        for key in ("id", "running", "ready", "stopping", "pid", "uptime", "ram_mb",
                    "memory_mb", "exit_code", "error", "console_next"):
            self.assertIn(key, status)
        self.assertEqual(status["ram_mb"], 4096)
        self.assertTrue(status["running"])
        self.assertFalse(status["ready"])
        run.stop(timeout=3)


class RegistryTest(SpecBase):
    def setUp(self) -> None:
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("Als root wird absichtlich nicht gestartet")
        (self.dir / "server.jar").write_bytes(b"jar")
        self.zweiter = self.root / "u1" / "inst2"
        self.zweiter.mkdir()
        (self.zweiter / "server.jar").write_bytes(b"jar")

    def test_verwaltung_zaehlt_laufende_server_und_speicher(self) -> None:
        gestoppt: list[str] = []
        reg = runner.RunnerRegistry(on_exit=lambda r, code: gestoppt.append(r.spec.instance_id))
        a = FakeRunner(self.spec(ram_mb=4096), FakeServer(), on_exit=reg._on_exit)
        b = FakeRunner(self.spec(instance_id="inst2", directory=self.zweiter, ram_mb=2048),
                       FakeServer(), on_exit=reg._on_exit)
        reg._runners["inst1"] = a
        reg._runners["inst2"] = b
        self.assertEqual(reg.running_ram_mb(), 0)
        a.start()
        b.start()
        self.assertEqual(sorted(reg.running_ids()), ["inst1", "inst2"])
        self.assertEqual(reg.running_ram_mb(), 6144)
        self.assertEqual(len(reg.statuses()), 2)
        reg.stop_all(timeout=3)
        self.assertEqual(reg.running_ids(), [])
        self.assertEqual(reg.running_ram_mb(), 0)
        # Die Rückmeldung kommt aus dem Lese-Thread, kurz nach dem Prozessende.
        ende = time.time() + 5
        while len(gestoppt) < 2 and time.time() < ende:
            time.sleep(0.05)
        self.assertEqual(sorted(gestoppt), ["inst1", "inst2"])

    def test_neue_angaben_gelten_erst_beim_naechsten_start(self) -> None:
        reg = runner.RunnerRegistry()
        run = FakeRunner(self.spec(ram_mb=4096), FakeServer())
        reg._runners["inst1"] = run
        run.start()
        gleicher = reg.get(self.spec(ram_mb=8192))
        self.assertIs(gleicher, run)
        self.assertEqual(run.active_spec.ram_mb, 4096)       # läuft weiter mit 4 GB
        self.assertEqual(run.spec.ram_mb, 8192)
        self.assertEqual(reg.running_ram_mb(), 4096)
        run.stop(timeout=3)


# ---------------------------------------------------------------- Quellen und Archive

class SourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mcsm-src-")
        self.root = pathlib.Path(self._tmp.name)
        self.old = sources_linux.data_root()
        sources_linux.set_data_root(self.root)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(sources_linux.set_data_root, self.old)

    def test_ordner(self) -> None:
        sources_linux.ensure_dirs()
        for folder in (sources_linux.cache_dir(), sources_linux.runtime_dir(),
                       sources_linux.users_dir(), sources_linux.transfer_dir()):
            self.assertTrue(folder.is_dir())
        self.assertGreater(sources_linux.free_disk_mb(self.root), 0)

    def test_bedrock_adresse_ist_die_linux_variante(self) -> None:
        url = sources_linux.bedrock_download_url("1.26.51.1")
        self.assertIn("/bin-linux/", url)
        self.assertNotIn("bin-win", url)
        self.assertTrue(url.endswith("bedrock-server-1.26.51.1.zip"))
        self.assertIn("/bin-linux-preview/", sources_linux.bedrock_download_url("1.26.60.28", True))
        for bad in ("", "1.26", "neueste", "1.2.3.4; rm -rf /", "../../etc"):
            with self.assertRaises(ValueError):
                sources_linux.bedrock_download_url(bad)

    def test_nur_erwartete_hosts_und_https(self) -> None:
        for bad in ("http://www.minecraft.net/x.zip", "https://boese.example/x.zip",
                    "ftp://x/y", "", "https:///x"):
            with self.assertRaises(sources_linux.SourceError):
                sources_linux._check_url(bad, ("www.minecraft.net",))
        self.assertTrue(sources_linux._check_url("https://www.minecraft.net/x.zip",
                                                 ("www.minecraft.net",)))

    def test_versionspruefung_und_gamerule(self) -> None:
        self.assertEqual(sources_linux.check_version("1.21.11"), "1.21.11")
        for bad in ("", "../etc", "latest", "1.21 11"):
            with self.assertRaises(sources_linux.SourceError):
                sources_linux.check_version(bad)
        self.assertIsNone(sources_linux.command_block_gamerule("1.21.8"))
        self.assertEqual(sources_linux.command_block_gamerule("1.21.9"), "commandBlocksEnabled")
        self.assertEqual(sources_linux.command_block_gamerule("1.21.11"), "command_blocks_work")
        self.assertEqual(sources_linux.command_block_gamerule("26.3"), "command_blocks_work")
        self.assertEqual(sources_linux.version_tuple("1.21.11"), (1, 21, 11))

    def test_java_schaetzung(self) -> None:
        self.assertEqual(sources_linux._guess_java_major("1.16.5"), 8)
        self.assertEqual(sources_linux._guess_java_major("1.18.2"), 17)
        self.assertEqual(sources_linux._guess_java_major("1.21.11"), 21)
        self.assertEqual(sources_linux._guess_java_major("26.3"), 25)
        self.assertEqual(sources_linux._pick_java(22), 25)

    def test_dateinamen_aus_api_werden_entschaerft(self) -> None:
        self.assertEqual(sources_linux._safe_name("../../etc/passwd"), "etc_passwd")
        self.assertEqual(sources_linux._safe_name("jdk-21.0.12.1+1"), "jdk-21.0.12.1+1")
        self.assertEqual(sources_linux._safe_name("", "ersatz"), "ersatz")

    def test_pfade_im_archiv(self) -> None:
        root = self.root / "ziel"
        root.mkdir()
        for bad in ("../weg.txt", "/etc/passwd", "a/../../weg.txt", "", ".", "..",
                    "C:/Windows/x.txt", "..\\weg.txt"):
            self.assertIsNone(sources_linux._inside(root, bad), bad)
        self.assertEqual(sources_linux._inside(root, "a/b.txt"), root / "a" / "b.txt")
        self.assertEqual(sources_linux._inside(root, "./a/b.txt"), root / "a" / "b.txt")

    def test_zip_kann_nicht_ausbrechen(self) -> None:
        archiv = self.root / "test.zip"
        with zipfile.ZipFile(archiv, "w") as zf:
            zf.writestr("server.properties", "server-port=25565\n")
            zf.writestr("bedrock_server", "binaer")
            zf.writestr("worlds/Bedrock level/level.dat", "welt")
            zf.writestr("../entwischt.txt", "boese")
            zf.writestr("unter/tief/datei.txt", "ok")
        ziel = self.root / "instanz"
        geschrieben = sources_linux.extract_zip(archiv, ziel)
        self.assertGreaterEqual(geschrieben, 4)
        self.assertFalse((self.root / "entwischt.txt").exists())
        self.assertFalse((ziel.parent / "entwischt.txt").exists())
        self.assertTrue((ziel / "unter" / "tief" / "datei.txt").is_file())

    def test_zip_update_behaelt_welt_und_konfiguration(self) -> None:
        archiv = self.root / "test.zip"
        with zipfile.ZipFile(archiv, "w") as zf:
            zf.writestr("server.properties", "neu\n")
            zf.writestr("worlds/Bedrock level/level.dat", "neu")
            zf.writestr("bedrock_server", "neu")
        ziel = self.root / "instanz"
        (ziel / "worlds" / "Bedrock level").mkdir(parents=True)
        (ziel / "worlds" / "Bedrock level" / "level.dat").write_text("meine welt", encoding="utf-8")
        (ziel / "server.properties").write_text("meine einstellungen", encoding="utf-8")
        sources_linux.extract_zip(archiv, ziel, sources_linux.BEDROCK_KEEP,
                                  sources_linux.BEDROCK_KEEP_DIRS)
        self.assertEqual((ziel / "worlds" / "Bedrock level" / "level.dat").read_text(encoding="utf-8"),
                         "meine welt")
        self.assertEqual((ziel / "server.properties").read_text(encoding="utf-8"),
                         "meine einstellungen")
        self.assertEqual((ziel / "bedrock_server").read_text(encoding="utf-8"), "neu")

    def test_tar_kann_nicht_ausbrechen(self) -> None:
        archiv = self.root / "jre.tar.gz"
        with tarfile.open(archiv, "w:gz") as tf:
            daten = b"#!/bin/sh\necho java\n"
            info = tarfile.TarInfo("jdk-21-jre/bin/java")
            info.size = len(daten)
            info.mode = 0o4777                       # setuid und für alle schreibbar
            tf.addfile(info, io.BytesIO(daten))
            weg = tarfile.TarInfo("../entwischt.txt")
            weg.size = len(daten)
            tf.addfile(weg, io.BytesIO(daten))
            absolut = tarfile.TarInfo("/etc/passwd")
            absolut.size = len(daten)
            tf.addfile(absolut, io.BytesIO(daten))
            link = tarfile.TarInfo("jdk-21-jre/böse")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
            hoch = tarfile.TarInfo("jdk-21-jre/hoch")
            hoch.type = tarfile.SYMTYPE
            hoch.linkname = "../../../../etc/passwd"
            tf.addfile(hoch)
            gut = tarfile.TarInfo("jdk-21-jre/legal/LICENSE")
            gut.type = tarfile.SYMTYPE
            gut.linkname = "../bin/java"
            tf.addfile(gut)
        ziel = self.root / "runtime" / "jre21"
        sources_linux.extract_tar(archiv, ziel)
        java = ziel / "jdk-21-jre" / "bin" / "java"
        self.assertTrue(java.is_file())
        self.assertFalse((self.root / "entwischt.txt").exists())
        self.assertFalse((ziel / "böse").is_symlink())
        self.assertFalse((ziel / "jdk-21-jre" / "böse").is_symlink())
        self.assertFalse((ziel / "jdk-21-jre" / "hoch").is_symlink())
        if os.name == "posix":
            self.assertEqual(java.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(sources_linux._find_java_in(ziel), java)

    def test_kaputtes_archiv_meldet_sich(self) -> None:
        kaputt = self.root / "kaputt.zip"
        kaputt.write_bytes(b"das ist kein zip")
        with self.assertRaises(sources_linux.SourceError):
            sources_linux.extract_zip(kaputt, self.root / "ziel")
        with self.assertRaises(sources_linux.SourceError):
            sources_linux.extract_tar(kaputt, self.root / "ziel2")

    def test_pruefsumme_und_beiblatt(self) -> None:
        sources_linux.ensure_dirs()
        datei = sources_linux.cache_dir() / "datei.bin"
        datei.write_bytes(b"minecraft")
        summe = sources_linux.sha256_of(datei)
        self.assertEqual(len(summe), 64)
        sources_linux._write_sidecar(datei, summe)
        self.assertEqual(sources_linux._read_sidecar(datei), summe)
        sources_linux._sidecar(datei).write_text("murks\n", encoding="utf-8")
        self.assertEqual(sources_linux._read_sidecar(datei), "")

    def test_beiblatt_nur_im_zwischenspeicher(self) -> None:
        """In einem Instanzordner sollen keine .sha256-Dateien zwischen den Plugins liegen."""
        sources_linux.ensure_dirs()
        instanz = sources_linux.users_dir() / "u1" / "inst1" / "plugins"
        instanz.mkdir(parents=True)
        jar = instanz / "Geyser-Spigot.jar"
        jar.write_bytes(b"jar")
        sources_linux._write_sidecar(jar, sources_linux.sha256_of(jar))
        self.assertFalse(sources_linux._sidecar(jar).exists())
        self.assertFalse(sources_linux._in_cache(jar))
        self.assertTrue(sources_linux._in_cache(sources_linux.cache_dir() / "x.zip"))

    def test_platte_voll_wird_gemeldet(self) -> None:
        with self.assertRaises(sources_linux.DiskFull) as ctx:
            sources_linux.require_free_disk(10 ** 9, self.root)
        self.assertIn("frei", str(ctx.exception))
        # DiskFull ist eine SourceError (und damit ein ValueError) - der Daemon fängt sie mit.
        self.assertIsInstance(ctx.exception, sources_linux.SourceError)
        self.assertIsInstance(ctx.exception, ValueError)
        sources_linux.require_free_disk(0, self.root)      # genug Platz: kein Fehler

    def test_eula_muss_bestaetigt_sein(self) -> None:
        with self.assertRaises(sources_linux.SourceError) as ctx:
            sources_linux.install_paper("1.21.11", self.root / "instanz", False)
        self.assertIn("EULA", str(ctx.exception))

    def test_crossplay_dateien_entfernen(self) -> None:
        plugins = self.root / "instanz" / "plugins"
        plugins.mkdir(parents=True)
        for name in sources_linux.CROSSPLAY_JARS:
            (plugins / name).write_bytes(b"jar")
        (plugins / "MCSMCompanion.jar").write_bytes(b"jar")
        sources_linux.remove_crossplay(self.root / "instanz")
        for name in sources_linux.CROSSPLAY_JARS:
            self.assertFalse((plugins / name).exists())
        self.assertTrue((plugins / "MCSMCompanion.jar").is_file())

    def test_java_pfad_ohne_laufzeit(self) -> None:
        self.assertIsNone(sources_linux._find_java_in(self.root / "gibtsnicht"))
        self.assertIsNone(sources_linux.java_major_of(self.root / "gibtsnicht" / "java"))
        self.assertIsNone(sources_linux.java_path("keine zahl"))

    def test_geyser_projekt_wird_geprueft(self) -> None:
        for bad in ("boese", "", "../geyser"):
            with self.assertRaises(sources_linux.SourceError):
                sources_linux.geyser_download(bad)
        with self.assertRaises(sources_linux.SourceError):
            sources_linux.hangar_download("../weg")

    def test_adoptium_lehnt_unsinn_ab(self) -> None:
        for bad in (0, 7, 200, "zwanzig"):
            with self.assertRaises(sources_linux.SourceError):
                sources_linux.adoptium_jre(bad)


# ---------------------------------------------------------------- Getrennte Server-Benutzer

class IsolationTest(SpecBase):
    """Jeder Kunden-Server soll unter einem eigenen Unix-Benutzer laufen.

    Der Vorrat an Benutzern wird von deploy.sh angelegt; hier wird nur geprüft, dass der
    Befehl richtig eingepackt wird und dass ohne Einrichtung nichts kaputtgeht.
    """

    def test_spec_prueft_den_benutzernamen(self) -> None:
        self.assertEqual(self.spec().run_as, "")
        self.assertEqual(self.spec(run_as="mcsmsrv07").run_as, "mcsmsrv07")
        for bad in ("root; rm -rf /", "../etc", "Gross", "mit leer", "x" * 40):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.spec(run_as=bad)

    def test_befehl_wird_unter_sudo_eingepackt(self) -> None:
        echt = isolation.sudo_path
        isolation.sudo_path = lambda: "/usr/bin/sudo"                # type: ignore[assignment]
        try:
            cmd = isolation.wrap(["/srv/jre/bin/java", "-jar", "server.jar"], "mcsmsrv03",
                                 {"HOME": "/srv/x/.mcsm/home", "LANG": "C.UTF-8"})
        finally:
            isolation.sudo_path = echt                              # type: ignore[assignment]
        self.assertEqual(cmd[:5], ["/usr/bin/sudo", "-n", "-u", "mcsmsrv03", "--"])
        self.assertIn("/usr/bin/env", cmd)
        self.assertIn("HOME=/srv/x/.mcsm/home", cmd)
        self.assertIn("/srv/jre/bin/java", cmd)
        # Die Umask wird gesetzt, damit der Daemon die Dateien des Servers noch anfassen kann.
        self.assertTrue(any(f"umask {isolation.SERVER_UMASK}" in teil for teil in cmd))

    def test_ohne_benutzer_bleibt_der_befehl_unveraendert(self) -> None:
        self.assertEqual(isolation.wrap(["java", "-jar", "x.jar"], ""), ["java", "-jar", "x.jar"])

    def test_ohne_sudo_klare_meldung(self) -> None:
        echt = isolation.sudo_path
        isolation.sudo_path = lambda: ""                             # type: ignore[assignment]
        try:
            with self.assertRaises(ValueError) as fehler:
                isolation.wrap(["java"], "mcsmsrv01")
            self.assertIn("sudo", str(fehler.exception))
        finally:
            isolation.sudo_path = echt                              # type: ignore[assignment]

    def test_status_ist_immer_beantwortbar(self) -> None:
        lage = isolation.status()
        self.assertIn("aktiv", lage)
        self.assertIsInstance(lage["benutzer"], int)
        self.assertEqual(isolation.user_for_owner(""), "")


if __name__ == "__main__":
    unittest.main()
