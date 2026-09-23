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

/** /stats [Spieler] – Übersicht über Spielzeit, Tode, Kills und zurückgelegte Strecke. */
public final class StatsCommand implements TabExecutor {

    private final CompanionPlugin plugin;
    private final PlayerStatsStore stats;
    private final AfkManager afk;

    public StatsCommand(CompanionPlugin plugin, PlayerStatsStore stats, AfkManager afk) {
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
        UUID id;
        if (args.length == 0) {
            if (!(sender instanceof Player self)) {
                Msg.send(sender, "<gray>Verwendung: <white>/stats <Spieler></white></gray>");
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
        Player online = Bukkit.getPlayer(id);
        int deaths = stats.deaths(id);
        int playerKills = stats.playerKills(id);
        int mobKills = stats.mobKills(id);

        sender.sendMessage(Msg.mm("<dark_gray>──────── <green>Statistik</green> <white><name></white> <dark_gray>────────</dark_gray>",
                Msg.text("name", name)));
        // Der Zustandstext ist fest vorgegeben und darf deshalb als MiniMessage eingesetzt werden.
        Msg.send(sender, "<gray>Zustand: " + state(id, online) + "</gray>");
        Msg.send(sender, "<gray>Spielzeit: <white><total></white></gray>",
                Msg.text("total", PlayerStatsStore.duration(stats.playMillis(id))));
        Msg.send(sender, "<gray>Erster Besuch: <white><first></white></gray>",
                Msg.text("first", PlayerStatsStore.stamp(stats.firstJoin(id))));
        Msg.send(sender, "<gray>Zuletzt gesehen: <white><last></white></gray>",
                Msg.text("last", online != null ? "jetzt gerade" : PlayerStatsStore.ago(stats.lastQuit(id))));
        Msg.send(sender, "<gray>Tode: <white><deaths></white> <dark_gray>|</dark_gray> "
                        + "Spieler besiegt: <white><pk></white> <dark_gray>|</dark_gray> "
                        + "Kreaturen besiegt: <white><mk></white></gray>",
                Msg.number("deaths", deaths), Msg.number("pk", playerKills), Msg.number("mk", mobKills));
        Msg.send(sender, "<gray>Verhältnis Kills/Tode: <white><ratio></white></gray>",
                Msg.text("ratio", ratio(playerKills, deaths)));
        Msg.send(sender, "<gray>Zurückgelegte Strecke: <white><km></white></gray>",
                Msg.text("km", distance(stats.distanceMeters(id))));
        return true;
    }

    /** Onlinezustand als fertiger MiniMessage-Text. */
    private String state(UUID id, Player online) {
        if (online == null) {
            return "<red>offline</red>";
        }
        if (afk.isAfk(id)) {
            return "<yellow>online, AFK seit " + PlayerStatsStore.duration(afk.afkMillis(id)) + "</yellow>";
        }
        return "<green>online</green>";
    }

    /** Kills je Tod mit einer Nachkommastelle; ohne Tod zählt der Kill-Wert selbst. */
    private static String ratio(int kills, int deaths) {
        double value = deaths <= 0 ? kills : (double) kills / deaths;
        return String.format(Locale.GERMANY, "%.1f", value);
    }

    /** Meter bis 1000, darüber Kilometer mit einer Nachkommastelle. */
    private static String distance(long meters) {
        if (meters < 1000L) {
            return meters + " m";
        }
        return String.format(Locale.GERMANY, "%.1f km", meters / 1000.0);
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
