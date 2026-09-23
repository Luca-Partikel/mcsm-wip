package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.util.Collections;
import java.util.Set;
import java.util.TreeMap;
import java.util.logging.Level;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;

/** Serverweite Warps in plugins/MCSMCompanion/warps.yml. */
public final class WarpStore {

    private final CompanionPlugin plugin;
    private YamlConfiguration data = new YamlConfiguration();

    public WarpStore(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    private File file() {
        return new File(plugin.getDataFolder(), "warps.yml");
    }

    public void load() {
        data = YamlConfiguration.loadConfiguration(file());
    }

    /** Schreibt warps.yml; Fehler landen im Protokoll, nicht beim Aufrufer. */
    public void save() {
        try {
            File dir = plugin.getDataFolder();
            if (!dir.isDirectory() && !dir.mkdirs()) {
                plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden.");
                return;
            }
            data.save(file());
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "warps.yml konnte nicht gespeichert werden", ex);
        }
    }

    private static String path(String name) {
        return "warps." + name;
    }

    /** Alle Warp-Namen, alphabetisch. */
    public Set<String> names() {
        ConfigurationSection sec = data.getConfigurationSection("warps");
        if (sec == null) {
            return Collections.emptySet();
        }
        return new TreeMap<>(sec.getValues(false)).keySet();
    }

    public int count() {
        return names().size();
    }

    public boolean exists(String name) {
        return data.isConfigurationSection(path(name));
    }

    /** Anlegen oder überschreiben. */
    public void set(String name, Location loc, String creator) {
        String p = path(name);
        World w = loc.getWorld();
        data.set(p + ".world", w == null ? "world" : w.getName());
        data.set(p + ".x", loc.getX());
        data.set(p + ".y", loc.getY());
        data.set(p + ".z", loc.getZ());
        data.set(p + ".yaw", (double) loc.getYaw());
        data.set(p + ".pitch", (double) loc.getPitch());
        data.set(p + ".creator", creator == null ? "?" : creator);
        data.set(p + ".created", System.currentTimeMillis() / 1000L);
        save();
    }

    public boolean delete(String name) {
        if (!exists(name)) {
            return false;
        }
        data.set(path(name), null);
        save();
        return true;
    }

    /** Wer den Warp angelegt hat; "?" wenn unbekannt. */
    public String creator(String name) {
        return data.getString(path(name) + ".creator", "?");
    }

    /** Name der hinterlegten Welt, auch wenn diese nicht geladen ist. */
    public String worldName(String name) {
        return data.getString(path(name) + ".world", "world");
    }

    /** Null, wenn der Warp fehlt; Location mit world == null, wenn die Welt nicht existiert. */
    public Location get(String name) {
        String p = path(name);
        if (!data.isConfigurationSection(p)) {
            return null;
        }
        World w = Bukkit.getWorld(data.getString(p + ".world", "world"));
        return new Location(w, data.getDouble(p + ".x"), data.getDouble(p + ".y"), data.getDouble(p + ".z"),
                (float) data.getDouble(p + ".yaw"), (float) data.getDouble(p + ".pitch"));
    }
}
