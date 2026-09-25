# Offene Punkte aus der Nutzung (Stand 2026-09-25 abends)

Reihenfolge = Wichtigkeit. Alles vom Betreiber während der ersten echten Nutzung gemeldet.

## 1. Gehostete Server müssen sich ganz normal bedienen lassen

**Jetzt:** Klickt man in der Seitenleiste einen Server an, der auf dem Root liegt, zeigt die Seite die
lokale Kopie mit dem Hinweis „Die Kopie auf diesem PC ist gesperrt" – Starten, Konsole, Dateien,
Einstellungen sind tot. Nur „Zurück auf diesen PC holen" geht.

**Soll:** Dieselbe Seite mit denselben Reitern, nur arbeiten sie gegen den Root-Server:

* **Übersicht** – Zustand, Spielerzahl, Laufzeit, Arbeitsspeicher und TPS vom Root; als Adresse die
  Unterdomäne (`eutopia.arcardia-nexus.de`) mit Kopierknopf statt der FritzBox-Adresse; Start/Stopp
  wirken auf den Root. Ein ruhiger Hinweisstreifen „läuft auf dem Root-Server" mit Knopf zum
  Zurückholen genügt – keine Sperrmeldung.
* **Verbinden** – Unterdomäne für Java, Adresse und Port für Bedrock; kein FritzBox-/Portfreigabe-Teil,
  der dort sinnlos ist.
* **Spieler** – Freigabeliste, Sperren und Online-Liste über die Konsole des Root-Servers.
* **Konsole** – Konsole des Root-Servers, Befehle gehen dorthin.
* **Dateien** – Dateien auf dem Root: anzeigen, bearbeiten, hochladen, löschen (Plugins, Welten, Konfiguration).
* **Einstellungen** – `server.properties` und die Einstellungen der gehosteten Instanz; RAM nur im
  Rahmen des Passes, mit verständlicher Meldung, wenn das Budget nicht reicht.

Die lokale Kopie bleibt derweil unangetastet und wird nicht gestartet – das ist der einzige Unterschied,
und er gehört in einen Nebensatz, nicht in eine Warnbox.

Der Cloud-Bereich bleibt für Konto, Pässe, Übertragungen und den Zustand der Maschine.

## 2. Übertragung schneller machen

Gemessen: Leitung 55 Mbit/s (64 MB in 9,3 s) – 630 MB wären 1,5 Minuten. Es dauert deutlich länger.

Ursachen und Abhilfe:

1. **Nachladbares nicht übertragen.** An einem echten Server gemessen: `libraries` 79 MB, `cache` 58 MB,
   `versions` 28 MB, dazu `logs` – zusammen 56 % der Daten, die der Root in Sekunden selbst lädt.
   Weglassen und auf dem Root neu beschaffen.
2. **Verbindung wiederverwenden.** `urllib` baut für jede Anfrage eine neue TLS-Verbindung auf; bei
   92 % Dateien unter 1 MB kostet das mehr als die Daten. Eine Verbindung für die ganze Übertragung.
3. **Mehrere Dateien gleichzeitig** (3–4), damit die Leitung trotz Wartezeiten voll wird.
4. **Kleine Dateien bündeln** – ein Paket statt hunderter Einzelanfragen.

## 3. Übertragung zuverlässig im Hintergrund

Läuft bereits im Hintergrund-Thread (Reiterwechsel stört nicht) und nimmt nach einem Abriss an der
gemeldeten Stelle wieder auf – aber nur fünfmal, danach bleibt sie liegen. Fehlt:

* dauerhaftes Weiterversuchen mit wachsender Pause statt Aufgeben,
* beim Programmstart erkennen, dass eine Übertragung offen ist, und **von selbst** weitermachen,
* sichtbarer Fortschritt mit Geschwindigkeit und Restzeit, auch wenn man den Bereich verlässt.

## 4. Schlafende Server: Wecken beim Beitritt, Ruhe bei Leerstand – erledigt am 26.09.

**Nachgemessen am 26.09. auf dem Root**, mit eigenen Testinstanzen (Eutopia wurde nicht angefasst;
sie lief die ganze Zeit mit zwei Spielern weiter) und mit einem eigenen Minecraft-Handshake von
einem Rechner ausserhalb:

1. Eine gehostete Instanz hat den Port und die Route **ab der Anlage**: Port 25691, Eintrag
   `pruef-schlaefer` in `routes.json`, `started_at: 0` – ohne jeden Start.
