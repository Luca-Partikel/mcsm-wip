package de.mcsm.companion;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

import net.kyori.adventure.title.Title;
import org.bukkit.Bukkit;
import org.bukkit.NamespacedKey;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.potion.PotionEffect;
import org.bukkit.potion.PotionEffectType;
import org.bukkit.scheduler.BukkitTask;

/**
 * Helfer für die Eingriffe des Wartungszugangs: heilen, sättigen, Flug, Unverwundbarkeit,
 * Geschwindigkeit, Welten sichern und der Neustart-Countdown mit Titel und Chatmeldung.
 */
public final class AdminTools {

    /** Sekundenmarken, an denen ohne eigene Konfiguration gewarnt wird. */
    private static final List<Integer> DEFAULT_MARKS = List.of(600, 300, 120, 60, 30, 15, 10, 5, 4, 3, 2, 1);
    /** Obergrenze für /admin restart, damit kein Countdown über Stunden läuft. */
    public static final int MAX_RESTART_SECONDS = 3600;

    private final CompanionPlugin plugin;
    /** Spieler mit eingeschalteter Unverwundbarkeit über /admin god. */
    private final Set<UUID> god = new HashSet<>();
    /** Merker in den Spielerdaten, damit der Schutz einen Neustart übersteht. */
    private final NamespacedKey key;

    private BukkitTask restartTask;
    private int restartLeft;

    public AdminTools(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.key = new NamespacedKey(plugin, "god");
    }

    // ------------------------------------------------------------------ Spielerzustand

    /** Volles Leben, volle Sättigung, kein Feuer/Frost, schädliche Effekte weg. */
    public void heal(Player t) {
        t.setHealth(AdminProfileStore.maxHealth(t));
        t.setFireTicks(0);
        t.setFreezeTicks(0);
        t.setFallDistance(0f);
        t.setRemainingAir(t.getMaximumAir());
        for (PotionEffect e : new ArrayList<>(t.getActivePotionEffects())) {
            PotionEffectType type = e.getType();
            if (type.getEffectCategory() == PotionEffectType.Category.HARMFUL) {
                t.removePotionEffect(type);
            }
        }
        feed(t);
    }

    /** Hunger und Sättigung auffüllen. */
    public void feed(Player t) {
        t.setFoodLevel(20);
        t.setSaturation(20f);
        t.setExhaustion(0f);
    }

    /** Flug umschalten; gibt den neuen Zustand zurück. */
    public boolean toggleFly(Player t) {
        boolean on = !t.getAllowFlight();
        try {
            t.setAllowFlight(on);
        } catch (IllegalArgumentException ex) {
            return t.getAllowFlight();                        // Kreativmodus: Flug bleibt an
        }
        if (!on && t.isFlying()) {
            t.setFlying(false);
        }
        return on;
    }

    /** Unverwundbarkeit umschalten; unsichtbare Betreiber bleiben in jedem Fall unverwundbar. */
    public boolean toggleGod(Player t) {
        UUID id = t.getUniqueId();
        boolean on = !god.contains(id);
        if (on) {
            god.add(id);
        } else {
            god.remove(id);
        }
        if (on) {
            t.getPersistentDataContainer().set(key, PersistentDataType.BYTE, (byte) 1);
        } else {
            t.getPersistentDataContainer().remove(key);
        }
        t.setInvulnerable(on || plugin.vanish().isVanished(t));
        return on;
    }

    public boolean hasGod(Player t) {
        return god.contains(t.getUniqueId());
    }

    /**
     * Beitritt: den gemerkten Schutz aus den Spielerdaten übernehmen. Ohne das wäre die
     * Unverwundbarkeit nach einem Neustart weiter aktiv, ohne dass /admin god davon wüsste.
     */
    public void restore(Player t) {
        Byte stored = t.getPersistentDataContainer().get(key, PersistentDataType.BYTE);
        boolean on = stored != null && stored != 0;
        if (on) {
            god.add(t.getUniqueId());
        } else {
            god.remove(t.getUniqueId());
        }
        t.setInvulnerable(on || plugin.vanish().isVanished(t));
    }

    /** Beim Verlassen des Servers nur den Merker im Speicher löschen. */
    public void forget(Player t) {
        god.remove(t.getUniqueId());
    }

