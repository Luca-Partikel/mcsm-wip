package de.mcsm.companion;

import java.util.Collections;
import java.util.List;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;

/** /rules – zeigt die Serverregeln aus der Konfiguration. Recht mcsm.rules (Standard: alle). */
public final class RulesCommand implements TabExecutor {

    /** Standardregeln, falls in der Konfiguration nichts steht. */
    public static final List<String> DEFAULT_RULES = List.of(
            "<white>Sei freundlich.</white> <gray>Keine Beleidigungen, kein Spam, keine Werbung.</gray>",
            "<white>Kein Griefing.</white> <gray>Fremde Bauten bleiben unangetastet.</gray>",
            "<white>Kein Stehlen.</white> <gray>Finger weg von fremden Kisten.</gray>",
            "<white>Keine Cheats.</white> <gray>Keine Hacks, X-Ray oder unerlaubten Mods.</gray>",
            "<white>Keine Lag-Maschinen.</white> <gray>Redstone-Uhren und Mob-Massen bitte im Rahmen halten.</gray>",
            "<white>Probleme melden.</white> <gray>Bei Ärger erst reden, dann das Team fragen.</gray>");

    private final CompanionPlugin plugin;

    public RulesCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!plugin.settings().raw().getBoolean("features.rules", true)) {
            Msg.error(sender, "Die Regelanzeige ist auf diesem Server deaktiviert.");
            return true;
        }
        List<String> rules = plugin.settings().raw().getStringList("rules");
        if (rules.isEmpty()) {
            rules = DEFAULT_RULES;
        }
        sender.sendMessage(Msg.mm("<dark_gray>――――――――――――――――――</dark_gray>"));
        sender.sendMessage(Msg.prefixed("<green><bold>Regeln auf <server></bold></green>",
                Msg.text("server", plugin.settings().serverName)));
        int i = 0;
        for (String rule : rules) {
            i++;
            sender.sendMessage(Msg.mm("<gray>" + i + ".</gray> " + rule));
        }
        sender.sendMessage(Msg.mm("<dark_gray>――――――――――――――――――</dark_gray>"));
        sender.sendMessage(Msg.mm("<gray>Alle Befehle: </gray>"
                + "<click:suggest_command:'/mcsm'><white>/mcsm</white></click>"));
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return Collections.emptyList();
    }
}
