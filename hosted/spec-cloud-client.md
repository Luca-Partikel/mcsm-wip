# Gruppe „cloud-client“ – was das PC-Programm von der API erwartet

Gegenstück zu `hosted/mcsmd.py`. Geschrieben aus der Sicht des Programms auf dem PC; alles hier
Beschriebene ist in `core/cloud.py` **fertig umgesetzt** und gegen einen Nachbau des Daemons
(echte `hosted/core/paths.py` + `transfer.py`) mit 49 Prüfungen durchgespielt: Anmeldung,
Hochladen mit Abbruch und Fortsetzen, Steuern, Rückholen mit Prüfsummen, Freigeben, 401.

Dateien auf der PC-Seite:

| Datei | Inhalt |
|---|---|
| `core/cloud.py` | Anmeldung, Token-Ablage, alle API-Aufrufe, Manifest, Upload/Download, Freigabe |
| `app.py` | Routen `GET/POST /api/cloud/…` für die Oberfläche (deutsche Fehlermeldungen) |
| `web/app.js`, `index.html`, `style.css` | Bereich „Cloud“, Knöpfe an jedem Server, Hinweis beim Start |
| `core/store.py` | neues Feld `cloud` je Server (Verknüpfung zur Instanz auf dem Root) |

Grundlage: `ARCHITEKTUR.md`, `spec-konten.md`, `spec-transfer.md`, `spec-runner.md`.
Basisadresse: `https://api.arcardia-nexus.de` (überschreibbar mit `MCSM_CLOUD_API`, nur für Tests).
Alles nur Standardbibliothek; bei kaputter Zertifikatsprüfung übernimmt Windows-curl.

---

## 1. Was ich von der Gegenseite noch brauche

Vier Punkte; alles andere rufe ich so auf, wie `mcsmd.py` es heute schon anbietet.

### 1.1 Anmeldung über Discord vom PC aus (heute 501)

Der PC hat keine öffentliche Adresse, kann also keine Rückleitung von Discord entgegennehmen.
Deshalb: **Start → Browser → Abholen.**

```
GET /api/auth/discord/start?device=<PC-Name>          (ohne Token)
 -> {"url": "https://discord.com/oauth2/authorize?…",
     "state": "<32 Hex>", "poll_secret": "<>=16 Zeichen>", "expires_at": 1758800000}
```
Das Programm öffnet `url` im Browser. `state` und `poll_secret` bleiben **nur im Arbeitsspeicher**
des Programms (nichts davon landet auf der Platte).

```
GET /api/auth/discord/poll?state=<state>&secret=<poll_secret>     (ohne Token)
 -> {"pending": true}                                   solange der Benutzer noch nicht fertig ist
 -> {"token": "<Sitzungstoken>", "expires_at": 1761392000,
     "user": {"id": "…", "name": "Luca", "role": "user"}}          genau einmal
 -> 403 {"error": "…"}                                  falscher/abgelaufener Vorgang
```

Erwartungen:

* Das Token wird **nur gegen `state` + `poll_secret` ausgegeben** und danach nicht noch einmal
  (die Rückleitung im Browser darf es nicht anzeigen).
* Zeitkonstanter Vergleich von `poll_secret`, Vorgang nach 10 Minuten verfallen.
* Ich frage alle 2 Sekunden, höchstens 10 Minuten lang.
* `GET /api/auth/discord/callback` ist allein Sache des Browsers; ich rufe es nie auf. Schön wäre
  eine kleine Seite mit „Geschafft – du kannst das Fenster schließen“.
* `device` ist nur die Notiz für `users.create_session(note=…)` (Sitzungsliste).

Solange das nicht steht, funktioniert bei mir der Ersatzweg `POST /api/auth/invite`
(`{"code", "name", "note"}` → `{"token", "expires_at", "user"}`) – in der Oberfläche als
„Ich habe einen Einladungscode“ hinter dem Discord-Knopf.

### 1.2 `X-MCSM-Path` beim Hochladen entschlüsseln

Kopfzeilen können kein UTF-8 und kein Leerzeichen sicher tragen – die Standardwelt von Bedrock
heißt aber **`Bedrock level`** (mit Leerzeichen), Ordner heißen „Welt Nr. 2“ oder „Schatzsuche“.
Ich schicke den Pfad deshalb **prozentkodiert** (genau wie `h_download` ihn in seiner Antwort
kodiert) und **zusätzlich als Abfrageparameter**:

