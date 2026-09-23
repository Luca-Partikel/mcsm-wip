package de.mcsm.companion;

import java.util.Set;

import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerTeleportEvent;

/**
 * Hängt Teleportdienst und Teleportverlauf in die Spielereignisse ein: Schaden bricht die
 * Aufwärmzeit ab, jeder größere Teleport und jeder Tod werden für /back gemerkt.
 * Alle Handler laufen auf MONITOR und ändern nichts am Ereignis.
 */
public final class TeleportListener implements Listener {

    /** Ursachen, die für /back gemerkt werden. */
    private static final Set<PlayerTeleportEvent.TeleportCause> TRACKED = Set.of(
            PlayerTeleportEvent.TeleportCause.COMMAND,
            PlayerTeleportEvent.TeleportCause.PLUGIN,
            PlayerTeleportEvent.TeleportCause.ENDER_PEARL,
            PlayerTeleportEvent.TeleportCause.CONSUMABLE_EFFECT,
            PlayerTeleportEvent.TeleportCause.NETHER_PORTAL,
            PlayerTeleportEvent.TeleportCause.END_PORTAL,
            PlayerTeleportEvent.TeleportCause.END_GATEWAY);

    /** Kürzere Sprünge in derselben Welt sind für /back uninteressant. */
    private static final double MIN_DISTANCE = 8.0D;

    private final TeleportService service;
    private final TeleportHistory history;

    public TeleportListener(TeleportService service, TeleportHistory history) {
        this.service = service;
        this.history = history;
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onDamage(EntityDamageEvent event) {
        if (event.getEntity() instanceof Player player && service.isPending(player)) {
            service.onDamage(player);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onTeleport(PlayerTeleportEvent event) {
        if (!TRACKED.contains(event.getCause())) {
            return;
        }
        Location from = event.getFrom();
        Location to = event.getTo();
        World a = from.getWorld();
        World b = to.getWorld();
        if (a == null) {
            return;
        }
        if (a.equals(b) && from.distanceSquared(to) < MIN_DISTANCE * MIN_DISTANCE) {
            return;
        }
        history.rememberBack(event.getPlayer(), from);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onDeath(PlayerDeathEvent event) {
        Player victim = event.getPlayer();
        history.rememberDeath(victim, victim.getLocation());
        service.cancel(victim, null);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        service.clear(event.getPlayer());
    }
}
