"""Selbsttests für den Hosted-Daemon (hosted/mcsmd.py).

Für jeden Test läuft ein echter HTTP-Dienst auf 127.0.0.1 mit einem frischen Temp-Ordner als
Datenwurzel. Es wird kein Minecraft-Server gestartet und nichts aus dem Netz geladen.

    python -m unittest discover -s hosted/tests
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

_HOSTED = pathlib.Path(__file__).resolve().parent.parent
if str(_HOSTED) not in sys.path:
    sys.path.insert(0, str(_HOSTED))

import mcsmd                                                        # noqa: E402

TAG = 86400
_SHA_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def sha256_of(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


class Basis(unittest.TestCase):
    """Ein Dienst je Test, eigene Datenwurzel, kein Netz."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mcsmd-test-")
        root = pathlib.Path(self.tmp)
        self.env_backup = {k: os.environ.get(k) for k in
                           ("MCSM_DATA", "MCSM_DATA_ROOT", "MCSM_LOG", "MCSM_LOG_STDOUT",
                            "MCSM_BIND", "MCSM_PORT", "MCSM_MACHINE_RAM_MB",
                            "MCSM_DISCORD_DIR", "MCSM_DOMAIN", "MCSM_ROUTES",
                            "MCSM_ADMIN_HOST", "MCSM_API_HOST", "MCSM_COMPANION_JAR")}
        os.environ["MCSM_DATA_ROOT"] = str(root)
        os.environ["MCSM_DATA"] = str(root / "data")
        os.environ["MCSM_DOMAIN"] = "arcardia-nexus.de"
        os.environ["MCSM_ROUTES"] = str(root / "data" / "routes.json")
        # Discord-Zugangsdaten bewusst in einen leeren Ordner: ohne sie antwortet nur die
        # Anmeldung über Discord mit 501, der Rest des Dienstes läuft normal weiter.
        os.environ["MCSM_DISCORD_DIR"] = str(root / "discord")
        os.environ["MCSM_LOG"] = str(root / "api.log")
        os.environ["MCSM_LOG_STDOUT"] = "0"
        os.environ["MCSM_BIND"] = "127.0.0.1"
        os.environ["MCSM_PORT"] = "0"

        # Zustand im Speicher zurücksetzen (Ports ohne Bind-Versuch: unabhängig von der Maschine)
        mcsmd.POOL = mcsmd.ports.PortPool(probe=False)
        mcsmd.REGISTRY = mcsmd.runner.RunnerRegistry(on_exit=mcsmd.on_runner_exit)
        mcsmd.XBOX = mcsmd.xbox.BroadcasterRegistry()
        mcsmd.JOBS.clear()
        mcsmd._runtime.update({"ports": [], "warned": {}, "last_expiry_check": 0,
                               "housekeeping_at": 0, "save_all_at": 0, "admin_invite": "",
                               "started_at": 0, "woken": {}, "routen_stand": None,
                               "widerruf_gewarnt": {}})
        # Ratenbremse und angefangene Anmeldungen zurücksetzen: alle Tests kommen aus derselben
        # Rückschleife und würden sich sonst gegenseitig aussperren.
        mcsmd.BREMSE.leeren()
        mcsmd._LOGINS.clear()
        mcsmd.oauth.alles_leeren()
        mcsmd.oauth.vergessen()
        mcsmd.startup()

        self.httpd = mcsmd.build_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.admin_token = self.make_admin()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(10)
        mcsmd.close_log()
        for key, value in self.env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- Hilfen --------------------------------------------------------
    def call(self, method: str, path: str, body=None, token: str = "", raw: bytes | None = None,
             headers: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        head = {}
        if raw is not None:
            data = raw
            head["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
        if token:
            head["Authorization"] = f"Bearer {token}"
        head.update(headers or {})
        request = urllib.request.Request(url, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(request, timeout=25) as resp:
                return resp.status, self._body(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, self._body(exc)

    def call_full(self, method: str, path: str, body=None, token: str = "",
                  headers: dict | None = None):
        """Wie `call`, liefert aber zusätzlich die Kopfzeilen der Antwort."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        head = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
        if token:
            head["Authorization"] = f"Bearer {token}"
        head.update(headers or {})
        request = urllib.request.Request(url, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(request, timeout=25) as resp:
                return resp.status, self._body(resp), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, self._body(exc), dict(exc.headers)

    @staticmethod
    def _body(resp):
        try:
            raw = resp.read()
            kind = (resp.headers.get("Content-Type") or "")
        finally:
            resp.close()
        if "json" in kind:
            return json.loads(raw.decode("utf-8"))
        return raw

    def admin_code(self) -> str:
        path = mcsmd.store_hosted.data_dir() / mcsmd.ADMIN_FILE
        text = path.read_text(encoding="utf-8")
        found = re.search(r"Einladungscode:\s*([A-Z0-9-]+)", text)
        self.assertIsNotNone(found, "Der Einladungscode steht nicht in der Datei.")
        return found.group(1)

    def make_admin(self) -> str:
        self.first_code = self.admin_code()          # die Datei verschwindet danach
        status, data = self.call("POST", "/api/auth/invite",
                                 {"code": self.first_code, "name": "Betreiber"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["user"]["role"], "admin")
        return data["token"]

    def make_user(self, name: str = "Luca") -> tuple:
        status, data = self.call("POST", "/api/admin/invites", {"uses_max": 1, "days": 7},
                                 token=self.admin_token)
        self.assertEqual(status, 201, data)
        code = data["invite"]["code"]
        status, data = self.call("POST", "/api/auth/invite", {"code": code, "name": name})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["user"]["role"], "user")
        return data["token"], data["user"]["id"]

    def make_server(self, token: str, name: str = "Welt-A", **extra):
        payload = {"name": name, "type": "java", "flavor": "paper", "version": "1.21.1",
                   "ram_mb": 2048}
        payload.update(extra)
        status, data = self.call("POST", "/api/servers", payload, token=token)
        self.assertEqual(status, 201, data)
        return data["server"]

    def set_state(self, instance_id: str, state: str):
        status, data = self.call("POST", f"/api/admin/instances/{instance_id}/state",
                                 {"state": state}, token=self.admin_token)
        self.assertEqual(status, 200, data)
        return data["server"]

    def issue_pass(self, user_id: str, *, kind: str = "local", days: int = 30,
                   max_concurrent: int = 2, ram_total_mb: int = 8192):
        status, data = self.call("POST", "/api/admin/passes",
                                 {"user_id": user_id, "kind": kind, "days": days,
                                  "max_concurrent": max_concurrent,
                                  "ram_total_mb": ram_total_mb},
                                 token=self.admin_token)
        self.assertEqual(status, 201, data)
        return data["pass"]


# ---------------------------------------------------------------- Anmeldung und Rechte

class AnmeldungTest(Basis):
    def test_health_ohne_token(self):
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "mcsmd")

    def test_unbekannte_adresse(self):
        status, data = self.call("GET", "/api/gibtsnicht")
        self.assertEqual(status, 404)
        self.assertIn("gibt es", data["error"])

    def test_falsche_methode(self):
        status, data = self.call("GET", "/api/auth/invite")
        self.assertEqual(status, 405)
        self.assertIn("GET", data["error"])

    def test_ohne_token_401(self):
        status, data = self.call("GET", "/api/me")
        self.assertEqual(status, 401)
        self.assertIn("Authorization", data["error"])

    def test_falsches_token_401(self):
        status, data = self.call("GET", "/api/me", token="völlig-erfunden")
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "Bitte neu anmelden.")

    def test_erster_admin_datei_verschwindet(self):
        self.assertFalse((mcsmd.store_hosted.data_dir() / mcsmd.ADMIN_FILE).exists())

    def test_zweiter_code_macht_keinen_admin(self):
        token, _uid = self.make_user()
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["user"]["role"], "user")

    def test_code_zweimal_einloesen(self):
        status, data = self.call("POST", "/api/auth/invite",
                                 {"code": self.first_code, "name": "Zweiter"})
        self.assertEqual(status, 400)
        self.assertIn("aufgebraucht", data["error"])

    def test_abmelden(self):
        token, _uid = self.make_user()
        status, _ = self.call("POST", "/api/auth/logout", {}, token=token)
        self.assertEqual(status, 200)
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(status, 401)

    def test_discord_ohne_zugangsdaten(self):
        """Die Routen sind verdrahtet; ohne Zugangsdaten sagen sie das in einem deutschen Satz.

        Wichtig: der Dienst läuft trotzdem, die Anmeldung mit Einladungscode bleibt möglich –
        und das Anwendungsgeheimnis steht in keiner Meldung.
        """
        for path in ("/api/auth/discord/start", "/auth/discord/start"):
            status, data = self.call("GET", path)
            self.assertEqual(status, 501, path)
            self.assertIn("nicht eingerichtet", data["error"])
        status, data = self.call("GET", "/api/auth/discord/callback?state=x&code=y")
        self.assertIn(status, (400, 501), data)

    def test_kaputtes_json(self):
        token, _uid = self.make_user()
        status, data = self.call("POST", "/api/servers", raw=b"{kein json",
                                 token=token, headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("JSON", data["error"])

    def test_adminroute_nur_fuer_admin(self):
        token, _uid = self.make_user()
        for path in ("/api/admin/users", "/api/admin/passes", "/api/admin/status"):
            status, data = self.call("GET", path, token=token)
            self.assertEqual(status, 403, path)
            self.assertIn("Betreiber", data["error"])

    def test_protokoll_ohne_token(self):
        token, _uid = self.make_user()
        self.call("GET", "/api/me", token=token)
        # Der Dienst schreibt die Zugriffszeile **nach** dem Absenden der Antwort. Der Aufrufer
        # kann also schon weiterlaufen, während der Arbeitsfaden noch protokolliert – deshalb
        # kurz darauf warten statt sofort zu lesen.
        text = ""
        ende = time.time() + 5
        while time.time() < ende:
            text = pathlib.Path(os.environ["MCSM_LOG"]).read_text(encoding="utf-8")
            if "GET /api/me -> 200" in text:
                break
            time.sleep(0.05)
        self.assertIn("GET /api/me -> 200", text)
        self.assertNotIn(token, text)


# ---------------------------------------------------------------- Konto und Grenzen

class KontoTest(Basis):
    def test_me_ohne_pass(self):
        token, _uid = self.make_user()
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["limits"]["max_concurrent"], 0)
        self.assertEqual(data["limits_text"], "kein gültiger Pass")
        self.assertEqual(data["slots_free"], 0)
        self.assertIn("machine", data)

    def test_me_mit_pass(self):
        token, uid = self.make_user()
        self.issue_pass(uid, max_concurrent=2, ram_total_mb=8192)
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["limits"]["max_concurrent"], 2)
        self.assertEqual(data["ram_total_mb"], 8192)
        self.assertEqual(len(data["passes"]), 1)
        self.assertEqual(data["passes"][0]["state"], "active")

    def test_pass_grenzen_werden_geprueft(self):
        _token, uid = self.make_user()
        status, data = self.call("POST", "/api/admin/passes",
                                 {"user_id": uid, "kind": "local", "days": 30,
                                  "max_concurrent": 2, "ram_total_mb": 99999},
                                 token=self.admin_token)
        self.assertEqual(status, 400)
        self.assertIn("10240", data["error"])

    def test_konto_sperren_nimmt_sitzungen(self):
        token, uid = self.make_user()
        status, _ = self.call("POST", f"/api/admin/users/{uid}/block",
                              {"blocked": True, "reason": "Zahlung offen"},
                              token=self.admin_token)
        self.assertEqual(status, 200)
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(status, 401)


# ---------------------------------------------------------------- Instanzen

class InstanzTest(Basis):
    def test_anlegen_und_liste(self):
        token, uid = self.make_user()
        server = self.make_server(token)
        self.assertEqual(server["state"], "local_only")
        self.assertEqual(server["origin"], "local")
        folder = mcsmd.sources_linux.users_dir() / uid / server["id"]
        self.assertTrue(folder.is_dir(), "Der Instanzordner wurde nicht angelegt.")
        status, data = self.call("GET", "/api/servers", token=token)
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in data["servers"]], [server["id"]])

    def test_anlegen_ist_unbegrenzt(self):
        token, _uid = self.make_user()
        for i in range(4):
            self.make_server(token, name=f"Welt-{i}")
        status, data = self.call("GET", "/api/servers", token=token)
        self.assertEqual(len(data["servers"]), 4)

    def test_gleicher_name_abgelehnt(self):
        token, _uid = self.make_user()
        self.make_server(token, name="Welt-A")
        status, data = self.call("POST", "/api/servers",
                                 {"name": "welt-a", "version": "1.21.1"}, token=token)
        self.assertEqual(status, 400)
        self.assertIn("schon einen Server", data["error"])

    def test_premium_braucht_premium_pass(self):
        token, uid = self.make_user()
        status, data = self.call("POST", "/api/servers",
                                 {"name": "Root-Welt", "origin": "premium"}, token=token)
        self.assertEqual(status, 400)
        self.assertIn("Premium-Pass", data["error"])
        self.issue_pass(uid, kind="premium")
        status, data = self.call("POST", "/api/servers",
                                 {"name": "Root-Welt", "origin": "premium"}, token=token)
        self.assertEqual(status, 201, data)
        self.assertEqual(data["server"]["state"], "hosted")

    def test_fremder_server_403(self):
        token_a, _uid_a = self.make_user("Luca")
        token_b, _uid_b = self.make_user("Mara")
        server = self.make_server(token_a)
        status, data = self.call("GET", f"/api/servers/{server['id']}", token=token_b)
        self.assertEqual(status, 403)
        self.assertIn("nicht zu deinem Konto", data["error"])
        status, data = self.call("POST", f"/api/servers/{server['id']}/start", token=token_b)
        self.assertEqual(status, 403)

    def test_admin_darf_fremden_server_sehen(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        status, data = self.call("GET", f"/api/servers/{server['id']}", token=self.admin_token)
        self.assertEqual(status, 200, data)

    def test_unbekannter_server_404(self):
        token, _uid = self.make_user()
        status, data = self.call("GET", "/api/servers/abc123abc123", token=token)
        self.assertEqual(status, 404)
        self.assertIn("gibt keinen Server", data["error"])

    def test_einstellungen_aendern(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        status, data = self.call("POST", f"/api/servers/{server['id']}/settings",
                                 {"name": "Neue Welt", "ram_mb": 4096}, token=token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["server"]["name"], "Neue Welt")
        self.assertEqual(data["server"]["ram_mb"], 4096)

    def test_loeschen(self):
        token, uid = self.make_user()
        server = self.make_server(token)
        folder = mcsmd.sources_linux.users_dir() / uid / server["id"]
        status, data = self.call("DELETE", f"/api/servers/{server['id']}", token=token)
        self.assertEqual(status, 200, data)
        self.assertFalse(folder.exists())
        status, _ = self.call("GET", f"/api/servers/{server['id']}", token=token)
        self.assertEqual(status, 404)

    def test_loeschen_auf_dem_root_braucht_force(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        status, data = self.call("DELETE", f"/api/servers/{server['id']}", token=token)
        self.assertEqual(status, 409)
        self.assertIn("force=1", data["error"])
        status, _ = self.call("DELETE", f"/api/servers/{server['id']}?force=1", token=token)
        self.assertEqual(status, 200)


# ---------------------------------------------------------------- Startprüfung

class StartTest(Basis):
    def test_local_only_startet_nicht(self):
        token, uid = self.make_user()
        self.issue_pass(uid)
        server = self.make_server(token)
        status, data = self.call("POST", f"/api/servers/{server['id']}/start", token=token)
        self.assertEqual(status, 409)
        self.assertIn("liegt nur auf deinem PC", data["error"])

    def test_ohne_pass_startet_nicht(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        status, data = self.call("POST", f"/api/servers/{server['id']}/start", token=token)
        self.assertEqual(status, 409)
        self.assertIn("keinen gültigen Pass", data["error"])

    def test_vorgang_gehoert_zum_konto_auch_ohne_instanz(self):
        """Die Rechteprüfung darf nicht ausfallen, wenn der Instanz-Datensatz weg ist."""
        token_a, uid_a = self.make_user("Anna")
        token_b, _uid_b = self.make_user("Bert")
        job = mcsmd.job_new("install", "schon-geloescht", owner=uid_a)
        status, data = self.call("GET", f"/api/jobs/{job['id']}", token=token_b)
        self.assertEqual(status, 403, data)
        status, data = self.call("GET", f"/api/jobs/{job['id']}", token=token_a)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["job"]["id"], job["id"])

    def test_check_start_in_der_uebersicht(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        status, data = self.call("GET", f"/api/servers/{server['id']}", token=token)
        self.assertEqual(status, 200)
        self.assertFalse(data["server"]["check_start"]["ok"])
        self.assertEqual(data["server"]["check_start"]["code"], "zustand")

    def test_stoppen_wenn_nichts_laeuft(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        status, data = self.call("POST", f"/api/servers/{server['id']}/stop", {}, token=token)
        self.assertEqual(status, 409)
        self.assertIn("läuft nicht", data["error"])

    def test_befehl_ohne_server(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        status, data = self.call("POST", f"/api/servers/{server['id']}/command",
                                 {"command": "list"}, token=token)
        self.assertEqual(status, 409)

    def test_konsole_leer(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        status, data = self.call("GET", f"/api/servers/{server['id']}/console", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["lines"], [])
        self.assertEqual(data["next"], 0)


# ---------------------------------------------------------------- Dateien

class DateiTest(Basis):
    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.server = self.make_server(self.token)
        self.set_state(self.server["id"], "hosted")
        self.folder = mcsmd.sources_linux.users_dir() / self.uid / self.server["id"]
        (self.folder / "plugins").mkdir(parents=True, exist_ok=True)
        (self.folder / "server.properties").write_text("max-players=10\nmotd=Test\n",
                                                       encoding="utf-8")
        (self.folder / "plugins" / "Alt.jar").write_bytes(b"alt")
        (self.folder / runner_dir()).mkdir(parents=True, exist_ok=True)
        (self.folder / runner_dir() / "mcsm.pid").write_text("1\n", encoding="utf-8")

    def test_liste_zeigt_keine_verwaltung(self):
        status, data = self.call("GET", f"/api/servers/{self.server['id']}/files",
                                 token=self.token)
        self.assertEqual(status, 200, data)
        names = [e["name"] for e in data["entries"]]
        self.assertIn("plugins", names)
        self.assertIn("server.properties", names)
        self.assertNotIn(".mcsm", names)

    def test_textdatei_lesen_und_schreiben(self):
        path = "server.properties"
        status, data = self.call(
            "GET", f"/api/servers/{self.server['id']}/files?action=read&path={path}",
            token=self.token)
        self.assertEqual(status, 200, data)
        self.assertIn("max-players", data["text"])
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/files",
                                 {"action": "write", "path": path,
                                  "text": "max-players=12\nmotd=Neu\n"}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertIn("max-players=12",
                      (self.folder / "server.properties").read_text(encoding="utf-8"))

    def test_technik_darf_nicht_geschrieben_werden(self):
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/files",
                                 {"action": "write", "path": "server.jar", "text": "x"},
                                 token=self.token)
        self.assertEqual(status, 400)

    def test_verwaltung_ist_unsichtbar(self):
        status, data = self.call(
            "GET", f"/api/servers/{self.server['id']}/files?action=read&path=.mcsm/mcsm.pid",
            token=self.token)
        self.assertEqual(status, 400)

    def test_ausbruch_mit_punkt_punkt(self):
        status, data = self.call(
            "GET", f"/api/servers/{self.server['id']}/files?action=read&path=../../geheim",
            token=self.token)
        self.assertEqual(status, 400)

    def test_plugin_loeschen(self):
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/files",
                                 {"action": "delete", "path": "plugins/Alt.jar"},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertFalse((self.folder / "plugins" / "Alt.jar").exists())

    def test_server_properties_nicht_loeschen(self):
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/files",
                                 {"action": "delete", "path": "server.properties"},
                                 token=self.token)
        self.assertEqual(status, 400)
        self.assertTrue((self.folder / "server.properties").exists())

    def test_ordner_anlegen(self):
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/files",
                                 {"action": "mkdir", "path": "plugins/Essentials"},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertTrue((self.folder / "plugins" / "Essentials").is_dir())


def runner_dir() -> str:
    return mcsmd.runner.MANAGE_DIR


# ---------------------------------------------------------------- Übertragung

class UebertragungTest(Basis):
    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.server = self.make_server(self.token)
        self.folder = mcsmd.sources_linux.users_dir() / self.uid / self.server["id"]

    def manifest(self, entries: dict) -> dict:
        files = []
        for path, data in entries.items():
            files.append({"path": path, "size": len(data), "sha256": sha256_of(data)})
        return {"version": 1, "files": files}

    def test_plugin_hochladen(self):
        self.set_state(self.server["id"], "hosted")
        content = b"Ein Plugin" * 10
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest({"plugins/Neu.jar": content})},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        session = data["session"]["id"]
        self.assertEqual(data["session"]["file_count"], 1)

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/chunk",
                                 raw=content, token=self.token,
                                 headers={"X-MCSM-Session": session,
                                          "X-MCSM-Path": "plugins/Neu.jar",
                                          "X-MCSM-Offset": "0"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["done"])
        self.assertTrue(data["complete"])

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/finish",
                                 {"session": session}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual((self.folder / "plugins" / "Neu.jar").read_bytes(), content)

    def test_chunk_pfad_ist_prozentkodiert(self):
        """Der Kopf X-MCSM-Path kommt prozentkodiert – so schickt ihn das Programm auf dem PC.

        Kopfzeilen tragen kein UTF-8 und kein Leerzeichen sicher. Die Standardwelt von Bedrock
        heißt „Bedrock level“, und im libraries-Baum von Paper steht „…-deprecated+build.21“.
        Ohne Entschlüsseln landete daraus ein Ordner „Bedrock%20level“ – bzw. der Daemon wies
        die Datei als „gehört nicht zu dieser Übertragung“ ab.
        """
        self.set_state(self.server["id"], "local_only")
        heikel = "libraries/net/md-5/bungeecord-chat/1.21-R0.2-deprecated+build.21/chat.jar"
        welt = "Bedrock level/level.dat"
        inhalt = {heikel: b"eine Bibliothek", welt: b"eine Welt"}
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest(inhalt), "purpose": "instance"},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        session = data["session"]["id"]

        for rel, daten in inhalt.items():
            status, data = self.call(
                "POST", f"/api/servers/{self.server['id']}/upload/chunk",
                raw=daten, token=self.token,
                headers={"X-MCSM-Session": session,
                         "X-MCSM-Path": urllib.parse.quote(rel),
                         "X-MCSM-Offset": "0"})
            self.assertEqual(status, 200, data)
            self.assertTrue(data["done"], data)

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/finish",
                                 {"session": session}, token=self.token)
        self.assertEqual(status, 200, data)
        for rel, daten in inhalt.items():
            self.assertEqual((self.folder / rel).read_bytes(), daten)

    def test_chunk_pfad_aus_abfrageparameter(self):
        """Fehlt der Kopf, gilt der Abfrageparameter „path“ (parse_qs entschlüsselt ihn schon)."""
        self.set_state(self.server["id"], "local_only")
        rel = "Bedrock level/level.dat"
        daten = b"Welt ohne Kopfzeile"
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest({rel: daten}), "purpose": "instance"},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        session = data["session"]["id"]

        pfad = (f"/api/servers/{self.server['id']}/upload/chunk"
                f"?session={session}&path={urllib.parse.quote(rel)}&offset=0")
        status, data = self.call("POST", pfad, raw=daten, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["done"], data)
        self.assertEqual((self.folder / rel).read_bytes(), daten)

    def test_release_verweigert_nicht_uebertragbare_dateien(self):
        """Dateien, die kein Manifest abbilden kann, dürfen nicht ungesehen mitgelöscht werden."""
        self.set_state(self.server["id"], "hosted")
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "server.properties").write_bytes(b"level-name=welt\n")
        sperrig = self.folder / ("x" * 210 + ".yml")
        sperrig.write_bytes(b"unter Linux erlaubt, im Manifest nicht")
        self.set_state(self.server["id"], "awaiting_pull")
        hier = mcsmd.transfer.build_manifest(self.folder)
        antrag = {"version": 1, "files": hier["files"]}

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/release",
                                 {"manifest": antrag}, token=self.token)
        self.assertEqual(status, 409, data)
        self.assertIn("nicht auf den PC", data["error"])
        self.assertTrue(sperrig.exists(), "Die Datei muss liegen bleiben.")

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/release",
                                 {"manifest": antrag, "force": True}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertFalse(self.folder.exists())

    def test_abgebrochene_uebertragung_wird_abgeraeumt(self):
        """Nach „abort“ bleibt nichts im Speicher und nichts auf der Platte liegen."""
        self.set_state(self.server["id"], "hosted")
        content = b"Ein Plugin" * 50
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest({"plugins/Halb.jar": content})},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        session = data["session"]["id"]
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/chunk",
                                 raw=content[:100], token=self.token,
                                 headers={"X-MCSM-Session": session,
                                          "X-MCSM-Path": "plugins/Halb.jar",
                                          "X-MCSM-Offset": "0"})
        self.assertEqual(status, 200, data)
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/abort",
                                 {"session": session}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertNotIn(session, mcsmd.transfer._sessions)
        self.assertEqual(mcsmd.transfer.list_sessions(mcsmd.transfer_store()), [])
        self.assertFalse(list(self.folder.rglob("*" + mcsmd.transfer.PART_SUFFIX)))

    def test_stueckweise_mit_falschem_versatz(self):
        self.set_state(self.server["id"], "hosted")
        content = b"0123456789"
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest({"plugins/Teil.jar": content})},
                                 token=self.token)
        session = data["session"]["id"]
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/chunk",
                                 raw=content[:4], token=self.token,
                                 headers={"X-MCSM-Session": session,
                                          "X-MCSM-Path": "plugins/Teil.jar",
                                          "X-MCSM-Offset": "0"})
        self.assertEqual(status, 200, data)
        self.assertFalse(data["done"])
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/chunk",
                                 raw=content[4:], token=self.token,
                                 headers={"X-MCSM-Session": session,
                                          "X-MCSM-Path": "plugins/Teil.jar",
                                          "X-MCSM-Offset": "0"})
        self.assertEqual(status, 400)
        self.assertIn("Byte 4", data["error"])

    def test_verwaltungsdatei_wird_abgelehnt(self):
        self.set_state(self.server["id"], "hosted")
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest({"mcsm.json": b"{}"})},
                                 token=self.token)
        self.assertEqual(status, 400)

    def test_ganze_instanz_hochladen(self):
        content = b"level"
        status, data = self.call(
            "POST", f"/api/servers/{self.server['id']}/upload/begin",
            {"purpose": "instance", "manifest": self.manifest({"world/level.dat": content})},
            token=self.token)
        self.assertEqual(status, 200, data)
        session = data["session"]["id"]
        status, _ = self.call("GET", f"/api/servers/{self.server['id']}", token=self.token)
        self.assertEqual(
            mcsmd.instances.get_instance(self.server["id"])["state"], "uploading")
        self.call("POST", f"/api/servers/{self.server['id']}/upload/chunk", raw=content,
                  token=self.token,
                  headers={"X-MCSM-Session": session, "X-MCSM-Path": "world/level.dat",
                           "X-MCSM-Offset": "0"})
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/finish",
                                 {"session": session}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["server"]["state"], "hosted")
        self.assertGreater(data["server"]["size_bytes"], 0)

    def test_fremde_sitzung_abgelehnt(self):
        self.set_state(self.server["id"], "hosted")
        content = b"x"
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/upload/begin",
                                 {"manifest": self.manifest({"plugins/A.jar": content})},
                                 token=self.token)
        session = data["session"]["id"]
        token_b, _uid_b = self.make_user("Mara")
        server_b = self.make_server(token_b, name="Welt-B")
        self.set_state(server_b["id"], "hosted")
        status, data = self.call("POST", f"/api/servers/{server_b['id']}/upload/chunk",
                                 raw=content, token=token_b,
                                 headers={"X-MCSM-Session": session,
                                          "X-MCSM-Path": "plugins/A.jar",
                                          "X-MCSM-Offset": "0"})
        self.assertEqual(status, 403)

    def test_rueckholung_und_freigabe(self):
        self.set_state(self.server["id"], "hosted")
        (self.folder / "world").mkdir(parents=True, exist_ok=True)
        (self.folder / "world" / "level.dat").write_bytes(b"welt")
        (self.folder / "server.properties").write_text("max-players=10\n", encoding="utf-8")

        status, data = self.call("GET", f"/api/servers/{self.server['id']}/manifest",
                                 token=self.token)
        self.assertEqual(status, 200, data)
        manifest = data["manifest"]
        self.assertEqual(manifest["file_count"], 2)

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/pull", {},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["server"]["state"], "downloading")

        status, chunk = self.call(
            "GET", f"/api/servers/{self.server['id']}/download?path=world/level.dat&offset=0",
            token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(chunk, b"welt")

        status, data = self.call("POST", f"/api/servers/{self.server['id']}/release",
                                 {"manifest": manifest}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["server"]["state"], "local_only")
        self.assertFalse(self.folder.exists(), "Der Ordner auf dem Root wurde nicht gelöscht.")

    def test_freigabe_ohne_vollstaendige_rueckholung(self):
        self.set_state(self.server["id"], "hosted")
        (self.folder / "world").mkdir(parents=True, exist_ok=True)
        (self.folder / "world" / "level.dat").write_bytes(b"welt")
        (self.folder / "server.properties").write_text("x=1\n", encoding="utf-8")
        self.call("POST", f"/api/servers/{self.server['id']}/pull", {}, token=self.token)
        halb = self.manifest({"world/level.dat": b"welt"})
        status, data = self.call("POST", f"/api/servers/{self.server['id']}/release",
                                 {"manifest": halb}, token=self.token)
        self.assertEqual(status, 409)
        self.assertIn("nicht vollständig", data["error"])
        self.assertTrue(self.folder.exists(), "Der Ordner darf noch nicht gelöscht sein.")


# ---------------------------------------------------------------- Pässe: Ablauf und Widerruf

class AblaufTest(Basis):
    def test_abgelaufener_pass_parkt_lokale_instanz(self):
        token, uid = self.make_user()
        self.issue_pass(uid, days=1)
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        spaeter = mcsmd.store_hosted.now() + 2 * TAG
        mcsmd.tick(now=spaeter)
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertEqual(inst["state"], "awaiting_pull")
        status, data = self.call("GET", "/api/me", token=token)
        arten = [n["kind"] for n in data["notices"]]
        self.assertIn("pull", arten)

    def test_abgelaufener_premium_pass_setzt_auf_ruhend(self):
        token, uid = self.make_user()
        self.issue_pass(uid, kind="premium", days=1)
        status, data = self.call("POST", "/api/servers",
                                 {"name": "Root-Welt", "origin": "premium"}, token=token)
        self.assertEqual(status, 201, data)
        server = data["server"]
        spaeter = mcsmd.store_hosted.now() + 2 * TAG
        mcsmd.tick(now=spaeter)
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertEqual(inst["state"], "suspended")

    def test_widerruf_parkt_erst_nach_der_ankuendigung(self):
        """Ein Widerruf schaltet nicht mitten im Spiel ab: erst ansagen, in 10 Minuten stoppen."""
        token, uid = self.make_user()
        entry = self.issue_pass(uid, days=30)
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        status, data = self.call("POST", f"/api/admin/passes/{entry['id']}/revoke", {},
                                 token=self.admin_token)
        self.assertEqual(status, 200)
        jetzt = mcsmd.store_hosted.now()
        self.assertGreater(int(data["stoppt_um"]), jetzt)
        self.assertLessEqual(int(data["stoppt_um"]), jetzt + mcsmd.WARN_MINUTES * 60 + 5)
        mcsmd.tick(now=jetzt)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "hosted")
        # Nach der Frist geht er wirklich aus.
        mcsmd.tick(now=jetzt + mcsmd.WARN_MINUTES * 60 + 1)
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertEqual(inst["state"], "awaiting_pull")

    def test_widerruf_frist_endet_spaetestens_beim_ablauf(self):
        """Läuft der Pass ohnehin in drei Minuten ab, wird nicht zehn Minuten gewartet."""
        token, uid = self.make_user()
        entry = self.issue_pass(uid, days=30)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.store_hosted.patch_by("passes", "id", entry["id"],
                                    {"expires_at": jetzt + 180, "revoked_at": jetzt})
        self.assertEqual(mcsmd.widerruf_frist(uid, jetzt), jetzt + 180)
        self.assertEqual(mcsmd.widerruf_frist(uid, jetzt + 181), 0)

    def test_gueltiger_pass_laesst_alles_stehen(self):
        token, uid = self.make_user()
        self.issue_pass(uid, days=30)
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        mcsmd.tick()
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertEqual(inst["state"], "hosted")

    def test_warnung_wird_nur_einmal_gemerkt(self):
        _token, uid = self.make_user()
        entry = self.issue_pass(uid, days=1)
        kurz_vorher = int(entry["expires_at"]) - 300
        mcsmd.tick(now=kurz_vorher)
        self.assertIn(entry["id"], mcsmd._runtime["warned"])
        gemerkt = dict(mcsmd._runtime["warned"])
        mcsmd.tick(now=kurz_vorher + 60)
        self.assertEqual(mcsmd._runtime["warned"], gemerkt)


# ---------------------------------------------------------------- Adminansicht

class AdminTest(Basis):
    def test_uebersicht(self):
        token, uid = self.make_user()
        self.issue_pass(uid)
        self.make_server(token)
        status, data = self.call("GET", "/api/admin/status", token=self.admin_token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["users"], 2)
        self.assertEqual(data["instances"], 1)
        self.assertIn("disk_free_text", data)
        self.assertEqual(data["ram_running_mb"], 0)

    def test_benutzerliste_mit_grenzen(self):
        _token, uid = self.make_user()
        self.issue_pass(uid, max_concurrent=3, ram_total_mb=9216)
        status, data = self.call("GET", "/api/admin/users", token=self.admin_token)
        self.assertEqual(status, 200)
        found = [u for u in data["users"] if u["id"] == uid]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["limits"]["max_concurrent"], 3)
        self.assertIn("9 GB", found[0]["limits_text"])
        self.assertNotIn("discord_id", found[0])

    def test_pass_verlaengern_und_grenzen_setzen(self):
        _token, uid = self.make_user()
        entry = self.issue_pass(uid, days=1)
        status, data = self.call("POST", f"/api/admin/passes/{entry['id']}/extend",
                                 {"days": 7}, token=self.admin_token)
        self.assertEqual(status, 200, data)
        self.assertGreater(data["pass"]["expires_at"], entry["expires_at"])
        status, data = self.call("POST", f"/api/admin/passes/{entry['id']}/limits",
                                 {"max_concurrent": 4}, token=self.admin_token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["pass"]["max_concurrent"], 4)

    def test_einladungen_auflisten(self):
        self.make_user()
        status, data = self.call("GET", "/api/admin/invites", token=self.admin_token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["invites"]), 2)
        for invite in data["invites"]:
            self.assertRegex(invite["code"], r"^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")

    def test_konto_loeschen_raeumt_instanzen(self):
        token, uid = self.make_user()
        server = self.make_server(token)
        folder = mcsmd.sources_linux.users_dir() / uid / server["id"]
        status, data = self.call("DELETE", f"/api/admin/users/{uid}", token=self.admin_token)
        self.assertEqual(status, 200, data)
        self.assertFalse(folder.exists())
        self.assertIsNone(mcsmd.instances.get_instance(server["id"]))
        self.assertIsNone(mcsmd.users.get_user(uid))

    def test_letzter_admin_bleibt(self):
        status, data = self.call("POST", f"/api/admin/users/"
                                         f"{mcsmd.users.all_users()[0]['id']}/role",
                                 {"role": "user"}, token=self.admin_token)
        self.assertEqual(status, 400)
        self.assertIn("Admin", data["error"])


# ---------------------------------------------------------------- Ports und Hilfsfunktionen

class HilfenTest(Basis):
    def test_ports_werden_gespeichert(self):
        mcsmd.POOL.allocate_for("abc123abc123", "java", geyser=True)
        mcsmd.save_ports()
        stand = json.loads((mcsmd.store_hosted.data_dir() / mcsmd.RUNTIME_FILE)
                           .read_text(encoding="utf-8"))
        pools = sorted(e["pool"] for e in stand["ports"])
        # Zwei für die Instanz (TCP + UDP) und einer für den Verteiler auf 25565.
        self.assertEqual(pools, ["bedrock", "java", "java"])
        frisch = mcsmd.ports.PortPool(probe=False)
        self.assertEqual(frisch.restore(stand["ports"]), [])
        # 25565 gehört dem Verteiler, die Instanz bekommt deshalb 25566.
        self.assertEqual(frisch.assignment(mcsmd.routen.ROUTER_OWNER)["port"], 25565)
        self.assertEqual(frisch.assignment("abc123abc123")["port"], 25566)

    def test_verteiler_port_bleibt_reserviert(self):
        """Port 25565 gehört dem Verteiler – keine Instanz darf ihn bekommen."""
        self.assertEqual(mcsmd.POOL.owner_of(mcsmd.ports.JAVA_POOL, 25565),
                         mcsmd.routen.ROUTER_OWNER)
        for nummer in range(5):
            lease = mcsmd.POOL.allocate_for(f"instanz{nummer}", "java")
            self.assertNotIn(25565, lease["java"].ports())

    def test_server_properties_werden_gesetzt(self):
        token, uid = self.make_user()
        server = self.make_server(token, name="Bedrock-Welt", type="bedrock", flavor="paper")
        folder = mcsmd.sources_linux.users_dir() / uid / server["id"]
        mcsmd.POOL.allocate_for(server["id"], "bedrock", max_players=10)
        assignment = mcsmd.POOL.assignment(server["id"])
        mcsmd.apply_ports(mcsmd.instances.get_instance(server["id"]), folder, assignment, False)
        props = mcsmd.read_properties(folder / "server.properties")
        self.assertEqual(props["transport"], "nethernet")
        self.assertEqual(props["server-port"], str(assignment["port"]))
        self.assertIn("-", props["server-udp-ports"])

    def test_properties_behalten_kommentare(self):
        path = pathlib.Path(self.tmp) / "server.properties"
        path.write_text("# Kommentar\nmotd=Alt\nmax-players=10\n", encoding="utf-8")
        mcsmd.patch_properties(path, {"motd": "Neu", "difficulty": "hard"})
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Kommentar", text)
        self.assertIn("motd=Neu", text)
        self.assertIn("difficulty=hard", text)
        self.assertNotIn("motd=Alt", text)

    def test_fehlerabbildung(self):
        self.assertEqual(mcsmd.status_for_value_error(ValueError("Es gibt keinen Server …")), 404)
        self.assertEqual(mcsmd.status_for_value_error(
            ValueError("Auf dem Server ist zu wenig Platz frei: …")), 507)
        self.assertEqual(mcsmd.status_for_value_error(ValueError("Irgendwas anderes")), 400)

    def test_geheimnisse_nicht_im_protokoll(self):
        zeile = mcsmd.scrub("GET /api/x?session=abcdef&token=geheim&path=welt")
        self.assertNotIn("geheim", zeile)
        self.assertNotIn("abcdef", zeile)
        self.assertIn("path=welt", zeile)
        # Einladungscodes stehen in der Anzeigeform, nicht als name=wert – auch die müssen weg.
        code = mcsmd.scrub("Einladungscode: ABCD-EFGH-JKLM (steht auch in /srv/...)")
        self.assertNotIn("ABCD-EFGH-JKLM", code)

    def test_admin_code_steht_nicht_im_protokoll(self):
        """Der Code für das Betreiberkonto darf nur in der Datei stehen.

        Das Protokoll geht zusätzlich ins systemd-Journal (Gruppe adm liest mit) – wer den Code
        hat, bekommt das Betreiberkonto.
        """
        mcsmd.close_log()
        text = pathlib.Path(os.environ["MCSM_LOG"]).read_text(encoding="utf-8", errors="replace")
        self.assertIn("ERSTER-ADMIN.txt", text)
        self.assertNotIn(self.first_code, text)
        self.assertNotIn(mcsmd.users.format_code(self.first_code), text)

    def test_koerper_ohne_anmeldung_ist_klein_begrenzt(self):
        """Unangemeldet wird nur wenig eingelesen – erst die Anmeldung, dann der Körper.

        Sonst könnte jeder mit vielen gleichzeitigen „Content-Length: 16 MB“ und einem ungültigen
        Token den Arbeitsspeicher der Maschine belegen, bevor überhaupt die 401 herausgeht.
        """
        gross = {"code": "A" * (mcsmd.UNAUTH_LIMIT + 5000), "name": "Test"}
        status, data = self.call("POST", "/api/auth/invite", gross)
        self.assertEqual(status, 400, data)
        self.assertIn("zu groß", data["error"])
        self.assertLess(mcsmd.UNAUTH_LIMIT, mcsmd.MAX_JSON_BYTES)

    def test_dienst_begrenzt_gleichzeitige_anfragen(self):
        self.assertEqual(self.httpd.max_workers, mcsmd.MAX_WORKERS)
        self.assertTrue(hasattr(self.httpd, "_slots"))

    def test_alle_routen_haben_eine_rechtepruefung(self):
        """Ohne Anmeldung erreichbar sind nur die Anmeldewege, die Erreichbarkeitsprüfung und
        die Oberfläche selbst (die ihre Daten anschließend angemeldet holt).

        ``/api/router/wake`` trägt kein Sitzungstoken, weil der Verteiler kein Konto hat – dafür
        prüft die Route selbst Rückschleife und gemeinsames Geheimnis (siehe WeckrufTest).
        """
        offen = {entry["regex"].pattern for entry in mcsmd.ROUTES if not entry["auth"]
                 and not entry["admin"]}
        self.assertEqual(offen, {
            r"^/api/health$",
            r"^/api/router/wake$",
            r"^/api/auth/invite$",
            r"^/api/auth/logout$",
            r"^(?:/api)?/auth/discord/start$",
            r"^(?:/api)?/auth/discord/callback$",
            r"^(?:/api)?/auth/discord/register$",
            r"^(?:/api)?/auth/discord/poll$",
            r"^(?:/api)?/auth/discord/finish$",
            r"^(?:/api)?/auth/discord/claim$",
            r"^/(?:admin\.html)?$",
            r"^/(admin\.css|admin\.js|logo\.svg)$",
            r"^/favicon\.ico$",
        })


# ---------------------------------------------------------------- Oberfläche ausliefern

class OberflaecheTest(Basis):
    """Die Betreiberoberfläche gibt es nur unter admin.<domain> – nicht unter api.<domain>."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["MCSM_ADMIN_HOST"] = "admin.arcardia-nexus.de"
        os.environ["MCSM_API_HOST"] = "api.arcardia-nexus.de"

    def hole(self, pfad: str, host: str = "admin.arcardia-nexus.de"):
        url = f"http://127.0.0.1:{self.port}{pfad}"
        request = urllib.request.Request(url, headers={"Host": host})
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_html_kommt_unter_admin(self):
        for pfad in ("/", "/admin.html"):
            status, body, headers = self.hole(pfad)
            with self.subTest(pfad=pfad):
                self.assertEqual(status, 200)
                self.assertIn(b"<html lang=\"de\">", body)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))

    def test_css_und_js_mit_richtigem_typ(self):
        status, body, headers = self.hole("/admin.css")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/css"))
        self.assertGreater(len(body), 500)
        status, body, headers = self.hole("/admin.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/javascript"))
        self.assertIn(b"REFRESH_MS", body)

    def test_unter_api_gibt_es_keine_oberflaeche(self):
        for pfad in ("/", "/admin.html", "/admin.css", "/admin.js"):
            status, _body, _headers = self.hole(pfad, host="api.arcardia-nexus.de")
            with self.subTest(pfad=pfad):
                self.assertEqual(status, 404)

    def test_api_geht_unter_beiden_namen(self):
        for host in ("admin.arcardia-nexus.de", "api.arcardia-nexus.de"):
            status, _body, _headers = self.hole("/api/health", host=host)
            self.assertEqual(status, 200, host)

    def test_kein_verzeichnis_ausbruch(self):
        for pfad in ("/../mcsmd.py", "/..%2fmcsmd.py", "/web/admin.js", "/admin.js/../mcsmd.py",
                     "/../../srv/mcsm/data/users.json", "/core/oauth.py"):
            status, _body, _headers = self.hole(pfad)
            with self.subTest(pfad=pfad):
                self.assertEqual(status, 404)

    def test_kopfzeilen_gegen_falsches_raten_und_alte_staende(self):
        status, _body, headers = self.hole("/admin.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers.get("ETag"))

    def test_etag_spart_die_zweite_uebertragung(self):
        _status, _body, headers = self.hole("/admin.js")
        url = f"http://127.0.0.1:{self.port}/admin.js"
        request = urllib.request.Request(url, headers={"Host": "admin.arcardia-nexus.de",
                                                       "If-None-Match": headers["ETag"]})
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                self.assertEqual(resp.status, 304)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 304)

    def test_jede_von_der_seite_benutzte_route_gibt_es_wirklich(self):
        """Die Oberfläche darf keine Adresse aufrufen, die der Dienst nicht kennt."""
        js = (pathlib.Path(mcsmd.WEB_DIR) / "admin.js").read_text(encoding="utf-8")
        gefunden = set(re.findall(r"'(/api/[A-Za-z0-9/_.-]*)", js))
        self.assertTrue(gefunden)
        beispiele = {
            "/api/me": "/api/me",
            "/api/health": "/api/health",
            "/api/auth/logout": "/api/auth/logout",
            "/api/auth/discord/start": "/api/auth/discord/start",
            "/api/auth/discord/register": "/api/auth/discord/register",
            "/api/admin/status": "/api/admin/status",
            "/api/admin/users": "/api/admin/users",
            "/api/admin/invites": "/api/admin/invites",
            "/api/admin/passes": "/api/admin/passes",
            "/api/admin/instances": "/api/admin/instances",
            "/api/admin/users/": "/api/admin/users/abc123/block",
            "/api/admin/invites/": "/api/admin/invites/ABCD-EFGH-JKLM/revoke",
            "/api/admin/passes/": "/api/admin/passes/abc123/revoke",
            "/api/servers/": "/api/servers/abc123/start",
        }
        for pfad in sorted(gefunden):
            probe = beispiele.get(pfad)
            self.assertIsNotNone(probe, f"Für {pfad} fehlt in diesem Test eine Probe")
            with self.subTest(pfad=pfad):
                self.assertTrue(any(e["regex"].match(probe) for e in mcsmd.ROUTES),
                                f"admin.js ruft {pfad} auf – dafür gibt es keine Route")


# ---------------------------------------------------------------- Discord-Anmeldung

class DiscordTest(Basis):
    """Anmeldung über Discord – mit erfundenen Zugangsdaten, ohne eine Anfrage ins Netz."""

    def setUp(self) -> None:
        super().setUp()
        ordner = pathlib.Path(self.tmp) / "discord"
        ordner.mkdir(parents=True, exist_ok=True)
        (ordner / "discord_client_id").write_text("123456789012345678\n", encoding="utf-8")
        (ordner / "discord_secret").write_text("x" * 32 + "\n", encoding="utf-8")
        mcsmd.oauth.vergessen()

    def test_start_liefert_eine_discord_adresse(self):
        status, data = self.call("GET", "/api/auth/discord/start?ziel="
                                 + urllib.parse.quote("https://admin.arcardia-nexus.de/admin.html"))
        self.assertEqual(status, 200, data)
        self.assertTrue(data["url"].startswith("https://discord.com/oauth2/authorize?"), data)
        self.assertIn("scope=identify", data["url"])
        self.assertIn("state=", data["url"])
        self.assertEqual(data["flow"], "admin")
        self.assertTrue(data["state"])

    def test_start_ohne_api_vorsatz(self):
        """Discord ruft /auth/discord/callback ohne „/api“ auf – der Start geht auch so."""
        status, data = self.call("GET", "/auth/discord/start")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["url"].startswith("https://discord.com/"))

    def test_programm_bekommt_ein_abholgeheimnis(self):
        status, data = self.call("GET", "/api/auth/discord/start?device=Luca-PC")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["flow"], "app")
        self.assertGreaterEqual(len(data["poll_secret"]), 16)
        self.assertIn(data["state"], mcsmd._LOGINS)

    def test_admin_start_bindet_den_browser(self):
        """Der Start setzt ein kurzlebiges Cookie; ohne das nimmt die Rückleitung nichts an."""
        status, data, headers = self.call_full(
            "GET", "/api/auth/discord/start?flow=admin",
            headers={"Host": "admin.arcardia-nexus.de"})
        self.assertEqual(status, 200, data)
        keks = headers.get("Set-Cookie", "")
        self.assertIn(mcsmd.ANMELDE_COOKIE + "=", keks)
        self.assertIn("HttpOnly", keks)
        self.assertIn("Secure", keks)
        self.assertIn("SameSite=Lax", keks)
        vorgang = mcsmd._LOGINS[data["state"]]
        self.assertTrue(vorgang["browser"])

    def test_rueckleitung_ohne_cookie_setzt_keine_sitzung(self):
        """Anmelde-CSRF: eine fremd begonnene Anmeldung darf im Browser des Betreibers nicht
        stillschweigend zu einer Sitzung werden."""
        status, data, _headers = self.call_full(
            "GET", "/api/auth/discord/start?flow=admin",
            headers={"Host": "admin.arcardia-nexus.de"})
        self.assertEqual(status, 200, data)
        state = data["state"]
        vorher = len(mcsmd.store_hosted.load("sessions"))
        status, headers = self.ohne_folgen(
            f"/auth/discord/callback?code=abc&state={urllib.parse.quote(state)}")
        self.assertEqual(status, 302)
        ort = urllib.parse.unquote(headers.get("Location", ""))
        self.assertIn("#fehler=", ort)
        self.assertIn("anderen Browser", ort)
        self.assertNotIn("#token=", ort)
        # Keine neue Sitzung, und der Vorgang bleibt unbenutzt.
        self.assertEqual(len(mcsmd.store_hosted.load("sessions")), vorher)
        self.assertIsNone(mcsmd._LOGINS[state]["ergebnis"])

    def ohne_folgen(self, pfad: str, host: str = "admin.arcardia-nexus.de"):
        """Aufruf, der einer Weiterleitung **nicht** folgt (wir wollen die 302 selbst sehen)."""
        class Bleib(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_a, **_kw):
                return None

        opener = urllib.request.build_opener(Bleib)
        url = f"http://127.0.0.1:{self.port}{pfad}"
        request = urllib.request.Request(url, headers={"Host": host})
        try:
            with opener.open(request, timeout=20) as resp:
                resp.read()
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code, dict(exc.headers)

    def test_abholweg_legt_keinen_vorgang_an(self):
        """Weg B (Abholcode): ohne diesen Verzicht entstünde nie ein Abholcode."""
        merkmal = "a" * 64
        status, data = self.call("GET", "/api/auth/discord/start?flow=app&abhol=" + merkmal)
        self.assertEqual(status, 200, data)
        self.assertNotIn("poll_secret", data)
        self.assertNotIn(data["state"], mcsmd._LOGINS)
        # Und die Rückleitung stolpert nicht über den fehlenden Vorgang.
        status, _body = self.call("GET", f"/auth/discord/callback?state={data['state']}")
        self.assertIn(status, (400, 403), status)

    def test_anwendungsgeheimnis_steht_in_keiner_antwort(self):
        status, data = self.call("GET", "/api/auth/discord/start?device=Luca-PC")
        self.assertEqual(status, 200)
        self.assertNotIn("x" * 32, json.dumps(data))
        text = pathlib.Path(os.environ["MCSM_LOG"]).read_text(encoding="utf-8")
        self.assertNotIn("x" * 32, text)

    def test_abholen_wartet_bis_der_browser_fertig_ist(self):
        _status, data = self.call("GET", "/api/auth/discord/start?device=Luca-PC")
        state, secret = data["state"], data["poll_secret"]
        status, out = self.call("GET", f"/api/auth/discord/poll?state={state}&secret={secret}")
        self.assertEqual(status, 200, out)
        self.assertTrue(out["pending"])

    def test_abholen_mit_falschem_geheimnis(self):
        _status, data = self.call("GET", "/api/auth/discord/start?device=Luca-PC")
        status, out = self.call("GET", f"/api/auth/discord/poll?state={data['state']}&secret=falsch")
        self.assertEqual(status, 403, out)
        self.assertIn("erneut", out["error"])

    def test_abholen_liefert_das_token_genau_einmal(self):
        _status, data = self.call("GET", "/api/auth/discord/start?device=Luca-PC")
        state, secret = data["state"], data["poll_secret"]
        # Die Rückleitung im Browser hinterlegt das Ergebnis – hier ohne Netz nachgestellt.
        token, session = mcsmd.users.create_session(
            mcsmd.users.find_by_name("Betreiber")["id"], note="Test")
        mcsmd._login_setzen(state, ergebnis={"token": token,
                                             "expires_at": session["expires_at"],
                                             "user": mcsmd.public_user(
                                                 mcsmd.users.find_by_name("Betreiber"))})
        status, out = self.call("GET", f"/api/auth/discord/poll?state={state}&secret={secret}")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["token"], token)
        status, out = self.call("GET", f"/api/auth/discord/poll?state={state}&secret={secret}")
        self.assertEqual(status, 403, out)

    def test_fremdes_ziel_wird_nicht_uebernommen(self):
        """Ein erfundenes „ziel“ darf das Sitzungstoken nicht auf einen fremden Server schicken."""
        self.assertEqual(mcsmd._admin_ziel("https://boese.example/klau"),
                         "https://admin.arcardia-nexus.de/admin.html")
        self.assertEqual(mcsmd._admin_ziel("https://admin.arcardia-nexus.de/admin.html"),
                         "https://admin.arcardia-nexus.de/admin.html")

    def test_health_meldet_dass_discord_da_ist(self):
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["discord"])

    def test_health_meldet_offenen_erstzugang(self):
        """Solange es keinen Betreiber gibt, klappt die Oberfläche das Codefeld auf."""
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertFalse(data["erstzugang"])        # im Test gibt es schon einen Betreiber
        self._erstzugang()
        status, data = self.call("GET", "/api/health")
        self.assertTrue(data["erstzugang"])

    def _erstzugang(self) -> str:
        """Kontenliste leeren und den Code für das erste Betreiberkonto hinterlegen."""
        for entry in mcsmd.users.all_users():
            mcsmd.store_hosted.remove_by("users", "id", entry["id"])
        self.assertEqual(mcsmd.users.count_admins(), 0)
        mcsmd.ensure_first_admin()
        code = str(mcsmd._runtime.get("admin_invite") or "")
        self.assertTrue(code)
        return code

    def test_erster_login_wird_betreiber(self):
        """Das erste Discord-Konto wird Betreiber – aber nur mit dem Code aus ERSTER-ADMIN.txt."""
        code = self._erstzugang()
        self.assertTrue(mcsmd.oauth.erster_admin_faellig())
        # Ohne den Code bekommt niemand das Betreiberkonto, auch nicht der Schnellste.
        with self.assertRaises(mcsmd.oauth.EinladungNoetig) as fall:
            mcsmd.oauth.anmelden(
                mcsmd.oauth.Identitaet("123456789012345678", "luca", "Luca", ""))
        self.assertIn("ERSTER-ADMIN.txt", fall.exception.message)
        self.assertEqual(mcsmd.users.count_admins(), 0)
        # Mit dem Code schon.
        anmeldung = mcsmd.oauth.anmelden(
            mcsmd.oauth.Identitaet("123456789012345678", "luca", "Luca", ""),
            invite_code=code)
        self.assertTrue(anmeldung.admin_vergeben)
        self.assertEqual(anmeldung.user["role"], "admin")
        # Der zweite kommt ohne Einladungscode nicht herein.
        with self.assertRaises(mcsmd.oauth.EinladungNoetig):
            mcsmd.oauth.anmelden(
                mcsmd.oauth.Identitaet("876543210987654321", "anna", "Anna", ""))

    def test_discord_konto_bleibt_verknuepft(self):
        code = self._erstzugang()
        identitaet = mcsmd.oauth.Identitaet("123456789012345678", "luca", "Luca", "")
        erste = mcsmd.oauth.anmelden(identitaet, invite_code=code)
        zweite = mcsmd.oauth.anmelden(identitaet)
        self.assertEqual(erste.user["id"], zweite.user["id"])
        self.assertFalse(zweite.neu)

    def test_cookie_gilt_als_anmeldung(self):
        status, data = self.call("GET", "/api/me", headers={
            "Cookie": f"{mcsmd.COOKIE_NAME}={self.admin_token}"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["user"]["role"], "admin")

    def test_abmelden_loescht_das_cookie(self):
        url = f"http://127.0.0.1:{self.port}/api/auth/logout"
        request = urllib.request.Request(url, data=b"{}", method="POST", headers={
            "Content-Type": "application/json",
            "Cookie": f"{mcsmd.COOKIE_NAME}={self.admin_token}"})
        with urllib.request.urlopen(request, timeout=20) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("Max-Age=0", resp.headers.get("Set-Cookie", ""))
        status, _ = self.call("GET", "/api/me", token=self.admin_token)
        self.assertEqual(status, 401)


# ---------------------------------------------------------------- Ratenbremse am Dienst

class BremseTest(Basis):
    def test_zu_viele_anmeldeversuche_werden_gesperrt(self):
        grenze = mcsmd.RATE_LIMITS["anmeldung"][0]
        letzte = None
        for _ in range(grenze + 2):
            letzte = self.call("POST", "/api/auth/invite",
                               {"code": "AAAA-BBBB-CCCC", "name": "Test"})
        self.assertEqual(letzte[0], 429, letzte)
        self.assertIn("Zu viele Anfragen", letzte[1]["error"])

    def test_gesperrt_heisst_auch_kein_richtiger_code_mehr(self):
        grenze = mcsmd.RATE_LIMITS["anmeldung"][0]
        for _ in range(grenze + 2):
            self.call("POST", "/api/auth/invite", {"code": "AAAA-BBBB-CCCC", "name": "T"})
        status, data = self.call("POST", "/api/admin/invites", {"uses_max": 1, "days": 7},
                                 token=self.admin_token)
        self.assertEqual(status, 201, data)
        code = data["invite"]["code"]
        status, _data = self.call("POST", "/api/auth/invite", {"code": code, "name": "Neu"})
        self.assertEqual(status, 429)

    def test_angemeldete_aufrufe_werden_nicht_so_schnell_gebremst(self):
        for _ in range(mcsmd.RATE_LIMITS["anmeldung"][0] + 5):
            status, _ = self.call("GET", "/api/me", token=self.admin_token)
            self.assertEqual(status, 200)

    def test_falsches_token_zaehlt_in_einem_eigenen_topf_mit(self):
        """Abgewiesene Anfragen werden gebremst – aber milder, damit ein abgelaufenes Token
        niemanden minutenlang von der Neuanmeldung aussperrt."""
        letzte = None
        for _ in range(mcsmd.RATE_LIMITS["abgewiesen"][0] + 2):
            letzte = self.call("GET", "/api/me", token="falschfalschfalsch")
        self.assertEqual(letzte[0], 429, letzte)
        # Der Anmeldeweg selbst ist davon unberührt.
        status, data = self.call("POST", "/api/admin/invites", {"uses_max": 1, "days": 7},
                                 token=self.admin_token)
        self.assertEqual(status, 201, data)
        status, data = self.call("POST", "/api/auth/invite",
                                 {"code": data["invite"]["code"], "name": "Neu"})
        self.assertEqual(status, 200, data)

    def test_wartezeit_steht_in_der_antwort(self):
        for _ in range(mcsmd.RATE_LIMITS["anmeldung"][0] + 2):
            self.call("POST", "/api/auth/invite", {"code": "AAAA-BBBB-CCCC", "name": "T"})
        url = f"http://127.0.0.1:{self.port}/api/auth/invite"
        request = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=20)
            self.fail("Es hätte eine 429 kommen müssen")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 429)
            self.assertTrue(int(exc.headers.get("Retry-After", "0")) > 0)


