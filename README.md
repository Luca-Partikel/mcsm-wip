# Minecraft Server Manager (Windows, lokal)

Richtet Minecraft-Server auf diesem PC ein und verwaltet sie über eine Oberfläche – vier Server-Arten:
**Bedrock Dedicated Server** (Xbox, PS4/PS5, Switch, Handy, Windows-App), **Java Edition (nur Java, Paper)**,
**Java + Crossplay** (Paper + Geyser + Floodgate + ViaVersion) oder ein **Modpack von Modrinth**
(Fabric, NeoForge, Forge oder Quilt – Loader und Mods werden automatisch installiert).

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
| Modpack (.mrpack) + Mods | Modrinth (api.modrinth.com, cdn.modrinth.com) | beim Anlegen / Versionswechsel eines Modpack-Servers |
| Mod-Loader-Server (Fabric-Launcher, Quilt-Installer, NeoForge-/Forge-Installer) | meta.fabricmc.net, meta.quiltmc.org, maven.neoforged.net, maven.minecraftforge.net | beim Anlegen eines Modpack-Servers |
| Java-Laufzeit (Temurin 8/17/21/25, je nach Minecraft-Version) | api.adoptium.net | automatisch, auch nachträglich beim Start |
| MCXboxBroadcast (Xbox-Freunde-Modus) | GitHub-Release | beim Einrichten des Freunde-Modus |
| Python (nur bei der Weitergabe) | winget bzw. python.org | beim ersten Start von `Start.bat` |

Ordner: `servers/<id>/` (Server + Welt), `cache/` (Downloads), `runtime/` (Java), `data/` (Liste, Log).

## Oberfläche

- **Übersicht:** Status, Adressen, Kennzahlen, Welten mit Backup-Knopf, Xbox-Freunde-Modus, letzte Konsolenzeilen.
- **Verbinden:** Heimnetz-Adresse, öffentliche IP, Windows-Firewall-Regel per Knopf (UAC), FritzBox-Anleitung mit den
  konkreten Ports des Servers, Wege für Xbox/PS5/Switch.
- **Spieler:** Freigabeliste (Whitelist), gesperrte Spieler und wer gerade online ist – mit Hinzufügen,
  Entfernen, Sperren, Entsperren und Hinauswerfen. Nur auf Bedrock-Servern und Java-Servern mit Paper.
- **Konsole:** Live-Ausgabe und Befehle.
- **Dateien:** Server-Ordner (nur Welten, Einstellungen, Plugins, Logs, Backups – Technik ausgeblendet);
  `server.properties` als Formular mit deutschen Erklärungen, YAML/JSON/TXT direkt editierbar (mit `.bak`-Sicherung).
- **Einstellungen:** Name, MOTD, Port(s), Spieler, RAM, Modus, Version/Neuinstallation, Löschen.
- **Cloud:** Konto und Pässe auf dem Root-Server, gehostete Server mit Zustand, Adresse und Steuerung,
  Umzug in beide Richtungen (siehe unten).

## Ports

- Bedrock (NetherNet, ab 1.26.51): **TCP** Hauptport (Standard 19132) **und** ein **UDP-Bereich** für Spieldaten
  (Standard 19142–19159, wird vom Manager gesetzt; ein Port je Spieler-Sitzung, der Bereich wächst mit
  `max-players`). LAN-Suche: UDP 7551. Für Freunde aus dem Internet muss der Server zusätzlich seine
  **öffentliche IPv4** kennen (`server-udp-ports` mit `IP:extern:intern`), sonst bietet er nur die LAN-Adresse an –
  der Manager fragt sie bei jedem Start per UPnP von der FritzBox ab, alternativ unter Einstellungen eintragen.
- Java: **TCP** (Standard 25565); bei Crossplay zusätzlich **UDP** für Geyser (Standard 19132). Modpack-Server: nur TCP.

## Modpacks

