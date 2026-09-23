package de.mcsm.companion;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;

/** Join-/Leave-Texte, Erst-Join, stiller Betreiber-Join und Tablist-Aktualisierung. */
public final class JoinQuitListener implements Listener {

    private final CompanionPlugin plugin;

    public JoinQuitListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onJoin(PlayerJoinEvent event) {
        Config cfg = plugin.settings();
        Player p = event.getPlayer();

        // Bereits unsichtbare Betreiber dürfen dem Neuankömmling nicht angezeigt werden.
        plugin.vanish().hideVanishedFrom(p);

        if (cfg.isAdmin(p.getName())) {
            if (cfg.adminOp && !p.isOp()) {
                p.setOp(true);
            }
            if (cfg.adminSilentJoin) {
                event.joinMessage(null);
                plugin.vanish().vanish(p);
            } else {
                plugin.vanish().repairStaleState(p);
                if (cfg.featJoinLeave) {
                    event.joinMessage(joinText(p));
                }
            }
            // Bestätigung erst im nächsten Tick, damit sie nach den Vanilla-Meldungen ankommt.
            Bukkit.getScheduler().runTask(plugin, () -> {
                if (p.isOnline()) {
                    p.sendMessage(plugin.adminInfo(p));
                }
            });
        } else {
            plugin.vanish().repairStaleState(p);
            if (cfg.featJoinLeave) {
                event.joinMessage(joinText(p));
            }
        }
        // Unverwundbarkeit aus /admin god steht im Spieler-NBT und muss zum Merker passen.
        if (plugin.adminTools() != null) {
            plugin.adminTools().restore(p);
        }
        plugin.tablist().refreshLater();
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onQuit(PlayerQuitEvent event) {
        Config cfg = plugin.settings();
        Player p = event.getPlayer();
        if (plugin.vanish().isVanished(p)) {
            event.quitMessage(null);
            plugin.vanish().forget(p);
        } else if (cfg.featJoinLeave) {
            event.quitMessage(Msg.mm(cfg.leaveFormat, Msg.name("name", p)));
        }
        plugin.tpa().clear(p);
        if (plugin.adminTools() != null) {
            plugin.adminTools().forget(p);
        }
        plugin.tablist().refreshLater();
    }

    /** Normaler bzw. erster Join-Text für diesen Spieler. */
    public net.kyori.adventure.text.Component joinText(Player p) {
        Config cfg = plugin.settings();
        String format = p.hasPlayedBefore() ? cfg.joinFormat : cfg.firstJoinFormat;
        return Msg.mm(format, Msg.name("name", p));
    }
}
