# Gruppe „admin“ – Betreiberoberfläche für admin.arcardia-nexus.de

Dateien (neu, gehören allein zu dieser Gruppe):

| Datei | Inhalt |
|---|---|
| `hosted/web/admin.html` | Gerüst der Seite: Anmeldung (zwei Schritte), Kopfbereich, sechs Reiter, feste Formulare |
| `hosted/web/admin.css` | Farben, Karten, Knöpfe, Hinweiskästen – übernommen aus `web/style.css` |
| `hosted/web/admin.js` | Gesamte Logik: Anmeldung, Abruf, Zeichnen, Rückfragen, Beispielansicht |

Eine einzelne Seite, kein Framework, kein Bauschritt, keine Abhängigkeit von außen – genau wie
`web/app.js` im Programm auf dem PC. Geprüft mit `node --check admin.js` (in Ordnung) und im
Browser mit der eingebauten Beispielantwort.

Es wurde **keine** Datei der parallel laufenden Gruppen angefasst (`mcsmd.py`, `core/*.py`).

---

## 1. Was der Daemon noch verdrahten muss

### 1.1 Die Seite ausliefern (**nötig, fehlt noch**)

`mcsmd.py` kennt zurzeit nur `/api/...`; es gibt keine Auslieferung von Dateien. nginx leitet
`admin.arcardia-nexus.de` **und** `api.arcardia-nexus.de` an `127.0.0.1:8765` weiter, also muss der
Daemon die drei Dateien selbst ausliefern:

* `GET /` und `GET /admin.html` → `admin.html`, dazu `/admin.css` und `/admin.js`
* Quelle: `/opt/mcsm/web/` (der Ordner `hosted/web/` dieses Repos; `deploy.sh` muss ihn mitnehmen)
* Nur diese drei Namen bedienen, keine Pfade zusammensetzen, kein Verzeichnis durchreichen.
* Kopfzeilen: `Content-Type` passend (`text/html; charset=utf-8`, `text/css`, `application/javascript`),
  `Cache-Control: no-store` (die Oberfläche ändert sich oft) und `X-Content-Type-Options: nosniff`.
* Die Auslieferung nur für den Host `admin.arcardia-nexus.de` erlauben, damit die Seite nicht
  zusätzlich unter `api.` auftaucht (`_web_erlaubt`).
* Umgekehrt genauso: Pfade unter `/api/admin/` antworten **nur** unter `admin.<domain>` (und aus
  der Rückschleife), sonst 404 (`_admin_host_ok` in `_dispatch`). Ohne das trennen die beiden
  Namen nur drei Oberflächendateien: nginx bedient sie aus **einem** Block, und eine spätere
  Absicherung „Verwaltung nur aus dem Büro-Netz“ bliebe wirkungslos, weil derselbe Weg über den
  API-Namen offenstünde.

Alternative, falls das lieber nicht in den Daemon soll: nginx liefert `/srv/mcsm/web/` selbst aus und
reicht nur `/api/` weiter. Dann muss `deploy.sh` die Dateien dorthin legen. Die Seite ruft die API
**immer relativ** auf (`/api/...`), damit sie unter beiden Wegen ohne CORS auskommt.

### 1.2 Anmeldung: nur über Discord (im Daemon fertig)

Auf der Anmeldeseite steht **genau ein** Knopf: „Mit Discord anmelden“. Ein Feld für den
Einladungscode gibt es dort nicht mehr – es wurde nur in dem einen Fall gebraucht, in dem der
Server das Discord-Konto noch nicht kennt, und genau dafür gibt es jetzt einen zweiten Schritt.

```
GET /api/auth/discord/start?ziel=<absolute URL dieser Seite>
→ 200 {"url": "https://discord.com/oauth2/authorize?…", "state": "<zufällig>"}
```

* Es wird nur eine `url` akzeptiert, die mit `https://discord.com/` oder `https://discordapp.com/`
  beginnt – sonst zeigt die Seite einen Hinweiskasten mit dem Satz des Servers.
* `ziel` ist ein **Wunsch**, kein Befehl: der Daemon muss den Wert gegen eine eigene weiße Liste
  prüfen (`https://admin.arcardia-nexus.de/admin.html`) und sonst seinen Standard nehmen.
* `state` wird beim Daemon hinterlegt und von ihm geprüft; die Seite legt ihn nur nachrichtlich in
  `sessionStorage` ab.

