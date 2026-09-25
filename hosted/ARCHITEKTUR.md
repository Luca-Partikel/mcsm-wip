# Hosted-Modus – Aufbau und Verabredungen

Der Minecraft Server Manager bekommt eine zweite Betriebsart: Neben den Servern auf dem eigenen PC
(„lokal") können Server auf einem Root-Server laufen („gehostet"). Gesteuert wird **alles weiterhin
aus dem Programm auf dem PC** – der Root-Server hat keine eigene Spieleroberfläche, nur eine API.

* Root-Server: Debian 12, 8 Kerne, 13 GB RAM, 30 GB Platte, feste IPv4 `45.132.89.224`
* Dienstbenutzer `mcsm`, Programm unter `/opt/mcsm`, Daten unter `/srv/mcsm`
* Python 3.11 (Standardbibliothek, keine Fremdpakete – wie im lokalen Programm)

## Ordner auf dem Root-Server

```
/opt/mcsm/                     Programmdateien (aus dem Ordner hosted/ dieses Repos)
/srv/mcsm/data/                users.json, invites.json, passes.json, instances.json, sessions.json
/srv/mcsm/users/<user-id>/<instanz-id>/    je Serverinstanz ein Ordner (Welt, Plugins, Logs …)
/srv/mcsm/runtime/             Java-Laufzeiten (Adoptium, tar.gz)
/srv/mcsm/cache/               Downloads (Paper, BDS, Geyser …)
/srv/mcsm/transfer/            Zwischenablage für Übertragungen
/var/log/mcsm/                 Protokolle
```

## Pässe

Ein Pass wird vom Admin (Betreiber) ausgestellt und gehört genau einem Benutzer.

| Feld | Bedeutung |
|---|---|
| `kind` | `local` (Pass für eigene Server, die vom PC hochgeladen werden) oder `premium` (reine Root-Server) |
| `days` | Laufzeit in Tagen (Tages-, Wochen-, Monatspass = 1, 7, 30 – beliebige Zahl erlaubt) |
| `max_concurrent` | Wie viele Server **gleichzeitig eingeschaltet** sein dürfen |
| `ram_total_mb` | Summe des Arbeitsspeichers über alle **laufenden** Server (z. B. 8192 → zwei Server mit je 4 GB) |
| `issued_at`, `expires_at` | Zeitpunkte (Unix-Sekunden) |
| `revoked_at` | Gesetzt, wenn der Admin den Pass vorzeitig zurückzieht |

Regeln:

* **Erstellen** von Serverinstanzen ist unbegrenzt. Begrenzt ist nur das **Einschalten**.
* Ein Start wird abgelehnt, wenn `laufende Server >= max_concurrent` oder
  `Summe RAM der laufenden Server + RAM des neuen Servers > ram_total_mb`.
  Die Ablehnung nennt den Grund in verständlichem Deutsch und sagt, was gerade läuft.
* Hat ein Benutzer mehrere gültige Pässe, gilt jeweils der **größte** Wert (Summe wäre zu großzügig
  und würde die Maschine überbuchen).
* Premium-Pässe erlauben zusätzlich Instanzen, die **dauerhaft auf dem Root bleiben** und nie vom PC
  hochgeladen wurden.

## Ablauf oder Widerruf eines Passes

1. 10 Minuten vorher: Warnung an alle Spieler auf den betroffenen Servern, dazu eine Meldung im
   Programm. Der Konsolenbefehl `mcsmstop` des Begleit-Plugins (Titel + Chat + Countdown) wird
   dafür **nur** benutzt, wenn das Plugin gerade Spieler meldet: `mcsmstop` ist eine
   Abschaltanforderung, und auf einem **leeren** Server fährt das Plugin sofort herunter – als
   Vorwarnung wären das zehn Minuten zu früh. Sonst bleibt es bei `say`.
2. Bei Ablauf: Server werden sauber gestoppt (`mcsmstop 10` bzw. `stop`), Welt gespeichert.
   Ein **Widerruf** bekommt dieselbe Frist: erst die Ansage, zehn Minuten später der Stopp
   (gerechnet aus `revoked_at`, höchstens bis `expires_at`).
