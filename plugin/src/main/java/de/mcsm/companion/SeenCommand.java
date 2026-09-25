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

/** /seen &lt;Spieler&gt; – wann war der Spieler zuletzt da? */
public final class SeenCommand implements TabExecutor {

    private final CompanionPlugin plugin;
    private final PlayerStatsStore stats;
    private final AfkManager afk;

    public SeenCommand(CompanionPlugin plugin, PlayerStatsStore stats, AfkManager afk) {
        this.plugin = plugin;
        this.stats = stats;
        this.afk = afk;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!stats.enabled()) {
            Msg.error(sender, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        if (args.length != 1) {
            Msg.send(sender, "<gray>Verwendung: <white>/seen <Spieler></white></gray>");
            return true;
        }
        UUID id = stats.resolveVisible(sender, args[0]);
        if (id == null) {
            Msg.error(sender, "Spieler <white><target></white> ist unbekannt.", Msg.text("target", args[0]));
            return true;
        }
        String name = stats.nameOf(id);
        Player online = Bukkit.getPlayer(id);
        if (online != null && online.isOnline()) {
            long session = stats.sessionStart(id) > 0L
                    ? System.currentTimeMillis() - stats.sessionStart(id) : 0L;
            Msg.send(sender, "<white><name></white> <green>ist gerade online</green> "
                            + "<gray>(seit <white><session></white>).</gray>",
                    Msg.text("name", name),
                    Msg.text("session", PlayerStatsStore.duration(session)));
            if (afk.isAfk(id)) {
                String reason = afk.reason(id);
                Msg.send(sender, reason.isEmpty()
                                ? "<gray>Er ist allerdings seit <white><since></white> AFK.</gray>"
                                : "<gray>Er ist allerdings seit <white><since></white> AFK: <white><reason></white></gray>",
                        Msg.text("since", PlayerStatsStore.duration(afk.afkMillis(id))),
                        Msg.text("reason", reason));
            }
            return true;
        }
        long last = stats.lastQuit(id);
        if (last <= 0L) {
            Msg.send(sender, "<gray>Von <white><name></white> ist kein Abmeldezeitpunkt bekannt.</gray>",
                    Msg.text("name", name));
            return true;
        }
        Msg.send(sender, "<white><name></white> <gray>war zuletzt <white><ago></white> hier "
                        + "<dark_gray>(<stamp>)</dark_gray>.</gray>",
                Msg.text("name", name),
                Msg.text("ago", PlayerStatsStore.ago(last)),
                Msg.text("stamp", PlayerStatsStore.stamp(last)));
        Msg.line(sender, "<gray>Gesamte Spielzeit: <white><total></white></gray>",
                Msg.text("total", PlayerStatsStore.duration(stats.playMillis(id))));
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
        for (String name : stats.knownNames(sender)) {
            if (name.toLowerCase(Locale.ROOT).startsWith(prefix) && !out.contains(name)) {
                out.add(name);
            }
        }
        return out;
    }
}