Die Rückleitung hängt ihr Ergebnis an die **Raute** – sie geht nie an einen Server:

```
…/admin.html#token=<sitzungstoken>                     fertig angemeldet
…/admin.html#registrierung=<zettel>&name=<anzeigename> Konto fehlt noch → Schritt 2
…/admin.html#verknuepft=1                              Discord an ein bestehendes Konto gehängt
…/admin.html#fehler=<deutscher Satz, URL-kodiert>      etwas ist schiefgegangen
```

Die Seite liest die Raute beim Start, entfernt sie sofort aus der Adresszeile und legt ein Token in
`localStorage` (`mcsm_admin_token`) ab. Akzeptiert werden für das Token nur Zeichen
`A–Z a–z 0–9 . _ -`, Länge 16–512; für den Merkzettel zusätzlich `~`, Länge 16–256.

**Schritt 2 – Einladungscode nachtragen.** Kommt `#registrierung=`, zeigt die Seite eine eigene
Karte: „Hallo &lt;Name&gt; – fast geschafft“, ein Feld für den Code (Striche setzt die Seite selbst,
Format `ABCD-EFGH-IJKL`) und den Knopf „Konto einrichten“:

```
POST /api/auth/discord/register  {"zettel": "<zettel>", "code": "ABCD-EFGH-IJKL",
                                  "note": "Verwaltung im Browser"}
→ 200 {"token": "…", "expires_at": …, "user": {…}}       wie bei der Anmeldung
→ 4xx {"error": "deutscher Satz"}
```

* Ein Tippfehler im Code bleibt in Schritt 2 stehen (Hinweiskasten, Feld behält den Fokus).
* Ist der **Merkzettel** abgelaufen oder verbraucht (403/410 oder „abgelaufen“ im Satz), führt die
  Seite zurück auf „Mit Discord anmelden“ und erklärt, warum. Ein Rückwärtszähler nennt vorher die
  restliche Zeit (`oauth.REGISTRIERUNG_TTL_SECONDS`, 15 Minuten).
* **Ausnahme, die bleiben muss:** Solange es noch gar kein Betreiberkonto gibt
  (`GET /api/health` → `"erstzugang": true`), verlangt der Daemon für das allererste Konto den Code
  aus `ERSTER-ADMIN.txt`. Schritt 2 sagt das ausdrücklich in einem eigenen Hinweiskasten und nennt
  die Datei; der Weg ist derselbe.

**Discord nachtragen.** Ein angemeldetes Konto ohne Discord bekommt oben rechts „Discord
verknüpfen“ (`GET /api/auth/discord/start?link=1&ziel=…`). Nach der Rückleitung mit `#verknuepft=1`
bleibt die Sitzung bestehen, es erscheint nur eine kurze Meldung.

**Das Client-Secret darf niemals in eine dieser Dateien oder in eine Antwort gelangen** – der
Code-Tausch geschieht allein im Daemon. Die Seite sieht nur `url`, `state`, den Merkzettel und am
Ende das Sitzungstoken.

### 1.3 Zusätzliche Felder, die die Oberfläche gern hätte

Alles hier ist **freiwillig**: fehlt ein Feld, zeigt die Seite `–` oder einen Ersatztext, sie bricht
nicht ab.

| Route | Feld | Wofür | Zustand |
|---|---|---|---|
| `GET /api/me` → `user` | `discord_name` | Name oben rechts | fehlt |
| `GET /api/me` → `user` | `avatar_url` | Bild oben rechts; nur `https://cdn.discordapp.com/…` wird angezeigt, sonst Anfangsbuchstabe | fehlt |
| `GET /api/admin/users` → je Konto | `discord_name` | Spalte „Discord“ (sonst nur „verknüpft / nicht verknüpft“ aus `discord_linked`) | fehlt |
| `GET /api/admin/users` → je Konto | `last_seen` (Unix-Sekunden) | Spalte „Letzte Anmeldung“ – größter `last_seen` der Sitzungen des Kontos | fehlt |
| `GET /api/admin/status` | `load` = `[1min, 5min, 15min]` (`os.getloadavg()`) | Kachel „Last“ | fehlt |
| Instanz (`server_view`) | `subdomain` bzw. fertiges `address` | Spalte „Adresse“; ohne das Feld steht dort `arcardia-nexus.de:<port>` | fehlt |

Genutzt werden sonst genau die Felder, die `users.public_user`, `passes.public_pass`,
`instances.public_instance` und `server_view` heute schon liefern.

