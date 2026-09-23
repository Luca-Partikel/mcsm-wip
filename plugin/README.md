# MCSMCompanion – Begleit-Plugin des Minecraft Server Managers

Ein Paper-Plugin, das der Manager vor jedem Start automatisch in jeden Java-Server kopiert
(`plugins/MCSMCompanion.jar`). Es bringt ein schöneres Chat-, Join-, Leave- und Todesformat,
eine Tablist mit Sponsor-Zeile, die wichtigsten Essentials-Befehle (TPA, Teleport mit
Aufwärmzeit, `/back`, `/rtp`, Warps, `/top`, Spielmodus, Homes, Spawn), private Nachrichten,
AFK, Spielerstatistik, Begrüßung, Serverregeln, eine Seitenleiste, rotierende Hinweise,
das Überspringen der Nacht und einen eigenen Hardcore-Modus mit Gräbern und Wiederbelebung per
Totem. Dazu kommen die Moderationsbefehle (Sperren, Kicks, Stummschaltungen, Verwarnungen)
mit deutschen Sperrbildschirmen, die Freigabeliste und ein Herunterfahren mit Ansage.
Der Wartungszugang arbeitet mit zwei vollständig getrennten Spielerprofilen (unsichtbar
und normal). Außerdem meldet es dem Manager über `status.json`, wer online ist, welche Plugins
laufen, wie TPS, MSPT und Betriebszeit stehen und ob ein Plugin die Spielerzahl, den Ping oder
die MOTD manipulieren könnte.