```
POST /api/servers/<id>/upload/chunk?session=<sid>&path=<pfad>&offset=<n>
X-MCSM-Session: <sid>
X-MCSM-Path:    Bedrock%20level/level.dat
X-MCSM-Offset:  8388608
Content-Type:   application/octet-stream
<rohe Bytes, höchstens 8 MB>
```

Nötig ist auf der Gegenseite **eines von beidem**:
`rel = urllib.parse.unquote(req.header("X-MCSM-Path"))` **oder** `rel = req.q("path")`
(`parse_qs` entschlüsselt schon). Ohne das landet auf dem Root ein Ordner `Bedrock%20level`.
Session und Offset schicke ich doppelt, damit auch da beides passt.

### 1.3 Adresse der Instanz mitgeben (Kür)

Die Oberfläche zeigt die Adresse zum Eintippen. Ich bilde sie notfalls selbst
(`name` → Kleinbuchstaben, alles außer `a–z0–9` → `-`, dann `.arcardia-nexus.de`), aber richtig
weiß es nur der Daemon. Wenn `server_view` zusätzlich

```json
{"address": "mein-server.arcardia-nexus.de"}
```

liefert (oder `host`/`hostname`), nehme ich diesen Wert. Der Port kommt aus `ports_live`,
sonst aus `ports` (`java` bzw. `bedrock`).

Seit es `hosted/router.py` gibt, ist das sogar die einzige verlässliche Quelle: der Verteiler
schlägt den **Schlüssel der Routing-Tabelle** nach (`mein-server` → Port/Instanz). Bitte genau
diesen Schlüssel + Basisdomain als `address` mitschicken – meine Notlösung aus dem Namen rät nur
und liegt daneben, sobald jemand seinen Server umbenennt oder zwei Namen denselben Schlüssel
ergeben.

### 1.4 Offene Übertragungen auflisten (Kür)

Die Sitzungskennung einer angefangenen Übertragung merke ich mir in `data/servers.json`
(`cloud.session`). Geht sie verloren (neu installiert, Datei gelöscht), beginne ich von vorn –
schon fertige Dateien überspringt `UploadSession.begin` ja dank Prüfsumme. Bequemer wäre

```
GET /api/servers/<id>/upload/sessions -> {"sessions": [ status(), … ]}
```

dann könnte ich eine unterbrochene Übertragung auch ohne lokale Notiz fortsetzen.

---

## 2. Was ich aufrufe (alles schon vorhanden)

Token immer als `Authorization: Bearer <token>`, Antworten JSON, Fehler `{"error": "…"}`.

| Zweck | Aufruf | Was ich aus der Antwort lese |
|---|---|---|
| Erreichbarkeit | `GET /api/health` | nichts weiter |
| Konto | `GET /api/me` | `user`, `passes[]`, `limits`, `limits_text`, `notices[]`, `machine`, `slots_used`, `ram_used_mb`, `ram_total_mb` |
| Liste | `GET /api/servers` | `servers[]` (siehe unten) |
| Anlegen | `POST /api/servers` | `{"server": {...}}` mit `id` |
| Einzeln | `GET /api/servers/<id>` | `server.running`, `server.state` |
| Starten | `POST /api/servers/<id>/start` | `ok`, `ports`, `server` |
| Stoppen | `POST /api/servers/<id>/stop` `{"announce_seconds": 10}` | `message` (zeige ich unverändert) |
| Befehl | `POST /api/servers/<id>/command` `{"command": "list"}` | `ok` |
| Konsole | `GET /api/servers/<id>/console?since=&tail=` | `next`, `lines[]`, `live.running` |
| Dateien | `GET /api/servers/<id>/files?path=&action=list\|read` | `entries[]` bzw. `text` |
| Speichern | `POST /api/servers/<id>/files` `{"action":"write","path","text"}` | `ok` |
| Löschen | `POST /api/servers/<id>/files` `{"action":"delete","path"}` | `ok` |
| Ordner anlegen | `POST /api/servers/<id>/files` `{"action":"mkdir","path"}` | `ok` |
| Umbenennen | `POST /api/servers/<id>/files` `{"action":"rename","path","new_path"}` | `ok` |
| Einzelne Datei prüfen | `GET /api/servers/<id>/files?action=info&path=&hash=1` | `size`, `mtime`, `sha256` |
| Übertragung | `POST /api/servers/<id>/upload/begin` `{"manifest", "purpose": "instance"}` | `session.id`, `session.missing[]`, `session.next`, `session.received_bytes`, `session.complete` |
| Stück | `POST /api/servers/<id>/upload/chunk` (siehe 1.2) | `offset` (neuer Stand), `done` |
| Stand | `GET /api/servers/<id>/upload/status?session=` | wie oben – **das ist meine Wiederaufnahme** |
| Fertig | `POST /api/servers/<id>/upload/finish` `{"session", "hashes": true}` | `report.files`, `report.bytes`, `server.state` |
| Abbruch | `POST /api/servers/<id>/upload/abort` `{"session", "back_to_local": true}` | `ok` |
| Dateiliste | `GET /api/servers/<id>/manifest` | `manifest` (Format aus spec-transfer.md) |
| Rückholung anmelden | `POST /api/servers/<id>/pull` | `server.state` = `downloading` |
| Stück holen | `GET /api/servers/<id>/download?path=&offset=&length=` | rohe Bytes |
| Freigeben | `POST /api/servers/<id>/release` `{"manifest": …}` | `ok`, `deleted` |

