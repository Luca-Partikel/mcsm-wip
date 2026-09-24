package de.mcsm.companion;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.World;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.scheduler.BukkitRunnable;

/**
 * Überspringt die Nacht, sobald ein Anteil der wachen Spieler einer Welt schläft.
 * AFK-Spieler, Zuschauer und unsichtbare Betreiber zählen nicht mit. Der Übergang läuft weich:
 * die Weltzeit wird in kleinen Schritten bis zum Sonnenaufgang vorgestellt, danach klart das
 * Wetter auf. Ereignisse liefert {@link SleepListener}.
 */
public final class SleepManager implements Runnable {

    /** Zeitfenster, in dem in einem Bett geschlafen werden kann. */
    private static final long NIGHT_START = 12542L;
    private static final long NIGHT_END = 23459L;
    /** Anzahl der Schritte des weichen Übergangs und deren Abstand in Ticks. */
    private static final int STEPS = 20;
    private static final long STEP_TICKS = 2L;

    private final CompanionPlugin plugin;
    /** Welten, deren Nacht gerade übersprungen wird (kein zweiter Anlauf, kein Vanilla-Sprung). */
    private final Set<UUID> skipping = new HashSet<>();

    public SleepManager(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public boolean enabled() {
        return plugin.settings().raw().getBoolean("features.sleep", true);
    }

    /** Läuft gerade ein Übergang in dieser Welt? */
    public boolean isSkipping(World world) {
        return skipping.contains(world.getUID());
    }

    /** Prüfung alle 20 Ticks über alle Oberwelten. */
    @Override
    public void run() {
        if (!enabled()) {
            return;
        }
        for (World w : Bukkit.getWorlds()) {
            if (w.getEnvironment() == World.Environment.NORMAL) {
                evaluate(w);
            }
        }
    }

    /** Sofortige Neubewertung, z. B. direkt nach dem Hinlegen oder Aufstehen. */
    public void refresh(World world) {
        if (enabled() && world.getEnvironment() == World.Environment.NORMAL) {
            evaluate(world);
        }
    }

    private void evaluate(World world) {
        if (skipping.contains(world.getUID())) {
            return;
        }
        boolean night = isNight(world);
        if (!night && !world.isThundering()) {
            return;
        }
        List<Player> counted = counted(world);
        if (counted.isEmpty()) {
            return;
        }
        List<Player> sleeping = new ArrayList<>();
        for (Player p : counted) {
            if (p.isSleeping()) {
                sleeping.add(p);
            }
        }
        if (sleeping.isEmpty()) {
            return;
        }
        int needed = needed(counted.size());
        if (sleeping.size() >= needed) {
            skip(world, sleeping);
        } else {
            progress(world, sleeping.size(), needed);
        }
    }

    /** Spieler, die für die Rechnung zählen: wach spielbar, nicht AFK, nicht unsichtbar. */
    private List<Player> counted(World world) {
        YamlConfiguration y = plugin.settings().raw();
        long afkMs = Math.max(10L, y.getLong("sleep.afk_seconds", 180L)) * 1000L;
        List<Player> out = new ArrayList<>();
        for (Player p : world.getPlayers()) {
            GameMode gm = p.getGameMode();
            if (gm != GameMode.SURVIVAL && gm != GameMode.ADVENTURE) {
                continue;
            }
            if (p.isSleepingIgnored() || plugin.vanish().isVanished(p)) {
                continue;
            }
            if (!p.isSleeping() && p.getIdleDuration().toMillis() >= afkMs) {
                continue;
            }
            out.add(p);
        }
        return out;
    }

    /** Benötigte Schläfer bei dieser Spielerzahl (aufgerundet, mindestens einer). */
    public int needed(int counted) {
        int percent = Math.min(100, Math.max(1, plugin.settings().raw().getInt("sleep.percent", 50)));
        return Math.max(1, (counted * percent + 99) / 100);
    }

    /** Fortschritt in der Aktionsleiste aller Spieler der Welt. */
    private void progress(World world, int sleeping, int needed) {
        if (!plugin.settings().raw().getBoolean("sleep.actionbar", true)) {
            return;
        }
        Component bar = Msg.mm("<gray>Es schlafen</gray> <white><now></white><gray>/</gray><white><need></white>"
                        + " <gray>– der Rest der Nacht wird dann übersprungen.</gray>",
                Msg.number("now", sleeping), Msg.number("need", needed));
        for (Player p : world.getPlayers()) {
            plugin.clock().suppress(p, 2500L);
            p.sendActionBar(bar);
        }
    }

    /** Startet den weichen Übergang und meldet, wer die Nacht übersprungen hat. */
    private void skip(World world, List<Player> sleeping) {
        skipping.add(world.getUID());
        YamlConfiguration y = plugin.settings().raw();
        if (y.getBoolean("sleep.announce", true)) {
            StringBuilder names = new StringBuilder();
            for (Player p : sleeping) {
                if (names.length() > 0) {
                    names.append(", ");
                }
                names.append(p.getName());
            }
            // Mehrere Schläfer brauchen den Plural, sonst steht da "Steve, Alex überspringt".
            String verb = sleeping.size() > 1 ? "überspringen" : "überspringt";
            Bukkit.broadcast(Msg.prefixed("<gray>Gute Nacht! <white><who></white> <gray><verb> die Nacht in</gray>"
                            + " <white><world></white><gray>.</gray></gray>",
                    Msg.text("who", names.toString()), Msg.text("verb", verb),
                    Msg.text("world", world.getName())));
        }
        final long from = world.getFullTime();
        final long target = ((from / 24000L) + 1L) * 24000L;
        final boolean clearWeather = y.getBoolean("sleep.clear_weather", true);
        new BukkitRunnable() {
            private int step;

            @Override
            public void run() {
                step++;
                World w = Bukkit.getWorld(world.getUID());
                if (w == null) {
                    finish(null);
                    return;
                }
                w.setFullTime(from + ((target - from) * step) / STEPS);
                if (step >= STEPS) {
                    finish(w);
                }
            }

            private void finish(World w) {
                if (w != null) {
                    w.setFullTime(target);
                    if (clearWeather) {
                        w.setStorm(false);
                        w.setThundering(false);
                    }
                    for (Player p : w.getPlayers()) {
                        plugin.clock().suppress(p, 2500L);
                        p.sendActionBar(Msg.mm("<gray>Ein neuer Tag beginnt.</gray>"));
                    }
                }
                skipping.remove(world.getUID());
                cancel();
            }
        }.runTaskTimer(plugin, STEP_TICKS, STEP_TICKS);
    }

    /** Hängen gebliebene Übergänge beim Abschalten vergessen. */
    public void shutdown() {
        skipping.clear();
    }

    private static boolean isNight(World world) {
        long t = world.getTime();
        return t >= NIGHT_START && t <= NIGHT_END;
    }
}
