package de.mcsm.companion;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.MiniMessage;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;

/** MiniMessage-Helfer mit dem einheitlichen Präfix "✦ MCSM ¦". */
public final class Msg {

    public static final String PREFIX =
            "<gray>✦</gray> <green><bold>MCSM</bold></green> <dark_gray>¦</dark_gray> <white>";
    /** Erste Zeile der Serverlisten-Anzeige – gleiche Handschrift wie das Präfix. */
    public static final String MOTD_KOPF =
            "<gray>✦</gray> <green><bold>MCSM</bold></green> <dark_gray>¦</dark_gray> <white><server_name></white>";

    private static final MiniMessage MM = MiniMessage.miniMessage();

    private Msg() {
    }

    public static Component mm(String text, TagResolver... resolvers) {
        try {
            return MM.deserialize(text, resolvers);
        } catch (RuntimeException ex) {
            // Fehlerhaftes Format aus der Konfiguration soll den Server nicht stören.
            return Component.text(MM.stripTags(text, resolvers));
        }
    }

    public static Component prefixed(String text, TagResolver... resolvers) {
        return mm(PREFIX + text, resolvers);
    }

    public static void send(CommandSender to, String text, TagResolver... resolvers) {
        to.sendMessage(prefixed(text, resolvers));
    }

    public static void error(CommandSender to, String text, TagResolver... resolvers) {
        to.sendMessage(prefixed("<red>" + text + "</red>", resolvers));
    }

    /** Spielername als Platzhalter, ohne dass MiniMessage darin Tags interpretiert. */
    public static TagResolver name(String key, Player player) {
        return Placeholder.component(key, Component.text(player.getName()));
    }

    public static TagResolver text(String key, String value) {
        return Placeholder.unparsed(key, value == null ? "" : value);
    }

    public static TagResolver number(String key, long value) {
        return Placeholder.unparsed(key, Long.toString(value));
    }
}
