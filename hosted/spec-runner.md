# Gruppe „runner“ – Minecraft-Server unter Linux betreiben

Dateien dieser Gruppe:

* `hosted/core/sources_linux.py` – Downloads (Paper, Adoptium-JRE als tar.gz, Bedrock-Linux-ZIP,
  Geyser/Floodgate, ViaVersion/ViaBackwards), sicheres Entpacken, Java-Laufzeit einrichten
* `hosted/core/runner.py` – ein `Runner` je Instanz: starten, Konsole puffern, Befehle senden,
  sauber stoppen, Laufzeit und Speicher melden
* `hosted/core/ports.py` – Portvergabe aus den freigegebenen Bereichen
* `hosted/tests/test_runner.py` – 67 Selbsttests, nur Standardbibliothek, ohne Netz und ohne Server

Geprüft mit Python 3.14 (PC; ein Symlink-Test übersprungen, weil Windows keine Verknüpfungen ohne
Sonderrechte anlegt) **und** Python 3.11.2 auf dem Root-Server (alle 67 grün, keine Übersprünge).

Dazu vier echte Läufe auf `45.132.89.224` als Benutzer `mcsm`:

| Lauf | Ergebnis |
|---|---|
| Java | Temurin-JRE 21 geladen (52 MB, SHA256 aus der Adoptium-API) und entpackt, Paper 1.21.11 Build 132 (SHA256 aus der Fill-API), Start, `Done (…)` nach 12 s, `list`/`seed`, Gamerule nach dem Start gesetzt, Stopp mit Ankündigung in 3,8 s, Code 0, Welt gespeichert |
| Bedrock | `bedrock-server-1.26.51.1.zip` aus **bin-linux** (95 MB) entpackt, `bedrock_server` auf 0750, Start mit `LD_LIBRARY_PATH`, `Server started.`, `list` → „There are 0/5 players online“, Stopp in 0,5 s, Code 0 |
| Crossplay | Geyser-Spigot 2.11.3 + Floodgate 2.2.5 (SHA256 aus der Geyser-API) und ViaVersion/ViaBackwards 5.12.0 (SHA256 aus der Hangar-API) nach `plugins/`, `remove_crossplay` räumt sauber weg |
| Waise | Nach simuliertem Daemon-Neustart wird der noch laufende Server erkannt, ein zweiter Start abgelehnt, `stop_orphan` beendet ihn per SIGTERM in 1,6 s (Paper speichert dabei, Code 143) |

Der Zwischenspeicher auf dem Server ist danach schon gefüllt (`/srv/mcsm/cache` mit JRE 21 und der
Bedrock-ZIP, `/srv/mcsm/runtime/jre21`), die Testinstanzen sind gelöscht.

---

## 1. `core/ports.py`

### Bereiche

```python
POOLS = {"java":    PortRange("java",    TCP, 25565, 25700),
         "bedrock": PortRange("bedrock", UDP, 19132, 19300)}
```

Namen als Konstanten: `JAVA_POOL = "java"`, `BEDROCK_POOL = "bedrock"`, `TCP = "tcp"`, `UDP = "udp"`.
`firewall_ranges() -> [(proto, erster, letzter)]` liefert genau das, was die Firewall öffnen muss.

Wer welche Ports braucht:

| Serverart | Ports |
|---|---|
| Java ohne Crossplay | 1× TCP aus `java` |
| Java mit Geyser | 1× TCP aus `java` + 1× UDP aus `bedrock` |
| Bedrock (BDS mit NetherNet) | 1× TCP aus `java` (Verbindungsaufbau) + **Block** aus `bedrock` (Spieldaten) |

`udp_block_size(max_players) -> int` = `max(16, min(64, max_players + 8))` – BDS bindet pro Sitzung
einen eigenen UDP-Port aus `server-udp-ports` (genau wie im lokalen Programm).
`requirements(kind, *, geyser=False, max_players=10) -> [(pool, anzahl)]`.

### `class PortPool(*, probe: bool = True)`

`probe=True` prüft zusätzlich mit einem Bind-Versuch, ob der Port wirklich frei ist (fremder Dienst,
hängengebliebener Server). Für Selbsttests `probe=False`. Alle Methoden sind über eine `RLock`
abgesichert.

