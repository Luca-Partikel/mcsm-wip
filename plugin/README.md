# MCSMCompanion – Begleit-Plugin des Minecraft Server Managers

Ein Paper-Plugin, das der Manager vor jedem Start automatisch in jeden Java-Server kopiert
(`plugins/MCSMCompanion.jar`). Es bringt ein schöneres Chat-, Join-, Leave- und Todesformat,
eine Tablist mit Sponsor-Zeile, die wichtigsten Essentials-Befehle (TPA, Teleport, Spielmodus,
Homes, Spawn) und einen eigenen Hardcore-Modus mit Gräbern und
Wiederbelebung per Totem. Außerdem meldet es dem Manager über
`status.json`, welche Plugins laufen und ob eines davon die Spielerzahl, den Ping oder die
MOTD manipulieren könnte.

* Paket: `de.mcsm.companion`, Hauptklasse `de.mcsm.companion.CompanionPlugin`
* Zielplattform: Paper ab 1.21 (`api-version: "1.21"`), getestet mit Paper 26.2 auf Java 25
* Kompiliert für Java 21 (`--release 21`), keine externen Bibliotheken – nur die in Paper
  gebündelte Adventure-API (MiniMessage)
* Fertiges Jar: `assets/MCSMCompanion.jar`

## Funktionen

| Bereich | Verhalten |
|---|---|
| Chat | `AsyncChatEvent`-Renderer mit MiniMessage-Format (`chat_format`). Spieler mit `mcsm.chat.color` dürfen `&`-Farbcodes benutzen, alle anderen schreiben reinen Text. |
| Join / Leave | Eigene Texte (`join_format`, `leave_format`), beim allerersten Besuch `first_join_format`. |
| Tod | Die Vanilla-Ursache bleibt erhalten, wird grau gefärbt, der Name des Gestorbenen weiß hervorgehoben und mit `death_prefix` (Standard `☠`) eingeleitet. |
| Tablist | Kopfzeile mit Servername und Online-Zähler, Fußzeile mit Sponsor-Text (Standard „Sponsored by Novelnia“) und „MinecraftManager“. Aktualisierung bei Join/Quit und alle 5 s. |
| Serverliste | Optional eigene MOTD-Zeile (`motd_line`). |
| TPA | `/tpa`, `/tpaccept`, `/tpdeny` mit klickbaren Schaltflächen. Anfragen verfallen nach 60 s, Abklingzeit 10 s. |
| Teleport | `/tp <Spieler> [Spieler2]` für OPs. Alles andere (Koordinaten, `~`/`^`, Selektoren wie `@p`, Entities) wird unverändert an das Vanilla-`/tp` weitergereicht. |
| Spielmodus | `/gm <0|1|2|3|survival|creative|adventure|spectator> [Spieler]` für OPs. |
| Homes | `/sethome`, `/home`, `/delhome`, `/homes`; gespeichert in `homes.yml`; ohne OP maximal `max_homes` (Standard 3). |
| Spawn | `/spawn` teleportiert zum Spawnpunkt der Hauptwelt. |
| Hardcore | Eigener MCSM-Hardcore-Modus (`hardcore`, `/hardcore on|off|status`): Wer im Überleben- oder Abenteuer-Modus stirbt, bekommt am Todesort ein Grab (eigener Spielerkopf, zwei schwebende Textzeilen „R.I.P Name“ und Datum) und bleibt als Zuschauer daran gebunden, bis ein Mitspieler das Grab mit einem Totem der Unsterblichkeit rechtsklickt oder eines davor ablegt. Details im Abschnitt „Hardcore-Modus“. |
| Status | `status.json` für den Manager (beim Start, alle 60 s und beim Beenden). |

Alle Meldungen sind deutsch, MiniMessage-formatiert und tragen das Präfix `[MCSM]`.

## Befehle