3. Instanzen aus **lokalen** Pässen werden auf `awaiting_pull` gesetzt: Sie starten nicht mehr.
4. Beim nächsten Start des Programms (oder sofort, wenn es läuft) wird der Besitzer aufgefordert,
   den Server **zurückzuholen**. Erst wenn alle Dateien lokal angekommen und geprüft sind
   (Größe + SHA256 je Datei), meldet das Programm `release` – **dann erst** löscht sich der
   Ordner auf dem Root-Server. Liegt im Ordner eine Datei, die gar nicht ins Manifest passt
   (Name mit Rückstrich, von Windows belegter Name, Leerzeichen am Rand, zu lang, Symlink),
   verweigert der Daemon die Freigabe mit einer Aufzählung dieser Dateien – sie würden sonst
   ungesehen mitgelöscht. Mit `force=1` lässt sich das ausdrücklich übergehen.
5. Instanzen aus **Premium**-Pässen bleiben liegen (sie haben keine lokale Heimat), werden aber
   gestoppt und als `suspended` geführt. Nach `premium_keep_days` (Standard 14) ohne gültigen Pass
   darf der Admin sie löschen; das Programm warnt vorher deutlich.

## Zustände einer Instanz

`local_only` → `uploading` → `hosted` (läuft/gestoppt auf dem Root) → `awaiting_pull` →
`downloading` → `local_only` (Root-Ordner gelöscht). Premium: `hosted` ↔ `suspended`.

Ein Server ist **immer nur an einer Stelle spielbar**. Solange er `hosted` ist, sperrt das lokale
Programm den Start der lokalen Kopie und weist darauf hin.

## API (alles JSON, Token im Kopf `Authorization: Bearer <token>`)

| Route | Zweck |
|---|---|
| `POST /api/auth/invite` | Einladungscode einlösen, Konto anlegen |
| `GET /api/auth/discord/start` | Anmeldung über Discord beginnen (liefert URL + Zustand) |
| `GET /api/auth/discord/callback` | Rückleitung von Discord, liefert Sitzungstoken |
| `GET /api/auth/discord/poll` | Das Programm auf dem PC holt das Token ab (Zustand + Geheimnis) |
| `POST /api/auth/discord/register` | Merkzettel + Einladungscode → Konto und Sitzung (zweiter Schritt, wenn Discord geklappt hat, es das Konto hier aber noch nicht gibt) |
| `POST /api/auth/logout` | Sitzung beenden |
| `GET /api/me` | Konto, Pässe, Grenzen, laufende Server, freier Arbeitsspeicher |
| `GET /api/servers` | Eigene Instanzen mit Zustand |
| `POST /api/servers` | Instanz anlegen (Premium) bzw. für eine Übertragung vorbereiten |
| `POST /api/servers/<id>/start` \| `/stop` \| `/command` | Steuern |
| `GET /api/servers/<id>/console` | Konsolenausgabe ab Zeile n |
| `GET/POST /api/servers/<id>/files` | Dateien auflisten, lesen, schreiben, löschen |
| `POST /api/servers/<id>/upload` | Datei hochladen (stückweise, SHA256) |
| `GET /api/servers/<id>/download` | Datei oder Ordner herunterladen |
| `POST /api/servers/<id>/release` | Root-Ordner löschen (nach geprüfter Rückholung) |
| `GET /api/admin/*`, `POST /api/admin/*` | Benutzer, Einladungen, Pässe, alle Instanzen |

Fehler immer als `{"error": "deutscher Satz"}` mit passendem HTTP-Status.

`/api/admin/*` antwortet **nur** unter `admin.<domain>` (und aus der Rückschleife). Unter
`api.<domain>` gibt es dort 404 – sonst nähme eine spätere Absicherung in nginx („Verwaltung nur
aus dem Büro-Netz“) keine Wirkung, weil derselbe Weg über den API-Namen offenstünde.

**Erstes Betreiberkonto:** Es entsteht ausschließlich mit dem Einladungscode aus
`ERSTER-ADMIN.txt` – auf dem Weg über den Einladungscode **und** auf dem Weg über Discord. Ohne
diese Prüfung bekäme auf einer frischen Anlage schlicht der Schnellste das Betreiberkonto, denn
die Discord-Wege sind über nginx öffentlich erreichbar.

**Anmeldung im Browser:** `GET /auth/discord/start` setzt für den Weg `admin` ein kurzlebiges
Cookie (`HttpOnly`, `Secure`, `SameSite=Lax`); die Rückleitung wird nur angenommen, wenn es dazu
passt. Sonst könnte ein Angreifer die Anmeldung mit **seinem** Discord-Konto beginnen und dem
Betreiber die Rückleitungsadresse schicken – der arbeitete danach unbemerkt in dessen Konto.

## Trennung der Konten auf dem Betriebssystem