| Methode | Bedeutung |
|---|---|
| `allocate_for(owner, kind, *, geyser=False, max_players=10) -> {pool: PortLease}` | **die Hauptfunktion:** alle Ports einer Instanz in einem Schritt, alles oder nichts. Ports, die die Instanz nicht mehr braucht (Crossplay aus), werden dabei frei |
| `allocate(owner, pool, count=1) -> PortLease` | ersten freien Block im Bereich buchen; schon vorhandene Buchung derselben Größe bleibt bestehen |
| `reserve(owner, pool, first, count=1) -> PortLease` | bestimmten Block buchen (Wiederherstellen, Wunschport) |
| `release(owner, pool=None) -> int` | Ports einer Instanz freigeben (Anzahl gelöster Buchungen) |
| `assignment(owner) -> dict` | `{"port": 25565, "bedrock_port": 19132, "udp_first": …, "udp_last": …}` – fertig für `server.properties`; `bedrock_port` nur mit Geyser/Bedrock, `udp_*` nur bei einem Block |
| `lease(owner, pool)` / `leases(owner=None)` | einzelne bzw. alle Buchungen |
| `is_free(pool, first, count=1, *, owner=None)` | freie Prüfung; eigene Buchungen der Instanz stören nicht |
| `used_ports(pool) -> set[int]`, `free_ports(pool) -> int` | Auskunft |
| `owner_of(pool, port) -> str \| None` | **nur** für Protokoll und Admin-Ansicht |
| `snapshot() -> list[dict]` | zum Speichern |
| `restore(entries) -> list[str]` | zum Laden; die Rückgabe nennt verworfene Einträge in deutschen Sätzen fürs Protokoll |

Fehlermeldungen nennen absichtlich **keine fremde Instanz** („Port 25600 (TCP) ist schon belegt.“),
damit ein Benutzer nichts über die Server anderer erfährt.

### Datenformat `snapshot()` / `restore()`

```json
[{"owner": "a1b2c3d4", "pool": "bedrock", "proto": "udp", "first": 19132, "count": 18}]
```

`PortLease` hat `owner`, `pool`, `first`, `count` und die abgeleiteten `last`, `proto`, `ports()`.

### Fehler

`PortsExhausted(ValueError)` – „kein Platz mehr“ (der Daemon kann daraus einen eigenen HTTP-Status
machen, z. B. 503). Alles andere ist ein einfacher `ValueError` (ungültige Kennung, Port außerhalb
des Bereichs, unbekannter Bereich).

---

## 2. `core/sources_linux.py`

### Ordner

`data_root()` (Standard `/srv/mcsm`, per Umgebungsvariable `MCSM_DATA_ROOT` überschreibbar),
`set_data_root(path)`, `cache_dir()`, `runtime_dir()`, `users_dir()`, `transfer_dir()`,
`ensure_dirs()`.
`free_disk_mb(path=None) -> int` und `require_free_disk(need_mb=0, path=None)` – letzteres wirft,
wenn nach dem Download weniger als `MIN_FREE_MB = 2048` frei blieben. Vor jedem Download wird das
selbst geprüft.

### Laden

| Funktion | Bedeutung |
|---|---|
| `fetch_json(url, hosts=())`, `fetch_text(url, hosts=())` | API-Abfragen |
| `download(url, dest, progress=None, status=None, sha256="", hosts=()) -> str` | lädt und gibt die SHA256 zurück. Erst `.part`, dann `os.replace`; prüft Content-Length und Prüfsumme; bis zu 3 Versuche |
| `cached_download(url, dest, sha256="", …) -> pathlib.Path` | nimmt eine schon vorhandene, geprüfte Datei |
| `sha256_of(path) -> str` | Prüfsumme einer Datei |

`progress(done, total)` und `status(text)` sind dieselben Rückrufe wie im lokalen Programm, damit
der Daemon sie in sein Job-Objekt hängen kann.

**Sicherheit:** `_check_url` lässt nur HTTPS und nur die erwarteten Hosts zu (papermc, adoptium,
github, minecraft.net, geysermc, hangar). Eine manipulierte API-Antwort kann den Daemon also nicht
dazu bringen, irgendetwas aus dem Internet zu holen. Dateinamen aus API-Antworten laufen durch
`_safe_name`.

**Prüfsummen:** Paper, Adoptium, Geyser/Floodgate und Hangar nennen selbst eine SHA256 – die wird
geprüft. Mojang nennt für Bedrock keine; dort wird die berechnete Summe als `<datei>.sha256`
**nur im Zwischenspeicher** abgelegt und beim nächsten Griff geprüft, damit eine beschädigte
Datei aus dem Cache auffällt. In Instanzordnern entstehen keine `.sha256`-Dateien.

### Entpacken (ohne Fremdpakete, ohne `extractall`)