Sperren, Freigabeliste und Spielerdaten bleiben dabei im Vanilla-Format
(`banned-players.json`, `banned-ips.json`, `whitelist.json`) – das Plugin schreibt eigene
Texte, aber keine eigene Wahrheit.

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
| Aufwärmzeit | `/home`, `/spawn`, der angenommene `/tpa`, `/back`, `/warp`, `/rtp` und `/top` laufen über einen gemeinsamen Teleportdienst: Fortschrittsbalken in der Aktionsleiste, Abbruch bei Bewegung (mehr als ein halber Block) oder Schaden, danach eine Abklingzeit je Befehl. OPs und `mcsm.teleport.bypass` warten nie. |
| Zurück | `/back` führt zur letzten Position vor einem Teleport oder zum eigenen Todesort (`/back tod`). Gespeichert in `teleports.yml`, Einträge älter als `teleport.history_days` fallen beim Laden weg. |
| Wildnis | `/rtp` (`/wild`) sucht eine sichere Zufallsposition zwischen `min_radius` und `radius`; Chunks werden asynchron geladen, pro Versuch vergeht höchstens ein Tick. |
| Warps | Serverweite Punkte in `warps.yml`: `/warp`, `/warps` für alle, `/setwarp`, `/delwarp` für `mcsm.warp.admin`. |
| Oberfläche | `/top` bringt auf den höchsten sicheren Block über der eigenen Position. |
| Private Nachrichten | `/msg` (`/w`, `/tell`, `/pm`) und `/r`; eigene Formate, Antwortziel je Absender, Ton beim Empfänger, AFK-Hinweis an den Absender, auch die Konsole als Ziel. `/socialspy` liest für OPs alles mit. |
| Abwesenheit | `/afk [Grund]`, automatisch nach `afk.timeout_seconds` Untätigkeit; Chat-Meldung, Zusatz im Tablist-Namen, `setSleepingIgnored`. Unsichtbare Betreiber bleiben unerwähnt. |
| Statistik | `stats.yml` je Spieler: Spielzeit, erster Besuch, letztes Verlassen, Tode, Spieler-/Kreatur-Kills, zurückgelegte Strecke. Abrufbar über `/playtime`, `/seen`, `/stats`. |
| Umgebung | `/near [Radius]` listet sichtbare Mitspieler nach Entfernung mit Himmelsrichtung und AFK-Markierung. |
| Chat-Zusätze | Erwähnungen werden farbig hervorgehoben (mit Ton), `[i]`/`[item]` zeigt den gehaltenen Gegenstand mit Hover, dazu ein Spamschutz (Anzahl je Zeitfenster und Wiederholungssperre). Greift nicht in `chat_format` ein, sondern umschließt den vorhandenen Renderer. |
| Schlafen | Sobald `sleep.percent` der zählenden Spieler einer Oberwelt schläft, wird die Nacht weich übersprungen (Fortschritt in der Aktionsleiste, Meldung im Chat, optional Wetter klären). |
| Begrüßung | Titel, Untertitel, Ton und ein Hinweis auf `/rules` und `/mcsm` beim Beitritt|
| Regeln | `/rules` (`/regeln`) zeigt die Regeln aus der Konfiguration, sonst sechs deutsche Standardregeln. |
| Übersicht | `/mcsm` (`/befehle`) listet alle Befehle in sechs Kategorien, jede Zeile anklickbar, und zeigt Servername, Version, Spielerzahl, TPS und Betriebszeit. Angezeigt wird nur, was registriert und für den Absender erlaubt ist. |
| Seitenleiste | `/sb` (`/sidebar`, `/leiste`) blendet ein eigenes Scoreboard ein: Spielerzahl, eigene Spielzeit, Welt, Koordinaten, Weltzeit, TPS. Fremde Scoreboards werden nie überschrieben. Zustand je Spieler in `sidebar.yml`. |
| Hinweise | Rotierende Tipps im Chat im Abstand von `broadcast.interval_minutes`. |
| Wartungsprofile | Unsichtbar und sichtbar sind zwei getrennte Spielerprofile – siehe Abschnitt „Wartungsprofile“. |
| Kennzahlen | TPS, MSPT, Betriebszeit, geladene Chunks und Entities in `status.json`; bei anhaltend niedrigen TPS eine Warnung im Spiel und im Protokoll. |
| Sperren | `/ban`, `/tempban`, `/unban`, `/banlist`, `/kick` mit deutschem, mehrzeiligem Sperrbildschirm (Grund, Sperrer, Sperrzeitpunkt, Restzeit). Die Sperren liegen in den **eingebauten** Listen `banned-players.json` und `banned-ips.json` und wirken auch ohne Plugin; der Manager kann sie dort direkt lesen. Dauerangaben wie `30m`, `12h`, `7d`, `2w`, auch kombiniert (`1d12h`) und mit deutschen Einheiten. Wartungszugang und OPs lassen sich nicht sperren – die Ablehnung ist neutral formuliert und nennt nie einen Grund dafür. |
| Stummschaltungen | `/mute`, `/tempmute`, `/unmute`, `/mutelist`; gespeichert in `mutes.yml`. Greift im Chat, bei den Umgehungsbefehlen aus `mute.blocked_commands` (auch mit Namensraum, `/minecraft:me`) und beim Beschriften von Schildern und Büchern. Abgelaufene Einträge räumt ein Minutentakt weg und meldet es dem Betroffenen. |
| Verwarnungen | `/warn`, `/warns` mit Verlauf (Datum, Grund, Verwarner) in `warns.yml`. Optional sperrt `warn.auto_ban` ab `warn.auto_ban_threshold` Verwarnungen automatisch auf Zeit; Standard ist aus. |
| Freigabeliste | `/wl an\|aus\|status\|add\|remove\|list\|reload` steuert die **eingebaute** Whitelist – `whitelist.json` bleibt Vanilla-gültig und für den Manager lesbar. Wer nicht freigegeben ist, bekommt einen eigenen mehrzeiligen deutschen Text statt der Vanilla-Zeile. Das An- und Ausschalten wird im Chat angesagt, auch wenn es über `/minecraft:whitelist` oder den Manager passiert. |
| Herunterfahren | `/mcsmstop <Sekunden> [Grund]` fährt mit Countdown herunter: Titel, Chatmeldung und Ton bei 60/30/15/10/5/4/3/2/1, danach `save-all`, freundlicher Abschied für alle und `Bukkit.shutdown()`. `0` = sofort, `abbrechen` stoppt. Ist niemand online, entfällt der Countdown. Bei **jedem** Serverstopp – auch über die Konsole oder den Manager – werden verbliebene Spieler mit einer deutschen Nachricht getrennt statt mit einem harten Verbindungsabbruch. |
| Hardcore | Eigener MCSM-Hardcore-Modus (`hardcore`, `/hardcore on|off|status`): Wer im Überleben- oder Abenteuer-Modus stirbt, bekommt am Todesort ein Grab (eigener Spielerkopf, zwei schwebende Textzeilen „R.I.P Name“ und Datum) und bleibt als Zuschauer daran gebunden, bis ein Mitspieler das Grab mit einem Totem der Unsterblichkeit rechtsklickt oder eines davor ablegt. Details im Abschnitt „Hardcore-Modus“. |
| Status | `status.json` für den Manager (beim Start, alle 30 s und beim Beenden) – darin auch die Online-Liste mit AFK-Zustand und Spielzeit, damit der Manager dafür kein `list` in die Serverkonsole schreiben muss. |

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
| `/back [ort\|tod]` (`/zurueck`) | Zur letzten Position bzw. zum eigenen Todesort | `mcsm.back` (alle) |
| `/rtp` (`/wild`, `/wildnis`) | Sichere Zufallsposition in der Wildnis | `mcsm.rtp` (alle) |
| `/top` (`/oben`) | Auf den höchsten sicheren Block über dir | `mcsm.top` (alle) |
| `/warp <Name>` | Zu einem serverweiten Warp (ohne Namen: Liste) | `mcsm.warp` (alle) |
| `/warps` | Alle Warps anzeigen (klickbar) | `mcsm.warp` |
| `/setwarp <Name>` | Warp an der eigenen Position setzen | `mcsm.warp.admin` (OP) |
| `/delwarp <Name>` | Warp löschen | `mcsm.warp.admin` (OP) |
| `/msg <Spieler> <Text>` (`/w`, `/tell`, `/pm`) | Private Nachricht senden | `mcsm.msg` (alle) |
| `/r <Text>` (`/reply`) | Auf die letzte private Nachricht antworten | `mcsm.msg` |
| `/socialspy [an\|aus\|status]` | Private Nachrichten mitlesen | `mcsm.socialspy` (OP) |
| `/afk [Grund]` | Sich als abwesend melden bzw. zurückkommen | `mcsm.afk` (alle) |
| `/playtime [Spieler]` | Spielzeit, laufende Sitzung, erster Besuch | `mcsm.playtime` (alle) |
| `/seen <Spieler>` | Wann war jemand zuletzt da? | `mcsm.seen` (alle) |
| `/stats [Spieler]` | Spielzeit, Tode, Kills, K/D, Strecke | `mcsm.stats` (alle) |
| `/near [Radius]` | Mitspieler in der Nähe mit Entfernung und Richtung | `mcsm.near` (alle) |
| `/rules` (`/regeln`) | Serverregeln anzeigen | `mcsm.rules` (alle) |
| `/mcsm` (`/befehle`) | Übersicht aller Befehle und Serverinfo | `mcsm.info` (alle) |
| `/sb [an\|aus]` (`/sidebar`, `/leiste`) | Seitenleiste ein- oder ausschalten | `mcsm.sidebar` (alle) |
| `/hardcore on` | Hardcore-Modus einschalten (ohne Wirkung auf frühere Tode) | `mcsm.hardcore` (OP, Konsole) |
| `/hardcore off` | Hardcore-Modus ausschalten, alle Toten wiederbeleben, alle Gräber entfernen | `mcsm.hardcore` |
| `/hardcore status` | Zustand und Liste der toten Spieler anzeigen | `mcsm.hardcore` |
| `/ban <Spieler> [Grund]` (`/bannen`, `/mcsmban`) | Dauerhaft sperren | `mcsm.ban` (OP, Konsole) |
| `/tempban <Spieler> <Dauer> [Grund]` (`/tban`, `/zeitbann`) | Auf Zeit sperren (`30m`, `12h`, `7d`, `2w`, `1d12h`) | `mcsm.ban` |
| `/unban <Spieler>` (`/pardon2`, `/entsperren`) | Konto- und zugehörige IP-Sperre aufheben | `mcsm.ban` |
| `/banlist [Seite]` (`/bans`, `/sperren`, `/mcsmbanlist`) | Alle Sperren mit Grund, Sperrer und Ablauf (anklickbar) | `mcsm.ban` |
| `/kick <Spieler> [Grund]` (`/rauswerfen`, `/mcsmkick`) | Vom Server werfen | `mcsm.ban` |
| `/mute <Spieler> [Grund]` (`/stumm`) | Dauerhaft stummschalten | `mcsm.mute` (OP, Konsole) |
| `/tempmute <Spieler> <Dauer> [Grund]` (`/tmute`) | Auf Zeit stummschalten | `mcsm.mute` |
| `/unmute <Spieler>` | Stummschaltung aufheben | `mcsm.mute` |
| `/mutelist` (`/mutes`) | Stummgeschaltete mit Grund und Restzeit | `mcsm.mute` |
| `/warn <Spieler> <Grund>` (`/verwarnen`) | Verwarnen, ggf. mit Auto-Sperre | `mcsm.warn` (OP, Konsole) |
| `/warns <Spieler>` (`/warnungen`) | Verwarnungsverlauf anzeigen | `mcsm.warn` |
| `/wl <an\|aus\|status\|add\|remove\|list\|reload>` (`/freigabe`) | Freigabeliste verwalten | `mcsm.whitelist` (OP, Konsole) |
| `/mcsmstop <Sekunden\|abbrechen> [Grund]` (`/serverstop`) | Herunterfahren mit Ansage | `mcsm.admin` (OP, Konsole) |

