# Gruppe „transfer“ – Pfade und Übertragungen

Dateien dieser Gruppe:

* `hosted/core/paths.py` – sichere Pfade im Instanzordner, Freigabe-Liste, Dateiliste
* `hosted/core/transfer.py` – Manifest, Upload/Download in Stücken, Prüfung, Löschen nach Rückholung
* `hosted/tests/test_paths.py`, `hosted/tests/test_transfer.py` – 49 Selbsttests, nur Standardbibliothek

Geprüft mit Python 3.14 (PC, 3 Symlink-Tests übersprungen, weil Windows keine Verknüpfungen ohne
Sonderrechte anlegt) **und** Python 3.11.2 auf dem Root-Server (alle 49 grün). Zusätzlich auf dem
Server ein Lasttest: 250 MB in 8-MB-Stücken, Abbruch bei 90 MB, Wiederaufnahme, `verify`, Löschen –
sauber durchgelaufen. Die Module liegen zum Prüfen schon in `/opt/mcsm/core/` und `/opt/mcsm/tests/`.

---

## 1. `core/paths.py`

Alles, was von außen als Pfad kommt, läuft zuerst hier durch. Fehler sind immer `ValueError` mit
einem deutschen Satz, den der Daemon unverändert als `{"error": ...}` ausgeben kann.

### Prüfen und auflösen

| Funktion | Bedeutung |
|---|---|
| `normalize(rel: str) -> str` | macht `ordner/datei` daraus; leerer String = Instanzordner. Wirft bei `..`, absolutem Pfad, `:`/`?`/`*`/`\|`/`"`/`<`/`>`, Steuerzeichen, Namen > 200 Bytes, Pfad > 1024 Zeichen, Tiefe > 32, Windows-Gerätenamen (`con`, `nul`, `lpt1` …), Namen mit Punkt am Ende |
| `check_name(name: str) -> str` | ein einzelner Name (für „Ordner anlegen“, „umbenennen“) |
| `parts(rel) -> list[str]` | Bestandteile eines geprüften Pfads |
| `resolve(root, rel, *, allow_symlinks=False) -> pathlib.Path` | **die zentrale Funktion.** Dreifach geprüft: Namensregeln, jeder vorhandene Zwischenschritt auf Verknüpfung, `realpath` gegen `realpath(root)`. Das Ziel muss nicht existieren (Upload) |
| `relative(root, path) -> str` | absoluter Pfad → geprüfter relativer Pfad |
| `inside(root, path) -> bool` | liegt der Pfad nach Auflösen aller Verknüpfungen im Instanzordner? |

Rückstriche werden wie Trennzeichen behandelt (`plugins\x.jar` → `plugins/x.jar`), äußere
Leerzeichen werden entfernt. Windows-verbotene Zeichen sind auch auf dem Linux-Server verboten,
damit der Ordner später wieder auf den PC zurückkommen kann.

### Freigabe-Liste

| Funktion | für |
|---|---|
| `check_transfer(rel) -> str` | Übertragungen: alles **außer** Verwaltungsdateien (`.mcsm`, `mcsm.json`, `mcsm-instanz.json`) und Teil-Dateien (`*.mcsmpart`). Servertechnik (`server.jar`, `libraries/…`) ist erlaubt, weil eine Instanz vollständig umzieht |
| `check_user_read(rel) -> str` | Anzeigen/Lesen: alles außer Verwaltungsdateien |
| `check_user_write(rel) -> str` | Speichern/Hochladen durch den Benutzer: keine Verwaltung, keine Technik (`TECH_NAMES`), im Wurzelverzeichnis nur bekannte Dateien (`USER_FILES`), bekannte Ordner (`USER_DIRS`) oder Textdateien – sonst der Hinweis „Plugins nach plugins, Mods nach mods …“ |
| `check_user_delete(rel) -> str` | Löschen: Welten und Plugins ja, Technik/Verwaltung/`server.properties` nein |
| `classify(rel, is_dir=False) -> str` | `world`, `plugins`, `plugin`, `config`, `log`, `backup`, `folder`, `tech`, `reserved`, `other` |
| `is_hidden(rel, kind=None) -> bool` | in der Liste zugeklappt (Technik, `.dat_old`, `session.lock` …) |
| `is_text(rel) -> bool` | im Editor anzeigbar (`TEXT_EXT`; Skripte absichtlich **nicht**) |

Konstanten: `TEXT_EXT`, `USER_DIRS`, `USER_FILES`, `TECH_NAMES`, `RESERVED_NAMES`,
`HIDDEN_NAMES`, `HIDDEN_EXT`, `PART_SUFFIX = ".mcsmpart"`, `MAX_EDIT_BYTES = 1_500_000`,
`MAX_REL_LEN`, `MAX_NAME_LEN`, `MAX_DEPTH`.

