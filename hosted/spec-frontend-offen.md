# Was die Oberfläche für gehostete Server vom Root-Server braucht

Nachtrag zu `spec-cloud-client.md`, geschrieben aus Sicht der **Serverseite im PC-Programm**
(`web/app.js`, `app.py`). Seit Punkt 1 aus `OFFENE-PUNKTE.md` zeigt ein Server im Zustand `hosted`
dieselbe Seite mit denselben Reitern wie ein Server auf diesem PC – Übersicht, Verbinden, Spieler,
Konsole, Dateien, Einstellungen –, nur arbeiten sie gegen den Root-Server.

Alles hier Beschriebene ist auf der PC-Seite **fertig**. Fehlt etwas auf der Gegenseite, bleibt die
Oberfläche stehen: Wege, die mit **404, 405 oder 501** antworten, werden zu
`{"supported": false, "hint": "<deutscher Satz>"}`, und fehlende **Felder** werden nicht erfunden –
die Stelle zeigt dann „unbekannt“ statt einer geratenen Zahl.

---

## 1. Felder je Instanz (`server_view` / `GET /api/servers`, `GET /api/servers/<id>`)

Gemeinsame Benennung, an die sich beide Seiten halten:

| Feld | Typ | Wofür die Oberfläche es benutzt |
|---|---|---|
| `sleeping` | bool | Eigener Zustand **„schläft“** neben „läuft“ und „gestoppt“ (eigene Farbe, eigener Text). Wahr, wenn die Instanz gehostet ist, nicht läuft und der Ruhezustand an ist. |
| `hibernation` | bool (Standard `true`) | Schalter „Ruhezustand bei Leerstand“ in den Einstellungen und die Karte in der Übersicht. |
| `hibernation_minutes` | int (Standard `15`) | Wartezeit daneben. Die Oberfläche nimmt 5 bis 1440 an. |
| `address` | str | Adresse zum Eintippen, z. B. `eutopia.arcardia-nexus.de` – mit Kopierknopf. Bei Bedrock darf `<name>:<port>` kommen; bei Java bitte **ohne** Port (der Verteiler hört auf 25565). |
| `port` | int | Port für Bedrock (und für Java nur, wenn er einmal nicht 25565 ist). Ersatzweise `ports_live`/`ports`. |
| `players_online`, `players_max` | int | Kachel „Spieler“ in der Übersicht. |
| `uptime` | int (Sekunden) | Kachel „Laufzeit“. Ersatzweise `live.uptime`. |
| `ram_mb`, `memory_mb` | int | Kachel „Arbeitsspeicher“ („2,7 GB von 4 GB“). Ersatzweise `live.ram_mb` / `live.memory_mb`. |
| `tps` | float | Kachel „TPS“ (grün ab 19, gelb ab 15, sonst rot). Fehlt sie, steht dort „–“. |
| `max_players` | int | Spieler-Reiter („3 von 10“). Steht heute nicht in `public_instance`; die Oberfläche nimmt sonst den Wert der lokalen Kopie. |
| `geyser` | bool | Zeigt im Reiter „Verbinden“ zusätzlich die Bedrock-Adresse für Konsolen und Handys. |

**Fehlt `sleeping`**, leitet die Oberfläche den Zustand nur ab, wenn `hibernation` mitkommt – sonst
sähe jeder gestoppte Server aus wie ein schlafender. **Fehlen `hibernation`/`hibernation_minutes`
ganz**, sagt die Einstellungsseite in einem ruhigen Satz, dass dieser Root-Server den Ruhezustand
noch nicht kennt, und speichert die übrigen Werte trotzdem.

## 2. Einstellungen einer gehosteten Instanz

**Lesen** geschieht heute über `GET /api/servers/<id>` (Felder aus Abschnitt 1) – ein eigener Weg
`GET /api/servers/<id>/settings` wäre schöner, ist aber nicht nötig.

**Schreiben** läuft über den vorhandenen Weg:

