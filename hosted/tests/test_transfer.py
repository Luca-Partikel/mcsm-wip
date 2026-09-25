"""Selbsttests für hosted/core/transfer.py – Manifest, Stücke, Wiederaufnahme, Rückholung."""
from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

# hosted/core unter einem eigenen Paketnamen laden (im Projektordner gibt es schon ein „core“).
_HOSTED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "hosted_core" not in sys.modules:
    _spec = importlib.machinery.ModuleSpec("hosted_core", None, is_package=True)
    _pkg = importlib.util.module_from_spec(_spec)
    _pkg.__path__ = [os.path.join(_HOSTED, "core")]
    sys.modules["hosted_core"] = _pkg

paths = importlib.import_module("hosted_core.paths")
transfer = importlib.import_module("hosted_core.transfer")


def write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def make_instance(root: pathlib.Path) -> None:
    """Eine kleine Instanz, wie sie vom PC hochgeladen würde."""
    write(root / "server.properties", b"level-name=welt\nmax-players=10\n")
    write(root / "server.jar", b"MZ" + b"\x00" * 5000)
    write(root / "plugins" / "EssentialsX.jar", os.urandom(20_000))
    write(root / "plugins" / "config" / "config.yml", b"aktiv: true\n")
    write(root / "welt" / "level.dat", os.urandom(4096))
    write(root / "welt" / "region" / "r.0.0.mca", os.urandom(70_000))
    write(root / "welt" / "leer.dat", b"")
    write(root / "logs" / "latest.log", b"[INFO] Fertig\n")


def push(session, source: pathlib.Path, chunk: int = 8192, stop_after: int = 0) -> int:
    """Simuliert den PC: sendet Stücke, bis alles da ist (oder nach ``stop_after`` Stücken)."""
    sent = 0
    while True:
        todo = session.next_needed()
        if todo is None:
            return sent
        with (source / todo["path"]).open("rb") as fh:
            fh.seek(todo["offset"])
            data = fh.read(min(chunk, todo["size"] - todo["offset"]))
        session.write_chunk(todo["path"], todo["offset"], data)
        sent += 1
        if stop_after and sent >= stop_after:
            return sent