# ---------------------------------------------------------------- Unterdomänen und routes.json

class VerteilerTest(Basis):
    def test_neue_instanz_bekommt_eine_adresse(self):
        token, _uid = self.make_user()
        server = self.make_server(token, name="Mein Server")
        self.assertEqual(server["marke"], "mein-server")
        self.assertEqual(server["subdomain"], "mein-server.arcardia-nexus.de")
        self.assertEqual(server["address"], "mein-server.arcardia-nexus.de")

    def test_adresse_steht_in_der_serverliste_und_in_me(self):
        token, _uid = self.make_user()
        server = self.make_server(token, name="Mein Server")
        status, data = self.call("GET", "/api/servers", token=token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["servers"][0]["address"], "mein-server.arcardia-nexus.de")
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(data["addresses"][server["id"]], "mein-server.arcardia-nexus.de")
        self.assertEqual(data["domain"], "arcardia-nexus.de")

    def test_bedrock_bekommt_domain_und_port(self):
        token, uid = self.make_user()
        server = self.make_server(token, name="Handy Welt", type="bedrock")
        self.set_state(server["id"], "hosted")
        mcsmd.POOL.allocate_for(server["id"], "bedrock", max_players=10)
        zuteilung = mcsmd.POOL.assignment(server["id"])
        mcsmd.mirror_ports(mcsmd.instances.get_instance(server["id"]), zuteilung)
        status, data = self.call("GET", "/api/servers", token=token)
        self.assertEqual(status, 200, data)
        adresse = data["servers"][0]["address"]
        self.assertEqual(adresse, f"arcardia-nexus.de:{zuteilung['port']}")

    def test_routes_json_wird_geschrieben(self):
        token, _uid = self.make_user()
        server = self.make_server(token, name="Mein Server")
        self.set_state(server["id"], "hosted")
        mcsmd.schreibe_routen("Test")
        pfad = mcsmd.routen.routes_path()
        data = json.loads(pfad.read_text(encoding="utf-8"))
        # Der Port kommt jetzt schon beim Anlegen aus dem PortPool – nicht erst beim Start.
        port = mcsmd.ports_live_of(server["id"])["port"]
        self.assertEqual(data["mein-server"],
                         {"port": port, "instanz": server["id"], "name": "Mein Server",
                          "max": mcsmd.routen.MAX_SPIELER_VORGABE, "wecken": True})

    def test_zurueckgeholte_instanz_verschwindet_aus_der_tabelle(self):
        token, uid = self.make_user()
        self.issue_pass(uid)
        server = self.make_server(token, name="Mein Server")
        self.set_state(server["id"], "hosted")
        mcsmd.instances.set_ports(server["id"], {"java": 25570})
        mcsmd.schreibe_routen("Test")
        self.assertIn("mein-server", json.loads(
            mcsmd.routen.routes_path().read_text(encoding="utf-8")))
        status, _ = self.call("POST", f"/api/servers/{server['id']}/pull", {}, token=token)
        self.assertEqual(status, 200)
        self.assertNotIn("mein-server", json.loads(
            mcsmd.routen.routes_path().read_text(encoding="utf-8")))

    def test_porteintrag_wird_beim_spiegeln_berichtigt(self):
        """Befund C: ein alter Eintrag einer anderen Instanz ließ das Spiegeln still scheitern."""
        token, _uid = self.make_user()
        alt = self.make_server(token, name="Alte Welt")
        neu = self.make_server(token, name="Neue Welt")
        mcsmd.instances.set_ports(alt["id"], {"java": 25570})    # Rest aus einem früheren Lauf
        mcsmd.POOL.reserve(neu["id"], mcsmd.ports.JAVA_POOL, 25570)
        gespiegelt = mcsmd.mirror_ports(mcsmd.instances.get_instance(neu["id"]),
                                        mcsmd.POOL.assignment(neu["id"]))
        self.assertEqual(gespiegelt, {"java": 25570})
        self.assertEqual(mcsmd.instances.get_instance(neu["id"])["ports"], {"java": 25570})
        self.assertEqual(mcsmd.instances.get_instance(alt["id"])["ports"], {})

    def test_laufender_server_verliert_seinen_porteintrag_nicht(self):
        token, _uid = self.make_user()
        eins = self.make_server(token, name="Eins")
        zwei = self.make_server(token, name="Zwei")
        mcsmd.POOL.reserve(eins["id"], mcsmd.ports.JAVA_POOL, 25571)
        mcsmd.instances.set_ports(eins["id"], {"java": 25571})
        # Zwei behauptet denselben Port, ohne ihn zu besitzen – der Eintrag von Eins bleibt.
        mcsmd.mirror_ports(mcsmd.instances.get_instance(zwei["id"]), {"port": 25571})
        self.assertEqual(mcsmd.instances.get_instance(eins["id"])["ports"], {"java": 25571})


