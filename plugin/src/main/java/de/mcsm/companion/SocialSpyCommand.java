package de.mcsm.companion;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;

import org.bukkit.Bukkit;
import org.bukkit.NamespacedKey;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.persistence.PersistentDataType;

/**
 * /socialspy – zeigt Berechtigten alle privaten Nachrichten samt Richtung. Der Zustand liegt im
 * Speicher und zusätzlich in den Spielerdaten, damit er einen Neustart übersteht.
 */
public final class SocialSpyCommand implements TabExecutor, Listener {

    private static final String DEFAULT_SPY =
            "<dark_gray>[</dark_gray><gray>Spy</gray><dark_gray>]</dark_gray> "
            + "<gray><from></gray> <dark_gray>→</dark_gray> <gray><to></gray><dark_gray>:</dark_gray> <gray><message></gray>";
    private static final List<String> SUB = List.of("an", "aus", "status");

    private final CompanionPlugin plugin;
    private final Set<UUID> spies = new HashSet<>();
    private final NamespacedKey key;

    public SocialSpyCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.key = new NamespacedKey(plugin, "socialspy");
    }

    /** Darf der Absender mitlesen? Wartungszugang, OPs und Inhaber von mcsm.socialspy. */
    public boolean allowed(CommandSender sender) {
        if (!(sender instanceof Player)) {
            return true;
        }
        return plugin.settings().isAdmin(sender.getName()) || sender.isOp()
                || sender.hasPermission("mcsm.socialspy");
    }

    public boolean isSpy(Player p) {
        return spies.contains(p.getUniqueId());
    }

    /** Zustand aus den Spielerdaten übernehmen, sobald der Spieler wieder da ist. */
    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        Player p = event.getPlayer();
        Byte stored = p.getPersistentDataContainer().get(key, PersistentDataType.BYTE);
        if (stored != null && stored != 0 && allowed(p)) {
            spies.add(p.getUniqueId());
        } else {
            spies.remove(p.getUniqueId());
            if (stored != null && !allowed(p)) {
                p.getPersistentDataContainer().remove(key);
            }
        }
    }

    /** Beim Verlassen den Merker im Speicher löschen; der Zustand steht in den Spielerdaten. */
    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        spies.remove(event.getPlayer().getUniqueId());
    }

    /** Leitet eine private Nachricht an alle Mitleser weiter (außer an die Beteiligten selbst). */
    public void report(CommandSender from, CommandSender to, String message) {
        if (spies.isEmpty()) {
            return;
        }
        String format = plugin.settings().raw().getString("msg.spy_format", DEFAULT_SPY);
        if (format == null || format.isEmpty()) {
            format = DEFAULT_SPY;
        }
        for (Player spy : Bukkit.getOnlinePlayers()) {
            if (!spies.contains(spy.getUniqueId()) || spy.equals(from) || spy.equals(to)) {
                continue;
            }
            spy.sendMessage(Msg.mm(format,
                    Msg.text("from", from.getName()),
                    Msg.text("to", to.getName()),
                    Msg.text("message", message)));
        }
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!allowed(sender)) {
            Msg.error(sender, "Dazu hast du keine Berechtigung.");
            return true;
        }
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        boolean on;
        if (args.length == 0) {
            on = !isSpy(player);
        } else {
            switch (args[0].toLowerCase(Locale.ROOT)) {
                case "an": case "on": case "ein": case "true":
                    on = true;
                    break;
                case "aus": case "off": case "false":
                    on = false;
                    break;
                case "status":
                    Msg.send(player, isSpy(player)
                            ? "<gray>Mitlesen ist <green>eingeschaltet</green>.</gray>"
                            : "<gray>Mitlesen ist <red>ausgeschaltet</red>.</gray>");
                    return true;
                default:
                    Msg.send(player, "<gray>Verwendung: <white>/socialspy [an|aus|status]</white></gray>");
                    return true;
            }
        }
        set(player, on);
        Msg.send(player, on
                ? "<green>Mitlesen eingeschaltet.</green> <gray>Du siehst jetzt alle privaten Nachrichten.</gray>"
                : "<gray>Mitlesen ausgeschaltet.</gray>");
        return true;
    }

    private void set(Player p, boolean on) {
        if (on) {
            spies.add(p.getUniqueId());
            p.getPersistentDataContainer().set(key, PersistentDataType.BYTE, (byte) 1);
        } else {
            spies.remove(p.getUniqueId());
            p.getPersistentDataContainer().remove(key);
        }
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length == 1 && allowed(sender)) {
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
