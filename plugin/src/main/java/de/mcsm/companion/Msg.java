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

    /**
     * Zeile ohne Präfix – für Auflistungen. Bei einer Statistik oder einer Liste gehört das
     * Präfix einmal an die Überschrift; vor jeder Zeile wiederholt macht es die Ausgabe unruhig.
     */
    public static void line(CommandSender to, String text, TagResolver... resolvers) {
        to.sendMessage(mm("  " + text, resolvers));
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

    /** Der Servername als fertiger Baustein in der im Programm gewählten Farbe bzw. dem Verlauf. */
    public static Component serverNameComponent(Config cfg) {
        String name = cfg.serverName == null ? "" : cfg.serverName;
        if (!cfg.nameGefaerbt()) {
            return Component.text(name);
        }
        String safe = MM.escapeTags(name);
        String snip = (cfg.nameColor2 != null && !cfg.nameColor2.isBlank())
                ? "<gradient:" + cfg.nameColor + ":" + cfg.nameColor2 + ">" + safe + "</gradient>"
                : "<color:" + cfg.nameColor + ">" + safe + "</color>";
        return mm(snip);
    }

    /**
     * Platzhalter für den Servernamen. Ist im Programm eine Farbe gesetzt, kommt der Name als
     * fertig gefärbter Baustein – seine Farbe schlägt die der Vorlage, damit er überall gleich
     * aussieht. Ohne eigene Farbe bleibt es schlichter Text, sodass die jeweilige Vorlage
     * (etwa der Tablist-Verlauf) weiter greift.
     */
    public static TagResolver serverName(String key, Config cfg) {
        return cfg.nameGefaerbt()
                ? Placeholder.component(key, serverNameComponent(cfg))
                : Placeholder.unparsed(key, cfg.serverName == null ? "" : cfg.serverName);
    }
}