# ---------------------------------------------------------------- Ruhende Server wecken

class WeckenTest(Basis):
    """Befund A: ein ruhender Premium-Server muss mit neuem Pass wieder startbar werden."""

    def premium_instanz(self, token: str, name: str = "Root-Welt"):
        status, data = self.call("POST", "/api/servers",
                                 {"name": name, "origin": "premium"}, token=token)
        self.assertEqual(status, 201, data)
        return data["server"]

    def test_neuer_pass_macht_ruhenden_server_wieder_startbar(self):
        token, uid = self.make_user()
        self.issue_pass(uid, kind="premium", days=1)
        server = self.premium_instanz(token)
        spaeter = mcsmd.store_hosted.now() + 2 * TAG
        mcsmd.tick(now=spaeter)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "suspended")
        status, data = self.call("POST", f"/api/servers/{server['id']}/start", {}, token=token)
        self.assertEqual(status, 409, data)
        self.assertIn("ruht", data["error"])

        self.issue_pass(uid, kind="premium", days=30)
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertEqual(inst["state"], "hosted", "Der Rückweg suspended -> hosted fehlt")
        pruefung = mcsmd.instances.check_start(server["id"])
        self.assertTrue(pruefung.ok, pruefung.reason)

    def test_der_besitzer_bekommt_eine_meldung(self):
        token, uid = self.make_user()
        self.issue_pass(uid, kind="premium", days=1)
        server = self.premium_instanz(token)
        mcsmd.tick(now=mcsmd.store_hosted.now() + 2 * TAG)
        self.issue_pass(uid, kind="premium", days=30)
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(status, 200, data)
        hinweise = [n for n in data["notices"] if n["kind"] == "wieder_startbar"]
        self.assertEqual(len(hinweise), 1, data["notices"])
        self.assertIn("Root-Welt", hinweise[0]["text"])
        self.assertIn(server["id"], hinweise[0]["instance"])

    def test_geweckt_wird_nicht_von_selbst_gestartet(self):
        token, uid = self.make_user()
        self.issue_pass(uid, kind="premium", days=1)
        server = self.premium_instanz(token)
        mcsmd.tick(now=mcsmd.store_hosted.now() + 2 * TAG)
        self.issue_pass(uid, kind="premium", days=30)
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertFalse(inst["running"])
        self.assertIsNone(mcsmd.REGISTRY.find(server["id"]))

    def test_verlaengern_weckt_ebenfalls(self):
        token, uid = self.make_user()
        entry = self.issue_pass(uid, kind="premium", days=1)
        server = self.premium_instanz(token)
        mcsmd.tick(now=mcsmd.store_hosted.now() + 2 * TAG)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "suspended")
        status, data = self.call("POST", f"/api/admin/passes/{entry['id']}/extend",
                                 {"days": 30}, token=self.admin_token)
        self.assertEqual(status, 200, data)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "hosted")
        self.assertEqual(len(data["wieder_startbar"]), 1)

    def test_lokaler_pass_weckt_keinen_premium_server(self):
        token, uid = self.make_user()
        entry = self.issue_pass(uid, kind="premium", days=30)
        server = self.premium_instanz(token)
        # Widerruf statt Zeitreise: der Pass ist damit auch „jetzt“ ungültig. Gestoppt wird
        # aber erst nach der Ankündigungsfrist.
        status, _ = self.call("POST", f"/api/admin/passes/{entry['id']}/revoke", {},
                              token=self.admin_token)
        self.assertEqual(status, 200)
        mcsmd.tick(now=mcsmd.store_hosted.now() + mcsmd.WARN_MINUTES * 60 + 1)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "suspended")
        self.issue_pass(uid, kind="local", days=30)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "suspended",
                         "Ein lokaler Pass deckt keine reine Root-Instanz")

    def test_minutenpruefung_weckt_auch_ohne_neuen_pass_aufruf(self):
        """Ein Pass, der in der Zukunft gilt (z. B. von Hand eingetragen), wirkt spätestens im
        nächsten Durchlauf der Überwachungsschleife."""
        token, uid = self.make_user()
        self.issue_pass(uid, kind="premium", days=1)
        server = self.premium_instanz(token)
        mcsmd.tick(now=mcsmd.store_hosted.now() + 2 * TAG)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "suspended")
        mcsmd.passes.issue_pass(uid, "premium", 30, 2, 8192)
        mcsmd.tick()
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "hosted")

    def test_meldung_verschwindet_nach_dem_starten(self):
        token, uid = self.make_user()
        self.issue_pass(uid, kind="premium", days=1)
        server = self.premium_instanz(token)
        mcsmd.tick(now=mcsmd.store_hosted.now() + 2 * TAG)
        self.issue_pass(uid, kind="premium", days=30)
        self.assertIn(server["id"], mcsmd._runtime["woken"])
        mcsmd._woken_vergessen(server["id"])
        status, data = self.call("GET", "/api/me", token=token)
        self.assertEqual(status, 200)
        self.assertFalse([n for n in data["notices"] if n["kind"] == "wieder_startbar"])


