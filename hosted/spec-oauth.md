# Gruppe „oauth“ – Anmeldung über Discord

Dateien (fertig, nur Standardbibliothek, kompilieren unter Python 3.11):

| Datei | Inhalt |
|---|---|
| `hosted/core/oauth.py` | Discord-Anmeldung: Zustandswerte, Code-Tausch, Profilabruf, Kontozuordnung, Abholcodes |
| `hosted/tests/test_oauth.py` | 72 Selbsttests, ohne echte Zugangsdaten und ohne Netzverbindung |
| `hosted/spec-oauth.md` | dieses Papier |

Geprüft mit:

* `python -m py_compile hosted/core/oauth.py hosted/tests/test_oauth.py` – lokal (3.14) und auf dem
  Root-Server (3.11)
* `python -m unittest discover -s hosted/tests -p test_oauth.py` – **72 Tests, alle grün**, lokal und
  auf dem Root-Server
* voller Durchlauf aller Selbsttests des Hosted-Modus lokal: **353 Tests, alle grün** (4 übersprungen,
  Linux-spezifisch)
* echter Leseversuch auf dem Root-Server als Dienstbenutzer `mcsm` gegen `/srv/mcsm/data`:
  Zugangsdaten werden gefunden, das Geheimnis kommt in keiner Meldung vor, die Rechteprüfung meldet
  zu Recht, dass `discord_client_id` derzeit `640` hat (Gruppe liest mit) – Empfehlung `600`.

Abhängigkeiten: `oauth` → `users` → `store_hosted` (alles relative Importe, keine Ringe). Das Modul
legt **keine** neuen Dateien unter `/srv/mcsm/data` an; Zustandswerte und Abholcodes leben nur im
Arbeitsspeicher.

---

## 1. Was das Modul tut – und was ausdrücklich nicht

* Umfang (`scope`) ist **ausschließlich `identify`**. Wir holen Discord-Kennung, Benutzernamen,
  Anzeigenamen und die Adresse des Profilbilds. **Keine E-Mail-Adresse**, keine Serverliste, keine
  Freundesliste, kein `bot`, kein `guilds`. Alles andere aus der Antwort von `/users/@me` wird
  verworfen (auch ein eventuell mitgeliefertes `email`-Feld).
* Das Zugriffstoken von Discord wird für genau einen Abruf benutzt, danach bei Discord widerrufen
  (`/oauth2/token/revoke`) und weggeworfen. Es wird **nirgends gespeichert und nirgends
  protokolliert**. Der Widerruf läuft in einem `finally`, also auch dann, wenn der Profilabruf
  scheitert.
* Ein `refresh_token` wird nie angefordert und nie ausgewertet.
* Das Anwendungsgeheimnis wird einmal gelesen, im Arbeitsspeicher gehalten und kommt in keiner
  Meldung vor. `repr(Zugangsdaten)` liefert `secret=<geheim>`; `_sauber()` ersetzt das Geheimnis
  vorsorglich in jedem Fremdtext, der in eine Fehlermeldung wandert (z. B. eine Fehlerantwort von
  Discord, die den gesendeten Wert zurückspiegelt).
* Fehlen die Zugangsdaten, wirft **nur** die Anmeldung einen Fehler (`OAuthNichtEingerichtet`,
  HTTP 501). Der Dienst läuft weiter, die Anmeldung mit Einladungscode bleibt möglich.

---

## 2. Zugangsdaten

```
/srv/mcsm/data/discord_client_id      erste Zeile: 15–22 Ziffern
/srv/mcsm/data/discord_secret         erste Zeile: 16–200 Zeichen [A-Za-z0-9_.-], ohne Leerzeichen
```

Ordner: `MCSM_DISCORD_DIR`, sonst der normale Datenordner (`MCSM_DATA`, sonst `/srv/mcsm/data`).
Die Selbsttests setzen `MCSM_DISCORD_DIR` auf einen Temp-Ordner und brauchen deshalb keine echten
Zugangsdaten.

