package de.mcsm.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /ping [Spieler] – die Verzögerung (ms) des eigenen oder eines anderen Spielers, farbig bewertet. */
public final class PingCommand implements TabExecutor {

    private final CompanionPlugin plugin;

    public PingCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        Player ziel;
        if (args.length == 0) {
            if (!(sender instanceof Player self)) {
                Msg.send(sender, "<gray>Verwendung: <white>/ping <Spieler></white></gray>");
                return true;
            }
            ziel = self;
        } else {
            ziel = plugin.findVisible(sender, args[0]);
            if (ziel == null) {
                Msg.error(sender, "Spieler <white><t></white> ist nicht online.", Msg.text("t", args[0]));
                return true;
            }
        }
        int ping = ziel.getPing();
        String farbe = ping < 80 ? "green" : ping < 200 ? "yellow" : "red";
        if (ziel.equals(sender)) {
            Msg.send(sender, "<gray>Dein Ping: <" + farbe + "><ms> ms</" + farbe + "></gray>",
                    Msg.number("ms", ping));
        } else {
            Msg.send(sender, "<gray>Ping von <white><name></white>: <" + farbe + "><ms> ms</" + farbe + "></gray>",
                    Msg.name("name", ziel), Msg.number("ms", ping));
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        if (args.length == 1) {
            String vor = args[0].toLowerCase(Locale.ROOT);
            List<String> namen = new ArrayList<>();
            for (Player p : Bukkit.getOnlinePlayers()) {
                if (plugin.findVisibleExact(sender, p.getName()) != null
                        && p.getName().toLowerCase(Locale.ROOT).startsWith(vor)) {
                    namen.add(p.getName());
                }
            }
            return namen;
        }
        return Collections.emptyList();
    }
}
