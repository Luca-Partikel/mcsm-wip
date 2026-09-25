# Gruppe „router“ – ein Server, eine Adresse, kein Port

Dateien dieser Gruppe:

* `hosted/router.py` – der Verteiler: lauscht auf TCP 25565, liest den Minecraft-Handshake,
  schlägt die Adresse in `routes.json` nach und reicht die Verbindung an `127.0.0.1:<port>` weiter
* `hosted/mcsm-router.service` – eigener systemd-Dienst (Benutzer `mcsm`, `Restart=always`, gehärtet)
* `hosted/tests/test_router.py` – 94 Selbsttests, nur Standardbibliothek, ohne Netz nach außen
* `hosted/spec-router.md` – dieses Papier

Geprüft mit Python 3.14 (PC) **und** Python 3.11.2 auf dem Root-Server – beide Male 94 von 94 grün,
keine Übersprünge. Dazu ein echter Lauf auf `45.132.89.224` (siehe Abschnitt 7).

**Der Spieler tippt nur noch `mein-server.arcardia-nexus.de` ein – keinen Port.**

---

## 1. Wie es funktioniert

```
Spieler gibt „mein-server.arcardia-nexus.de“ ein
        │  TCP auf Port 25565 (Java-Standard, deshalb tippt niemand einen Port)
        v
   mcsm-router  ──lesen──>  Handshake: Protokollversion, Adresse, Port, nächster Zustand
        │                   Adresse säubern (Nullbyte-Zusätze weg, klein, Punkt am Ende weg)
        │        ──suchen─>  /srv/mcsm/data/routes.json:  „mein-server“ -> 25567
        │
        ├── gefunden:  Verbindung zu 127.0.0.1:25567, **die gelesenen Bytes zuerst**
        │              unverändert hinüberschieben, danach in beide Richtungen durchreichen
        │
        └── unbekannt: deutsche Meldung „Diesen Server gibt es hier nicht“
                       (Zustand 1 als Statusantwort, Zustand 2 als Trennmeldung)
```

Wichtig ist das Puffern: Der Verteiler liest den Handshake, **verbraucht ihn aber nicht**. Alles,
was er gelesen hat (oft Handshake *und* Anmeldung in einem Häppchen), geht Byte für Byte unverändert
an den Zielserver. Der Server sieht genau den Datenstrom, den der Client geschickt hat – deshalb
funktionieren Mojang-Anmeldung, Verschlüsselung und `online-mode=true` ohne Zutun weiter. Der
Verteiler entschlüsselt nichts, ändert nichts und kennt keine Konten.

---

## 2. `router.py`

### Einstellungen (Umgebungsvariablen)

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `MCSM_ROUTER_BIND` | `""` (alle) | Adresse zum Lauschen; leer = IPv4 **und** IPv6 |
| `MCSM_ROUTER_PORT` | `25565` | Port zum Lauschen |
| `MCSM_ROUTES` | `/srv/mcsm/data/routes.json` | Zuordnungstabelle |
| `MCSM_ROUTER_DOMAIN` | `arcardia-nexus.de` | Basisdomain (davor steht die Marke des Servers) |
| `MCSM_ROUTER_TARGET` | `127.0.0.1` | Zielrechner der Weiterleitung |
| `MCSM_ROUTER_LIMIT` | `200` | gleichzeitige Verbindungen je Ziel |
| `MCSM_ROUTER_LIMIT_TOTAL` | `1000` | gleichzeitige Verbindungen insgesamt |
| `MCSM_ROUTER_TIMEOUT` | `5` | Zeitlimit für den Handshake in Sekunden |
| `MCSM_ROUTER_LOG` | `/var/log/mcsm/router.log` | Protokolldatei |
| `MCSM_ROUTER_LOG_STDOUT` | `1` | zusätzlich auf die Ausgabe (fürs Journal) |
| `MCSM_ROUTER_STATS` | `300` | Abstand der Zählerzeile in Sekunden, `0` = aus |

Aufrufe von Hand: `python3 router.py`, `python3 router.py --pruefen` (nur Einstellungen und
Tabelle anzeigen, nichts öffnen), `--version`, `--hilfe`.

### Protokoll-Zerleger

Zwei Ausnahmen trennen die beiden Fälle, die man beim Lesen eines Datenstroms auseinanderhalten muss:

