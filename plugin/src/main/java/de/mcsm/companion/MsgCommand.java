package de.mcsm.companion;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

import org.bukkit.Bukkit;
import org.bukkit.Sound;
import org.bukkit.SoundCategory;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerQuitEvent;

/**
 * /msg (auch /w, /tell, /pm) und /r – private Nachrichten mit eigenem Format. Das Antwortziel wird
 * je Absender gemerkt. Unsichtbare Betreiber sind nur für Berechtigte auffindbar.
 */
public final class MsgCommand implements TabExecutor, Listener {

    private static final String DEFAULT_OUT =
            "<dark_gray>[</dark_gray><gray>du</gray> <dark_gray>→</dark_gray> <white><to></white><dark_gray>]</dark_gray> "
            + "<gray><message></gray>";
    private static final String DEFAULT_IN =
            "<dark_gray>[</dark_gray><white><from></white> <dark_gray>→</dark_gray> <gray>dir</gray><dark_gray>]</dark_gray> "
            + "<gray><message></gray>";
    /** Ersatz-Kennung für die Konsole, die keine UUID besitzt. */
    private static final UUID CONSOLE = new UUID(0L, 0L);
    private static final List<String> CONSOLE_NAMES = List.of("konsole", "console", "server");

    private final CompanionPlugin plugin;
    private final SocialSpyCommand spy;
    private final AfkManager afk;
    /** Absender -> zuletzt beteiligter Gesprächspartner (für /r). */
    private final Map<UUID, UUID> partner = new HashMap<>();

    public MsgCommand(CompanionPlugin plugin, SocialSpyCommand spy, AfkManager afk) {
        this.plugin = plugin;
        this.spy = spy;
        this.afk = afk;
    }

    private boolean enabled() {
        return plugin.settings().raw().getBoolean("features.msg", true);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        UUID id = event.getPlayer().getUniqueId();
        partner.remove(id);
        partner.values().removeIf(id::equals);
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!enabled()) {
            Msg.error(sender, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        boolean reply = !command.getName().equalsIgnoreCase("msg");
        if (reply) {
            return reply(sender, args);
        }
        if (args.length < 2) {
            Msg.send(sender, "<gray>Verwendung: <white>/msg <Spieler> <Nachricht></white></gray>");
            return true;
        }
        CommandSender target = resolve(sender, args[0]);
        if (target == null) {
            Msg.error(sender, "Spieler <white><target></white> wurde nicht gefunden.", Msg.text("target", args[0]));
            return true;
        }
        return deliver(sender, target, join(args, 1));
    }

    private boolean reply(CommandSender sender, String[] args) {
        if (args.length < 1) {
            Msg.send(sender, "<gray>Verwendung: <white>/r <Nachricht></white></gray>");
            return true;
        }
        UUID id = partner.get(idOf(sender));
        CommandSender target = id == null ? null : lookup(id);
        if (target == null) {
            Msg.error(sender, "Es gibt niemanden, dem du antworten könntest.");
            return true;
        }
        if (target instanceof Player p && plugin.findVisible(sender, p.getName()) == null) {
            Msg.error(sender, "Es gibt niemanden, dem du antworten könntest.");
            return true;
        }
        return deliver(sender, target, join(args, 0));
    }

    /** Stellt die Nachricht zu, merkt das Antwortziel und meldet sie den Mitlesern. */
    private boolean deliver(CommandSender from, CommandSender to, String text) {
        if (text.isBlank()) {
            Msg.error(from, "Deine Nachricht ist leer.");
            return true;
        }
        if (from == to || (from instanceof Player a && to instanceof Player b && a.equals(b))) {
            Msg.error(from, "Du kannst dir nicht selbst schreiben.");
            return true;
        }
        String out = str("msg.format_out", DEFAULT_OUT);
        String in = str("msg.format_in", DEFAULT_IN);
        from.sendMessage(Msg.mm(out,
                Msg.text("from", from.getName()), Msg.text("to", to.getName()), Msg.text("message", text)));
        to.sendMessage(Msg.mm(in,
                Msg.text("from", from.getName()), Msg.text("to", to.getName()), Msg.text("message", text)));

        partner.put(idOf(from), idOf(to));
        partner.put(idOf(to), idOf(from));
        spy.report(from, to, text);

        if (to instanceof Player p) {
            if (plugin.settings().raw().getBoolean("msg.sound", true)) {
                p.playSound(p.getLocation(), Sound.ENTITY_EXPERIENCE_ORB_PICKUP, SoundCategory.MASTER, 0.6f, 1.7f);
            }
            if (afk.isAfk(p)) {
                String reason = afk.reason(p.getUniqueId());
                Msg.send(from, reason.isEmpty()
                                ? "<gray><name> ist seit <white><since></white> AFK.</gray>"
                                : "<gray><name> ist seit <white><since></white> AFK: <white><reason></white></gray>",
                        Msg.name("name", p),
                        Msg.text("since", PlayerStatsStore.duration(afk.afkMillis(p.getUniqueId()))),
                        Msg.text("reason", reason));
            }
        }
        return true;
    }

    /** Ziel nach Namen; "konsole"/"console"/"server" spricht die Serverkonsole an. */
    private CommandSender resolve(CommandSender viewer, String name) {
        if (CONSOLE_NAMES.contains(name.toLowerCase(Locale.ROOT))) {
            return Bukkit.getConsoleSender();
        }
        return plugin.findVisible(viewer, name);
    }

    private static CommandSender lookup(UUID id) {
        if (CONSOLE.equals(id)) {
            return Bukkit.getConsoleSender();
        }
        Player p = Bukkit.getPlayer(id);
        return p != null && p.isOnline() ? p : null;
    }

    private static UUID idOf(CommandSender sender) {
        return sender instanceof Player p ? p.getUniqueId() : CONSOLE;
    }

    private static String join(String[] args, int from) {
        StringBuilder sb = new StringBuilder();
        for (int i = from; i < args.length; i++) {
            if (sb.length() > 0) {
                sb.append(' ');
            }
            sb.append(args[i]);
        }
        return sb.toString().trim();
    }

    private String str(String key, String def) {
        String v = plugin.settings().raw().getString(key, def);
        return v == null || v.isEmpty() ? def : v;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length != 1 || !command.getName().equalsIgnoreCase("msg")) {
            return out;
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        for (Player p : plugin.visiblePlayers(sender)) {
            if (!p.equals(sender) && p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                out.add(p.getName());
            }
        }
        return out;
    }
}