OPs und die Konsole dürfen immer.

### Namensgleichheit mit Vanilla

`/ban`, `/kick` und `/banlist` heißen wie Vanilla-Befehle. Ein Plugin-Befehl aus der `plugin.yml`
hat in Paper Vorrang – **im Testlauf auf Paper 26.2 bestätigt**: `/ban` ohne Argumente antwortet
mit `[MCSM] Verwendung: /ban <Spieler> [Grund]`, während `/minecraft:banlist` weiterhin die
Vanilla-Ausgabe liefert. Sollte ein anderes Plugin den Namen einmal zuerst belegen, führen die
Aliase `/mcsmban`, `/mcsmkick` und `/mcsmbanlist` oder der Namensraum `/mcsmcompanion:ban`
immer zur Plugin-Fassung. `/unban` heißt bewusst **nicht** `/pardon`; der Alias ist `/pardon2`.
`/whitelist` bleibt vollständig bei Vanilla – die eigenen Meldungen greifen über
`WhitelistToggleEvent` trotzdem, auch wenn der Manager oder `/minecraft:whitelist` umschaltet.

### Unterbefehle: `/wl`

| Unterbefehl | Bedeutung |
|---|---|
| `/wl an` (`on`, `ein`) | Freigabeliste einschalten |
| `/wl aus` (`off`) | Freigabeliste ausschalten |
| `/wl status` | Zustand und Anzahl der Einträge |
| `/wl add <Spieler>` (`hinzu`) | Eintragen, auch für nie dagewesene Spieler (fragt dann im Nebenthread bei Mojang nach) |
| `/wl remove <Spieler>` (`rem`, `entfernen`, `raus`) | Austragen; ist der Spieler online und die Liste aktiv, wird er mit Hinweistext getrennt (OPs nicht) |
| `/wl list` (`liste`) | Alle Namen, online grün / offline grau |
| `/wl reload` (`neuladen`) | `whitelist.json` neu einlesen |