* `Unvollstaendig` – die Bytes reichen noch nicht, **weiterlesen** (kein Fehler!)
* `Protokollfehler` – das kann kein Minecraft-Handshake sein, Verbindung schließen

| Funktion | Bedeutung |
|---|---|
| `lese_varint(daten, pos=0) -> (wert, danach)` | VarInt, höchstens 5 Byte, Ergebnis als **vorzeichenbehaftete** 32-Bit-Zahl (`\xff\xff\xff\xff\x0f` = -1) |
| `schreibe_varint(wert) -> bytes` | Gegenstück, auch für negative Zahlen |
| `lese_string(daten, pos=0, max_bytes=1024)` | VarInt-Länge **in Byte**, danach UTF-8 |
| `schreibe_string(text) -> bytes` | Gegenstück (Umlaute zählen als 2 Byte – „korrekt längencodiert“) |
| `paket(kennziffer, *teile) -> bytes` | Gesamtlänge, Kennziffer, Inhalt |
| `zerlege_handshake(puffer) -> Handshake` | das erste Paket eines Java-Clients |
| `zerlege_alten_ping(puffer) -> (adresse, port) \| None` | Ping vor 1.7; `None` = noch weiterlesen |

`Handshake` ist ein `NamedTuple`: `protokoll`, `adresse` (roh, **mit** allen Zusätzen), `port`,
`zustand`, `laenge` (so viele Byte des Puffers gehören zum Paket) sowie `ist_status` und
`ist_beitritt`.

Der Zerleger prüft streng:

* Paketlänge ≤ 0 oder > 4096 → `Protokollfehler` (der Handshake ist immer klein)
* Kennziffer ≠ `0x00` → `Protokollfehler`
* nächster Zustand muss 1 (Statusabfrage), 2 (Beitritt) oder 3 (Übergabe ab 1.20.5) sein
* ein Paket, das vollständig da ist, aber kürzer als sein Inhalt verspricht → `Protokollfehler`
  („Das Handshake-Paket ist kürzer als angekündigt.“), **nicht** `Unvollstaendig`
* erstes Byte `0xFE` → alter Ping, eigener Weg
* mehr als 8192 Byte ohne erkennbaren Handshake → Verbindung zu

Alter Ping: `0xFE` allein (ganz alt), `0xFE 0x01` (1.4/1.5) und die Fassung 1.6 mit
`MC|PingHost`-Zusatz, der die eingetippte Adresse mitbringt. Ist diese Adresse bekannt, wird die
Verbindung weitergereicht (der Server selbst kann besser antworten); sonst kommt die höfliche
Antwort als Kick-Paket `0xFF` mit UTF-16BE-Text.

### Antworten an den Spieler

| Funktion | Ergebnis |
|---|---|
| `status_paket(meldung, protokoll)` | Statusantwort (Zustand 1): JSON mit `description`, Spielerzahl 0, gespiegelte Protokollversion – so zeigt der Client die Meldung als gewöhnliche Serverbeschreibung an und nicht „Version passt nicht“ |
| `ping_paket(nutzlast)` | Laufzeitmessung: dieselben 8 Byte zurück |
| `trenn_paket(meldung)` | Trennmeldung (Zustand 2): JSON-Chat-Komponente, längencodiert |
| `alter_ping_paket(meldung)` | `0xFF` + Zeichenzahl + UTF-16BE (`§1\0127\01.6.4\0<meldung>\00\00`) |

Sichtbare Texte (Deutsch):

| Fall | Text |
|---|---|
| Adresse steht nicht in der Tabelle | **Diesen Server gibt es hier nicht** |
| Server ist aus / nimmt keine Verbindung an | **Dieser Server läuft gerade nicht** |
| Grenze erreicht | **Dieser Server ist gerade voll** |
| Name in der Statusantwort | Arcardia Nexus |

Die Meldungen nennen absichtlich **keine** anderen Server und keine Portnummern – ein Fremder
erfährt über die Adresse eines anderen nichts.

### Adressen

`bereinige_adresse(roh)` schneidet ab dem ersten Nullbyte ab (Forge `\0FML\0`, `\0FML2\0`,
`\0FML3\0`, Geyser/Floodgate und Proxy-Weiterleitungen hängen dort ihre Zusätze an), entfernt
Leerzeichen, einen Punkt am Ende, Klammern um eine IPv6-Adresse, macht alles klein und kürzt auf
253 Zeichen. **Weitergereicht wird trotzdem die unveränderte Adresse** – nur das Nachschlagen
benutzt die gesäuberte Fassung.

