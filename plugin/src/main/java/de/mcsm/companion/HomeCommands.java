package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;
import org.bukkit.event.player.PlayerTeleportEvent;

/** /sethome, /home, /delhome, /homes – Recht mcsm.home (Standard: alle). */
public final class HomeCommands implements TabExecutor {

    private static final Pattern NAME = Pattern.compile("[a-z0-9_-]{1,16}");

    private final CompanionPlugin plugin;

    public HomeCommands(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!plugin.settings().featHomes) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        String name = args.length >= 1 ? args[0].toLowerCase(Locale.ROOT) : "home";
        switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "sethome": return setHome(player, name);
            case "home": return home(player, name, args.length == 0);
            case "delhome": return delHome(player, name);
            case "homes": return list(player);
            default: return false;
        }
    }

    private boolean setHome(Player p, String name) {
        if (!NAME.matcher(name).matches()) {
            Msg.error(p, "Ungültiger Name. Erlaubt: Buchstaben, Ziffern, _ und - (max. 16 Zeichen).");
            return true;
        }
        HomeStore store = plugin.homes();
        int max = plugin.settings().maxHomes;
        boolean unlimited = p.isOp() || p.hasPermission("mcsm.home.unlimited");
        if (!unlimited && !store.exists(p.getUniqueId(), name) && store.count(p.getUniqueId()) >= max) {
            Msg.error(p, "Du hast bereits <white><n></white> Homes. Lösche eins mit <white>/delhome <Name></white>.",
                    Msg.number("n", max));
            return true;
        }
        store.set(p.getUniqueId(), name, p.getLocation());
        Msg.send(p, "<gray>Home <white><n></white> gesetzt.</gray>", Msg.text("n", name));
        return true;
    }

    private boolean home(Player p, String name, boolean defaultName) {
        HomeStore store = plugin.homes();
        Set<String> names = store.names(p.getUniqueId());
        if (names.isEmpty()) {
            Msg.error(p, "Du hast noch kein Home. Setze eins mit <white>/sethome</white>.");
            return true;
        }
        if (defaultName && !names.contains(name) && names.size() == 1) {
            name = names.iterator().next();
        }
        Location loc = store.get(p.getUniqueId(), name);
        if (loc == null) {
            Msg.error(p, "Home <white><n></white> nicht gefunden. Deine Homes: <white><list></white>",
                    Msg.text("n", name), Msg.text("list", String.join(", ", names)));
            return true;
        }
        if (loc.getWorld() == null) {
            Msg.error(p, "Die Welt dieses Homes existiert nicht mehr.");
            return true;
        }
        final String shown = name;
        p.teleportAsync(loc, PlayerTeleportEvent.TeleportCause.COMMAND).thenAccept(ok -> {
            if (Boolean.TRUE.equals(ok)) {
                Msg.send(p, "<gray>Willkommen zu Hause (<white><n></white>).</gray>", Msg.text("n", shown));
            } else {
                Msg.error(p, "Teleport fehlgeschlagen.");
            }
        });
        return true;
    }

    private boolean delHome(Player p, String name) {
        if (plugin.homes().delete(p.getUniqueId(), name)) {
            Msg.send(p, "<gray>Home <white><n></white> gelöscht.</gray>", Msg.text("n", name));
        } else {
            Msg.error(p, "Home <white><n></white> nicht gefunden.", Msg.text("n", name));
        }
        return true;
    }

    private boolean list(Player p) {
        Set<String> names = plugin.homes().names(p.getUniqueId());
        if (names.isEmpty()) {
            Msg.send(p, "<gray>Du hast noch kein Home. Setze eins mit <white>/sethome</white>.</gray>");
            return true;
        }
        StringBuilder sb = new StringBuilder();
        for (String n : names) {
            if (sb.length() > 0) {
                sb.append("<gray>, </gray>");
            }
            sb.append("<click:run_command:'/home ").append(n).append("'><hover:show_text:'<gray>Zu </gray><white>")
              .append(n).append("</white><gray> teleportieren</gray>'><white>").append(n).append("</white></hover></click>");
        }
        Msg.send(p, "<gray>Deine Homes (<white><c></white>): </gray>" + sb, Msg.number("c", names.size()));
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length != 1 || !(sender instanceof Player player) || command.getName().equalsIgnoreCase("homes")) {
            return out;
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        for (String n : plugin.homes().names(player.getUniqueId())) {
            if (n.startsWith(prefix)) {
                out.add(n);
            }
        }
        return out;
    }
}
