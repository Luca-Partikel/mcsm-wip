package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Level;

import org.bukkit.Bukkit;
import org.bukkit.Statistic;
import org.bukkit.command.CommandSender;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;

/**
 * Spielerstatistik in plugins/MCSMCompanion/stats.yml: Spielzeit, erster Join, letztes Verlassen,
 * Tode, Kills und zurückgelegte Strecke. Werte kommen aus den Vanilla-Statistiken; für Spieler,
 * die gerade nicht online sind, gilt der zuletzt gespeicherte Stand.
 */
public final class PlayerStatsStore implements Listener, Runnable {

    /** Strecken-Statistiken in Zentimetern, die zur Gesamtstrecke addiert werden. */
    private static final Statistic[] DISTANCE = {
        Statistic.WALK_ONE_CM, Statistic.SPRINT_ONE_CM, Statistic.CROUCH_ONE_CM,
        Statistic.SWIM_ONE_CM, Statistic.WALK_ON_WATER_ONE_CM, Statistic.WALK_UNDER_WATER_ONE_CM,
        Statistic.FLY_ONE_CM, Statistic.AVIATE_ONE_CM, Statistic.CLIMB_ONE_CM,
        Statistic.FALL_ONE_CM, Statistic.BOAT_ONE_CM, Statistic.MINECART_ONE_CM,
        Statistic.HORSE_ONE_CM, Statistic.PIG_ONE_CM, Statistic.STRIDER_ONE_CM
    };

    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm");

    private final CompanionPlugin plugin;
    private YamlConfiguration data = new YamlConfiguration();
    /** Beginn der laufenden Sitzung je Spieler – Notreserve, falls die Vanilla-Statistik leer ist. */
    private final Map<UUID, Long> sessionStart = new HashMap<>();
    /** Kleingeschriebener Name -> UUID, damit /seen auch für Offline-Spieler funktioniert. */
    private final Map<String, UUID> byName = new HashMap<>();

    public PlayerStatsStore(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    private File file() {
        return new File(plugin.getDataFolder(), "stats.yml");
    }

    /** Ist die Statistik überhaupt eingeschaltet? */
    public boolean enabled() {
        return plugin.settings().raw().getBoolean("features.stats", true);
    }

    public void load() {
        data = YamlConfiguration.loadConfiguration(file());
        byName.clear();
        ConfigurationSection sec = data.getConfigurationSection("players");
        if (sec != null) {
            for (String key : sec.getKeys(false)) {
                String name = sec.getString(key + ".name");
                if (name == null || name.isBlank()) {
                    continue;
                }
                try {
                    byName.put(name.toLowerCase(Locale.ROOT), UUID.fromString(key));
                } catch (IllegalArgumentException ignored) {
                    // beschädigter Eintrag – überspringen
                }
            }
        }
        for (Player p : Bukkit.getOnlinePlayers()) {
            begin(p);
        }
    }

    public void save() {
        try {
            if (!plugin.getDataFolder().isDirectory() && !plugin.getDataFolder().mkdirs()) {
                plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden.");
                return;
            }
            data.save(file());
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "stats.yml konnte nicht gespeichert werden", ex);
        }
    }

    /** Alle Online-Spieler abgleichen und die Datei schreiben (Zeitgeber und Plugin-Ende). */
    @Override
    public void run() {
        if (!enabled()) {
            return;
        }
        for (Player p : Bukkit.getOnlinePlayers()) {
            sync(p);
        }
        save();
    }

    /** Beim Plugin-Ende: letzten Stand sichern. */
    public void shutdown() {
        for (Player p : Bukkit.getOnlinePlayers()) {
            sync(p);
        }
        save();
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        begin(event.getPlayer());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        Player p = event.getPlayer();
        sync(p);
        data.set(base(p.getUniqueId()) + ".last_quit", System.currentTimeMillis());
        sessionStart.remove(p.getUniqueId());
        save();
    }