Nachgeschlagen wird in dieser Reihenfolge (`Routen.kandidaten`):

1. die ganze Adresse (`mc.beispiel.de` als Schlüssel ist also möglich)
2. der Teil vor der Basisdomain (`mein-server.arcardia-nexus.de` → `mein-server`) – der Normalfall
3. bei tieferen Namen zusätzlich die linkeste Marke (`welt.gruppe.arcardia-nexus.de` → `welt`)
4. `@standard`, aber **nur**, wenn gar keine eigene Unterdomain angesprochen wurde:
   nackte Basisdomain, eingetippte IP-Adresse, allgemeine Marke
   (`server`, `www`, `api`, `admin`, `mc`, `play`, `status`, `panel`) oder eine völlig fremde Adresse

Punkt 4 ist freiwillig: Steht `@standard` nicht in der Tabelle, kommt die Meldung „Diesen Server
gibt es hier nicht“. Ein **Tippfehler in der Unterdomain** landet nie beim Standardserver – sonst
würde ein Spieler unbemerkt auf einem fremden Server stehen.

### Zuordnungstabelle (`Routen`)

* Gelesen wird `/srv/mcsm/data/routes.json`, und zwar **nur, wenn sich etwas geändert hat**:
  geprüft werden Zeitstempel (`st_mtime_ns`), Größe und Inode, und dafür höchstens einmal je
  Sekunde nachgesehen (`pruef_abstand`). Zwischen zwei Änderungen wird die Datei nicht angefasst.
* Eine Änderung wirkt **ohne Neustart** und steht mit „neu: …“ / „weg: …“ im Protokoll.
* Ist die Datei beschädigt oder nicht lesbar, bleibt die **letzte gültige** Tabelle in Kraft und es
  gibt eine Klage im Protokoll (jede Klage nur einmal, nicht in Endlosschleife). Ein Tippfehler des
  Betreibers wirft niemanden aus dem Spiel.
* Einzelne kaputte Einträge werden übersprungen (mit Begründung), die übrigen gelten.
* Schlüssel, die mit `_` anfangen, sind Platz für Bemerkungen in der Datei.

### Zähler und Grenzen

`Zaehler` führt die offenen Verbindungen je Ziel und insgesamt (`RLock`-gesichert):

* je Ziel Standard **200**, im Eintrag mit `"grenze"` überschreibbar
* insgesamt **1000** (schützt den Rechner vor einer Flut von Threads)
* ist die Grenze erreicht: „Dieser Server ist gerade voll“, passend zum Zustand
* freigegeben wird im `finally`, also auch bei Fehlern und Abbrüchen

### Ablauf einer Verbindung (`Verteiler`)

1. Annehmen, `TCP_NODELAY`, eigener Thread (Daemon-Thread, damit ein Stopp nicht hängt).
2. Erstes Paket lesen, **höchstens 5 Sekunden** (`MCSM_ROUTER_TIMEOUT`) und höchstens 8192 Byte.
   Eine leere Verbindung (Portprüfung) ist kein Fehler und wird still geschlossen.
3. Adresse nachschlagen. Unbekannt → deutsche Meldung, fertig.
4. Zeigt ein Eintrag auf den **eigenen** Port, wird nicht weitergeleitet (das wäre eine Schleife,
   bis keine Sockets mehr übrig sind) – der Spieler bekommt „Dieser Server läuft gerade nicht“ und
   der Betreiber eine deutliche Zeile im Protokoll.
5. Platz beim Zähler nehmen, zum Ziel verbinden (5 s Frist). Klappt das nicht: „Dieser Server läuft
   gerade nicht“.
6. **Gepufferte Bytes zuerst**, unverändert. Danach zwei Richtungen: der Rückweg
   (Ziel → Spieler) in einem eigenen Thread, der Hinweg (Spieler → Ziel) im vorhandenen.
7. Endet eine Richtung, bekommt die Gegenseite ein `shutdown(SHUT_WR)` – so sieht der Server ein
   ordentliches Verbindungsende. Nach `ABKLINGZEIT` (10 s) werden beide Sockets im `finally`
   geschlossen; ein noch wartender Rückweg-Thread läuft dadurch auf und beendet sich.
   **Keine Zombie-Sockets**, kein Thread ohne Ende.

