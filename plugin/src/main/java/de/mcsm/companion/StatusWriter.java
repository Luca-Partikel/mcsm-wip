package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.logging.Level;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;

/**
 * Schreibt plugins/MCSMCompanion/status.json (atomar über Temp-Datei + Umbenennen), damit der
 * Manager Version, Spielerzahl, verdächtige Plugins und den Hardcore-Zustand auslesen kann.
 */
public final class StatusWriter {

    /** Schlagwörter (klein geschrieben) in Name oder Beschreibung, die auf Manipulation hindeuten. */
    private static final List<String> SUSPICIOUS = List.of(
            "fake", "spoof", "playercount", "player-count", "fakeplayer", "bot", "serverlistplus",
            "pingspoof", "maxplayers", "onlinecount", "ghostplayer", "fakeonline");

    /** Bekannte, vom Manager selbst installierte Plugins werden nicht als verdächtig gemeldet. */
    private static final Set<String> TRUSTED = Set.of(
            "mcsmcompanion", "geyser-spigot", "floodgate", "viaversion", "viabackwards", "viarewind");

    private final CompanionPlugin plugin;

    public StatusWriter(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public File file() {
        return new File(plugin.getDataFolder(), "status.json");
    }

    public void write() {
        String json;
        try {
            json = build();
        } catch (RuntimeException ex) {
            plugin.getLogger().log(Level.WARNING, "status.json konnte nicht erstellt werden", ex);
            return;
        }
        File target = file();
        File dir = target.getParentFile();
        if (dir != null && !dir.isDirectory() && !dir.mkdirs()) {
            plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden: " + dir);
            return;
        }
        Path tmp = new File(dir, "status.json.tmp").toPath();
        Path dst = target.toPath();
        try {
            Files.writeString(tmp, json, StandardCharsets.UTF_8);
            try {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (IOException atomicFailed) {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "status.json konnte nicht geschrieben werden", ex);
        }
    }

    public List<String> suspiciousPlugins() {
        List<String> out = new ArrayList<>();
        for (Plugin p : Bukkit.getPluginManager().getPlugins()) {
            if (isSuspicious(p)) {
                out.add(p.getPluginMeta().getName());
            }
        }
        return out;
    }

    private static boolean isSuspicious(Plugin p) {
        String name = p.getPluginMeta().getName();
        if (TRUSTED.contains(name.toLowerCase(Locale.ROOT))) {
            return false;
        }
        String desc = p.getPluginMeta().getDescription();
        String hay = (name + " " + (desc == null ? "" : desc)).toLowerCase(Locale.ROOT);
        for (String key : SUSPICIOUS) {
            if (hay.contains(key)) {
                return true;
            }
        }
        return false;
    }

    private String build() {
        Config cfg = plugin.settings();
        StringBuilder sb = new StringBuilder(1024);
        sb.append("{\n");
        field(sb, "plugin_version", plugin.getPluginMeta().getVersion()).append(",\n");
        field(sb, "paper_version", Bukkit.getVersion()).append(",\n");
        field(sb, "minecraft_version", Bukkit.getMinecraftVersion()).append(",\n");
        field(sb, "server_name", cfg.serverName).append(",\n");
        field(sb, "mode", cfg.mode).append(",\n");
        sb.append("  \"online\": ").append(Bukkit.getOnlinePlayers().size()).append(",\n");
        sb.append("  \"max_players\": ").append(Bukkit.getMaxPlayers()).append(",\n");
        sb.append("  \"online_mode\": ").append(Bukkit.getOnlineMode()).append(",\n");

        sb.append("  \"plugins\": [");
        boolean first = true;
        for (Plugin p : Bukkit.getPluginManager().getPlugins()) {
            sb.append(first ? "\n" : ",\n");
            first = false;
            sb.append("    {\"name\": ").append(quote(p.getPluginMeta().getName()))
              .append(", \"version\": ").append(quote(p.getPluginMeta().getVersion()))
              .append(", \"enabled\": ").append(p.isEnabled()).append('}');
        }
        sb.append(first ? "],\n" : "\n  ],\n");

        sb.append("  \"suspicious\": ").append(array(suspiciousPlugins())).append(",\n");

        HardcoreManager hc = plugin.hardcore();
        sb.append("  \"hardcore\": {\"enabled\": ").append(hc != null && hc.isEnabled())
          .append(", \"dead\": ").append(array(hc == null ? List.of() : hc.deadNames())).append("},\n");
        sb.append("  \"updated\": ").append(System.currentTimeMillis() / 1000L).append('\n');
        sb.append("}\n");
        return sb.toString();
    }

    private static StringBuilder field(StringBuilder sb, String key, String value) {
        return sb.append("  ").append(quote(key)).append(": ").append(quote(value));
    }

    private static String array(List<String> items) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(quote(items.get(i)));
        }
        return sb.append(']').toString();
    }

    static String quote(String s) {
        if (s == null) {
            return "null";
        }
        StringBuilder sb = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.append('"').toString();
    }
}
