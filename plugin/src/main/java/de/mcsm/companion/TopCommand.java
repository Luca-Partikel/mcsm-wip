package de.mcsm.companion;

import java.util.Collections;
import java.util.List;

import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /top – auf den höchsten sicheren Block an der eigenen Position.
 * Recht mcsm.top (Standard: alle).
 */
public final class TopCommand implements TabExecutor {

    private static final String KEY = "top";

    private final CompanionPlugin plugin;
    private final TeleportService teleports;

    public TopCommand(CompanionPlugin plugin, TeleportService teleports) {
        this.plugin = plugin;
        this.teleports = teleports;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!plugin.settings().raw().getBoolean("features.top", true)) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        Location here = player.getLocation();
        Location target = TeleportService.safeTarget(here);
        if (target == null) {
            Msg.error(player, "Über dir ist kein sicherer Block.");
            return true;
        }
        if (target.getBlockY() <= here.getBlockY()) {
            Msg.send(player, "<gray>Du stehst bereits ganz oben.</gray>");
            return true;
        }
        teleports.request(player, target, KEY,
                "<gray>Du stehst jetzt auf Höhe <white>" + target.getBlockY() + "</white>.</gray>");
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return Collections.emptyList();
    }
}