`servers/<id>/` enthält je nach Loader `server.jar` (Fabric-Launcher), `quilt-server-launch.jar` + `server.jar` (Quilt)
oder `libraries/` + `user_jvm_args.txt` + `win_args.txt` (NeoForge/Forge, gestartet über `@`-Argumentdateien).
Die vom Pack installierten Dateien stehen in `modpack-index.json`; ein Versionswechsel (Einstellungen → Modpack)
entfernt genau diese und lädt die neue Version – Welt und eigene Konfiguration bleiben. Kein Geyser/Crossplay und
keine Bukkit-Plugins auf Mod-Loadern. API: `GET /api/modpacks/search?q=…&page=0`, `GET /api/modpacks/<project_id>/versions`.

## Spielerverwaltung (Tab „Spieler“)

Drei Karten: **Freigabeliste** (Schalter an/aus, Namen hinzufügen und entfernen), **Gesperrte Spieler**
(mit Grund, wer gesperrt hat und Ablauf) und **Gerade online** (Hinauswerfen, Sperren). Den Tab gibt es auf
Java-Servern mit Paper und auf Bedrock-Servern (dort heißt die Liste `allowlist.json`; Bedrock kennt keine
eigene Sperrliste – wer nicht mehr mitspielen soll, wird aus der Erlaubnisliste genommen). Modpack-Server
haben den Tab nicht.

- **Server läuft:** Alles geht als Konsolenbefehl an den Server (`whitelist`/`allowlist on|off|add|remove|reload`,
  `ban`, `pardon`, `kick`), greift also sofort; danach liest der Manager die Dateien neu. Den Namen schlägt
  der Server selbst nach.
- **Server gestoppt:** Der Manager schreibt `whitelist.json`, `banned-players.json` bzw. `allowlist.json`
  direkt – gültiges JSON, UTF-8, atomar über `.tmp` + Umbenennen, im Vanilla-Format (Zeiten als
  `yyyy-MM-dd HH:mm:ss Z`). Die UUID zu einem neuen Namen kommt aus `usercache.json` oder von
  `api.mojang.com`; nur bei gestopptem Server sind auch **Sperren auf Zeit** (1h bis 30d) möglich.
- **Wer online ist** liest der Manager aus `plugins/MCSMCompanion/status.json`, wenn dort eine Spielerliste
  steht; sonst schickt er `list` an die Konsole und wertet die Antwort aus.
- API: `GET /api/servers/<id>/players` und `POST /api/servers/<id>/players`
  mit `{"action": "whitelist_on|whitelist_off|whitelist_add|whitelist_remove|ban|unban|kick",
  "name": "…", "reason": "…", "duration": "1h|6h|1d|7d|30d"}`.

## Xbox-Freunde-Modus

Ein Bot-Konto (empfohlen: Zweitkonto) meldet sich per MCXboxBroadcast bei Xbox Live an; alle Freunde dieses Kontos
sehen den Server unter **Freunde** und treten ohne DNS-Umstellung bei. Einrichtung komplett geführt in der
Oberfläche (Code-Anmeldung auf microsoft.com/link). Freunde außerhalb des Heimnetzes brauchen weiterhin die Portfreigabe.

## Begleit-Plugin MCSMCompanion (Paper)

Auf jedem Paper-Server (Java-only und Crossplay) legt der Manager vor **jedem Start** `plugins/MCSMCompanion.jar`
aus `assets/` neu ab und schreibt die verwalteten Schlüssel in `plugins/MCSMCompanion/config.yml`
(Servername, Manager-Version, Sponsor-Zeile, Hardcore). Wird das Jar gelöscht, ist es beim nächsten
Start wieder da. Es bringt Chat-Format mit Farbcodes, Join-/Leave-/Todesmeldungen, Tablist „Sponsored by Novelnia“,
`/tpa` `/tp` `/gm` `/sethome` `/home` `/spawn` und meldet über `status.json`
verdächtige Plugins (gefälschte Spielerzahlen). Quelltext und Details: `plugin/README.md`, Bauen: `tools/build_plugin.py`.

