package de.mcsm.companion;

import java.net.InetAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

import com.destroystokyo.paper.profile.PlayerProfile;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import org.bukkit.BanEntry;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /ban, /tempban, /unban (auch /pardon2), /banlist und /kick.
 *
 * <p>Alle Befehle sind auch von der Konsole nutzbar. Gesperrt wird über die eingebaute BanList,
 * die Texte kommen aus der Konfiguration.</p>
 */
public final class BanCommands implements TabExecutor {

    private final CompanionPlugin plugin;
    private final BanService service;

    public BanCommands(CompanionPlugin plugin, BanService service) {
        this.plugin = plugin;
        this.service = service;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!service.banEnabled()) {
            Msg.error(sender, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "ban": return ban(sender, args, false);
            case "tempban": return ban(sender, args, true);
            case "unban": return unban(sender, args);
            case "banlist": return list(sender, args);
            case "kick": return kick(sender, args);
            default: return false;
        }
    }

    // ---------------------------------------------------------------- Sperren

    private boolean ban(CommandSender sender, String[] args, boolean temp) {
        if (args.length < (temp ? 2 : 1)) {
            Msg.send(sender, temp
                    ? "<gray>Verwendung: <white>/tempban <Spieler> <Dauer> [Grund]</white> "
                      + "<dark_gray>(z. B. 30m, 12h, 7d, 2w)</dark_gray></gray>"
                    : "<gray>Verwendung: <white>/ban <Spieler> [Grund]</white></gray>");
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
        if (isSelf(sender, target.id)) {
            Msg.error(sender, "Du kannst dich nicht selbst sperren.");
            return true;
        }
        if (service.isProtected(target.id, target.name)) {
            Msg.error(sender, "<white><n></white> kann nicht gesperrt werden.", Msg.text("n", target.name));
            return true;
        }
        BanEntry<PlayerProfile> old = service.findBan(target.name);
        if (old != null) {
            // Wie beim Stummschalten: eine bestehende Sperre wird ueberschrieben, damit sich Dauer
            // und Grund aendern lassen, ohne vorher zu entsperren.
            Msg.send(sender, "<gray><white><n></white> war bereits gesperrt – die Angabe wird überschrieben.</gray>",
                    Msg.text("n", target.name));
            service.unbanQuiet(old);
        }

        service.ban(target, reason, expiresAt, BanService.actorName(sender));
        if (temp) {
            Msg.send(sender, "<gray><white><n></white> wurde für <white><d></white> gesperrt: "
                            + "<white><r></white></gray>",
                    Msg.text("n", target.name),
                    Msg.text("d", BanService.remaining(expiresAt - System.currentTimeMillis())),
                    Msg.text("r", reason));
        } else {
            Msg.send(sender, "<gray><white><n></white> wurde dauerhaft gesperrt: <white><r></white></gray>",
                    Msg.text("n", target.name), Msg.text("r", reason));
        }
        return true;
    }

    private boolean unban(CommandSender sender, String[] args) {
        if (args.length < 1) {
            Msg.send(sender, "<gray>Verwendung: <white>/unban <Spieler></white></gray>");
            return true;
        }
        BanEntry<PlayerProfile> entry = service.findBan(args[0]);
        if (entry == null) {
            Msg.error(sender, "<white><n></white> ist nicht gesperrt.", Msg.text("n", args[0]));
            return true;
        }
        PlayerProfile profile = entry.getBanTarget();
        String name = profile == null || profile.getName() == null ? args[0] : profile.getName();
        service.unban(entry, BanService.actorName(sender));
        Msg.send(sender, "<gray>Die Sperre von <white><n></white> wurde aufgehoben.</gray>",
                Msg.text("n", name));
        return true;
    }

    // ---------------------------------------------------------------- Liste

    private boolean list(CommandSender sender, String[] args) {
        List<BanEntry<PlayerProfile>> bans = service.allBans();
        List<BanEntry<InetAddress>> ips = service.allIpBans();
        int total = bans.size() + ips.size();
        if (total == 0) {
            Msg.send(sender, "<gray>Zurzeit ist niemand gesperrt.</gray>");
            return true;
        }
        int perPage = Math.max(1, service.cfg().getInt("ban.list_per_page", 8));
        int pages = (total + perPage - 1) / perPage;
        int page = 1;
        if (args.length >= 1) {
            try {
                page = Integer.parseInt(args[0].trim());
            } catch (NumberFormatException ex) {
                Msg.error(sender, "<white><s></white> ist keine Seitenzahl.", Msg.text("s", args[0]));
                return true;
            }
        }
        page = Math.min(Math.max(1, page), pages);

        Msg.send(sender, "<gray>Sperren <dark_gray>(</dark_gray><white><n></white> "
                        + "<gray>Konten</gray><dark_gray>,</dark_gray> <white><i></white> <gray>IP</gray>"
                        + "<dark_gray>)</dark_gray> <dark_gray>–</dark_gray> Seite <white><p></white>"
                        + "<dark_gray>/</dark_gray><white><q></white></gray>",
                Msg.number("n", bans.size()), Msg.number("i", ips.size()),
                Msg.number("p", page), Msg.number("q", pages));

        int from = (page - 1) * perPage;
        int to = Math.min(total, from + perPage);
        for (int i = from; i < to; i++) {
            sender.sendMessage(i < bans.size() ? row(bans.get(i)) : ipRow(ips.get(i - bans.size())));
        }
        if (page < pages) {
            Msg.send(sender, "<gray>Weiter mit <white>/banlist <p></white>.</gray>", Msg.number("p", page + 1));
        }
        return true;
    }

    private static Component row(BanEntry<PlayerProfile> entry) {
        PlayerProfile profile = entry.getBanTarget();
        String name = profile == null || profile.getName() == null ? "?" : profile.getName();
        Component target = Component.text(name, NamedTextColor.WHITE)
                .clickEvent(ClickEvent.suggestCommand("/unban " + name))
                .hoverEvent(Component.text("Gesperrt am "
                        + BanService.stamp(BanService.millis(entry.getCreated()))
                        + "\nAnklicken, um /unban vorzubereiten", NamedTextColor.GRAY));
        return Msg.mm("<dark_gray>•</dark_gray> <t> <dark_gray>|</dark_gray> <gray><reason></gray> "
                        + "<dark_gray>|</dark_gray> <gray>von <white><source></white>, "
                        + "<white><expiry></white></gray>",
                Placeholder.component("t", target),
                Msg.text("reason", BanService.reasonOf(entry)),
                Msg.text("source", BanService.sourceOf(entry)),
                Msg.text("expiry", BanService.expiryText(BanService.millis(entry.getExpiration()))));
    }

    private static Component ipRow(BanEntry<InetAddress> entry) {
        InetAddress addr = entry.getBanTarget();
        return Msg.mm("<dark_gray>•</dark_gray> <dark_gray>[</dark_gray><yellow>IP</yellow><dark_gray>]</dark_gray> "
                        + "<white><a></white> <dark_gray>|</dark_gray> <gray><reason></gray> "
                        + "<dark_gray>|</dark_gray> <gray>von <white><source></white>, "
                        + "<white><expiry></white></gray>",
                Msg.text("a", addr == null ? "?" : addr.getHostAddress()),
                Msg.text("reason", BanService.cleanIpReason(BanService.reasonOf(entry))),
                Msg.text("source", BanService.sourceOf(entry)),
                Msg.text("expiry", BanService.expiryText(BanService.millis(entry.getExpiration()))));
    }

    // ---------------------------------------------------------------- Kick

    private boolean kick(CommandSender sender, String[] args) {
        if (args.length < 1) {
            Msg.send(sender, "<gray>Verwendung: <white>/kick <Spieler> [Grund]</white></gray>");
            return true;
        }
        // Bewusst genau passend (wie /ban über BanService.resolve): ein Teilstück träfe sonst den
        // Erstbesten – bei „Anna“ und „AnnaBelle“ fiele die Wahl dem Zufall zu.
        Player target = plugin.findVisibleExact(sender, args[0]);
        if (target == null) {
            Msg.error(sender, "<white><n></white> ist nicht online.", Msg.text("n", args[0]));
            return true;
        }
        if (isSelf(sender, target.getUniqueId())) {
            Msg.error(sender, "Du kannst dich nicht selbst hinauswerfen.");
            return true;
        }
        if (service.isProtected(target.getUniqueId(), target.getName())) {
            Msg.error(sender, "<white><n></white> kann nicht hinausgeworfen werden.",
                    Msg.text("n", target.getName()));
            return true;
        }
        String reason = BanService.joinReason(args, 1);
        if (reason.isBlank()) {
            reason = service.defaultReason();
        }
        String name = target.getName();
        service.kick(target, reason, BanService.actorName(sender));
        Msg.send(sender, "<gray><white><n></white> wurde hinausgeworfen: <white><r></white></gray>",
                Msg.text("n", name), Msg.text("r", reason));
        return true;
    }

    private static boolean isSelf(CommandSender sender, UUID id) {
        return sender instanceof Player p && id != null && p.getUniqueId().equals(id);
    }

    // ---------------------------------------------------------------- Tab-Vervollständigung

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        String name = command.getName().toLowerCase(Locale.ROOT);
        if (args.length == 1) {
            String prefix = args[0].toLowerCase(Locale.ROOT);
            if (name.equals("unban")) {
                for (BanEntry<PlayerProfile> e : service.allBans()) {
                    PlayerProfile p = e.getBanTarget();
                    String n = p == null ? null : p.getName();
                    if (n != null && n.toLowerCase(Locale.ROOT).startsWith(prefix)) {
                        out.add(n);
                    }
                }
                return out;
            }
            if (name.equals("banlist")) {
                return out;
            }
            for (Player p : plugin.visiblePlayers(sender)) {
                if (p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                    out.add(p.getName());
                }
            }
            return out;
        }
        if (args.length == 2 && name.equals("tempban")) {
            return BanService.durationSuggestions(args[1]);
        }
        return out;
    }
}
