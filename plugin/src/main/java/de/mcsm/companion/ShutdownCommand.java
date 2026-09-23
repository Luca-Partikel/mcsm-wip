package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /mcsmstop &lt;Sekunden&gt; [Grund] – Herunterfahren mit Ansage. Recht mcsm.admin bzw. Konsole.
 *
 * <p>Steht bewusst neben /admin restart: dort geht es um einen Neustart durch den Manager,
 * hier um das geordnete Beenden. Der Manager ruft diesen Befehl vor jedem Stopp auf.</p>
 */
public final class ShutdownCommand implements TabExecutor {

    private static final List<String> FIRST = List.of("0", "10", "30", "60", "300", "abbrechen");
    private static final String USAGE =
            "<gray>Verwendung: <white>/mcsmstop <Sekunden> [Grund]</white></gray>\n"
            + "<gray><white>0</white> fährt sofort herunter, "
            + "<white>/mcsmstop abbrechen</white> stoppt einen laufenden Countdown.</gray>";

    private final CompanionPlugin plugin;
    private final ShutdownService shutdown;

    public ShutdownCommand(CompanionPlugin plugin, ShutdownService shutdown) {
        this.plugin = plugin;
        this.shutdown = shutdown;
    }

    /** Konsole immer, Spieler mit Wartungszugang, OP oder mcsm.admin. */
    private boolean allowed(CommandSender sender) {
        if (!(sender instanceof Player)) {
            return true;
        }
        return plugin.settings().isAdmin(sender.getName()) || sender.isOp() || sender.hasPermission("mcsm.admin");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!allowed(sender)) {
            Msg.error(sender, "Dazu hast du keine Berechtigung.");
            return true;
        }
        if (!shutdown.featureOn()) {
            Msg.error(sender, "Das geordnete Herunterfahren ist auf diesem Server deaktiviert.");
            return true;
        }
        if (args.length == 0) {
            Msg.send(sender, USAGE);
            if (shutdown.running()) {
                Msg.send(sender, "<gray>Es läuft ein Countdown: noch <white><t></white>. Grund: <white><g></white></gray>",
                        Msg.text("t", ShutdownService.human(shutdown.secondsLeft())),
                        Msg.text("g", shutdown.reason()));
            }
            return true;
        }

        String first = args[0].toLowerCase(Locale.ROOT);
        if (first.equals("abbrechen") || first.equals("cancel") || first.equals("stop")) {
            if (!shutdown.cancel()) {
                Msg.send(sender, "<gray>Es läuft gerade kein Countdown.</gray>");
            } else {
                plugin.getLogger().info("Herunterfahren abgebrochen von " + sender.getName() + ".");
            }
            return true;
        }

        int seconds;
        try {
            seconds = Integer.parseInt(first);
        } catch (NumberFormatException ex) {
            Msg.send(sender, USAGE);
            return true;
        }
        if (seconds < 0) {
            Msg.error(sender, "Die Sekunden dürfen nicht negativ sein.");
            return true;
        }
        if (seconds > ShutdownService.MAX_SECONDS) {
            Msg.error(sender, "Höchstens <m> Sekunden sind möglich.", Msg.number("m", ShutdownService.MAX_SECONDS));
            return true;
        }

        String reason = args.length > 1 ? String.join(" ", List.of(args).subList(1, args.length)) : "";
        if (seconds > 0 && !Bukkit.getOnlinePlayers().isEmpty()) {
            Msg.send(sender, "<gold>Herunterfahren in <white><t></white>.</gold> "
                    + "<gray>Abbrechen mit <white>/mcsmstop abbrechen</white>.</gray>",
                    Msg.text("t", ShutdownService.human(seconds)));
        } else {
            Msg.send(sender, "<gold>Der Server wird jetzt heruntergefahren.</gold>");
        }
        plugin.getLogger().info("Herunterfahren angefordert von " + sender.getName()
                + " in " + seconds + " s.");
        shutdown.start(seconds, reason);
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (!allowed(sender) || args.length != 1) {
            return out;
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        for (String s : FIRST) {
            if (s.startsWith(prefix)) {
                out.add(s);
            }
        }
        return out;
    }
}