* `extract_zip(archive, target, keep=(), keep_dirs=(), status=None) -> int`
* `extract_tar(archive, target) -> int`

Abgelehnt werden absolute Pfade, Laufwerksangaben, alles mit `..`, harte Links, Geräte und FIFOs;
Symlinks nur, wenn ihr Ziel im Zielordner bleibt. Rechte werden auf `mode & 0o755` beschnitten
(kein setuid aus einem Archiv). `tarfile.extractall(filter=…)` gibt es in Python 3.11.2 noch nicht,
deshalb wird jeder Eintrag selbst geprüft. `keep`/`keep_dirs` lassen vorhandene Dateien und Ordner
in Ruhe – ein Bedrock-Update überschreibt so keine Welt und keine `server.properties`
(`BEDROCK_KEEP`, `BEDROCK_KEEP_DIRS`).

### Paper

`paper_versions()`, `paper_stable_build(v)`, `paper_download(v) -> (url, dateiname, sha256)`,
`paper_java_info(v) -> {"major": int, "flags": [...]}`, `java_major_for(v) -> int`,
`check_version(v)`, `version_tuple(v)`, `command_block_gamerule(v) -> str | None`
(ab 1.21.9 sind `pvp`/Befehlsblöcke Gamerules statt `server.properties` – siehe `after_ready_commands`).

`install_paper(version, target, eula_accepted, progress=None, status=None, jar="server.jar") -> dict`
legt `server.jar`, `build.txt` und `eula.txt` an und liefert
`{"version", "build", "jar", "java_major", "java_flags"}`. **Ohne `eula_accepted=True` wird nichts
eingerichtet** – die Bestätigung muss aus dem Programm des Benutzers kommen.

### Java-Laufzeit

`adoptium_jre(major) -> (url, release_name, sha256)` (linux/x64, `.tar.gz`),
`java_path(major) -> pathlib.Path | None` (vorhandene, wirklich startende Laufzeit),
`java_major_of(exe) -> int | None`,
`ensure_java(major, progress=None, status=None) -> pathlib.Path`.
`ensure_java` entpackt in `jre<major>.tmp`, prüft mit `java -version` und tauscht erst dann per
`os.replace` – es bleibt nie eine halb entpackte Laufzeit liegen.
`AVAILABLE_JAVA = (8, 11, 17, 21, 25)`.

### Bedrock

`bedrock_versions() -> {"latest": …, "preview": …}` (aus `serverBedrockLinux`),
`bedrock_download_url(version, preview=False)` (**bin-linux**),
`install_bedrock(version, target, preview=False, …) -> {"version", "binary"}` – entpackt, behält
Welt und Konfiguration und setzt `bedrock_server` auf 0750.

### Crossplay

`geyser_download(project="geyser", flavor="spigot") -> (url, dateiname, sha256)` – schlägt erst den
Build nach und lädt dann genau diesen (sonst passt die Prüfsumme nicht zum Build).
`hangar_download(project) -> (url, version, sha256)`.
`install_crossplay(target, …) -> dict` legt `Geyser-Spigot.jar`, `floodgate-spigot.jar`,
`ViaVersion.jar`, `ViaBackwards.jar` in `plugins/`.
`remove_crossplay(target)` entfernt genau diese vier (`CROSSPLAY_JARS`) wieder.

### Fehler

`SourceError(ValueError)` mit deutschem Satz – der Daemon kann ihn unverändert als
`{"error": …}` ausgeben. `DiskFull(SourceError)` für „Platte zu voll“ (ein zweiter Versuch hilft
dort nicht; passender HTTP-Status z. B. 507).

---

## 3. `core/runner.py`

### Wurzelordner

`instances_root()` (Standard `sources_linux.users_dir()`), `set_instances_root(path)`,
`check_instance_dir(directory) -> pathlib.Path`. Letzteres verlangt einen absoluten Pfad unter dem
Wurzelordner, ohne `..` und ohne Symlink nach draußen (alles wird aufgelöst), und lehnt den
Wurzelordner selbst ab.

### `@dataclass(frozen=True) class RunSpec`