**Angekündigter Stopp:** Auf Paper-Servern schickt der Knopf **„Stoppen“** zuerst `mcsmstop 10` – das Plugin
zählt im Spiel herunter (und überspringt den Countdown, wenn niemand online ist) und stoppt den Server dann
selbst. Antwortet der Server innerhalb von 15 Sekunden nicht (z. B. weil das Plugin fehlt und er
„Unknown command“ meldet), geht wie bisher `stop` hinterher. Bedrock und Modpacks stoppen unverändert.

**MCSM-Hardcore** (kein Vanilla-Hardcore, beim Erstellen oder in den Einstellungen wählbar, im Dashboard schaltbar,
im Spiel `/hardcore on|off` für OPs): Wer stirbt, wird ohne Todesbildschirm sofort Zuschauer an seinem Grab
(„R.I.P Name“ + Datum), im Chat steht „Name ist von uns gegangen (Koordinaten: …)“. Ein Mitspieler belebt ihn wieder,
indem er ein Totem der Unsterblichkeit am Grab rechtsklickt oder ablegt – der Tote spawnt dann dort im Überlebensmodus
(oder beim nächsten Login). Ein Wechsel per Befehl im Spiel wird nach dem Stopp in die Einstellungen übernommen.
API: `POST /api/servers/<id>/hardcore {"enabled": true|false}`.

## Cloud (Root-Server)

Neben den Servern auf diesem PC kann ein Server auf dem Root-Server laufen
(`https://api.arcardia-nexus.de`, Programm im Ordner `hosted/`). Gesteuert wird alles weiterhin von
hier – der Bereich **Cloud** in der Seitenleiste zeigt:

- **Nicht angemeldet:** was der Root-Server bringt (läuft rund um die Uhr, feste Adresse
  `mein-server.arcardia-nexus.de`, keine Portfreigabe nötig) und den Knopf **„Mit Discord anmelden“**
  (der Browser öffnet sich, das Programm holt die Sitzung ab). Ersatzweg: Einladungscode.
- **Angemeldet:** die eigenen **Pässe** mit Restzeit und Grenzen („2 Server gleichzeitig, zusammen 8 GB“),
  die **eigenen Server auf dem Root** mit Zustand, Adresse und Steuerung (Starten, Stoppen, Konsole,
  Befehle, Dateien) sowie die Server auf diesem PC.

**Umzug.** Bei jedem lokalen Server steht „⬆ Auf den Root-Server verschieben“: der ganze Serverordner
wird stückweise mit SHA256 je Datei übertragen (`backups/` bleibt hier). Danach ist die **lokale Kopie
gesperrt** – ein Server ist immer nur an einer Stelle spielbar. Umgekehrt holt „⬇ Zurück auf diesen PC
holen“ alles wieder her; der Ordner auf dem Root wird **erst gelöscht**, wenn hier jede Datei geprüft
angekommen ist. Abgebrochene Übertragungen setzen beim nächsten Versuch dort auf, wo sie stehen
geblieben sind.

**Abgelaufener Pass.** Dann stoppt der Root-Server die Server angekündigt und speichert die Welt; beim
nächsten Programmstart erscheint oben ein deutlicher Hinweis mit dem Knopf **„Jetzt zurückholen“**.

**Sicherheit.** Das Sitzungstoken liegt allein in `data/cloud-token.json` (Rechte nur für den
angemeldeten Windows-Benutzer) – es steht nie im Protokoll und nie in der Oberfläche. Antwortet der
Root-Server mit 401, meldet sich das Programm ab und fragt neu. Ohne Anmeldung ändert sich nichts:
die Server auf diesem PC laufen wie bisher.

Schnittstelle und Erwartungen an die API: `hosted/spec-cloud-client.md`.

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