### Wartungsbefehle: `/admin`

`/admin` hat bewusst kein eigenes Recht in der `plugin.yml`, damit der Befehl in der
Tab-Vervollständigung nicht auffällt; geprüft wird im Code (Wartungszugang, OP oder
`mcsm.admin`), Unbefugte bekommen die normale deutsche Ablehnung.

| Unterbefehl | Bedeutung |
|---|---|
| `/admin join` | Sichtbar werden, Normal-Profil laden, immer Spielmodus Überleben |
| `/admin vanish` | Normal-Profil sichern, Unsichtbar-Profil laden, unsichtbar werden |
| `/admin profile` (`profil`) | Aktives Profil, Anzahl Gegenstände je Profil und Zeitpunkt der letzten Sicherung |
| `/admin status` | Serverinfo (unverändert) |
| `/admin reload` | Konfiguration neu lesen (unverändert) |
| `/admin inv <Spieler>` | Inventar eines Spielers bearbeitbar öffnen |
| `/admin ec <Spieler>` | Endertruhe eines Spielers öffnen |
| `/admin heal [Spieler]` | Volles Leben und Sättigung, kein Feuer/Frost, schädliche Effekte weg |
| `/admin feed [Spieler]` | Hunger und Sättigung auffüllen |
| `/admin fly [Spieler]` | Flug an/aus |
| `/admin god [Spieler]` | Unverwundbarkeit an/aus |
| `/admin speed <1-10> [Spieler]` | Flug- oder Gehgeschwindigkeit (1 = normal, 10 = Höchstwert) |
| `/admin save` | `savePlayers()` und alle Welten sichern, Rückmeldung mit Dauer und Weltenzahl |
| `/admin restart <Sekunden>` | Countdown mit Titel und Chat für alle, dann sichern und herunterfahren; `0` = sofort, `abbrechen` stoppt |

## Rechte

