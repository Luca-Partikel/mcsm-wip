package de.mcsm.companion;

import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerBedEnterEvent;
import org.bukkit.event.player.PlayerBedLeaveEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.world.TimeSkipEvent;

/**
 * Meldet dem {@link SleepManager}, wenn sich jemand hinlegt, aufsteht oder den Server verlässt,
 * damit der Fortschritt sofort und nicht erst beim nächsten Durchlauf stimmt.
 */
public final class SleepListener implements Listener {

    private final CompanionPlugin plugin;
    private final SleepManager sleep;

    public SleepListener(CompanionPlugin plugin, SleepManager sleep) {
        this.plugin = plugin;
        this.sleep = sleep;
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBedEnter(PlayerBedEnterEvent event) {
        if (!event.enterAction().canSleep().success()) {
            return;
        }
        // Erst im nächsten Tick liegt der Spieler wirklich im Bett.
        later(event.getPlayer().getWorld());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBedLeave(PlayerBedLeaveEvent event) {
        later(event.getPlayer().getWorld());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        Player p = event.getPlayer();
        later(p.getWorld());
    }

    /** Während des eigenen weichen Übergangs darf Vanilla die Zeit nicht zusätzlich vorstellen. */
    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onTimeSkip(TimeSkipEvent event) {
        if (sleep.enabled() && sleep.isSkipping(event.getWorld())) {
            event.setCancelled(true);
        }
    }

    private void later(World world) {
        if (!sleep.enabled() || !plugin.isEnabled()) {
            return;       // beim Plugin-Ende (Abschieds-Kick in onDisable) gibt es keinen nächsten Tick mehr
        }
        Bukkit.getScheduler().runTask(plugin, () -> sleep.refresh(world));
    }
}