# ---------------------------------------------------------------- Abbruch einer Übertragung

class AbbruchTest(Basis):
    """Befund D: nach einem Abbruch darf auf dem Root nichts Fertiges liegen bleiben."""

    def test_abbruch_raeumt_den_ordner(self):
        token, uid = self.make_user()
        server = self.make_server(token)
        inhalt = b"Welt" * 64
        manifest = {"files": [{"path": "welt/level.dat", "size": len(inhalt),
                               "sha256": sha256_of(inhalt)}]}
        status, data = self.call("POST", f"/api/servers/{server['id']}/upload/begin",
                                 {"manifest": manifest, "purpose": "instance"}, token=token)
        self.assertEqual(status, 200, data)
        sid = data["session"]["id"]
        status, data = self.call("POST", f"/api/servers/{server['id']}/upload/chunk", raw=inhalt,
                                 token=token,
                                 headers={"X-MCSM-Session": sid, "X-MCSM-Path": "welt/level.dat",
                                          "X-MCSM-Offset": "0"})
        self.assertEqual(status, 200, data)
        ordner = mcsmd.sources_linux.users_dir() / uid / server["id"]
        self.assertTrue((ordner / "welt" / "level.dat").is_file())

        status, data = self.call("POST", f"/api/servers/{server['id']}/upload/abort",
                                 {"session": sid, "back_to_local": True}, token=token)
        self.assertEqual(status, 200, data)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "local_only")
        self.assertFalse(ordner.exists(),
                         "Nach dem Abbruch liegen noch Dateien auf dem Root-Server")
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["size_bytes"], 0)

    def test_abbruch_ohne_rueckkehr_laesst_die_uebertragung_stehen(self):
        """Ohne „back_to_local“ will der Benutzer später weitermachen – dann bleibt alles."""
        token, uid = self.make_user()
        server = self.make_server(token)
        inhalt = b"Welt" * 64
        manifest = {"files": [{"path": "welt/level.dat", "size": len(inhalt),
                               "sha256": sha256_of(inhalt)}]}
        status, data = self.call("POST", f"/api/servers/{server['id']}/upload/begin",
                                 {"manifest": manifest, "purpose": "instance"}, token=token)
        sid = data["session"]["id"]
        self.call("POST", f"/api/servers/{server['id']}/upload/chunk", raw=inhalt, token=token,
                  headers={"X-MCSM-Session": sid, "X-MCSM-Path": "welt/level.dat",
                           "X-MCSM-Offset": "0"})
        status, _ = self.call("POST", f"/api/servers/{server['id']}/upload/abort",
                              {"session": sid}, token=token)
        self.assertEqual(status, 200)
        ordner = mcsmd.sources_linux.users_dir() / uid / server["id"]
        self.assertTrue((ordner / "welt" / "level.dat").is_file())
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "uploading")

    def test_aufraeumen_findet_liegengebliebene_dateien(self):
        token, uid = self.make_user()
        server = self.make_server(token)
        ordner = mcsmd.sources_linux.users_dir() / uid / server["id"]
        ordner.mkdir(parents=True, exist_ok=True)
        (ordner / "rest.dat").write_bytes(b"alt")
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["state"], "local_only")
        mcsmd.housekeeping(mcsmd.store_hosted.now())
        self.assertFalse(ordner.exists())