```python
credentials_dir() -> Path
credentials_paths() -> tuple[Path, Path]      # (client-id-Datei, Geheimnis-Datei)
zugangsdaten(*, neu_lesen=False) -> Zugangsdaten      # gecacht; OAuthNichtEingerichtet bei Fehlern
eingerichtet() -> bool                                # ohne Ausnahme, für /api/me und /api/health
vergessen() -> None                                   # nach dem Austauschen der Dateien
startmeldung() -> str                                 # ein Satz fürs Protokoll, nie mit Geheimnis
```

Gelesen wird **einmal**; danach kommt der Wert aus dem Speicher. Auch ein Fehler wird gemerkt, damit
nicht jede Anfrage erneut auf die Platte greift – `vergessen()` setzt beides zurück. Wechselt der
Ordner (Selbsttests), wird automatisch neu gelesen.

`startmeldung()` warnt zusätzlich, wenn eine der Dateien unter Linux für andere Benutzer lesbar ist
(`Rechte & 0o077`). Beispiel:

```
Discord-Anmeldung aktiv (Client-ID …, Umfang „identify“, Zugangsdaten aus „/srv/mcsm/data“).
Achtung: „discord_client_id“ ist auch für andere Benutzer lesbar – bitte auf 600 setzen.
```

Fehlt etwas, lautet die Meldung z. B.:

```
Discord-Anmeldung nicht aktiv: Die Anmeldung über Discord ist auf diesem Server noch nicht
eingerichtet: Die Datei „discord_secret“ mit dem Anwendungsgeheimnis fehlt im Ordner
„/srv/mcsm/data“. Der Dienst läuft trotzdem; die Anmeldung mit Einladungscode bleibt möglich.
```

---

## 3. Die drei Anmeldewege

| Weg (`flow`) | Rückleitung (`redirect_uri`) | Wer fängt sie auf |
|---|---|---|
| `admin` | `https://admin.arcardia-nexus.de/auth/discord/callback` | der Daemon (über nginx) |
| `app` | `https://api.arcardia-nexus.de/auth/discord/callback` | der Daemon (über nginx) |
| `lokal` | `http://127.0.0.1:<port>/cloud-callback` | **das Programm auf dem PC selbst** |

```python
clean_flow(raw) -> str
clean_port(raw) -> int                  # 1024–65535
lokale_rueckleitung(port) -> str        # http://127.0.0.1:<port>/cloud-callback
redirect_uri(flow, port=0) -> str
```

Discord vergleicht die Rückleitung **zeichengenau** mit der Liste in der App – Platzhalter gibt es
nicht. Der lokale Port muss deshalb fest sein (siehe Abschnitt 9).

---

## 4. Ablauf

### 4.1 Anmeldung beginnen

```python
start(flow, *, invite_code="", port=0, abhol_hash="", prompt="", now=None) -> dict
# -> {"url", "state", "flow", "redirect_uri", "scope", "expires_at", "gueltig_sekunden"}
```

* erzeugt den Zustandswert mit `secrets.token_urlsafe(32)`,
* merkt sich dazu Weg, Port, Einladungscode und (für den Notweg) das Abholmerkmal,
* baut die Adresse `https://discord.com/oauth2/authorize?response_type=code&client_id=…&scope=identify&redirect_uri=…&state=…`.

Ein mitgegebener Einladungscode wird sofort geprüft (`users.normalize_code`) und am Zustandswert
gemerkt – der Benutzer muss ihn nach der Rückleitung nicht erneut eintippen, und ein Tippfehler
fällt auf, **bevor** der Umweg über Discord beginnt. `prompt` darf leer, `none` oder `consent` sein.

### 4.2 Zustandswert (state)

* Gültigkeit `STATE_TTL_SECONDS = 600` (10 Minuten), **genau einmal** verwendbar.
* `zustand_einloesen()` vergleicht zeitkonstant (`secrets.compare_digest`) und entfernt den Eintrag
  beim ersten Zugriff – ein doppelt geöffneter Rückweg läuft damit ins Leere.