Statusabfragen auf eine **unbekannte** Adresse beantwortet der Verteiler selbst: Statusanfrage
kurz nachlesen, Statusantwort schicken, danach die Laufzeitmessung (Paket `0x01`) mit denselben
8 Byte beantworten, dann `shutdown` und zu. So sieht der Spieler in der Serverliste die deutsche
Meldung statt „Verbindung fehlgeschlagen“.

### Protokoll und Datensparsamkeit

Format wie beim Daemon: `2026-09-25 05:01:59 [bereich] Text`. Bereiche: `verteiler` (Dienst,
Tabelle), `weiter` (Weiterleitung), `abgewiesen` (unbekannt, voll, Server aus), `fehler`, `zahlen`.

```
[verteiler] Zuordnungstabelle gelesen (3 Einträge; neu: test-welt).
[weiter] „test-welt.arcardia-nexus.de“ -> 127.0.0.1:25567 [beitritt] klient=8be20327 instanz=pruefung1 offen=1
[abgewiesen] Unbekannte Adresse „gibt-es-nicht.arcardia-nexus.de“ von a888b8c8 (zustand=2, protokoll=767).
[fehler] Zeitlimit: ::ffff:94.31.94.42 hat in 5 s kein vollständiges erstes Paket geschickt (1 Byte).
[zahlen] angenommen=23 weitergeleitet=14 unbekannt=3 abgewiesen=0 aus=1 alter-ping=1 fehler=4 offen=0 (keine)
```

`klient=8be20327` ist eine Kennung: die ersten 8 Hexzeichen von `sha256(salz + ip)` mit einem
**je Prozessstart neu gewürfelten** Salz. Sie erlaubt es, die Zeilen einer Sitzung zusammenzubringen,
lässt sich aber nicht auf eine IP zurückrechnen und überlebt keinen Neustart. **Im Klartext steht
eine Spieler-IP nur in `[fehler]`-Zeilen** (Zeitlimit, kein Minecraft, abgebrochene Verbindung) –
dort braucht der Betreiber sie, um jemanden aussperren zu können.

### IPv4 und IPv6

Ohne `MCSM_ROUTER_BIND` wird **ein** Socket auf `::` ohne `IPV6_V6ONLY` geöffnet – der nimmt unter
Linux beides an (im Protokoll: „Es wird auf :::25565 gelauscht (IPv4 und IPv6)“, IPv4-Gegenstellen
erscheinen als `::ffff:…`). Geht das nicht, werden zwei getrennte Sockets versucht (IPv6 und IPv4)
und beide mit `selectors` bedient. Ziel der Weiterleitung ist immer `127.0.0.1:<port>`.

---

## 3. `routes.json`

```json
{
  "_bemerkung": "wird vom Daemon geschrieben - nicht von Hand aendern",
  "mein-server": {"port": 25567, "instanz": "a1b2c3d4e5f6"},
  "zweite-welt": {"port": 25568, "instanz": "0f1e2d3c4b5a", "grenze": 50},
  "@standard":   {"port": 25567, "instanz": "a1b2c3d4e5f6"}
}
```

| Feld | Pflicht | Bedeutung |
|---|---|---|
| Schlüssel | ja | Marke vor der Basisdomain (`mein-server`), ein ganzer Name (`mc.beispiel.de`) oder `@standard`. Klein, Buchstaben/Ziffern/Bindestrich/Punkt, höchstens 253 Zeichen |
| `port` | ja | interner TCP-Port auf 127.0.0.1, 1–65535 (ganze Zahl; `"port": 25567` als Text ist erlaubt) |
| `instanz` | nein | Kennung der Serverinstanz – steht nur im Protokoll |
| `grenze` | nein | eigene Grenze für gleichzeitige Verbindungen (0/fehlt = Vorgabe 200) |

Auch erlaubt: `{"mein-server": 25567}` (nur der Port) und eine Hülle
`{"routen": { … }}` bzw. `{"routes": { … }}`.

Rechte: Besitzer `mcsm`, `0600` genügt (der Verteiler läuft als `mcsm`). Geschrieben wird **atomar**
(`.tmp` + `os.replace`) – der Verteiler liest sonst irgendwann eine halbe Datei.

---

## 4. `mcsm-router.service`

Eigener Dienst, absichtlich **ohne** `Requires=mcsm.service`: Fällt der Daemon aus oder wird er
neu gestartet, spielen die laufenden Server ungestört weiter.