# ---------------------------------------------------------------- Neustart ohne Unterbrechung

class NeustartTest(Basis):
    """Befund B: ein Neustart des Dienstes darf die Kundenserver nicht herunterfahren."""

    def test_beenden_stoppt_keine_server(self):
        token, uid = self.make_user()
        self.issue_pass(uid)
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        mcsmd.instances.set_running(server["id"], True)
        mcsmd._shutdown_state["done"] = False
        try:
            mcsmd.shutdown()
        finally:
            mcsmd._shutdown_state["done"] = False
            mcsmd.STOP_EVENT.clear()
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertTrue(inst["running"],
                        "Der Datensatz darf beim Beenden auf „läuft“ stehen bleiben – daran "
                        "erkennt der nächste Start, dass er den Prozess übernehmen darf.")

    def test_mit_mcsm_stop_servers_wird_doch_gestoppt(self):
        token, uid = self.make_user()
        self.issue_pass(uid)
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        mcsmd.instances.set_running(server["id"], True)
        os.environ["MCSM_STOP_SERVERS"] = "1"
        mcsmd._shutdown_state["done"] = False
        try:
            mcsmd.shutdown()
        finally:
            os.environ.pop("MCSM_STOP_SERVERS", None)
            mcsmd._shutdown_state["done"] = False
            mcsmd.STOP_EVENT.clear()
        self.assertFalse(mcsmd.instances.get_instance(server["id"])["running"])

    def test_datensatz_ohne_prozess_wird_berichtigt(self):
        token, uid = self.make_user()
        self.issue_pass(uid)
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        mcsmd.instances.set_running(server["id"], True)
        mcsmd.adopt_running()
        self.assertFalse(mcsmd.instances.get_instance(server["id"])["running"])

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Der Lebendtest eines fremden Prozesses liest /proc – nur unter Linux.")
    def test_uebernommener_prozess_meldet_sich_als_uebernommen(self):
        """Der Runner führt einen fremden Prozess weiter – ohne Konsole, aber stoppbar."""
        spec = mcsmd.runner.RunSpec(instance_id="pruefung", directory=self._instanzordner(),
                                    kind="java", java_major=21)
        run = mcsmd.runner.Runner(spec)
        eigen = os.getpid()
        run.adopt(eigen, started_at=mcsmd.store_hosted.now() - 60)
        try:
            self.assertTrue(run.running)
            self.assertTrue(run.adopted)
            self.assertTrue(run.status()["uebernommen"])
            self.assertGreaterEqual(run.uptime, 60)
            with self.assertRaises(ValueError) as fehler:
                run.send("list")
            self.assertIn("stoppen", str(fehler.exception))
            zeilen = run.console(0)["lines"]
            self.assertTrue(any("übernommen" in z for z in zeilen), zeilen)
        finally:
            run.proc = None

    def _instanzordner(self):
        folder = pathlib.Path(self.tmp) / "users" / "konto" / "pruefung"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "server.jar").write_bytes(b"x")
        return folder


