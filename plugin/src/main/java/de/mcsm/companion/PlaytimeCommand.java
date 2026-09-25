package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /playtime [Spieler] – Spielzeit insgesamt und in der laufenden Sitzung. */
public final class PlaytimeCommand implements TabExecutor {

    private final CompanionPlugin plugin;
    private final PlayerStatsStore stats;

    public PlaytimeCommand(CompanionPlugin plugin, PlayerStatsStore stats) {
        this.plugin = plugin;
        this.stats = stats;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!stats.enabled()) {
            Msg.error(sender, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        UUID id;
        if (args.length == 0) {
            if (!(sender instanceof Player self)) {
                Msg.send(sender, "<gray>Verwendung: <white>/playtime <Spieler></white></gray>");
                return true;
            }
            id = self.getUniqueId();
        } else {
            id = stats.resolveVisible(sender, args[0]);
            if (id == null) {
                Msg.error(sender, "Spieler <white><target></white> ist unbekannt.", Msg.text("target", args[0]));
                return true;
            }
        }
        String name = stats.nameOf(id);
        Msg.line(sender, "<gray>Spielzeit von <white><name></white>: <green><total></green></gray>",
                Msg.text("name", name),
                Msg.text("total", PlayerStatsStore.duration(stats.playMillis(id))));
        Player online = Bukkit.getPlayer(id);
        if (online != null && stats.sessionStart(id) > 0L) {
            long session = System.currentTimeMillis() - stats.sessionStart(id);
            Msg.send(sender, "<gray>Aktuelle Sitzung: <white><session></white></gray>",
                    Msg.text("session", PlayerStatsStore.duration(session)));
        }
        long first = stats.firstJoin(id);
        if (first > 0L) {
            Msg.send(sender, "<gray>Zum ersten Mal dabei seit <white><since></white> <dark_gray>(<stamp>)</dark_gray></gray>",
                    Msg.text("since", PlayerStatsStore.duration(System.currentTimeMillis() - first)),
                    Msg.text("stamp", PlayerStatsStore.stamp(first)));
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
        for (Player p : plugin.visiblePlayers(sender)) {
            if (p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                out.add(p.getName());
            }
        }
        return out;
    }
}