class Basis(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcsm-transfer-"))
        self.source = self.tmp / "pc" / "meinserver"
        self.target = self.tmp / "root" / "users" / "u1" / "i1"
        self.store = self.tmp / "root" / "transfer"
        self.data_root = self.tmp / "root" / "users"
        make_instance(self.source)
        self.manifest = transfer.build_manifest(self.source)

    def tearDown(self):
        for info in transfer.list_sessions(self.store):
            transfer.forget_session(info["id"])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def begin_upload(self, **kwargs):
        return transfer.UploadSession.begin(self.store, self.target, self.manifest,
                                            instance_id="i1", reserve=0, **kwargs)


class TestManifest(Basis):
    def test_manifest_zaehlt_und_hasht(self):
        self.assertEqual(self.manifest["file_count"], 8)
        gesamt = sum(p.stat().st_size for p in self.source.rglob("*") if p.is_file())
        self.assertEqual(self.manifest["total_bytes"], gesamt)
        index = transfer.manifest_index(self.manifest)
        self.assertIn("plugins/EssentialsX.jar", index)
        echt = hashlib.sha256((self.source / "welt" / "level.dat").read_bytes()).hexdigest()
        self.assertEqual(index["welt/level.dat"]["sha256"], echt)
        self.assertEqual(index["welt/leer.dat"]["sha256"], transfer.EMPTY_SHA256)

    def test_manifest_ueberspringt_verwaltung(self):
        write(self.source / ".mcsm" / "intern.json", b"{}")
        write(self.source / "welt" / "level.dat.mcsmpart", b"halb")
        neu = transfer.build_manifest(self.source)
        namen = [e["path"] for e in neu["files"]]
        self.assertNotIn(".mcsm/intern.json", namen)
        self.assertNotIn("welt/level.dat.mcsmpart", namen)

    def test_pruefung_von_aussen(self):
        geprueft = transfer.check_manifest(self.manifest)
        self.assertEqual(geprueft["file_count"], self.manifest["file_count"])
        schlecht = [
            {},
            {"files": []},
            {"files": [{"path": "../geheim", "size": 1, "sha256": "a" * 64}]},
            {"files": [{"path": "/etc/passwd", "size": 1, "sha256": "a" * 64}]},
            {"files": [{"path": "a.txt", "size": -1, "sha256": "a" * 64}]},
            {"files": [{"path": "a.txt", "size": 1, "sha256": "kurz"}]},
            {"files": [{"path": "a.txt", "size": 1}]},
            {"files": [{"path": "a.txt", "size": 1, "sha256": "a" * 64},
                       {"path": "a.txt", "size": 2, "sha256": "b" * 64}]},
            {"files": [{"path": "gross.bin", "size": transfer.MAX_FILE_BYTES + 1,
                        "sha256": "a" * 64}]},
            {"files": [{"path": ".mcsm/x", "size": 1, "sha256": "a" * 64}]},
        ]
        for obj in schlecht:
            with self.subTest(obj=obj):
                with self.assertRaises(ValueError):
                    transfer.check_manifest(obj)

    def test_leere_datei_ohne_pruefsumme_erlaubt(self):
        geprueft = transfer.check_manifest({"files": [{"path": "leer.txt", "size": 0}]})
        self.assertEqual(geprueft["files"][0]["sha256"], transfer.EMPTY_SHA256)

    def test_vergleich(self):
        bericht = transfer.compare_manifests(self.manifest, self.manifest)
        self.assertTrue(bericht["ok"])
        kaputt = {"files": [dict(e) for e in self.manifest["files"][:-1]]}
        bericht = transfer.compare_manifests(self.manifest, kaputt)
        self.assertFalse(bericht["ok"])
        self.assertEqual(bericht["missing_count"], 1)

    def test_groesse_und_platz(self):
        self.assertEqual(transfer.dir_size(self.source), self.manifest["total_bytes"])
        self.assertEqual(transfer.dir_stats(self.source)["files"], 8)
        self.assertGreater(transfer.free_space(self.tmp), 0)
        transfer.check_space(self.tmp, 1024, reserve=0)
        with self.assertRaises(ValueError) as fehler:
            transfer.check_space(self.tmp, 10 ** 15, reserve=transfer.RESERVE_BYTES)
        self.assertIn("Platz", str(fehler.exception))


class TestUpload(Basis):
    def test_ganzer_upload(self):
        session = self.begin_upload()
        self.assertFalse(session.complete())
        push(session, self.source)
        bericht = session.finish()
        self.assertTrue(bericht["ok"])
        self.assertEqual(bericht["files"], 8)
        self.assertEqual(session.state, "done")
        pruefung = transfer.verify(self.target, self.manifest)
        self.assertTrue(pruefung["ok"])
        self.assertEqual((self.target / "welt" / "region" / "r.0.0.mca").read_bytes(),
                         (self.source / "welt" / "region" / "r.0.0.mca").read_bytes())
        self.assertTrue((self.target / "welt" / "leer.dat").is_file())
        self.assertFalse(list(self.target.rglob("*" + transfer.PART_SUFFIX)))

    def test_platz_wird_vorher_geprueft(self):
        with self.assertRaises(ValueError):
            transfer.UploadSession.begin(self.store, self.target, self.manifest,
                                         reserve=10 ** 15)

    def test_abbruch_und_fortsetzen(self):
        session = self.begin_upload()
        push(session, self.source, chunk=4096, stop_after=3)
        offen_vorher = session.status()["received_bytes"]
        self.assertGreater(offen_vorher, 0)
        self.assertFalse(session.complete())
        # Verbindung weg: Sitzung aus dem Zwischenspeicher nehmen und neu laden
        transfer.forget_session(session.id)
        wieder = transfer.open_session(self.store, session.id)
        self.assertEqual(wieder.status()["received_bytes"], offen_vorher)
        push(wieder, self.source, chunk=4096)
        self.assertTrue(wieder.finish()["ok"])

    def test_zweiter_versuch_ueberspringt_fertige_dateien(self):
        session = self.begin_upload()
        push(session, self.source, chunk=100_000, stop_after=4)
        fertig = session.status()["done_files"]
        self.assertGreater(fertig, 0)
        session.abort()
        self.assertFalse(list(self.target.rglob("*" + transfer.PART_SUFFIX)))
        neu = self.begin_upload()
        self.assertGreaterEqual(neu.status()["skipped_files"], fertig)
        self.assertEqual(neu.status()["done_files"], neu.status()["skipped_files"])
        push(neu, self.source)
        self.assertTrue(neu.finish()["ok"])

    def test_falscher_versatz(self):
        session = self.begin_upload()
        with self.assertRaises(ValueError) as fehler:
            session.write_chunk("server.properties", 5, b"abc")
        self.assertIn("Byte 0", str(fehler.exception))

    def test_zu_viele_daten(self):
        session = self.begin_upload()
        with self.assertRaises(ValueError):
            session.write_chunk("server.properties", 0, b"x" * 100_000)

    def test_unbekannte_datei_und_ausbruch(self):
        session = self.begin_upload()
        for bad in ("gibtsnicht.txt", "../geheim.txt", "/etc/passwd", ".mcsm/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    session.write_chunk(bad, 0, b"x")

    def test_pruefsummenfehler(self):
        gefaelscht = {"version": 1, "files": [dict(e) for e in self.manifest["files"]]}
        for entry in gefaelscht["files"]:
            if entry["path"] == "server.properties":
                entry["sha256"] = "f" * 64
        session = transfer.UploadSession.begin(self.store, self.target, gefaelscht, reserve=0)
        inhalt = (self.source / "server.properties").read_bytes()
        with self.assertRaises(ValueError) as fehler:
            session.write_chunk("server.properties", 0, inhalt)
        self.assertIn("Prüfsumme", str(fehler.exception))
        # Teil-Datei ist weg, die Datei beginnt wieder bei Null und wird nicht abgelegt
        self.assertFalse((self.target / ("server.properties" + transfer.PART_SUFFIX)).exists())
        self.assertFalse((self.target / "server.properties").exists())
        offen = [m for m in session.missing() if m["path"] == "server.properties"]
        self.assertEqual(offen[0]["offset"], 0)

    def test_abschluss_erst_wenn_alles_da_ist(self):
        session = self.begin_upload()
        push(session, self.source, stop_after=1)
        with self.assertRaises(ValueError) as fehler:
            session.finish()
        self.assertIn("noch nicht fertig", str(fehler.exception))

    def test_doppeltes_stueck_wird_abgelehnt(self):
        session = self.begin_upload()
        inhalt = (self.source / "server.properties").read_bytes()
        session.write_chunk("server.properties", 0, inhalt)
        with self.assertRaises(ValueError):
            session.write_chunk("server.properties", 0, inhalt)

    def test_leeres_stueck(self):
        session = self.begin_upload()
        with self.assertRaises(ValueError):
            session.write_chunk("server.properties", 0, b"")

    def test_sitzungskennung_geprueft(self):
        for bad in ("", "../../etc/passwd", "ABC", "x" * 32, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    transfer.open_session(self.store, bad)
        with self.assertRaises(ValueError):
            transfer.open_session(self.store, transfer.new_id())


class TestDownload(Basis):
    def setUp(self):
        super().setUp()
        self.zurueck = self.tmp / "pc" / "zurueckgeholt"

    def fetch(self, rel: str, offset: int, length: int) -> bytes:
        """Spielt die Antwort des Root-Servers (dort liegt die Instanz in self.source)."""
        return transfer.read_chunk(self.source, rel, offset, length)

    def test_download_laeuft_durch(self):
        session = transfer.DownloadSession.begin(self.store, self.zurueck, self.manifest,
                                                 reserve=0)
        bericht = session.run(self.fetch, chunk_size=16384)
        self.assertTrue(bericht["ok"])
        self.assertTrue(transfer.verify(self.zurueck, self.manifest)["ok"])

    def test_download_nach_abbruch_fortsetzen(self):
        session = transfer.DownloadSession.begin(self.store, self.zurueck, self.manifest,
                                                 reserve=0)
        zaehler = {"n": 0}

        def wackelig(rel, offset, length):
            zaehler["n"] += 1
            if zaehler["n"] == 3:
                raise OSError("Verbindung weg")
            return self.fetch(rel, offset, length)

        with self.assertRaises(OSError):
            session.run(wackelig, chunk_size=8192)
        self.assertFalse(session.complete())
        transfer.forget_session(session.id)
        wieder = transfer.open_session(self.store, session.id)
        self.assertTrue(wieder.run(self.fetch, chunk_size=8192)["ok"])
        self.assertTrue(transfer.verify(self.zurueck, self.manifest)["ok"])

    def test_read_chunk_grenzen(self):
        self.assertEqual(len(transfer.read_chunk(self.source, "welt/level.dat", 0, 100)), 100)
        self.assertEqual(transfer.read_chunk(self.source, "welt/level.dat", 99999, 100), b"")
        for args in (("../geheim.txt", 0, 10), ("welt/level.dat", -1, 10),
                     ("welt/level.dat", 0, 0),
                     ("welt/level.dat", 0, transfer.MAX_CHUNK_BYTES + 1),
                     ("gibtsnicht", 0, 10)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    transfer.read_chunk(self.source, *args)


class TestRueckholung(Basis):
    def test_bestaetigung_und_loeschen(self):
        # Instanz liegt auf dem Root (self.target), wird auf den PC zurückgeholt
        session = self.begin_upload()
        push(session, self.source)
        session.finish()
        root_manifest = transfer.build_manifest(self.target)
        zurueck = self.tmp / "pc" / "zurueck"
        empfang = transfer.DownloadSession.begin(self.store, zurueck, root_manifest, reserve=0)

        # Erst unvollständig: das Löschen muss abgelehnt werden
        with self.assertRaises(ValueError):
            transfer.confirm_pull(zurueck, root_manifest)

        empfang.run(lambda rel, off, ln: transfer.read_chunk(self.target, rel, off, ln))
        bericht = transfer.confirm_pull(zurueck, root_manifest)
        self.assertTrue(bericht["ok"])
        weg = transfer.delete_tree(self.target, self.data_root)
        self.assertTrue(weg["deleted"])
        self.assertFalse(self.target.exists())

    def test_verify_erkennt_fehler(self):
        session = self.begin_upload()
        push(session, self.source)
        session.finish()
        bericht = transfer.verify(self.target, self.manifest)
        self.assertTrue(bericht["ok"])
        # eine Datei verstümmeln, eine löschen
        with (self.target / "welt" / "region" / "r.0.0.mca").open("r+b") as fh:
            fh.truncate(1000)
        (self.target / "logs" / "latest.log").unlink()
        bericht = transfer.verify(self.target, self.manifest)
        self.assertFalse(bericht["ok"])
        self.assertEqual(bericht["missing"], ["logs/latest.log"])
        self.assertEqual(bericht["wrong_size"], ["welt/region/r.0.0.mca"])
        with self.assertRaises(ValueError):
            transfer.confirm_pull(self.target, self.manifest)

    def test_verify_erkennt_falschen_inhalt(self):
        session = self.begin_upload()
        push(session, self.source)
        session.finish()
        ziel = self.target / "welt" / "level.dat"
        ziel.write_bytes(os.urandom(4096))                 # gleiche Größe, anderer Inhalt
        bericht = transfer.verify(self.target, self.manifest)
        self.assertFalse(bericht["ok"])
        self.assertEqual(bericht["wrong_hash"], ["welt/level.dat"])
        self.assertTrue(transfer.verify(self.target, self.manifest, hashes=False)["ok"])

    def test_zusaetzliche_dateien_stoeren_nicht(self):
        session = self.begin_upload()
        push(session, self.source)
        session.finish()
        write(self.target / "plugins" / "neu.jar", b"neu")
        bericht = transfer.verify(self.target, self.manifest)
        self.assertTrue(bericht["ok"])
        self.assertEqual(bericht["extra"], ["plugins/neu.jar"])

    def test_loeschen_nur_im_datenbereich(self):
        for bad in (self.data_root, self.tmp, self.tmp / ".." / "irgendwo"):
            with self.subTest(bad=str(bad)):
                with self.assertRaises(ValueError):
                    transfer.delete_tree(bad, self.data_root)
        fehlt = transfer.delete_tree(self.data_root / "u9" / "i9", self.data_root)
        self.assertFalse(fehlt["deleted"])


class TestAufraeumen(Basis):
    def test_alte_sitzungen_verschwinden(self):
        session = self.begin_upload()
        push(session, self.source, stop_after=2)
        sitzungen = transfer.list_sessions(self.store)
        self.assertEqual(len(sitzungen), 1)
        self.assertEqual(sitzungen[0]["state"], "open")
        self.assertEqual(transfer.cleanup_old(self.store), [])
        # Sitzung künstlich altern lassen
        session.data["updated_at"] = 1
        session.save()
        session.data["updated_at"] = 1
        transfer._write_json(session.dir / transfer.SESSION_FILE, session.data)
        transfer.forget_session(session.id)
        self.assertEqual(transfer.cleanup_old(self.store), [session.id])
        self.assertEqual(transfer.list_sessions(self.store), [])
        self.assertFalse(list(self.target.rglob("*" + transfer.PART_SUFFIX)))


class TestGrenzenUndPlatz(Basis):
    """Grenzen, die verhindern, dass ein Konto den Daemon oder die Platte aufbraucht."""

    def test_offene_sitzungen_je_konto_begrenzt(self):
        offen = []
        for i in range(transfer.MAX_OPEN_SESSIONS_PER_USER):
            ziel = self.tmp / "root" / "users" / "u1" / f"viele{i}"
            offen.append(transfer.UploadSession.begin(self.store, ziel, self.manifest,
                                                      user_id="konto-a", instance_id=f"v{i}",
                                                      reserve=0))
        with self.assertRaises(ValueError) as fehler:
            transfer.UploadSession.begin(self.store, self.tmp / "root" / "users" / "u1" / "zuviel",
                                         self.manifest, user_id="konto-a", instance_id="zuviel",
                                         reserve=0)
        self.assertIn("Übertragungen für dein Konto", str(fehler.exception))
        # Ein anderes Konto ist davon nicht betroffen.
        transfer.UploadSession.begin(self.store, self.tmp / "root" / "users" / "u2" / "i1",
                                     self.manifest, user_id="konto-b", instance_id="b1",
                                     reserve=0)
        # Nach dem Verwerfen ist wieder Platz.
        offen[0].discard()
        transfer.UploadSession.begin(self.store, self.tmp / "root" / "users" / "u1" / "wieder",
                                     self.manifest, user_id="konto-a", instance_id="wieder",
                                     reserve=0)

    def test_verworfene_sitzung_ist_restlos_weg(self):
        session = self.begin_upload(user_id="konto-a")
        push(session, self.source, stop_after=2)
        session.abort()
        session.discard()
        self.assertEqual(transfer.list_sessions(self.store), [])
        self.assertNotIn(session.id, transfer._sessions)
        self.assertFalse(session.dir.exists())
        self.assertFalse(list(self.target.rglob("*" + transfer.PART_SUFFIX)))

    def test_platzpruefung_rechnet_offene_sitzungen_mit(self):
        """Drei gleichzeitige Uploads dürfen die Reserve nicht gemeinsam aufbrauchen."""
        gesamt = int(self.manifest["total_bytes"])
        frei = {"wert": gesamt * 2 + transfer.RESERVE_BYTES + 1000}
        echt = transfer.free_space
        transfer.free_space = lambda pfad: frei["wert"]              # type: ignore[assignment]
        try:
            transfer.UploadSession.begin(self.store, self.tmp / "root" / "users" / "u1" / "a",
                                         self.manifest, user_id="k", instance_id="a")
            transfer.UploadSession.begin(self.store, self.tmp / "root" / "users" / "u1" / "b",
                                         self.manifest, user_id="k", instance_id="b")
            with self.assertRaises(ValueError) as fehler:
                transfer.UploadSession.begin(self.store, self.tmp / "root" / "users" / "u1" / "c",
                                             self.manifest, user_id="k", instance_id="c")
            self.assertIn("Reserve", str(fehler.exception))
        finally:
            transfer.free_space = echt                              # type: ignore[assignment]

    def test_manifest_meldet_nicht_uebertragbare_dateien(self):
        """Was nicht ins Manifest passt, muss gemeldet werden – sonst löscht es der Daemon ungesehen."""
        write(self.source / "plugins" / ("x" * 210 + ".yml"), b"zu langer Name")
        bericht = transfer.build_manifest(self.source)
        uebersprungen = [e["path"] for e in bericht["skipped"]]
        self.assertTrue(any("x" * 210 in p for p in uebersprungen), uebersprungen)
        self.assertTrue(all("x" * 210 not in e["path"] for e in bericht["files"]))
        self.assertTrue(bericht["skipped"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
