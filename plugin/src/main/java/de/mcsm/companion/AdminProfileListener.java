package de.mcsm.companion;

import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerQuitEvent;

/**
 * Hält die Wartungsprofile beim Verlassen des Servers auf dem neuesten Stand.
 *
 * <p>Läuft auf MONITOR, also nach {@link JoinQuitListener}. Welches Profil gerade gilt, steht
 * in admin-profiles.yml; dass die Unsichtbarkeit zu diesem Zeitpunkt bereits vergessen wurde,
 * spielt deshalb keine Rolle.</p>
 */
public final class AdminProfileListener implements Listener {

    private final CompanionPlugin plugin;

    public AdminProfileListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        plugin.vanish().profiles().captureActive(event.getPlayer());
    }
}