---

## 2. Benutzte Routen (Stand `mcsmd.py`, alle mit `Authorization: Bearer <token>`)

| Aufruf | Wann | Erwartete Antwort |
|---|---|---|
| `GET /api/me` | einmal beim Anmelden | `{"user": {…, "role": "admin"}}` – ohne `role == "admin"` zeigt die Seite einen Hinweis statt der Verwaltung |
| `POST /api/auth/logout` | Knopf „Abmelden“ | egal |
| `GET /api/auth/discord/start` | „Mit Discord anmelden“ und „Discord verknüpfen“ (`?link=1`) | `{"url": "https://discord.com/…", "state": "…"}` |
| `POST /api/auth/discord/register` | Schritt 2, „Konto einrichten“ | Rumpf `{zettel, code, note}` → `{"token", "expires_at", "user"}` |
| `GET /api/health` | auf der Anmeldeseite und in Schritt 2 | `{"discord": false}` heißt: Der Knopf wird stumpf, die Seite erklärt, dass die Zugangsdaten fehlen. `{"erstzugang": true}` heißt: Dieser Server hat noch keinen Betreiber – Schritt 2 sagt dann, dass dafür der Code aus `ERSTER-ADMIN.txt` gilt (siehe spec-oauth.md, Abschnitt 5) |
| `GET /api/admin/status` | alle 5 s | siehe unten |
| `GET /api/admin/users` | alle 5 s | `{"users": [ … ]}` mit `limits`, `limits_text`, `passes` |
| `GET /api/admin/invites` | alle 5 s, nur im Reiter „Einladungscodes“ | `{"invites": [ … ]}` mit `state`, `state_text`, `uses`, `uses_max` |
| `GET /api/admin/passes` | alle 5 s, nur im Reiter „Pässe“ | `{"passes": [ … ], "limits": {…}}` |
| `GET /api/admin/instances` | alle 5 s (Reiter Benutzer, Server, Maschine) | `{"instances": [ … ]}` mit `live` und `ports_live` |
| `POST /api/admin/invites` | „Code erzeugen“ | Rumpf `{uses_max: 1–50, days: 0–365, note}` → `{"invite": {"code": "KRF2-TGQN-SPAP"}}` |
| `POST /api/admin/invites/<code>/revoke` | „Zurückziehen“ | der Code wird in der **Strichform** geschickt (`users.normalize_code` nimmt sie an) |
| `POST /api/admin/users/<id>/block` | „Sperren/Entsperren“ | Rumpf `{blocked: bool, reason: str}` |
| `POST /api/admin/users/<id>/rename` | „Umbenennen“ in der aufgeklappten Zeile | Rumpf `{name}` (2–32 Zeichen) |
| `POST /api/admin/users/<id>/role` | „Zum Betreiber machen“ / „Betreiberrecht abgeben“ | Rumpf `{role: "admin"\|"user"}`; das **eigene** Recht lässt die Seite nicht abgeben |
| `POST /api/admin/passes` | „Pass ausstellen“ | Rumpf `{user_id, kind: "local"\|"premium", days, max_concurrent, ram_total_mb, note}` |
| `POST /api/admin/passes/<id>/extend` | „Verlängern“ | Rumpf `{days: 1–3650}` |
| `POST /api/admin/passes/<id>/limits` | „Grenzen“ | Rumpf `{max_concurrent: 1–10, ram_total_mb}` |
| `POST /api/admin/passes/<id>/revoke` | „Widerrufen“ | Antwort enthält `stoppt_um` (Unix-Sekunden): Der Dienst sagt den Stopp sofort im Spiel an und fährt die Server erst **10 Minuten** später herunter (höchstens bis `expires_at`) |
| `POST /api/servers/<id>/start` | „Starten“ in der Server-Übersicht | Admin darf fremde Instanzen steuern (`Req.instance`) |
| `POST /api/servers/<id>/stop` | „Stoppen“ | – |
| `GET /api/servers/<id>/console?tail=200` | Serverfenster, alle 3 s solange es offen ist | `{"next", "lines": [...], "live": {…}}`; ist der Server aus, kommen keine Zeilen und das Fenster sagt das |