Ein Kunde darf Plugins und ganze Serverdateien hochladen – ein Plugin ist damit beliebiger
Java-Code. Deshalb läuft **jedes Konto unter einem eigenen Unix-Benutzer**:

* `deploy.sh` legt als root einen Vorrat `mcsmsrv01 … mcsmsrv32` an (je mit gleichnamiger Gruppe),
  nimmt `mcsm` in alle diese Gruppen auf und schreibt `/etc/sudoers.d/mcsm`. Erlaubt ist dort
  ausschließlich das Starten von Befehlen als genau diese unprivilegierten Benutzer – **nie root**.
* Jedes Konto bekommt beim ersten Start fest einen dieser Benutzer (gemerkt als `server_user`
  in `users.json`, siehe `core/isolation.py`).
* Rechte: `/srv/mcsm` und `/srv/mcsm/users` sind `0711` (durchlaufen, nicht auflisten),
  `/srv/mcsm/data` bleibt `0700` für `mcsm`, der Kontoordner ist `2710` und der Instanzordner
  `2770` mit der Gruppe des Server-Benutzers. Ein fremder Server-Benutzer kommt damit nicht in
  den Ordner, und kein Serverprozess kommt an `users.json`, `invites.json`, `passes.json`,
  `daemon.json` oder `discord_secret`.
* Der Verwaltungsordner `.mcsm` der Instanz gehört dem Dienst: er wird bei **jedem** Start
  ausdrücklich auf `0710` gesetzt (Gruppe des Server-Benutzers, aber nur „durchlaufen“). Sonst
  erbt er vom Instanzordner (setgid, `2770`) die Schreibrechte der Gruppe, und ein hochgeladenes
  Plugin könnte `mcsm.pid` neu schreiben, `install.json` austauschen und die Merkdatei von
  `fix_tree` löschen. Nur `.mcsm/home` und `.mcsm/tmp` (HOME und TMPDIR des Servers) sind `2770`.
* `isolation.fix_tree` läuft über **Dateideskriptoren** (`O_NOFOLLOW`, `fchown`/`fchmod`), nicht
  über Pfade: zwischen „ist das ein Symlink?“ und `chown` darf der Serverbenutzer sonst eine
  Verknüpfung nach `/srv/mcsm/data` dazwischenschieben. Dasselbe gilt für Lesen, Schreiben und
  Übertragen einzelner Dateien (`core/paths.py`: `ordner_griff`, `lies_stueck`, `schreibe_atomar`).
* Der Serverprozess läuft mit `umask 0002`, damit der Daemon seine Dateien weiter bearbeiten kann
  (`server.properties` schreibt der Server selbst neu). Was Minecraft über eine Zwischendatei
  ablegt (`level.dat`), wird `0600` – der Daemon kann das nicht selbst ändern und lässt es den
  Server-Benutzer nachziehen (`isolation.grant_group`, u. a. nach jedem Stopp und vor jedem
  Manifest). Ohne das wäre die Rückholung auf den PC unvollständig.
* `mcsm.service` kann deshalb **keine** seccomp-gestützte Härtung setzen: systemd erzwingt dann
  „no new privileges“, und `sudo` funktioniert nicht mehr. Die Datei erklärt das an der Stelle.
* Fehlt der Vorrat (Entwicklungsrechner, altes Deployment), läuft alles wie früher unter `mcsm` –
  der Daemon schreibt dann beim Start eine deutliche Warnung. `MCSM_REQUIRE_ISOLATION=1` lehnt in
  diesem Fall jeden Start ab.

## Ports

Vergeben werden sie aus `core/ports.py` (`PortPool`, TCP 25565–25700, UDP 19132–19300) und danach
in den Instanz-Datensatz gespiegelt. `instances.PORT_RANGES` leitet sich aus `ports.POOLS` ab – es
gibt nur eine Quelle für die Bereiche.

Der **TCP-Port** kommt, sobald eine Instanz auf dem Root liegt (`hosted`) oder gerade hochgeladen
wird (`uploading`) – nicht erst beim ersten Start (`mcsmd.ensure_port`, `mcsmd.PORT_ZUSTAENDE`).
Sonst überspringt `routes.tabelle()` sie, und der Verteiler meldet den Spielern „Diesen Server
gibt es hier nicht“, obwohl der Server längst da ist. Genau das war der Befund zu „Eutopia“.
Er bleibt der Instanz danach erhalten – auch über einen Stopp und den Ruhezustand hinweg, sonst
verschwände ein schlafender Server wieder aus der Tabelle. Zurück geht er erst, wenn die Instanz
den Root verlässt (zurückgeholt, geparkt, gelöscht).

