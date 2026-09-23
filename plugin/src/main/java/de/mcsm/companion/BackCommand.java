package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /back – zurück zur letzten Position vor einem Teleport oder zum eigenen Todesort.
 * Recht mcsm.back (Standard: alle). Es wird nur der eigene Verlauf benutzt.
 */
public final class BackCommand implements TabExecutor {

    private static final List<String> MODES = List.of("ort", "tod");

    private final CompanionPlugin plugin;
    private final TeleportService teleports;
    private final TeleportHistory history;

    public BackCommand(CompanionPlugin plugin, TeleportService teleports, TeleportHistory history) {
        this.plugin = plugin;
        this.teleports = teleports;
        this.history = history;
    }

    private int cooldown() {
        return Math.max(0, plugin.settings().raw().getInt("teleport.back_cooldown_seconds", 10));
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!plugin.settings().raw().getBoolean("features.back", true)) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        TeleportHistory.Spot spot = choose(player, args);
        if (spot == null) {
            return true;
        }
        Location target = spot.toLocation();
        if (target == null) {
            Msg.error(player, "Die Welt <white><w></white> gibt es nicht mehr.", Msg.text("w", spot.worldName()));
            return true;
        }
        String where = spot.blockX() + ", " + spot.blockY() + ", " + spot.blockZ();
        teleports.request(player, target, "back",
                "<gray>Du bist zurück bei <white>" + where + "</white>.</gray>", -1, cooldown());
        return true;
    }

    /** Wählt den gewünschten Ort aus; gibt {@code null} zurück und meldet, wenn es keinen gibt. */
    private TeleportHistory.Spot choose(Player player, String[] args) {
        TeleportHistory.Spot last = history.back(player.getUniqueId());
        TeleportHistory.Spot death = history.death(player.getUniqueId());
        String mode = args.length >= 1 ? args[0].toLowerCase(Locale.ROOT) : "";
        switch (mode) {
            case "tod", "death" -> {
                if (death == null) {
                    Msg.error(player, "Von dir ist kein Todesort bekannt.");
                    return null;
                }
                return death;
            }
            case "ort", "last" -> {
                if (last == null) {
                    Msg.error(player, "Es ist keine frühere Position bekannt.");
                    return null;
                }
                return last;
            }
            default -> {
                if (last == null && death == null) {
                    Msg.error(player, "Es gibt noch nichts, wohin du zurück könntest.");
                    return null;
                }
                if (last == null) {
                    return death;
                }
                if (death == null) {
                    return last;
                }
                // der jüngere der beiden Orte
                return death.time() >= last.time() ? death : last;
            }
        }
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length != 1) {
            return out;
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        for (String m : MODES) {
            if (m.startsWith(prefix)) {
                out.add(m);
            }
        }
        return out;
    }
}
