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
            "<gray>✦</gray> <green><bold>MCSM</bold></green> <dark_gray>¦</dark_gray> <server_name>";

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

    /** Standard-Verlauf des Servernamens (Markenfarbe), solange im Programm nichts anderes gesetzt ist. */
    public static final String DEFAULT_NAME_1 = "#3ddc84";
    public static final String DEFAULT_NAME_2 = "#8ff0b4";

    /**
     * Der Servername als fertiger, <b>immer gefärbter</b> Baustein: entweder in der im Programm
     * gewählten Farbe/dem Verlauf oder – solange nichts gesetzt ist – im Marken-Grünverlauf. Weil
     * der Name seine Farbe selbst mitbringt, sieht er überall gleich aus, ohne dass eine Vorlage
     * ihn einfärben müsste.
     */
    public static Component serverNameComponent(Config cfg) {
        String safe = MM.escapeTags(cfg.serverName == null ? "" : cfg.serverName);
        boolean eigen = cfg.nameGefaerbt();
        String c1 = eigen ? cfg.nameColor : DEFAULT_NAME_1;
        String c2 = eigen ? cfg.nameColor2 : DEFAULT_NAME_2;
        String snip = (c2 != null && !c2.isBlank())
                ? "<gradient:" + c1 + ":" + c2 + ">" + safe + "</gradient>"
                : "<color:" + c1 + ">" + safe + "</color>";
        return mm(snip);
    }

    /**
     * Platzhalter für den Servernamen – immer der fertig gefärbte Baustein. Damit die Farbe
     * wirklich greift, dürfen die Vorlagen den <code>&lt;server_name&gt;</code> nicht mehr in eine
     * eigene Farbe wickeln (siehe MOTD-Kopf und Tablist).
     */
    public static TagResolver serverName(String key, Config cfg) {
        return Placeholder.component(key, serverNameComponent(cfg));
    }
}