Eine Instanz im Zustand `local_only` bekommt **keinen** Port: sie liegt nur auf dem PC, steht
nicht in `routes.json`, und der Port täte dort nichts. `MAX_INSTANCES_PER_USER` erlaubt 500
angelegte Server je Konto, der Java-Bereich hat aber nur rund 136 Ports für **alle** Konten – ein
einziges Konto könnte den Bereich mit Servern leerräumen, die es niemals hochlädt.

Der **UDP-Block** (Bedrock/Geyser) wird nur im Betrieb gebraucht und beim Stopp freigegeben.

## Ruhezustand und Wecken

Ein gehosteter Java-Server steht in `routes.json`, **auch wenn er aus ist** – mit `wecken: true`,
wenn der Besitzer den Ruhezustand eingeschaltet hat. Daraus ergeben sich drei Wege:

* **Ping (Handshake mit Zustand 1).** Der Verteiler kommt auf `127.0.0.1:<port>` nicht durch und
  antwortet selbst wie ein echter Server: Anzeigename, `0/<max>` (aus `server.properties`) und die
  Beschreibung „Server ist ausgeschaltet – tritt bei, um ihn zu starten“, **ohne** Fehlerfarbe. In
  der Serverliste sieht er damit normal aus.
* **Beitritt (Zustand 2 oder 3).** Der Verteiler ruft `POST /api/router/wake` über die
  Rückschleife auf, mit dem gemeinsamen Geheimnis aus `/srv/mcsm/data/router_secret` im Kopf
  `X-MCSM-Router`. Der Dienst prüft Pass, Kontingent und Platz (`instances.check_start`) und
  startet. Der Spieler wird getrennt mit „Der Server startet gerade. Bitte verbinde dich in etwa
  einer Minute noch einmal.“ – oder, wenn er nicht starten darf, mit dem Grund im Klartext.
  Mehrere Beitritte in derselben Sekunde starten **einen** Server: `mcsmd.WAKE_LOCKS` hält je
  Instanz eine Sperre, und wer sie nicht bekommt, hört „startet gerade“ – was stimmt.
* **Leerstand.** `mcsmd.ruhezustand_pruefen` läuft im Takt der Überwachungsschleife (60 s) und
  legt einen Server schlafen, der `hibernation_minutes` (Vorgabe 15) ohne Spieler steht: erst
  `save-all`, dann der gewöhnliche angekündigte Stopp. Die Spielerzahl kommt aus `status.json` des
  Begleit-Plugins, ersatzweise über den Konsolenbefehl `list`; ist sie **unbekannt**, wird nicht
  schlafen gelegt. Ebenso nicht, solange eine Übertragung für die Instanz offen ist oder der
  Server erst seit weniger als `HIBERNATION_GRACE` (5 Minuten) läuft – ein frisch geweckter
  Server soll nicht einschlafen, bevor der Spieler, der ihn geweckt hat, überhaupt drin ist.

Ein schlafender Server zählt **nicht** als laufend (`instances.is_running` verlangt `running`):
Platz und Arbeitsspeicher sind im Pass wieder frei. Beim Wecken wird das Kontingent erneut
geprüft; reicht es nicht, sagt die Trennmeldung das. `server_view` führt „schläft“ (`sleeping`)
als eigenen Zustand neben „läuft“ und „gestoppt“; ein- und ausschalten lässt sich das je Server
über `POST /api/servers/<id>/settings` mit `hibernation` und `hibernation_minutes`.

## Xbox-Freunde-Modus (Konsolen)

Konsolenspieler (Xbox, PlayStation, Switch) können keine Adresse eintippen. Sie kommen über
**Freunde → Beitreten** herein: Ein Bot-Konto meldet sich bei Xbox Live an und zeigt den Server
allen seinen Freunden als beitretbare Welt. Dazu läuft je Instanz ein eigener Prozess
(`MCXboxBroadcast Standalone`, `hosted/core/xbox.py` – die Linux-Fassung des Abschnitts „Xbox“
aus `core/manager.py` des PC-Programms).