# --------------------------------------------------------------------------- Absicherungen

class HostTrennungTest(Basis):
    """Die Betreiberwege gibt es nur unter dem Verwaltungsnamen, nicht unter dem API-Namen."""

    def test_admin_weg_unter_api_namen_404(self):
        status, data = self.call("GET", "/api/admin/status", token=self.admin_token,
                                 headers={"Host": "api.arcardia-nexus.de"})
        self.assertEqual(status, 404, data)
        self.assertIn("nicht", data["error"])

    def test_admin_weg_unter_verwaltungsnamen_geht(self):
        for host in ("admin.arcardia-nexus.de", "127.0.0.1"):
            status, data = self.call("GET", "/api/admin/status", token=self.admin_token,
                                     headers={"Host": host})
            self.assertEqual(status, 200, (host, data))

    def test_eigene_wege_bleiben_unter_beiden_namen(self):
        token, _uid = self.make_user()
        for host in ("api.arcardia-nexus.de", "admin.arcardia-nexus.de"):
            status, data = self.call("GET", "/api/servers", token=token,
                                     headers={"Host": host})
            self.assertEqual(status, 200, (host, data))


class VorabBremseTest(Basis):
    """Die Bremse greift, bevor sessions.json und users.json für ein erfundenes Token gelesen werden."""

    def test_vorab_topf_ist_grosszuegiger_als_angemeldet(self):
        vorab = mcsmd.RATE_LIMITS["vorab"][0]
        self.assertGreater(vorab, mcsmd.RATE_LIMITS["angemeldet"][0])

    def test_vorab_bremse_sperrt_erfundene_token(self):
        mcsmd.BREMSE.leeren()
        grenze = mcsmd.RATE_LIMITS["vorab"][0]
        try:
            mcsmd.RATE_LIMITS["vorab"] = (3, 60, 60)
            gesehen = []
            for _ in range(6):
                status, _data = self.call("GET", "/api/servers", token="erfunden")
                gesehen.append(status)
            self.assertIn(401, gesehen)
            self.assertIn(429, gesehen, gesehen)
        finally:
            mcsmd.RATE_LIMITS["vorab"] = (grenze, 60, 60)
            mcsmd.BREMSE.leeren()


class MarkenSperreTest(Basis):
    """Eine frei gewordene Unterdomäne geht nicht sofort an ein fremdes Konto."""

    def test_geloeschte_marke_bleibt_gesperrt(self):
        token_a, _uid_a = self.make_user(name="Konto A")
        erster = self.make_server(token_a, name="Familien Welt")
        self.assertEqual(erster["marke"], "familien-welt")
        status, _ = self.call("DELETE", f"/api/servers/{erster['id']}", token=token_a)
        self.assertEqual(status, 200)
        gesperrt = mcsmd.routen.gesperrte_marken()
        self.assertIn("familien-welt", gesperrt)

        token_b, _uid_b = self.make_user(name="Konto B")
        zweiter = self.make_server(token_b, name="Familien Welt")
        self.assertNotEqual(zweiter["marke"], "familien-welt")

    def test_eigenes_konto_bekommt_seine_marke_wieder(self):
        token, _uid = self.make_user()
        erster = self.make_server(token, name="Familien Welt")
        status, _ = self.call("DELETE", f"/api/servers/{erster['id']}", token=token)
        self.assertEqual(status, 200)
        wieder = self.make_server(token, name="Familien Welt")
        self.assertEqual(wieder["marke"], "familien-welt")

    def test_sperre_laeuft_ab(self):
        mcsmd.routen.marke_aufgeben("alte-welt", "konto-x")
        self.assertIn("alte-welt", mcsmd.routen.gesperrte_marken())
        spaeter = mcsmd.store_hosted.now() + (mcsmd.routen.SPERRE_TAGE + 1) * TAG
        self.assertNotIn("alte-welt", mcsmd.routen.gesperrte_marken(now=spaeter))
        self.assertEqual(mcsmd.routen.sperrliste_aufraeumen(now=spaeter), 1)


class DateiHashTest(Basis):
    """`action=info&hash=1` liefert die Prüfsumme – dafür braucht es nicht das ganze Manifest."""

    def test_info_mit_pruefsumme(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        folder = mcsmd.instance_dir(mcsmd.instances.get_instance(server["id"]))
        (folder / "world").mkdir(parents=True, exist_ok=True)
        (folder / "world" / "level.dat").write_bytes(b"welt")
        status, data = self.call("GET", f"/api/servers/{server['id']}/files"
                                        f"?action=info&path=world/level.dat&hash=1", token=token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["size"], 4)
        self.assertEqual(data["sha256"], sha256_of(b"welt"))

    def test_ohne_hash_keine_pruefsumme(self):
        token, _uid = self.make_user()
        server = self.make_server(token)
        self.set_state(server["id"], "hosted")
        folder = mcsmd.instance_dir(mcsmd.instances.get_instance(server["id"]))
        (folder / "server.properties").write_text("x=1", encoding="utf-8")
        status, data = self.call("GET", f"/api/servers/{server['id']}/files"
                                        f"?action=info&path=server.properties", token=token)
        self.assertEqual(status, 200, data)
        self.assertNotIn("sha256", data)


# ---------------------------------------------------------------- Feste Ports

class PortVergabeTest(Basis):
    """Befund: „Eutopia“ stand mit ``ports: {}`` in der Datenbank – und damit nicht in der
    Tabelle des Verteilers. Der Port muss schon beim Anlegen da sein – aber nur für eine Instanz,
    die wirklich auf dem Root liegt."""

    def test_instanz_auf_dem_root_hat_sofort_einen_port(self):
        """Premium: sie entsteht direkt hier und muss ab der ersten Sekunde erreichbar sein."""
        token, uid = self.make_user()
        self.issue_pass(uid, kind="premium", days=1)
        status, data = self.call("POST", "/api/servers",
                                 {"name": "Mein Server", "origin": "premium"}, token=token)
        self.assertEqual(status, 201, data)
        server = data["server"]
        self.assertEqual(server["state"], "hosted")
        self.assertTrue(server["ports"].get("java"), "Beim Anlegen fehlt der Port")
        self.assertEqual(server["ports"]["java"], mcsmd.ports_live_of(server["id"])["port"])
        self.assertNotEqual(server["ports"]["java"], mcsmd.routen.ROUTER_PORT)
        self.assertIn("mein-server", json.loads(
            mcsmd.routen.routes_path().read_text(encoding="utf-8")))

    def test_instanz_nur_auf_dem_pc_bekommt_keinen_port(self):
        """Sonst räumte ein einziges Konto den Bereich mit Servern leer, die es nie hochlädt.

        ``MAX_INSTANCES_PER_USER`` erlaubt 500 angelegte Server je Konto, der Java-Bereich hat
        rund 136 Ports für **alle** Konten. Eine Instanz im Zustand ``local_only`` steht ohnehin
        nicht in ``routes.json`` – der Port täte dort nichts.
        """
        token, _uid = self.make_user()
        server = self.make_server(token, name="Nur auf dem PC")
        self.assertEqual(server["state"], "local_only")
        self.assertEqual(server["ports"], {})
        self.assertEqual(mcsmd.ports_live_of(server["id"]), {})
        self.assertNotIn("nur-auf-dem-pc", json.loads(
            mcsmd.routen.routes_path().read_text(encoding="utf-8")))

    def test_viele_angelegte_instanzen_verbrauchen_den_bereich_nicht(self):
        token, _uid = self.make_user()
        for nummer in range(12):
            self.make_server(token, name=f"Welt {nummer}")
        self.assertEqual(mcsmd.POOL.free_ports(mcsmd.ports.JAVA_POOL),
                         mcsmd.ports.POOLS[mcsmd.ports.JAVA_POOL].size - 1,
                         "Angelegte Server dürfen nur den Port des Verteilers belegen")

    def test_port_kommt_mit_der_uebertragung(self):
        """Sobald die Instanz hier liegt, braucht sie ihre Adresse – ohne ersten Start."""
        token, _uid = self.make_user()
        server = self.make_server(token, name="Mein Server")
        self.assertEqual(server["ports"], {})
        gehostet = self.set_state(server["id"], "hosted")
        self.assertTrue(gehostet["ports"].get("java"), "Nach dem Hochladen fehlt der Port")
        data = json.loads(mcsmd.routen.routes_path().read_text(encoding="utf-8"))
        self.assertEqual(data["mein-server"]["port"], gehostet["ports"]["java"])

    def test_gehostete_instanz_steht_ohne_ersten_start_in_der_tabelle(self):
        token, _uid = self.make_user()
        server = self.make_server(token, name="Eutopia")
        self.set_state(server["id"], "hosted")
        data = json.loads(mcsmd.routen.routes_path().read_text(encoding="utf-8"))
        self.assertIn("eutopia", data)
        self.assertEqual(data["eutopia"]["instanz"], server["id"])
        self.assertTrue(data["eutopia"]["port"])

    def test_port_bleibt_nach_einem_stopp_erhalten(self):
        """Sonst verschwände ein schlafender Server wieder aus der Tabelle."""
        token, uid = self.make_user()
        server = self.make_server(token, name="Eutopia")
        port = self.set_state(server["id"], "hosted")["ports"]["java"]
        inst = mcsmd.instances.get_instance(server["id"])
        mcsmd.on_runner_exit(FalscherRunner(server["id"]), 0)
        self.assertEqual(mcsmd.ports_live_of(server["id"]).get("port"), port)
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["ports"]["java"], port)
        data = json.loads(mcsmd.routen.routes_path().read_text(encoding="utf-8"))
        self.assertEqual(data[inst["marke"]]["port"], port)

    def test_alte_instanz_ohne_port_wird_beim_start_versorgt(self):
        """Genau der Fall des Betreibers: Datensatz aus einer früheren Fassung, ports leer."""
        token, _uid = self.make_user()
        server = self.make_server(token, name="Eutopia")
        self.set_state(server["id"], "hosted")
        # Zustand von früher herstellen: Buchung weg, Datensatz leer.
        mcsmd.POOL.release(server["id"])
        mcsmd.instances.set_ports(server["id"], {})
        mcsmd.schreibe_routen("Test")
        self.assertNotIn("eutopia", json.loads(
            mcsmd.routen.routes_path().read_text(encoding="utf-8")))
        mcsmd.startup()
        inst = mcsmd.instances.get_instance(server["id"])
        self.assertTrue(inst["ports"].get("java"), "Der Port wurde nicht nachgetragen")
        data = json.loads(mcsmd.routen.routes_path().read_text(encoding="utf-8"))
        self.assertEqual(data["eutopia"]["port"], inst["ports"]["java"])

    def test_zurueckgeholte_instanz_gibt_den_port_zurueck(self):
        """Wieder auf dem PC: der Port gehört nicht mehr dieser Instanz."""
        token, _uid = self.make_user()
        server = self.make_server(token, name="Mein Server")
        self.assertTrue(self.set_state(server["id"], "hosted")["ports"]["java"])
        self.set_state(server["id"], "local_only")
        self.assertEqual(mcsmd.instances.get_instance(server["id"])["ports"], {})
        self.assertEqual(mcsmd.ports_live_of(server["id"]), {})
        self.assertNotIn("mein-server", json.loads(
            mcsmd.routen.routes_path().read_text(encoding="utf-8")))


