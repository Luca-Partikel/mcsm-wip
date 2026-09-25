# Gruppe „admin“ – Betreiberoberfläche für admin.arcardia-nexus.de

Dateien (neu, gehören allein zu dieser Gruppe):

| Datei | Inhalt |
|---|---|
| `hosted/web/admin.html` | Gerüst der Seite: Anmeldung, Kopfbereich, fünf Reiter, feste Formulare |
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

### 1.2 Anmeldung über Discord (**noch 501**)

Die Seite ruft beim Klick auf „Mit Discord anmelden“:

```
GET /api/auth/discord/start?ziel=<absolute URL dieser Seite>
→ 200 {"url": "https://discord.com/oauth2/authorize?…", "state": "<zufällig>"}
```

* Es wird nur eine `url` akzeptiert, die mit `https://discord.com/` oder `https://discordapp.com/`
  beginnt – sonst zeigt die Seite eine Fehlermeldung und blendet die Anmeldung über einen
  Einladungscode ein.
* `ziel` ist ein **Wunsch**, kein Befehl: der Daemon muss den Wert gegen eine eigene weiße Liste
  prüfen (`https://admin.arcardia-nexus.de/admin.html`) und sonst seinen Standard nehmen.
* `state` wird beim Daemon hinterlegt und von ihm geprüft; die Seite legt ihn nur nachrichtlich in
  `sessionStorage` ab.

```
GET /api/auth/discord/callback?code=…&state=…
→ 302 nach https://admin.arcardia-nexus.de/admin.html#token=<sitzungstoken>
   bei einem Fehler: …/admin.html#fehler=<deutscher Satz, URL-kodiert>
```

Die Seite liest `#token=…` beim Start, legt es in `localStorage` (`mcsm_admin_token`) ab und entfernt
die Raute sofort aus der Adresszeile. Akzeptiert werden nur Zeichen `A–Z a–z 0–9 . _ -`, Länge
16–512. Ein `#fehler=` wird als Hinweiskasten auf der Anmeldeseite gezeigt.

**Das Client-Secret darf niemals in eine dieser Dateien oder in eine Antwort gelangen** – der
Code-Tausch geschieht allein im Daemon. Die Seite sieht nur `url`, `state` und am Ende das
Sitzungstoken.

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
| `POST /api/auth/invite` | Notanmeldung mit Code | `{"token": "…"}` |
| `GET /api/health` | auf der Anmeldeseite | `{"erstzugang": true}` heißt: Dieser Server hat noch keinen Betreiber. Die Seite klappt dann das Codefeld auf und sagt, dass der Code aus `ERSTER-ADMIN.txt` kommt – er gilt für **beide** Wege, auch für Discord (siehe spec-oauth.md, Abschnitt 5) |
| `GET /api/admin/status` | alle 5 s | siehe unten |
| `GET /api/admin/users` | alle 5 s | `{"users": [ … ]}` mit `limits`, `limits_text`, `passes` |
| `GET /api/admin/invites` | alle 5 s, nur im Reiter „Einladungscodes“ | `{"invites": [ … ]}` mit `state`, `state_text`, `uses`, `uses_max` |
| `GET /api/admin/passes` | alle 5 s, nur im Reiter „Pässe“ | `{"passes": [ … ], "limits": {…}}` |
| `GET /api/admin/instances` | alle 5 s (Reiter Benutzer, Server, Maschine) | `{"instances": [ … ]}` mit `live` und `ports_live` |
| `POST /api/admin/invites` | „Code erzeugen“ | Rumpf `{uses_max: 1–50, days: 0–365, note}` → `{"invite": {"code": "KRF2-TGQN-SPAP"}}` |
| `POST /api/admin/invites/<code>/revoke` | „Zurückziehen“ | der Code wird in der **Strichform** geschickt (`users.normalize_code` nimmt sie an) |
| `POST /api/admin/users/<id>/block` | „Sperren/Entsperren“ | Rumpf `{blocked: bool, reason: str}` |
| `POST /api/admin/passes` | „Pass ausstellen“ | Rumpf `{user_id, kind: "local"\|"premium", days, max_concurrent, ram_total_mb, note}` |
| `POST /api/admin/passes/<id>/extend` | „Verlängern“ | Rumpf `{days: 1–3650}` |
| `POST /api/admin/passes/<id>/revoke` | „Widerrufen“ | Antwort enthält `stoppt_um` (Unix-Sekunden): Der Dienst sagt den Stopp sofort im Spiel an und fährt die Server erst **10 Minuten** später herunter (höchstens bis `expires_at`) |
| `POST /api/servers/<id>/start` | „Starten“ in der Server-Übersicht | Admin darf fremde Instanzen steuern (`Req.instance`) |
| `POST /api/servers/<id>/stop` | „Stoppen“ | – |