* `User=mcsm`, `Restart=always`, `RestartSec=3s`, `TimeoutStopSec=20`
* `ExecStartPre=… router.py --pruefen` – die Tabelle steht beim Start im Journal
* alle Einstellungen als `Environment=`-Zeilen, damit man sie ohne Codeänderung drehen kann
* Härtung: `ProtectSystem=strict` mit `ReadWritePaths=/var/log/mcsm` (mehr braucht er nicht),
  `ReadOnlyPaths=/srv/mcsm/data`, `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`,
  `ProtectProc=invisible`, `ProcSubset=pid`, `RestrictAddressFamilies=AF_INET AF_INET6`,
  `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute=yes` (kein JIT im Spiel – anders als
  beim Daemon, der Java startet), `CapabilityBoundingSet=` leer (25565 ist > 1024, es braucht keine
  Rechte), `LimitNOFILE=65536`
* `InaccessiblePaths=-/srv/mcsm/data/discord_secret` sowie `sessions.json`, `users.json`,
  `invites.json`: Der Verteiler kennt keine Konten – die Geheimnisse werden ihm ausdrücklich
  entzogen, obwohl er als derselbe Benutzer läuft.

`systemd-analyze verify /etc/systemd/system/mcsm-router.service` läuft ohne Beanstandung.

---

## 5. Selbsttests (`hosted/tests/test_router.py`, 94 Stück)

| Bereich | Was geprüft wird |
|---|---|
| VarInt | Hin und zurück für 0/1/127/128/255/2097151/2147483647, die bekannten Byte-Folgen aus der Protokollbeschreibung, negative Zahlen (5 Byte), Lesen mit Versatz, abgeschnitten → `Unvollstaendig`, 6 Byte → `Protokollfehler`, überschüssige Bits im 5. Byte |
| Strings | Hin und zurück (leer, 255 Zeichen, Umlaute), abgeschnitten, unsinnige Länge, negative Länge, kein UTF-8 |
| Handshake | Normalfall; **Adresslängen 1…253** (auch der Grenzfall, ab dem die Längenangabe zwei Byte braucht); Protokollversion und Port am Anschlag; Zustand 3; **jedes** Präfix des Pakets ergibt `Unvollstaendig`; überschüssige Bytes bleiben liegen; Nullbyte-Zusätze (FML/FML2/FML3/Proxy); falsche Kennziffer; falscher Zustand; Länge 0; Länge zu groß; „kürzer als angekündigt“; `0xFE`; Müll |
| Alter Ping | ganz alt, 1.4/1.5, 1.6 mit `MC|PingHost` (jedes Präfix → weiterlesen), fremder Kanal, Antwort wieder zerlegbar (Zeichenzahl = Textlänge) |
| Antworten | Statusantwort und Trennmeldung lassen sich zurücklesen, Umlaute werden in **Byte** gezählt, Laufzeitmessung |
| Adressen | Säubern (Groß/klein, Punkt, Nullbyte, Klammern, `None`, Kürzen), IP erkennen, Hostname prüfen, Schlüssel prüfen, Port prüfen (`1.5` und `True` sind keine Ports) |
| Tabelle | Nachschlagen über Marke/ganzen Namen/`@standard`; Port als Zahl; ohne Unterdomain mit und ohne Standardserver; Tippfehler landet **nicht** beim Standard; eigener Eintrag schlägt reservierte Marke; tiefere Unterdomain; fehlende Datei; leere Datei; kaputte Einträge einzeln übersprungen; kaputtes JSON und falscher Aufbau behalten die alte Tabelle; Hülle `routen`; Änderung wirkt sofort; **50 Anfragen lesen die Datei nur einmal**; eigene Grenze |
| Zähler | Grenze je Ziel, Gesamtgrenze, Freigeben unter Null |
| Weiterleitung (echte Sockets) | Beitritt und Statusabfrage werden **Byte für Byte** durchgereicht; Handshake und Anmeldung in einem Häppchen; Handshake byteweise; ~100 KB in beide Richtungen; Nullbyte-Zusatz; Großschreibung; geänderte Tabelle wirkt sofort; unbekannt bei Zustand 1 und 2; Statusabfrage ohne Anfragepaket; nackte Domain mit und ohne Standard; Ziel aus (Zustand 1 und 2); Grenze je Ziel und Freigabe danach; Eintrag zeigt auf den Verteiler; Zeitlimit; kein Minecraft; leere Verbindung; alter Ping (abweisen und weiterreichen); Zähler; **keine IP im Protokoll**; Kennung stabil aber nicht rückrechenbar; 25 Verbindungen gleichzeitig ohne Rest |
| Einstellungen | Zahlen werden begrenzt, Komma erlaubt, Pfade, `--pruefen` und `--version` laufen ohne Netz |

