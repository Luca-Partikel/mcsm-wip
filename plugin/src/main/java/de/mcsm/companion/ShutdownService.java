package de.mcsm.companion;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.logging.Level;

import net.kyori.adventure.key.Key;
import net.kyori.adventure.sound.Sound;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import net.kyori.adventure.title.Title;
import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.event.player.PlayerKickEvent;
import org.bukkit.scheduler.BukkitTask;

/**
 * Herunterfahren mit Ansage und das freundliche Trennen beim Plugin-Ende.
 *
 * <p>Steht neben dem Neustart-Countdown aus {@link AdminTools}: /admin restart startet den
 * Server über den Manager neu, /mcsmstop fährt ihn herunter. Beide laufen unabhängig
 * voneinander; der Manager ruft immer /mcsmstop auf.</p>
 */
public final class ShutdownService {

    /** Obergrenze, damit kein Countdown über Stunden läuft. */
    public static final int MAX_SECONDS = 3600;
    /** Sekundenmarken, an denen ohne eigene Konfiguration gewarnt wird. */
    private static final List<Integer> DEFAULT_MARKS = List.of(60, 30, 15, 10, 5, 4, 3, 2, 1);
    /** Unaufdringlicher Ton zur Warnung, ein tieferer kurz vor Schluss. */
    private static final Key SOUND_MARK = Key.key("minecraft:block.note_block.pling");
    private static final Key SOUND_LAST = Key.key("minecraft:block.note_block.bass");
    /** Ab dieser Restzeit wird der zweite Ton benutzt. */
    private static final int LAST_SECONDS = 5;

    public static final String DEFAULT_CHAT =
            "<gold>Der Server wird in <white><sekunden></white> heruntergefahren.</gold> "
            + "<gray>Grund: <white><grund></white></gray>";
    public static final String DEFAULT_TITLE = "<gold><bold>Herunterfahren</bold></gold>";
    public static final String DEFAULT_SUBTITLE = "<gray>in <white><sekunden></white></gray>";
    public static final String DEFAULT_CANCEL =
            "<green>Das Herunterfahren wurde abgebrochen.</green> <gray>Alles bleibt, wie es ist.</gray>";
    public static final String DEFAULT_REASON = "Wartung";

    /** Abschiedstext beim geplanten Herunterfahren (/mcsmstop). */
    public static final List<String> DEFAULT_KICK = List.of(
            "<gold><bold><server_name></bold></gold>",
            "",
            "<white>Der Server wird jetzt heruntergefahren.</white>",
            "<gray>Grund: <white><grund></white></gray>",
            "<gray>Danke fürs Spielen – bis bald!</gray>");

    /** Abschiedstext beim Plugin-Ende, also bei jedem Stopp (Konsole, Manager, Absturzfrei). */
    public static final List<String> DEFAULT_DISABLE_KICK = List.of(
            "<gold><bold><server_name></bold></gold>",
            "",
            "<white>Der Server wird gerade beendet.</white>",
            "<gray>Danke fürs Spielen – bis bald!</gray>");

    private final CompanionPlugin plugin;

    private BukkitTask task;
    private int left;
    private String reason = DEFAULT_REASON;

    public ShutdownService(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    /** Ist das Modul eingeschaltet? */
    public boolean featureOn() {
        return plugin.settings().raw().getBoolean("features.shutdown", true);
    }

    public boolean running() {
        return task != null;
    }

    public int secondsLeft() {
        return left;
    }

    public String reason() {
        return reason;
    }

    // ------------------------------------------------------------------ Countdown

    /**
     * Startet den Countdown. 0 Sekunden oder ein leerer Server bedeuten sofortiges
     * Herunterfahren; ein bereits laufender Countdown wird ersetzt.
     */
    public void start(int seconds, String why) {
        stopTask();
        reason = why == null || why.isBlank() ? defaultReason() : why.trim();
        int secs = Math.max(0, Math.min(MAX_SECONDS, seconds));
        if (secs == 0 || Bukkit.getOnlinePlayers().isEmpty()) {
            // Niemand da, der eine Ansage bräuchte – der Manager ruft den Befehl immer auf.
            shutdownNow();
            return;
        }
        left = secs;
        announce(left);
        task = Bukkit.getScheduler().runTaskTimer(plugin, this::tick, 20L, 20L);
    }

    /** Bricht einen laufenden Countdown ab; true, wenn einer lief. */
    public boolean cancel() {
        if (task == null) {
            return false;
        }
        stopTask();
        Bukkit.broadcast(Msg.prefixed(str("shutdown.cancel_format", DEFAULT_CANCEL), tags(0)));
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.clearTitle();
        }
        return true;
    }

    private void stopTask() {
        if (task != null) {
            task.cancel();
            task = null;
        }
        left = 0;
    }

    /** Wird jede Sekunde aufgerufen, solange ein Countdown läuft. */
    private void tick() {
        left--;
        if (left <= 0) {
            stopTask();
            shutdownNow();
            return;
        }
        if (marks().contains(Integer.valueOf(left))) {
            announce(left);
        }
    }

    private List<Integer> marks() {
        List<Integer> configured = plugin.settings().raw().getIntegerList("shutdown.marks");
        return configured.isEmpty() ? DEFAULT_MARKS : configured;
    }