| Befehl | Beschreibung | Recht |
|---|---|---|
| `/tpa <Spieler>` | Teleport-Anfrage senden | `mcsm.tpa` (alle) |
| `/tpaccept [Spieler]` | Anfrage annehmen (ohne Namen: die jüngste) | `mcsm.tpa` |
| `/tpdeny [Spieler]` | Anfrage ablehnen | `mcsm.tpa` |
| `/tp <Spieler> [Spieler2]` | Selbst zu einem Spieler oder Spieler zu Spieler2 teleportieren (Koordinaten/Selektoren wie Vanilla) | `mcsm.tp` (OP) |
| `/gm <Modus> [Spieler]` | Spielmodus setzen (`0-3`, `s/c/a/sp`, englische oder deutsche Namen) | `mcsm.gm` (OP) |
| `/sethome [Name]` | Home setzen (Standardname `home`) | `mcsm.home` (alle) |
| `/home [Name]` | Zum Home teleportieren | `mcsm.home` |
| `/delhome [Name]` | Home löschen | `mcsm.home` |
| `/homes` | Eigene Homes anzeigen (klickbar) | `mcsm.home` |
| `/spawn` | Zum Spawn der Hauptwelt | `mcsm.spawn` (alle) |
| `/hardcore on` | Hardcore-Modus einschalten (ohne Wirkung auf frühere Tode) | `mcsm.hardcore` (OP, Konsole) |
| `/hardcore off` | Hardcore-Modus ausschalten, alle Toten wiederbeleben, alle Gräber entfernen | `mcsm.hardcore` |
| `/hardcore status` | Zustand und Liste der toten Spieler anzeigen | `mcsm.hardcore` |

OPs und die Konsole dürfen immer.

## Rechte

| Recht | Standard | Bedeutung |
|---|---|---|
| `mcsm.tpa` | alle | `/tpa`, `/tpaccept`, `/tpdeny` |
| `mcsm.tp` | OP | `/tp` |
| `mcsm.gm` | OP | `/gm` |
| `mcsm.home` | alle | Homes |
| `mcsm.home.unlimited` | OP | keine Begrenzung durch `max_homes` |
| `mcsm.spawn` | alle | `/spawn` |
| `mcsm.chat.color` | OP | `&`-Farbcodes im Chat |
| `mcsm.hardcore` | OP | `/hardcore` |
| `mcsm.*` | OP | alles |

## Konfiguration: `plugins/MCSMCompanion/config.yml`