### Auflisten

* `list_dir(root, rel="") -> {"path": str, "entries": [...]}`
  Einträge: `name`, `path`, `is_dir`, `size` (bei Ordnern `None`), `mtime`, `kind`, `hidden`,
  `editable` – dieselben Schlüssel wie `core/manager.list_dir` im lokalen Programm, damit die
  Oberfläche gehostete und lokale Server gleich anzeigen kann. Verwaltungsdateien und Teil-Dateien
  kommen nicht vor.
* `entry_info(root, rel) -> dict` – ein einzelner Eintrag (wirft `OSError`, wenn er weg ist)
* `walk_files(root, rel="", *, skip_reserved=True)` – Generator über alle Dateien als relative
  Pfade, sortiert, ohne Verknüpfungen und ohne Verwaltungsdateien

---

## 2. `core/transfer.py`

### Datenformat: Manifest

```json
{
  "version": 1,
  "created_at": 1758790000,
  "file_count": 8,
  "total_bytes": 99299,
  "files": [
    {"path": "welt/level.dat", "size": 4096, "sha256": "<64 Hexzeichen>", "mtime": 1758789000}
  ]
}
```

`path` ist immer relativ mit `/`, `mtime` ist freiwillig (wird beim Ablegen mit `os.utime`
übernommen). Leere Dateien dürfen `sha256` weglassen, dann wird die Prüfsumme des leeren
Inhalts eingesetzt.

### Datenformat: Sitzung (`<transfer>/<id>/session.json`, atomar geschrieben)

```json
{
  "version": 1, "id": "<32 Hexzeichen>", "kind": "upload" | "download",
  "state": "open" | "done" | "aborted",
  "user_id": "", "instance_id": "i1", "target": "/srv/mcsm/users/u1/i1",
  "created_at": 0, "updated_at": 0, "finished_at": 0,
  "manifest": { … }, "done": ["welt/level.dat"], "skipped": ["server.jar"]
}
```

### Klassen

```python
class UploadSession(_ChunkSession)      # kind = "upload"   – PC → Root (Daemon nimmt an)
class DownloadSession(_ChunkSession)    # kind = "download" – Root → PC (empfangende Seite)
```

```python
@classmethod
begin(store_dir, target, manifest, *, user_id="", instance_id="",
      session_id=None, reserve=RESERVE_BYTES) -> Session
```
Prüft das Manifest streng, prüft **vor** dem ersten Byte den freien Platz, erkennt schon
vorhandene prüfsummengleiche Dateien (`skipped`, gelten sofort als fertig), legt leere Dateien
gleich an und schreibt die Sitzung. `target` muss ein absoluter Pfad sein.

| Methode | Rückgabe / Wirkung |
|---|---|
| `status()` | `{id, kind, state, target, instance_id, chunk_size, file_count, total_bytes, done_files, received_bytes, skipped_files, complete, missing[≤50], next, created_at, updated_at}` |
| `next_needed()` | `{"path", "offset", "size", "sha256"}` oder `None` |
| `missing(limit=50)` | dieselben Einträge als Liste – damit der PC mehrere Dateien parallel schicken kann |
| `write_chunk(rel, offset, data: bytes)` | `{"path", "offset", "size", "done", "complete"}`. `offset` muss der aktuellen Länge der Teil-Datei entsprechen, sonst nennt die Fehlermeldung das richtige Byte. Bei der letzten Lieferung wird SHA256 geprüft, die Teil-Datei per `os.replace` umbenannt und `mtime` gesetzt. Stimmt die Prüfsumme nicht, fliegt die Teil-Datei weg und die Datei beginnt wieder bei 0 |
| `finish(*, hashes=True)` | erst wenn alle Dateien fertig sind; prüft **alle** Prüfsummen erneut gegen das Manifest. `{"id", "files", "bytes", "skipped_files", "ok": True}` |
| `abort()` | Teil-Dateien weg, Zustand `aborted`; fertige Dateien bleiben liegen (nächster Versuch überspringt sie) |
| `discard()` | Teil-Dateien und Sitzungsordner ganz weg |
| `DownloadSession.run(fetch, *, chunk_size=CHUNK_SIZE, progress=None)` | Schleife: `fetch(pfad, versatz, laenge) -> bytes`; nach Verbindungsabbruch einfach erneut aufrufen. Ruft am Ende `finish()` |

### Modulfunktionen

