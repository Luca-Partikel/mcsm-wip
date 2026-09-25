package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Level;

import io.papermc.paper.scoreboard.numbers.NumberFormat;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Statistic;
import org.bukkit.World;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.scoreboard.Criteria;
import org.bukkit.scoreboard.DisplaySlot;
import org.bukkit.scoreboard.Objective;
import org.bukkit.scoreboard.Score;
import org.bukkit.scoreboard.Scoreboard;
import org.bukkit.scoreboard.ScoreboardManager;

/**
 * Seitenleiste (Scoreboard) mit Servername, Spielerzahl, eigener Spielzeit, Koordinaten, TPS und
 * Weltzeit. Aktualisierung im Sekundentakt und nur für Spieler, die die Leiste eingeschaltet haben;
 * wer sie aus hat, behält sein bisheriges Scoreboard. Zustand je Spieler in sidebar.yml.
 */
public final class SidebarManager implements Runnable {

    public static final String DEFAULT_TITLE =
            "<gray>✦</gray> <gradient:#3ddc84:#8ff0b4><bold><server_name></bold></gradient> <gray>✦</gray>";
    /** Dünne Trennlinie in der Breite der Leiste. */
    private static final String TRENNER = "<dark_gray><strikethrough>              </strikethrough></dark_gray>";
    private static final String OBJECTIVE = "mcsm_sidebar";
    /** Unsichtbare, eindeutige Kennungen der Zeilen (reine Farbcodes). */
    private static final String[] SLOTS = new String[16];

    static {
        for (int i = 0; i < SLOTS.length; i++) {
            SLOTS[i] = "§" + Character.forDigit(i, 16) + "§r";
        }
    }

    private final CompanionPlugin plugin;
    private final ServerMetrics metrics;
    /** Eigene Scoreboards der Spieler mit eingeschalteter Leiste. */
    private final Map<UUID, Scoreboard> boards = new HashMap<>();
    private YamlConfiguration data = new YamlConfiguration();

    public SidebarManager(CompanionPlugin plugin, ServerMetrics metrics) {
        this.plugin = plugin;
        this.metrics = metrics;
    }

    private File file() {
        return new File(plugin.getDataFolder(), "sidebar.yml");
    }

    public void load() {
        data = YamlConfiguration.loadConfiguration(file());
    }

    public boolean enabled() {
        return plugin.settings().raw().getBoolean("features.sidebar", true);
    }

    /** Hat der Spieler die Leiste an? Ohne eigene Entscheidung gilt der Standard aus der Konfiguration. */
    public boolean isOn(UUID player) {
        return data.getBoolean("players." + player,
                plugin.settings().raw().getBoolean("sidebar.default_on", false));
    }

    /** Schaltet die Leiste um und wendet die Änderung sofort an. Rückgabe: neuer Zustand. */
    public boolean toggle(Player player) {
        boolean now = !isOn(player.getUniqueId());
        data.set("players." + player.getUniqueId(), now);
        save();
        apply(player, now);
        return now;
    }

    /** Setzt die Leiste ausdrücklich auf an oder aus. */
    public void set(Player player, boolean on) {
        data.set("players." + player.getUniqueId(), on);
        save();
        apply(player, on);
    }

    /** Aktualisierung im Sekundentakt. */
    @Override
    public void run() {
        if (!enabled()) {
            clearAll();
            return;
        }
        for (Player p : Bukkit.getOnlinePlayers()) {
            apply(p, isOn(p.getUniqueId()));
        }
        for (Iterator<UUID> it = boards.keySet().iterator(); it.hasNext();) {
            Player p = Bukkit.getPlayer(it.next());
            if (p == null || !p.isOnline()) {
                it.remove();
            }
        }
    }

    /** Entfernt alle eigenen Leisten (Plugin-Ende oder Funktion abgeschaltet). */
    public void clearAll() {
        for (Map.Entry<UUID, Scoreboard> e : new HashMap<>(boards).entrySet()) {
            Player p = Bukkit.getPlayer(e.getKey());
            if (p != null && p.isOnline()) {
                restore(p, e.getValue());
            }
        }
        boards.clear();
    }

    private void apply(Player player, boolean on) {
        if (!on) {
            Scoreboard own = boards.remove(player.getUniqueId());
            if (own != null) {
                restore(player, own);
            }
            return;
        }
        Scoreboard board = boards.get(player.getUniqueId());
        if (board == null || !board.equals(player.getScoreboard())) {
            ScoreboardManager manager = Bukkit.getScoreboardManager();
            board = manager.getNewScoreboard();
            boards.put(player.getUniqueId(), board);
            player.setScoreboard(board);
        }
        draw(player, board);
    }

