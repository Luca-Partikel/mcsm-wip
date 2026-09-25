# Gruppe „konten“ – Benutzer, Einladungen, Pässe, Quoten, Instanzen

Dateien (alle fertig, kompilieren unter Python 3.11, nur Standardbibliothek):

| Datei | Inhalt |
|---|---|
| `hosted/core/store_hosted.py` | JSON-Ablage unter `/srv/mcsm/data`, atomar, gesperrt |
| `hosted/core/users.py` | Konten, Einladungscodes, Sitzungstoken, Discord-Schnittstelle |
| `hosted/core/passes.py` | Pässe, wirksame Grenzen, Restzeit, Ablaufwarnung |
| `hosted/core/instances.py` | Serverinstanzen, Zustände, **Startprüfung**, Ablauf-Aufräumen |
| `hosted/tests/test_passes.py` | 49 Selbsttests (Ablage, Konten, Einladungen, Sitzungen, Pässe) |
| `hosted/tests/test_instances.py` | 51 Selbsttests (Anlegen, Startprüfung, Zustände, Ablauf, Übersicht) |

Geprüft mit `python -m py_compile …` und `python -m unittest discover -s hosted/tests`
(lokal 3.14 und auf dem Root-Server mit 3.11: 100 Tests, alle grün; dazu ein Durchlauf als Benutzer
`mcsm` gegen echte Dateien – Rechte 600, Ordner 700).

Alle Module importieren sich gegenseitig **relativ** (`from . import store_hosted`), passen also in
`/opt/mcsm/core/`. `instances` → `passes` → `users` → `store_hosted`, keine Ringe.

---

## 1. Ablage – `store_hosted.py`

Ordner: `MCSM_DATA`, sonst `/srv/mcsm/data` (`DEFAULT_DATA_DIR`). Die Umgebungsvariable wird bei
**jedem** Aufruf gelesen, deshalb können Tests umschalten. Dateiaufbau je Datenart:

```json
{ "users": [ {...}, {...} ], "saved_at": 1700000000 }
```

`KINDS` = `users`, `invites`, `passes`, `instances`, `sessions` (Schlüssel = Wurzelschlüssel = Dateiname).

```python
lock() -> threading.RLock        # für Änderungen über mehrere Dateien: with store_hosted.lock():
now() -> int                     # Unix-Sekunden, ganzzahlig
new_id(nbytes=6) -> str          # 12 Hex-Zeichen
data_dir() -> Path ; ensure_dir() -> Path ; path_of(kind) -> Path
load(kind) -> list[dict]                     # fehlende Datei = []
save(kind, records) -> None                  # .tmp + fsync + os.replace, Datei 600, Ordner 700
update(kind, change: Callable[[list[dict]], T]) -> T    # laden, ändern, speichern in einem Zug
get_by(kind, field, value) -> dict | None
patch_by(kind, field, value, changes: dict, missing="") -> dict
remove_by(kind, field, value) -> int
```

Wichtig: eine **beschädigte** Datei liefert kein `[]`, sondern einen `ValueError` („… ist
beschädigt …“). Sonst würden beim nächsten Speichern alle Konten überschrieben. Der Daemon sollte
diesen Fehler beim Start einmal abfangen und laut melden, statt weiterzulaufen.

## 2. Konten – `users.py`

### JSON – `users.json`
```json
{"id": "9f3c…", "name": "Luca", "discord_id": "", "role": "user",
 "created_at": 1700000000, "blocked": false, "blocked_reason": ""}
```
`role` ∈ `admin` | `user`. Name: 2–32 Zeichen, Buchstaben (auch Umlaute), Ziffern, Leerzeichen,
`. _ -`; eindeutig ohne Rücksicht auf Groß-/Kleinschreibung. Mehrfache Leerzeichen, Tabulatoren und
Zeilenumbrüche werden zu einem Leerzeichen zusammengezogen, Steuerzeichen abgelehnt.
`discord_id`: nur Ziffern (15–22 Stellen), ebenfalls eindeutig.

```python
clean_name(raw) -> str ; clean_discord_id(raw) -> str ; clean_role(raw) -> str
all_users() ; get_user(user_id) ; find_by_name(name) ; find_by_discord(discord_id) ; count_admins(exclude="")
create_user(name, *, discord_id="", role="user", now=None) -> dict
rename_user(user_id, name) ; set_role(user_id, role) ; set_blocked(user_id, blocked, reason="")
link_discord(user_id, discord_id) ; delete_user(user_id) -> None
public_user(user) -> dict        # ohne discord_id, dafür "discord_linked": bool
```
* Das letzte nutzbare Admin-Konto lässt sich nicht entmachten, sperren oder löschen (`ValueError`).
* `set_blocked(…, True)` beendet **alle** Sitzungen des Kontos.
* `delete_user` räumt nur Konto + Sitzungen. **Pässe und Instanzen muss der Daemon vorher abräumen.**