* **Der Zustandswert allein bindet aber niemanden.** Er ist einmalig und befristet, hängt jedoch an
  keinem Browser. Ein Angreifer kann die Anmeldung mit seinem **eigenen** Discord-Konto beginnen,
  die Rückleitung abfangen (er führt die 302 einfach nicht aus) und dem Betreiber die Adresse
  schicken; dessen Browser holte sie und bekäme stillschweigend das Sitzungstoken des Angreifers
  gesetzt (Anmelde-CSRF). `SameSite=Lax` hilft nicht, weil die Sitzung durch eine gewöhnliche
  GET-Navigation auf die eigene Seite entsteht.
  Deshalb setzt `GET /auth/discord/start` beim Weg `admin` zusätzlich ein kurzlebiges Cookie
  `mcsm_anmeldung` (`HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age = STATE_TTL_SECONDS`) mit einem
  Zufallswert; dessen SHA256 merkt sich der Vorgang im Feld `browser`. Die Rückleitung wird **nur**
  angenommen, wenn das Cookie dazu passt – sonst kommt eine Meldung „Diese Anmeldung wurde in einem
  anderen Browser begonnen“ und **keine** Sitzung. Der Zustandswert bleibt dabei unverbraucht, der
  richtige Browser kann die Anmeldung also noch abschließen.
* Nur im Arbeitsspeicher: Ein Neustart des Dienstes macht laufende Anmeldungen ungültig. Der
  Benutzer liest dann: „Diese Anmeldung ist abgelaufen, wurde schon abgeschlossen oder gehört nicht
  zu diesem Server. Bitte die Anmeldung erneut starten.“
* Obergrenze `STATES_MAX = 500`; bei Überlauf fallen die ältesten heraus.
  `zustaende_aufraeumen(now=…)` und `zustaende_offen(now=…)` sind für das stündliche Aufräumen und
  für Übersichten des Betreibers da.

### 4.3 Rückleitung auswerten

```python
complete(state, code, *, error="", error_description="", now=None) -> Ergebnis
```

Reihenfolge: Abbruch prüfen → Zustandswert einlösen → Anmeldecode prüfen → Token holen → Profil
lesen → Token widerrufen. Bei falschem Zustandswert oder unsinnigem Anmeldecode geht **keine**
Anfrage ins Netz.

```python
class Identitaet(NamedTuple):
    discord_id: str      # nur Ziffern, 15–22 Stellen
    benutzername: str    # username
    anzeigename: str     # global_name, sonst username
    avatar_url: str      # cdn.discordapp.com, oder Standardbild
    def as_dict(self) -> dict

class Ergebnis(NamedTuple):
    identitaet: Identitaet
    flow: str
    invite_code: str      # was bei start() mitgegeben wurde
    port: int
    abhol_hash: str
```

Das Profilbild wird streng geprüft: Der Hash muss `[0-9a-f]{32}` sein (mit erlaubtem Vorsatz `a_`
für bewegte Bilder → `.gif`, sonst `.png`), sonst nimmt das Modul das Standardbild
`embed/avatars/<n>.png` (`(id >> 22) % 6`, bei alten Konten `discriminator % 5`). Ein
untergeschobener Wert wie `../../boese.png?x=y` landet also nie in einer Bildadresse.

### 4.4 Konto und Sitzung

```python
freier_name(identitaet) -> str
erster_admin_faellig(*, user_id="") -> bool
erstanmeldung_als_admin(user_id, *, now=None) -> tuple[dict, bool]
konto_fuer(identitaet, *, invite_code="", now=None) -> Konto        # (user, neu, admin_vergeben)
anmelden(ergebnis|identitaet, *, invite_code=None, note="", days=None, now=None) -> Anmeldung
verknuepfen(user_id, identitaet) -> dict
```

`anmelden()` macht beides in einem Zug und liefert

```python
class Anmeldung(NamedTuple):
    user: dict ; token: str ; expires_at: int ; neu: bool ; admin_vergeben: bool
    def as_dict(self) -> dict     # {"token", "expires_at", "user": public_user(...),
                                  #  "neues_konto", "admin_vergeben"}
```

Der Anzeigename wird aus Discord abgeleitet, aber nie ungeprüft übernommen: Erst werden alle
Zeichen entfernt, die `users.clean_name` nicht erlaubt (Emoji, Steuerzeichen …), dann wird der
Anzeigename, danach der Benutzername probiert; ist der Wunschname belegt, wird durchnummeriert
(`Luca 2`), und bleibt gar nichts übrig, heißt das Konto `Spieler <letzte 4 Stellen der Kennung>`.
Bei einer **erneuten** Anmeldung wird der Name **nicht** überschrieben – wer sich im Programm
umbenannt hat, behält seinen Namen.

