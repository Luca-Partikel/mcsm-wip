package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Level;

import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;

/**
 * Laufzeitzustand des MCSM-Hardcore-Modus in plugins/MCSMCompanion/hardcore.yml: Schalter, zuletzt
 * übernommener Wert des config.yml-Schlüssels "hardcore" und alle Gräber toter Spieler. Geschrieben
 * wird atomar über hardcore.yml.tmp + Umbenennen.
 */
public final class GraveStore {

    /** Ein Grab: Todesort, Kopfblock, ersetzte Blöcke, Anzeige-Entities und ausstehende Wiederbelebung. */
    public static final class Grave {
        public final UUID victim;
        public String name = "?";
        public String world = "world";
        public double x;
        public double y;
        public double z;
        /** Todeszeit in Unix-Sekunden. */
        public long time;
        public String headWorld = "world";
        public int headX;
        public int headY;
        public int headZ;
        /** Blocktyp, den der Kopf ersetzt hat (Material-Name, meist AIR). */
        public String replacedBlock = "AIR";
        /** Wurde unter dem Kopf ein Sockel gesetzt (Tod im Fall, in der Leere, in Lava oder Wasser)? */
        public boolean base;
        public String baseReplaced = "AIR";
        /** UUIDs der beiden TextDisplays über dem Kopf. */
        public final List<UUID> displays = new ArrayList<>();
        /** Wiederbelebt, während der Spieler offline war – wird beim nächsten Beitritt angewendet. */
        public boolean pendingRevive;
        public String reviver;

        public Grave(UUID victim) {
            this.victim = victim;
        }
    }

    private final CompanionPlugin plugin;
    private boolean enabled;
    private Boolean configSeen;
    private final Map<UUID, Grave> dead = new LinkedHashMap<>();

    public GraveStore(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public File file() {
        return new File(plugin.getDataFolder(), "hardcore.yml");
    }

    public void load() {
        dead.clear();
        YamlConfiguration y = YamlConfiguration.loadConfiguration(file());
        enabled = y.getBoolean("enabled", false);
        configSeen = y.isSet("config_hardcore") ? Boolean.valueOf(y.getBoolean("config_hardcore")) : null;
        ConfigurationSection sec = y.getConfigurationSection("dead");
        if (sec == null) {
            return;
        }
        for (String key : sec.getKeys(false)) {
            UUID id;
            try {
                id = UUID.fromString(key);
            } catch (IllegalArgumentException ex) {
                plugin.getLogger().warning("hardcore.yml: ungültige Spieler-UUID \"" + key + "\" übersprungen.");
                continue;
            }
            ConfigurationSection g = sec.getConfigurationSection(key);
            if (g == null) {
                continue;
            }
            Grave grave = new Grave(id);
            grave.name = g.getString("name", "?");
            grave.world = g.getString("world", "world");
            grave.x = g.getDouble("x");
            grave.y = g.getDouble("y");
            grave.z = g.getDouble("z");
            grave.time = g.getLong("time");
            grave.headWorld = g.getString("head.world", grave.world);
            grave.headX = g.getInt("head.x");
            grave.headY = g.getInt("head.y");
            grave.headZ = g.getInt("head.z");
            grave.replacedBlock = g.getString("replaced_block", "AIR");
            grave.base = g.getBoolean("base", false);
            grave.baseReplaced = g.getString("base_replaced", "AIR");
            for (String s : g.getStringList("displays")) {
                try {
                    grave.displays.add(UUID.fromString(s));
                } catch (IllegalArgumentException ex) {
                    // fehlerhafte Anzeige-UUID: die Anzeige wird beim nächsten Chunk-Laden neu erzeugt
                }
            }
            grave.pendingRevive = g.getBoolean("pending_revive", false);
            grave.reviver = g.getString("reviver");
            dead.put(id, grave);
        }
    }

    public void save() {
        YamlConfiguration y = new YamlConfiguration();
        y.set("enabled", enabled);
        if (configSeen != null) {
            y.set("config_hardcore", configSeen);
        }
        y.createSection("dead");
        for (Grave g : dead.values()) {
            String p = "dead." + g.victim + ".";
            y.set(p + "name", g.name);
            y.set(p + "world", g.world);
            y.set(p + "x", g.x);
            y.set(p + "y", g.y);
            y.set(p + "z", g.z);
            y.set(p + "time", g.time);
            y.set(p + "head.world", g.headWorld);
            y.set(p + "head.x", g.headX);
            y.set(p + "head.y", g.headY);
            y.set(p + "head.z", g.headZ);
            y.set(p + "replaced_block", g.replacedBlock);
            y.set(p + "base", g.base);
            y.set(p + "base_replaced", g.baseReplaced);
            List<String> ids = new ArrayList<>();
            for (UUID u : g.displays) {
                ids.add(u.toString());
            }
            y.set(p + "displays", ids);
            y.set(p + "pending_revive", g.pendingRevive);
            if (g.reviver != null) {
                y.set(p + "reviver", g.reviver);
            }
        }
        File target = file();
        File dir = target.getParentFile();
        if (dir != null && !dir.isDirectory() && !dir.mkdirs()) {
            plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden: " + dir);
            return;
        }
        Path tmp = new File(dir, "hardcore.yml.tmp").toPath();
        Path dst = target.toPath();
        try {
            Files.writeString(tmp, y.saveToString(), StandardCharsets.UTF_8);
            try {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (IOException atomicFailed) {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "hardcore.yml konnte nicht geschrieben werden", ex);
        }
    }

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    /** Zuletzt übernommener Wert von config.yml "hardcore"; null, wenn noch nie übernommen. */
    public Boolean configSeen() {
        return configSeen;
    }

    public void setConfigSeen(boolean value) {
        this.configSeen = value;
    }

    public Grave get(UUID victim) {
        return dead.get(victim);
    }

    public boolean contains(UUID victim) {
        return dead.containsKey(victim);
    }

    public void put(Grave grave) {
        dead.put(grave.victim, grave);
    }

    public Grave remove(UUID victim) {
        return dead.remove(victim);
    }

    public boolean isEmpty() {
        return dead.isEmpty();
    }

    /** Alle Gräber in Reihenfolge des Todes (nicht veränderbar). */
    public Collection<Grave> graves() {
        return Collections.unmodifiableCollection(dead.values());
    }
}