| Funktion | Zweck |
|---|---|
| `build_manifest(folder, *, rel="", hashes=True) -> dict` | Ordner einlesen (SHA256 je Datei) |
| `check_manifest(obj) -> dict` | von außen gekommenes Manifest streng prüfen |
| `manifest_index(manifest) -> dict[str, dict]` | Pfad → Eintrag |
| `compare_manifests(expected, actual) -> dict` | zwei Manifeste vergleichen, ohne die Platte anzufassen |
| `verify(folder, manifest, *, hashes=True) -> dict` | `{ok, files, bytes, expected_files, expected_bytes, missing, missing_count, wrong_size, wrong_size_count, wrong_hash, wrong_hash_count, extra, extra_count}`. Zusätzliche Dateien machen `ok` **nicht** falsch |
| `confirm_pull(folder, manifest) -> dict` | **Die Bestätigung der Rückholung.** Wirft `ValueError`, solange etwas fehlt |
| `delete_tree(folder, allowed_root) -> {"deleted", "bytes", "files"}` | löscht den Instanzordner – nur echt innerhalb von `allowed_root`, nie `allowed_root` selbst, nie eine Verknüpfung |
| `read_chunk(root, rel, offset, length=CHUNK_SIZE) -> bytes` | Quelle für den Download |
| `dir_size(folder) -> int`, `dir_stats(folder) -> {"bytes","files"}` | Ordnergröße |
| `free_space(path) -> int`, `check_space(path, needed, *, reserve=RESERVE_BYTES)` | Platte prüfen |
| `human(bytes) -> str` | „2,4 GB“ für Meldungen |
| `problem_text(report) -> str` | ein Satz, was noch fehlt (für `{"error": …}`) |
| `open_session(store_dir, id)`, `forget_session(id)`, `list_sessions(store_dir)`, `cleanup_old(store_dir, *, max_age=7 Tage)` | Sitzungen verwalten |
| `new_id()`, `check_id(value)`, `sha256_file(path)`, `sha256_bytes(data)` | Kleinigkeiten |

Grenzen (Konstanten, notfalls anpassen): `CHUNK_SIZE = 8 MB`, `MAX_CHUNK_BYTES = 16 MB`,
`MAX_FILES = 200_000`, `MAX_FILE_BYTES = 6 GB`, `MAX_TOTAL_BYTES = 20 GB`,
`RESERVE_BYTES = 3 GB` (aus ARCHITEKTUR.md), `SESSION_MAX_AGE = 7 Tage`.

---

## 3. Was der Daemon aufrufen muss

`store_dir` ist immer `/srv/mcsm/transfer`, `instance_dir` immer
`/srv/mcsm/users/<user-id>/<instanz-id>` (vom Instanz-Modul, nicht von dieser Gruppe).

**Upload (Instanz vom PC auf den Root)**

1. `POST /api/servers/<id>/upload/begin` mit `{"manifest": {...}}`
   → `transfer.UploadSession.begin(STORE, instance_dir, manifest, user_id=…, instance_id=…)`
   → Antwort `session.status()`; Instanz auf `uploading` setzen.
   Der Platzfehler kommt von hier als `ValueError` (HTTP 507 oder 400).
2. `POST /api/servers/<id>/upload/chunk` – Kopf `X-MCSM-Session`, `X-MCSM-Path`, `X-MCSM-Offset`,
   Rumpf = rohe Bytes (kein Base64, kein JSON: 8 MB sollen nicht um ein Drittel wachsen).
   → `transfer.open_session(STORE, sid).write_chunk(pfad, versatz, rumpf)`
   Der Rumpf muss vor dem Lesen gegen `MAX_CHUNK_BYTES` begrenzt werden (`Content-Length` prüfen).
3. `GET /api/servers/<id>/upload/status?session=…` → `status()` (Wiederaufnahme nach Abbruch)
4. `POST /api/servers/<id>/upload/finish` → `finish()`, Instanz auf `hosted` setzen
5. `POST /api/servers/<id>/upload/abort` → `abort()`

**Download / Rückholung (Root auf den PC)**

1. `GET /api/servers/<id>/manifest` → `transfer.build_manifest(instance_dir)`
   (bei 3 GB dauert das Prüfsummenlesen einige Sekunden – nicht im Takt der Statusabfrage aufrufen,
   Ergebnis in der Instanz merken)
2. `GET /api/servers/<id>/download?path=…&offset=…&length=…`
   → `transfer.read_chunk(instance_dir, pfad, versatz, laenge)`, Antwort als
   `application/octet-stream`. Die Sitzungsverwaltung des Downloads liegt auf dem PC
   (`DownloadSession` in diesem Modul, dort mit `run(fetch)`).
