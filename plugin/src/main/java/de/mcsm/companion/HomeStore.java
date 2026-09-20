package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.util.Collections;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.UUID;
import java.util.logging.Level;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;

/** Homes je Spieler-UUID in plugins/MCSMCompanion/homes.yml. */
public final class HomeStore {

    private final CompanionPlugin plugin;
    private YamlConfiguration data = new YamlConfiguration();

    public HomeStore(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    private File file() {
        return new File(plugin.getDataFolder(), "homes.yml");
    }

    public void load() {
        data = YamlConfiguration.loadConfiguration(file());
    }

    public void save() {
        try {
            if (!plugin.getDataFolder().isDirectory() && !plugin.getDataFolder().mkdirs()) {
                plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden.");
                return;
            }
            data.save(file());
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "homes.yml konnte nicht gespeichert werden", ex);
        }
    }

    private String path(UUID owner, String name) {
        return "homes." + owner + "." + name;
    }

    /** Alle Home-Namen des Spielers, alphabetisch. */
    public Set<String> names(UUID owner) {
        ConfigurationSection sec = data.getConfigurationSection("homes." + owner);
        if (sec == null) {
            return Collections.emptySet();
        }
        return new TreeMap<>(sec.getValues(false)).keySet();
    }

    public int count(UUID owner) {
        return names(owner).size();
    }

    public boolean exists(UUID owner, String name) {
        return data.isConfigurationSection(path(owner, name));
    }

    public void set(UUID owner, String name, Location loc) {
        String p = path(owner, name);
        World w = loc.getWorld();
        data.set(p + ".world", w == null ? "world" : w.getName());
        data.set(p + ".x", loc.getX());
        data.set(p + ".y", loc.getY());
        data.set(p + ".z", loc.getZ());
        data.set(p + ".yaw", (double) loc.getYaw());
        data.set(p + ".pitch", (double) loc.getPitch());
        save();
    }

    public boolean delete(UUID owner, String name) {
        if (!exists(owner, name)) {
            return false;
        }
        data.set(path(owner, name), null);
        Map<String, Object> rest = data.getConfigurationSection("homes." + owner) == null
                ? Collections.emptyMap() : data.getConfigurationSection("homes." + owner).getValues(false);
        if (rest.isEmpty()) {
            data.set("homes." + owner, null);
        }
        save();
        return true;
    }

    /** Null, wenn das Home fehlt; Location mit world == null, wenn die Welt nicht mehr existiert. */
    public Location get(UUID owner, String name) {
        String p = path(owner, name);
        if (!data.isConfigurationSection(p)) {
            return null;
        }
        World w = Bukkit.getWorld(data.getString(p + ".world", "world"));
        return new Location(w, data.getDouble(p + ".x"), data.getDouble(p + ".y"), data.getDouble(p + ".z"),
                (float) data.getDouble(p + ".yaw"), (float) data.getDouble(p + ".pitch"));
    }
}