    /** Nur das eigene Brett zurücknehmen – ein fremdes Scoreboard bleibt unangetastet. */
    private void restore(Player player, Scoreboard own) {
        if (!own.equals(player.getScoreboard())) {
            return;
        }
        ScoreboardManager manager = Bukkit.getScoreboardManager();
        player.setScoreboard(manager.getMainScoreboard());
    }

    private void draw(Player player, Scoreboard board) {
        Config cfg = plugin.settings();
        TagResolver tags = TagResolver.resolver(
                Msg.text("server_name", cfg.serverName),
                Msg.name("name", player));
        Component title = Msg.mm(title(), tags);

        Objective obj = board.getObjective(OBJECTIVE);
        if (obj == null) {
            obj = board.registerNewObjective(OBJECTIVE, Criteria.DUMMY, title);
            obj.setDisplaySlot(DisplaySlot.SIDEBAR);
        } else {
            obj.displayName(title);
        }
        obj.numberFormat(NumberFormat.blank());

        List<String> lines = lines(player);
        int count = Math.min(lines.size(), SLOTS.length);
        for (int i = 0; i < count; i++) {
            Score score = obj.getScore(SLOTS[i]);
            score.setScore(count - i);
            score.customName(Msg.mm(lines.get(i)));
        }
        for (int i = count; i < SLOTS.length; i++) {
            board.resetScores(SLOTS[i]);
        }
    }

    private String title() {
        String v = plugin.settings().raw().getString("sidebar.title", DEFAULT_TITLE);
        return v == null ? DEFAULT_TITLE : v;
    }

    /** Inhalt der Leiste, oberste Zeile zuerst. */
    private List<String> lines(Player player) {
        World world = player.getWorld();
        Location loc = player.getLocation();
        // Aufbau: dünne Trennlinie, dann Blöcke mit Überschrift in Grün und Wert in Weiß.
        // Leerzeilen zwischen den Blöcken geben Luft – das Ganze soll wie eine Karte wirken,
        // nicht wie eine Liste aus Schlüssel und Wert.
        List<String> out = new ArrayList<>();
        out.add(TRENNER);
        out.add(" <green>❖</green> <gray>Welt</gray>");
        out.add("  <white>" + weltName(world) + "</white> <dark_gray>·</dark_gray> <white>"
                + clock(world.getTime()) + "</white> " + wetter(world));
        out.add("  <dark_gray>x</dark_gray> <white>" + loc.getBlockX() + "</white>"
                + " <dark_gray>y</dark_gray> <white>" + loc.getBlockY() + "</white>"
                + " <dark_gray>z</dark_gray> <white>" + loc.getBlockZ() + "</white>");
        out.add("");
        out.add(" <green>❖</green> <gray>Du</gray>");
        out.add("  <white>" + playtime(player) + "</white> <dark_gray>gespielt</dark_gray>");
        out.add("");
        out.add(" <green>❖</green> <gray>Server</gray>");
        out.add("  <white>" + plugin.vanish().visibleOnline() + "</white><dark_gray>/</dark_gray><gray>"
                + Bukkit.getMaxPlayers() + "</gray> <dark_gray>online</dark_gray>");
        out.add("  " + metrics.tpsColored() + " <dark_gray>TPS</dark_gray>");
        out.add(TRENNER);
        out.add("<dark_gray>" + plugin.settings().sponsorText + "</dark_gray>");
        return out;
    }

    /** Weltname ohne technische Zusätze: „world_nether" wird zu „Nether". */
    private static String weltName(World world) {
        switch (world.getEnvironment()) {
            case NETHER: return "Nether";
            case THE_END: return "Ende";
            default: break;
        }
        String n = world.getName();
        return n.length() > 14 ? n.substring(0, 13) + "…" : n;
    }

    /** Kleines Zeichen für Wetter und Tageszeit. */
    private static String wetter(World world) {
        if (world.isThundering()) {
            return "<red>☈</red>";
        }
        if (world.hasStorm()) {
            return "<aqua>☂</aqua>";
        }
        long t = world.getTime();
        return t >= 13000L && t < 23000L ? "<aqua>☾</aqua>" : "<yellow>☀</yellow>";
    }

    /** Gespielte Zeit des Spielers als "4h 12m". */
    private static String playtime(Player player) {
        long seconds = Math.max(0L, player.getStatistic(Statistic.PLAY_ONE_MINUTE) / 20L);
        long h = seconds / 3600L;
        long m = (seconds % 3600L) / 60L;
        return h > 0L ? h + "h " + m + "m" : m + "m";
    }

    /** Weltzeit (0..23999) als Uhrzeit. */
    private static String clock(long time) {
        long hours = ((time / 1000L) + 6L) % 24L;
        long minutes = (time % 1000L) * 60L / 1000L;
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
            plugin.getLogger().log(Level.WARNING, "sidebar.yml konnte nicht gespeichert werden", ex);
        }
    }
}
