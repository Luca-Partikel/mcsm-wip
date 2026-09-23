package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Level;

import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;

/**
 * Verwarnungen je Spieler-UUID in plugins/MCSMCompanion/warns.yml: Zeitstempel, Grund und wer
 * verwarnt hat. Geschrieben wird atomar über warns.yml.tmp + Umbenennen (wie GraveStore).
 */
public final class WarnStore {

    /** Eine einzelne Verwarnung. */
    public static final class Warn {

        /** Zeitpunkt in Millisekunden. */
        public final long time;
        public final String reason;
        public final String source;

        public Warn(long time, String reason, String source) {
            this.time = time;
            this.reason = reason == null || reason.isBlank() ? "Kein Grund angegeben" : reason;
            this.source = source == null || source.isBlank() ? "?" : source;
        }
    }

    private final CompanionPlugin plugin;
    /** Spieler -> Verwarnungen, älteste zuerst. */
    private final Map<UUID, List<Warn>> warns = new LinkedHashMap<>();
    /** Spieler -> zuletzt bekannter Name, damit /warns auch offline arbeitet. */
    private final Map<UUID, String> names = new LinkedHashMap<>();

    public WarnStore(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public File file() {
        return new File(plugin.getDataFolder(), "warns.yml");
    }

    public void load() {
        warns.clear();
        names.clear();
        YamlConfiguration y = YamlConfiguration.loadConfiguration(file());
        ConfigurationSection sec = y.getConfigurationSection("warns");
        if (sec == null) {
            return;
        }
        for (String key : sec.getKeys(false)) {
            UUID id;
            try {
                id = UUID.fromString(key);
            } catch (IllegalArgumentException ex) {
                plugin.getLogger().warning("warns.yml: ungültige Spieler-UUID \"" + key + "\" übersprungen.");
                continue;
            }
            ConfigurationSection w = sec.getConfigurationSection(key);
            if (w == null) {
                continue;
            }
            names.put(id, w.getString("name", "?"));
            List<Warn> list = new ArrayList<>();
            ConfigurationSection entries = w.getConfigurationSection("entries");
            if (entries != null) {
                for (String index : entries.getKeys(false)) {
                    ConfigurationSection e = entries.getConfigurationSection(index);
                    if (e != null) {
                        list.add(new Warn(e.getLong("time"), e.getString("reason"), e.getString("source")));
                    }
                }
            }
            list.sort(Comparator.comparingLong((Warn a) -> a.time));
            if (!list.isEmpty()) {
                warns.put(id, list);
            }
        }
    }

    /** Schreibt warns.yml atomar; Fehler landen im Protokoll, nicht beim Aufrufer. */
    public void save() {
        YamlConfiguration y = new YamlConfiguration();
        for (Map.Entry<UUID, List<Warn>> e : warns.entrySet()) {
            String p = "warns." + e.getKey() + ".";
            y.set(p + "name", names.getOrDefault(e.getKey(), "?"));
            int i = 0;
            for (Warn w : e.getValue()) {
                String q = p + "entries." + i + ".";
                y.set(q + "time", w.time);
                y.set(q + "reason", w.reason);
                y.set(q + "source", w.source);
                i++;
            }
        }
        File target = file();
        File dir = target.getParentFile();
        if (dir != null && !dir.isDirectory() && !dir.mkdirs()) {
            plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden: " + dir);
            return;
        }
        Path tmp = new File(dir, "warns.yml.tmp").toPath();
        Path dst = target.toPath();
        try {
            Files.writeString(tmp, y.saveToString(), StandardCharsets.UTF_8);
            try {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (IOException atomicFailed) {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "warns.yml konnte nicht geschrieben werden", ex);
        }
    }

    /** Trägt eine Verwarnung ein und liefert die neue Gesamtzahl. */
    public int add(UUID id, String name, String reason, String source) {
        List<Warn> list = warns.computeIfAbsent(id, key -> new ArrayList<>());
        list.add(new Warn(System.currentTimeMillis(), reason, source));
        if (name != null && !name.isBlank()) {
            names.put(id, name);
        }
        save();
        return list.size();
    }

    /** Alle Verwarnungen eines Spielers, älteste zuerst (nicht veränderbar). */
    public List<Warn> list(UUID id) {
        List<Warn> list = warns.get(id);
        return list == null ? Collections.emptyList() : Collections.unmodifiableList(list);
    }

    /** Anzahl der Verwarnungen; mit maxAgeMillis &gt; 0 zählen nur jüngere Einträge mit. */
    public int count(UUID id, long maxAgeMillis) {
        List<Warn> list = warns.get(id);
        if (list == null) {
            return 0;
        }
        if (maxAgeMillis <= 0L) {
            return list.size();
        }
        long oldest = System.currentTimeMillis() - maxAgeMillis;
        int n = 0;
        for (Warn w : list) {
            if (w.time >= oldest) {
                n++;
            }
        }
        return n;
    }

    /** Löscht den Verlauf eines Spielers; true, wenn es etwas zu löschen gab. */
    public boolean clear(UUID id) {
        boolean had = warns.remove(id) != null;
        if (had) {
            names.remove(id);
            save();
        }
        return had;
    }

    /** Zuletzt bekannter Name des Spielers; "?" wenn unbekannt. */
    public String nameOf(UUID id) {
        return names.getOrDefault(id, "?");
    }

    /** UUID zu einem gespeicherten Namen (Groß-/Kleinschreibung egal); null, wenn unbekannt. */
    public UUID byName(String name) {
        if (name == null || name.isBlank()) {
            return null;
        }
        String wanted = name.toLowerCase(Locale.ROOT);
        for (Map.Entry<UUID, String> e : names.entrySet()) {
            if (e.getValue() != null && e.getValue().toLowerCase(Locale.ROOT).equals(wanted)) {
                return e.getKey();
            }
        }
        return null;
    }

    /** Namen aller Spieler mit Verwarnungen (für die Tab-Vervollständigung). */
    public List<String> names() {
        List<String> out = new ArrayList<>();
        for (Map.Entry<UUID, List<Warn>> e : warns.entrySet()) {
            String n = names.get(e.getKey());
            if (n != null && !n.equals("?")) {
                out.add(n);
            }
        }
        return out;
    }

    /** Anzahl Spieler mit mindestens einer Verwarnung. */
    public int playerCount() {
        return warns.size();
    }
}