### JSON – `invites.json`
```json
{"code": "KRF2TGQNSPAP", "created_by": "<admin-id>", "created_at": 0, "uses_max": 1,
 "expires_at": 0, "revoked_at": 0, "note": "", "redeemed_by": [{"user_id": "…", "at": 0}]}
```
`expires_at = 0` heißt „läuft nicht ab“. Gespeichert wird die Vergleichsform (12 Zeichen, ohne
Striche); für die Anzeige `format_code()` → `KRF2-TGQN-SPAP`. Zeichenvorrat ohne I/O/0/1.

```python
new_code() -> str ; normalize_code(raw) -> str ; format_code(code) -> str
create_invite(created_by, *, uses_max=1, days=7, note="", now=None) -> dict
get_invite(code) -> dict | None          # zeitkonstanter Vergleich, akzeptiert auch die Strichform
invite_state(invite, *, now=None) -> "open"|"used"|"expired"|"revoked"
invite_state_text(invite, *, now=None) -> str        # offen / aufgebraucht / abgelaufen / widerrufen
revoke_invite(code, *, now=None) -> dict
redeem_invite(code, name, *, discord_id="", now=None) -> dict   # -> das neue Konto
```
`redeem_invite` legt das Konto an und hakt die Einladung ab; scheitert der zweite Schritt, wird das
Konto wieder entfernt (kein halbes Konto). Für `POST /api/auth/invite` also genau ein Aufruf, danach
`create_session`.

### JSON – `sessions.json`
```json
{"token_hash": "<sha256 hex>", "user_id": "…", "created_at": 0, "expires_at": 0,
 "last_seen": 0, "revoked_at": 0, "note": "PC von Luca"}
```
**Das Klartext-Token wird nie gespeichert.** Es gibt es genau einmal – als Rückgabe von
`create_session`. Die Datei enthält nur SHA256.

```python
token_hash(token) -> str ; all_sessions()
create_session(user_id, *, days=30, note="", now=None) -> tuple[str, dict]   # (token, satz)
session_for_token(token, *, now=None) -> dict | None
user_for_token(token, *, now=None) -> dict | None     # <- das ruft die API bei jeder Anfrage auf
revoke_session(token, *, now=None) -> bool            # POST /api/auth/logout
revoke_all_sessions(user_id, *, now=None) -> int
sessions_of(user_id, *, now=None, only_valid=True) -> list[dict]   # ohne token_hash
purge_sessions(*, now=None, keep_days=7) -> int
```
`user_for_token` prüft Ablauf, Widerruf **und** die Sperre des Kontos und schreibt `last_seen`
höchstens alle 5 Minuten fort (`SESSION_TOUCH_SECONDS`). Rückgabe `None` → HTTP 401 mit
`{"error": "Bitte neu anmelden."}`.

### Discord (Ablauf kommt später)
```python
user_for_discord(discord_id, name="", *, invite_code="", now=None) -> dict
```
Findet das Konto zur `discord_id`. Gibt es keines **und** liegt kein Einladungscode vor, kommt ein
`ValueError` („… bitte zuerst einen Einladungscode einlösen.“) – eine Discord-Anmeldung allein darf
kein Konto anlegen. Der OAuth-Teil (Zustand, Code-Tausch, Profilabruf) gehört in den Daemon und ruft
danach nur diese Funktion und `create_session` auf.

## 3. Pässe – `passes.py`

### JSON – `passes.json`
```json
{"id": "…", "user_id": "…", "kind": "local", "days": 30, "max_concurrent": 2,
 "ram_total_mb": 8192, "issued_at": 0, "expires_at": 0, "revoked_at": 0,
 "issued_by": "<admin-id>", "note": ""}
```
Grenzen: `days` 1–3650, `max_concurrent` 1–10, `ram_total_mb` 1024–**10240**
(`RAM_TOTAL_MAX_MB` – 13 GB Maschine minus Luft für System und Java-Verwaltung).
`TEMPLATES` enthält Vorschläge für Tag/Woche/Monat.