| Recht | Standard | Bedeutung |
|---|---|---|
| `mcsm.tpa` | alle | `/tpa`, `/tpaccept`, `/tpdeny` |
| `mcsm.tp` | OP | `/tp` |
| `mcsm.gm` | OP | `/gm` |
| `mcsm.home` | alle | Homes |
| `mcsm.home.unlimited` | OP | keine Begrenzung durch `max_homes` |
| `mcsm.spawn` | alle | `/spawn` |
| `mcsm.back` | alle | `/back` |
| `mcsm.rtp` | alle | `/rtp` bzw. `/wild` |
| `mcsm.top` | alle | `/top` |
| `mcsm.warp` | alle | Warps benutzen (`/warp`, `/warps`) |
| `mcsm.warp.admin` | OP | Warps setzen und löschen (`/setwarp`, `/delwarp`) |
| `mcsm.teleport.bypass` | OP | Teleports ohne Aufwärm- und Abklingzeit |
| `mcsm.msg` | alle | Private Nachrichten (`/msg`, `/r`) |
| `mcsm.socialspy` | OP | Private Nachrichten mitlesen |
| `mcsm.afk` | alle | `/afk` |
| `mcsm.playtime` | alle | `/playtime` |
| `mcsm.seen` | alle | `/seen` |
| `mcsm.stats` | alle | `/stats` |
| `mcsm.near` | alle | `/near` |
| `mcsm.near.unlimited` | OP | `/near` ohne Begrenzung des Radius |
| `mcsm.chat.nospam` | OP | vom Spamschutz im Chat ausgenommen |
| `mcsm.rules` | alle | `/rules` |
| `mcsm.info` | alle | `/mcsm` |
| `mcsm.sidebar` | alle | `/sb` |
| `mcsm.chat.color` | OP | `&`-Farbcodes im Chat |
| `mcsm.admin` | OP | `/admin` |
| `mcsm.hardcore` | OP | `/hardcore` |
| `mcsm.ban` | OP | Sperren, Entsperren und Kicks (`/ban`, `/tempban`, `/unban`, `/banlist`, `/kick`) |
| `mcsm.mute` | OP | Stummschaltungen (`/mute`, `/tempmute`, `/unmute`, `/mutelist`) |
| `mcsm.warn` | OP | Verwarnungen (`/warn`, `/warns`) |
| `mcsm.whitelist` | OP | Freigabeliste verwalten (`/wl`) |
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
| `admin.vanish_gamemode` | Text | `creative` | Spielmodus des Unsichtbar-Profils, solange es noch nicht angelegt wurde |
| `admin.normal_gamemode` | Text | `survival` | Spielmodus des Normal-Profils, solange es noch nicht angelegt wurde |
| `admin.join_force_survival` | bool | `true` | `/admin join` schaltet immer auf Überleben (`gm 0`) |
| `admin.restart_marks` | Liste | `[600, 300, 120, 60, 30, 15, 10, 5, 4, 3, 2, 1]` | Sekunden, bei denen `/admin restart` warnt |
| `teleport.warmup_seconds` | Zahl | `3` | Stillstehen vor dem Teleport; `0` = sofort |
| `teleport.cooldown_seconds` | Zahl | `5` | Sperre nach einem Teleport, getrennt je Befehl |
| `teleport.cancel_on_damage` | bool | `true` | Schaden bricht die Aufwärmzeit ab |
| `teleport.back_cooldown_seconds` | Zahl | `10` | Sperre für `/back` |
| `teleport.history_days` | Zahl | `14` | So lange bleiben letzte Position und Todesort in `teleports.yml` |
| `teleport.rtp.world` | Text | leer | Welt für `/rtp`; leer = Hauptwelt |
| `teleport.rtp.radius` | Zahl | `5000` | Größter Abstand zur Weltmitte |
| `teleport.rtp.min_radius` | Zahl | `500` | Kleinster Abstand zur Weltmitte |
| `teleport.rtp.cooldown_seconds` | Zahl | `120` | Sperre für `/rtp` |
| `teleport.rtp.attempts` | Zahl | `30` | Höchstzahl geprüfter Zufallspunkte (max. 200) |
| `msg.format_out` / `msg.format_in` | Text | siehe Vorlage | Formate der gesendeten bzw. empfangenen Nachricht; Platzhalter `<from>`, `<to>`, `<message>` |
| `msg.spy_format` | Text | siehe Vorlage | Format für Mitleser (`/socialspy`) |
| `msg.sound` | bool | `true` | Kurzer Ton beim Empfänger |
| `afk.timeout_seconds` | Zahl | `300` | Untätigkeit bis zum automatischen AFK; `0` = keine Automatik |
| `afk.broadcast` | bool | `true` | Meldung im Chat bei AFK und Rückkehr |
| `afk.tablist_mark` | bool | `true` | AFK-Zusatz im Tablist-Namen |
| `afk.tablist_suffix` | Text | ` [AFK]` | Zusatz hinter dem Namen (MiniMessage) |
| `afk.afk_format` / `afk.back_format` | Text | siehe Vorlage | Meldungen; Platzhalter `<name>`, `<reason>` |
| `chat_extras.mentions` | bool | `true` | Spielernamen im Chat hervorheben |
| `chat_extras.mention_format` | Text | `<gold><bold>@<name></bold></gold>` | Darstellung einer Erwähnung |
| `chat_extras.mention_sound` | bool | `true` | Ton für den Erwähnten |
| `chat_extras.item_display` | bool | `true` | `[i]`/`[item]` durch den gehaltenen Gegenstand ersetzen |
| `chat_extras.spam_messages` | Zahl | `3` | Höchstzahl Nachrichten je Zeitfenster |
| `chat_extras.spam_seconds` | Zahl | `5` | Zeitfenster des Spamschutzes |
| `chat_extras.repeat_seconds` | Zahl | `30` | Sperre für dieselbe Nachricht; `0` = aus |
| `social.near_radius` | Zahl | `100` | Standardradius von `/near` |
| `social.near_max_radius` | Zahl | `500` | Höchstradius ohne `mcsm.near.unlimited` |
| `sleep.percent` | Zahl | `50` | Anteil der zählenden Spieler, der schlafen muss |
| `sleep.afk_seconds` | Zahl | `180` | Ab wann jemand als abwesend gilt und nicht mitzählt |
| `sleep.actionbar` | bool | `true` | Fortschritt in der Aktionsleiste |
| `sleep.announce` | bool | `true` | Im Chat melden, wer die Nacht übersprungen hat |
| `sleep.clear_weather` | bool | `true` | Beim Überspringen auch Regen und Gewitter beenden |
| `welcome.title` / `welcome.subtitle` | Text | siehe Vorlage | Begrüßung; Platzhalter `<name>`, `<server_name>`, `<online>`, `<max>` |
| `welcome.sound` | bool | `true` | Kurzer Ton beim Beitritt |
| `welcome.hint` | bool | `true` | Hinweis auf `/rules` und `/mcsm` |
| `welcome.first_join_spawn` | bool | `true` | Beim allerersten Beitritt zum Spawn teleportieren |
| `rules` | Liste | sechs Standardregeln | Serverregeln für `/rules`; leere Liste = eingebaute Regeln |
| `sidebar.title` | Text | Servername als Farbverlauf | Titel der Seitenleiste; Platzhalter `<server_name>`, `<name>` |
| `sidebar.default_on` | bool | `false` | Leiste für neue Spieler von Anfang an an |
| `sidebar.hint` | bool | `true` | Beim Einschalten einmal auf `/mcsm` hinweisen |
| `broadcast.interval_minutes` | Zahl | `10` | Abstand zwischen zwei Hinweisen |
| `broadcast.messages` | Liste | sechs Standardtipps | Hinweise; leere Liste = eingebaute Tipps |
| `metrics.tps_warn` | Zahl | `15.0` | Unter diesem TPS-Wert gilt der Server als überlastet |
| `metrics.warn_after_seconds` | Zahl | `60` | So lange muss der Wert am Stück zu niedrig sein |
| `ban.default_reason` | Text | `Kein Grund angegeben` | Grund, wenn beim Sperren oder Kicken keiner angegeben wird |
| `ban.also_ban_ip` | bool | `false` | Beim Sperren eines Online-Spielers zusätzlich dessen IP sperren |
| `ban.broadcast` | bool | `true` | Sperre, Entsperrung und Kick im Chat melden |
| `ban.list_per_page` | Zahl | `8` | Einträge je Seite in `/banlist` |
| `ban.screen_permanent` | Text | mehrzeilig | Sperrbildschirm bei dauerhafter Sperre; Platzhalter `<player>`, `<reason>`, `<source>`, `<date>`, `<remaining>`, `<expires>` |
| `ban.screen_temporary` | Text | mehrzeilig | Sperrbildschirm bei Zeitsperre (`<remaining>` z. B. „6 Tage 4 Stunden“) |
| `ban.screen_ip` | Text | mehrzeilig | Sperrbildschirm für eine gesperrte Verbindung |
| `ban.screen_kick` | Text | mehrzeilig | Text beim Hinauswerfen (`/kick`) |
| `ban.broadcast_ban` / `_tempban` / `_unban` / `_kick` | Text | siehe Vorlage | Chat-Meldungen; Platzhalter `<target>`, `<source>`, `<reason>`, `<duration>` |
| `mute.broadcast` | bool | `true` | Stummschaltung und Aufhebung im Chat melden |
| `mute.notify_on_join` | bool | `true` | Stummgeschaltete beim Beitritt daran erinnern |
| `mute.blocked_commands` | Liste | `msg, w, tell, pm, whisper, r, reply, me, say, tpa, tpahere, mail` | Für Stummgeschaltete gesperrte Befehle; leere Liste = keine Befehlssperre |
| `mute.chat_denied` / `mute.command_denied` | Text | siehe Vorlage | Abweisung im Chat bzw. bei einem gesperrten Befehl |
| `mute.write_denied` | Text | siehe Vorlage | Abweisung beim Beschriften von Schildern und Büchern |
| `mute.reason_line` | Text | siehe Vorlage | Zusatzzeile mit Grund und Restzeit; leer lassen, um sie wegzulassen |
| `mute.broadcast_mute` / `_tempmute` / `_unmute` | Text | siehe Vorlage | Chat-Meldungen |
| `warn.broadcast` | bool | `true` | Verwarnung im Chat melden |
| `warn.auto_ban` | bool | `false` | Ab einer bestimmten Zahl von Verwarnungen automatisch sperren |
| `warn.auto_ban_threshold` | Zahl | `3` | Ab wie vielen Verwarnungen automatisch gesperrt wird |
| `warn.auto_ban_duration` | Text | `7d` | Dauer der automatischen Sperre |
| `warn.auto_ban_reason` | Text | `Zu viele Verwarnungen` | Grund der automatischen Sperre |
| `warn.reset_after_ban` | bool | `true` | Verlauf nach einer automatischen Sperre leeren |
| `warn.expire_days` | Zahl | `0` | Ältere Verwarnungen zählen nicht mehr mit; `0` = alle zählen |
| `warn.broadcast_warn` | Text | siehe Vorlage | Chat-Meldung; Platzhalter `<target>`, `<source>`, `<reason>`, `<count>` |
| `whitelist.broadcast` | bool | `true` | An- und Ausschalten der Freigabeliste im Chat melden |
| `whitelist.on_format` / `off_format` | Text | siehe Vorlage | Diese Meldungen; Platzhalter `<server_name>` |
| `whitelist.kick_not_listed_on_enable` | bool | `false` | Beim Einschalten alle nicht Freigegebenen trennen (OPs bleiben) |
| `whitelist.kick_message` | Liste | 4 Zeilen | Text beim Verbindungsversuch ohne Freigabe |
| `whitelist.removed_kick_message` | Liste | 4 Zeilen | Text bei entzogener Freigabe (`/wl remove`) |
| `shutdown.marks` | Liste | `60, 30, 15, 10, 5, 4, 3, 2, 1` | Sekundenmarken, bei denen gewarnt wird |
| `shutdown.reason_default` | Text | `Wartung` | Grund, wenn beim Befehl keiner angegeben wurde |
| `shutdown.chat_format` / `title` / `subtitle` / `cancel_format` | Text | siehe Vorlage | Ansagen; Platzhalter `<sekunden>`, `<zahl>`, `<grund>`, `<server_name>` |
| `shutdown.sound` | bool | `true` | Kurzer Ton bei jeder Warnung |
| `shutdown.kick_message` | Liste | 5 Zeilen | Abschiedstext beim geplanten Herunterfahren (`/mcsmstop`) |
| `shutdown.disable_kick_message` | Liste | 4 Zeilen | Abschiedstext beim Plugin-Ende, also bei jedem Stopp |
| `shutdown.kick_on_disable` | bool | `true` | Beim Plugin-Ende noch verbundene Spieler freundlich trennen |
| `shutdown.kick_only_when_stopping` | bool | `true` | Nur, wenn der Server wirklich stoppt – schützt vor Hinauswerfen bei einem Plugin-Neuladen |
| `features.chat` | bool | `true` | Chatformat |
| `features.join_leave` | bool | `true` | Join-/Leave-Texte |
| `features.death` | bool | `true` | Todesmeldungen |
| `features.tablist` | bool | `true` | Tablist |
| `features.tpa` | bool | `true` | `/tpa` & Co. |
| `features.homes` | bool | `true` | Homes |
| `features.gamemode` | bool | `true` | `/gm` |
| `features.admin_profiles` | bool | `true` | Getrennte Wartungsprofile (aus: ein gemeinsames Inventar) |
| `features.teleport_warmup` | bool | `true` | Aufwärmzeit vor Teleports |
| `features.back` | bool | `true` | `/back` |
| `features.rtp` | bool | `true` | `/rtp` |
| `features.warps` | bool | `true` | Warps |
| `features.top` | bool | `true` | `/top` |
| `features.msg` | bool | `true` | Private Nachrichten |
| `features.afk` | bool | `true` | Abwesenheit |
| `features.stats` | bool | `true` | Spielerstatistik |
| `features.near` | bool | `true` | `/near` |
| `features.chat_extras` | bool | `true` | Chat-Zusätze |
| `features.sleep` | bool | `true` | Nacht überspringen |
| `features.welcome` | bool | `true` | Begrüßung |
| `features.rules` | bool | `true` | `/rules` |
| `features.sidebar` | bool | `true` | Seitenleiste |
| `features.broadcast` | bool | `true` | Rotierende Hinweise |
| `features.metrics` | bool | `true` | Kennzahlen in `status.json` und TPS-Warnung |
| `features.ban` | bool | `true` | Sperren und Kicks; `false` lässt auch `moderation` aus `status.json` weg |
| `features.mute` | bool | `true` | Stummschaltungen |
| `features.warn` | bool | `true` | Verwarnungen |
| `features.whitelist` | bool | `true` | Freigabeliste (`/wl`) |
| `features.shutdown` | bool | `true` | Herunterfahren mit Ansage und Abschied beim Plugin-Ende |