Aus `GET /api/admin/status` werden gelesen: `version`, `time`, `started_at`, `users`, `instances`,
`passes`, `running` (Liste der Laufzustände mit `id`, `running`, `ready`, `ram_mb`, `memory_mb`,
`uptime`), `ram_running_mb`, `ram_machine_mb`, `ram_available_mb`, `disk_free_bytes`,
`disk_free_text`, `disk_total_bytes`, `disk_free_percent`, `disk_warning`, `load`, `ports`
(vergebene Portbuchungen, nur gezählt), `routes` (Einträge im Verteiler, nur gezählt), `discord`,
`domain`, `premium_delete_candidates`, `transfers` (mit `kind`, `state`, `instance_id`,
`total_bytes`, `done_files`, `created_at`) und `size_bytes`.

Fehler werden immer als `{"error": "deutscher Satz"}` erwartet und unverändert angezeigt. Kommt kein
Satz mit, setzt die Seite selbst einen ein („Bitte neu anmelden.“, „Der Server ist gerade nicht
erreichbar.“ …) – **eine rohe Fehlernummer bekommt der Betreiber nie zu sehen**. Bei `401` wird das
Token verworfen und die Anmeldeseite gezeigt.

---

## 3. Aufbau der Seite

**Anmeldung** – Logo, „Verwaltung“, eine Karte mit dem einen Knopf „Mit Discord anmelden“. Fehlt
dem Server das Konto, tritt an die Stelle dieser Karte Schritt 2 (Einladungscode, siehe 1.2) mit
einer kleinen Schrittanzeige „1 Discord – 2 Einladungscode“.

**Kopfbereich** – links Logo und Name, in der Mitte der Kurzstand als Pillen (laufende Server,
vergebener Speicher, freie Platte), rechts das Konto (Bild, Name, Rolle), „Discord verknüpfen“
(nur ohne Verknüpfung) und „Abmelden“. Darunter die Reiter als Pillen, die mit einer Zahl sagen,
wie viel dahinter steckt.

Die Seite nutzt die **ganze Fensterbreite** (bis 1560 px); Formulare stehen links, ihre Vorschau
rechts daneben, damit im breiten Fenster keine leere Fläche bleibt.

1. **Übersicht** – fünf Kennzahlen (laufende Server, vergebener Speicher, freie Platte mit Balken,
   Konten, gültige Pässe). Darunter zwei Karten: *Das braucht Aufmerksamkeit* (knappe Platte, Server
   die auf Rückholung warten, ruhende Premium-Server, bald ablaufende Pässe, gesperrte Konten,
   löschbare Premium-Server, offene Übertragungen, Konten ohne Pass – jeder Punkt mit einem Knopf,
   der zum richtigen Reiter springt; ist nichts offen, steht „Alles ruhig“) und *Gerade an* (die
   laufenden Server mit Besitzer, Speicher, Laufzeit, „Ansehen“ und „Stoppen“). Zuletzt drei
   Balken für Arbeitsspeicher, Platte und Last.
2. **Benutzer** – vier Kennzahlen, Suchfeld und Filter (Alle / Mit Pass / Gesperrt), dann die Liste
   mit Name, Discord, Rolle, Grenzen, Anzahl Server, letzter Anmeldung und Zustand. Jede Zeile lässt
   sich aufklappen: Server des Kontos (mit Zustand, Größe, Arbeitsspeicher, „Ansehen“), seine Pässe
   und die Knöpfe „Pass ausstellen“ (springt ins Formular und trägt den Benutzer ein),
   „Umbenennen“, „Zum Betreiber machen“. „Sperren“ fragt nach einem Grund und nennt die laufenden
   Server, die dabei heruntergefahren werden.
3. **Einladungscodes** – Formular (Verwendungen, Gültigkeit in Tagen, Notiz), daneben eine Karte,
   die den Weg erklärt; der frische Code erscheint groß mit „Kopieren“. Liste aller Codes mit
   Zustand, Verwendungen, Ablauf, Filter „nur offene“, Kopieren und Zurückziehen (mit Rückfrage).
4. **Pässe** – das Herzstück. Links Benutzerauswahl, Anzahl gleichzeitiger Server, Art
   (Lokal/Premium als zwei Kacheln), Laufzeit (Schnellwahl Tag/Woche/Monat/Jahr plus freies Feld),
   RAM-Budget als Schieberegler 1–10 GB mit dem Hinweis „z. B. 8 GB = zwei Server mit je 4 GB“,
   Notiz. Rechts daneben die Vorschau in Klartext, die sich bei jeder Änderung mitschreibt:
   *„**Anna** darf ab sofort **14 Tage** lang **2 Server** gleichzeitig laufen lassen, zusammen
   höchstens **8 GB** Arbeitsspeicher.“* – dazu Art, Enddatum und der Schnitt je Server.
   Darunter alle Pässe mit Restzeit und Zustand, Filter „nur gültige“, „Verlängern“ (fragt nach den
   Tagen), „Grenzen“ (Anzahl und RAM nachträglich ändern) und „Widerrufen“ (Rückfrage nennt die
   laufenden Server und sagt ausdrücklich, dass sie in **10 Minuten** heruntergefahren werden).
