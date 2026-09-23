package de.mcsm.companion;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;

import io.papermc.paper.chat.ChatRenderer;
import io.papermc.paper.event.player.AsyncChatEvent;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.serializer.legacy.LegacyComponentSerializer;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import org.bukkit.Bukkit;
import org.bukkit.Sound;
import org.bukkit.SoundCategory;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.inventory.ItemStack;

/**
 * Zusätze für den Chat, ohne den vorhandenen ChatListener anzufassen: Erwähnungen mit Ton,
 * Anzeige des gehaltenen Gegenstands über [i] bzw. [item] und ein einfacher Spamschutz.
 *
 * <p>Der Chat läuft asynchron. Damit von dort keine Bukkit-Aufrufe nötig sind, hält diese Klasse
 * einen im Servertakt aufgefrischten Abzug der Spielerliste und der gehaltenen Gegenstände.</p>
 */
public final class ChatExtrasListener implements Listener, Runnable {

    /** Unsichtbarer Platzhalter, der den Nachrichtentext im fremden Format vertritt. */
    private static final String MARKER = "⁣MCSMCHAT⁣";
    private static final Component MARKER_COMPONENT = Component.text(MARKER);
    private static final LegacyComponentSerializer LEGACY = LegacyComponentSerializer.legacyAmpersand();
    private static final PlainTextComponentSerializer PLAIN = PlainTextComponentSerializer.plainText();
    private static final String DEFAULT_MENTION = "<gold><bold>@<name></bold></gold>";
    private static final Component EMPTY_HAND =
            Component.text("[leere Hand]", NamedTextColor.DARK_GRAY);

    private final CompanionPlugin plugin;
    /** Abzug der Spielerliste; wird im Servertakt ersetzt und async nur gelesen. */
    private volatile List<Known> known = List.of();
    private final Map<UUID, Known> byId = new ConcurrentHashMap<>();
    /** Fertig aufgebaute Anzeige des gehaltenen Gegenstands je Spieler. */
    private final Map<UUID, Component> held = new ConcurrentHashMap<>();
    private final Map<UUID, Spam> spam = new ConcurrentHashMap<>();

    public ChatExtrasListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    /** Ein Spieler, wie ihn der Chat-Thread sehen darf. */
    private record Known(UUID id, String name, Pattern pattern, boolean vanished,
                         boolean seesVanished, boolean exempt, boolean color) {
    }

    /** Spamzähler je Spieler. */
    private static final class Spam {
        private final ArrayDeque<Long> times = new ArrayDeque<>();
        private String last = "";
        private long lastAt;
    }

    private YamlConfiguration raw() {
        return plugin.settings().raw();
    }

    public boolean enabled() {
        return raw().getBoolean("features.chat_extras", true);
    }

    // ---------------------------------------------------------------- Abzug im Servertakt

    @Override
    public void run() {
        List<Known> list = new ArrayList<>();
        Set<UUID> alive = new HashSet<>();
        for (Player p : Bukkit.getOnlinePlayers()) {
            UUID id = p.getUniqueId();
            alive.add(id);
            Known k = new Known(id, p.getName(), mentionPattern(p.getName()),
                    plugin.vanish().isVanished(p), plugin.canSeeVanished(p), exempt(p),
                    p.hasPermission("mcsm.chat.color"));
            list.add(k);
            byId.put(id, k);
            ItemStack item = p.getInventory().getItemInMainHand();
            if (item.isEmpty()) {
                held.remove(id);
            } else {
                held.put(id, itemDisplay(item));
            }
        }
        known = List.copyOf(list);
        byId.keySet().retainAll(alive);
        held.keySet().retainAll(alive);
        spam.keySet().retainAll(alive);
    }

    private boolean exempt(Player p) {
        return p.isOp() || plugin.settings().isAdmin(p.getName()) || p.hasPermission("mcsm.chat.nospam");
    }

    private static Pattern mentionPattern(String name) {
        return Pattern.compile("@?\\b" + Pattern.quote(name) + "\\b", Pattern.CASE_INSENSITIVE);
    }

    /** Gegenstandsname in Klammern, mit dem echten Gegenstand als Hover. */
    private static Component itemDisplay(ItemStack item) {
        Component base = item.displayName();
        if (item.getAmount() > 1) {
            return Component.empty().append(base)
                    .append(Component.text(" ×" + item.getAmount(), NamedTextColor.GRAY));
        }
        return base;
    }