# ---------------------------------------------------------------- Ersatz für einen Server

class FalscherRunner:
    """Ersatz für einen laufenden Minecraft-Server: kein Java, kein Prozess, nur Buchführung."""

    def __init__(self, instance_id: str, *, spieler: int = 0, companion: bool = True,
                 ready: bool = True, antwortet: bool = True):
        class Spec:
            def __init__(self) -> None:
                self.instance_id = str(instance_id)
                self.companion = bool(companion)
                self.ram_mb = 2048
                self.is_java = True
                self.directory = None
                self.run_as = ""
                self.name = "Ersatz"

        self.spec = Spec()
        self.active_spec = self.spec
        self.running = True
        self.ready = bool(ready)
        self.stopping = False
        self.adopted = False
        self.befehle: list[str] = []
        self.gestoppt: list[int] = []
        self._spieler = int(spieler)
        self._antwortet = bool(antwortet)
        self._zeilen: list[str] = []

    def console(self, since: int = 0, tail=None) -> dict:
        return {"next": len(self._zeilen), "lines": list(self._zeilen[int(since):])}

    def send(self, command: str) -> None:
        text = str(command).strip()
        self.befehle.append(text)
        if text == "list" and self._antwortet:
            self._zeilen.append(f"There are {self._spieler} of a max of 20 players online:")

    def log(self, text: str) -> None:
        self._zeilen.append(str(text))

    def announce(self, text: str) -> bool:
        self.befehle.append(f"say {text}")
        return True

    def stop(self, timeout: int = 0, announce_seconds: int = 0) -> None:
        self.gestoppt.append(int(announce_seconds))
        self.running = False
        self.stopping = True

    def status(self) -> dict:
        return {"id": self.spec.instance_id, "running": self.running, "ready": self.ready}


# ---------------------------------------------------------------- Weckruf des Verteilers