---

## 5. Erster Login = Betreiber

```python
erster_admin_faellig(user_id="") -> bool
```

`True` nur, wenn

1. es **kein** nutzbares Admin-Konto gibt (`users.count_admins() == 0`, gesperrte zählen nicht) **und**
2. außer dem betrachteten Konto **überhaupt kein weiteres Konto** existiert.

Damit **darf** die allererste Anmeldung auf einem frischen Server das Betreiberkonto anlegen –
sie braucht dafür aber den Einladungscode aus `ERSTER-ADMIN.txt`, und zwar auch auf dem Weg über
Discord:

```python
oauth.erstzugang_setzen(code)     # setzt der Dienst in mcsmd.ensure_first_admin()
oauth.erstzugang_offen() -> bool
```

`konto_fuer` vergleicht den mitgegebenen `invite_code` zeitkonstant mit diesem Code, löst die
Einladung ein (`users.redeem_invite`) und setzt danach die Rolle `admin`. Ohne Code oder mit
falschem Code: `EinladungNoetig` (HTTP 403) mit dem Hinweis auf `ERSTER-ADMIN.txt`.

**Warum:** `GET /auth/discord/start` und `/auth/discord/callback` sind `auth=False` und über nginx
unter `api.` und `admin.` öffentlich erreichbar. Zwischen „Dienst läuft“ und „der Betreiber meldet
sich zum ersten Mal an“ liegen in der Praxis Stunden. Ohne diese Prüfung bekäme schlicht der
Schnellste ein Konto mit `role=admin` – die 0600-Datei schützte dann nichts mehr. Der Dienst legt
den Code bei **jedem** Start neu in `_runtime["admin_invite"]` und in `oauth` ab, solange es keinen
Betreiber gibt, und wirft ihn weg, sobald das erste Admin-Konto steht.

Danach ist die Tür zu: Jede weitere Discord-Anmeldung
ohne bekanntes Konto braucht einen Einladungscode, sonst `EinladungNoetig` (HTTP 403):

> Für dieses Discord-Konto gibt es hier noch kein Konto. Bitte zuerst einen Einladungscode des
> Betreibers einlösen.

Bedingung 2 ist wichtig: Auf einer Anlage, die schon Benutzerkonten hat, aber (etwa nach einem
Eingriff von Hand) keinen Admin mehr, könnte sich sonst ein Fremder das Betreiberkonto holen. In
diesem Fall bekommt nur ein **bestehendes** Konto die Rolle zurück – das erledigt
`erstanmeldung_als_admin()`, das auch bei jeder erneuten Anmeldung eines bekannten Kontos
mitläuft.

Ein gesperrtes Konto wird abgewiesen (`KontoGesperrt`, HTTP 403, mit dem Sperrgrund, falls
hinterlegt).

---

## 6. Wie das Programm auf dem PC an das Sitzungstoken kommt

Der Anspruch: **ohne Zwischenablage, ohne Abtippen**. Das Sitzungstoken darf nie in einer
Browseradresse und nie auf einer Webseite stehen.

### Weg A (bevorzugt) – Rückleitung auf 127.0.0.1

```
Programm                         Daemon (api.arcardia-nexus.de)            Discord
   |  lauscht auf 127.0.0.1:53127                                            |
   |-- GET /api/auth/discord/start?flow=lokal&port=53127 ------------------->|
   |<-- {url, state, expires_at} -------------------------------------------|
   |  öffnet url im Standardbrowser --------------------------------------->|
   |                                                Anmeldung + Zustimmung  |
   |<--- Browser wird auf http://127.0.0.1:53127/cloud-callback?code=…&state=… geleitet
   |  antwortet dem Browser mit einer kleinen deutschen Seite
   |  („Anmeldung abgeschlossen – dieses Fenster kann geschlossen werden.“)
   |-- POST /api/auth/discord/finish  {state, code} ----------------------->|
   |                                         oauth.complete() + anmelden()  |
   |<-- {token, expires_at, user, neues_konto, admin_vergeben} -------------|
```

