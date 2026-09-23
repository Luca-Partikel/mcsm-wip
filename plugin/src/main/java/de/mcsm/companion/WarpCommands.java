package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.JoinConfiguration;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /warp, /warps, /setwarp und /delwarp – serverweite Ziele aus warps.yml.
 * Benutzen darf jeder (mcsm.warp), anlegen und löschen nur mcsm.warp.admin.
 */
public final class WarpCommands implements TabExecutor {

    /** Gleiche Namensregel wie bei Homes. */
    private static final Pattern NAME = Pattern.compile("[a-z0-9_-]{1,16}");
    private static final String KEY = "warp";

    private final CompanionPlugin plugin;
    private final WarpStore warps;
    private final TeleportService teleports;

    public WarpCommands(CompanionPlugin plugin, WarpStore warps, TeleportService teleports) {
        this.plugin = plugin;
        this.warps = warps;
        this.teleports = teleports;
    }

    private boolean mayManage(Player player) {
        return player.isOp() || player.hasPermission("mcsm.warp.admin");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!plugin.settings().raw().getBoolean("features.warps", true)) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        String name = args.length >= 1 ? args[0].toLowerCase(Locale.ROOT) : "";
        switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "warp": return warp(player, name);
            case "warps": return list(player);
            case "setwarp": return setWarp(player, name);
            case "delwarp": return delWarp(player, name);
            default: return false;
        }
    }

    private boolean warp(Player player, String name) {
        if (name.isEmpty()) {
            return list(player);
        }
        Location target = warps.get(name);
        if (target == null) {
            Msg.error(player, "Den Warp <white><n></white> gibt es nicht.", Msg.text("n", name));
            return list(player);
        }
        if (target.getWorld() == null) {
            Msg.error(player, "Die Welt <white><w></white> dieses Warps gibt es nicht mehr.",
                    Msg.text("w", warps.worldName(name)));
            return true;
        }
        // Warp-Namen sind auf [a-z0-9_-] begrenzt und können daher keine MiniMessage-Tags einschleusen.
        teleports.request(player, target, KEY,
                "<gray>Du bist jetzt bei <white>" + name + "</white>.</gray>");
        return true;
    }

    private boolean list(Player player) {
        Set<String> names = warps.names();
        if (names.isEmpty()) {
            Msg.send(player, "<gray>Es gibt noch keine Warps.</gray>");
            return true;
        }
        List<Component> parts = new ArrayList<>();
        for (String n : names) {
            parts.add(Component.text(n, NamedTextColor.WHITE)
                    .clickEvent(ClickEvent.runCommand("/warp " + n))
                    .hoverEvent(Component.text("Nach " + n + " (" + warps.worldName(n) + "), gesetzt von "
                            + warps.creator(n), NamedTextColor.GRAY)));
        }
        Component joined = Component.join(
                JoinConfiguration.separator(Component.text(", ", NamedTextColor.GRAY)), parts);
        Msg.send(player, "<gray>Warps (<white><c></white>): </gray><list>",
                Msg.number("c", names.size()), Placeholder.component("list", joined));
        return true;
    }

    private boolean setWarp(Player player, String name) {
        if (!mayManage(player)) {
            Msg.error(player, "Dazu hast du keine Berechtigung.");
            return true;
        }
        if (!NAME.matcher(name).matches()) {
            Msg.error(player, "Ungültiger Name. Erlaubt: Buchstaben, Ziffern, _ und - (max. 16 Zeichen).");
            return true;
        }
        boolean existed = warps.exists(name);
        warps.set(name, player.getLocation(), player.getName());
        if (existed) {
            Msg.send(player, "<gray>Warp <white><n></white> überschrieben.</gray>", Msg.text("n", name));
        } else {
            Msg.send(player, "<gray>Warp <white><n></white> gesetzt.</gray>", Msg.text("n", name));
        }
        return true;
    }

    private boolean delWarp(Player player, String name) {
        if (!mayManage(player)) {
            Msg.error(player, "Dazu hast du keine Berechtigung.");
            return true;
        }
        if (warps.delete(name)) {
            Msg.send(player, "<gray>Warp <white><n></white> gelöscht.</gray>", Msg.text("n", name));
        } else {
            Msg.error(player, "Den Warp <white><n></white> gibt es nicht.", Msg.text("n", name));
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        String name = command.getName().toLowerCase(Locale.ROOT);
        if (args.length != 1 || name.equals("warps") || name.equals("setwarp")) {
            return out;
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        for (String n : warps.names()) {
            if (n.startsWith(prefix)) {
                out.add(n);
            }
        }
        return out;
    }
}