class WeckrufTest(Basis):
    """Der Verteiler meldet „jemand will auf <Instanz>“ – über die Rückschleife mit Geheimnis."""

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.server = self.make_server(self.token, name="Eutopia")
        self.set_state(self.server["id"], "hosted")
        self.geheimnis = mcsmd.ensure_router_secret()
        self.starts: list[str] = []
        self.echter_start = mcsmd.start_instanz

        def ersatz(inst):
            iid = str(inst.get("id") or "")
            self.starts.append(iid)
            time.sleep(0.3)                      # ein echter Start braucht auch einen Moment
            mcsmd.REGISTRY._runners[iid] = FalscherRunner(iid)
            mcsmd.instances.set_running(iid, True)
            return {"port": 25566}, mcsmd.REGISTRY._runners[iid]

        mcsmd.start_instanz = ersatz

    def tearDown(self) -> None:
        mcsmd.start_instanz = self.echter_start
        mcsmd.WAKE_LOCKS.clear()
        super().tearDown()

    def wake(self, body=None, *, geheimnis: str | None = None, headers=None):
        kopf = dict(headers or {})
        wert = self.geheimnis if geheimnis is None else geheimnis
        if wert:
            kopf["X-MCSM-Router"] = wert
        return self.call("POST", "/api/router/wake",
                         body if body is not None else {"instanz": self.server["id"]},
                         headers=kopf)

    def test_das_geheimnis_liegt_nur_fuer_den_dienst_bereit(self):
        pfad = mcsmd.router_secret_path()
        self.assertTrue(pfad.is_file())
        self.assertGreaterEqual(len(self.geheimnis), 32)
        # Ein zweiter Aufruf legt kein neues an – der Verteiler soll nicht ausgesperrt werden.
        self.assertEqual(mcsmd.ensure_router_secret(), self.geheimnis)
        if hasattr(os, "geteuid"):                    # Rechte gibt es nur unter Linux
            self.assertEqual(pfad.stat().st_mode & 0o777, 0o600)

    def test_ohne_geheimnis_gibt_es_diese_adresse_nicht(self):
        status, data = self.wake(geheimnis="")
        self.assertEqual(status, 404, data)
        self.assertEqual(self.starts, [])

    def test_falsches_geheimnis_wird_abgewiesen(self):
        status, data = self.wake(geheimnis="x" * 64)
        self.assertEqual(status, 404, data)
        self.assertEqual(self.starts, [])

    def test_aufruf_aus_dem_offenen_netz_wird_abgewiesen(self):
        """Hinter nginx trägt jede Anfrage X-Forwarded-For – ein echter Weckruf nie."""
        status, data = self.wake(headers={"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(status, 404, data)
        status, data = self.wake(headers={"X-Real-IP": "203.0.113.7"})
        self.assertEqual(status, 404, data)
        self.assertEqual(self.starts, [])

    def test_weckruf_startet_den_server(self):
        status, data = self.wake()
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertTrue(data["gestartet"])
        self.assertEqual(data["meldung"], mcsmd.MELDUNG_STARTET)
        self.assertEqual(self.starts, [self.server["id"]])

    def test_die_marke_genuegt_als_angabe(self):
        status, data = self.wake({"marke": "eutopia"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["gestartet"])

    def test_unbekannter_server(self):
        status, data = self.wake({"instanz": "gibt-es-nicht"})
        self.assertEqual(status, 404, data)
        self.assertIn("gibt es hier nicht", data["meldung"])

    def test_laufender_server_wird_nicht_zweimal_gestartet(self):
        status, data = self.wake()
        self.assertEqual(status, 200, data)
        status, data = self.wake()
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertFalse(data["gestartet"])
        self.assertEqual(data["meldung"], mcsmd.MELDUNG_STARTET)
        self.assertEqual(len(self.starts), 1)

    def test_mehrere_gleichzeitige_versuche_starten_einmal(self):
        """Ein Spieler drückt fünfmal auf „Beitreten“ – der Server geht einmal an."""
        ergebnisse: list = []
        sperre = threading.Lock()

        def versuch():
            antwort = self.wake()
            with sperre:
                ergebnisse.append(antwort)

        faeden = [threading.Thread(target=versuch) for _ in range(5)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(30)
        self.assertEqual(len(ergebnisse), 5)
        self.assertEqual(len(self.starts), 1, "Der Server wurde mehrfach gestartet")
        for status, data in ergebnisse:
            self.assertEqual(status, 200, data)
            self.assertTrue(data["ok"], data)
            self.assertEqual(data["meldung"], mcsmd.MELDUNG_STARTET)

    def test_ohne_pass_nennt_die_meldung_den_grund(self):
        mcsmd.start_instanz = self.echter_start          # der echte Weg soll ablehnen
        status, data = self.wake()
        self.assertEqual(status, 200, data)
        self.assertFalse(data["ok"])
        self.assertIn("keinen gültigen Pass", data["meldung"])

    def test_zurueckgeholter_server_wird_nicht_geweckt(self):
        self.set_state(self.server["id"], "awaiting_pull")
        status, data = self.wake()
        self.assertEqual(status, 200, data)
        self.assertFalse(data["ok"])
        self.assertIn("zurückgeholt", data["meldung"])
        self.assertEqual(self.starts, [])


# ---------------------------------------------------------------- Ruhezustand bei Leerstand

class RuhezustandTest(Basis):

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.issue_pass(self.uid)
        self.server = self.make_server(self.token, name="Eutopia")
        self.set_state(self.server["id"], "hosted")
        self.iid = self.server["id"]

    def laufender(self, **mehr) -> FalscherRunner:
        """Die Instanz als laufend eintragen und einen Ersatz-Server anhängen."""
        lang_genug = mcsmd.store_hosted.now() - 10 * mcsmd.HIBERNATION_GRACE
        mcsmd.instances.set_running(self.iid, True, now=lang_genug)
        fake = FalscherRunner(self.iid, **mehr)
        mcsmd.REGISTRY._runners[self.iid] = fake
        return fake

    def warte_bis_aus(self, fake: FalscherRunner, frist: float = 8.0) -> None:
        ende = time.time() + frist
        while fake.running and time.time() < ende:
            time.sleep(0.05)
        self.assertFalse(fake.running, "Der Server ist nicht in den Ruhezustand gegangen")

    # -- Einstellungen -------------------------------------------------

    def test_vorgaben(self):
        self.assertTrue(self.server["hibernation"])
        self.assertEqual(self.server["hibernation_minutes"], 15)
        self.assertEqual(mcsmd.instances.HIBERNATION_MINUTES_DEFAULT, 15)

    def test_einstellungen_sind_ueber_die_api_aenderbar(self):
        status, data = self.call("POST", f"/api/servers/{self.iid}/settings",
                                 {"hibernation": False, "hibernation_minutes": 45},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["server"]["hibernation"])
        self.assertEqual(data["server"]["hibernation_minutes"], 45)
        status, data = self.call("GET", f"/api/servers/{self.iid}", token=self.token)
        self.assertFalse(data["server"]["hibernation"])
        self.assertEqual(data["server"]["hibernation_minutes"], 45)

    def test_unsinnige_wartezeit_wird_abgelehnt(self):
        status, data = self.call("POST", f"/api/servers/{self.iid}/settings",
                                 {"hibernation_minutes": 0}, token=self.token)
        self.assertEqual(status, 400, data)
        self.assertIn("Wartezeit", data["error"])

    def test_abgeschalteter_ruhezustand_steht_in_der_tabelle(self):
        self.call("POST", f"/api/servers/{self.iid}/settings", {"hibernation": False},
                  token=self.token)
        data = json.loads(mcsmd.routen.routes_path().read_text(encoding="utf-8"))
        self.assertFalse(data["eutopia"]["wecken"])
        self.call("POST", f"/api/servers/{self.iid}/settings", {"hibernation": True},
                  token=self.token)
        data = json.loads(mcsmd.routen.routes_path().read_text(encoding="utf-8"))
        self.assertTrue(data["eutopia"]["wecken"])

    def test_sleeping_wird_ausgeliefert(self):
        status, data = self.call("GET", f"/api/servers/{self.iid}", token=self.token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["server"]["sleeping"], "Gehostet und aus heißt: schläft")
        self.laufender()
        status, data = self.call("GET", f"/api/servers/{self.iid}", token=self.token)
        self.assertFalse(data["server"]["sleeping"])

    def test_schlafender_server_zaehlt_nicht_als_laufend(self):
        """Platz und Arbeitsspeicher sind im Pass wieder frei."""
        status, data = self.call("GET", "/api/me", token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["slots_used"], 0)
        self.assertEqual(data["ram_used_mb"], 0)
        self.assertEqual(data["ram_free_mb"], data["ram_total_mb"])
        # Und er lässt sich sofort wieder starten – die Prüfung sagt „ja“.
        status, data = self.call("GET", f"/api/servers/{self.iid}", token=self.token)
        self.assertTrue(data["server"]["check_start"]["ok"], data["server"]["check_start"])

    # -- Einschlafen ---------------------------------------------------

    def test_leerer_server_geht_nach_der_wartezeit_schlafen(self):
        mcsmd.instances.set_hibernation(self.iid, minutes=1)
        fake = self.laufender(spieler=0)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.ruhezustand_pruefen(jetzt)
        self.assertEqual(mcsmd._runtime["leer_seit"].get(self.iid), jetzt)
        self.assertTrue(fake.running, "Noch ist die Wartezeit nicht um")
        mcsmd.ruhezustand_pruefen(jetzt + 61)
        self.warte_bis_aus(fake)
        self.assertIn("save-all", fake.befehle)
        self.assertEqual(fake.gestoppt, [mcsmd.HIBERNATION_ANNOUNCE])

    def test_spieler_auf_dem_server_verhindert_den_ruhezustand(self):
        mcsmd.instances.set_hibernation(self.iid, minutes=1)
        fake = self.laufender(spieler=2)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.ruhezustand_pruefen(jetzt)
        mcsmd.ruhezustand_pruefen(jetzt + 3600)
        self.assertTrue(fake.running)
        self.assertEqual(fake.gestoppt, [])

    def test_unbekannte_spielerzahl_verhindert_den_ruhezustand(self):
        """Antwortet der Server nicht, wird niemand hinausgeworfen."""
        mcsmd.instances.set_hibernation(self.iid, minutes=1)
        fake = self.laufender(spieler=0, antwortet=False)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.ruhezustand_pruefen(jetzt)
        mcsmd.ruhezustand_pruefen(jetzt + 3600)
        self.assertTrue(fake.running)

    def test_abgeschalteter_ruhezustand_laesst_den_server_laufen(self):
        mcsmd.instances.set_hibernation(self.iid, enabled=False, minutes=1)
        fake = self.laufender(spieler=0)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.ruhezustand_pruefen(jetzt)
        mcsmd.ruhezustand_pruefen(jetzt + 3600)
        self.assertTrue(fake.running)
        self.assertEqual(mcsmd._runtime["leer_seit"], {})

    def test_schonfrist_nach_dem_start(self):
        mcsmd.instances.set_hibernation(self.iid, minutes=1)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.instances.set_running(self.iid, True, now=jetzt)
        fake = FalscherRunner(self.iid, spieler=0)
        mcsmd.REGISTRY._runners[self.iid] = fake
        mcsmd.ruhezustand_pruefen(jetzt + 120)
        self.assertEqual(mcsmd._runtime["leer_seit"], {})
        self.assertTrue(fake.running)
        # Nach der Schonfrist läuft die Uhr.
        spaeter = jetzt + mcsmd.HIBERNATION_GRACE + 1
        mcsmd.ruhezustand_pruefen(spaeter)
        self.assertEqual(mcsmd._runtime["leer_seit"].get(self.iid), spaeter)

    def test_welt_die_noch_laedt_wird_nicht_schlafen_gelegt(self):
        mcsmd.instances.set_hibernation(self.iid, minutes=1)
        fake = self.laufender(spieler=0, ready=False)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.ruhezustand_pruefen(jetzt)
        mcsmd.ruhezustand_pruefen(jetzt + 3600)
        self.assertTrue(fake.running)

    def test_laufende_uebertragung_verhindert_den_ruhezustand(self):
        mcsmd.instances.set_hibernation(self.iid, minutes=1)
        inhalt = b"Ein Plugin"
        status, data = self.call(
            "POST", f"/api/servers/{self.iid}/upload/begin",
            {"manifest": {"version": 1, "files": [
                {"path": "plugins/Neu.jar", "size": len(inhalt),
                 "sha256": sha256_of(inhalt)}]}},
            token=self.token)
        self.assertEqual(status, 200, data)
        self.assertTrue(mcsmd.uebertragung_laeuft(self.iid))
        fake = self.laufender(spieler=0)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.ruhezustand_pruefen(jetzt)
        mcsmd.ruhezustand_pruefen(jetzt + 3600)
        self.assertTrue(fake.running)
        self.assertEqual(mcsmd._runtime["leer_seit"], {})

    def test_ein_start_stellt_die_uhr_zurueck(self):
        mcsmd.instances.set_hibernation(self.iid, minutes=5)
        fake = self.laufender(spieler=0)
        jetzt = mcsmd.store_hosted.now()
        mcsmd.ruhezustand_pruefen(jetzt)
        self.assertIn(self.iid, mcsmd._runtime["leer_seit"])
        mcsmd._leerstand_vergessen(self.iid)
        self.assertEqual(mcsmd._runtime["leer_seit"], {})
        self.assertTrue(fake.running)

    def test_spielerzahl_kommt_zuerst_vom_begleit_plugin(self):
        """Mit status.json muss kein Konsolenbefehl geschickt werden."""
        inst = mcsmd.instances.get_instance(self.iid)
        ordner = mcsmd.companion.plugin_dir(mcsmd.instance_dir(inst))
        ordner.mkdir(parents=True, exist_ok=True)
        (ordner / "status.json").write_text(
            json.dumps({"online": 3, "updated": time.time()}), encoding="utf-8")
        fake = self.laufender(spieler=0)
        self.assertEqual(mcsmd.spielerzahl(mcsmd.instances.get_instance(self.iid), fake), 3)
        self.assertNotIn("list", fake.befehle)


# --------------------------------------------------------------------------- Xbox-Freunde-Modus

class XboxTest(Basis):
    """Die Dienst-Seite des Xbox-Freunde-Modus: Wege, Felder, Adresse und Port.

    Es wird kein Bot gestartet – geprüft wird, was der Dienst darüber sagt und womit er ihn
    starten würde. Der Bot selbst hat eigene Selbsttests (test_xbox.py).
    """

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.issue_pass(self.uid)
        self.server = self.make_server(self.token, name="Eutopia")
        self.set_state(self.server["id"], "hosted")
        self.iid = self.server["id"]
        self.inst = mcsmd.instances.get_instance(self.iid)
        self.folder = mcsmd.instance_dir(self.inst)
        self.folder.mkdir(parents=True, exist_ok=True)

    def mit_crossplay(self) -> None:
        """Geyser vortäuschen – ohne Crossplay hat der Xbox-Freunde-Modus keinen Bedrock-Port."""
        (self.folder / "plugins").mkdir(parents=True, exist_ok=True)
        (self.folder / "plugins" / "Geyser-Spigot.jar").write_bytes(b"kein echtes Jar")
        geyser = self.folder / "plugins" / "Geyser-Spigot"
        geyser.mkdir(parents=True, exist_ok=True)
        (geyser / "config.yml").write_text(
            "bedrock:\n  address: 0.0.0.0\n  port: 19140\n", encoding="utf-8")

    # -- Adresse und Port ----------------------------------------------

    def test_beworben_wird_die_oeffentliche_adresse(self):
        """Nie 127.0.0.1: die Konsole des Freundes baut die Verbindung selbst dorthin auf."""
        self.assertEqual(mcsmd.xbox_address(), "arcardia-nexus.de")
        self.assertNotIn(mcsmd.xbox_address().lower(), mcsmd.xbox.LOOPBACK)

    def test_adresse_ist_uebersteuerbar(self):
        os.environ[mcsmd.XBOX_ADDRESS_ENV] = "mc.example.org"
        try:
            self.assertEqual(mcsmd.xbox_address(), "mc.example.org")
        finally:
            os.environ.pop(mcsmd.XBOX_ADDRESS_ENV, None)

    def test_port_kommt_aus_der_geyser_konfiguration(self):
        self.mit_crossplay()
        inst = mcsmd.instances.get_instance(self.iid)
        self.assertEqual(mcsmd.geyser_bedrock_port(self.folder), 19140)
        self.assertEqual(mcsmd.xbox_bedrock_port(inst, self.folder), 19140)

    def test_gebuchter_port_hat_vorrang(self):
        self.mit_crossplay()
        mcsmd.POOL.allocate_for(self.iid, "java", geyser=True, max_players=20)
        inst = mcsmd.instances.get_instance(self.iid)
        gebucht = mcsmd.ports_live_of(self.iid)["bedrock_port"]
        self.assertEqual(mcsmd.xbox_bedrock_port(inst, self.folder), gebucht)

    def test_spec_nennt_adresse_port_und_namen(self):
        self.mit_crossplay()
        (self.folder / "server.properties").write_text(
            "motd=Eutopia Survival\nmax-players=42\n", encoding="utf-8")
        spec = mcsmd.build_xbox_spec(mcsmd.instances.get_instance(self.iid))
        self.assertEqual(spec.address, "arcardia-nexus.de")
        self.assertEqual(spec.port, 19140)
        self.assertEqual(spec.name, "Eutopia")
        self.assertEqual(spec.motd, "Eutopia Survival")
        self.assertEqual(spec.max_players, 42)
        self.assertTrue(spec.query_server)

    # -- Zustand -------------------------------------------------------

    def test_status_hat_die_felder_des_pc_programms(self):
        self.mit_crossplay()
        status, data = self.call("GET", f"/api/servers/{self.iid}/xbox", token=self.token)
        self.assertEqual(status, 200, data)
        for feld in ("enabled", "autostart", "installed", "running", "state", "code", "url",
                     "gamertag", "error", "address", "port", "host_name", "token_cached",
                     "state_text", "possible"):
            self.assertIn(feld, data)
        self.assertFalse(data["enabled"])
        self.assertFalse(data["running"])
        self.assertEqual(data["address"], "arcardia-nexus.de")
        self.assertEqual(data["port"], 19140)
        self.assertTrue(data["possible"])

    def test_status_auch_unter_dem_ausgeschriebenen_namen(self):
        self.mit_crossplay()
        status, data = self.call("GET", f"/api/servers/{self.iid}/xbox/status", token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["address"], "arcardia-nexus.de")

    def test_java_ohne_crossplay_nennt_den_grund(self):
        status, data = self.call("GET", f"/api/servers/{self.iid}/xbox", token=self.token)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["possible"])
        self.assertIn("Crossplay", data["reason"])
        self.assertIn("Crossplay", data["state_text"])

    def test_serveransicht_bringt_den_zustand_mit(self):
        status, data = self.call("GET", f"/api/servers/{self.iid}", token=self.token)
        self.assertEqual(status, 200, data)
        self.assertIn("xbox", data["server"])
        self.assertFalse(data["server"]["xbox_enabled"])
        self.assertTrue(data["server"]["xbox_autostart"])

    # -- Einrichten und Schalten ---------------------------------------

    def test_einrichten_ohne_crossplay_wird_abgelehnt(self):
        status, data = self.call("POST", f"/api/servers/{self.iid}/xbox/setup", {},
                                 token=self.token)
        self.assertEqual(status, 409, data)
        self.assertIn("Crossplay", data["error"])

    def test_stoppen_ohne_laufenden_bot(self):
        status, data = self.call("POST", f"/api/servers/{self.iid}/xbox/stop", {},
                                 token=self.token)
        self.assertEqual(status, 409, data)
        self.assertIn("läuft", data["error"])

    def test_ausschalten_setzt_die_einstellung(self):
        mcsmd.instances.set_xbox(self.iid, enabled=True)
        status, data = self.call("POST", f"/api/servers/{self.iid}/xbox/disable", {},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["server"]["xbox_enabled"])
        self.assertFalse(mcsmd.instances.xbox_enabled(mcsmd.instances.get_instance(self.iid)))

    def test_anmeldung_verwerfen_loescht_nur_den_token(self):
        self.mit_crossplay()
        token = mcsmd.xbox.token_path(self.folder)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("{}", encoding="utf-8")
        status, data = self.call("POST", f"/api/servers/{self.iid}/xbox/reset", {},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertFalse(token.exists())
        self.assertTrue((self.folder / "plugins" / "Geyser-Spigot.jar").is_file())

    def test_einstellungen_schalten_den_modus(self):
        """Die Schalter sind auch über den Einstellungen-Weg erreichbar (Reiter „Einstellungen“)."""
        status, data = self.call("POST", f"/api/servers/{self.iid}/settings",
                                 {"xbox_enabled": True, "xbox_autostart": False},
                                 token=self.token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["server"]["xbox_enabled"])
        self.assertFalse(data["server"]["xbox_autostart"])
        status, data = self.call("POST", f"/api/servers/{self.iid}/settings",
                                 {"xbox_enabled": "aus"}, token=self.token)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["server"]["xbox_enabled"])
        status, data = self.call("POST", f"/api/servers/{self.iid}/settings",
                                 {"xbox_enabled": "vielleicht"}, token=self.token)
        self.assertEqual(status, 400, data)

    def test_konsole_des_bots(self):
        status, data = self.call("GET", f"/api/servers/{self.iid}/xbox/console", token=self.token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["lines"], [])
        self.assertFalse(data["running"])

    def test_fremdes_konto_kommt_nicht_heran(self):
        """Ein anderes Konto darf den Bot weder sehen noch schalten (403, wie überall)."""
        fremd, _ = self.make_user("Fremd")
        for weg in ("xbox", "xbox/status", "xbox/console"):
            status, data = self.call("GET", f"/api/servers/{self.iid}/{weg}", token=fremd)
            self.assertEqual(status, 403, data)
        for weg in ("xbox/setup", "xbox/start", "xbox/stop", "xbox/reset", "xbox/disable"):
            status, data = self.call("POST", f"/api/servers/{self.iid}/{weg}", {}, token=fremd)
            self.assertEqual(status, 403, data)


class CrossplayChatTest(Basis):
    """``enforce-secure-profile`` und das Serverbild – beides vor jedem Start."""

    def setUp(self) -> None:
        super().setUp()
        self.token, self.uid = self.make_user()
        self.issue_pass(self.uid)
        self.server = self.make_server(self.token, name="Eutopia")
        self.set_state(self.server["id"], "hosted")
        self.folder = mcsmd.instance_dir(mcsmd.instances.get_instance(self.server["id"]))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.props = self.folder / "server.properties"

    def test_crossplay_erlaubt_bedrock_spielern_den_chat(self):
        self.props.write_text("motd=Eutopia\nenforce-secure-profile=true\n", encoding="utf-8")
        self.assertTrue(mcsmd.ensure_crossplay_chat(self.folder, True))
        werte = mcsmd.read_properties(self.props)
        self.assertEqual(werte["enforce-secure-profile"], "false")
        self.assertEqual(werte["motd"], "Eutopia")            # nichts anderes angefasst
        # Beim zweiten Mal wird nicht wieder geschrieben.
        self.assertFalse(mcsmd.ensure_crossplay_chat(self.folder, True))

    def test_ohne_crossplay_bleibt_die_einstellung_stehen(self):
        self.props.write_text("enforce-secure-profile=true\n", encoding="utf-8")
        self.assertFalse(mcsmd.ensure_crossplay_chat(self.folder, False))
        self.assertEqual(mcsmd.read_properties(self.props)["enforce-secure-profile"], "true")

    def test_serverbild_wird_gesetzt_aber_nie_ueberschrieben(self):
        if not mcsmd.SERVER_ICON.is_file():                   # pragma: no cover
            self.skipTest("assets/server-icon.png liegt hier nicht")
        ziel = self.folder / "server-icon.png"
        self.assertTrue(mcsmd.ensure_server_icon(self.folder))
        self.assertEqual(ziel.read_bytes(), mcsmd.SERVER_ICON.read_bytes())
        ziel.write_bytes(b"eigenes Bild des Besitzers")
        self.assertFalse(mcsmd.ensure_server_icon(self.folder))
        self.assertEqual(ziel.read_bytes(), b"eigenes Bild des Besitzers")


if __name__ == "__main__":
    unittest.main()
