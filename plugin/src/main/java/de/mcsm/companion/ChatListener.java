package de.mcsm.companion;

import io.papermc.paper.chat.ChatRenderer;
import io.papermc.paper.event.player.AsyncChatEvent;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import net.kyori.adventure.text.serializer.legacy.LegacyComponentSerializer;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;

/** Chatformat über MiniMessage; Farbcodes mit & nur mit Recht mcsm.chat.color. */
public final class ChatListener implements Listener {

    private static final LegacyComponentSerializer LEGACY = LegacyComponentSerializer.legacyAmpersand();
    private static final PlainTextComponentSerializer PLAIN = PlainTextComponentSerializer.plainText();

    private final CompanionPlugin plugin;

    public ChatListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.NORMAL, ignoreCancelled = true)
    public void onChat(AsyncChatEvent event) {
        Config cfg = plugin.settings();
        if (!cfg.featChat) {
            return;
        }
        final String format = cfg.chatFormat;
        event.renderer(ChatRenderer.viewerUnaware((source, displayName, message) -> {
            Component body;
            if (source.hasPermission("mcsm.chat.color")) {
                body = LEGACY.deserialize(PLAIN.serialize(message));
            } else {
                body = Component.text(PLAIN.serialize(message));
            }
            return Msg.mm(format,
                    Placeholder.component("name", displayName),
                    Placeholder.component("message", body));
        }));
    }
}
