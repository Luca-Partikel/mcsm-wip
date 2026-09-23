package de.mcsm.companion;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /tpa, /tpaccept, /tpdeny – Anfragen verfallen nach 60 s, Abklingzeit 10 s. */
public final class TpaCommand implements TabExecutor {

    private static final long EXPIRE_MS = 60_000L;
    private static final long COOLDOWN_MS = 10_000L;

    private final CompanionPlugin plugin;
    /** Ziel -> (Anfragender -> Ablaufzeitpunkt), in Reihenfolge des Eingangs. */
    private final Map<UUID, LinkedHashMap<UUID, Long>> requests = new HashMap<>();
    private final Map<UUID, Long> lastRequest = new HashMap<>();

    public TpaCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    /** Beim Verlassen alle Anfragen von und an diesen Spieler vergessen. */
    public void clear(Player p) {
        UUID id = p.getUniqueId();
        requests.remove(id);
        lastRequest.remove(id);
        for (LinkedHashMap<UUID, Long> m : requests.values()) {
            m.remove(id);
        }
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!plugin.settings().featTpa) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "tpa": return tpa(player, args);
            case "tpaccept": return answer(player, args, true);
            case "tpdeny": return answer(player, args, false);
            default: return false;
        }
    }

    private boolean tpa(Player from, String[] args) {
        if (args.length != 1) {
            Msg.send(from, "<gray>Verwendung: <white>/tpa <Spieler></white></gray>");
            return true;
        }
        Player to = plugin.findVisible(from, args[0]);
        if (to == null) {
            Msg.error(from, "Spieler <white><target></white> wurde nicht gefunden.", Msg.text("target", args[0]));
            return true;
        }
        if (to.equals(from)) {
            Msg.error(from, "Du kannst dir keine Anfrage an dich selbst schicken.");
            return true;
        }
        long now = System.currentTimeMillis();
        long last = lastRequest.getOrDefault(from.getUniqueId(), 0L);
        if (now - last < COOLDOWN_MS) {
            long rest = (COOLDOWN_MS - (now - last) + 999) / 1000;
            Msg.error(from, "Bitte warte noch <white><s></white> Sekunden.", Msg.number("s", rest));
            return true;
        }
        lastRequest.put(from.getUniqueId(), now);
        requests.computeIfAbsent(to.getUniqueId(), k -> new LinkedHashMap<>()).put(from.getUniqueId(), now + EXPIRE_MS);

        Msg.send(from, "<gray>Teleport-Anfrage an <white><target></white> gesendet. Sie läuft in 60 Sekunden ab.</gray>",
                Msg.name("target", to));
        // Schaltflächen als fertige Komponenten: Platzhalter werden innerhalb von Tag-Argumenten
        // (click:run_command:'...') von MiniMessage nicht ersetzt.
        String n = from.getName();
        Component accept = Component.text("[Annehmen]", NamedTextColor.GREEN)
                .clickEvent(ClickEvent.runCommand("/tpaccept " + n))
                .hoverEvent(Component.text("Anfrage annehmen", NamedTextColor.GREEN));
        Component deny = Component.text("[Ablehnen]", NamedTextColor.RED)
                .clickEvent(ClickEvent.runCommand("/tpdeny " + n))
                .hoverEvent(Component.text("Anfrage ablehnen", NamedTextColor.RED));
        Msg.send(to, "<white><from></white> <gray>möchte sich zu dir teleportieren.</gray> <accept> <deny>",
                Msg.name("from", from), Placeholder.component("accept", accept), Placeholder.component("deny", deny));
        return true;
    }

    private boolean answer(Player target, String[] args, boolean accept) {
        LinkedHashMap<UUID, Long> pending = requests.get(target.getUniqueId());
        purge(pending);
        if (pending == null || pending.isEmpty()) {
            Msg.error(target, "Du hast keine offene Teleport-Anfrage.");
            return true;
        }
        UUID requesterId = null;
        if (args.length >= 1) {
            for (UUID id : pending.keySet()) {
                Player p = Bukkit.getPlayer(id);
                if (p != null && p.getName().equalsIgnoreCase(args[0])) {
                    requesterId = id;
                    break;
                }
            }
            if (requesterId == null) {
                Msg.error(target, "Von <white><name></white> liegt keine Anfrage vor.", Msg.text("name", args[0]));
                return true;
            }
        } else {
            // die jüngste Anfrage
            for (UUID id : pending.keySet()) {
                requesterId = id;
            }
        }
        pending.remove(requesterId);
        Player requester = Bukkit.getPlayer(requesterId);
        if (requester == null || !requester.isOnline()) {
            Msg.error(target, "Der anfragende Spieler ist nicht mehr online.");
            return true;
        }
        if (!accept) {
            Msg.send(target, "<gray>Anfrage von <white><name></white> abgelehnt.</gray>", Msg.name("name", requester));
            Msg.send(requester, "<red><name> hat deine Teleport-Anfrage abgelehnt.</red>", Msg.name("name", target));
            return true;
        }
        Msg.send(target, "<gray>Anfrage von <white><name></white> angenommen.</gray>", Msg.name("name", requester));
        Msg.send(requester, "<green><name> hat deine Anfrage angenommen – du wirst teleportiert.</green>",
                Msg.name("name", target));
        // Aufwärmzeit wie überall, aber keine zusätzliche Abklingzeit – /tpa hat schon eine eigene.
        plugin.teleports().request(requester, target.getLocation(), "tpa",
                "<gray>Du bist angekommen.</gray>", -1, 0);
        return true;
    }

    private static void purge(LinkedHashMap<UUID, Long> pending) {
        if (pending == null) {
            return;
        }
        long now = System.currentTimeMillis();
        Iterator<Map.Entry<UUID, Long>> it = pending.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<UUID, Long> e = it.next();
            if (e.getValue() < now || Bukkit.getPlayer(e.getKey()) == null) {
                it.remove();
            }
        }
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length != 1 || !(sender instanceof Player player)) {
            return out;
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        if (command.getName().equalsIgnoreCase("tpa")) {
            for (Player p : plugin.visiblePlayers(player)) {
                if (!p.equals(player) && p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                    out.add(p.getName());
                }
            }
        } else {
            LinkedHashMap<UUID, Long> pending = requests.get(player.getUniqueId());
            purge(pending);
            if (pending != null) {
                for (UUID id : pending.keySet()) {
                    Player p = Bukkit.getPlayer(id);
                    if (p != null && p.getName().toLowerCase(Locale.ROOT).startsWith(prefix)) {
                        out.add(p.getName());
                    }
                }
            }
        }
        return out;
    }
}
