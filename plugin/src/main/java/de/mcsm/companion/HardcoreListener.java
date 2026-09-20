package de.mcsm.companion;

import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.Action;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockBurnEvent;
import org.bukkit.event.block.BlockExplodeEvent;
import org.bukkit.event.block.BlockFadeEvent;
import org.bukkit.event.block.BlockPhysicsEvent;
import org.bukkit.event.block.BlockPistonExtendEvent;
import org.bukkit.event.block.BlockPistonRetractEvent;
import org.bukkit.event.entity.EntityChangeBlockEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityExplodeEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerDropItemEvent;
import org.bukkit.event.player.PlayerGameModeChangeEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerRespawnEvent;
import org.bukkit.event.world.EntitiesLoadEvent;
import org.bukkit.event.world.WorldLoadEvent;

/**
 * Ereignisse des MCSM-Hardcore-Modus: Tod, Wiedererscheinen am Grab, Zuschauer-Sperre, Beitritt,
 * Wiederbelebung per Totem (Rechtsklick oder Ablegen) und Schutz der Grabblöcke und -anzeigen.
 */
public final class HardcoreListener implements Listener {

    private final CompanionPlugin plugin;
    private final HardcoreManager hc;

    public HardcoreListener(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.hc = plugin.hardcore();
    }

    // ---- Tod, Wiedererscheinen, Sperre --------------------------------------------------------

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onDeath(PlayerDeathEvent event) {
        Player victim = event.getPlayer();
        if (hc.isDead(victim.getUniqueId())) {
            // Tote Zuschauer sterben nicht noch einmal (/kill und Ähnliches): abbrechen, Sperre bleibt.
            event.setCancelled(true);
            return;
        }
        if (event.isCancelled() || !hc.appliesTo(victim)) {
            return;
        }
        Component text = hc.handleDeath(victim);
        event.deathMessage(null);
        Bukkit.broadcast(text);
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onRespawn(PlayerRespawnEvent event) {
        Player p = event.getPlayer();
        GraveStore.Grave g = hc.grave(p.getUniqueId());
        if (g == null || g.pendingRevive) {
            return;
        }
        Location at = hc.respawnLocation(g);
        if (at != null) {
            event.setRespawnLocation(at);
        }
        Bukkit.getScheduler().runTask(plugin, () -> {
            if (p.isOnline() && hc.isDead(p.getUniqueId())) {
                hc.lockSpectator(p);
                Msg.send(p, HardcoreManager.DEAD_HINT);
            }
        });
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onGameModeChange(PlayerGameModeChangeEvent event) {
        Player p = event.getPlayer();
        if (!hc.isDead(p.getUniqueId()) || hc.isInternalChange() || event.getNewGameMode() == GameMode.SPECTATOR) {
            return;
        }
        event.setCancelled(true);
        Component hint = Msg.prefixed(HardcoreManager.DEAD_HINT);
        event.cancelMessage(hint);
        if (event.getCause() != PlayerGameModeChangeEvent.Cause.COMMAND) {
            p.sendMessage(hint);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        Player p = event.getPlayer();
        if (!hc.isDead(p.getUniqueId())) {
            return;
        }
        // Erst im nächsten Tick, nach Vanilla-Spawn und stillem Betreiber-Join.
        Bukkit.getScheduler().runTask(plugin, () -> {
            if (p.isOnline()) {
                hc.handleJoin(p);
            }
        });
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onDamage(EntityDamageEvent event) {
        Entity e = event.getEntity();
        if (hc.isGraveDisplay(e)) {
            event.setCancelled(true);
        } else if (e instanceof Player p && hc.isDead(p.getUniqueId())) {
            // Selbst-Wiederbelebung über /kill (Schaden vom Typ "kill" trifft auch Zuschauer) verhindern.
            event.setCancelled(true);
        }
    }

    // ---- Wiederbelebung -----------------------------------------------------------------------

    @EventHandler(priority = EventPriority.HIGH)
    public void onInteract(PlayerInteractEvent event) {
        if (event.getAction() != Action.RIGHT_CLICK_BLOCK || event.getClickedBlock() == null) {
            return;
        }
        if (hc.reviveByClick(event.getPlayer(), event.getClickedBlock())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onDrop(PlayerDropItemEvent event) {
        if (event.getItemDrop().getItemStack().getType() == Material.TOTEM_OF_UNDYING) {
            hc.trackTotem(event.getPlayer(), event.getItemDrop());
        }
    }

    // ---- Schutz der Gräber --------------------------------------------------------------------

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onBreak(BlockBreakEvent event) {
        if (!hc.isGraveBlock(event.getBlock())) {
            return;
        }
        event.setCancelled(true);
        Msg.send(event.getPlayer(), "<gray>Dieses Grab ist geschützt. Ein Rechtsklick mit einem "
                + "<white>Totem der Unsterblichkeit</white> belebt den Spieler wieder.</gray>");
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onEntityExplode(EntityExplodeEvent event) {
        event.blockList().removeIf(hc::isGraveBlock);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onBlockExplode(BlockExplodeEvent event) {
        event.blockList().removeIf(hc::isGraveBlock);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onPistonExtend(BlockPistonExtendEvent event) {
        if (hc.pistonTouchesGrave(event.getBlock(), event.getDirection(), event.getBlocks())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onPistonRetract(BlockPistonRetractEvent event) {
        if (hc.pistonTouchesGrave(event.getBlock(), event.getDirection(), event.getBlocks())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onBurn(BlockBurnEvent event) {
        if (hc.isGraveBlock(event.getBlock())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onFade(BlockFadeEvent event) {
        if (hc.isGraveBlock(event.getBlock())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onPhysics(BlockPhysicsEvent event) {
        if (hc.isGraveBlock(event.getBlock())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onEntityChangeBlock(EntityChangeBlockEvent event) {
        // Wither, Verwüster, Silberfische usw. – Spielerköpfe sind nicht wither-immun.
        if (hc.isGraveBlock(event.getBlock())) {
            event.setCancelled(true);
        }
    }

    // ---- Anzeigen nach dem Laden prüfen -------------------------------------------------------

    @EventHandler(priority = EventPriority.MONITOR)
    public void onEntitiesLoad(EntitiesLoadEvent event) {
        hc.checkChunk(event.getChunk(), event.getEntities());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onWorldLoad(WorldLoadEvent event) {
        hc.checkWorld(event.getWorld());
    }
}