    /** Stufe 1 entspricht der normalen Geschwindigkeit, Stufe 10 dem Höchstwert. */
    public static float flySpeed(int level) {
        return Math.min(1f, 0.1f * level);
    }

    public static float walkSpeed(int level) {
        return Math.min(1f, 0.2f + (level - 1) * 0.08f);
    }

    /** Setzt Flug- oder Gehgeschwindigkeit, je nachdem ob der Spieler gerade fliegt. */
    public boolean applySpeed(Player t, int level) {
        boolean flying = t.isFlying() || t.getAllowFlight();
        if (flying) {
            t.setFlySpeed(flySpeed(level));
        } else {
            t.setWalkSpeed(walkSpeed(level));
        }
        return flying;
    }

    // ------------------------------------------------------------------ Welten sichern

    /** Spieler- und Weltdaten sichern; liefert die benötigte Zeit in Millisekunden. */
    public long saveWorlds() {
        long t0 = System.nanoTime();
        Bukkit.savePlayers();
        for (World w : Bukkit.getWorlds()) {
            w.save();
        }
        return Math.max(1L, (System.nanoTime() - t0) / 1_000_000L);
    }

    // ------------------------------------------------------------------ Neustart

    public boolean restartRunning() {
        return restartTask != null;
    }

    public int restartSeconds() {
        return restartLeft;
    }

    /** Bricht einen laufenden Countdown ab; true, wenn einer lief. */
    public boolean cancelRestart() {
        if (restartTask == null) {
            return false;
        }
        restartTask.cancel();
        restartTask = null;
        restartLeft = 0;
        Bukkit.broadcast(Msg.prefixed("<green>Der Neustart wurde abgebrochen.</green>"));
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.clearTitle();
        }
        return true;
    }

    /**
     * Startet den Countdown. 0 Sekunden bedeutet sofortiges Herunterfahren.
     * Ein bereits laufender Countdown wird ersetzt.
     */
    public void startRestart(int seconds) {
        if (restartTask != null) {
            restartTask.cancel();
            restartTask = null;
        }
        if (seconds <= 0) {
            shutdownNow();
            return;
        }
        restartLeft = Math.min(MAX_RESTART_SECONDS, seconds);
        announce(restartLeft);
        restartTask = Bukkit.getScheduler().runTaskTimer(plugin, this::tick, 20L, 20L);
    }

    /** Wird jede Sekunde aufgerufen, solange ein Countdown läuft. */
    private void tick() {
        restartLeft--;
        if (restartLeft <= 0) {
            if (restartTask != null) {
                restartTask.cancel();
                restartTask = null;
            }
            shutdownNow();
            return;
        }
        if (marks().contains(restartLeft)) {
            announce(restartLeft);
        }
    }

    private List<Integer> marks() {
        List<Integer> configured = plugin.settings().raw().getIntegerList("admin.restart_marks");
        return configured.isEmpty() ? DEFAULT_MARKS : configured;
    }

    /** Chatmeldung und Titel für alle Spieler. */
    private void announce(int seconds) {
        Bukkit.broadcast(Msg.prefixed("<gold>Server-Neustart in <white><t></white>.</gold> "
                + "<gray>Bitte an einem sicheren Ort abmelden.</gray>", Msg.text("t", human(seconds))));
        Title title = Title.title(
                Msg.mm("<gold><bold>Neustart</bold></gold>"),
                Msg.mm("<gray>in <white><t></white></gray>", Msg.text("t", human(seconds))),
                Title.Times.times(Duration.ofMillis(200), Duration.ofMillis(1400), Duration.ofMillis(400)));
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.showTitle(title);
        }
    }

    private void shutdownNow() {
        restartLeft = 0;
        Bukkit.broadcast(Msg.prefixed("<red>Der Server wird jetzt neu gestartet.</red>"));
        Title title = Title.title(
                Msg.mm("<red><bold>Neustart</bold></red>"),
                Msg.mm("<gray>Bis gleich!</gray>"),
                Title.Times.times(Duration.ofMillis(200), Duration.ofSeconds(4), Duration.ofMillis(400)));
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.showTitle(title);
        }
        long ms = saveWorlds();
        plugin.getLogger().info("Neustart angefordert – Welten in " + ms + " ms gesichert.");
        Bukkit.shutdown();
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