3. `POST /api/servers/<id>/release` mit `{"manifest": {...}}` (das Manifest, das der PC **lokal**
   gebaut hat):
   ```python
   claimed = transfer.check_manifest(daten["manifest"])
   ist = transfer.build_manifest(instance_dir)
   bericht = transfer.compare_manifests(ist, claimed)     # stimmt die Behauptung mit dem Root überein?
   if not bericht["ok"]:
       raise ValueError("Die Rückholung ist noch nicht vollständig: " + transfer.problem_text(bericht))
   transfer.delete_tree(instance_dir, "/srv/mcsm/users")  # erst jetzt!
   ```
   `confirm_pull(ordner, manifest)` ist die gleiche Prüfung gegen eine **Platte** – sie gehört auf
   den PC (dort ist der Ordner) und ist hier mit enthalten, weil beide Seiten dasselbe Modul nutzen.

**Dateiverwaltung (`/api/servers/<id>/files`)**
`paths.list_dir`, `paths.resolve` + `paths.check_user_read` / `check_user_write` /
`check_user_delete`. Einzelne Dateien (Plugin, Welt-Zip) gehen über dieselbe Upload-Sitzung mit
einem Manifest aus einer Datei – dafür braucht es keinen zweiten Weg.

**Regelmäßig (Zeitgeber des Daemons, z. B. jede Stunde)**
`transfer.cleanup_old("/srv/mcsm/transfer")` – liegengebliebene Übertragungen samt Teil-Dateien.

**Fehlerbehandlung:** alles wirft `ValueError` mit fertigem deutschen Satz →
`{"error": str(exc)}`. `OSError` wird in den schreibenden Wegen schon in `ValueError` verwandelt;
bei `read_chunk`/`build_manifest` kann noch ein `OSError` durchkommen (Platte, Rechte) – der
Daemon sollte ihn zu „Die Datei kann auf dem Server nicht gelesen werden.“ machen.

---

## 4. Annahmen über andere Gruppen

* Der Instanzordner wird **nicht** von dieser Gruppe bestimmt: der Daemon übergibt ihn als
  absoluten Pfad. Erwartet wird `/srv/mcsm/users/<user-id>/<instanz-id>`.
* Rechte (welcher Benutzer welche Instanz sehen darf) und Passprüfung passieren **vor** dem Aufruf.
  Diese Gruppe prüft nur Pfade, Prüfsummen und Platz.
* `paths`/`transfer` brauchen keinen laufenden Server und keine Instanzverwaltung – die Tests
  laufen mit reinen Temp-Ordnern.
* Die Tests laden `hosted/core` über einen eigenen Paketnamen (`hosted_core`), weil im
  Projektordner schon ein Paket `core` liegt (das lokale Programm) und ein einfaches
  `sys.path.insert` das falsche erwischt. Ein `hosted/core/__init__.py` ist dafür **nicht**
  nötig; wenn die Daemon-Gruppe eines anlegt, stört es auch nicht.

## 5. Offene Punkte

1. **Wer baut das Manifest auf dem PC?** Das lokale Programm braucht dieselbe Logik.
   Vorschlag: `hosted/core/transfer.py` und `paths.py` wandern unverändert (als Kopie oder über
   eine gemeinsame Ablage) ins PC-Programm – die Module hängen an nichts außer der
   Standardbibliothek und arbeiten auf beiden Seiten gleich.
2. **Prüfsummen bei jedem `finish`.** Bei 3 GB liest der Server den Ordner noch einmal komplett
   (auf diesem Server ~ eine halbe Minute). Falls das im Betrieb stört: `finish(hashes=False)`
   prüft nur Vorhandensein und Größe – die Prüfsumme je Datei wurde beim letzten Stück ohnehin
   schon geprüft.
3. **Dateien mit Namen, die Windows nicht kann** (z. B. `frage?.txt`, auf Linux möglich) werden
   von `walk_files` und damit vom Manifest übergangen. Sie gehen bei einer Rückholung also nicht
   mit. Offen: soll der Daemon sie dem Benutzer melden („2 Dateien mit unzulässigen Namen“)?
4. **Parallele Stücke.** `missing()` liefert bis zu 50 offene Dateien, die Sitzung ist
   threadsicher (ein Objekt je Kennung, eigenes Schloss). Mehrere Stücke **derselben** Datei
   gleichzeitig gehen aber nicht (der Versatz muss passen). Das ist Absicht; falls der PC mehr
   Durchsatz braucht, mehrere Dateien gleichzeitig schicken.
5. **`RESERVE_BYTES = 3 GB`** gilt für den ganzen Datenträger. Ob zusätzlich eine Grenze je
   Benutzer nötig ist (Pass-Feld „Plattenplatz“), gehört in die Pass-Gruppe – hier ist der Haken
   `check_space(…, reserve=…)` schon vorgesehen.
6. **Sitzungen sind an den Ordner gebunden, nicht an den Benutzer.** `session.json` merkt sich
   `user_id`; dass ein Benutzer nur seine eigene Sitzung bedienen darf, muss der Daemon prüfen.