    private void begin(Player p) {
        UUID id = p.getUniqueId();
        sessionStart.put(id, System.currentTimeMillis());
        byName.put(p.getName().toLowerCase(Locale.ROOT), id);
        String b = base(id);
        data.set(b + ".name", p.getName());
        if (!data.isSet(b + ".first_join")) {
            long first = p.getFirstPlayed();
            data.set(b + ".first_join", first > 0L ? first : System.currentTimeMillis());
        }
    }

    /** Übernimmt den aktuellen Stand eines Online-Spielers in die Datei (ohne zu speichern). */
    public void sync(Player p) {
        UUID id = p.getUniqueId();
        String b = base(id);
        data.set(b + ".name", p.getName());
        if (!data.isSet(b + ".first_join")) {
            long first = p.getFirstPlayed();
            data.set(b + ".first_join", first > 0L ? first : System.currentTimeMillis());
        }
        data.set(b + ".play_ms", livePlayMillis(p));
        data.set(b + ".deaths", stat(p, Statistic.DEATHS));
        data.set(b + ".player_kills", stat(p, Statistic.PLAYER_KILLS));
        data.set(b + ".mob_kills", stat(p, Statistic.MOB_KILLS));
        data.set(b + ".distance_cm", liveDistanceCm(p));
        byName.put(p.getName().toLowerCase(Locale.ROOT), id);
    }

    private static String base(UUID id) {
        return "players." + id;
    }

    private static int stat(Player p, Statistic what) {
        try {
            return Math.max(0, p.getStatistic(what));
        } catch (IllegalArgumentException | IllegalStateException ex) {
            return 0;
        }
    }

    private long livePlayMillis(Player p) {
        long fromVanilla = (long) stat(p, Statistic.PLAY_ONE_MINUTE) * 50L;
        long stored = data.getLong(base(p.getUniqueId()) + ".play_ms", 0L);
        Long start = sessionStart.get(p.getUniqueId());
        long own = stored + (start == null ? 0L : Math.max(0L, System.currentTimeMillis() - start));
        return Math.max(fromVanilla, own);
    }

    private static long liveDistanceCm(Player p) {
        long sum = 0L;
        for (Statistic s : DISTANCE) {
            sum += stat(p, s);
        }
        return sum;
    }

    // ---------------------------------------------------------------- Abfragen

    /** UUID zu einem Namen – erst online, dann aus der Datei. Null, wenn unbekannt. */
    public UUID resolve(String name) {
        if (name == null || name.isBlank()) {
            return null;
        }
        Player online = Bukkit.getPlayerExact(name);
        if (online != null) {
            return online.getUniqueId();
        }
        return byName.get(name.toLowerCase(Locale.ROOT));
    }

    /** Zuletzt bekannter Name, notfalls die UUID als Text. */
    public String nameOf(UUID id) {
        Player online = Bukkit.getPlayer(id);
        if (online != null) {
            return online.getName();
        }
        String stored = data.getString(base(id) + ".name");
        return stored == null ? id.toString() : stored;
    }

    /**
     * UUID zu einem Namen, wobei unsichtbare Betreiber für Unbefugte verborgen bleiben.
     * Null, wenn der Name unbekannt oder für den Absender nicht sichtbar ist.
     */
    public UUID resolveVisible(CommandSender viewer, String name) {
        if (name == null || name.isBlank()) {
            return null;
        }
        Player online = Bukkit.getPlayerExact(name);
        if (online != null) {
            return plugin.findVisible(viewer, name) == null ? null : online.getUniqueId();
        }
        UUID id = byName.get(name.toLowerCase(Locale.ROOT));
        if (id == null) {
            return null;
        }
        Player still = Bukkit.getPlayer(id);
        if (still != null && plugin.vanish().isVanished(still) && !plugin.canSeeVanished(viewer)) {
            return null;
        }
        return id;
    }