Aus `GET /api/admin/status` werden gelesen: `version`, `time`, `started_at`, `users`, `instances`,
`passes`, `running` (Liste der Laufzustände mit `id`, `running`, `ready`, `ram_mb`, `memory_mb`,
`uptime`), `ram_running_mb`, `ram_machine_mb`, `ram_available_mb`, `disk_free_bytes`,
`disk_free_text`, `disk_total_bytes`, `disk_free_percent`, `disk_warning`, `free_ports_java`,
`free_ports_bedrock`, `premium_delete_candidates`, `transfers`, `size_bytes` – und zusätzlich das
noch fehlende `load`.

Fehler werden immer als `{"error": "deutscher Satz"}` erwartet und unverändert angezeigt. Kommt kein
Satz mit, setzt die Seite selbst einen ein („Bitte neu anmelden.“, „Der Server ist gerade nicht
erreichbar.“ …) – **eine rohe Fehlernummer bekommt der Betreiber nie zu sehen**. Bei `401` wird das
Token verworfen und die Anmeldeseite gezeigt.

---

## 3. Aufbau der Seite

**Anmeldung** – ein Knopf „Mit Discord anmelden“. Darunter unauffällig „Mit Einladungscode
anmelden“ (ruft `POST /api/auth/invite`), damit der Betreiber auch hereinkommt, solange Discord
nicht eingerichtet ist; bei einem Fehler der Discord-Anmeldung klappt dieser Teil von selbst auf.

**Kopfbereich** – Name und Bild des angemeldeten Kontos, Knopf „Abmelden“, darunter in einer Zeile
der Kurzstand (laufende Server, belegter Speicher, freie Platte).

1. **Benutzer** – vier Kennzahlen, dann die Liste mit Name, Discord, Rolle, angelegt, letzter
   Anmeldung und Zustand. Jede Zeile lässt sich aufklappen: Server des Kontos (mit Zustand, Größe,
   Arbeitsspeicher) und seine Pässe, dazu ein Knopf, der direkt ins Pass-Formular springt und den
   Benutzer schon einträgt. „Sperren“ fragt nach einem Grund und nennt die laufenden Server, die
   dabei heruntergefahren werden.
2. **Einladungscodes** – Formular (Verwendungen, Gültigkeit in Tagen, Notiz), der frische Code
   erscheint groß mit „Kopieren“. Liste aller Codes mit Zustand, Verwendungen, Ablauf, Kopieren und
   Zurückziehen (mit Rückfrage).
3. **Pässe** – das Herzstück. Benutzerauswahl, Art (Lokal/Premium als zwei Kacheln), Laufzeit
   (Schnellwahl Tag/Woche/Monat plus freies Feld), Anzahl gleichzeitiger Server, RAM-Budget als
   Schieberegler 1–10 GB mit dem Hinweis „z. B. 8 GB = zwei Server mit je 4 GB“, Notiz. Darunter die
   Vorschau in Klartext, die sich bei jeder Änderung mitschreibt:
   *„**Anna** darf ab sofort **14 Tage** lang **2 Server** gleichzeitig laufen lassen, zusammen
   höchstens **8 GB** Arbeitsspeicher. Der Pass endet am 9. Oktober 2026. Nach Ablauf holt Anna die
   Server auf den eigenen PC zurück.“*
   Darunter alle Pässe mit Restzeit und Zustand, Filter „nur gültige“, „Verlängern“ (fragt nach den
   Tagen) und „Widerrufen“ (Rückfrage nennt die laufenden Server und sagt ausdrücklich, dass sie in
   **10 Minuten** heruntergefahren werden).
