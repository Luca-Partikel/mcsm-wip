package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /afk [Grund] – schaltet den eigenen AFK-Zustand um. */
public final class AfkCommand implements TabExecutor {

    /** Längenbegrenzung, damit der Grund die Broadcast-Zeile nicht sprengt. */
    private static final int MAX_REASON = 48;

    private final AfkManager afk;

    public AfkCommand(AfkManager afk) {
        this.afk = afk;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!afk.enabled()) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        String reason = String.join(" ", args).trim();
        if (reason.length() > MAX_REASON) {
            reason = reason.substring(0, MAX_REASON).trim();
        }
        if (afk.isAfk(player) && !reason.isEmpty()) {
            // Bereits AFK und ein Grund angegeben: nur den Grund nachtragen, nicht abmelden.
            afk.updateReason(player, reason);
            return true;
        }
        afk.toggle(player, reason);
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return new ArrayList<>();
    }
}