| Feld | Standard | Bedeutung |
|---|---|---|
| `instance_id` | – | `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` |
| `directory` | – | Instanzordner (wird geprüft und aufgelöst) |
| `kind` | `"java"` | `"java"` oder `"bedrock"` |
| `name` | `"Minecraft Server"` | für Meldungen; Steuerzeichen raus, 48 Zeichen |
| `version` | `""` | Minecraft-Version (nur Information) |
| `ram_mb` | `4096` | 512 … 12288 (`RAM_MIN_MB`, `RAM_MAX_MB`) |
| `java` | `None` | Pfad zur `java`-Datei; sonst wird über `java_major` gesucht |
| `java_major` | `0` | für die Suche, wenn `java` fehlt |
| `java_flags` | `()` | wird durch `safe_java_flags` gefiltert |
| `jar` | `"server.jar"` | muss im Instanzordner liegen und auf `.jar` enden |
| `companion` | `True` | Paper mit Begleit-Plugin → Stopp per `mcsmstop` |
| `after_ready_commands` | `()` | Befehle, die nach `Done (…)` gehen (z. B. Gamerules ab 1.21.9) |
| `extra_env` | `{}` | zusätzliche Umgebungsvariablen (`[A-Z][A-Z0-9_]*`, einzeilig) |

`RunSpec.from_config(cfg, directory, **over)` baut die Angaben aus einer Konfiguration im Format des
lokalen Programms (`id`, `name`, `type`, `flavor`, `version`, `ram_mb`, `java_major`, `java_flags`);
`companion` wird daraus abgeleitet (nur `type=java` und `flavor=paper`).

Ungültige Angaben werfen sofort `ValueError` mit deutschem Satz – eine `RunSpec` ist also immer
startbar geprüft.

### Startbefehl und Umgebung

* `build_command(spec) -> list[str]` – Java: `java -Xms<hälfte>M -Xmx<ram>M` + vier
  `-D…encoding=UTF-8` + geprüfte Flags + `-jar <jar> nogui`. Bedrock: der absolute Pfad zu
  `bedrock_server`. Immer eine Argumentliste, nie eine Zeichenkette, nie eine Shell.
* `build_env(spec) -> dict[str, str]` – **der Server erbt nichts vom Daemon.** Gesetzt werden nur
  `PATH`, `HOME` (`<instanz>/.mcsm/home`), `TMPDIR` (`<instanz>/.mcsm/tmp`), `LANG`/`LC_ALL=C.UTF-8`,
  `TZ` und bei Bedrock `LD_LIBRARY_PATH` auf den Instanzordner.

### Verwaltungsordner `.mcsm`

Alles, was der Manager selbst im Instanzordner braucht, liegt in `MANAGE_DIR = ".mcsm"`
(`manage_dir(spec)`): die Prozesskennung `mcsm.pid` sowie `home/` und `tmp/` für den Server.
`.mcsm` steht in `core/paths.RESERVED_NAMES` – der Besitzer sieht den Ordner also nicht in seiner
Dateiliste, und bei einer Rückholung bleibt er auf dem Server. Diese Liste schützt aber nur die
HTTP-API, **nicht** den Serverprozess: Der Instanzordner hat setgid und `2770`, `UMask=0007` in der
Unit macht daraus für alles Neue `0770`/`0660` mit der Gruppe des Server-Benutzers – auch für
`.mcsm`. Ein hochgeladenes Plugin könnte damit `mcsm.pid` neu schreiben, `install.json`
austauschen (Quelle für `java_major`/`java_flags`) und die Merkdatei `.mcsm/gruppe` löschen, um
`isolation.fix_tree` bei jedem Start erneut auszulösen. Deshalb setzt `_prepare` bei **jedem**
Start `isolation.secure_manage(directory, spec.run_as)`:

* `.mcsm` → Gruppe des Server-Benutzers, Modus `0710` (`isolation.MANAGE_MODE`): nur durchlaufen,
  nichts anlegen oder löschen.
* `.mcsm/home` und `.mcsm/tmp` → `2770` (`isolation.WORK_MODE`): dort **muss** der Server
  schreiben dürfen (HOME und TMPDIR), sonst startet Java nicht.

Vererben genügt dafür nicht – das muss jeder Start richten. Auf dem Root-Server geprüft: nach
einem Paper-Lauf enthält der Instanzordner außer `.mcsm` nur die Dateien des Servers,
`paths.list_dir` zeigt `.mcsm` nicht und `paths.walk_files` nimmt es nicht in die Übertragung
(180 Dateien, keine aus `.mcsm`).
* `safe_java_flags(flags) -> list[str]` – nur `-XX:…`/`-D…` ohne Leerzeichen; verworfen werden
  `-XX:OnError`, `-XX:OnOutOfMemoryError`, `-XX:AbortVMOnException`, `-XX:ErrorFile`,
  `-XX:HeapDumpPath`, `-XX:LogFile`, Flight-Recorder-Schalter und alles andere aus `_FLAG_DENY`.
  Diese Schalter würden fremde Programme starten oder Dateien an beliebige Stellen schreiben –
  aus einer gespeicherten Konfiguration darf so etwas nie in die Kommandozeile kommen.
