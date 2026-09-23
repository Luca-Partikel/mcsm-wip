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
 * /mute, /tempmute, /unmute und /mutelist – Stummschaltungen aus mutes.yml.
 *
 * <p>Alle Befehle sind auch von der Konsole nutzbar. Wer stummgeschaltet ist, darf weder im Chat
 * schreiben noch die in der Konfiguration gelisteten Umgehungsbefehle benutzen.</p>
 */
public final class MuteCommands implements TabExecutor {

    private final CompanionPlugin plugin;
    private final BanService service;

    public MuteCommands(CompanionPlugin plugin, BanService service) {
        this.plugin = plugin;
        this.service = service;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!service.muteEnabled()) {
            Msg.error(sender, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "mute": return mute(sender, args, false);
            case "tempmute": return mute(sender, args, true);
            case "unmute": return unmute(sender, args);
            case "mutelist": return list(sender);
            default: return false;
        }
    }

    private boolean mute(CommandSender sender, String[] args, boolean temp) {
        if (args.length < (temp ? 2 : 1)) {
            Msg.send(sender, temp
                    ? "<gray>Verwendung: <white>/tempmute <Spieler> <Dauer> [Grund]</white> "
                      + "<dark_gray>(z. B. 30m, 12h, 7d, 2w)</dark_gray></gray>"
                    : "<gray>Verwendung: <white>/mute <Spieler> [Grund]</white></gray>");
            return true;
        }

        long expiresAt = 0L;
        if (temp) {
            long span = BanService.parseDuration(args[1]);
            if (span <= 0L) {
                Msg.error(sender, "<white><d></white> ist keine gültige Dauer. Beispiele: "
                                + "<white>30m</white>, <white>12h</white>, <white>7d</white>, <white>2w</white>.",
                        Msg.text("d", args[1]));
                return true;
            }
            expiresAt = System.currentTimeMillis() + span;
        }

        String reason = BanService.joinReason(args, temp ? 2 : 1);
        if (reason.isBlank()) {
            reason = service.defaultReason();
        }

        BanService.Target target = service.resolve(args[0]);
        if (target == null) {
            Msg.error(sender, "<white><n></white> ist unbekannt – dieser Spieler war noch nie hier.",
                    Msg.text("n", args[0]));
            return true;
        }
        if (sender instanceof Player p && p.getUniqueId().equals(target.id)) {
            Msg.error(sender, "Du kannst dich nicht selbst stummschalten.");
            return true;
        }
        if (service.isProtected(target.id, target.name)) {
            Msg.error(sender, "<white><n></white> kann nicht stummgeschaltet werden.",
                    Msg.text("n", target.name));
            return true;
        }
        MuteStore.Mute old = service.mutes().active(target.id);
        if (old != null) {
            Msg.send(sender, "<gray><white><n></white> war bereits stummgeschaltet "
                            + "<dark_gray>(<e>)</dark_gray> – die Angabe wird überschrieben.</gray>",
                    Msg.text("n", target.name),
                    Msg.text("e", BanService.expiryText(old.expires)));
        }

        service.mute(target, reason, expiresAt, BanService.actorName(sender));
        if (temp) {
            Msg.send(sender, "<gray><white><n></white> ist für <white><d></white> stummgeschaltet: "
                            + "<white><r></white></gray>",
                    Msg.text("n", target.name),
                    Msg.text("d", BanService.remaining(expiresAt - System.currentTimeMillis())),
                    Msg.text("r", reason));
        } else {
            Msg.send(sender, "<gray><white><n></white> ist dauerhaft stummgeschaltet: <white><r></white></gray>",
                    Msg.text("n", target.name), Msg.text("r", reason));
        }
        return true;
    }

    private boolean unmute(CommandSender sender, String[] args) {
        if (args.length < 1) {
            Msg.send(sender, "<gray>Verwendung: <white>/unmute <Spieler></white></gray>");
            return true;
        }
        UUID id = service.mutes().byName(args[0]);
        if (id == null) {
            BanService.Target target = service.resolve(args[0]);
            id = target == null ? null : target.id;
        }
        MuteStore.Mute gone = id == null ? null : service.unmute(id, BanService.actorName(sender));
        if (gone == null) {
            Msg.error(sender, "<white><n></white> ist nicht stummgeschaltet.", Msg.text("n", args[0]));
            return true;
        }
        Msg.send(sender, "<gray>Die Stummschaltung von <white><n></white> wurde aufgehoben.</gray>",
                Msg.text("n", gone.name));
        return true;
    }

    private boolean list(CommandSender sender) {
        List<MuteStore.Mute> all = service.mutes().all();
        if (all.isEmpty()) {
            Msg.send(sender, "<gray>Zurzeit ist niemand stummgeschaltet.</gray>");
            return true;
        }
        Msg.send(sender, "<gray>Stummgeschaltet <dark_gray>(</dark_gray><white><n></white><dark_gray>)</dark_gray>"
                + "<gray>:</gray>", Msg.number("n", all.size()));
        for (MuteStore.Mute m : all) {
            sender.sendMessage(Msg.mm("<dark_gray>•</dark_gray> <white><n></white> "
                            + "<dark_gray>|</dark_gray> <gray><r></gray> <dark_gray>|</dark_gray> "
                            + "<gray>von <white><s></white>, <white><e></white></gray>",
                    Msg.text("n", m.name),
                    Msg.text("r", m.reason),
                    Msg.text("s", m.source),
                    Msg.text("e", BanService.expiryText(m.expires))));
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        String name = command.getName().toLowerCase(Locale.ROOT);
        if (name.equals("mutelist")) {
            return out;
        }
        if (args.length == 1) {
            String prefix = args[0].toLowerCase(Locale.ROOT);
            if (name.equals("unmute")) {
                for (String n : service.mutes().names()) {
                    if (n.toLowerCase(Locale.ROOT).startsWith(prefix)) {
                        out.add(n);
                    }
                }
                return out;
            }
            for (Player p : plugin.visiblePlayers(sender)) {
                if (p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                    out.add(p.getName());
                }
            }
            return out;
        }
        if (args.length == 2 && name.equals("tempmute")) {
            return BanService.durationSuggestions(args[1]);
        }
        return out;
    }
}