    /** Alle bekannten Namen für die Tab-Vervollständigung, ohne unsichtbare Betreiber. */
    public List<String> knownNames(CommandSender viewer) {
        List<String> out = new ArrayList<>();
        ConfigurationSection sec = data.getConfigurationSection("players");
        if (sec == null) {
            return out;
        }
        boolean all = plugin.canSeeVanished(viewer);
        for (String key : sec.getKeys(false)) {
            String name = sec.getString(key + ".name");
            if (name == null || name.isBlank()) {
                continue;
            }
            if (!all) {
                Player online = Bukkit.getPlayerExact(name);
                if (online != null && plugin.vanish().isVanished(online)) {
                    continue;
                }
            }
            out.add(name);
        }
        return out;
    }

    public long playMillis(UUID id) {
        Player online = Bukkit.getPlayer(id);
        if (online != null) {
            return livePlayMillis(online);
        }
        return Math.max(0L, data.getLong(base(id) + ".play_ms", 0L));
    }

    public long firstJoin(UUID id) {
        long stored = data.getLong(base(id) + ".first_join", 0L);
        if (stored > 0L) {
            return stored;
        }
        Player online = Bukkit.getPlayer(id);
        return online == null ? 0L : online.getFirstPlayed();
    }

    /** Zeitpunkt des letzten Verlassens; 0, wenn nie gespeichert. */
    public long lastQuit(UUID id) {
        return Math.max(0L, data.getLong(base(id) + ".last_quit", 0L));
    }

    public int deaths(UUID id) {
        Player online = Bukkit.getPlayer(id);
        return online != null ? stat(online, Statistic.DEATHS) : data.getInt(base(id) + ".deaths", 0);
    }

    public int playerKills(UUID id) {
        Player online = Bukkit.getPlayer(id);
        return online != null ? stat(online, Statistic.PLAYER_KILLS) : data.getInt(base(id) + ".player_kills", 0);
    }

    public int mobKills(UUID id) {
        Player online = Bukkit.getPlayer(id);
        return online != null ? stat(online, Statistic.MOB_KILLS) : data.getInt(base(id) + ".mob_kills", 0);
    }

    /** Zurückgelegte Strecke in Metern. */
    public long distanceMeters(UUID id) {
        Player online = Bukkit.getPlayer(id);
        long cm = online != null ? liveDistanceCm(online) : data.getLong(base(id) + ".distance_cm", 0L);
        return cm / 100L;
    }

    /** Beginn der laufenden Sitzung; 0, wenn der Spieler offline ist. */
    public long sessionStart(UUID id) {
        Long v = sessionStart.get(id);
        return v == null ? 0L : v;
    }

    // ---------------------------------------------------------------- Formatierung

    /** Deutsche Dauer, z. B. "3 Std 12 Min", "2 Tage 5 Std" oder "45 Sek". */
    public static String duration(long millis) {
        long sec = Math.max(0L, millis) / 1000L;
        long days = sec / 86400L;
        long hours = (sec % 86400L) / 3600L;
        long minutes = (sec % 3600L) / 60L;
        StringBuilder sb = new StringBuilder(24);
        if (days > 0L) {
            sb.append(days).append(days == 1L ? " Tag" : " Tage");
        }
        if (hours > 0L) {
            if (sb.length() > 0) {
                sb.append(' ');
            }
            sb.append(hours).append(" Std");
        }
        if (minutes > 0L && days == 0L) {
            if (sb.length() > 0) {
                sb.append(' ');
            }
            sb.append(minutes).append(" Min");
        }
        if (sb.length() == 0) {
            sb.append(sec % 60L).append(" Sek");
        }
        return sb.toString();
    }

    /** "vor 3 Std 12 Min" bzw. "gerade eben". */
    public static String ago(long timestamp) {
        if (timestamp <= 0L) {
            return "unbekannt";
        }
        long diff = System.currentTimeMillis() - timestamp;
        if (diff < 60_000L) {
            return "gerade eben";
        }
        return "vor " + duration(diff);
    }

    /** Zeitpunkt als "23.09.2026 14:05"; leer, wenn unbekannt. */
    public static String stamp(long timestamp) {
        if (timestamp <= 0L) {
            return "unbekannt";
        }
        try {
            return STAMP.format(Instant.ofEpochMilli(timestamp).atZone(ZoneId.systemDefault()));
        } catch (RuntimeException ex) {
            return "unbekannt";
        }
    }
}