Das Token läuft also nur über die TLS-Verbindung zwischen Programm und Daemon. Der Browser sieht
lediglich `code` und `state`; beide sind ohne das Anwendungsgeheimnis wertlos und nach dem ersten
Einlösen verbraucht. Der lokale Server des Programms sollte

* nur an `127.0.0.1` binden (nicht an `0.0.0.0`),
* nur `GET /cloud-callback` beantworten und sich danach sofort schließen,
* den Zustandswert gegen den selbst gestarteten Vorgang prüfen,
* nach 10 Minuten von selbst aufgeben.

### Weg B (Notweg) – Abholcode, wenn der Port nicht frei ist

Wenn kein lokaler Port geöffnet werden kann (Firewall, strenge Richtlinie), nimmt das Programm
`flow=app`. Dann landet der Browser beim Daemon, der Daemon legt die Sitzung an und hinterlegt das
Token unter einem kurzlebigen **Abholcode**; die Seite zeigt nur diesen Code (nie das Token). Das
Programm holt das Token damit ab.

```python
abholcode_anlegen(inhalt, *, claim_hash="", now=None) -> tuple[str, int]
abholcode_einloesen(code, *, geheimnis="", now=None) -> dict
abholcode_formatieren(code) / abholcode_normalisieren(raw)
abholcodes_aufraeumen(now=None) / abholungen_offen(now=None)
```