* `check_command(text) -> str` – genau eine Zeile, keine Steuerzeichen, höchstens 512 Zeichen.
  Damit lässt sich über einen Konsolenbefehl kein zweiter Befehl einschmuggeln (`"say hi\nop x"`).

### `class Runner(spec, *, on_exit=None, on_line=None)`

`on_exit(runner, code)` wird nach dem Prozessende aus dem Lese-Thread gerufen (der Daemon gibt dort
Ports frei, schreibt `running=False` und protokolliert). `on_line(runner, line)` sieht jede
Konsolenzeile (z. B. für ein Protokoll auf Platte). Beide Rückrufe dürfen nichts Langes tun;
Ausnahmen werden verschluckt, damit sie den Server nicht mitreißen.

| Mitglied | Bedeutung |
|---|---|
| `start()` | prüft (kein root, Ordner und Server-Datei vorhanden, keine Waise), leert den Puffer und startet in eigener Sitzung (`start_new_session=True` → `setsid`); schreibt `mcsm.pid` |
| `stop(timeout=60, announce_seconds=0)` | Ankündigung → `stop` → Warten → SIGTERM an die **Prozessgruppe** → SIGKILL. Ein zweiter Aufruf wartet nur mit |
| `kill()` | nur für Notfälle (SIGKILL, ohne Speichern) |
| `send(command)` | ein Befehl in die Konsole (`> befehl` landet im Puffer) |
| `announce(text) -> bool` | `say <text>` an alle Spieler |
| `console(since=0, tail=None) -> {"next": int, "lines": [str]}` | Ringpuffer mit fortlaufender Nummer, genau wie lokal |
| `log(text)` | eigene Meldung in den Puffer (`[Manager] …`) |
| `running`, `ready`, `stopping`, `pid`, `uptime`, `exit_code`, `last_error` | Zustand |
| `memory_mb() -> int` | RSS der ganzen Prozessgruppe aus `/proc` |
| `status() -> dict` | siehe unten |
| `spec` / `active_spec` | gewünschte bzw. gerade laufende Angaben |

Ringpuffer: `MAX_LOG_LINES = 4000` Zeilen, jede auf `MAX_LINE_CHARS = 2000` gekürzt, ANSI-Farbcodes
entfernt. `console(since)` liefert alles ab Nummer `since`; eine längst verworfene Nummer liefert
einfach den ältesten vorhandenen Rest.

Bereit-Erkennung: `Done (…` (Paper/Vanilla) bzw. `Server started.` (BDS). Danach gehen die
`after_ready_commands` raus.

Stopp im Einzelnen (`announce_seconds > 0`):

1. Mit Begleit-Plugin: `mcsmstop <sekunden>`; bestätigt das Plugin mit „Herunterfahren angefordert“,
   zählt es im Spiel herunter und stoppt selbst.
2. Ohne Plugin (oder ohne Bestätigung): `say Dieser Server wird in <n> Sekunden gestoppt …` und
   warten.
3. `stop` in die Konsole, bis zu `timeout` Sekunden Zeit zum Speichern der Welt.
4. `SIGTERM` an die Prozessgruppe (`TERM_WAIT = 15`), dann `SIGKILL` (`KILL_WAIT = 5`).

Konstanten für den Pass-Ablauf: `STOP_COUNTDOWN = 10`, `STOP_TIMEOUT = 60`.

**Kein root:** `start()` wirft, wenn `os.geteuid() == 0` („der Dienst muss als Benutzer mcsm
gestartet werden“).

**Eigener Unix-Benutzer je Konto:** `RunSpec.run_as` nennt den Benutzer, unter dem dieser Server
laufen soll (der Daemon füllt es aus `core/isolation.py`, siehe ARCHITEKTUR.md). Ist es gesetzt,
packt `_spawn` den Befehl in
`sudo -n -u <benutzer> -- /usr/bin/env -i <umgebung> /bin/sh -c 'umask 0002; exec "$0" "$@"' …`:

* `env -i` baut die Umgebung selbst – nichts hängt davon ab, was sudo durchlässt.
* `sudo` bleibt als Elternprozess (PAM-Sitzung) stehen; der Java-Prozess hängt in **derselben**
  Prozessgruppe, deshalb stimmen `mcsm.pid`, die Speichermessung über die Gruppe und `killpg`
  weiter. Signale gehen über `isolation.signal_group`, weil der Daemon einem fremden Benutzer
  selbst kein Signal schicken darf.
