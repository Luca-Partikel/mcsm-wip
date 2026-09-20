package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.GameMode;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /gm <0|1|2|3|survival|creative|adventure|spectator> [Spieler] – Recht mcsm.gm (Standard: OP). */
public final class GamemodeCommand implements TabExecutor {

    private static final List<String> MODES = List.of("0", "1", "2", "3", "survival", "creative", "adventure", "spectator");

    private final CompanionPlugin plugin;

    public GamemodeCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!plugin.settings().featGamemode) {
            Msg.error(sender, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        if (args.length == 0 || args.length > 2) {
            Msg.send(sender, "<gray>Verwendung: <white>/gm <0|1|2|3|survival|creative|adventure|spectator> [Spieler]</white></gray>");
            return true;
        }
        GameMode mode = Config.parseGameMode(args[0], null);
        if (mode == null) {
            Msg.error(sender, "Unbekannter Spielmodus: <white><m></white>", Msg.text("m", args[0]));
            return true;
        }
        Player target;
        if (args.length == 2) {
            target = plugin.findPlayer(args[1]);
            if (target == null) {
                Msg.error(sender, "Spieler <white><n></white> wurde nicht gefunden.", Msg.text("n", args[1]));
                return true;
            }
        } else if (sender instanceof Player player) {
            target = player;
        } else {
            Msg.error(sender, "Von der Konsole aus bitte einen Spieler angeben.");
            return true;
        }
        target.setGameMode(mode);
        String modeName = Config.gameModeName(mode);
        if (target.equals(sender)) {
            Msg.send(sender, "<gray>Dein Spielmodus ist jetzt <white><m></white>.</gray>", Msg.text("m", modeName));
        } else {
            Msg.send(sender, "<gray>Spielmodus von <white><n></white> auf <white><m></white> gesetzt.</gray>",
                    Msg.name("n", target), Msg.text("m", modeName));
            Msg.send(target, "<gray>Dein Spielmodus ist jetzt <white><m></white>.</gray>", Msg.text("m", modeName));
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length == 1) {
            String prefix = args[0].toLowerCase(Locale.ROOT);
            for (String m : MODES) {
                if (m.startsWith(prefix)) {
                    out.add(m);
                }
            }
        } else if (args.length == 2) {
            String prefix = args[1].toLowerCase(Locale.ROOT);
            for (Player p : plugin.visiblePlayers(sender)) {
                if (p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                    out.add(p.getName());
                }
            }
        }
        return out;
    }
}
