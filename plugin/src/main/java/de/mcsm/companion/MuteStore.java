package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.logging.Level;

import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;

/**
 * Stummschaltungen je Spieler-UUID in plugins/MCSMCompanion/mutes.yml: Grund, Ablauf und wer sie
 * verhängt hat. Geschrieben wird atomar über mutes.yml.tmp + Umbenennen (wie GraveStore).
 *
 * <p>Die Einträge werden auch aus dem Chat-Thread gelesen, deshalb liegen sie in einer
 * nebenläufigen Map. Geschrieben wird nur aus dem Haupt-Thread.</p>
 */
public final class MuteStore {

    /** Eine Stummschaltung. {@code expires == 0} bedeutet dauerhaft. */
    public static final class Mute {

        public final UUID player;
        public final String name;
        public final String reason;
        public final String source;
        /** Zeitpunkt der Verhängung in Millisekunden. */
        public final long created;
        /** Ablauf in Millisekunden; 0 = dauerhaft. */
        public final long expires;

        public Mute(UUID player, String name, String reason, String source, long created, long expires) {
            this.player = player;
            this.name = name == null || name.isBlank() ? "?" : name;
            this.reason = reason == null || reason.isBlank() ? "Kein Grund angegeben" : reason;
            this.source = source == null || source.isBlank() ? "?" : source;
            this.created = created;
            this.expires = Math.max(0L, expires);
        }

        public boolean permanent() {
            return expires <= 0L;
        }

        public boolean expired(long now) {
            return !permanent() && expires <= now;
        }

        /** Verbleibende Zeit in Millisekunden; bei dauerhaften Stummschaltungen -1. */
        public long remaining(long now) {
            return permanent() ? -1L : Math.max(0L, expires - now);
        }
    }

    private final CompanionPlugin plugin;
    private final Map<UUID, Mute> mutes = new ConcurrentHashMap<>();

    public MuteStore(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public File file() {
        return new File(plugin.getDataFolder(), "mutes.yml");
    }

    /** Liest mutes.yml; abgelaufene Einträge fallen dabei gleich weg. */
    public void load() {
        mutes.clear();
        YamlConfiguration y = YamlConfiguration.loadConfiguration(file());
        ConfigurationSection sec = y.getConfigurationSection("mutes");
        if (sec == null) {
            return;
        }
        long now = System.currentTimeMillis();
        boolean dropped = false;
        for (String key : sec.getKeys(false)) {
            UUID id;
            try {
                id = UUID.fromString(key);
            } catch (IllegalArgumentException ex) {
                plugin.getLogger().warning("mutes.yml: ungültige Spieler-UUID \"" + key + "\" übersprungen.");
                continue;
            }
            ConfigurationSection m = sec.getConfigurationSection(key);
            if (m == null) {
                continue;
            }
            Mute mute = new Mute(id, m.getString("name", "?"), m.getString("reason"),
                    m.getString("source"), m.getLong("created"), m.getLong("expires"));
            if (mute.expired(now)) {
                dropped = true;
                continue;
            }
            mutes.put(id, mute);
        }
        if (dropped) {
            save();
        }
    }

    /** Schreibt mutes.yml atomar; Fehler landen im Protokoll, nicht beim Aufrufer. */
    public void save() {
        YamlConfiguration y = new YamlConfiguration();
        for (Mute m : mutes.values()) {
            String p = "mutes." + m.player + ".";
            y.set(p + "name", m.name);
            y.set(p + "reason", m.reason);
            y.set(p + "source", m.source);
            y.set(p + "created", m.created);
            y.set(p + "expires", m.expires);
        }
        File target = file();
        File dir = target.getParentFile();
        if (dir != null && !dir.isDirectory() && !dir.mkdirs()) {
            plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden: " + dir);
            return;
        }
        Path tmp = new File(dir, "mutes.yml.tmp").toPath();
        Path dst = target.toPath();
        try {
            Files.writeString(tmp, y.saveToString(), StandardCharsets.UTF_8);
            try {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (IOException atomicFailed) {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "mutes.yml konnte nicht geschrieben werden", ex);
        }
    }

    /** Aktive Stummschaltung oder null; abgelaufene Einträge werden dabei entfernt. */
    public Mute active(UUID id) {
        if (id == null) {
            return null;
        }
        Mute m = mutes.get(id);
        if (m == null) {
            return null;
        }
        if (m.expired(System.currentTimeMillis())) {
            mutes.remove(id, m);
            return null;
        }
        return m;
    }

    public boolean isMuted(UUID id) {
        return active(id) != null;
    }

    /** Setzt oder überschreibt eine Stummschaltung und speichert sofort. */
    public void put(Mute mute) {
        mutes.put(mute.player, mute);
        save();
    }

    /** Hebt eine Stummschaltung auf; liefert den entfernten Eintrag oder null. */
    public Mute remove(UUID id) {
        if (id == null) {
            return null;
        }
        Mute m = mutes.remove(id);
        if (m != null) {
            save();
        }
        return m;
    }

    /** Alle aktiven Stummschaltungen, neueste zuerst. */
    public List<Mute> all() {
        long now = System.currentTimeMillis();
        List<Mute> out = new ArrayList<>();
        for (Mute m : mutes.values()) {
            if (!m.expired(now)) {
                out.add(m);
            }
        }
        out.sort(Comparator.comparingLong((Mute m) -> m.created).reversed());
        return out;
    }

    /** Anzahl aktiver Stummschaltungen. */
    public int size() {
        long now = System.currentTimeMillis();
        int n = 0;
        for (Mute m : mutes.values()) {
            if (!m.expired(now)) {
                n++;
            }
        }
        return n;
    }

    /** UUID zu einem gespeicherten Namen (Groß-/Kleinschreibung egal); null, wenn unbekannt. */
    public UUID byName(String name) {
        if (name == null || name.isBlank()) {
            return null;
        }
        String wanted = name.toLowerCase(Locale.ROOT);
        for (Mute m : all()) {
            if (m.name.toLowerCase(Locale.ROOT).equals(wanted)) {
                return m.player;
            }
        }
        return null;
    }

    /** Namen aller aktiv Stummgeschalteten (für die Tab-Vervollständigung). */
    public List<String> names() {
        List<String> out = new ArrayList<>();
        for (Mute m : all()) {
            out.add(m.name);
        }
        return out;
    }

    /** Entfernt abgelaufene Einträge und liefert sie zurück. */
    public List<Mute> purgeExpired() {
        long now = System.currentTimeMillis();
        List<Mute> gone = new ArrayList<>();
        for (Mute m : mutes.values()) {
            if (m.expired(now)) {
                gone.add(m);
            }
        }
        for (Mute m : gone) {
            mutes.remove(m.player, m);
        }
        if (!gone.isEmpty()) {
            save();
        }
        return gone;
    }
}