* Ohne `run_as` bleibt alles wie vorher (Entwicklungsrechner, Selbsttests).

### Waisen nach einem Daemon-Neustart

Server laufen in eigener Sitzung und überleben den Daemon – die Unit setzt dafür
`KillMode=process`. Bei `KillMode=mixed` hätte systemd nach einem Absturz des Daemons allen
restlichen Prozessen der cgroup SIGKILL geschickt, also den Java-Prozessen, ohne dass die Welt
gespeichert wurde. Deshalb:

* `orphan_pid(spec) -> int | None` – liest `.mcsm/mcsm.pid` und prüft der Reihe nach:

  1. **Besitzer**: `os.stat("/proc/<pid>").st_uid` gegen den Unix-Benutzer dieser Instanz
     (`spec.run_as`, sonst der eigene). `/proc/<pid>` darf jeder lesen.
  2. **Kommandozeile** (`/proc/<pid>/cmdline`, ebenfalls für jeden lesbar): sie muss die Instanz
     nennen. Dafür steht bei Java `-Dmcsm.instanz=<kennung>` in `build_command`
     (`runner.INSTANZ_FLAG`, `runner.instanz_kennzeichen(spec)`); bei Bedrock steht der
     Instanzpfad ohnehin im Argument.
  3. Hilfsweise der **Arbeitsordner** (`/proc/<pid>/cwd`).
  4. Hilfsweise die **Startzeit**: Feld 22 aus `/proc/<pid>/stat` plus `btime` aus `/proc/stat`
     gegen den Zeitstempel in `.mcsm/mcsm.pid` (`PID_START_TOLERANZ = 5` Sekunden). So werden
     auch Server erkannt, die eine ältere Fassung noch ohne Kennzeichen gestartet hat.

  **`cwd` darf nicht entscheiden.** `os.readlink("/proc/<pid>/cwd")` verlangt `PTRACE_MODE_READ`:
  Der Daemon läuft als `mcsm`, der Serverprozess als `mcsmsrvNN` – der Aufruf scheitert mit
  „Zugriff verweigert“. Würde das als „läuft nicht mehr“ gewertet, verlöre der Dienst nach jedem
  `systemctl restart mcsm` jeden laufenden Server: Der Java-Prozess liefe weiter, wäre nicht mehr
  stoppbar, zählte nicht mehr gegen `max_concurrent` und `ram_total_mb`, und ein „Start“ des
  Besitzers brächte eine **zweite** JVM auf dieselbe Welt (Weltkorruption). Auf dem Root-Server
  nachgemessen: `readlink /proc/<pid>/cwd` als anderer Benutzer → rc=1, `cat /proc/<pid>/cmdline`
  → geht.
* `stop_orphan(spec, timeout=15) -> bool` – SIGTERM an die Gruppe, dann SIGKILL. Paper speichert
  bei SIGTERM ordentlich (im Test: 1,6 s, Code 143).
* `start()` lehnt ab, solange eine Waise lebt.

### `status()`

```json
{"id": "a1b2c3d4", "running": true, "ready": true, "stopping": false, "pid": 46580,
 "uptime": 15, "ram_mb": 2048, "memory_mb": 1470, "exit_code": null, "error": "",
 "console_next": 65}
```

`ram_mb` ist die Zusage (`-Xmx`) des laufenden Servers und 0, wenn er aus ist; `memory_mb` ist der
gemessene Verbrauch. Anzeigetexte macht das Programm auf dem PC – hier stehen nur Daten.

### `class RunnerRegistry(*, on_exit=None, on_line=None)`

| Methode | Bedeutung |
|---|---|
| `get(spec) -> Runner` | holt oder legt an. Läuft der Server, bleibt `active_spec` unverändert – neue Angaben gelten ab dem nächsten Start |
| `find(id)`, `all()`, `statuses()` | Auskunft |
| `running_ids() -> list[str]` | laufende Instanzen |
| `running_ram_mb() -> int` | Summe der Zusagen aller laufenden Server – Grundlage für `ram_total_mb` des Passes |
| `drop(id, timeout=20)` | stoppen und vergessen (Instanz gelöscht) |
| `stop_all(timeout=60, announce_seconds=0)` | alle gleichzeitig anstoßen, gemeinsam abwarten – beim Beenden des Daemons |

---

## 4. Was der Daemon aufrufen muss

**Beim Start des Daemons**

