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
 * /admin – Wartungsbefehle für den Wartungszugang, OPs und Inhaber von mcsm.admin.
 *
 * <p>/admin join und /admin vanish wechseln zwischen zwei dauerhaft getrennten Spielerprofilen
 * (siehe {@link AdminProfiles}); die übrigen Unterbefehle sind Eingriffe im laufenden Betrieb.</p>
 */
public final class AdminCommand implements TabExecutor {

    private static final List<String> SUB = List.of("join", "vanish", "profile", "status", "reload",
            "inv", "ec", "heal", "feed", "fly", "god", "speed", "save", "restart");
    private static final String USAGE =
            "<gray>Verwendung: <white>/admin <join|vanish|profile|status|reload></white></gray>\n"
            + "<gray>Eingriffe: <white>/admin <inv|ec> <Spieler></white>, "
            + "<white>/admin <heal|feed|fly|god> [Spieler]</white></gray>\n"
            + "<gray>Weiter: <white>/admin speed <1-10> [Spieler]</white>, <white>/admin save</white>, "
            + "<white>/admin restart <Sekunden|abbrechen></white></gray>";

    private final CompanionPlugin plugin;
    private final AdminTools tools;

    public AdminCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.tools = plugin.adminTools();
    }

    private boolean allowed(CommandSender sender) {
        if (!(sender instanceof Player)) {
            return true;                                   // Konsole
        }
        return plugin.settings().isAdmin(sender.getName()) || sender.isOp() || sender.hasPermission("mcsm.admin");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!allowed(sender)) {
            Msg.error(sender, "Dazu hast du keine Berechtigung.");
            return true;
        }
        if (args.length == 0) {
            Msg.send(sender, USAGE);
            return true;
        }
        switch (args[0].toLowerCase(Locale.ROOT)) {
            case "join": return join(sender);
            case "vanish": return vanish(sender);
            case "profile": case "profil": return profile(sender);
            case "status":
                sender.sendMessage(plugin.adminInfo(sender instanceof Player p ? p : null));
                return true;
            case "reload":
                plugin.reloadAll();
                Msg.send(sender, "<green>Konfiguration neu geladen.</green>");
                return true;
            case "inv": return openInventory(sender, args, false);
            case "ec": return openInventory(sender, args, true);
            case "heal": return heal(sender, args);
            case "feed": return feed(sender, args);
            case "fly": return fly(sender, args);
            case "god": return god(sender, args);
            case "speed": return speed(sender, args);
            case "save": return save(sender);
            case "restart": return restart(sender, args);
            default:
                Msg.send(sender, USAGE);
                return true;
        }
    }

    // ------------------------------------------------------------------ Sichtbarkeit und Profile

    private boolean join(CommandSender sender) {
        if (!(sender instanceof Player p)) {
            Msg.error(sender, "Dieser Unterbefehl ist nur für Spieler.");
            return true;
        }
        AdminProfiles profiles = plugin.vanish().profiles();
        if (!plugin.vanish().isVanished(p)) {
            // Sichtbar, aber noch im Unsichtbar-Profil (z. B. nach einem Plugin-Neustart):
            // dann nur das Profil geradeziehen, ohne erneute Beitrittsmeldung.
            if (profiles.handles(p) && AdminProfileStore.SLOT_VANISH.equals(profiles.active(p))) {
                plugin.vanish().joinNormal(p);
                Msg.send(p, "<green>Dein <white>Normal</white>-Profil ist wieder aktiv, Spielmodus "
                        + "<white><m></white>.</green>", Msg.text("m", Config.gameModeName(p.getGameMode())));
                return true;
            }
            Msg.send(p, "<gray>Du bist bereits sichtbar.</gray>");
            return true;
        }
        boolean switched = plugin.vanish().joinNormal(p);
        if (plugin.settings().featJoinLeave) {
            Bukkit.broadcast(Msg.mm(plugin.settings().joinFormat, Msg.name("name", p)));
        }
        plugin.tablist().refreshLater();
        plugin.status().write();
        Msg.send(p, "<green>Du bist jetzt sichtbar und normal beigetreten.</green> "
                + "<gray>Mit <white>/admin vanish</white> wirst du wieder unsichtbar.</gray>");
        if (switched) {
            Msg.send(p, "<gray>Dein <white>Normal</white>-Profil ist aktiv, Spielmodus "
                    + "<white><m></white>.</gray>", Msg.text("m", Config.gameModeName(p.getGameMode())));
        }
        return true;
    }

    private boolean vanish(CommandSender sender) {
        if (!(sender instanceof Player p)) {
            Msg.error(sender, "Dieser Unterbefehl ist nur für Spieler.");
            return true;
        }
        if (plugin.vanish().isVanished(p)) {
            Msg.send(p, "<gray>Du bist bereits unsichtbar.</gray>");
            return true;
        }
        if (plugin.settings().featJoinLeave) {
            Bukkit.broadcast(Msg.mm(plugin.settings().leaveFormat, Msg.name("name", p)));
        }
        plugin.vanish().vanish(p);
        plugin.tablist().refreshLater();
        plugin.status().write();
        Msg.send(p, "<green>Du bist jetzt unsichtbar.</green> "
                + "<gray>Mit <white>/admin join</white> wirst du wieder sichtbar.</gray>");
        Msg.send(p, "<gray>Dein <white>Unsichtbar</white>-Profil ist aktiv, Spielmodus "
                + "<white><m></white>.</gray>", Msg.text("m", Config.gameModeName(p.getGameMode())));
        return true;
    }

    private boolean profile(CommandSender sender) {
        if (!(sender instanceof Player p)) {
            Msg.error(sender, "Dieser Unterbefehl ist nur für Spieler.");
            return true;
        }
        AdminProfiles profiles = plugin.vanish().profiles();
        if (!profiles.enabled()) {
            Msg.send(sender, "<gray>Die getrennten Wartungsprofile sind abgeschaltet "
                    + "<dark_gray>(features.admin_profiles)</dark_gray>.</gray>");
            return true;
        }
        if (!profiles.handles(p)) {
            Msg.send(sender, "<gray>Für dich gelten keine getrennten Wartungsprofile.</gray>");
            return true;
        }
        Msg.send(sender, "<gray>Aktives Profil: <white><a></white></gray>",
                Msg.text("a", AdminProfileStore.slotName(profiles.active(p))));
        Msg.send(sender, "<dark_gray>•</dark_gray> <gray><v></gray>",
                Msg.text("v", profiles.describe(p, AdminProfileStore.SLOT_VANISH)));
        Msg.send(sender, "<dark_gray>•</dark_gray> <gray><n></gray>",
                Msg.text("n", profiles.describe(p, AdminProfileStore.SLOT_NORMAL)));
        return true;
    }

    // ------------------------------------------------------------------ Eingriffe

    private boolean openInventory(CommandSender sender, String[] args, boolean enderChest) {
        if (!(sender instanceof Player viewer)) {
            Msg.error(sender, "Dieser Unterbefehl ist nur für Spieler.");
            return true;
        }
        if (args.length != 2) {
            Msg.send(sender, "<gray>Verwendung: <white>/admin <c> <Spieler></white></gray>",
                    Msg.text("c", enderChest ? "ec" : "inv"));
            return true;
        }
        Player target = plugin.findPlayer(args[1]);
        if (target == null) {
            Msg.error(sender, "Spieler <white><n></white> wurde nicht gefunden.", Msg.text("n", args[1]));
            return true;
        }
        viewer.openInventory(enderChest ? target.getEnderChest() : target.getInventory());
        Msg.send(sender, "<gray><w> von <white><n></white> geöffnet.</gray>",
                Msg.text("w", enderChest ? "Endertruhe" : "Inventar"), Msg.name("n", target));
        return true;
    }

    private boolean heal(CommandSender sender, String[] args) {
        Player target = target(sender, args, 1);
        if (target == null) {
            return true;
        }
        tools.heal(target);
        report(sender, target, "geheilt und gesättigt", "Du wurdest geheilt und gesättigt.");
        return true;
    }

    private boolean feed(CommandSender sender, String[] args) {
        Player target = target(sender, args, 1);
        if (target == null) {
            return true;
        }
        tools.feed(target);
        report(sender, target, "gesättigt", "Du wurdest gesättigt.");
        return true;
    }

    private boolean fly(CommandSender sender, String[] args) {
        Player target = target(sender, args, 1);
        if (target == null) {
            return true;
        }
        boolean on = tools.toggleFly(target);
        report(sender, target, on ? "darf jetzt fliegen" : "darf nicht mehr fliegen",
                on ? "Du darfst jetzt fliegen." : "Du darfst nicht mehr fliegen.");
        return true;
    }

    private boolean god(CommandSender sender, String[] args) {
        Player target = target(sender, args, 1);
        if (target == null) {
            return true;
        }
        boolean on = tools.toggleGod(target);
        report(sender, target, on ? "ist jetzt unverwundbar" : "ist wieder verwundbar",
                on ? "Du bist jetzt unverwundbar." : "Du bist wieder verwundbar.");
        return true;
    }

    private boolean speed(CommandSender sender, String[] args) {
        if (args.length < 2 || args.length > 3) {
            Msg.send(sender, "<gray>Verwendung: <white>/admin speed <1-10> [Spieler]</white> "
                    + "<dark_gray>(1 = normal, 10 = Höchstwert)</dark_gray></gray>");
            return true;
        }
        int level;
        try {
            level = Integer.parseInt(args[1].trim());
        } catch (NumberFormatException ex) {
            Msg.error(sender, "<white><v></white> ist keine Zahl von 1 bis 10.", Msg.text("v", args[1]));
            return true;
        }
        if (level < 1 || level > 10) {
            Msg.error(sender, "Bitte eine Stufe von 1 bis 10 angeben.");
            return true;
        }
        Player target = target(sender, args, 2);
        if (target == null) {
            return true;
        }
        boolean flying = tools.applySpeed(target, level);
        String what = flying ? "Fluggeschwindigkeit" : "Gehgeschwindigkeit";
        if (target.equals(sender)) {
            Msg.send(sender, "<gray>Deine <white><w></white> steht jetzt auf Stufe <white><l></white>.</gray>",
                    Msg.text("w", what), Msg.number("l", level));
        } else {
            Msg.send(sender, "<gray><w> von <white><n></white> auf Stufe <white><l></white> gesetzt.</gray>",
                    Msg.text("w", what), Msg.name("n", target), Msg.number("l", level));
            Msg.send(target, "<gray>Deine <white><w></white> steht jetzt auf Stufe <white><l></white>.</gray>",
                    Msg.text("w", what), Msg.number("l", level));
        }
        return true;
    }

    private boolean save(CommandSender sender) {
        Msg.send(sender, "<gray>Welten werden gesichert …</gray>");
        long ms = tools.saveWorlds();
        Msg.send(sender, "<green>Alle Welten und Spielerdaten gesichert</green> "
                + "<dark_gray>(<white><ms></white> ms, <white><n></white> Welten)</dark_gray>",
                Msg.number("ms", ms), Msg.number("n", Bukkit.getWorlds().size()));
        return true;
    }

    private boolean restart(CommandSender sender, String[] args) {
        if (args.length != 2) {
            if (tools.restartRunning()) {
                Msg.send(sender, "<gray>Ein Neustart läuft bereits: noch <white><t></white>. "
                        + "Abbrechen mit <white>/admin restart abbrechen</white>.</gray>",
                        Msg.text("t", AdminTools.human(tools.restartSeconds())));
            } else {
                Msg.send(sender, "<gray>Verwendung: <white>/admin restart <Sekunden></white> "
                        + "<dark_gray>(0 = sofort, \"abbrechen\" stoppt den Countdown)</dark_gray></gray>");
            }
            return true;
        }
        String arg = args[1].toLowerCase(Locale.ROOT);
        if (arg.equals("abbrechen") || arg.equals("cancel") || arg.equals("stop")) {
            if (tools.cancelRestart()) {
                Msg.send(sender, "<green>Neustart abgebrochen.</green>");
            } else {
                Msg.send(sender, "<gray>Es läuft gerade kein Neustart.</gray>");
            }
            return true;
        }
        int seconds;
        try {
            seconds = Integer.parseInt(arg);
        } catch (NumberFormatException ex) {
            Msg.error(sender, "<white><v></white> ist keine Sekundenzahl.", Msg.text("v", args[1]));
            return true;
        }
        if (seconds < 0 || seconds > AdminTools.MAX_RESTART_SECONDS) {
            Msg.error(sender, "Bitte 0 bis <white><max></white> Sekunden angeben.",
                    Msg.number("max", AdminTools.MAX_RESTART_SECONDS));
            return true;
        }
        plugin.getLogger().info("Neustart in " + seconds + " s, ausgelöst von " + sender.getName() + ".");
        if (seconds > 0) {
            Msg.send(sender, "<gold>Neustart in <white><t></white>.</gold> "
                    + "<gray>Abbrechen mit <white>/admin restart abbrechen</white>.</gray>",
                    Msg.text("t", AdminTools.human(seconds)));
        }
        tools.startRestart(seconds);
        return true;
    }

    // ------------------------------------------------------------------ Helfer

    /**
     * Zielspieler eines Unterbefehls: Argument an Position {@code index} oder der Absender selbst.
     * Gibt null zurück und meldet den Fehler, wenn kein Ziel ermittelbar ist.
     */
    private Player target(CommandSender sender, String[] args, int index) {
        if (args.length > index + 1) {
            Msg.error(sender, "Zu viele Angaben für <white>/admin <c></white>.", Msg.text("c", args[0]));
            return null;
        }
        if (args.length == index + 1) {
            Player p = plugin.findPlayer(args[index]);
            if (p == null) {
                Msg.error(sender, "Spieler <white><n></white> wurde nicht gefunden.", Msg.text("n", args[index]));
            }
            return p;
        }
        if (sender instanceof Player self) {
            return self;
        }
        Msg.error(sender, "Von der Konsole aus bitte einen Spieler angeben.");
        return null;
    }

    private static void report(CommandSender sender, Player target, String forSender, String forTarget) {
        if (target.equals(sender)) {
            Msg.send(sender, "<green><t></green>", Msg.text("t", forTarget));
            return;
        }
        Msg.send(sender, "<gray><white><n></white> <t>.</gray>", Msg.name("n", target), Msg.text("t", forSender));
        Msg.send(target, "<green><t></green>", Msg.text("t", forTarget));
    }

    // ------------------------------------------------------------------ Vervollständigung

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (!allowed(sender)) {
            return out;
        }
        if (args.length == 1) {
            addMatches(out, args[0], SUB);
            return out;
        }
        String sub = args[0].toLowerCase(Locale.ROOT);
        if (args.length == 2) {
            switch (sub) {
                case "inv": case "ec": case "heal": case "feed": case "fly": case "god":
                    addPlayers(out, sender, args[1]);
                    return out;
                case "speed":
                    addMatches(out, args[1], List.of("1", "2", "3", "4", "5", "6", "7", "8", "9", "10"));
                    return out;
                case "restart":
                    addMatches(out, args[1], List.of("0", "10", "30", "60", "300", "abbrechen"));
                    return out;
                default:
                    return out;
            }
        }
        if (args.length == 3 && sub.equals("speed")) {
            addPlayers(out, sender, args[2]);
        }
        return out;
    }

    private static void addMatches(List<String> out, String prefix, List<String> options) {
        String p = prefix.toLowerCase(Locale.ROOT);
        for (String s : options) {
            if (s.startsWith(p)) {
                out.add(s);
            }
        }
    }

    private void addPlayers(List<String> out, CommandSender sender, String prefix) {
        String p = prefix.toLowerCase(Locale.ROOT);
        for (Player online : plugin.visiblePlayers(sender)) {
            if (online.getName().toLowerCase(Locale.ROOT).startsWith(p)) {
                out.add(online.getName());
            }
        }
    }
}
