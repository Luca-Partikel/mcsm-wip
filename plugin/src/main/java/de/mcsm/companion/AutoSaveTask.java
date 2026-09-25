package de.mcsm.companion;

import java.util.ArrayDeque;
import java.util.Deque;

import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.world.WorldLoadEvent;

/**
 * Speichern in der Hand des Plugins statt in der von Minecraft.
 *
 * <p>Der eingebaute Autosave schreibt alle Welten im selben Tick weg – auf großen Welten ruckelt
 * es dabei spürbar. Hier wird er abgeschaltet ({@link World#setAutoSave(boolean)}) und durch ein
 * eigenes Speichern ersetzt: in festem Abstand, eine Welt nach der anderen, jeweils ein Tick
 * Abstand. Das verteilt die Last und fällt im Spiel nicht auf.
 *
 * <p>Beim Beenden des Plugins wird der eingebaute Autosave wieder eingeschaltet – wer das Plugin
 * entfernt, soll nicht mit ungespeicherten Welten dastehen.
 */
public final class AutoSaveTask implements Runnable, Listener {

    /** Kleinster sinnvoller Abstand; darunter bringt eigenes Speichern nichts. */
    private static final int MIN_MINUTEN = 1;
    private static final int MAX_MINUTEN = 180;

    private final CompanionPlugin plugin;
    private long naechster;
    private boolean vanillaAus;

    public AutoSaveTask(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public boolean enabled() {
        return plugin.settings().raw().getBoolean("autosave", true);
    }

    /** Abstand in Minuten, auf einen vernünftigen Bereich begrenzt. */
    public int minuten() {
        int v = plugin.settings().raw().getInt("autosave_minutes", 10);
        return Math.max(MIN_MINUTEN, Math.min(MAX_MINUTEN, v));
    }

    /** Soll der eingebaute Autosave abgeschaltet werden? */
    public boolean vanillaAbschalten() {
        return plugin.settings().raw().getBoolean("autosave_vanilla_aus", true);
    }

    /** Beim Start und nach jedem Neuladen der Konfiguration anwenden. */
    public void applyConfig() {
        boolean aus = enabled() && vanillaAbschalten();
        for (World w : Bukkit.getWorlds()) {
            w.setAutoSave(!aus);
        }
        vanillaAus = aus;
        naechster = System.currentTimeMillis() + minuten() * 60_000L;
        if (aus) {
            plugin.getLogger().info("Eingebautes Speichern abgeschaltet – das Plugin speichert alle "
                    + minuten() + " Minuten selbst.");
        }
    }

    /** Neu geladene Welten bekommen dieselbe Einstellung. */
    @EventHandler(priority = EventPriority.MONITOR)
    public void onWorldLoad(WorldLoadEvent event) {
        if (vanillaAus) {
            event.getWorld().setAutoSave(false);
        }
    }

    @Override
    public void run() {
        if (!enabled()) {
            return;
        }
        long jetzt = System.currentTimeMillis();
        if (jetzt < naechster) {
            return;
        }
        naechster = jetzt + minuten() * 60_000L;
        speichereVerteilt(false);
    }

    /**
     * Alle Welten speichern, je Tick eine – so entsteht kein einzelner langer Ruckler.
     *
     * @param sofort true = alles im selben Tick (nur beim Herunterfahren sinnvoll)
     */
    public void speichereVerteilt(boolean sofort) {
        Deque<World> offen = new ArrayDeque<>(Bukkit.getWorlds());
        if (offen.isEmpty()) {
            return;
        }
        if (sofort) {
            while (!offen.isEmpty()) {
                sicherSpeichern(offen.poll());
            }
            Bukkit.savePlayers();
            return;
        }
        Bukkit.getScheduler().runTaskTimer(plugin, aufgabe -> {
            World w = offen.poll();
            if (w == null) {
                Bukkit.savePlayers();
                aufgabe.cancel();
                return;
            }
            sicherSpeichern(w);
        }, 1L, 1L);
    }

    private void sicherSpeichern(World world) {
        try {
            world.save();
        } catch (RuntimeException ex) {
            plugin.getLogger().warning("Welt „" + world.getName() + "“ konnte nicht gespeichert "
                    + "werden: " + ex.getMessage());
        }
    }

    /** Beim Beenden: einmal alles sichern und den eingebauten Autosave zurückgeben. */
    public void shutdown() {
        if (!vanillaAus) {
            return;
        }
        speichereVerteilt(true);
        for (World w : Bukkit.getWorlds()) {
            w.setAutoSave(true);
        }
        vanillaAus = false;
    }
}