5. **Server** – alle Instanzen aller Konten: Name mit Art und Fassung, Besitzer, Zustand (mit
   Laufzeit), Größe, Arbeitsspeicher („3,1 GB von 4 GB“, solange der Server läuft), Adresse mit
   Ports, Starten/Stoppen/Ansehen. Laufende zuerst, dann nach Besitzer; Suchfeld und Filter
   (Alle / Laufend / Gestoppt / Wartend). Ein Klick auf die Zeile öffnet das **Serverfenster**:
   alle Angaben als Liste und die letzten 200 Konsolenzeilen, die sich alle 3 Sekunden
   nachziehen (Warnungen gelb, Fehler rot).
6. **Maschine** – freier Arbeitsspeicher, an Server vergeben, freie Platte, Last als Kacheln mit
   Balken; eine Karte mit den Auslastungsbalken, eine mit den laufenden Servern, eine mit den
   offenen Übertragungen (Art, fertige Dateien, Gesamtgröße, Beginn) und eine mit den Angaben zum
   Dienst (Fassung, Laufzeit, Zahlen, vergebene Ports, Einträge im Verteiler, ob Discord
   eingerichtet ist). **Fällt die Platte unter 15 %, steht ganz oben ein roter Kasten**, der
   erklärt, dass die Platte der Engpass ist und unter 3 GB jeder Start abgelehnt wird. Ruhende
   Premium-Server aus `premium_delete_candidates` bekommen einen eigenen Hinweis.

Alle sichtbaren Texte sind deutsch und ohne Fachchinesisch; Zahlen stehen mit Komma und Einheit da
(„8 GB“, „4,10 GB“, „läuft in 11 Tagen ab“). Leere Listen erklären in einem Satz, was fehlt, und
zeigen – wo es passt – einen Knopf dorthin, wo man es anlegt.

**Barrierearm:** ganz oben ein Sprunglink „Zum Inhalt springen“, sichtbarer Fokusring
(`:focus-visible`) auf allem Bedienbaren, die Reiter als `role="tablist"` mit `aria-selected`,
Suchfelder und Filtergruppen mit `aria-label`, jeder Kasten als `role="dialog"` mit
`aria-modal`/`aria-labelledby`, Escape schließt, die Tabulatortaste bleibt im offenen Kasten.
`prefers-reduced-motion` schaltet die Übergänge ab.

---

## 4. Auffrischen, ohne Eingaben zu verlieren

* Ein einziger Zeitgeber, alle **5 Sekunden** (`REFRESH_MS`). Geholt werden immer `status` und
  `users`, dazu nur das, was der offene Reiter braucht (`invites` auch für die Übersicht, weil dort
  die Zahl am Reiter steht; `instances` überall außer im Reiter „Einladungscodes“).
* Das **Serverfenster** hat einen eigenen Takt von 3 Sekunden für die Konsole; solange es offen
  ist, ruht die allgemeine Auffrischung (`state.freeze`).
* Während das Fenster im Hintergrund liegt (`document.hidden`), wird nichts geholt; beim
  Zurückkommen sofort einmal.
* Solange eine Rückfrage offen ist oder ein Abruf läuft, wird nicht erneut geholt
  (`state.freeze`, `state.busy`).
* **Alle Formulare stehen fest im HTML** und werden nie neu gezeichnet. Gezeichnet werden nur die
  Listen – und auch die nur, wenn sich die Daten wirklich geändert haben (Vergleich über eine
  Signatur) und wenn in diesem Bereich gerade kein Eingabefeld den Fokus hat (`paint()`).
* Die Benutzerauswahl im Pass-Formular wird nur dann neu befüllt, wenn sich die Kontenliste ändert;
  die getroffene Wahl bleibt erhalten.
* Aufgeklappte Benutzerzeilen merkt sich die Seite (`state.open`) und überlebt damit jedes
  Auffrischen.