**Beim Anlegen** schicke ich `{"name", "type", "flavor", "version", "ram_mb", "origin": "local",
"geyser"}` – dieselben Werte, die der Server lokal hatte. Die Kennung vergibt der Root.

**Einzelne Dateien** (ein Plugin nachlegen, `world/level.dat` holen) laufen über dieselben Wege
wie die große Übertragung, nur mit `purpose: "files"` und einem Manifest mit genau einem Eintrag.
In `core/cloud.py` sind das:

```python
remote_file_delete(remote_id, path)          remote_file_mkdir(remote_id, path)
remote_file_rename(remote_id, path, neu)     remote_file_info(remote_id, path, mit_hash=False)
remote_file_upload(remote_id, quelle, ziel="")      # begin(files) → chunk → finish
remote_file_download(remote_id, rel, ziel=None)     # info(hash) → download-Stücke → SHA256
```

Im Programm hängen sie an `POST cloud/file/delete`, `cloud/file/mkdir`, `cloud/file/rename`,
`cloud/file/upload` und `cloud/file/download`. Der Upload nimmt entweder `local` (ein Pfad auf dem
PC – so ruft es ein Skript auf) oder `inhalt` als Base64 (so ruft es die Oberfläche auf, denn der
Browser gibt keinen Dateipfad heraus). Für einzelne Dateien muss der Server auf dem Root liegen
(`hosted`/`suspended`) und darf nicht laufen – das sagt der Daemon in einem deutschen Satz.

**Abläufe**

*Hochladen:* Manifest bauen → `upload/begin` → Schleife `upload/status` → für jede Datei aus
`missing[]` Stücke ab `offset` → wenn `complete`, `upload/finish`. Ein abgerissener Aufruf wird bis
zu fünfmal wiederholt, dazwischen frage ich `upload/status` neu – der Versatz kommt also immer von
der Gegenseite, nie aus meiner Erinnerung.

*Zurückholen:* läuft der Server, erst `stop` und warten (bis 150 s) → `pull` → `manifest` →
je Datei Stücke nach `<datei>.mcsmpart`, am Ende SHA256 prüfen und umbenennen → **alle** Dateien
gegen das Manifest prüfen (Größe + SHA256) → erst dann `release` mit genau dem Manifest, das der
Root mir geliefert hat. Bricht etwas ab, bleiben Teil-Dateien liegen und der nächste Anlauf setzt
dort auf; der Ordner auf dem Root wird nie vorher angefasst.

---

## 3. Was ich sicher **nicht** schicke

* Verwaltungsdateien `.mcsm`, `mcsm.json`, `mcsm-instanz.json` und `*.mcsmpart`
  (= `paths.RESERVED_NAMES`, `paths.check_transfer` würde sie ablehnen).
* Den Ordner **`backups/`** – Sicherungen sind lokal gemeint und würden die Übertragung leicht
  verdoppeln. Falls das auf dem Root erwünscht ist: eine Zeile in `core/cloud.py`
  (`SKIP_TOP_DIRS`).
* Dateien mit Namen, die `paths.normalize` ablehnen würde (Steuerzeichen, `:`/`?`/`*`, Punkt am
  Ende, Gerätenamen wie `con`, Tiefe > 32, Name > 200 Bytes). Sie werden übersprungen und in der
  Oberfläche als „übergangen“ genannt, statt die ganze Übertragung scheitern zu lassen.
* Dateien > 6 GB oder Ordner > 20 GB – dann melde ich vorher einen verständlichen Satz.

