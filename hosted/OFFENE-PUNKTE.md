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

## 4. Schlafende Server: Wecken beim Beitritt, Ruhe bei Leerstand

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