```python
sources_linux.set_data_root("/srv/mcsm"); sources_linux.ensure_dirs()
pool = ports.PortPool()                       # probe=True
for satz in gespeicherte_ports: ...           # pool.restore(satz) – Meldungen ins Protokoll
registry = runner.RunnerRegistry(on_exit=nach_dem_ende)
for instanz in alle_instanzen():               # Waisen aus dem letzten Lauf einsammeln
    spec = runner.RunSpec.from_config(instanz, ordner_von(instanz))
    if runner.orphan_pid(spec) is not None:
        runner.stop_orphan(spec)               # oder dem Admin melden
```

**Instanz einrichten** (im Job-Thread, `progress`/`status` in das Job-Objekt)

```python
java = sources_linux.ensure_java(sources_linux.java_major_for(version))      # nur Java
info = sources_linux.install_paper(version, ordner, eula_accepted=True, …)   # oder
sources_linux.install_bedrock(version, ordner, …)
if geyser: sources_linux.install_crossplay(ordner, …)
else:      sources_linux.remove_crossplay(ordner)
```

**Starten** (nach `instances.check_start`, also nach der Pass-Prüfung)

```python
leases = pool.allocate_for(instanz_id, art, geyser=geyser, max_players=max_players)
zuteilung = pool.assignment(instanz_id)        # in server.properties schreiben, dann speichern
spec = runner.RunSpec(instance_id=…, directory=…, kind=…, ram_mb=…, java=java,
                      java_flags=tuple(info["java_flags"]), companion=…,
                      after_ready_commands=gamerules)
registry.get(spec).start()
```

`server.properties` schreibt der Daemon (Gruppe „konten“/Daemon): Java `server-port=port`,
Bedrock zusätzlich `transport=nethernet` und `server-udp-ports=<udp_first>-<udp_last>`,
Geyser `bedrock.port=bedrock_port`. Die Gamerules für `after_ready_commands` ergeben sich aus
`sources_linux.command_block_gamerule(version)`.

**Steuern**

| Route | Aufruf |
|---|---|
| `POST /api/servers/<id>/stop` | `registry.find(id).stop(announce_seconds=10)` (im Thread) |
| `POST /api/servers/<id>/command` | `registry.find(id).send(text)` – `ValueError` → 400 |
| `GET /api/servers/<id>/console?since=n` | `registry.find(id).console(n)` unverändert als JSON |
| `GET /api/me` | `registry.running_ram_mb()`, `pool.free_ports(…)`, `sources_linux.free_disk_mb()` |

**Ablauf oder Widerruf eines Passes**

```python
runner_.announce("Dein Pass läuft in 10 Minuten ab – der Server wird dann gestoppt.")
...                                            # 10 Minuten später
runner_.stop(timeout=90, announce_seconds=runner.STOP_COUNTDOWN)
```

**`mcsmstop` ist keine Ankündigung.** Der Befehl des Begleit-Plugins ist eine
Abschaltanforderung: Ist gerade **niemand** auf dem Server, fährt `ShutdownService.start()` ihn
sofort herunter, statt den Countdown zu laufen. Für die 10-Minuten-Vorwarnung darf er deshalb nur
benutzt werden, wenn das Plugin frische Spielerzahlen meldet und wirklich jemand da ist
(`mcsmd.companion_spieler`, `status.json` höchstens `COMPANION_FRISCH = 120` Sekunden alt);
sonst bleibt es bei `say`. Gestoppt wird in jedem Fall erst zum Ablauf – dort kündigt
`Runner.stop` den Stopp über `_announce_stop` noch einmal im Spiel an.

**Nach dem Ende** (im `on_exit`-Rückruf): `pool.release(instanz_id)`, Ports speichern,
`running=False` setzen. Beim Löschen einer Instanz `registry.drop(id)` und `pool.release(id)`.

**Beim Beenden des Daemons:** `registry.stop_all(timeout=60)` – sonst bleiben Server als Waisen
zurück (sie hängen absichtlich an keinem Elternprozess).

**Firewall/systemd:** `ports.firewall_ranges()` nennt die zu öffnenden Bereiche
(TCP 25565-25700, UDP 19132-19300). Der Dienst muss als `User=mcsm` laufen.

---

## 5. Annahmen über andere Gruppen

* Der Instanzordner ist `/srv/mcsm/users/<konto>/<instanz>/` und gehört `mcsm`. Er wird vom Daemon
  (bzw. der Gruppe „transfer“) angelegt, bevor `start()` gerufen wird.
* Der Datensatz einer Instanz liefert `id`, `name`, `type`, `flavor`, `version`, `ram_mb` und –
  für Paper – `java_major`/`java_flags` aus der Installation. `RunSpec.from_config` liest genau das.
