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
 * /wl – Freigabeliste verwalten. Recht mcsm.whitelist (Standard: OP), auch von der Konsole.
 *
 * <p>Der Name /whitelist ist von Vanilla belegt, deshalb /wl mit dem Alias /freigabe.
 * Alle Änderungen laufen über die eingebaute Bukkit-Whitelist, damit whitelist.json
 * unverändert im Vanilla-Format bleibt.</p>
 */
public final class WhitelistCommand implements TabExecutor {

    private static final List<String> SUB =
            List.of("an", "aus", "add", "remove", "list", "reload", "status");
    private static final String USAGE =
            "<gray>Verwendung: <white>/wl <an|aus|status></white></gray>\n"
            + "<gray>Eintragen: <white>/wl add <Spieler></white>, "
            + "<white>/wl remove <Spieler></white></gray>\n"
            + "<gray>Weiter: <white>/wl list</white>, <white>/wl reload</white></gray>";

    private final CompanionPlugin plugin;
    private final WhitelistService whitelist;

    public WhitelistCommand(CompanionPlugin plugin, WhitelistService whitelist) {
        this.plugin = plugin;
        this.whitelist = whitelist;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!whitelist.featureOn()) {
            Msg.error(sender, "Die Freigabeliste ist auf diesem Server deaktiviert.");
            return true;
        }
        if (args.length == 0) {
            Msg.send(sender, USAGE);
            status(sender);
            return true;
        }
        String sub = args[0].toLowerCase(Locale.ROOT);
        Boolean toggle = WhitelistService.parseSwitch(sub);
        if (toggle != null) {
            return toggle(sender, toggle.booleanValue());
        }
        switch (sub) {
            case "add": case "hinzu": case "hinzufuegen":
                return add(sender, args);
            case "remove": case "rem": case "entfernen": case "raus":
                return remove(sender, args);
            case "list": case "liste":
                return list(sender);
            case "reload": case "neuladen":
                whitelist.reload();
                Msg.send(sender, "<green>whitelist.json wurde neu eingelesen.</green> "
                        + "<gray>Einträge: <white><c></white></gray>", Msg.number("c", whitelist.names().size()));
                return true;
            case "status":
                status(sender);
                return true;
            default:
                Msg.send(sender, USAGE);
                return true;
        }
    }

    // ------------------------------------------------------------------ Unterbefehle

    private boolean toggle(CommandSender sender, boolean on) {
        if (whitelist.isOn() == on) {
            Msg.send(sender, on
                    ? "<gray>Die Freigabeliste ist bereits <white>aktiv</white>.</gray>"
                    : "<gray>Die Freigabeliste ist bereits <white>aus</white>.</gray>");
            return true;
        }
        // Die Chatmeldung für alle kommt aus WhitelistListener, damit sie auch beim
        // Vanilla-Befehl und beim Manager erscheint. Fällt sie aus (abgeschaltet oder niemand
        // online), bekommt wenigstens der Absender eine Bestätigung.
        boolean broadcast = whitelist.broadcastToggle() && !Bukkit.getOnlinePlayers().isEmpty();
        whitelist.setOn(on);
        if (!broadcast) {
            Msg.send(sender, on
                    ? "<green>Die Freigabeliste ist jetzt aktiv.</green>"
                    : "<yellow>Die Freigabeliste ist jetzt aus.</yellow>");
        }
        return true;
    }

    private boolean add(CommandSender sender, String[] args) {
        if (args.length != 2) {
            Msg.send(sender, "<gray>Verwendung: <white>/wl add <Spieler></white></gray>");
            return true;
        }
        whitelist.add(sender, args[1]);
        return true;
    }

    private boolean remove(CommandSender sender, String[] args) {
        if (args.length != 2) {
            Msg.send(sender, "<gray>Verwendung: <white>/wl remove <Spieler></white></gray>");
            return true;
        }
        whitelist.remove(sender, args[1]);
        return true;
    }

    private boolean list(CommandSender sender) {
        List<String> names = whitelist.names();
        if (names.isEmpty()) {
            Msg.send(sender, "<gray>Die Freigabeliste ist <white>leer</white>.</gray>");
            return true;
        }
        Msg.send(sender, "<gray>Freigabeliste (<white><c></white>):</gray>", Msg.number("c", names.size()));
        StringBuilder line = new StringBuilder();
        for (String name : names) {
            boolean online = Bukkit.getPlayerExact(name) != null;
            if (line.length() > 0) {
                line.append("<dark_gray>, </dark_gray>");
            }
            line.append(online ? "<green>" : "<gray>").append(name).append(online ? "</green>" : "</gray>");
        }
        sender.sendMessage(Msg.mm(line.toString()));
        return true;
    }

    private void status(CommandSender sender) {
        Msg.send(sender, whitelist.isOn()
                ? "<gray>Freigabeliste: <green>aktiv</green> <dark_gray>•</dark_gray> Einträge <white><c></white></gray>"
                : "<gray>Freigabeliste: <yellow>aus</yellow> <dark_gray>•</dark_gray> Einträge <white><c></white></gray>",
                Msg.number("c", whitelist.names().size()));
    }

    // ------------------------------------------------------------------ Tab-Vervollständigung

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
            return out;
        }
        if (args.length != 2) {
            return out;
        }
        String prefix = args[1].toLowerCase(Locale.ROOT);
        String sub = args[0].toLowerCase(Locale.ROOT);
        if (sub.equals("remove") || sub.equals("rem") || sub.equals("entfernen") || sub.equals("raus")) {
            for (String name : whitelist.names()) {
                if (name.toLowerCase(Locale.ROOT).startsWith(prefix)) {
                    out.add(name);
                }
            }
            return out;
        }
        if (sub.equals("add") || sub.equals("hinzu") || sub.equals("hinzufuegen")) {
            for (Player p : plugin.visiblePlayers(sender)) {
                String name = p.getName();
                if (name.toLowerCase(Locale.ROOT).startsWith(prefix) && !whitelist.contains(name)) {
                    out.add(name);
                }
            }
        }
        return out;
    }
}
