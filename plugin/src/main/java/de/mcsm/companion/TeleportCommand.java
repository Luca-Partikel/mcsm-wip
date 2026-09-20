package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;
import org.bukkit.event.player.PlayerTeleportEvent;

/**
 * /tp <Spieler> [Spieler2] – Recht mcsm.tp (Standard: OP).
 * Alles, was nicht nach 1–2 Spielernamen aussieht (Koordinaten, Selektoren, Entities),
 * wird unverändert an das Vanilla-/tp (minecraft:tp) weitergereicht.
 */
public final class TeleportCommand implements TabExecutor {

    private final CompanionPlugin plugin;

    public TeleportCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    /** Koordinaten (~, ^, Zahlen) oder Selektoren (@p, @a, ...) – das ist Vanilla-Syntax. */
    private static boolean looksLikeCoordOrSelector(String s) {
        return s.startsWith("@") || s.startsWith("~") || s.startsWith("^") || s.matches("-?\\d+(\\.\\d+)?");
    }

    /** True, wenn die Argumente genau 1–2 Online-Spielernamen sind (Essentials-Form). */
    private boolean isPlayerForm(CommandSender sender, String[] args) {
        if (args.length == 1) {
            return sender instanceof Player && !looksLikeCoordOrSelector(args[0]) && plugin.findPlayer(args[0]) != null;
        }
        if (args.length == 2) {
            return !looksLikeCoordOrSelector(args[0]) && !looksLikeCoordOrSelector(args[1])
                    && plugin.findPlayer(args[0]) != null && plugin.findPlayer(args[1]) != null;
        }
        return false;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length == 0) {
            Msg.send(sender, "<gray>Verwendung: <white>/tp <Spieler> [Spieler2]</white> "
                    + "<dark_gray>oder Vanilla-Syntax (Koordinaten/Selektoren)</dark_gray></gray>");
            return true;
        }
        if (!isPlayerForm(sender, args)) {
            // Alles andere (Koordinaten, Selektoren, Entities, unbekannte Namen) an Vanilla /tp weiterreichen.
            return Bukkit.dispatchCommand(sender, "minecraft:tp " + String.join(" ", args));
        }
        Player mover;
        Player destination;
        if (args.length == 1) {
            if (!(sender instanceof Player player)) {
                Msg.error(sender, "Von der Konsole aus: <white>/tp <Spieler> <Spieler2></white>");
                return true;
            }
            mover = player;
            destination = plugin.findPlayer(args[0]);
        } else {
            mover = plugin.findPlayer(args[0]);
            destination = plugin.findPlayer(args[1]);
        }
        if (mover == null) {
            Msg.error(sender, "Spieler <white><n></white> wurde nicht gefunden.", Msg.text("n", args[0]));
            return true;
        }
        if (destination == null) {
            Msg.error(sender, "Spieler <white><n></white> wurde nicht gefunden.", Msg.text("n", args[args.length - 1]));
            return true;
        }
        if (mover.equals(destination)) {
            Msg.error(sender, "Start und Ziel sind derselbe Spieler.");
            return true;
        }
        mover.teleportAsync(destination.getLocation(), PlayerTeleportEvent.TeleportCause.COMMAND);
        if (mover.equals(sender)) {
            Msg.send(sender, "<gray>Du wurdest zu <white><to></white> teleportiert.</gray>", Msg.name("to", destination));
        } else {
            Msg.send(sender, "<gray><white><from></white> wurde zu <white><to></white> teleportiert.</gray>",
                    Msg.name("from", mover), Msg.name("to", destination));
            Msg.send(mover, "<gray>Du wurdest zu <white><to></white> teleportiert.</gray>", Msg.name("to", destination));
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if ((args.length == 1 || args.length == 2) && !looksLikeCoordOrSelector(args[0])
                && !looksLikeCoordOrSelector(args[args.length - 1])) {
            String prefix = args[args.length - 1].toLowerCase(Locale.ROOT);
            for (Player p : plugin.visiblePlayers(sender)) {
                if (p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                    out.add(p.getName());
                }
            }
        }
        return out;
    }
}