* **Beworben wird die öffentliche Adresse des Root-Servers** (`arcardia-nexus.de`, übersteuerbar
  mit `MCSM_XBOX_ADDRESS`) und der **Bedrock-Port der Instanz**: bei Java der Geyser-Port, bei BDS
  der eingetippte Port. **Nie `127.0.0.1`** – die Konsole des Freundes baut die Verbindung selbst
  dorthin auf; `xbox.LOOPBACK` lehnt solche Werte ab. Genau das war der Fehler nach dem Umzug: in
  der mitgezogenen `config.yml` stand noch die Heimadresse des Betreibers.
* Der Bot läuft **unter demselben Unix-Benutzer wie der Server** (`core/isolation.py`), in eigener
  Sitzung, und trägt `-Dmcsm.xbox=<kennung>` in der Kommandozeile – daran findet der Dienst einen
  vergessenen Bot in `/proc` wieder (wie `-Dmcsm.instanz=` beim Server).
* Die Jar liegt **einmal** in `/srv/mcsm/cache` (mit Prüfsumme). Damit der Serverbenutzer sie
  lesen kann, wird sie `0644` und der Zwischenspeicher bekommt nur „durchlaufen“ für andere
  (0711, dasselbe Muster wie `/srv/mcsm`). Eine Kopie je Instanz wären 40 MB – die Platte ist
  hier der engere Engpass.
* **Die Anmeldung liegt in `<instanz>/xbox/cache/cache.json`** und wird bei einer Übertragung
  **mitgenommen**. Stolperstein: `cache` steht in `paths.NACHLADBAR_DIRS`, das gilt aber nur für
  den **ersten** Pfadteil, und der ist hier `xbox`. `transfer.xbox_anmeldung_dabei()` hält die
  Zusage fest, ein Selbsttest prüft sie auf beiden Seiten. Ohne sie müsste sich der Betreiber
  nach jedem Umzug neu bei Xbox Live anmelden.
* Der Bot **startet und stoppt mit dem Server** (`xbox_enabled` und `xbox_autostart` im
  Instanz-Datensatz) und geht beim Ruhezustand mit – der Serverprozess endet dort auf demselben
  Weg. Beim Beenden des Dienstes werden die Bots **wirklich gestoppt** (anders als die Server):
  Sie halten keine Welt, und zwei Bots kündigten Xbox Live dieselbe Sitzung doppelt an. Der
  nächste Start fährt sie in `xbox_wieder_aufnehmen()` in Sekunden wieder hoch.
* Angemeldet wird per **Gerätecode**: Der Dienst zieht die Zeile mit `microsoft.com/link` und dem
  Code aus der Ausgabe und bietet sie im Status an (`state` = `off` | `starting` | `login` |
  `online`, dazu `state_text` in deutschem Klartext). Einlösen kann den Code nur der Betreiber.

Routen (gleiche Namen und Felder wie in `app.py` des PC-Programms):
`GET /api/servers/<id>/xbox` und `…/xbox/status`, `POST …/xbox/setup` (Vorgang: Java, Jar,
Konfiguration, Bot), `POST …/xbox/start` | `/stop` | `/reset` | `/disable`,
`GET …/xbox/console`. Die beworbene Adresse setzt **der Dienst**, nicht das Konto – sonst könnte
ein Kunde Xbox Live eine fremde Adresse als „seinen“ Server ankündigen.

## Crossplay: Chat und Serverbild

Zwei Kleinigkeiten, die vor **jedem** Start nachgezogen werden (`mcsmd.ensure_crossplay_chat`,
`mcsmd.ensure_server_icon`), weil der Server `server.properties` selbst neu schreibt:

* Mit Geyser steht `enforce-secure-profile` auf **false**. Bedrock-Spieler kommen über Floodgate
  herein und haben keine Mojang-Chatsignatur; bleibt die Einstellung auf `true`, verwirft der
  Server ihre Chatnachrichten – sie können spielen, aber nichts schreiben, und niemand sieht,
  woran es liegt.
* Ohne eigenes Bild bekommt der Server `assets/server-icon.png` als `server-icon.png`. Ein
  vorhandenes Bild des Besitzers wird **nie** überschrieben.

## Grenzen der Maschine (wichtig für die Pässe)

13 GB RAM und 30 GB Platte. Realistisch: **höchstens 10 GB** gleichzeitig vergeben (Rest für System
und Java-Verwaltung), und die Platte ist der engere Engpass – eine gut erkundete Welt wiegt schnell
2–5 GB, dazu Sicherungen und Übertragungen. Der Daemon lehnt einen Start ab, wenn weniger als
**3 GB Platte** frei wären, und meldet dem Admin, wenn die Platte unter 15 % fällt.
