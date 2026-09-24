package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Level;

import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;

/**
 * Zeigt jedem Spieler die Uhrzeit der Spielwelt in der Actionbar (über der Schnellzugriffsleiste).
 * Standardmäßig für alle an; mit /uhr kann sie jeder für sich abschalten (gespeichert in clock.yml).
 *
 * <p>Andere Anzeigen benutzen dieselbe Zeile (Teleport-Aufwärmzeit, Ortssuche, Schlafen). Sie melden
 * sich über {@link #suppress(Player, long)} kurz an, damit die Uhr ihnen nicht dazwischenfunkt.
 */
public final class ClockTask implements Runnable {

    public static final String DEFAULT_FORMAT =
            "<symbol> <white><zeit></white> <dark_gray>•</dark_gray> <gray>Tag <white><tag></white></gray>";
    private static final String DEFAULT_DAY = "<yellow>☀</yellow>";
    private static final String DEFAULT_NIGHT = "<aqua>☾</aqua>";
    private static final String DEFAULT_RAIN = "<gray>☂</gray>";
    private static final String DEFAULT_STORM = "<red>☈</red>";

    private final CompanionPlugin plugin;
    /** Bis wann (System.currentTimeMillis) eine andere Anzeige Vorrang hat. */
    private final Map<UUID, Long> quiet = new HashMap<>();
    private YamlConfiguration data = new YamlConfiguration();

    public ClockTask(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    private File file() {
        return new File(plugin.getDataFolder(), "clock.yml");
    }

    public void load() {
        data = YamlConfiguration.loadConfiguration(file());
    }

    public boolean enabled() {
        return plugin.settings().raw().getBoolean("features.clock", true);
    }

    /** Hat der Spieler die Uhr an? Ohne eigene Entscheidung gilt der Standard aus der Konfiguration. */
    public boolean isOn(UUID player) {
        return data.getBoolean("players." + player,
                plugin.settings().raw().getBoolean("clock.default_on", true));
    }

    /** Schaltet die Uhr für diesen Spieler um. Rückgabe: neuer Zustand. */
    public boolean toggle(Player player) {
        boolean now = !isOn(player.getUniqueId());
        set(player, now);
        return now;
    }

    public void set(Player player, boolean on) {
        data.set("players." + player.getUniqueId(), on);
        save();
        if (!on) {
            player.sendActionBar(Component.empty());
        } else {
            show(player);
        }
    }

    /**
     * Lässt die Uhr für diesen Spieler kurz aussetzen, damit eine andere Meldung in der Actionbar
     * stehen bleibt. Wird von Teleport, Ortssuche und Schlafanzeige aufgerufen.
     */
    public void suppress(Player player, long millis) {
        quiet.put(player.getUniqueId(), System.currentTimeMillis() + Math.max(0L, millis));
    }

    public void forget(Player player) {
        quiet.remove(player.getUniqueId());
    }

    @Override
    public void run() {
        if (!enabled()) {
            return;
        }
        long now = System.currentTimeMillis();
        quiet.values().removeIf(until -> until <= now);
        for (Player p : Bukkit.getOnlinePlayers()) {
            Long until = quiet.get(p.getUniqueId());
            if (until != null && until > now) {
                continue;
            }
            if (!isOn(p.getUniqueId())) {
                continue;
            }
            show(p);
        }
    }

    private void show(Player player) {
        if (!enabled() || !isOn(player.getUniqueId())) {
            return;
        }
        player.sendActionBar(line(player));
    }

    /** Baut die Anzeige für diesen Spieler. */
    private Component line(Player player) {
        World world = timeWorld(player);
        long time = world.getTime();
        String format = plugin.settings().raw().getString("clock.format", DEFAULT_FORMAT);
        if (format == null || format.isBlank()) {
            format = DEFAULT_FORMAT;
        }
        return Msg.mm(format,
                Msg.text("zeit", clock(time)),
                Msg.number("tag", world.getFullTime() / 24000L + 1L),
                Msg.text("welt", world.getName()),
                net.kyori.adventure.text.minimessage.tag.resolver.Placeholder.parsed("symbol", symbol(world, time)));
    }

    /**
     * Nether und Ende haben keinen sichtbaren Tagesablauf – dort zeigt die Uhr die Zeit der Hauptwelt,
     * damit sie nicht stehen bleibt oder Unsinn anzeigt.
     */
    private World timeWorld(Player player) {
        World w = player.getWorld();
        if (w.getEnvironment() == World.Environment.NORMAL) {
            return w;
        }
        java.util.List<World> worlds = Bukkit.getWorlds();
        return worlds.isEmpty() ? w : worlds.get(0);
    }

    private String symbol(World world, long time) {
        var raw = plugin.settings().raw();
        boolean night = time >= 13000L && time < 23000L;
        if (world.isThundering()) {
            return str(raw.getString("clock.symbol_storm", DEFAULT_STORM), DEFAULT_STORM);
        }
        if (world.hasStorm()) {
            return str(raw.getString("clock.symbol_rain", DEFAULT_RAIN), DEFAULT_RAIN);
        }
        return night ? str(raw.getString("clock.symbol_night", DEFAULT_NIGHT), DEFAULT_NIGHT)
                : str(raw.getString("clock.symbol_day", DEFAULT_DAY), DEFAULT_DAY);
    }

    private static String str(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }

    /** Weltzeit (0..23999) als Uhrzeit; 0 entspricht 6:00 Uhr morgens. */
    public static String clock(long time) {
        long t = ((time % 24000L) + 24000L) % 24000L;
        long hours = ((t / 1000L) + 6L) % 24L;
        long minutes = (t % 1000L) * 60L / 1000L;
        return String.format(Locale.ROOT, "%02d:%02d", hours, minutes);
    }

    private void save() {
        try {
            if (!plugin.getDataFolder().isDirectory() && !plugin.getDataFolder().mkdirs()) {
                plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden.");
                return;
            }
            data.save(file());
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "clock.yml konnte nicht gespeichert werden", ex);
        }
    }
}