Aufruf: `python -m unittest discover -s hosted/tests` (oder gezielt
`python -m unittest hosted.tests.test_router`).

---

## 6. Was noch verdrahtet werden muss

Diese Gruppe legt nur neue Dateien an. `mcsmd.py`, `core/*.py` und `deploy.sh` wurden **nicht**
angefasst (dort arbeitet ein anderer Durchgang). Damit der Verteiler im Betrieb wirkt, fehlt noch:

1. **Port 25565 nicht doppelt vergeben.** `core/ports.py` vergibt den Java-Bereich ab **25565** –
   genau dem Port des Verteilers. Solange der Verteiler läuft, lehnt der Bind-Versuch von
   `PortPool(probe=True)` den Port von selbst ab; verlässlich ist das aber erst mit einer
   Buchung. Vorschlag beim Start des Daemons:

   ```python
   POOL.reserve("@verteiler", ports.JAVA_POOL, 25565)   # der Verteiler sitzt hier
   ```

   Alternativ den Bereich in `ports.POOLS` auf 25566–25700 legen. `instances.PORT_RANGES`
   (25565–25664) müsste dann mitwandern.
2. **`routes.json` schreiben.** Der Daemon ist die einzige Stelle, die weiß, welcher Server auf
   welchem Port läuft. Nötig wäre eine kleine Datei `core/routes.py` (oder eine Funktion in
   `mcsmd.py`) mit:
   * `marke(instanz) -> str`: aus dem Servernamen eine DNS-Marke machen – klein, Umlaute als
     `ae/oe/ue/ss`, Leerzeichen und Punkte zu `-`, nur `a-z0-9-`, höchstens 63 Zeichen, nicht mit
     `-` beginnen/enden. **Eindeutig über alle Konten** (sonst landet ein Spieler beim fremden
     Server): bei Dopplung die Instanzkennung anhängen, als Notnagel die Kennung allein. Die Marke
     gehört in den Instanz-Datensatz (`"marke"`), damit sie sich beim Umbenennen nicht ändert und
     Spieler eine Adresse behalten. Reserviert bleiben `server`, `www`, `api`, `admin`, `mc`,
     `play`, `status`, `panel`.
   * **Frei gewordene Marken kommen nicht sofort wieder in den Topf.** Wird eine Instanz gelöscht
     oder zurückgeholt und vergessen, verschwindet ihr Datensatz – und mit ihm die Marke. Ohne
     Schonfrist bekäme sie der Nächste, dessen Servername dieselbe Marke ergibt: Alle Spieler, die
     `familien-welt.arcardia-nexus.de` im Serverbrowser stehen haben, landeten bei einem
     **fremden** Server. Deshalb merkt `core/routes.py` aufgegebene Marken mit Zeitpunkt und Konto
     in `/srv/mcsm/data/marken.json` (`SPERRE_TAGE = 30`); `gesperrte_marken()` fließt in
     `marke_fuer()` ein, `marke_aufgeben()` / `marke_freigeben()` / `sperrliste_aufraeumen()`
     pflegen die Liste (letzteres im stündlichen Aufräumen). Dem **eigenen** Konto steht seine
     Marke jederzeit wieder offen. Der Verteiler liest diese Datei nicht.
   * `schreibe_routen()`: alle Instanzen mit Zustand `hosted`, Art **java** und einem Java-Port
     als `{marke: {"port": …, "instanz": …}}` **atomar** nach `/srv/mcsm/data/routes.json`
     (`.tmp` + `os.replace`, `0600`, Besitzer `mcsm`).
   * Gerufen nach: Anlegen, Umbenennen, Portwechsel, Zustandswechsel, Start, Stopp, Löschen,
     Pass-Ablauf – und einmal beim Start des Daemons (Wiederherstellen).
   * Gestoppte Instanzen dürfen drinbleiben: der Verteiler meldet dann „Dieser Server läuft gerade
     nicht“, was für den Spieler die bessere Auskunft ist als „gibt es hier nicht“.