Der Manager schreibt die Datei vor jedem Start. Fehlt sie, legt das Plugin die mitgelieferte
Vorlage an; fehlende Schlüssel werden mit Standardwerten ergänzt. Alle Texte sind
[MiniMessage](https://docs.advntr.dev/minimessage/format.html).

| Schlüssel | Typ | Standard | Bedeutung |
|---|---|---|---|
| `server_name` | Text | `Minecraft Server` | Name in der Tablist-Kopfzeile |
| `manager_version` | Text | `unbekannt` | Version des Managers (Info-Zeile, `status.json`) |
| `mode` | `local` \| `hosted` | `local` | Eigener PC oder Paid-/Root-Server |
| `sponsor_text` | Text | `Sponsored by Novelnia` | Sponsor-Zeile, Platzhalter `<sponsor>` |
| `chat_format` | Text | `<gray>[</gray><green>Spieler</green><gray>]</gray> <white><name></white> <dark_gray>»</dark_gray> <gray><message></gray>` | Platzhalter `<name>`, `<message>` |
| `join_format` | Text | `<green>+</green> <white><name></white> <gray>ist beigetreten</gray>` | Platzhalter `<name>` |
| `leave_format` | Text | `<red>-</red> <white><name></white> <gray>hat den Server verlassen</gray>` | Platzhalter `<name>` |
| `first_join_format` | Text | `<gold>★</gold> <white><name></white> <gray>ist zum ersten Mal hier – willkommen!</gray>` | Platzhalter `<name>` |
| `death_prefix` | Text | `<red>☠</red> ` | Präfix vor der Todesmeldung |
| `tablist_header` | Text | `<gradient:#3ddc84:#8ff0b4><bold><server_name></bold></gradient>\n<gray>Online <white><online></white>/<white><max></white></gray>` | Platzhalter `<server_name>`, `<online>`, `<max>`, `<sponsor>`, `<manager_version>`, `<mode>` |
| `tablist_footer` | Text | `<gray>Sponsored by <gold>Novelnia</gold></gray> <dark_gray>•</dark_gray> <gray>MinecraftManager</gray>` | dieselben Platzhalter; fehlt der Schlüssel und `sponsor_text` ist geändert, wird `<gray><sponsor></gray> • MinecraftManager` benutzt |
| `motd_line` | Text | leer | Eigene MOTD (leer = Server-MOTD bleibt) |
| `max_homes` | Zahl | `3` | Home-Limit ohne OP / `mcsm.home.unlimited` |
| `hardcore` | bool | `false` | MCSM-Hardcore-Modus; wird nur übernommen, wenn sich der Wert seit dem letzten Start geändert hat (siehe „Hardcore-Modus“) |
| `features.chat` | bool | `true` | Chatformat |
| `features.join_leave` | bool | `true` | Join-/Leave-Texte |
| `features.death` | bool | `true` | Todesmeldungen |
| `features.tablist` | bool | `true` | Tablist |
| `features.tpa` | bool | `true` | `/tpa` & Co. |
| `features.homes` | bool | `true` | Homes |
| `features.gamemode` | bool | `true` | `/gm` |

Beispiel, wie der Manager die Datei schreibt:

```yaml
server_name: "Mein Server"
manager_version: "1.4.0"
mode: "local"
sponsor_text: "Sponsored by Novelnia"
hardcore: false
features:
  chat: true
  join_leave: true
  death: true
  tablist: true
  tpa: true
  homes: true
  gamemode: true
```

Weitere Dateien im Plugin-Ordner: `homes.yml` (Homes je Spieler-UUID mit `world`, `x`, `y`,
`z`, `yaw`, `pitch`), `hardcore.yml` (Zustand des Hardcore-Modus, siehe unten) und `status.json`.

## Hardcore-Modus

Kein Vanilla-Hardcore (`hardcore=true` in `server.properties` bleibt unberührt), sondern eine
eigene, umkehrbare Variante: Wer stirbt, bleibt tot, bis ein Mitspieler ihn wiederbelebt.

**Ein- und Ausschalten.** Der Manager schreibt `hardcore: true|false` in die `config.yml`; das
Plugin merkt sich in `hardcore.yml` (`config_hardcore`), welchen Wert es zuletzt übernommen hat,
und wendet den Schlüssel nur an, wenn er sich geändert hat. Dazwischen gilt, was per
`/hardcore on|off` geschaltet wurde (`enabled` in `hardcore.yml`). `on` wirkt nicht rückwirkend;
`off` belebt alle Toten wieder (online: sofort, offline: beim nächsten Beitritt) und entfernt alle
Gräber. `/hardcore status` zeigt den Zustand und alle Toten mit Grabkoordinaten und Todeszeit.

**Tod.** Zählt nur im Überleben- oder Abenteuer-Modus; Kreativ- und
Zuschauer-Spieler sind ausgenommen. Gegenstände und Erfahrung fallen wie in Vanilla. Der
Todesbildschirm wird übersprungen (Wiedererscheinen im nächsten Tick), statt der normalen
Todesmeldung erscheint `☠ Name ist von uns gegangen (Koordinaten: X: 123 Y: 64 Z: -45)`.

**Grab.** Am Todesort (Block der Todesposition, sonst bis 3 Blöcke höher, dann bis 3 tiefer) wird
in einem ersetzbaren Block mit festem Untergrund der Spielerkopf des Toten gesetzt (Gesicht in
Blickrichtung wie beim Platzieren von Hand). Gibt es keinen festen Untergrund (Fall, Leere, Lava,
offenes Wasser), kommt darunter ein Sockel aus polierten Schwarzsteinziegeln. Über dem Kopf
schweben zwei Textanzeigen (`TextDisplay`, zum Betrachter gedreht, halbtransparenter Hintergrund):
`R.I.P Name` und `dd.MM.yy - HH:mm` (Serverzeit). Beide tragen den Datenschlüssel
`mcsm:grave = <UUID des Toten>`, sind unverwundbar und werden beim Aktivieren des Plugins,
beim Laden der Welt und beim Laden des Chunks geprüft und bei Bedarf neu erzeugt.

**Schutz.** Kopf und Sockel lassen sich nicht abbauen (Hinweis an den Spieler), werden aus
Explosionen herausgenommen, halten Kolben auf und ignorieren Feuer, Verfall, Blockphysik und
blockverändernde Mobs (Wither, Verwüster, Silberfische).

**Zuschauer-Sperre.** Der Tote erscheint einen Block über seinem Grab und wird im nächsten Tick
Zuschauer. Jeder Spielmoduswechsel (auch `/gamemode`, `/gm`) wird abgebrochen: „Du bist tot –
ein Mitspieler muss dich am Grab mit einem Totem wiederbeleben.“ Beim Beitritt landet er wieder
als Zuschauer am Grab. Schaden an Toten (etwa `/kill`) wird verworfen, ein erneuter Tod
abgebrochen.

**Wiederbelebung.** Ein lebender Mitspieler klickt den Grabkopf mit einem Totem der
Unsterblichkeit in Haupt- oder Nebenhand rechts an (verbraucht ein Totem, außer im
Kreativmodus) oder lässt ein Totem fallen, das innerhalb von 5 Sekunden bis auf 1,5 Blöcke an
den Kopf herankommt (das Totem verschwindet). Dann wird der Kopf durch den ursprünglichen Block
ersetzt (Lava und Feuer durch Luft; ein gesetzter Sockel bleibt als Standfläche), die Anzeigen
verschwinden, der Tote wird an die Grabstelle teleportiert (auf den ersten freien Platz ab der
Kopfposition, nie in Fels oder Flüssigkeit), kommt in den Überlebensmodus, ist geheilt und satt
und bekommt nach einem Tod in Lava oder Wasser 15 Sekunden Feuerschutz und Wasseratmung,
und alle sehen `♥ Name wurde von Retter wiederbelebt!`. Ist der Tote offline, wird die
Wiederbelebung vorgemerkt (`pending_revive`, `reviver`) und beim nächsten Beitritt angewendet.

**Datei `hardcore.yml`** (atomar geschrieben):

```yaml
enabled: true
config_hardcore: true
dead:
  069a79f4-44e9-4726-a5be-fca90e38aaf5:
    name: Steve
    world: world
    x: 123.4
    y: 64.0
    z: -45.2
    time: 1789939944
    head: {world: world, x: 123, y: 64, z: -46}
    replaced_block: AIR
    base: false
    base_replaced: AIR
    displays: [<uuid>, <uuid>]
    pending_revive: false
    reviver: Alex
```

## Status: `plugins/MCSMCompanion/status.json`

Wird beim Aktivieren, alle 60 Sekunden, bei jeder Änderung des
Hardcore-Zustands (Schalter, Tod, Wiederbelebung) und beim Beenden geschrieben – atomar über `status.json.tmp` + Umbenennen, damit der Manager nie eine halbe
Datei liest.

```json
{
  "plugin_version": "1.0.0",
  "paper_version": "26.2-126-c646faf (MC: 26.2)",
  "minecraft_version": "26.2",
  "server_name": "Mein Server",
  "mode": "local",
  "online": 3,
  "max_players": 10,
  "online_mode": true,
  "plugins": [
    {"name": "MCSMCompanion", "version": "1.0.0", "enabled": true},
    {"name": "Geyser-Spigot", "version": "2.11.3-SNAPSHOT", "enabled": true}
  ],
  "suspicious": ["FakePlayers"],
  "hardcore": {"enabled": true, "dead": ["Steve"]},
  "updated": 1789939944
}
```

| Feld | Bedeutung |
|---|---|
| `plugin_version` | Version dieses Plugins |
| `paper_version` | `Bukkit.getVersion()` |
| `minecraft_version` | `Bukkit.getMinecraftVersion()` |
| `server_name`, `mode` | aus der Konfiguration |
| `online` | tatsächliche Spielerzahl |
| `max_players` | `Bukkit.getMaxPlayers()` |
| `online_mode` | Online-Modus des Servers |
| `plugins` | alle geladenen Plugins mit Version und Zustand |
| `suspicious` | Plugins, deren Name oder Beschreibung (ohne Groß-/Kleinschreibung) `fake`, `spoof`, `playercount`, `player-count`, `fakeplayer`, `bot`, `serverlistplus`, `pingspoof`, `maxplayers`, `onlinecount`, `ghostplayer` oder `fakeonline` enthält – Heuristik, keine Garantie. Geyser, Floodgate, ViaVersion, ViaBackwards und dieses Plugin sind ausgenommen. Die Liste wird beim Start zusätzlich als Warnung ins Log geschrieben. |
| `hardcore` | `enabled`: Hardcore-Modus aktiv; `dead`: Namen der Toten (inklusive vorgemerkter Wiederbelebungen) |
| `updated` | Unix-Zeit (Sekunden) |

Beim Beenden steht das Plugin selbst mit `"enabled": false` in der Liste – daran erkennt der
Manager, dass der letzte Eintrag vom Herunterfahren stammt.

## Bauen

```
python tools/build_plugin.py [servers/<id>]
```

Das Skript benutzt nur die Python-Standardbibliothek und das JDK unter
`runtime/jdk21/<jdk>/bin` (`javac`, `jar`). Den Classpath liefert ein Paper-Serverordner
(Argument oder automatisch der erste unter `servers/`), der mindestens einmal gestartet
wurde: `versions/<v>/paper-<v>.jar` und `libraries/**/*.jar`.

Paper 26.x ist mit Java 25 gebaut (Klassendatei-Version 69), javac 21 liest solche Jars nicht.
Deshalb legt das Skript unter `build/plugin/api/` eine reine Compile-Kopie der betroffenen Jars
an, in der die Versionsnummer der Klassendateien auf 65 (Java 21) gesetzt ist – javac braucht
daraus nur die Signaturen. Das Plugin selbst wird mit `--release 21` übersetzt und läuft damit
auf Paper 1.21.x (Java 21) genauso wie auf Paper 26.x (Java 25). `build/` ist Wegwerf-Ausgabe
und steht in `.gitignore`; das Ergebnis `assets/MCSMCompanion.jar` wird eingecheckt.

Kompiliert wird mit `-Xlint:all -Werror` (ohne `serial` und ohne `classfile`, weil die
JetBrains-Annotationen der API nur zur Compile-Zeit existieren und nicht im Server liegen).

## Integration in den Manager (Vertrag)

1. Vor jedem Start `assets/MCSMCompanion.jar` nach `servers/<id>/plugins/MCSMCompanion.jar`
   kopieren (auch wenn es gelöscht wurde) und `plugins/MCSMCompanion/config.yml` schreiben.
2. Nach dem Start bzw. periodisch `plugins/MCSMCompanion/status.json` lesen; `suspicious`
   nicht leer → Hinweis in der Oberfläche (bei `mode: hosted` besonders relevant, weil dort
   die Spielerzahl nicht über die Manager-Einstellungen manipuliert werden kann).
3. `updated` älter als etwa 3 Minuten bei laufendem Server → Plugin wurde entfernt oder
   deaktiviert.