Beispiel, wie der Manager die Datei schreibt (die übrigen Schlüssel dürfen fehlen – dann gelten
die Standardwerte):

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

Weitere Dateien im Plugin-Ordner – alle legt das Plugin selbst an, keine gehört dem Manager:

| Datei | Inhalt |
|---|---|
| `homes.yml` | Homes je Spieler-UUID mit `world`, `x`, `y`, `z`, `yaw`, `pitch` |
| `warps.yml` | Serverweite Warps mit Ersteller und Anlagezeit |
| `teleports.yml` | Letzte Position und Todesort je Spieler für `/back` |
| `stats.yml` | `players.<uuid>.{name, first_join, last_quit, play_ms, deaths, player_kills, mob_kills, distance_cm}` |
| `sidebar.yml` | Je Spieler, ob die Seitenleiste an ist |
| `admin-profiles.yml` | Die beiden Wartungsprofile und das jeweils aktive (`aktiv.<uuid>`) |
| `hardcore.yml` | Zustand des Hardcore-Modus (siehe unten) |
| `mutes.yml` | `mutes.<uuid>.{name, reason, source, created, expires}` – `expires: 0` = dauerhaft, Zeiten in Millisekunden |
| `warns.yml` | `warns.<uuid>.{name, entries.<i>.{time, reason, source}}` |
| `status.json` | Meldung an den Manager (siehe unten) |

