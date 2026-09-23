package de.mcsm.companion;

import com.destroystokyo.paper.event.profile.ProfileWhitelistVerifyEvent;
import com.destroystokyo.paper.event.server.WhitelistToggleEvent;
import org.bukkit.Bukkit;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;

/**
 * Eigener Ablehnungstext der Freigabeliste und die Chatmeldung beim Umschalten.
 *
 * <p>Paper prüft die Whitelist in {@link ProfileWhitelistVerifyEvent}; wird dort ein eigener Text
 * gesetzt, schickt der Server ihn statt der Vanilla-Zeile und lehnt die Verbindung weiterhin mit
 * dem Ergebnis KICK_WHITELIST ab. Der alte Weg über {@code PlayerLoginEvent} ist in Paper 26.2
 * als veraltet markiert (forRemoval) und würde mit {@code -Werror} den Übersetzer stoppen.</p>
 *
 * <p>Der Prüfpunkt läuft nicht zwingend im Hauptthread – hier wird deshalb nur der vorgebaute
 * Text aus {@link WhitelistService#kickMessage()} gesetzt und keine Server-API angefasst.</p>
 */
public final class WhitelistListener implements Listener {

    private final CompanionPlugin plugin;
    private final WhitelistService whitelist;

    public WhitelistListener(CompanionPlugin plugin, WhitelistService whitelist) {
        this.plugin = plugin;
        this.whitelist = whitelist;
    }

    /** Eigener, mehrzeiliger Ablehnungstext für alle, die nicht freigegeben sind. */
    @EventHandler(priority = EventPriority.NORMAL)
    public void onVerify(ProfileWhitelistVerifyEvent event) {
        if (!whitelist.featureOn() || event.isWhitelisted()) {
            return;
        }
        event.kickMessage(whitelist.kickMessage());
    }

    /**
     * Meldung für alle, wenn die Freigabeliste an- oder ausgeschaltet wird – egal ob über /wl,
     * den Vanilla-Befehl oder den Manager.
     */
    @EventHandler(priority = EventPriority.MONITOR)
    public void onToggle(WhitelistToggleEvent event) {
        if (!whitelist.featureOn()) {
            return;
        }
        boolean on = event.isEnabled();
        if (whitelist.broadcastToggle() && !Bukkit.getOnlinePlayers().isEmpty()) {
            Bukkit.broadcast(whitelist.toggleMessage(on));
        }
        plugin.getLogger().info("Freigabeliste " + (on ? "eingeschaltet" : "ausgeschaltet")
                + " – Einträge: " + whitelist.names().size());
        if (on && whitelist.kickOnEnable() && plugin.isEnabled()) {
            // Erst im nächsten Tick, damit der Server den neuen Zustand fertig gesetzt hat.
            Bukkit.getScheduler().runTask(plugin, () -> {
                int n = whitelist.kickNotListed();
                if (n > 0) {
                    plugin.getLogger().info("Freigabeliste: " + n + " nicht freigegebene Spieler getrennt.");
                }
            });
        }
    }
}