```python
issue_pass(user_id, kind, days, max_concurrent, ram_total_mb, *, issued_by="", note="", now=None) -> dict
revoke_pass(pass_id, *, now=None) ; extend_pass(pass_id, days, *, now=None)
set_limits(pass_id, *, max_concurrent=None, ram_total_mb=None) ; delete_pass(pass_id)
all_passes() ; get_pass(pass_id) ; passes_of(user_id)          # neueste zuerst
pass_state(entry, *, now=None) -> "active"|"expired"|"revoked" ; is_active(entry, *, now=None)
active_passes(user_id, *, now=None, kind="") -> list[dict]      # spätestes Ende zuerst
allows_origin(entry, origin) -> bool ; has_premium(user_id, *, now=None) -> bool
effective_limits(user_id, *, now=None) -> dict
remaining_seconds(entry, *, now=None) ; remaining_text(entry, *, now=None) -> str
expiring_soon(minutes=10, *, now=None) -> list[dict]
newly_expired(since, *, now=None) -> list[dict]
public_pass(entry, *, now=None) -> dict ; limits_text(limits) -> str
gb_text(mb) -> str ; duration_text(seconds) -> str ; kind_text(kind) -> str
```

`effective_limits` – **größter** Wert, nicht Summe:
```json
{"max_concurrent": 2, "ram_total_mb": 8192, "premium": false, "pass_ids": ["…"],
 "pass_count": 2, "expires_at": 1700086400, "next_expiry": 1700003600}
```
Ohne gültigen Pass ist alles 0/leer. `expires_at` = spätestes Ende, `next_expiry` = nächstes Ende.

`extend_pass` verlängert ab dem alten Ende, bei einem schon abgelaufenen Pass ab **jetzt**; ein
zurückgezogener Pass lässt sich nicht verlängern. Ein Pass gilt bis `expires_at` **ausschließlich**
(`now >= expires_at` → abgelaufen).

Texte, die die Oberfläche direkt anzeigen kann: `remaining_text` → „läuft in 3 Tagen 4 Stunden ab“,
„läuft in 10 Minuten ab“, „läuft in weniger als einer Minute ab“, „abgelaufen“, „zurückgezogen“.
`limits_text` → „2 gleichzeitig laufende Server, zusammen 8 GB“ bzw. „kein gültiger Pass“.

## 4. Instanzen – `instances.py`

### JSON – `instances.json`
```json
{"id": "…", "owner": "<user-id>", "name": "Welt-A", "type": "java", "flavor": "paper",
 "version": "1.21.1", "ram_mb": 4096, "ports": {"java": 25565, "bedrock": 19132},
 "state": "hosted", "origin": "local", "running": false,
 "created_at": 0, "updated_at": 0, "started_at": 0, "parked_at": 0, "size_bytes": 0}
```
`type` ∈ `java`|`bedrock`, `flavor` ∈ `paper|vanilla|fabric|neoforge|forge|quilt`,
`origin` ∈ `local`|`premium`, `state` ∈ `local_only|uploading|hosted|awaiting_pull|downloading|suspended`.
Der Ordner auf der Platte heißt nach der `id` (`/srv/mcsm/users/<owner>/<id>/`), nie nach dem Namen.
Namen sind je Konto eindeutig (die Meldungen nennen sie). Ports: java 25565–25664, bedrock 19132–19231.

```python
create_instance(owner, name, *, server_type="java", flavor="paper", version="", ram_mb=4096,
                origin="local", ports=None, state="", geyser=False, now=None) -> dict
rename_instance ; set_ram ; set_version ; set_ports ; set_size ; delete_instance
set_state(instance_id, state, *, force=False, now=None) -> dict
set_running(instance_id, running, *, now=None) -> dict
release_instance(instance_id, *, now=None) -> dict
all_instances() ; get_instance(id) ; instances_of(owner) ; running_of(owner) ; is_running(inst)
ram_in_use_mb(owner) ; ports_in_use() ; free_ports(server_type, *, geyser=False) ; free_disk_bytes()
public_instance(inst) -> dict ; state_text(state) ; size_text(bytes) ; name_list(names)
check_start(instance_id, *, now=None, free_bytes=None) -> StartCheck
overview(user_id, *, now=None, free_bytes=None) -> dict
target_state_after_expiry(inst) ; uncovered(user_id, *, now=None) ; park_uncovered(user_id, *, now=None)
excess_running(user_id, *, now=None) ; premium_delete_candidates(*, keep_days=14, now=None)
total_size_bytes(user_id="")
```