Die **Sperren** liegen bewusst nicht hier, sondern in `banned-players.json` und `banned-ips.json`
im Serverordner – im Vanilla-Format, damit sie auch ohne Plugin wirken und der Manager sie direkt
lesen kann. Ebenso steht die Freigabeliste ausschließlich in `whitelist.json`; das Plugin legt
dafür keine eigene Datei an.

## Wartungsprofile

Der Wartungszugang spielt mit **zwei vollständig getrennten Spielerprofilen**. Welches gilt,
hängt allein an der Sichtbarkeit:

* **Unsichtbar-Profil** – gilt, solange der Zugang unsichtbar ist: beim stillen Beitritt und
  nach `/admin vanish`. Ohne gespeicherte Daten startet es leer im Modus
  `admin.vanish_gamemode` (Standard Kreativ).
* **Normal-Profil** – gilt ab `/admin join`. Ohne gespeicherte Daten startet es leer im Modus
  `admin.normal_gamemode` (Standard Überleben). `/admin join` schaltet zusätzlich **immer** auf
  Überleben (`gm 0`), solange `admin.join_force_survival` nicht abgeschaltet ist; ein Flugrecht
  aus dem Kreativmodus wird dabei nicht mitgenommen.

Zu einem Profil gehören: kompletter Inventarinhalt, Rüstung, Zweithand, ausgewählter
Hotbar-Platz, Erfahrung (Level und Anteil), Leben, Hunger, Sättigung, Spielmodus,
`allowFlight`/`isFlying` und aktive Trank-Effekte. **Die Endertruhe gehört nicht dazu** – sie
ist für beide Profile dieselbe.