    /** Chatmeldung, Titel und Ton für alle Spieler. */
    private void announce(int seconds) {
        TagResolver tags = tags(seconds);
        Bukkit.broadcast(Msg.prefixed(str("shutdown.chat_format", DEFAULT_CHAT), tags));
        Title title = Title.title(
                Msg.mm(str("shutdown.title", DEFAULT_TITLE), tags),
                Msg.mm(str("shutdown.subtitle", DEFAULT_SUBTITLE), tags),
                Title.Times.times(Duration.ofMillis(200L), Duration.ofMillis(1400L), Duration.ofMillis(400L)));
        boolean sound = plugin.settings().raw().getBoolean("shutdown.sound", true);
        Key key = seconds <= LAST_SECONDS ? SOUND_LAST : SOUND_MARK;
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.showTitle(title);
            if (sound) {
                p.playSound(Sound.sound(key, Sound.Source.MASTER, 0.7F, seconds <= LAST_SECONDS ? 0.8F : 1.4F));
            }
        }
    }

    // ------------------------------------------------------------------ Herunterfahren

    /** Sichern, alle Spieler freundlich trennen und den Server beenden. */
    public void shutdownNow() {
        stopTask();
        long ms = saveAll();
        plugin.getLogger().info("Herunterfahren angefordert (" + reason + ") – Welten in " + ms + " ms gesichert.");
        int n = kickAll(lines("shutdown.kick_message", DEFAULT_KICK));
        if (n > 0) {
            plugin.getLogger().info(n + " Spieler wurden mit Abschiedstext getrennt.");
        }
        Bukkit.shutdown();
    }

    /** Spieler- und Weltdaten sichern; liefert die benötigte Zeit in Millisekunden. */
    private long saveAll() {
        long t0 = System.nanoTime();
        try {
            Bukkit.savePlayers();
            for (World w : Bukkit.getWorlds()) {
                w.save();
            }
        } catch (RuntimeException ex) {
            plugin.getLogger().log(Level.WARNING, "Sichern vor dem Herunterfahren fehlgeschlagen", ex);
        }
        return Math.max(1L, (System.nanoTime() - t0) / 1_000_000L);
    }

    /**
     * Trennt beim Plugin-Ende alle noch verbundenen Spieler mit einer freundlichen Nachricht,
     * statt sie in einen harten Verbindungsabbruch laufen zu lassen.
     *
     * <p>Wird aus {@code CompanionPlugin.onDisable()} aufgerufen. Dort ist kein Scheduler mehr
     * erlaubt, darum läuft alles unmittelbar und jeder Spieler einzeln in try/catch. Standardmäßig
     * greift die Methode nur, wenn der Server wirklich stoppt – bei einem reinen Plugin-Neuladen
     * bleibt niemand auf der Strecke.</p>
     *
     * @return Anzahl der getrennten Spieler
     */
    public int disconnectAll() {
        if (!plugin.settings().raw().getBoolean("shutdown.kick_on_disable", true)) {
            return 0;
        }
        if (plugin.settings().raw().getBoolean("shutdown.kick_only_when_stopping", true) && !isStopping()) {
            return 0;
        }
        return kickAll(lines("shutdown.disable_kick_message", DEFAULT_DISABLE_KICK));
    }

    /** Fährt der Server gerade herunter? Fehlt die Auskunft, wird von "nein" ausgegangen. */
    private boolean isStopping() {
        try {
            return Bukkit.isStopping();
        } catch (RuntimeException ex) {
            return false;
        }
    }

    /** Wirft alle Spieler mit dem übergebenen Text hinaus; Fehler je Spieler werden geschluckt. */
    private int kickAll(Component message) {
        int n = 0;
        for (Player p : new ArrayList<>(Bukkit.getOnlinePlayers())) {
            try {
                p.kick(message, PlayerKickEvent.Cause.RESTART_COMMAND);
                n++;
            } catch (RuntimeException ex) {
                plugin.getLogger().log(Level.FINE, "Trennen von " + p.getName() + " fehlgeschlagen", ex);
            }
        }
        return n;
    }

    // ------------------------------------------------------------------ Texte

    private String defaultReason() {
        return str("shutdown.reason_default", DEFAULT_REASON);
    }

    private String str(String key, String def) {
        String v = plugin.settings().raw().getString(key, def);
        return v == null ? def : v;
    }

    /** Mehrzeiligen Text aus einer Liste bauen; leere Liste = mitgelieferter Standard. */
    private Component lines(String key, List<String> fallback) {
        List<String> raw = plugin.settings().raw().getStringList(key);
        List<String> use = raw.isEmpty() ? fallback : raw;
        return Msg.mm(String.join("\n", use), tags(0));
    }

    /** Platzhalter <sekunden>, <grund> und <server_name>. */
    private TagResolver tags(int seconds) {
        return TagResolver.resolver(
                Msg.text("sekunden", human(seconds)),
                Msg.number("zahl", seconds),
                Msg.text("grund", reason),
                Msg.text("server_name", plugin.settings().serverName));
    }

    /** "1:30 Minuten", "45 Sekunden" – kurze deutsche Zeitangabe. */
    public static String human(int seconds) {
        if (seconds < 60) {
            return seconds + (seconds == 1 ? " Sekunde" : " Sekunden");
        }
        int m = seconds / 60;
        int s = seconds % 60;
        if (s == 0) {
            return m + (m == 1 ? " Minute" : " Minuten");
        }
        return m + ":" + (s < 10 ? "0" : "") + s + " Minuten";
    }
}
