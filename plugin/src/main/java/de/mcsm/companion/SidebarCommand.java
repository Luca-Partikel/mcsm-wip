package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /sb [on|off] – schaltet die Seitenleiste um. Recht mcsm.sidebar (Standard: alle). */
public final class SidebarCommand implements TabExecutor {

    private final CompanionPlugin plugin;
    private final SidebarManager sidebar;

    public SidebarCommand(CompanionPlugin plugin, SidebarManager sidebar) {
        this.plugin = plugin;
        this.sidebar = sidebar;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!sidebar.enabled()) {
            Msg.error(player, "Die Seitenleiste ist auf diesem Server deaktiviert.");
            return true;
        }
        boolean on;
        if (args.length == 0) {
            on = sidebar.toggle(player);
        } else {
            switch (args[0].toLowerCase(Locale.ROOT)) {
                case "on": case "an": case "ein":
                    on = true;
                    sidebar.set(player, true);
                    break;
                case "off": case "aus":
                    on = false;
                    sidebar.set(player, false);
                    break;
                default:
                    Msg.send(player, "<gray>Verwendung: <white>/sb [an|aus]</white></gray>");
                    return true;
            }
        }
        if (on) {
            Msg.send(player, "<gray>Seitenleiste <green>eingeschaltet</green>.</gray>");
        } else {
            Msg.send(player, "<gray>Seitenleiste <red>ausgeschaltet</red>.</gray>");
        }
        // Hinweis auf die Übersicht, falls jemand den Rest des Plugins noch nicht kennt.
        if (on && plugin.settings().raw().getBoolean("sidebar.hint", true)) {
            Msg.send(player, "<gray>Alle Befehle: </gray>"
                    + "<click:suggest_command:'/mcsm'><white>/mcsm</white></click>");
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length != 1) {
            return out;
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        for (String option : new String[] {"an", "aus"}) {
            if (option.startsWith(prefix)) {
                out.add(option);
            }
        }
        return out;
    }
}
