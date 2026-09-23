package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Level;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;

/**
 * Merkt sich je Spieler die letzte Position vor einem Teleport und den letzten eigenen Todesort.
 * Liegt in plugins/MCSMCompanion/teleports.yml und wird nur bei Änderungen geschrieben.
 */
public final class TeleportHistory {

    /** Ein gemerkter Ort mit Zeitstempel in Unix-Sekunden. */
    public static final class Spot {

        private final String world;
        private final double x;
        private final double y;
        private final double z;
        private final float yaw;
        private final float pitch;
        private final long time;

        Spot(String world, double x, double y, double z, float yaw, float pitch, long time) {
            this.world = world;
            this.x = x;
            this.y = y;
            this.z = z;
            this.yaw = yaw;
            this.pitch = pitch;
            this.time = time;
        }

        static Spot of(Location loc) {
            World w = loc.getWorld();
            return new Spot(w == null ? "world" : w.getName(), loc.getX(), loc.getY(), loc.getZ(),
                    loc.getYaw(), loc.getPitch(), System.currentTimeMillis() / 1000L);
        }

        /** Name der Welt, auch wenn diese nicht mehr geladen ist. */
        public String worldName() {
            return world;
        }

        /** Zeitpunkt in Unix-Sekunden. */
        public long time() {
            return time;
        }

        public int blockX() {
            return (int) Math.floor(x);
        }

        public int blockY() {
            return (int) Math.floor(y);
        }

        public int blockZ() {
            return (int) Math.floor(z);
        }

        /** Position; {@code null}, wenn die Welt nicht mehr existiert. */
        public Location toLocation() {
            World w = Bukkit.getWorld(world);
            return w == null ? null : new Location(w, x, y, z, yaw, pitch);
        }
    }

    private final CompanionPlugin plugin;
    private final Map<UUID, Spot> back = new HashMap<>();
    private final Map<UUID, Spot> deaths = new HashMap<>();
    private boolean dirty;

    public TeleportHistory(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    private File file() {
        return new File(plugin.getDataFolder(), "teleports.yml");
    }

    /** Wie lange gemerkte Orte überleben (Tage). */
    private int keepDays() {
        return Math.max(1, plugin.settings().raw().getInt("teleport.history_days", 14));
    }

    /** Liest teleports.yml und wirft dabei zu alte Einträge weg. */
    public void load() {
        back.clear();
        deaths.clear();
        dirty = false;
        YamlConfiguration y = YamlConfiguration.loadConfiguration(file());
        long oldest = System.currentTimeMillis() / 1000L - (long) keepDays() * 86400L;
        readInto(y.getConfigurationSection("back"), back, oldest);
        readInto(y.getConfigurationSection("death"), deaths, oldest);
    }

    private void readInto(ConfigurationSection sec, Map<UUID, Spot> into, long oldest) {
        if (sec == null) {
            return;
        }
        for (String key : sec.getKeys(false)) {
            UUID id;
            try {
                id = UUID.fromString(key);
            } catch (IllegalArgumentException ex) {
                plugin.getLogger().warning("teleports.yml: ungültige Spieler-UUID \"" + key + "\" übersprungen.");
                continue;
            }
            ConfigurationSection s = sec.getConfigurationSection(key);
            if (s == null) {
                continue;
            }
            long time = s.getLong("time");
            if (time < oldest) {
                dirty = true;
                continue;
            }
            into.put(id, new Spot(s.getString("world", "world"), s.getDouble("x"), s.getDouble("y"),
                    s.getDouble("z"), (float) s.getDouble("yaw"), (float) s.getDouble("pitch"), time));
        }
    }

    /** Schreibt nur, wenn sich seit dem letzten Speichern etwas geändert hat. */
    public void saveIfDirty() {
        if (dirty) {
            save();
        }
    }

    /** Schreibt teleports.yml; Fehler landen im Protokoll, nicht beim Aufrufer. */
    public void save() {
        YamlConfiguration y = new YamlConfiguration();
        writeAll(y, "back", back);
        writeAll(y, "death", deaths);
        try {
            File dir = plugin.getDataFolder();
            if (!dir.isDirectory() && !dir.mkdirs()) {
                plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden.");
                return;
            }
            y.save(file());
            dirty = false;
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "teleports.yml konnte nicht gespeichert werden", ex);
        }
    }

    private static void writeAll(YamlConfiguration y, String root, Map<UUID, Spot> from) {
        for (Map.Entry<UUID, Spot> e : from.entrySet()) {
            String p = root + "." + e.getKey() + ".";
            Spot s = e.getValue();
            y.set(p + "world", s.world);
            y.set(p + "x", s.x);
            y.set(p + "y", s.y);
            y.set(p + "z", s.z);
            y.set(p + "yaw", (double) s.yaw);
            y.set(p + "pitch", (double) s.pitch);
            y.set(p + "time", s.time);
        }
    }

    // ------------------------------------------------------------------ Zugriff

    /** Position vor einem Teleport merken. */
    public void rememberBack(Player player, Location from) {
        if (from == null || from.getWorld() == null) {
            return;
        }
        back.put(player.getUniqueId(), Spot.of(from));
        dirty = true;
    }

    /** Todesort merken. */
    public void rememberDeath(Player player, Location where) {
        if (where == null || where.getWorld() == null) {
            return;
        }
        deaths.put(player.getUniqueId(), Spot.of(where));
        dirty = true;
    }

    /** Letzte Position vor einem Teleport; {@code null}, wenn keine bekannt ist. */
    public Spot back(UUID player) {
        return back.get(player);
    }

    /** Letzter Todesort; {@code null}, wenn keiner bekannt ist. */
    public Spot death(UUID player) {
        return deaths.get(player);
    }

    public void clearBack(UUID player) {
        if (back.remove(player) != null) {
            dirty = true;
        }
    }

    public void clearDeath(UUID player) {
        if (deaths.remove(player) != null) {
            dirty = true;
        }
    }

    /** Alles zu einem Spieler vergessen. */
    public void forget(UUID player) {
        clearBack(player);
        clearDeath(player);
    }
}