```
POST /api/servers/<id>/settings      Authorization: Bearer <token>
{"name": "Eutopia", "ram_mb": 4096, "hibernation": true, "hibernation_minutes": 15}
 -> 200 {"server": { … server_view … }}
 -> 409 {"error": "„Eutopia“ läuft – der Arbeitsspeicher lässt sich erst nach dem Stoppen ändern."}
```

* `name` und `ram_mb` nimmt der Daemon schon an. **Offen sind `hibernation` und
  `hibernation_minutes`.** Die Oberfläche schickt sie mit; kommen sie in der Antwort nicht zurück,
  meldet sie „Den Ruhezustand kennt dieser Root-Server noch nicht – der Rest ist gespeichert.“
* Reicht das Kontingent des Passes für den Arbeitsspeicher nicht, bitte den **Satz im Klartext**
  (welcher Pass, was läuft gerade) – die Oberfläche zeigt ihn unverändert an.
* Route im Programm: `GET/POST /api/cloud/settings` (`app.py`), Prüfung dort:
  Name 2–32 Zeichen, `ram_mb` 512–32768, `hibernation_minutes` 5–1440.

## 3. server.properties

Gelesen und geschrieben über die vorhandenen Dateiwege
(`GET …/files?action=read&path=server.properties`, `POST …/files {"action":"write"}`).
Die Oberfläche zeigt daraus **dasselbe Formular mit deutschen Beschriftungen** wie bei einem lokalen
Server und lässt Kommentare und unbekannte Zeilen der Datei unangetastet.

Der Daemon lehnt Schreiben ab, solange der Server läuft (`_no_changes_while_running`) – das ist
richtig so; die Oberfläche sperrt den Knopf vorher und sagt „Bitte zuerst stoppen“.
Route im Programm: `GET/POST /api/cloud/props`.

## 4. Spielerverwaltung über den Root-Server

Es gibt dafür **keinen eigenen Weg** auf dem Root-Server und es braucht auch keinen. Die Oberfläche
setzt sie aus dem zusammen, was es schon gibt (Route im Programm: `GET/POST /api/cloud/players`):

* **Listen lesen:** `whitelist.json` / `allowlist.json`, `banned-players.json`, `banned-ips.json`
  und `server.properties` (für `white-list` bzw. `allow-list`) über `files?action=read`.
  Eine fehlende Datei gilt als leere Liste, eine unlesbare wird als „beschädigt“ gemeldet und
  **nicht** überschrieben.
* **Wer ist online:** Konsolenbefehl `list` und die Antwort aus `…/console` – dieselbe Auswertung
  wie bei einem Server auf diesem PC.
* **Ändern bei laufendem Server:** über die Konsole (`whitelist add|remove`, `whitelist on|off`,
  `ban`, `pardon`, `kick`).
* **Ändern bei gestopptem Server:** das Programm schreibt die JSON-Dateien über
  `files {"action":"write"}` (die Spieler-Nummer holt es wie lokal bei Mojang).

Zwei Kleinigkeiten wären hilfreich, sind aber nicht nötig:

1. `max_players` in `server_view` (siehe Abschnitt 1).
2. Eine Zeile in der Konsolenantwort, wenn der Server einen Befehl abgelehnt hat – heute erkennt
   die Oberfläche das daran, dass der Name danach nicht in der Liste steht, und sagt das ehrlich.

## 5. Was die Oberfläche bewusst **nicht** mehr zeigt

* Keine Sperrmeldung für die lokale Kopie – nur einen ruhigen Streifen „läuft auf dem Root-Server“
  mit dem Knopf zum Zurückholen. Gestartet wird die lokale Kopie weiterhin nicht (`app.py`
  lehnt das mit einem erklärenden Satz ab).
* Im Reiter „Verbinden“ **kein** FritzBox- und Firewall-Teil – auf dem Root-Server gibt es keine
  Portfreigabe; stattdessen steht dort, dass ein schlafender Server beim Beitritt von selbst
  startet und die erste Verbindung freundlich getrennt wird.
