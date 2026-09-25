package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /warn und /warns – Verwarnungen mit Verlauf aus warns.yml. Ist die Automatik eingeschaltet,
 * folgt ab einer einstellbaren Zahl von Verwarnungen eine Zeitsperre.
 */
public final class WarnCommands implements TabExecutor {

    private final CompanionPlugin plugin;
    private final BanService service;

    public WarnCommands(CompanionPlugin plugin, BanService service) {
        this.plugin = plugin;
        this.service = service;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!service.warnEnabled()) {
            Msg.error(sender, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        if (command.getName().equalsIgnoreCase("warns")) {
            return history(sender, args);
        }
        return warn(sender, args);
    }

    private boolean warn(CommandSender sender, String[] args) {
        if (args.length < 2) {
            Msg.line(sender, "<gray>Verwendung: <white>/warn <Spieler> <Grund></white></gray>");
            return true;
        }
        String reason = BanService.joinReason(args, 1);
        BanService.Target target = service.resolve(args[0]);
        if (target == null) {
            Msg.error(sender, "<white><n></white> ist unbekannt – dieser Spieler war noch nie hier.",
                    Msg.text("n", args[0]));
            return true;
        }
        if (sender instanceof Player p && p.getUniqueId().equals(target.id)) {
            Msg.error(sender, "Du kannst dich nicht selbst verwarnen.");
            return true;
        }
        if (service.isProtected(target.id, target.name)) {
            Msg.error(sender, "<white><n></white> kann nicht verwarnt werden.", Msg.text("n", target.name));
            return true;
        }
        int total = service.warn(target, reason, BanService.actorName(sender));
        Msg.send(sender, "<gray><white><n></white> wurde verwarnt <dark_gray>(</dark_gray>"
                        + "<white><c></white><gray>. Verwarnung</gray><dark_gray>)</dark_gray>"
                        + "<gray>:</gray> <white><r></white></gray>",
                Msg.text("n", target.name), Msg.number("c", total), Msg.text("r", reason));
        return true;
    }

    private boolean history(CommandSender sender, String[] args) {
        if (args.length < 1) {
            Msg.send(sender, "<gray>Verwendung: <white>/warns <Spieler></white></gray>");
            return true;
        }
        UUID id = service.warns().byName(args[0]);
        String name = args[0];
        if (id == null) {
            BanService.Target target = service.resolve(args[0]);
            if (target != null) {
                id = target.id;
                name = target.name;
            }
        } else {
            name = service.warns().nameOf(id);
        }
        if (id == null) {
            Msg.error(sender, "<white><n></white> ist unbekannt.", Msg.text("n", args[0]));
            return true;
        }
        List<WarnStore.Warn> list = service.warns().list(id);
        if (list.isEmpty()) {
            Msg.line(sender, "<gray><white><n></white> hat keine Verwarnungen.</gray>", Msg.text("n", name));
            return true;
        }

        boolean auto = service.cfg().getBoolean("warn.auto_ban", false);
        int threshold = Math.max(1, service.cfg().getInt("warn.auto_ban_threshold", 3));
        long maxAge = Math.max(0, service.cfg().getInt("warn.expire_days", 0)) * 86_400_000L;
        int counted = service.warns().count(id, maxAge);
        if (auto) {
            Msg.line(sender, "<gray>Verwarnungen von <white><n></white><dark_gray>:</dark_gray> "
                            + "<white><c></white> <gray>von</gray> <white><t></white> "
                            + "<gray>bis zur automatischen Sperre.</gray>",
                    Msg.text("n", name), Msg.number("c", counted), Msg.number("t", threshold));
        } else {
            Msg.line(sender, "<gray>Verwarnungen von <white><n></white><dark_gray>:</dark_gray> "
                            + "<white><c></white></gray>",
                    Msg.text("n", name), Msg.number("c", list.size()));
        }
        int index = 1;
        for (WarnStore.Warn w : list) {
            sender.sendMessage(Msg.mm("<dark_gray>•</dark_gray> <white><i>.</white> "
                            + "<gray><d></gray> <dark_gray>|</dark_gray> <gray><r></gray> "
                            + "<dark_gray>|</dark_gray> <gray>von <white><s></white></gray>",
                    Msg.number("i", index),
                    Msg.text("d", BanService.stamp(w.time)),
                    Msg.text("r", w.reason),
                    Msg.text("s", w.source)));
            index++;
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
        if (command.getName().equalsIgnoreCase("warns")) {
            for (String n : service.warns().names()) {
                if (n.toLowerCase(Locale.ROOT).startsWith(prefix) && !out.contains(n)) {
                    out.add(n);
                }
            }
        }
        return out;
    }
}
