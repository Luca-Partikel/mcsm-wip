package de.mcsm.companion;

import java.util.Locale;

import io.papermc.paper.event.player.AsyncChatEvent;
import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.player.PlayerCommandPreprocessEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerMoveEvent;
import org.bukkit.event.player.PlayerQuitEvent;

/** Meldet Aktivität an den AfkManager: Bewegung, Chat, Befehle, Interaktion, Blöcke. */
public final class AfkListener implements Listener {

    private final CompanionPlugin plugin;
    private final AfkManager afk;

    public AfkListener(CompanionPlugin plugin, AfkManager afk) {
        this.plugin = plugin;
        this.afk = afk;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        afk.touch(event.getPlayer());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        afk.forget(event.getPlayer().getUniqueId());
    }

    /** Nur echte Ortswechsel zählen – reines Umsehen lässt den AFK-Zustand bestehen. */
    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onMove(PlayerMoveEvent event) {
        if (!afk.enabled() || !event.hasChangedBlock()) {
            return;
        }
        afk.activity(event.getPlayer());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onInteract(PlayerInteractEvent event) {
        if (afk.enabled()) {
            afk.activity(event.getPlayer());
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBreak(BlockBreakEvent event) {
        if (afk.enabled()) {
            afk.activity(event.getPlayer());
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onPlace(BlockPlaceEvent event) {
        if (afk.enabled()) {
            afk.activity(event.getPlayer());
        }
    }

    /** Der Chat läuft asynchron – die Rückmeldung muss in den Servertakt zurück. */
    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onChat(AsyncChatEvent event) {
        if (!afk.enabled()) {
            return;
        }
        Player p = event.getPlayer();
        Bukkit.getScheduler().runTask(plugin, () -> {
            if (p.isOnline()) {
                afk.activity(p);
            }
        });
    }

    /** /afk selbst darf den Zustand natürlich nicht sofort wieder beenden. */
    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onCommand(PlayerCommandPreprocessEvent event) {
        if (!afk.enabled()) {
            return;
        }
        String msg = event.getMessage();
        int space = msg.indexOf(' ');
        String head = (space < 0 ? msg : msg.substring(0, space)).toLowerCase(Locale.ROOT);
        if (head.equals("/afk") || head.endsWith(":afk")) {
            return;
        }
        afk.activity(event.getPlayer());
    }
}