* **Anlegen ist unbegrenzt** – es wird kein Pass geprüft, außer bei `origin="premium"`
  (dafür braucht es einen gültigen Premium-Pass). Premium-Instanzen starten im Zustand `hosted`,
  lokale in `local_only`.
* `set_state` erlaubt nur die Wege aus `TRANSITIONS`; `force=True` ist für den Betreiber und für das
  Parken beim Ablauf. Beim Verlassen von `hosted` wird `running` auf `false` gesetzt, beim Parken
  `parked_at` gefüllt, bei `local_only` die Größe genullt.
* `set_running(True)` geht nur im Zustand `hosted` und setzt `started_at` (davon hängt
  `excess_running` ab).
* `release_instance` (für `POST /api/servers/<id>/release`) verlangt Zustand `downloading` oder
  `awaiting_pull` und setzt auf `local_only`. **Erst danach** darf der Daemon den Ordner auf dem Root
  löschen; ob er den Datensatz behält oder `delete_instance` ruft, entscheidet er.

### Startprüfung – das Kernstück
```python
StartCheck(ok: bool, code: str, reason: str)      # NamedTuple, wahrheitswertig (bool(check) == ok)
```
`check_start` prüft in dieser Reihenfolge und bricht beim ersten Grund ab:

| `code` | wann | Beispielsatz |
|---|---|---|
| `kein_konto` | Konto weg | „Das Konto zu diesem Server gibt es nicht mehr …“ |
| `konto_gesperrt` | `blocked` | „Dein Konto ist gesperrt … Grund: Zahlung offen“ |
| `zustand` | nicht `hosted` | „„Welt-A“ wartet darauf, auf deinen PC zurückgeholt zu werden. …“ |
| `laeuft_schon` | läuft schon | „„Welt-A“ läuft schon.“ |
| `kein_pass` | kein gültiger Pass | „Du hast gerade keinen gültigen Pass – …“ |
| `kein_premium_pass` | Premium-Instanz, nur lokaler Pass | „„Root-Welt“ ist ein reiner Root-Server – …“ |
| `platte` | < 3 GB frei (`MIN_FREE_DISK_MB`) | „Auf dem Root-Server sind nur noch 2 GB frei – unter 3 GB …“ |
| `ram_zu_gross` | Server allein größer als der Pass | „Dein Pass erlaubt zusammen 4 GB, „Welt-A“ ist aber mit 6 GB eingerichtet – …“ |
| `anzahl` | `laufende >= max_concurrent` | „Dein Pass erlaubt 2 gleichzeitig laufende Server – es laufen bereits Welt-A und Welt-B.“ |
| `ram` | Summe zu groß | „Dein Pass erlaubt zusammen 8 GB; es laufen schon 6 GB (Welt-A), dieser Server braucht 4 GB.“ |

`ok` → `code="ok"`, `reason=""` (Konstante `instances.OK`). Eine unbekannte Kennung ist ein
`ValueError` (der Daemon antwortet darauf mit 404), alles andere ist ein normales `StartCheck` für
HTTP 409 mit `{"error": check.reason}`.
`free_bytes` mitgeben, wenn der Daemon den freien Platz schon kennt (sonst misst die Funktion selbst
mit `shutil.disk_usage`).

`overview(user_id)` liefert alles für `GET /api/me`: `limits`, `limits_text`, `running`
(als `public_instance`), `running_names`, `instances`, `ram_used_mb`, `ram_free_mb`, `ram_total_mb`,
`slots_used`, `slots_free`, `disk_free_bytes`, `disk_ok`.

## 5. Was der Daemon aufrufen muss

**Anmeldung**
* `POST /api/auth/invite`: `users.redeem_invite(code, name)` → `users.create_session(user["id"])`,
  Token einmal zurückgeben.
* `GET /api/auth/discord/callback`: `users.user_for_discord(discord_id, name, invite_code=…)` →
  `users.create_session(...)`.
* Jede geschützte Route: `users.user_for_token(token)`; `None` → 401.
* `POST /api/auth/logout`: `users.revoke_session(token)`.

**Jede Route, die einen Server betrifft**: `instances.get_instance(id)` und prüfen, dass
`inst["owner"] == user["id"]` **oder** `user["role"] == "admin"`. Das macht kein Modul für den Daemon –
sonst könnte jeder fremde Kennungen ausprobieren.