2. Status-Ping auf `pruef-schlaefer.arcardia-nexus.de` bei ausgeschaltetem Server:
   `{"version":{"name":"Pruef Schlaefer"},"players":{"max":10,"online":0},`
   `"description":{"text":"Server ist ausgeschaltet – tritt bei, um ihn zu starten"}}` –
   keine Fehlerfarbe, die Spielerplätze kommen aus `server.properties`.
3. Beitritt weckt: Protokoll `„Pruef Schlaefer“ (1711805b0add) wurde durch einen Beitritt geweckt
   und gestartet.`, der Spieler bekommt „Der Server startet gerade …“. Drei Weckrufe (zwei
   gleichzeitig, einer vier Sekunden später) ergaben **genau einen** Start.
4. Ablehnung nennt den Grund: „Dein Pass erlaubt 1 gleichzeitig laufenden Server – es läuft bereits
   Pruef Schlaefer.“ / „Du hast gerade keinen gültigen Pass – …“ / „Der Server ist nicht vollständig
   installiert (server.jar fehlt).“ Ist der Dienst selbst aus, hört der Spieler „Dieser Server ist
   ausgeschaltet und lässt sich gerade nicht wecken. Bitte später noch einmal versuchen.“ – der
   Verteiler antwortet auf Pings trotzdem weiter.
5. Leerstand (Wartezeit zum Prüfen auf 1 Minute gesetzt): `save-all` → „Saved the game“ → sauberer
   Stopp, Protokoll `stand 1 Minuten ohne Spieler – die Welt wird gespeichert und der Server geht in
   den Ruhezustand`. Danach zählt das Konto 0 laufende Server und 0 MB Arbeitsspeicher, und die
   Startprüfung des **zweiten** Servers, die vorher am Kontingent scheiterte, sagt wieder „ok“.
6. Schonfrist greift: Start um 01:09:21, die Leerstands-Uhr begann erst um 01:14:31 (fünf Minuten
   später) und der Server schlief um 01:15:31 ein – trotz einer Wartezeit von einer Minute.

**Dabei behoben:** `ensure_port` gab auch Instanzen im Zustand `local_only` einen Port. Die liegen
nur auf dem PC, stehen nicht in `routes.json` – aber `MAX_INSTANCES_PER_USER` erlaubt 500 angelegte
Server je Konto, und der Java-Bereich hat rund 136 Ports für **alle** Konten. Ein einziges Konto
hätte den Bereich mit Servern leerräumen können, die es niemals hochlädt; danach bekäme kein
wirklich gehosteter Server mehr eine Adresse – genau der Fehler, um den es hier geht. Der Port
kommt jetzt nur für `hosted` und `uploading` (`mcsmd.PORT_ZUSTAENDE`). Nachgemessen: fünf angelegte
Server mit Herkunft „local“ belegen keinen Port, ein Premium-Server bekommt ihn sofort, und
`local_only → hosted → local_only` vergibt ihn und gibt ihn wieder zurück.

**Offen dazu:** Punkt 5 der Liste unten – die Oberfläche auf dem PC muss den Schalter
„Ruhezustand bei Leerstand“ und den Zustand „schläft“ noch anzeigen. Der Dienst liefert beides
(`hibernation`, `hibernation_minutes`, `sleeping` in `/api/servers`).

<details>
<summary>Ursprünglicher Befund und Bauplan</summary>

**Beobachtet:** `eutopia.arcardia-nexus.de` meldet „Diesen Server gibt es hier nicht". Ursache ist nicht
der Verteiler – der kennt drei Meldungen (unbekannt / läuft nicht / voll). Die Instanz steht als
`hosted` mit Marke `eutopia` in der Datenbank, hat aber `ports: {}`: Ports werden erst beim **ersten
Start** vergeben, und `routes.tabelle()` überspringt jeden Eintrag ohne Port. Der Server steht also gar
nicht erst in der Tabelle.

**Zu bauen:**

1. **Port beim Anlegen/Hochladen vergeben**, nicht erst beim Start. Dann steht jede gehostete Instanz
   ab der ersten Sekunde in `routes.json` und behält ihre Adresse dauerhaft.
2. **Ping auf einen schlafenden Server** beantwortet der Verteiler wie ein echter Server: Name,
   `0/<max>` Spieler, Beschreibung „Server ist ausgeschaltet – tritt bei, um ihn zu starten".
   Keine Fehlermeldung, in der Serverliste sieht er normal aus.
