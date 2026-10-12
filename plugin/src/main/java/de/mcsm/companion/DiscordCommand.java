package de.mcsm.companion;

import java.util.Collections;
import java.util.List;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;

/**
 * /discord – zeigt den im Programm hinterlegten Einladungslink als anklickbaren Link. Ist keiner
 * gesetzt (oder kein gültiger http/https-Link), sagt der Befehl das freundlich.
 */
public final class DiscordCommand implements TabExecutor {

    private final CompanionPlugin plugin;

    public DiscordCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        String url = plugin.settings().raw().getString("discord_url", "").trim();
        // Nur ein sauberer http/https-Link ohne Leerzeichen/Anführungszeichen/Winkelklammern – so ist
        // das Einbauen in die MiniMessage-Klick-Aktion gefahrlos.
        if (!url.matches("https?://[^\\s'\"<>]+")) {
            Msg.error(sender, "Für diesen Server ist (noch) kein Discord-Link hinterlegt.");
            return true;
        }
        sender.sendMessage(Msg.prefixed(
                "<gray>Unser Discord: </gray>"
                + "<click:open_url:'" + url + "'><hover:show_text:'<gray>Zum Öffnen klicken</gray>'>"
                + "<aqua><underlined>" + url + "</underlined></aqua></hover></click>"));
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return Collections.emptyList();
    }
}