    // ---------------------------------------------------------------- Spamschutz

    @EventHandler(priority = EventPriority.LOWEST, ignoreCancelled = true)
    public void onChatSpam(AsyncChatEvent event) {
        if (!enabled()) {
            return;
        }
        Player p = event.getPlayer();
        Known k = byId.get(p.getUniqueId());
        if (k != null && k.exempt()) {
            return;
        }
        int maxMessages = Math.max(1, raw().getInt("chat_extras.spam_messages", 3));
        long window = Math.max(1L, raw().getInt("chat_extras.spam_seconds", 5)) * 1000L;
        long repeatWindow = Math.max(0L, raw().getInt("chat_extras.repeat_seconds", 30)) * 1000L;
        String text = PLAIN.serialize(event.message()).trim();

        Spam s = spam.computeIfAbsent(p.getUniqueId(), id -> new Spam());
        long now = System.currentTimeMillis();
        String problem = null;
        synchronized (s) {
            while (!s.times.isEmpty() && now - s.times.peekFirst() > window) {
                s.times.pollFirst();
            }
            if (repeatWindow > 0L && !text.isEmpty() && text.equalsIgnoreCase(s.last)
                    && now - s.lastAt <= repeatWindow) {
                problem = "Bitte wiederhole dich nicht – schreib etwas Neues.";
            } else if (s.times.size() >= maxMessages) {
                problem = "Nicht so schnell – warte einen Moment, bevor du weiterschreibst.";
            } else {
                s.times.addLast(now);
                s.last = text;
                s.lastAt = now;
            }
        }
        if (problem != null) {
            event.setCancelled(true);
            Msg.error(p, problem);
        }
    }

    // ---------------------------------------------------------------- Erwähnungen und Gegenstände

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onChatDecorate(AsyncChatEvent event) {
        if (!enabled()) {
            return;
        }
        Player source = event.getPlayer();
        Known self = byId.get(source.getUniqueId());
        String plain = PLAIN.serialize(event.message());
        boolean mentions = raw().getBoolean("chat_extras.mentions", true);
        boolean items = raw().getBoolean("chat_extras.item_display", true);
        String mentionFormat = raw().getString("chat_extras.mention_format", DEFAULT_MENTION);
        if (mentionFormat == null || mentionFormat.isEmpty()) {
            mentionFormat = DEFAULT_MENTION;
        }

        Component body = self != null && self.color()
                ? LEGACY.deserialize(plain) : Component.text(plain);
        if (items) {
            Component display = held.getOrDefault(source.getUniqueId(), EMPTY_HAND);
            body = body.replaceText(b -> b.matchLiteral("[item]").replacement(display));
            body = body.replaceText(b -> b.matchLiteral("[i]").replacement(display));
        }
        List<Known> pinged = new ArrayList<>();
        if (mentions) {
            for (Known k : known) {
                if (k.vanished() && !(self != null && self.seesVanished())) {
                    continue;
                }
                if (!k.pattern().matcher(plain).find()) {
                    continue;
                }
                Component replacement = Msg.mm(mentionFormat, Msg.text("name", k.name()));
                body = body.replaceText(b -> b.match(k.pattern()).replacement(replacement));
                if (!k.id().equals(source.getUniqueId())) {
                    pinged.add(k);
                }
            }
        }

        final Component decorated = body;
        final ChatRenderer inner = event.renderer();
        event.renderer((src, displayName, message, viewer) ->
                inner.render(src, displayName, MARKER_COMPONENT, viewer)
                        .replaceText(b -> b.matchLiteral(MARKER).replacement(decorated)));

        if (!pinged.isEmpty()) {
            List<UUID> ids = new ArrayList<>(pinged.size());
            for (Known k : pinged) {
                ids.add(k.id());
            }
            Bukkit.getScheduler().runTask(plugin, () -> ping(ids));
        }
    }

    /** Spielt den Erwähnten einen Ton vor (läuft im Servertakt). */
    private void ping(List<UUID> ids) {
        if (!raw().getBoolean("chat_extras.mention_sound", true)) {
            return;
        }
        for (UUID id : ids) {
            Player p = Bukkit.getPlayer(id);
            if (p != null && p.isOnline()) {
                p.playSound(p.getLocation(), Sound.BLOCK_NOTE_BLOCK_PLING, SoundCategory.MASTER, 0.8f, 1.6f);
            }
        }
    }
}
