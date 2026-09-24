package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /uhr [an|aus] – blendet die Uhrzeit in der Actionbar ein oder aus. Recht mcsm.clock (Standard: alle). */
public final class ClockCommand implements TabExecutor {

    private final ClockTask clock;

    public ClockCommand(ClockTask clock) {
        this.clock = clock;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!clock.enabled()) {
            Msg.error(player, "Die Uhr ist auf diesem Server abgeschaltet.");
            return true;
        }
        boolean on;
        if (args.length == 0) {
            on = clock.toggle(player);
        } else {
            switch (args[0].toLowerCase(Locale.ROOT)) {
                case "an": case "on": case "ein":
                    on = true;
                    clock.set(player, true);
                    break;
                case "aus": case "off":
                    on = false;
                    clock.set(player, false);
                    break;
                default:
                    Msg.send(player, "<gray>Verwendung: <white>/uhr [an|aus]</white></gray>");
                    return true;
            }
        }
        if (on) {
            Msg.send(player, "<gray>Die Uhrzeit wird dir jetzt über der Schnellzugriffsleiste angezeigt.</gray>");
        } else {
            Msg.send(player, "<gray>Uhr ausgeblendet. Mit <white>/uhr an</white> holst du sie zurück.</gray>");
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length == 1) {
            String prefix = args[0].toLowerCase(Locale.ROOT);
            for (String s : List.of("an", "aus")) {
                if (s.startsWith(prefix)) {
                    out.add(s);
                }
            }
        }
        return out;
    }
}
