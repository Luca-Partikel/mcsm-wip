package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;

/** /hardcore <on|off|status> – Recht mcsm.hardcore (Standard: OP), auch von der Konsole. */
public final class HardcoreCommand implements TabExecutor {

    private static final List<String> SUB = List.of("on", "off", "status");

    private final CompanionPlugin plugin;

    public HardcoreCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        HardcoreManager hc = plugin.hardcore();
        if (args.length != 1) {
            usage(sender);
            return true;
        }
        switch (args[0].toLowerCase(Locale.ROOT)) {
            case "on":
                if (!hc.enable()) {
                    Msg.send(sender, "<gray>Der Hardcore-Modus ist bereits aktiv.</gray>");
                    return true;
                }
                Bukkit.broadcast(Msg.prefixed("<red>Hardcore-Modus aktiviert!</red> <gray>Wer ab jetzt stirbt, bleibt tot, "
                        + "bis ein Mitspieler ihn am Grab mit einem Totem der Unsterblichkeit wiederbelebt.</gray>"));
                return true;
            case "off":
                if (!hc.isEnabled() && hc.graves().isEmpty()) {
                    Msg.send(sender, "<gray>Der Hardcore-Modus ist bereits inaktiv.</gray>");
                    return true;
                }
                int n = hc.disable();
                Bukkit.broadcast(Msg.prefixed("<green>Hardcore-Modus deaktiviert.</green> "
                        + "<gray><n> Spieler wiederbelebt, alle Gräber entfernt.</gray>", Msg.number("n", n)));
                return true;
            case "status":
                status(sender, hc);
                return true;
            default:
                usage(sender);
                return true;
        }
    }

    private static void usage(CommandSender sender) {
        Msg.send(sender, "<gray>Verwendung: <white>/hardcore <on|off|status></white></gray>");
    }

    private static void status(CommandSender sender, HardcoreManager hc) {
        Msg.send(sender, hc.isEnabled()
                ? "<gray>Hardcore-Modus: <red>aktiv</red></gray>"
                : "<gray>Hardcore-Modus: <green>inaktiv</green></gray>");
        if (hc.graves().isEmpty()) {
            Msg.send(sender, "<gray>Tote Spieler: <white>keine</white></gray>");
            return;
        }
        Msg.send(sender, "<gray>Tote Spieler (<white><n></white>):</gray>", Msg.number("n", hc.graves().size()));
        for (GraveStore.Grave g : hc.graves()) {
            Msg.send(sender, "<gray>- <white><line></white></gray>", Msg.text("line", HardcoreManager.describe(g)));
        }
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length == 1) {
            String prefix = args[0].toLowerCase(Locale.ROOT);
            for (String s : SUB) {
                if (s.startsWith(prefix)) {
                    out.add(s);
                }
            }
        }
        return out;
    }
}