## 5. Sicherheit und Sorgfalt

* Jeder Wert aus der API läuft durch `esc()`, bevor er in HTML landet – kein `innerHTML` mit rohen
  Namen, Notizen oder Fehlertexten.
* Das Sitzungstoken steht nur in `localStorage` und geht ausschließlich als
  `Authorization: Bearer …` an den **eigenen** Ursprung. Es gibt bewusst **keine** Möglichkeit, die
  API-Adresse über die Adresszeile umzubiegen, damit niemand das Token auf einen fremden Server
  schicken kann.
* Als Bild wird nur `https://cdn.discordapp.com/…` geladen, sonst der Anfangsbuchstabe des Namens –
  so holt die Seite nichts von beliebigen Adressen.
* Alle Kennungen werden mit `encodeURIComponent` in die Pfade gesetzt.
* Jeder Eingriff mit Folgen (Sperren, Zurückziehen, Widerrufen, Verlängern, Stoppen) fragt vorher in
  einem eigenen Kasten nach und sagt, was passiert. Nur „Starten“ und „Code erzeugen“ laufen ohne
  Rückfrage.
* Zahlen aus den Formularen werden vor dem Senden auf die Grenzen gestutzt, die `GET
  /api/admin/passes` unter `limits` mitschickt (`days`, `max_concurrent`, `ram_total_mb`, je als
  Paar). Fehlt das Feld, gelten die Werte aus `passes.py` (Tage 1–3650, gleichzeitig 1–10,
  RAM 1–10 GB). Dieselben Grenzen stehen als `min`/`max` an den Eingabefeldern und in den
  Rückfragen „Verlängern“ und „Grenzen“ – die letzte Prüfung macht natürlich der Server.

## 6. Beispielansicht (zum Anschauen ohne Server)

Wird die Datei direkt geöffnet (`file://`) oder mit `?beispiel=1` aufgerufen, spricht die Seite
**niemanden** an, sondern beantwortet sich selbst aus einer kleinen eingebauten Antwort
(vier Konten, fünf Server, drei Pässe, zwei Codes, eine offene Übertragung, ein Pass der in zwei
Tagen abläuft, knappe Platte mit 13,7 % und ein paar Konsolenzeilen). Oben steht dann ein gelber
Streifen „Beispielansicht – es sind Übungsdaten zu sehen, der Server wird nicht gefragt.“
Jeder Eingriff antwortet mit „In der Beispielansicht wird nichts wirklich geändert.“

Geprüft wurde so: `python -m unittest discover -s hosted/tests` (darin `test_admin_web.py` für den
Zusammenhalt der drei Dateien), dann der Daemon mit `MCSM_DATA` auf einen Temp-Ordner gestartet und
die Seite im eingebauten Browser geöffnet – Anmeldeseite, Beispielansicht mit allen sechs Reitern,
Aufklappen einer Benutzerzeile, Vorschau des Passes, Serverfenster mit Konsole, die Rückfragen und
die schmale Fensterbreite (390 px) durchgesehen; keine Meldung in der Konsole.

## 7. Offene Punkte

1. **Unterdomänen** gibt es noch nirgends. Sobald feststeht, wie eine Instanz ihren Namen bekommt
   (`<name>.arcardia-nexus.de` mit SRV-Eintrag oder fester Port), reicht das Feld `subdomain` oder
   `address` in `server_view` – die Spalte zeigt es dann von selbst an.
2. **Konto löschen** ist absichtlich nicht eingebaut, obwohl `DELETE /api/admin/users/<id>` es
   könnte: das löscht Welten unwiederbringlich. Wenn es hinein soll, dann mit einer Rückfrage, in
   die der Kontoname abgetippt werden muss.
3. **Pass löschen** (`DELETE /api/admin/passes/<id>`) fehlt noch. Abgelaufene Pässe stehen weiter in
   der Liste; das ist als Verlauf eher nützlich als störend.
4. **Befehle an einen Server** (`POST /api/servers/<id>/command`) zeigt das Serverfenster bewusst
   nicht – die Konsole ist hier zum Nachsehen da, nicht zum Mitspielen.
5. Die Oberfläche prüft **nicht**, ob die Summe aller vergebenen Pässe noch in die Maschine passt
   (offener Punkt 6 in `spec-konten.md`). Sie zeigt aber im Reiter „Maschine“ deutlich, wie viel
   noch frei ist, damit der Betreiber es selbst sieht.