3. **Beitritt weckt ihn.** Der Verteiler meldet dem Dienst „jemand will auf <Instanz>" (Aufruf an
   127.0.0.1 mit gemeinsamem Geheimnis, nicht über das offene Netz). Der Dienst prüft Pass, Kontingent
   und Platz und startet. Der Spieler wird freundlich getrennt: „Der Server startet gerade. Bitte
   verbinde dich in etwa einer Minute noch einmal." Darf nicht gestartet werden, nennt die Nachricht
   den Grund im Klartext (kein gültiger Pass, Kontingent voll, Platte knapp).
4. **Ruhezustand bei Leerstand.** Ist ein gehosteter Server **15 Minuten** ohne Spieler, speichert er
   (`save-all`) und fährt sauber herunter – mit Ankündigung, falls doch noch jemand drauf ist.
   * Spielerzahl kommt aus `status.json` des Begleit-Plugins, ersatzweise über den Konsolenbefehl `list`.
   * Schonfrist nach dem Start (Vorschlag 5 Minuten), damit ein frisch geweckter Server nicht sofort
     wieder einschläft, bevor jemand drin ist.
   * Kein Ruhezustand, solange eine Übertragung läuft.
   * Ein schlafender Server zählt **nicht** als laufend – er gibt Platz und Arbeitsspeicher im Pass
     wieder frei. Beim Wecken wird das Kontingent erneut geprüft; reicht es nicht, sagt die Nachricht das.
5. **Im Programm abschaltbar.** Je Server eine Einstellung „Ruhezustand bei Leerstand" (an/aus) und die
   Wartezeit in Minuten. Standard an, 15 Minuten. In der Übersicht ein eigener Zustand „schläft"
   neben „läuft" und „gestoppt".

</details>

## 5. Eine einzige Serverliste – Cloud ist nur ein Merkmal, kein eigener Ort

**Beanstandung des Betreibers (mit Bildschirmfoto):** Die Seitenleiste trennt heute „MEINE SERVER" und
„ROOT-SERVER", und die Cloud-Seite listet Server gleich zweimal auf („Server auf dem Root-Server" und
„Server auf diesem PC"). Das ist doppelt und verwirrend.

**So soll es sein:**

* **Alle Server stehen unter „Meine Server"** – eine einzige Liste, egal wo sie gerade laufen.
  Jeder Eintrag trägt eine kleine Pille **„Cloud"** oder **„Lokal"**. Sonst nichts Besonderes.
* **Ein Klick öffnet immer dieselbe Serverseite** mit denselben Reitern und derselben Bedienung
  (siehe Punkt 1). Ob der Server auf dem PC oder auf dem Root läuft, ändert nur, wohin die Befehle gehen –
  und steht als Pille im Kopf der Seite.
* **Die Cloud-Seite behält nur die Übersicht:** Konto, Pass mit Restlaufzeit und Auslastung, Zustand des
  Root-Servers (Platte, Arbeitsspeicher, freie Adressen), laufende Übertragungen, Hinweise.
  **Keine Serverlisten mehr** – die stehen in der Seitenleiste.
* Der Wechsel zwischen den Orten (verschieben / zurückholen) gehört an den Server selbst, nicht in eine
  eigene Liste.


## 6. Xbox-Freunde-Modus auf dem Root – erledigt am 26.09.

**War:** Die Konsolenspieler des Betreibers kamen über den Xbox-Freunde-Modus herein. Beim Umzug
auf den Root kam die Anmeldung mit (`xbox/cache/cache.json` lag im Instanzordner), aber es lief
dort kein Bot – und in der mitgezogenen `xbox/config.yml` stand noch `ip: 94.114.30.114`, die
Heimadresse des Betreibers. Seine Freunde sahen den Server also nicht mehr.

**Jetzt:** `hosted/core/xbox.py` betreibt den Bot je Instanz auf dem Root, beworben werden
`arcardia-nexus.de:19132` und der Instanzname. Nachgemessen am 26.09. auf dem Root: Der Bot
meldet sich mit der mitgezogenen Anmeldung als `Shapzyy5977` an, die Xbox-Live-Sitzung steht, und
er startet und stoppt mit dem Server. Der Server wurde dafür nicht neu gestartet.

**Offen dazu:**

* `enforce-secure-profile=false` steht jetzt in `server.properties`, wirkt aber erst beim
  **nächsten** Start des Servers – bis dahin können Bedrock-Spieler weiter nicht im Chat
  schreiben. Dasselbe gilt für `server-icon.png` (Paper liest es beim Start).
* Die Oberfläche auf dem PC muss die Routen noch bedienen (Reiter „Xbox-Freunde-Modus“ für einen
  gehosteten Server, Anmelde-Code anzeigen). Die Felder heissen wie lokal.
