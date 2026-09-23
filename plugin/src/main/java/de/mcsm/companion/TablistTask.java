package de.mcsm.companion;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import org.bukkit.Bukkit;
import org.bukkit.entity.Player;

/** Setzt Kopf- und Fußzeile der Tablist (alle 5 Sekunden sowie bei Join/Quit). */
public final class TablistTask implements Runnable {

    private final CompanionPlugin plugin;

    public TablistTask(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public void run() {
        Config cfg = plugin.settings();
        if (!cfg.featTablist) {
            return;
        }
        TagResolver tags = TagResolver.resolver(
                Msg.text("server_name", cfg.serverName),
                Msg.number("online", plugin.vanish().visibleOnline()),
                Msg.number("max", Bukkit.getMaxPlayers()),
                Placeholder.parsed("sponsor", cfg.sponsorText),
                Msg.text("manager_version", cfg.managerVersion),
                Msg.text("mode", cfg.mode));
        Component header = Msg.mm(cfg.tablistHeader, tags);
        Component footer = Msg.mm(cfg.tablistFooter, tags);
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.sendPlayerListHeaderAndFooter(header, footer);
        }
    }

    /** Aktualisierung im nächsten Tick (nach Join/Quit ist die Spielerliste dann aktuell). */
    public void refreshLater() {
        if (!plugin.isEnabled()) {
            return;       // beim Plugin-Ende (Abschieds-Kick in onDisable) gibt es keinen nächsten Tick mehr
        }
        Bukkit.getScheduler().runTask(plugin, this);
    }

    /** Entfernt Kopf- und Fußzeile wieder (Plugin-Ende oder Feature abgeschaltet). */
    public void clear() {
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.sendPlayerListHeaderAndFooter(Component.empty(), Component.empty());
        }
    }
}
