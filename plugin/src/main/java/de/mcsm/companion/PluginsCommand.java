package de.mcsm.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.event.HoverEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.format.TextDecoration;
import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.plugin.Plugin;

/**
 * Die schöne Plugin-Liste des Begleit-Plugins.
 *
 * <p>Sie ersetzt die Bukkit-Befehle /pl, /plugins, /version und /icanhasbukkit: Wer
 * {@link #PERMISSION} hat, bekommt diese Ausgabe, alle anderen sehen den Befehl gar nicht erst
 * ({@link CommandGuardListener}). Eigenständig erreichbar ist sie über /mcsmpl.</p>
 */
public final class PluginsCommand implements TabExecutor {

    /** Wer das darf, sieht Plugin-Liste und Fassungen – sonst gibt es den Befehl hier nicht. */
    public static final String PERMISSION = "mcsm.plugins";

    private final CompanionPlugin plugin;

    public PluginsCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length > 0 && istFassung(args[0])) {
            fassungen(sender);
            return true;
        }
        liste(sender);
        return true;
    }

    private static boolean istFassung(String arg) {
        String a = arg.toLowerCase(Locale.ROOT);
        return a.equals("fassung") || a.equals("fassungen") || a.equals("version") || a.equals("ver");
    }

    /** Die Liste aller Plugins – aktive zuerst, das eigene ganz oben. */
    public void liste(CommandSender to) {
        List<Plugin> alle = new ArrayList<>(List.of(Bukkit.getPluginManager().getPlugins()));
        alle.sort(Comparator.comparing((Plugin p) -> !eigenes(p))
                .thenComparing(p -> !p.isEnabled())
                .thenComparing(p -> name(p).toLowerCase(Locale.ROOT)));

        List<String> verdaechtig = plugin.status() == null
                ? Collections.emptyList()
                : plugin.status().suspiciousPlugins();

        List<Plugin> aktiv = new ArrayList<>();
        List<Plugin> aus = new ArrayList<>();
        for (Plugin p : alle) {
            (p.isEnabled() ? aktiv : aus).add(p);
        }

        Msg.send(to, "<gray>Plugins auf</gray> <white><server></white> <dark_gray>·</dark_gray> "
                        + "<green><aktiv></green><dark_gray>/</dark_gray><gray><gesamt></gray>"
                        + " <dark_gray>aktiv</dark_gray>",
                Msg.text("server", plugin.settings().serverName),
                Msg.number("aktiv", aktiv.size()),
                Msg.number("gesamt", alle.size()));

        if (!aktiv.isEmpty()) {
            Msg.line(to, "<green>❖</green> <gray>Aktiv</gray>");
            for (Plugin p : aktiv) {
                to.sendMessage(eintrag(p, verdaechtig.contains(name(p))));
            }
        }
        if (!aus.isEmpty()) {
            Msg.line(to, "<red>❖</red> <gray>Abgeschaltet</gray>");
            for (Plugin p : aus) {
                to.sendMessage(eintrag(p, verdaechtig.contains(name(p))));
            }
        }
        Msg.line(to, "<dark_gray>Zeig mit der Maus auf einen Namen für Fassung, Autor und Zweck.</dark_gray>");
    }

    /** Kurze Fassungsübersicht – der Ersatz für /version und /icanhasbukkit. */
    public void fassungen(CommandSender to) {
        Msg.send(to, "<gray>Fassungen von</gray> <white><server></white>",
                Msg.text("server", plugin.settings().serverName));
        Msg.line(to, "<green>❖</green> <gray>Server</gray>");
        Msg.line(to, "  <gray>Minecraft</gray> <white><mc></white>"
                        + " <dark_gray>·</dark_gray> <gray>Plugins</gray> <white><anzahl></white>"
                        + " <dark_gray>·</dark_gray> <gray>Java</gray> <white><java></white>",
                Msg.text("mc", Bukkit.getMinecraftVersion()),
                Msg.number("anzahl", Bukkit.getPluginManager().getPlugins().length),
                Msg.text("java", System.getProperty("java.version", "unbekannt")));
        Msg.line(to, "<green>❖</green> <gray>MCSM</gray>");
        Msg.line(to, "  <gray>Begleit-Plugin</gray> <white><plugin></white>"
                        + " <dark_gray>·</dark_gray> <gray>Manager</gray> <white><manager></white>"
                        + " <dark_gray>·</dark_gray> <gray>Betrieb</gray> <white><betrieb></white>",
                Msg.text("plugin", plugin.getPluginMeta().getVersion()),
                Msg.text("manager", plugin.settings().managerVersion),
                Msg.text("betrieb", "hosted".equals(plugin.settings().mode) ? "Root-Server" : "eigener PC"));
        Msg.line(to, "<dark_gray>Mehr Zahlen zeigt </dark_gray><gray>/status</gray><dark_gray>.</dark_gray>");
    }

    /**
     * Eine Zeile der Liste. Name und Fassung stehen im Chat, alles Weitere hängt am Mauszeiger –
     * so bleibt die Liste auch bei vielen Plugins ruhig. Die Texte kommen aus fremden Plugins und
     * werden deshalb als reiner Text gebaut, nicht über MiniMessage.
     */
    private Component eintrag(Plugin p, boolean verdaechtig) {
        String name = name(p);
        String fassung = p.getPluginMeta().getVersion();
        NamedTextColor farbe = p.isEnabled() ? NamedTextColor.GREEN : NamedTextColor.RED;

        Component zeile = Component.text("   ")
                .append(Component.text("●", farbe))
                .append(Component.text(" " + name, eigenes(p) ? NamedTextColor.GREEN : NamedTextColor.WHITE))
                .append(Component.text(" " + fassung, NamedTextColor.DARK_GRAY));
        if (verdaechtig) {
            zeile = zeile.append(Component.text(" ⚠", NamedTextColor.GOLD));
        }

        Component hinweis = Component.text(name, farbe, TextDecoration.BOLD)
                .append(Component.text(" " + fassung, NamedTextColor.GRAY));
        String zweck = p.getPluginMeta().getDescription();
        if (zweck != null && !zweck.isBlank()) {
            hinweis = hinweis.appendNewline().append(Component.text(kurz(zweck), NamedTextColor.GRAY));
        }
        List<String> autoren = p.getPluginMeta().getAuthors();
        if (autoren != null && !autoren.isEmpty()) {
            hinweis = hinweis.appendNewline()
                    .append(Component.text("von " + String.join(", ", autoren), NamedTextColor.DARK_GRAY));
        }
        hinweis = hinweis.appendNewline()
                .append(Component.text(p.isEnabled() ? "läuft" : "abgeschaltet", farbe));
        if (verdaechtig) {
            hinweis = hinweis.appendNewline().append(Component.text(
                    "⚠ kann Spielerzahl, Ping oder MOTD verändern", NamedTextColor.GOLD));
        }
        zeile = zeile.hoverEvent(HoverEvent.showText(hinweis));

        String seite = p.getPluginMeta().getWebsite();
        if (eigenes(p)) {
            zeile = zeile.clickEvent(ClickEvent.suggestCommand("/mcsm"));
        } else if (seite != null && (seite.startsWith("https://") || seite.startsWith("http://"))) {
            zeile = zeile.clickEvent(ClickEvent.openUrl(seite));
        }
        return zeile;
    }

    private static String name(Plugin p) {
        return p.getPluginMeta().getName();
    }

    private boolean eigenes(Plugin p) {
        return p.getName().equals(plugin.getName());
    }

    /** Beschreibungen fremder Plugins können lang sein – im Mauszeiger reicht der Anfang. */
    private static String kurz(String text) {
        String t = String.join(" ", text.trim().split("\\s+"));
        return t.length() <= 90 ? t : t.substring(0, 87) + "...";
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        if (args.length == 1 && "fassungen".startsWith(args[0].toLowerCase(Locale.ROOT))) {
            return List.of("fassungen");
        }
        return Collections.emptyList();
    }
}