3. **Adresse im Programm anzeigen.** `GET /api/servers` und `GET /api/me` sollten die fertige
   Adresse mitliefern (`"adresse": "mein-server.arcardia-nexus.de"`), damit das PC-Programm sie
   zum Kopieren anbietet. Die Basisdomain gehört dafür in die Einstellungen des Daemons
   (`MCSM_DOMAIN`), damit beide Dienste denselben Wert benutzen.
4. **`deploy.sh` erweitern** (drei Zeilen):
   * `install -o root -g root -m 0755 "$SRC/router.py" "$APP/router.py"`
   * `install -o root -g root -m 0644 "$SRC/mcsm-router.service" /etc/systemd/system/`
   * `systemctl daemon-reload && systemctl enable --now mcsm-router.service` (und im Hinweistext
     am Ende: TCP 25565 muss offen sein – das ist er schon)
   * dazu passend `/etc/logrotate.d/mcsm` um `router.log` ergänzen:

     ```
     /var/log/mcsm/*.log {
         weekly
         rotate 8
         compress
         missingok
         notifempty
         create 0640 mcsm mcsm
         copytruncate
     }
     ```
     (`copytruncate`, weil der Verteiler die Datei offen hält.)
5. **Firewall enger ziehen (freiwillig).** Mit dem Verteiler braucht von außen nur noch TCP 25565
   offen zu sein; die Java-Ports 25566–25700 könnten zugemacht werden. Das ist zusätzlich ein
   Schutz gegen das Umgehen der Grenzen. **Bedrock bleibt davon unberührt** (siehe Punkt 7.2).

---

## 7. Der Lauf auf dem Root-Server

Geprüft wurde am 25.09. auf `45.132.89.224` – **ohne** Eingriff in den laufenden Betrieb: der
Verteiler lief zeitweilig aus `/tmp/router-test` als Benutzer `mcsm` mit einer eigenen Tabelle
(`/tmp/router-test/routes.json`), als Ziel ein einfacher TCP-Horcher auf `127.0.0.1:25567`, der die
empfangenen Bytes zurückmeldet. Nach dem Lauf wurden beide Prozesse beendet und der Ordner gelöscht;
`/opt/mcsm`, `/srv/mcsm` und die Dienste sind unverändert.

| Prüfung | Ergebnis |
|---|---|
| 94 Selbsttests mit Python 3.11.2 | alle grün, keine Übersprünge (15,3 s) |
| Start | `Es wird auf :::25565 gelauscht (IPv4 und IPv6)`, Tabelle mit 2 Einträgen gelesen |
| 13 Prüfungen **vom Server aus** (`127.0.0.1:25565`) | alle in Ordnung |
| 13 Prüfungen **vom PC aus** über `test-welt.arcardia-nexus.de:25565` | alle in Ordnung – Wildcard-DNS und Firewall stimmen |
| Beitritt | 51 Byte Handshake + Anmeldung kamen unverändert beim Testziel an und zurück |
| Forge-Zusatz, Großschreibung, Punkt am Ende | weitergeleitet |
| Unbekannt, Zustand 2 | `{"text":"Diesen Server gibt es hier nicht","color":"red"}` |
| Unbekannt, Zustand 1 | Statusantwort mit gespiegelter Protokollversion 767, danach die Laufzeitmessung beantwortet |
| Nackte Domain ohne `@standard` | „Diesen Server gibt es hier nicht“ |
| Alter Ping `FE 01` | `§1\0127\01.6.4\0Diesen Server gibt es hier nicht\00\00` |
| Zeitlimit | halber Handshake → nach 5,0 s zu, keine Antwort |
| Fremde Bytes (`02 99 01`) | zu, Protokoll: „spricht kein Minecraft“ |
| Eintrag zeigt auf einen Port ohne Horcher | „Dieser Server läuft gerade nicht“ |
| Eintrag zeigt auf 25565 (Schleife) | abgefangen, Meldung an den Spieler, Klage im Protokoll |
| Tabelle im Betrieb geändert | „Zuordnungstabelle gelesen (3 Einträge; neu: neu-dazu)“, der neue Eintrag wirkte sofort – ohne Neustart |
| IPv6 (`::1`) | weitergeleitet |
| 10 Verbindungen gleichzeitig | alle mit unveränderten Bytes, `offen=10`, danach alles frei |
| `SIGTERM` | `Signal 15 – der Verteiler hört auf.` + Zählerzeile, Port sofort frei |
| Protokoll | in allen `[weiter]`-Zeilen nur `klient=<kennung>`; die Klartext-IP stand ausschließlich in den beiden `[fehler]`-Zeilen |

