"""Selbsttests für die Übertragungswege des Daemons: Pakete und das Beschaffen danach.

Die Prüf-Umgebung (HTTP-Dienst auf 127.0.0.1, eigene Datenwurzel, kein Netz) kommt aus
``test_daemon``; hier stehen nur die Tests zu ``upload/bundle``, ``download/bundle`` und zu dem
Vorgang, der nach einer Übertragung den Serverkern nachlädt.

    python -m unittest discover -s hosted/tests
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tarfile
import time
import unittest

_TESTS = pathlib.Path(__file__).resolve().parent
for _ordner in (_TESTS, _TESTS.parent):
    if str(_ordner) not in sys.path:
        sys.path.insert(0, str(_ordner))

import mcsmd                                                        # noqa: E402
from test_daemon import Basis, sha256_of                            # noqa: E402


def tar_paket(dateien: dict) -> bytes:
    puffer = io.BytesIO()
    with tarfile.open(fileobj=puffer, mode="w", format=tarfile.PAX_FORMAT,
                      encoding="utf-8") as archiv:
        for name, inhalt in dateien.items():
            info = tarfile.TarInfo(name)
            info.size = len(inhalt)
            info.mtime = 1758790000
            archiv.addfile(info, io.BytesIO(inhalt))
    return puffer.getvalue()


class PaketTest(Basis):
    """Viele kleine Dateien in einer Anfrage – in beide Richtungen."""

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.server = self.make_server(self.token)
        self.folder = mcsmd.sources_linux.users_dir() / self.uid / self.server["id"]

    def manifest(self, entries: dict) -> dict:
        return {"version": 1,
                "files": [{"path": p, "size": len(d), "sha256": sha256_of(d)}
                          for p, d in entries.items()]}

    def beispiel(self) -> dict:
        return {f"plugins/config/datei-{i}.yml": (f"wert: {i}\n" * (i + 1)).encode("utf-8")
                for i in range(12)}

    def test_paket_hochladen(self):
        self.set_state(self.server["id"], "local_only")
        dateien = self.beispiel()
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest(dateien), "purpose": "instance"},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        sitzung = data["session"]
        self.assertTrue(sitzung["bundle"])
        self.assertEqual(sitzung["bundle_small_bytes"], mcsmd.transfer.BUNDLE_SMALL_BYTES)

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/bundle",
                                 raw=tar_paket(dateien), token=self.token,
                                 headers={"X-MCSM-Session": sitzung["id"]})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["files"], len(dateien))
        self.assertTrue(data["complete"])
        for name, inhalt in dateien.items():
            self.assertEqual((self.folder / name).read_bytes(), inhalt)

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/finish",
                                 {"session": sitzung["id"]}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertIsNone(data["job"])          # ohne Versionsangabe wird nichts nachgeladen

    def test_paket_mit_ausbruch_wird_abgelehnt(self):
        self.set_state(self.server["id"], "local_only")
        dateien = self.beispiel()
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest(dateien), "purpose": "instance"},
                                 token=self.token)
        sitzung = data["session"]["id"]
        boese = tar_paket({"../../geklaut.txt": b"weg damit"})
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/bundle",
                                 raw=boese, token=self.token,
                                 headers={"X-MCSM-Session": sitzung})
        self.assertEqual(status, 400, data)
        self.assertFalse((self.folder.parent.parent / "geklaut.txt").exists())

    def test_paket_gehoert_zur_eigenen_sitzung(self):
        self.set_state(self.server["id"], "local_only")
        dateien = self.beispiel()
        self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                  {"manifest": self.manifest(dateien), "purpose": "instance"}, token=self.token)
        fremd, _uid = self.make_user("Mara")
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/bundle",
                                 raw=tar_paket(dateien), token=fremd,
                                 headers={"X-MCSM-Session": "0" * 32})
        self.assertIn(status, (403, 404), data)

    def test_status_nennt_mehr_offene_dateien(self):
        self.set_state(self.server["id"], "local_only")
        dateien = {f"plugins/config/d{i}.yml": f"{i}\n".encode("utf-8") for i in range(60)}
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest(dateien), "purpose": "instance"},
                                 token=self.token)
        sitzung = data["session"]["id"]
        status, data = self.call(
            "GET", f"/api/servers/{self.server['id']}/upload/status?session={sitzung}&missing=200",
            token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["session"]["missing"]), 60)

    def test_paket_herunterladen(self):
        self.set_state(self.server["id"], "hosted")
        dateien = self.beispiel()
        for name, inhalt in dateien.items():
            ziel = self.folder / name
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(inhalt)
        self.call("POST", f"/api/servers/{self.server['id']}/pull", {}, token=self.token)
        status, roh, kopf = self.call_bytes(
            "POST", f"/api/servers/{self.server['id']}/download/bundle",
            {"paths": list(dateien)}, token=self.token)
        self.assertEqual(status, 200, roh)
        self.assertEqual(int(kopf.get("X-MCSM-Count") or 0), len(dateien))
        with tarfile.open(fileobj=io.BytesIO(roh), mode="r|") as archiv:
            gelesen = {m.name: archiv.extractfile(m).read() for m in archiv}
        self.assertEqual(gelesen, dateien)

    def call_bytes(self, method: str, path: str, body=None, token: str = ""):
        """Wie `call`, liefert aber die rohen Bytes und die Kopfzeilen (tar-Antwort)."""
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self.port}{path}"
        daten = json.dumps(body).encode("utf-8") if body is not None else None
        kopf = {"Content-Type": "application/json"} if daten else {}
        if token:
            kopf["Authorization"] = f"Bearer {token}"
        anfrage = urllib.request.Request(url, data=daten, method=method, headers=kopf)
        try:
            with urllib.request.urlopen(anfrage, timeout=25) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)


class BeschaffenTest(Basis):
    """Nach einer Übertragung holt der Root, was nicht über die Leitung kam."""

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.server = self.make_server(self.token)
        self.folder = mcsmd.sources_linux.users_dir() / self.uid / self.server["id"]
        self.gerufen: dict = {}
        self.echt = {"ensure_java": mcsmd.sources_linux.ensure_java,
                     "install_paper": mcsmd.sources_linux.install_paper,
                     "install_crossplay": mcsmd.sources_linux.install_crossplay}

        def ensure_java(major, progress=None, status=None):
            self.gerufen["java"] = int(major)
            return pathlib.Path("/kein/java")

        def install_paper(version, target, eula, progress=None, status=None, jar="server.jar"):
            self.gerufen["paper"] = str(version)
            pfad = pathlib.Path(target) / jar
            pfad.parent.mkdir(parents=True, exist_ok=True)
            pfad.write_bytes(b"Paper")
            return {"version": str(version), "build": "paper-1.jar", "jar": jar,
                    "java_major": 21, "java_flags": []}

        def install_crossplay(target, progress=None, status=None):
            self.gerufen["crossplay"] = True
            return {}

        mcsmd.sources_linux.ensure_java = ensure_java
        mcsmd.sources_linux.install_paper = install_paper
        mcsmd.sources_linux.install_crossplay = install_crossplay

    def tearDown(self) -> None:
        for name, fn in self.echt.items():
            setattr(mcsmd.sources_linux, name, fn)
        super().tearDown()

    def test_nach_der_uebertragung_wird_der_kern_geholt(self):
        self.set_state(self.server["id"], "local_only")
        inhalt = b"level-name=welt\n"
        manifest = {"version": 1, "files": [{"path": "server.properties", "size": len(inhalt),
                                             "sha256": sha256_of(inhalt)}]}
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": manifest, "purpose": "instance"}, token=self.token)
        sitzung = data["session"]["id"]
        self.call("POST", f"/api/servers/{self.server['id']}/upload/chunk", raw=inhalt,
                  token=self.token, headers={"X-MCSM-Session": sitzung,
                                             "X-MCSM-Path": "server.properties",
                                             "X-MCSM-Offset": "0"})
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/finish",
                                 {"session": sitzung, "version": "1.21.1", "geyser": True},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["job"], data)
        ende = time.time() + 20
        stand = {}
        while time.time() < ende:
            status, antwort = self.call("GET", f"/api/jobs/{data['job']['id']}", token=self.token)
            stand = antwort.get("job") or {}
            if stand.get("state") != "läuft":
                break
            time.sleep(0.1)
        self.assertEqual(stand.get("state"), "fertig", stand)
        # Dieselbe Version wie auf dem PC – und die Einstellungen wurden nicht überschrieben.
        self.assertEqual(self.gerufen.get("paper"), "1.21.1")
        self.assertTrue(self.gerufen.get("crossplay"))
        self.assertEqual((self.folder / "server.properties").read_bytes(), inhalt)
        self.assertTrue((self.folder / "server.jar").is_file())


class ModpackBeschaffenTest(Basis):
    """Ein eigenes Modpack (Fabric) bekommt den Fabric-Launcher – nicht Paper, kein Crossplay."""

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.server = self.make_server(self.token, name="Modpack-A", flavor="fabric")
        self.folder = mcsmd.sources_linux.users_dir() / self.uid / self.server["id"]
        self.gerufen: dict = {}
        self.echt = {"ensure_java": mcsmd.sources_linux.ensure_java,
                     "install_paper": mcsmd.sources_linux.install_paper,
                     "install_fabric": mcsmd.sources_linux.install_fabric,
                     "install_crossplay": mcsmd.sources_linux.install_crossplay}

        def ensure_java(major, progress=None, status=None):
            self.gerufen["java"] = int(major)
            return pathlib.Path("/kein/java")

        def install_paper(version, target, eula, progress=None, status=None, jar="server.jar"):
            self.gerufen["paper"] = str(version)       # darf nie gerufen werden
            return {}

        def install_fabric(version, target, eula, progress=None, status=None,
                           jar="server.jar", loader_version=""):
            self.gerufen["fabric"] = str(version)
            pfad = pathlib.Path(target) / jar
            pfad.parent.mkdir(parents=True, exist_ok=True)
            pfad.write_bytes(b"Fabric-Launcher")
            return {"version": str(version), "jar": jar, "loader": "fabric",
                    "loader_version": "0.17.0", "java_major": 21, "java_flags": []}

        def install_crossplay(target, progress=None, status=None):
            self.gerufen["crossplay"] = True           # darf nie gerufen werden
            return {}

        mcsmd.sources_linux.ensure_java = ensure_java
        mcsmd.sources_linux.install_paper = install_paper
        mcsmd.sources_linux.install_fabric = install_fabric
        mcsmd.sources_linux.install_crossplay = install_crossplay

    def tearDown(self) -> None:
        for name, fn in self.echt.items():
            setattr(mcsmd.sources_linux, name, fn)
        super().tearDown()

    def test_modpack_bekommt_fabric_statt_paper(self):
        self.set_state(self.server["id"], "local_only")
        inhalt = b"level-name=welt\n"
        manifest = {"version": 1, "files": [{"path": "server.properties", "size": len(inhalt),
                                             "sha256": sha256_of(inhalt)}]}
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": manifest, "purpose": "instance"}, token=self.token)
        sitzung = data["session"]["id"]
        self.call("POST", f"/api/servers/{self.server['id']}/upload/chunk", raw=inhalt,
                  token=self.token, headers={"X-MCSM-Session": sitzung,
                                             "X-MCSM-Path": "server.properties",
                                             "X-MCSM-Offset": "0"})
        # geyser=True: trotzdem darf ein Modpack kein Crossplay bekommen.
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/finish",
                                 {"session": sitzung, "version": "1.21.11", "geyser": True},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        ende = time.time() + 20
        stand = {}
        while time.time() < ende:
            status, antwort = self.call("GET", f"/api/jobs/{data['job']['id']}", token=self.token)
            stand = antwort.get("job") or {}
            if stand.get("state") != "läuft":
                break
            time.sleep(0.1)
        self.assertEqual(stand.get("state"), "fertig", stand)
        self.assertEqual(self.gerufen.get("fabric"), "1.21.11")
        self.assertIsNone(self.gerufen.get("paper"))
        self.assertIsNone(self.gerufen.get("crossplay"))
        self.assertEqual(self.gerufen.get("java"), 21)
        self.assertTrue((self.folder / "server.jar").is_file())
        info = mcsmd.read_install_info(self.folder)
        self.assertEqual(info.get("loader"), "fabric")
        self.assertEqual(int(info.get("java_major") or 0), 21)
        # Die hochgeladenen Einstellungen bleiben unberührt.
        self.assertEqual((self.folder / "server.properties").read_bytes(), inhalt)


class WebpanelConfigTest(unittest.TestCase):
    """Webpanel-Erkennung und das Schreiben des [webpanel]-Abschnitts (reine Funktionen)."""

    def setUp(self) -> None:
        import tempfile
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "mods").mkdir()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_erkennung_ab_version_1(self) -> None:
        (self.dir / "mods" / "psychohorror-1.0.3.jar").write_bytes(b"x")
        self.assertTrue(mcsmd.has_webpanel_mod(self.dir))

    def test_alte_beta_hat_kein_webpanel(self) -> None:
        (self.dir / "mods" / "psychohorror-0.9.0-beta.1.jar").write_bytes(b"x")
        self.assertFalse(mcsmd.has_webpanel_mod(self.dir))

    def test_section_patch_setzt_werte_und_behaelt_rest(self) -> None:
        cfg = self.dir / "config" / "psychohorror-common.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[general]\n    enabled = true\n\n[webpanel]\n    enabled = false\n"
                       "    port = 8765\n    bindAddress = \"\"\n    publicUrl = \"\"\n"
                       "    sessionHours = 72\n", encoding="utf-8")
        mcsmd.write_webpanel_config(self.dir, 8771, "https://keviony.arcardia-nexus.de")
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("enabled = true", text)
        self.assertIn("port = 8771", text)
        self.assertIn('bindAddress = "127.0.0.1"', text)
        self.assertIn('publicUrl = "https://keviony.arcardia-nexus.de"', text)
        self.assertIn("sessionHours = 72", text)       # übriges Feld bleibt
        self.assertIn("[general]", text)               # anderer Abschnitt bleibt

    def test_section_wird_angelegt_wenn_fehlt(self) -> None:
        cfg = self.dir / "config" / "psychohorror-common.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[general]\n    enabled = true\n", encoding="utf-8")
        mcsmd.write_webpanel_config(self.dir, 8772, "https://x.arcardia-nexus.de")
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("[webpanel]", text)
        self.assertIn("port = 8772", text)
        self.assertIn('bindAddress = "127.0.0.1"', text)


class ServerUpdateTest(Basis):
    """/update hebt einen gehosteten Server auf eine neue Version (Welt/Einstellungen bleiben)."""

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.server = self.make_server(self.token)            # Paper, Version 1.21.1
        self.folder = mcsmd.sources_linux.users_dir() / self.uid / self.server["id"]
        self.gerufen: dict = {}
        self.echt = {"ensure_java": mcsmd.sources_linux.ensure_java,
                     "install_paper": mcsmd.sources_linux.install_paper,
                     "install_crossplay": mcsmd.sources_linux.install_crossplay}

        def ensure_java(major, progress=None, status=None):
            return pathlib.Path("/kein/java")

        def install_paper(version, target, eula, progress=None, status=None, jar="server.jar"):
            self.gerufen["paper"] = str(version)
            pfad = pathlib.Path(target) / jar
            pfad.parent.mkdir(parents=True, exist_ok=True)
            pfad.write_bytes(b"Paper")
            return {"version": str(version), "build": "paper-1.jar", "jar": jar,
                    "java_major": 21, "java_flags": []}

        def install_crossplay(target, progress=None, status=None):
            return {}

        mcsmd.sources_linux.ensure_java = ensure_java
        mcsmd.sources_linux.install_paper = install_paper
        mcsmd.sources_linux.install_crossplay = install_crossplay

    def tearDown(self) -> None:
        for name, fn in self.echt.items():
            setattr(mcsmd.sources_linux, name, fn)
        super().tearDown()

    def _warte(self, job_id: str) -> dict:
        ende = time.time() + 20
        while time.time() < ende:
            status, antwort = self.call("GET", f"/api/jobs/{job_id}", token=self.token)
            stand = antwort.get("job") or {}
            if stand.get("state") != "läuft":
                return stand
            time.sleep(0.1)
        return {}

    def test_update_holt_neuen_kern_und_setzt_version(self):
        self.set_state(self.server["id"], "hosted")
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/update",
                                 {"version": "1.21.11", "eula": True}, token=self.token)
        self.assertEqual(status, 202, data)
        stand = self._warte(data["job"]["id"])
        self.assertEqual(stand.get("state"), "fertig", stand)
        self.assertEqual(self.gerufen.get("paper"), "1.21.11")
        _, ansicht = self.call("GET", f"/api/servers/{self.server['id']}", token=self.token)
        self.assertEqual((ansicht.get("server") or {}).get("version"), "1.21.11")

    def test_gleiche_version_abgelehnt(self):
        self.set_state(self.server["id"], "hosted")
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/update",
                                 {"version": "1.21.1", "eula": True}, token=self.token)
        self.assertEqual(status, 400, data)

    def test_ohne_eula_abgelehnt(self):
        self.set_state(self.server["id"], "hosted")
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/update",
                                 {"version": "1.21.11"}, token=self.token)
        self.assertEqual(status, 400, data)

    def test_modpack_abgelehnt(self):
        token, uid = self.make_user("Mara")
        pack = self.make_server(token, name="Modpack-U", flavor="fabric")
        self.set_state(pack["id"], "hosted")
        status, data = self.call("POST", f"/api/servers/{pack['id']}/update",
                                 {"version": "1.21.11", "eula": True}, token=token)
        self.assertEqual(status, 400, data)
        self.assertIn("Modpack", str(data))


if __name__ == "__main__":
    unittest.main()
