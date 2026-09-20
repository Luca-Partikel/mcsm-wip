package de.mcsm.companion;

import java.util.Iterator;

import com.destroystokyo.paper.event.server.PaperServerListPingEvent;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;

/** Serverliste: optionale MOTD-Zeile und unsichtbare Betreiber nicht mitzählen. */
public final class PingListener implements Listener {

    private final CompanionPlugin plugin;

    public PingListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.NORMAL)
    public void onPing(PaperServerListPingEvent event) {
        Config cfg = plugin.settings();
        if (!cfg.motdLine.isBlank()) {
            event.motd(Msg.mm(cfg.motdLine, Msg.text("server_name", cfg.serverName)));
        }
        int hidden = 0;
        for (Player v : plugin.vanish().vanishedPlayers()) {
            hidden++;
            Iterator<PaperServerListPingEvent.ListedPlayerInfo> it = event.getListedPlayers().iterator();
            while (it.hasNext()) {
                if (v.getUniqueId().equals(it.next().id())) {
                    it.remove();
                }
            }
        }
        if (hidden > 0) {
            event.setNumPlayers(Math.max(0, event.getNumPlayers() - hidden));
        }
    }
}
