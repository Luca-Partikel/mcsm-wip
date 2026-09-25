# Offene Punkte der PC-Seite an den Root-Server (Konto-Einstellungen)

Nachtrag zu `hosted/spec-cloud-client.md`. Dort steht, was das Programm auf dem PC heute aufruft;
hier stehen die **drei Wege, die es noch nicht gibt**. Sie gehören zum Dialog
„Konto-Einstellungen“ (Zahnrad oben rechts im Bereich „Cloud“).

Die Oberfläche ist so gebaut, dass sie **ohne** diese Wege sauber weiterläuft: `core/cloud.py`
fängt die Antworten **404, 405 und 501** ab und liefert statt eines Fehlers

```json
{"supported": false, "hint": "<deutscher Satz, der erklärt, was hier fehlt>"}
```

Der Dialog zeigt den Satz als ruhigen Hinweis an und lässt alles andere unberührt. Sobald die
Gegenseite die Route anbietet, funktioniert der Knopf ohne Änderung an der Oberfläche.

---

## 1. Anzeigename ändern — `POST /api/me`

```
POST /api/me            Authorization: Bearer <token>
{"name": "Luca P."}
 -> 200 {"user": { … public_user … }}
 -> 400 {"error": "Bitte einen Namen mit 2 bis 32 Zeichen eintragen."}
```

* Der Name ist **nur Anzeige** – die Kennung (`id`) bleibt.
* Prüfung wie beim Anlegen des Kontos: 2 bis 32 Zeichen, keine Steuerzeichen, Ränder getrimmt.
* Antwort bitte mit dem vollständigen `public_user`, damit die Oberfläche nichts raten muss.
* Aufruf in `core/cloud.py`: `set_name(name)`, Route im Programm: `POST /api/cloud/account/name`.

Heute antwortet der Daemon auf `POST /api/me` mit 404. Die Oberfläche zeigt dann:
„Dieser Root-Server kann den Anzeigenamen noch nicht ändern. Bitte den Betreiber darum bitten.“

## 2. Einzelne Sitzung beenden — `POST /api/sessions/revoke`

`GET /api/sessions` gibt es schon und liefert je Sitzung `created_at`, `expires_at`,
`last_seen`, `note`, `valid` – aber **keine Kennung**. Zum Beenden fehlt der Gegenweg:

```
POST /api/sessions/revoke      Authorization: Bearer <token>
{"created_at": 1758800000}
 -> 200 {"ok": true}
 -> 404 {"error": "Diese Sitzung gibt es nicht (mehr)."}
```

* `created_at` ist als Schlüssel brauchbar (je Konto eindeutig genug), schöner wäre eine eigene
  `id` je Sitzung in `sessions_of()` – dann bitte `{"id": "…"}` statt `created_at`; die
  Oberfläche schickt beides, wenn das Feld da ist.
* Beendet werden darf **nur** eine Sitzung des eigenen Kontos.
* Beendet jemand die Sitzung, mit der er gerade arbeitet, ist das in Ordnung: der nächste Aufruf
  bekommt 401, und das Programm meldet sich sauber ab.
* Aufruf: `revoke_session(created_at)`, Route im Programm: `POST /api/cloud/session/revoke`.

## 3. Discord nachträglich verknüpfen — Rückweg für das PC-Programm

`GET /api/auth/discord/start?link=1&ziel=<seite>` gibt es. Für die Verwaltung im Browser passt
das: der Browser bringt sein Cookie mit und landet danach auf `<ziel>#verknuepft=1`.

Vom PC aus fehlt der letzte Schritt: Das Programm hat zwar das Token (es ruft die Route mit
`Authorization: Bearer …` auf und bekommt die `url`), kann die **Rückleitung aber nicht
auffangen** – es hat keine öffentliche Adresse. Heute behilft sich die Oberfläche mit einem Satz
(„Bestätige im Browser und klicke danach auf Aktualisieren“). Sauber wäre derselbe Bau wie bei
der Anmeldung:

```
GET /api/auth/discord/start?link=1&device=<PC-Name>
 -> {"url": "…", "state": "<32 Hex>", "poll_secret": "…", "expires_at": …}

GET /api/auth/discord/poll?state=…&secret=…
 -> {"pending": true} | {"verknuepft": true} | 403 {"error": "…"}
```

Dann meldet das Programm „Discord ist jetzt verknüpft“ von selbst, statt darum zu bitten,
nachzusehen.

---

## Was auf der PC-Seite schon fertig ist

| Zweck | `core/cloud.py` | Route im Programm |
|---|---|---|
| Sitzungen auflisten | `sessions()` | `GET /api/cloud/sessions` |
| Anzeigename ändern | `set_name(name)` | `POST /api/cloud/account/name` |
| Sitzung beenden | `revoke_session(created_at)` | `POST /api/cloud/session/revoke` |
| Discord verknüpfen | `discord_link_start(ziel)` | `POST /api/cloud/link/discord` |
| Erstanmeldung: Merkzettel | `login_poll()` → `{"needs_invite": true, …}` | `GET /api/cloud/login/poll` |
| Erstanmeldung: Code nachreichen | `login_register(code)` | `POST /api/cloud/login/register` |
| Anmeldung verwerfen | `login_abort()` | `POST /api/cloud/login/abort` |

Die beiden letzten Zeilen des Anmeldewegs sind gegen `POST /api/auth/discord/register` gebaut,
so wie `hosted/mcsmd.py` es heute anbietet: `{"zettel", "code", "note"}` →
`{"token", "expires_at", "user"}`, Fehler als `{"error": "deutscher Satz"}`. Der Merkzettel
bleibt **nur im Arbeitsspeicher** des Programms und verfällt mit `gueltig_sekunden`.