* Die Pass-Prüfung („darf das jetzt starten?“) macht `instances.check_start`; der Runner prüft sie
  nicht nachträglich. `registry.running_ram_mb()` steht als Gegenprobe bereit.
* `core/paths.py` prüft alle Pfade der Datei-API. `runner.check_instance_dir` ist davon unabhängig
  und prüft nur den Instanzordner selbst.
* `".mcsm"` bleibt in `core/paths.RESERVED_NAMES`. Fällt der Name dort weg, sähe der Besitzer
  `mcsm.pid` und die Arbeitsordner des Servers in seiner Dateiliste und würde sie mit zurückholen.

---

## 6. Offene Punkte

1. **Portmodell abstimmen (wichtig).** `core/instances.py` hat eigene Portlogik:
   `PORT_RANGES = {"java": (25565, 25664), "bedrock": (19132, 19231)}`, `ports_in_use()`,
   `free_ports()`, `clean_ports()`, und speichert `{"java": …, "bedrock": …}` im Datensatz.
   Zwei Unterschiede zu dieser Gruppe:
   * Die Bereiche sind kleiner als in `ARCHITEKTUR.md` (25565-25700 / 19132-19300). Unkritisch,
     aber `clean_ports` würde einen vom Pool vergebenen Port über 25664 bzw. 19231 ablehnen.
   * `clean_ports` verbietet einem **Bedrock**-Server einen Java-Port. BDS 1.26.51+ braucht mit
     NetherNet aber beides: TCP für den Verbindungsaufbau und einen UDP-Block für die Spieldaten
     (so steht es auch im lokalen `manager.port_list`, und der Rauchtest lief genau so:
     `server-port=25565`, `server-udp-ports=19132-19147`). Mit nur einem UDP-Port kommt kein
     Spieler auf einen gehosteten Bedrock-Server.

   **Vorschlag:** `ports.PortPool` ist die vergebende Stelle (sie kennt Blöcke und prüft mit einem
   Bind-Versuch), der Daemon spiegelt das Ergebnis über `instances.set_ports` in den Datensatz und
   speichert `pool.snapshot()` daneben. Dazu in `instances.py` zwei kleine Änderungen:
   `PORT_RANGES` auf die Bereiche aus `ARCHITEKTUR.md` erweitern und den Satz „Ein Bedrock-Server
   hat keinen Java-Port“ streichen. Der Block lässt sich aus dem gespeicherten Bedrock-Port
   ableiten (`udp_first = ports["bedrock"]`, `udp_last = udp_first + udp_block_size(max_players) - 1`),
   dann braucht der Datensatz keine neuen Schlüssel.
2. **Modpacks** (Fabric/Quilt/NeoForge/Forge) sind hier nicht eingebaut – `flavor` außer `paper`
   startet zwar als Java-Server mit `server.jar`, aber Loader-Installation und die Argument-Dateien
   von NeoForge/Forge fehlen. Das lokale `core/modpacks.py` müsste dafür nach Linux übertragen
   werden (eigene Aufgabe; Vorschlag: gehostet zunächst nur Paper und Bedrock anbieten).
3. **Begleit-Plugin.** `companion=True` schickt beim Stoppen `mcsmstop`. Wer die
   `MCSMCompanion.jar` vor dem Start nach `plugins/` legt, ist noch offen – lokal macht das
   `core/companion.prepare`. Ohne Plugin funktioniert alles, die Ankündigung läuft dann über `say`.
4. **Konsole über einen Daemon-Neustart hinweg** ist nicht erhalten (der Ringpuffer lebt im
   Arbeitsspeicher). Paper schreibt `logs/latest.log`, BDS nicht. Falls das gewünscht ist, wäre
   `on_line` die Stelle, um mitzuschreiben.
5. **Speicherwert.** `memory_mb()` liest RSS aus `/proc` – bei Java ist das inklusive vorgetouchter
   Heap-Seiten (`-XX:+AlwaysPreTouch` steht in den empfohlenen Flags von Paper), der Wert liegt
   also von Anfang an nahe bei `-Xmx`. Für die Anzeige „belegt/zugesagt“ reicht das; für eine
   feinere Auswertung müssten `smaps_rollup`-Werte her.
6. **`-Xms` ist die Hälfte von `-Xmx`** (wie lokal). Bei knapp bemessenen Pässen könnte man auf
   `-Xms = -Xmx` wechseln, damit der Verbrauch von Anfang an ehrlich ist.