**Start** (`POST /api/servers/<id>/start`):
1. `check = instances.check_start(id, free_bytes=…)`; `if not check.ok:` → 409 `{"error": check.reason}`
2. Prozess starten, danach `instances.set_running(id, True)`. Beim Stoppen `set_running(id, False)`.

**Überwachungsschleife (etwa jede Minute)**
1. `passes.expiring_soon(10)` → je betroffenen Benutzer die laufenden Server ermitteln
   (`instances.running_of(user_id)`) und die 10-Minuten-Warnung schicken (`mcsmstop`, sonst `say`)
   – die Warnung selbst muss der Daemon sich merken, damit sie nicht jede Minute neu kommt.
2. `passes.newly_expired(letzter_lauf)` → betroffene Benutzer sammeln.
3. Je Benutzer: `instances.uncovered(user_id)` → diese Server sauber stoppen
   (`mcsmstop 0` bzw. `stop`), dann `instances.park_uncovered(user_id)`
   (lokal → `awaiting_pull`, Premium → `suspended`).
4. `instances.excess_running(user_id)` → nach einem Widerruf mit kleinerem Ersatzpass: diese Server
   in der gelieferten Reihenfolge stoppen (zuletzt gestartete zuerst).
5. Gelegentlich `users.purge_sessions()` und `instances.premium_delete_candidates()`
   (Letzteres nur melden – gelöscht wird auf Wunsch des Betreibers).

**Admin-Routen**: `users.create_invite`, `users.all_invites`, `users.set_blocked`, `users.set_role`,
`passes.issue_pass`, `passes.revoke_pass`, `passes.extend_pass`, `passes.set_limits`,
`instances.all_instances`, `passes.public_pass`, `users.public_user`, `instances.public_instance`.

**Fehlerbehandlung**: alle Module werfen `ValueError` mit einem fertigen deutschen Satz. Der Daemon
kann den Text unverändert als `{"error": "…"}` durchgeben (HTTP 400, bzw. 404 bei „gibt es nicht“).
Es steckt nie ein Pfad oder eine innere Kennung im Text, die nichts nach draußen soll.

## 6. Offene Punkte / Absprachen

1. **`hosted/core/__init__.py` fehlt noch.** Der Daemon-Agent sollte sie anlegen (die Module nutzen
   relative Importe). Meine Tests laden `hosted/core` bewusst unter einem eigenen Paketnamen
   (`mcsm_hosted`), weil im Projektwurzelverzeichnis der gleichnamige Ordner `core/` des
   PC-Programms liegt und sonst zuerst gefunden würde.
2. **Wer misst `size_bytes`?** Ich schreibe den Wert nur fort (`instances.set_size`). Das Ausrechnen
   (`paths`/`transfer`-Gruppe oder ein Zeitgeber im Daemon) liegt außerhalb dieser Gruppe.
3. **Der Ordner auf der Platte** wird von mir nie angefasst – kein `mkdir`, kein `rmtree`. Nach
   `create_instance` muss der Daemon `/srv/mcsm/users/<owner>/<id>/` anlegen, nach `release_instance`
   bzw. `delete_instance` löschen.
4. **Mehrfachwarnung:** `expiring_soon` ist zustandslos und liefert denselben Pass in jeder Minute
   des Fensters. Wer schon gewarnt wurde, muss der Daemon sich merken (z. B. Feld `warned_at` in
   seiner eigenen Laufzeitablage) – ich habe absichtlich nichts in `passes.json` dafür angelegt.
5. **Deckung Premium/lokal:** Ein Premium-Pass deckt auch hochgeladene Server ab (er ist der
   größere), ein lokaler Pass deckt keine Premium-Instanz. Falls das anders gewollt ist, steckt die
   Regel allein in `passes.allows_origin`.
6. **Obergrenze der Maschine:** `passes.RAM_TOTAL_MAX_MB = 10240` begrenzt nur den einzelnen Pass.
   Ob die **Summe aller gleichzeitig laufenden Server über alle Benutzer** noch in die 13 GB passt,
   prüft niemand – dafür braucht es eine Gesamtprüfung im Daemon (freier Arbeitsspeicher der
   Maschine vor dem Start). Vorschlag: vor `set_running` zusätzlich gegen
   `/proc/meminfo` prüfen und bei Enge einen eigenen Satz melden.
7. **Zeitzonen/Uhr:** alles in Unix-Sekunden, `now` ist überall überschreibbar (Tests). Für die
   Anzeige rechnet das PC-Programm um.