Beim Herunterladen überspringe ich Einträge, deren Name unter Windows unmöglich ist (z. B.
`frage?.txt`), und sage am Ende, wie viele es waren. **Wichtig:** solche Dateien stehen dann auch
nicht in meinem `release`-Manifest. Wenn `compare_manifests` sie auf dem Root findet, ist der
Bericht nicht `ok` und die Freigabe scheitert – mein Vorschlag (offener Punkt 3 aus
spec-transfer.md): `walk_files` lässt sie schon beim Manifest weg, damit beide Seiten dieselbe
Liste sehen.

---

## 4. Felder, auf die sich die Oberfläche stützt

`server_view` (aus `instances.public_instance` + Daemon): `id`, `name`, `type`, `flavor`,
`version`, `ram_mb`, `ram_text`, `ports`, `ports_live`, `state`, `state_text`, `origin`,
`running`, `size_bytes`, `size_text`, `parked_at`.
Unbekannte Felder stören nicht, fehlende fange ich ab – `state`, `running` und `name` brauche ich.

`passes[]` (aus `passes.public_pass`): `kind_text`, `state`, `remaining_text`, `max_concurrent`,
`ram_total_mb`, `ram_total_text`, `expires_at`. Angezeigt wird daraus z. B.
„Monatspass · gültig · läuft in 3 Tagen ab · 2 Server gleichzeitig, zusammen 8 GB“.

`notices[]`: `kind` (`pull`, `pull_running`, `suspended`, `expiring`) und `text`. Den Text zeige
ich **unverändert** an – er kommt aus dem Daemon, nicht aus meiner Übersetzungstabelle.

Der auffällige Hinweis beim Programmstart („Jetzt zurückholen“) hängt allein an
`state ∈ {awaiting_pull, downloading}`; `notices` ergänzt ihn.

---

## 5. Sicherheit auf der PC-Seite (damit die Gegenseite es weiß)

* Das Sitzungstoken liegt **nur** in `data/cloud-token.json`, Rechte per `icacls` auf den
  angemeldeten Windows-Benutzer beschränkt (eine einzige ACL, Vererbung entfernt). In
  `data/cloud.json` stehen nur Konto-Anzeige und Verknüpfungen – nie das Token.
* Das Token steht nie im Protokoll, nie in einer Antwort an die Oberfläche (`/api/cloud/status`
  ist token-frei) und nie in einer Kommandozeile: der curl-Rückfall bekommt die Kopfzeilen über
  eine `-K`-Konfigurationsdatei mit denselben eingeschränkten Rechten, die sofort gelöscht wird.
* Auf **401** lösche ich das Token sofort und die Oberfläche bietet eine neue Anmeldung an.
  Bitte 401 wirklich nur bei ungültiger Sitzung senden (für „gehört dir nicht“ ist 403 richtig),
  sonst melde ich den Benutzer bei einem Rechtefehler unnötig ab.
* `POST /api/auth/logout` rufe ich beim Abmelden auf; scheitert es, lösche ich trotzdem lokal.
* Ein Server ist immer nur an einer Stelle spielbar: solange `state` gehostet ist, sperrt das
  Programm die lokale Kopie (Starten, Neu installieren, Löschen) mit einem erklärenden Satz.

---

## 6. Offene Punkte

1. **Premium-Instanzen ohne lokale Heimat.** Holt jemand so eine Instanz auf den PC, lege ich
   lokal einen neuen Server an (Name, Typ, Version, RAM aus der Instanz) und markiere ihn als
   installiert. Ob der Root den Datensatz danach behalten soll, entscheidet `release`
   (`forget` schicke ich **nicht**, der Datensatz bleibt also bestehen).
2. **Ports auf dem Root.** Ich zeige `ports_live`, sonst `ports`. Wenn sich die Portvergabe bei
   jedem Start ändert, sollte die Adresse besser ohne Port funktionieren (SRV-Eintrag) – sonst
   müssen Freunde nach jedem Start eine neue Zahl eintippen.
3. **Größe der Instanz.** `size_bytes` zeige ich als Anhaltspunkt für die Dauer der Rückholung;
   wer ihn füllt (Zeitgeber oder `upload/finish`), ist mir gleich.
4. **Gleichzeitige Übertragungen** mache ich bewusst nicht: eine Datei nach der anderen, Stücke
   zu 8 MB. Reicht der Durchsatz nicht, wäre `missing()` (bis zu 50 Dateien) die Stelle, an der
   sich das parallelisieren ließe.
5. **Uhrzeit.** Ich rechne `expires_at` lokal in die Zeitzone des PCs um; alle Zeiten sind
   Unix-Sekunden.