4. **Server** – alle Instanzen aller Konten: Name mit Art und Fassung, Besitzer, Zustand (mit
   Laufzeit), Größe, Arbeitsspeicher („3,1 GB von 4 GB“, solange der Server läuft), Ports, Adresse,
   Starten/Stoppen. Laufende zuerst, dann nach Besitzer.
5. **Maschine** – freier Arbeitsspeicher, an Server vergeben, freie Platte, Last; zwei Balken für
   Speicher und Platte; die laufenden Server mit Besitzer; Dienstfassung, Laufzeit, Zahlen, freie
   Ports, offene Übertragungen. **Fällt die Platte unter 15 %, steht ganz oben ein roter Kasten**,
   der erklärt, dass die Platte der Engpass ist und unter 3 GB jeder Start abgelehnt wird. Ruhende
   Premium-Server aus `premium_delete_candidates` bekommen einen eigenen Hinweis.

Alle sichtbaren Texte sind deutsch und ohne Fachchinesisch; Zahlen stehen mit Komma und Einheit da
(„8 GB“, „4,10 GB“, „läuft in 11 Tagen ab“).

---

## 4. Auffrischen, ohne Eingaben zu verlieren

* Ein einziger Zeitgeber, alle **5 Sekunden** (`REFRESH_MS`). Geholt werden immer `status` und
  `users`, dazu nur das, was der offene Reiter braucht.
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
* Zahlen aus den Formularen werden vor dem Senden auf die Grenzen aus `passes.py` gestutzt
  (Tage 1–3650, gleichzeitig 1–10, RAM 1–10 GB); die letzte Prüfung macht natürlich der Server.

## 6. Beispielansicht (zum Anschauen ohne Server)

Wird die Datei direkt geöffnet (`file://`) oder mit `?beispiel=1` aufgerufen, spricht die Seite
**niemanden** an, sondern beantwortet sich selbst aus einer kleinen eingebauten Antwort
(drei Konten, fünf Server, drei Pässe, zwei Codes, knappe Platte mit 13,7 %). Oben steht dann ein
gelber Streifen „Beispielansicht – es sind Übungsdaten zu sehen, der Server wird nicht gefragt.“
Jeder Eingriff antwortet mit „In der Beispielansicht wird nichts wirklich geändert.“

Geprüft wurde so: `node --check admin.js`, dann die Seite über einen kurzlebigen lokalen
Dateiserver im eingebauten Browser geöffnet (`?beispiel=1`, 900 und 1280 Pixel breit) und alle fünf
Reiter, das Aufklappen einer Benutzerzeile, die Vorschau des Passes, die Rückfrage beim Widerruf und
die Fehlermeldungen durchgeklickt – keine Meldung in der Konsole.

## 7. Offene Punkte

1. **Auslieferung der Dateien** (Abschnitt 1.1) und **Discord-Anmeldung** (1.2) fehlen im Daemon.
   Bis dahin kommt man über den Einladungscode herein.
2. **Unterdomänen** gibt es noch nirgends. Sobald feststeht, wie eine Instanz ihren Namen bekommt
   (`<name>.arcardia-nexus.de` mit SRV-Eintrag oder fester Port), reicht das Feld `subdomain` oder
   `address` in `server_view` – die Spalte zeigt es dann von selbst an.
3. **Konto löschen** ist absichtlich nicht eingebaut, obwohl `DELETE /api/admin/users/<id>` es
   könnte: das löscht Welten unwiederbringlich. Wenn es hinein soll, dann mit einer Rückfrage, in
   die der Kontoname abgetippt werden muss.
4. **Rolle ändern** (`POST /api/admin/users/<id>/role`) und **Umbenennen** sind ebenfalls noch nicht
   in der Oberfläche – beides ist selten und kann nachgereicht werden.
5. **Pass-Grenzen nachträglich ändern** (`POST /api/admin/passes/<id>/limits`) fehlt noch; bisher
   gibt es Verlängern und Widerrufen. Sinnvoll wäre ein „Ändern“ neben „Verlängern“, das denselben
   Schieberegler zeigt.
6. Die Oberfläche prüft **nicht**, ob die Summe aller vergebenen Pässe noch in die Maschine passt
   (offener Punkt 6 in `spec-konten.md`). Sie zeigt aber im Reiter „Maschine“ deutlich, wie viel
   noch frei ist, damit der Betreiber es selbst sieht.
