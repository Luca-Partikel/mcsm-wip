package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /admin <join|vanish|status|reload> – nur für den Wartungszugang, OPs und Inhaber von mcsm.admin.
 */
public final class AdminCommand implements TabExecutor {

    private static final List<String> SUB = List.of("join", "vanish", "status", "reload");

    private final CompanionPlugin plugin;

    public AdminCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    private boolean allowed(CommandSender sender) {
        if (!(sender instanceof Player)) {
            return true;                                   // Konsole
        }
        return plugin.settings().isAdmin(sender.getName()) || sender.isOp() || sender.hasPermission("mcsm.admin");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!allowed(sender)) {
            Msg.error(sender, "Dazu hast du keine Berechtigung.");
            return true;
        }
        if (args.length != 1) {
            Msg.send(sender, "<gray>Verwendung: <white>/admin <join|vanish|status|reload></white></gray>");
            return true;
        }
        switch (args[0].toLowerCase(Locale.ROOT)) {
            case "join": return join(sender);
            case "vanish": return vanish(sender);
            case "status":
                sender.sendMessage(plugin.adminInfo(sender instanceof Player p ? p : null));
                return true;
            case "reload":
                plugin.reloadAll();
                Msg.send(sender, "<green>Konfiguration neu geladen.</green>");
                return true;
            default:
                Msg.send(sender, "<gray>Verwendung: <white>/admin <join|vanish|status|reload></white></gray>");
                return true;
        }
    }

    private boolean join(CommandSender sender) {
        if (!(sender instanceof Player p)) {
            Msg.error(sender, "Dieser Unterbefehl ist nur für Spieler.");
            return true;
        }
        if (!plugin.vanish().isVanished(p)) {
            Msg.send(p, "<gray>Du bist bereits sichtbar.</gray>");
            return true;
        }
        plugin.vanish().unvanish(p);
        if (plugin.settings().featJoinLeave) {
            Bukkit.broadcast(Msg.mm(plugin.settings().joinFormat, Msg.name("name", p)));
        }
        plugin.tablist().refreshLater();
        plugin.status().write();
        Msg.send(p, "<green>Du bist jetzt sichtbar und normal beigetreten.</green> "
                + "<gray>Mit <white>/admin vanish</white> wirst du wieder unsichtbar.</gray>");
        return true;
    }

    private boolean vanish(CommandSender sender) {
        if (!(sender instanceof Player p)) {
            Msg.error(sender, "Dieser Unterbefehl ist nur für Spieler.");
            return true;
        }
        if (plugin.vanish().isVanished(p)) {
            Msg.send(p, "<gray>Du bist bereits unsichtbar.</gray>");
            return true;
        }
        if (plugin.settings().featJoinLeave) {
            Bukkit.broadcast(Msg.mm(plugin.settings().leaveFormat, Msg.name("name", p)));
        }
        plugin.vanish().vanish(p);
        plugin.tablist().refreshLater();
        plugin.status().write();
        Msg.send(p, "<green>Du bist jetzt unsichtbar.</green> "
                + "<gray>Mit <white>/admin join</white> wirst du wieder sichtbar.</gray>");
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length == 1 && allowed(sender)) {
            String prefix = args[0].toLowerCase(Locale.ROOT);
            for (String s : SUB) {
                if (s.startsWith(prefix)) {
                    out.add(s);
                }
            }
        }
        return out;
    }
}