Beim Wechsel wird zuerst der aktuelle Zustand in das **alte** Profil gesichert und die Datei
geschrieben, dann das **neue** Profil vollständig angewendet (Inventar vorher restlos geleert,
auch der Gegenstand am Mauszeiger, dazu Effekte, Feuer und Frost). Welches Profil gerade gilt,
steht dauerhaft in `admin-profiles.yml`; beim Beitritt wird das Inventar deshalb immer dem
Profil zugeordnet, in dem der Server verlassen wurde. Gesichert wird bei jedem Wechsel, beim
Verlassen des Servers und beim Herunterfahren des Plugins.

`/admin profile` zeigt das aktive Profil, die Anzahl der Gegenstände je Profil und den
Zeitpunkt der letzten Sicherung. Mit `features.admin_profiles: false` fällt alles auf das
frühere Verhalten zurück: ein gemeinsames Inventar, beim Unsichtbarwerden nur ein Wechsel des
Spielmodus.

Die Geh- und Fluggeschwindigkeit (`/admin speed`) gehört nicht zum Profil und gilt bis zum
nächsten Beitritt. Eine per `/admin god` gesetzte Unverwundbarkeit gehört ebenfalls nicht zum
Profil, bleibt aber über `/admin join` und `/admin vanish` hinweg bestehen und übersteht als
Merker in den Spielerdaten auch einen Neustart; der unsichtbare Zustand setzt sie ohnehin.

`admin-profiles.yml` wird atomar über `admin-profiles.yml.tmp` und Umbenennen geschrieben, damit
ein Absturz mitten im Schreiben die gesicherten Inventare nicht zerstört. Ist die Datei beim
Start trotzdem unlesbar, wird sie als `admin-profiles.yml.broken-<Zeitstempel>` beiseite gelegt
und die Profilverwaltung bleibt bis zum nächsten Start abgeschaltet – so überschreibt ein leerer
Zustand nicht die noch vorhandenen Daten. Ein fehlgeschlagener Profilwechsel wird nicht
gespeichert: das bisherige Profil wird wiederhergestellt und bleibt aktiv.

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

Wird beim Aktivieren, alle 30 Sekunden, bei jeder Änderung des
Hardcore-Zustands (Schalter, Tod, Wiederbelebung) und beim Beenden geschrieben – atomar über `status.json.tmp` + Umbenennen, damit der Manager nie eine halbe
Datei liest. Die 30 Sekunden sind kein Zufall: der Manager verwirft die Spielerliste, sobald
`updated` älter als eine Minute ist, und fragt dann wieder über `list` in der Serverkonsole nach.

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
  "tps": 19.98,
  "mspt": 3.41,
  "uptime": 7245,
  "loaded_chunks": 1834,
  "entities": 612,
  "players": [
    {"name": "Steve", "afk": false, "playtime": 7245},
    {"name": "Alex", "afk": true, "playtime": 1980}
  ],
  "moderation": {"bans": 3, "ip_bans": 0, "mutes": 1, "warned": 2},
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
| `online` | sichtbare Spielerzahl (ohne unsichtbare Betreiber) |
| `max_players` | `Bukkit.getMaxPlayers()` |
| `online_mode` | Online-Modus des Servers |
| `tps` | Ticks pro Sekunde (1-Minuten-Mittel, höchstens 20,0) |
| `mspt` | Millisekunden je Tick (Mittel der letzten Ticks) |
| `uptime` | Sekunden seit dem Aktivieren des Plugins |
| `loaded_chunks` | Summe der geladenen Chunks aller Welten |
| `entities` | Summe aller Entities aller Welten |
| `players` | Online-Liste für den Manager: `name`, `afk` (Abwesenheit) und `playtime` (gespielte Sekunden inklusive laufender Sitzung). Unsichtbare Betreiber bleiben draußen – ihr Name gehört weder in diese Datei noch in die Oberfläche; `online` zählt genauso, damit Zahl und Liste sich nicht widersprechen. |
| `moderation` | `bans`, `ip_bans` (aus `banned-players.json` / `banned-ips.json`), `mutes` (aktive Stummschaltungen) und `warned` (Spieler mit mindestens einer Verwarnung). Fehlt bei `features.ban: false`. |
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
4. Die Online-Liste aus `players` benutzen, solange `updated` höchstens eine Minute alt ist
   (das Plugin schreibt alle 30 s); erst danach auf `list` in der Serverkonsole zurückfallen.
5. Zum Stoppen `mcsmstop <Sekunden> [Grund]` in die Konsole schicken statt `stop` – der
   Countdown wird bei leerem Server übersprungen, der Befehl kann also immer gleich lauten.
   Bei `stop` greift trotzdem der freundliche Abschied aus `onDisable`.