* 12 Zeichen aus `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (ohne I, O, 0, 1), angezeigt als
  `ABCD-EFGH-JKLM`; Groß-/Kleinschreibung und Trennzeichen sind beim Einlösen egal.
* Gültig `ABHOL_TTL_SECONDS = 300` (5 Minuten), **genau einmal** einlösbar, Vergleich zeitkonstant.
* Zusätzlicher Riegel gegen Raten: Das Programm erzeugt vor dem Start ein Zufallsgeheimnis, schickt
  nur `abhol_hash = sha256(geheimnis)` an `/start` und legt beim Abholen das Geheimnis selbst vor.
  Passt es nicht: HTTP 403. Nach `ABHOL_FEHLVERSUCHE_MAX = 10` Fehlversuchen innerhalb von
  5 Minuten werden **alle** offenen Abholungen verworfen (HTTP 429) – der Angreifer muss dann von
  vorn beginnen, und der Benutzer bekommt einen verständlichen Satz.
* Obergrenze `ABHOL_MAX = 200`.
* **Wichtig für den Daemon:** Wenn `/auth/discord/start` mit `abhol=<sha256>` gerufen wird, darf
  `mcsmd` dafür **keinen** Vorgang in `_LOGINS` anlegen. Sonst meldet `_login_setzen(state, …)` in
  der Rückleitung Erfolg, der Zweig mit `abholcode_anlegen` wird nie erreicht, und das fertige
  Sitzungstoken liegt bis zu zehn Minuten unabholbar im Arbeitsspeicher (`/claim` antwortet mit
  403, weil am Vorgang kein Geheimnis hängt).

### Weg C – Admin-Oberfläche

Der Browser bleibt im Spiel; der Daemon sollte das Sitzungstoken nach `complete()` + `anmelden()`
als Cookie setzen (`mcsm_sitzung`, `HttpOnly`, `Secure`, `SameSite=Lax`, Pfad `/`, Laufzeit wie
`expires_at`) und die Oberfläche ausliefern. Dafür muss die Prüfung in `mcsmd.py` neben
`Authorization: Bearer …` auch dieses Cookie annehmen (siehe Abschnitt 8).

---

## 7. Fehlerfälle

Alle Ausnahmen erben von `OAuthFehler` und tragen `.message` (fertiger deutscher Satz) und
`.status` (passender HTTP-Status) – der Daemon kann daraus unverändert
`{"error": "<Satz>"}` machen.

| Klasse | Status | Wann | Text (gekürzt) |
|---|---|---|---|
| `OAuthNichtEingerichtet` | 501 | Client-ID/Geheimnis fehlt, leer, unlesbar oder krumm | „… ist auf diesem Server noch nicht eingerichtet: Die Datei „discord_secret“ … fehlt …“ |
| `OAuthZustandFehler` | 400 | state fehlt, abgelaufen, doppelt, unbekannt | „Diese Anmeldung ist abgelaufen, wurde schon abgeschlossen oder gehört nicht zu diesem Server …“ |
| `OAuthAbgebrochen` | 400 | `error=access_denied` | „Die Anmeldung bei Discord wurde abgebrochen. Es wurden keine Daten übernommen.“ |
| `OAuthFehler` | 400 | `invalid_grant` (Code abgelaufen/zweimal benutzt) | „Der Anmeldecode von Discord ist nicht mehr gültig – er gilt nur wenige Minuten und nur einmal …“ |
| `OAuthFehler` | 500 | `invalid_client` | „Discord lehnt die Zugangsdaten dieses Servers ab. Bitte Client-ID und Anwendungsgeheimnis … prüfen.“ |
| `OAuthFehler` | 400 | `invalid_request` | „… Stimmt die Rückleitung in der Discord-App genau mit der hier eingetragenen überein?“ |
| `DiscordFehler` | 502 | keine Verbindung, Zeitüberschreitung, DNS | „Discord ist gerade nicht erreichbar (…). Bitte später erneut versuchen.“ |
| `DiscordFehler` | 503 | HTTP 429 von Discord | „Discord bremst uns gerade (zu viele Anfragen). Bitte in einer Minute erneut versuchen.“ |
| `DiscordFehler` | 502 | Antwort kein JSON, zu groß, ohne Kennung, falscher Umfang | „Die Antwort von Discord war nicht lesbar …“ |
| `EinladungNoetig` | 403 | kein Konto und kein Einladungscode | „Für dieses Discord-Konto gibt es hier noch kein Konto …“ |
| `KontoGesperrt` | 403 | Konto gesperrt | „Dieses Konto ist gesperrt. Bitte beim Betreiber melden. Grund: …“ |

Weitere Sicherungen: Zeitüberschreitung `HTTP_TIMEOUT = 10 s` je Anfrage, Antwortgrenze
`MAX_ANTWORT_BYTES = 64 KiB`, eigener `User-Agent`, `Accept: application/json`, Anmeldecode und
Token werden gegen ein Muster geprüft, bevor sie verschickt werden.

---

## 8. Was noch verdrahtet werden muss

Das Modul ist fertig, aber noch nicht angeschlossen – `mcsmd.py` und `core/*` der anderen Gruppe
wurden bewusst nicht angefasst. Nötig sind:

1. **Modul laden** – in `mcsmd.py` die Liste `_MODULES` um `"oauth"` erweitern und
   `oauth = _CORE["oauth"]` setzen (wie bei den anderen Modulen).
2. **Startmeldung** – beim Start einmal `log_event(oauth.startmeldung())`. Wirft nichts, meldet nur.
3. **Routen** (die drei 501-Platzhalter ersetzen bzw. ergänzen):

   | Methode | Pfad | Inhalt |
   |---|---|---|
   | `GET` | `/api/auth/discord/start` | `oauth.start(req.q("flow","admin"), invite_code=req.q("code"), port=req.qint("port"), abhol_hash=req.q("abhol"))` → JSON zurückgeben (ohne `auth`) |
   | `GET` | `/auth/discord/callback` | Rückleitung von Discord für `admin` **und** `app`: `oauth.complete(req.q("state"), req.q("code"), error=req.q("error"), error_description=req.q("error_description"))`, dann `oauth.anmelden(...)`; bei `flow == "admin"` Cookie setzen und HTML ausliefern, bei `flow == "app"` `oauth.abholcode_anlegen(anmeldung.as_dict(), claim_hash=ergebnis.abhol_hash)` und den Code als HTML-Seite zeigen |
   | `POST` | `/api/auth/discord/finish` | Weg A: `{state, code}` aus dem Programm → `complete()` + `anmelden()` → `as_dict()` (ohne `auth`) |
   | `POST` | `/api/auth/discord/claim` | Weg B: `{code, geheimnis}` → `oauth.abholcode_einloesen(...)` (ohne `auth`) |
   | `POST` | `/api/auth/discord/link` | angemeldet: `{state, code}` → `complete()` + `oauth.verknuepfen(req.me["id"], ergebnis.identitaet)` |

   **Wichtig:** nginx leitet alles unter `/` unverändert an `127.0.0.1:8765` weiter. Die in der
   Discord-App eingetragene Rückleitung heißt `/auth/discord/callback` – **ohne** `/api`. Die Route
   muss also genau auf diesem Pfad liegen (zusätzlich zur bestehenden unter `/api/...`, die für
   Programme bequem bleibt). Alternativ ein `location /auth/discord/callback { proxy_pass
   http://127.0.0.1:8765/api/auth/discord/callback; }` in nginx – die Route im Daemon ist die
   einfachere Lösung.
4. **Ausnahmen übersetzen** – im Fehlerbehandler von `mcsmd.py` `oauth.OAuthFehler` abfangen und in
   `ApiError(exc.message, exc.status)` verwandeln (analog zu `status_for_value_error`).
5. **Cookie für die Admin-Oberfläche** – in der Prüfung der Anmeldung neben
   `Authorization: Bearer …` auch `Cookie: mcsm_sitzung=…` annehmen. Beim Abmelden Cookie löschen
   (`Max-Age=0`) und `users.revoke_session` rufen.
6. **Aufräumen** – im stündlichen Durchgang `oauth.zustaende_aufraeumen()` und
   `oauth.abholcodes_aufraeumen()` rufen; beim Start `oauth.alles_leeren()`.
7. **Erstes Admin-Konto** – wenn `anmelden()` `admin_vergeben=True` liefert, sollte der Daemon den
   Einladungscode aus `_runtime["admin_invite"]` widerrufen und `ERSTER-ADMIN.txt` löschen (wie in
   `h_auth_invite`), damit es nur einen Weg zum ersten Betreiberkonto gibt.
8. **Übersichten** – `/api/health` und `/api/me` um `"discord": oauth.eingerichtet()` ergänzen,
   damit das Programm auf dem PC den Anmeldeknopf nur anzeigt, wenn er auch funktioniert.
9. **Ausliefern** – in `deploy.sh` die Dateiliste um `spec-oauth.md` ergänzen (`core/` und `tests/`
   werden komplett kopiert, `oauth.py` und `test_oauth.py` kommen also von selbst mit).
10. **Programm auf dem PC** – Weg A umsetzen (lokaler Zuhörer auf festem Port, Browser öffnen,
    `finish` aufrufen), Weg B als Notweg mit Eingabefeld für den Abholcode.

---

## 9. Nötige Änderungen in der Discord-App „MinecraftServerManager“

Im Entwicklerportal unter **OAuth2 → Redirects** müssen **alle** benutzten Rückleitungen
zeichengenau eingetragen sein:

```
https://admin.arcardia-nexus.de/auth/discord/callback      (vorhanden bzw. einzutragen)
https://api.arcardia-nexus.de/auth/discord/callback        (vorhanden bzw. einzutragen)
http://127.0.0.1:53127/cloud-callback                      ← NEU, für Weg A
http://127.0.0.1:53128/cloud-callback                      ← NEU, Ausweichport
http://127.0.0.1:53129/cloud-callback                      ← NEU, Ausweichport
```

* Discord erlaubt `http://` nur für die Rückschleife (`127.0.0.1`/`localhost`) – für alles andere
  ist `https://` Pflicht, das ist hier erfüllt.
* **Platzhalter für den Port gibt es nicht.** Deshalb drei feste Ports: Das Programm probiert
  53127, dann 53128, dann 53129, und weicht erst danach auf Weg B (`flow=app`) aus. Wer weitere
  Ports will, muss sie ebenfalls eintragen.
* Umfang bleibt `identify`. Keine Bot-Einladung, kein `email`, keine Privilegien
  (Intents) nötig.
* „Public Client“ darf **aus** bleiben: Der Code-Tausch läuft mit Geheimnis auf dem Root-Server,
  nie im Programm auf dem PC und nie im Browser.
* Wird das Geheimnis neu ausgestellt, danach `/srv/mcsm/data/discord_secret` austauschen
  (Besitzer `mcsm`, Rechte `600`) und den Dienst neu starten – oder `oauth.vergessen()` auslösen.
* Empfehlung aus der Prüfung auf dem Server: `chmod 600 /srv/mcsm/data/discord_client_id`
  (liegt derzeit auf `640`).

---

## 10. Selbsttests (72)

| Gruppe | Anzahl | Inhalt |
|---|---|---|
| `TestZugangsdaten` | 8 | Lesen und Merken, fehlende/leere/krumme Dateien, Geheimnis in keiner Ausgabe, Ordnerwechsel, Rückfall auf `MCSM_DATA` |
| `TestRueckleitungen` | 5 | feste Adressen, lokale Rückleitung, Portprüfung, unbekannter Weg |
| `TestStart` | 8 | Aufbau der Anmelde-URL, nur `identify`, Zufälligkeit des Zustandswerts, Port im lokalen Weg, gemerkter Einladungscode, `prompt`, Abholmerkmal |
| `TestZustand` | 7 | einmalige Verwendung, unbekannt, abgelaufen, krumme Werte, Aufräumen, Obergrenze, kein Netzverkehr bei falschem Zustand |
| `TestComplete` | 21 | glatter Durchlauf (mit Prüfung der gesendeten Felder), lokaler Weg, Bildadressen (PNG/GIF/Standard/alt), gefälschter Avatarwert, Abbruch, fehlender/krummer Code, `invalid_grant`, `invalid_client`, Netzausfall, Zeitüberschreitung, HTTP 429, unlesbare Antwort, Antwort ohne Token, falscher Umfang, Profil ohne Kennung, scheiternder Widerruf, Geheimnis nicht in Fehlermeldungen, zu große Antwort |
| `TestKonto` | 14 | erster Login wird Betreiber, zweiter braucht Einladung, Einlösen einer Einladung, abgelaufene Einladung, Wiedererkennen, Sperre, Rollen-Nachholen, Namenskollision und Emoji-Namen, Sitzungstoken, Verknüpfen, Prüfung der Kennung |
| `TestAbholcodes` | 9 | Anlegen und einmaliges Einlösen, Schreibweisen, Ablauf, krumme Codes, Programmgeheimnis, zu viele Fehlversuche, Aufräumen und Obergrenze |

Discord wird dabei nachgebaut: Die Tests ersetzen `oauth._urlopen` – die **einzige** Stelle, an der
das Modul `urllib.request.urlopen` benutzt – durch ein Objekt, das vorbereitete Antworten liefert
(`FakeAntwort`), Ausnahmen wirft (`urllib.error.HTTPError`, `URLError`, `TimeoutError`) und die
gesendeten Felder und Kopfzeilen mitschreibt. Die Zeit kommt überall über den Parameter `now`.

---

## 11. Grenzen und offene Punkte

* Zustandswerte und Abholcodes liegen nur im Arbeitsspeicher. Bei einem Neustart des Dienstes
  mitten in einer Anmeldung muss der Benutzer neu beginnen (mit klarer Meldung). Für eine Anlage
  mit mehreren Daemon-Prozessen müsste das in eine gemeinsame Ablage wandern – heute läuft genau
  ein Prozess, deshalb ist der Speicher richtig.
* Das Modul selbst bremst nur die Abholcodes. Ein allgemeiner Schutz gegen zu viele Anfragen
  (`/start`, `/finish`) gehört in `mcsmd.py` bzw. nginx.
* Ein Wechsel des Discord-Anzeigenamens wird nicht nachgezogen; der Name im Konto gehört dem
  Benutzer. Wenn das gewünscht ist, gehört es in die Admin-Oberfläche (Knopf „Namen von Discord
  übernehmen“).
* `avatar_url` wird **nicht** gespeichert, sondern bei jeder Anmeldung neu geliefert. Wer das Bild
  in der Oberfläche dauerhaft zeigen will, muss es im Konto ablegen – das wäre eine Ergänzung in
  `users.py` (`avatar_url`-Feld) und ist hier bewusst nicht vorgenommen worden.
