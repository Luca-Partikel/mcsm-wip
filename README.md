# Minecraft Server Manager (Windows, lokal)

Richtet Minecraft-Server auf diesem PC ein und verwaltet sie über eine Oberfläche –
**Bedrock Dedicated Server** (Xbox, PS4/PS5, Switch, Handy, Windows-App) oder
**Java (Paper) mit Crossplay** über Geyser + Floodgate (+ ViaVersion).

## Installieren / Starten

- **Weitergeben:** `dist\MinecraftServerManager-Setup.exe` (oder das ZIP) – doppelklicken, fertig.
  Installiert nach `%LocalAppData%\Programs\MinecraftServerManager`, legt Verknüpfungen auf Desktop und
  im Startmenü an und startet den Manager. Python wird beim ersten Start automatisch eingerichtet, falls es fehlt.
- **Aus diesem Ordner:** `Start.bat` doppelklicken. Läuft ohne Konsolenfenster; die Oberfläche öffnet sich als
  eigenes Fenster (Edge/Chrome im App-Modus). `Start-Debug.bat` zeigt zusätzlich die Konsole.
- **Beenden:** Knopf **„Manager beenden“** in der Oberfläche – stoppt alle Server sauber (Welten werden gespeichert).
  Fenster schließen lässt die Server weiterlaufen; `Start.bat` öffnet das Fenster wieder.
- **Pakete neu bauen:** `tools\Build-Installer.bat` (erzeugt `dist\…zip` und per IExpress `dist\…Setup.exe`).

Keine Abhängigkeiten außer Python-Standardbibliothek – kein `pip install`.

## Was der Manager automatisch macht

| Bestandteil | Quelle | Wann |
|---|---|---|
| Bedrock Dedicated Server | minecraft.net (offiziell) | beim Anlegen eines Bedrock-Servers |
| Visual C++ Laufzeit | Microsoft (nur falls sie fehlt, UAC-Abfrage) | vor dem ersten Bedrock-Start |
| Paper + Geyser + Floodgate + ViaVersion/ViaBackwards | PaperMC, GeyserMC, Hangar | beim Anlegen eines Java-Servers |
| Java-Laufzeit (Temurin 8/17/21/25, je nach Paper-Version) | api.adoptium.net | automatisch, auch nachträglich beim Start |
| MCXboxBroadcast (Xbox-Freunde-Modus) | GitHub-Release | beim Einrichten des Freunde-Modus |
| Python (nur bei der Weitergabe) | winget bzw. python.org | beim ersten Start von `Start.bat` |

Ordner: `servers/<id>/` (Server + Welt), `cache/` (Downloads), `runtime/` (Java), `data/` (Liste, Log).

## Oberfläche

- **Übersicht:** Status, Adressen, Kennzahlen, Welten mit Backup-Knopf, Xbox-Freunde-Modus, letzte Konsolenzeilen.
- **Verbinden:** Heimnetz-Adresse, öffentliche IP, Windows-Firewall-Regel per Knopf (UAC), FritzBox-Anleitung mit den
  konkreten Ports des Servers, Wege für Xbox/PS5/Switch.
- **Konsole:** Live-Ausgabe und Befehle.
- **Dateien:** Server-Ordner (nur Welten, Einstellungen, Plugins, Logs, Backups – Technik ausgeblendet);
  `server.properties` als Formular mit deutschen Erklärungen, YAML/JSON/TXT direkt editierbar (mit `.bak`-Sicherung).
- **Einstellungen:** Name, MOTD, Port(s), Spieler, RAM, Modus, Version/Neuinstallation, Löschen.

## Ports

- Bedrock (NetherNet, ab 1.26.51): **TCP** Hauptport (Standard 19132) **und** ein **UDP-Bereich** für Spieldaten
  (Standard 19142–19159, wird vom Manager gesetzt; ein Port je Spieler-Sitzung, der Bereich wächst mit
  `max-players`). LAN-Suche: UDP 7551. Für Freunde aus dem Internet muss der Server zusätzlich seine
  **öffentliche IPv4** kennen (`server-udp-ports` mit `IP:extern:intern`), sonst bietet er nur die LAN-Adresse an –
  der Manager fragt sie bei jedem Start per UPnP von der FritzBox ab, alternativ unter Einstellungen eintragen.
- Java: **TCP** (Standard 25565); bei Crossplay zusätzlich **UDP** für Geyser (Standard 19132).

## Xbox-Freunde-Modus

Ein Bot-Konto (empfohlen: Zweitkonto) meldet sich per MCXboxBroadcast bei Xbox Live an; alle Freunde dieses Kontos
sehen den Server unter **Freunde** und treten ohne DNS-Umstellung bei. Einrichtung komplett geführt in der
Oberfläche (Code-Anmeldung auf microsoft.com/link). Freunde außerhalb des Heimnetzes brauchen weiterhin die Portfreigabe.

## Updates

Der Manager prüft beim Start (und alle 6 Stunden) das neueste GitHub-Release des in `core/version.py` eingetragenen
Repositorys (`UPDATE_REPO`). Gibt es eine neuere Version, erscheint unten links **„Update auf vX.Y.Z verfügbar“**:
Changelog ansehen → „Jetzt aktualisieren“ → das Paket wird geladen (Prüfsumme aus `SHA256SUMS.txt`), die alte
Version nach `dataackup` gesichert, die Programmdateien ersetzt und der Manager neu gestartet. Welten und
Einstellungen bleiben unberührt. Im Entwicklungsordner (mit `.git`) sind Updates deaktiviert.

**Neue Version veröffentlichen:** `tools\Release.bat 1.6.0 --push` (setzt die Version, baut zur Kontrolle, erstellt
Commit + Tag `v1.6.0` und pusht). Die GitHub-Action `.github/workflows/release.yml` baut daraus das Release mit
ZIP, Setup.exe und `SHA256SUMS.txt`.

## Portfreigabe automatisch (FritzBox UPnP)

Ist in der FritzBox für diesen PC „Selbstständige Portfreigaben für dieses Gerät erlauben“ aktiv
(Heimnetz → Netzwerk → Netzwerkverbindungen → PC bearbeiten), legt der Manager unter **Verbinden → Schritt 4**
alle nötigen Freigaben per UPnP an (und entfernt sie wieder). In den Server-Einstellungen lässt sich das auch
automatisch bei jedem Start anfordern.

## Hinweise

- Bedrock-Spieler müssen im Spiel mit einem Microsoft-/Xbox-Konto angemeldet sein (`online-mode`).
- Bei DS-Lite-Anschlüssen (keine eigene IPv4) funktionieren Portfreigaben nicht; siehe Hilfe → FritzBox.
- Die Oberfläche ist nur von diesem PC aus erreichbar (127.0.0.1 + Sitzungs-Token).