So lässt sich der Lauf wiederholen (Testziel und Verteiler von Hand, nichts installiert):

```sh
python3 /tmp/router-test/echo_ziel.py 25567 &                 # Testserver: schickt alles zurueck
su -s /bin/sh mcsm -c "MCSM_ROUTES=/tmp/router-test/routes.json \
    MCSM_ROUTER_LOG=/tmp/router-test/router.log MCSM_ROUTER_PORT=25565 \
    python3 /tmp/router-test/router.py &"
python3 /tmp/router-test/probe_client.py 127.0.0.1 25565      # 13 Pruefungen
```

---

## 8. Offene Punkte

1. **Der Server sieht `127.0.0.1` als Spieler-IP.** Ein reiner TCP-Verteiler kann das nicht ändern,
   ohne den Datenstrom anzufassen. Folgen: `/ban-ip` sperrt den Verteiler (also alle),
   IP-basierte Plugins und die Paper-Einstellung `player-ip-throttle` wirken nicht mehr, und in
   `latest.log` steht immer `/127.0.0.1`. Zwei Wege, falls das störend wird:
   * **HAProxy-Protokoll** (`proxy-protocol: true` in `config/paper-global.yml`, BDS kann es nicht):
     Der Verteiler schickt vor den gepufferten Bytes eine Kopfzeile mit der echten Gegenstelle.
     Das ist wenig Code (v1 ist eine Textzeile) und wäre rückwärtskompatibel abschaltbar
     (`MCSM_ROUTER_PROXY=1`). **Wichtig:** Der Server darf dann nur noch vom Verteiler erreichbar
     sein, sonst kann jeder eine falsche IP behaupten – also gleichzeitig die Java-Ports in der
     Firewall zumachen (Punkt 6.5) und den Server auf `127.0.0.1` binden.
   * Sperren über das Begleit-Plugin statt über IP-Sperren regeln.
   Bis dahin gilt: Der Verteiler zählt Verbindungen je Ziel, das fängt das Gröbste ab.
2. **Bedrock läuft nicht über den Verteiler.** Die Bedrock-Edition spricht UDP (RakNet/NetherNet);
   dort gibt es keinen Handshake mit Adresse, aus dem sich das Ziel ablesen ließe, und der Client
   fragt den Port beim Verbinden nicht per Namen. Möglichkeiten für später: je Instanz ein eigener
   UDP-Port (wie jetzt, der Spieler tippt einen Port ein) oder ein DNS-Eintrag je Server
   (`_minecraft._udp`-SRV wird von Bedrock nicht gelesen – also eher „Adresse plus Port anzeigen“).
   Für **Geyser** gilt dasselbe: Der Geyser-UDP-Port bleibt wie bisher direkt erreichbar.
3. **Keine Bremse je IP.** Begrenzt wird nach Ziel und Gesamtzahl, nicht nach Herkunft. Wer viele
   halbe Handshakes schickt, belegt bis zu 5 s je Verbindung einen Thread. Eine kleine Sperrliste
   („mehr als N Fehler in einer Minute → 10 Minuten ignorieren“) wäre der nächste Schritt; sie
   müsste IPs kurzzeitig im Arbeitsspeicher halten, was zur Datensparsamkeit passen muss.
4. **Ein Thread je Verbindung.** Bei den erwarteten Spielerzahlen (wenige Server, 13 GB RAM) ist das
   genau richtig und viel einfacher als `selectors` für alle Richtungen. Die Gesamtgrenze von 1000
   ist die Bremse; darüber lehnt der Verteiler höflich ab, statt dem Rechner die Threads auszugehen.
5. **`@standard` ist noch nicht vergeben.** Sobald es einen „Hauptserver“ gibt, wäre er die freundliche
   Antwort auf `arcardia-nexus.de`. Solange keiner eingetragen ist, bekommt der Spieler dort die
   Meldung „Diesen Server gibt es hier nicht“.
6. **Die Statusantwort ist bewusst karg** (Name „Arcardia Nexus“, 0 von 0 Spielern). Ein Symbol
   (`favicon`) und ein freundlicherer Text ließen sich leicht nachrüsten, sobald es ein Bild gibt.
