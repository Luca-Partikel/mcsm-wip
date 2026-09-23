package de.mcsm.companion;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.command.TabExecutor;

/**
 * /mcsm – Übersicht aller Befehle des Plugins, nach Themen sortiert und anklickbar. Gezeigt wird
 * nur, was es auf diesem Server wirklich gibt und wofür der Absender das Recht hat.
 */
public final class McsmCommand implements TabExecutor {

    /** Ein Eintrag der Übersicht: Befehlsname, angezeigte Schreibweise, kurze Erklärung. */
    private record Entry(String command, String usage, String description) {
    }

    /** Eine Kategorie mit ihren Einträgen. */
    private record Group(String title, List<Entry> entries) {
    }

    private static final List<Group> GROUPS = List.of(
            new Group("Teleport", List.of(
                    new Entry("tpa", "/tpa <Spieler>", "Teleport-Anfrage senden"),
                    new Entry("tpaccept", "/tpaccept", "Anfrage annehmen"),
                    new Entry("tpdeny", "/tpdeny", "Anfrage ablehnen"),
                    new Entry("tp", "/tp <Spieler>", "Direkt teleportieren"),
                    new Entry("spawn", "/spawn", "Zum Spawn"),
                    new Entry("back", "/back", "Zurück zum letzten Ort"),
                    new Entry("rtp", "/rtp", "Zufälliger Ort in der Wildnis"),
                    new Entry("warp", "/warp <Name>", "Zu einem Warp-Punkt"),
                    new Entry("warps", "/warps", "Alle Warp-Punkte"),
                    new Entry("setwarp", "/setwarp <Name>", "Warp hier setzen"),
                    new Entry("delwarp", "/delwarp <Name>", "Warp löschen"),
                    new Entry("top", "/top", "An die Oberfläche"))),
            new Group("Zuhause", List.of(
                    new Entry("sethome", "/sethome [Name]", "Home setzen"),
                    new Entry("home", "/home [Name]", "Zu einem Home"),
                    new Entry("delhome", "/delhome <Name>", "Home löschen"),
                    new Entry("homes", "/homes", "Eigene Homes anzeigen"))),
            new Group("Unterhaltung", List.of(
                    new Entry("msg", "/msg <Spieler> <Text>", "Private Nachricht"),
                    new Entry("r", "/r <Text>", "Auf die letzte Nachricht antworten"),
                    new Entry("afk", "/afk", "Als abwesend melden"))),
            new Group("Zahlen und Namen", List.of(
                    new Entry("playtime", "/playtime [Spieler]", "Gespielte Zeit"),
                    new Entry("seen", "/seen <Spieler>", "Zuletzt online"),
                    new Entry("stats", "/stats [Spieler]", "Spielstatistik"),
                    new Entry("near", "/near", "Spieler in der Nähe"))),
            new Group("Server", List.of(
                    new Entry("rules", "/rules", "Serverregeln"),
                    new Entry("sb", "/sb", "Seitenleiste an/aus"),
                    new Entry("mcsm", "/mcsm", "Diese Übersicht"))),
            new Group("Moderation", List.of(
                    new Entry("ban", "/ban <Spieler> [Grund]", "Dauerhaft sperren"),
                    new Entry("tempban", "/tempban <Spieler> <Dauer>", "Auf Zeit sperren"),
                    new Entry("unban", "/unban <Spieler>", "Sperre aufheben"),
                    new Entry("banlist", "/banlist", "Alle Sperren anzeigen"),
                    new Entry("kick", "/kick <Spieler> [Grund]", "Vom Server werfen"),
                    new Entry("mute", "/mute <Spieler> [Grund]", "Stummschalten"),
                    new Entry("tempmute", "/tempmute <Spieler> <Dauer>", "Auf Zeit stummschalten"),
                    new Entry("unmute", "/unmute <Spieler>", "Stummschaltung aufheben"),
                    new Entry("mutelist", "/mutelist", "Stummgeschaltete anzeigen"),
                    new Entry("warn", "/warn <Spieler> <Grund>", "Verwarnen"),
                    new Entry("warns", "/warns <Spieler>", "Verwarnungen anzeigen"))),
            new Group("Verwaltung", List.of(
                    new Entry("gm", "/gm <0-3> [Spieler]", "Spielmodus wechseln"),
                    new Entry("hardcore", "/hardcore <on|off|status>", "Hardcore-Modus schalten"),
                    new Entry("socialspy", "/socialspy [an|aus]", "Private Nachrichten mitlesen"),
                    new Entry("wl", "/wl <an|aus|add|remove|list>", "Freigabeliste verwalten"),
                    new Entry("mcsmstop", "/mcsmstop <Sekunden>", "Server mit Ansage herunterfahren"))));

    private final CompanionPlugin plugin;
    private final ServerMetrics metrics;

    public McsmCommand(CompanionPlugin plugin, ServerMetrics metrics) {
        this.plugin = plugin;
        this.metrics = metrics;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        Config cfg = plugin.settings();
        sender.sendMessage(Msg.mm("<dark_gray>――――――――――――――――――</dark_gray>"));
        sender.sendMessage(Msg.prefixed("<green><bold><server></bold></green> <dark_gray>|</dark_gray> "
                        + "<gray>Minecraft <white><mc></white>, <white><online></white>/<white><max></white> online, "
                        + "TPS </gray>" + metrics.tpsColored() + "<gray>, läuft seit <white><up></white></gray>",
                Msg.text("server", cfg.serverName),
                Msg.text("mc", Bukkit.getMinecraftVersion()),
                Msg.number("online", plugin.vanish().visibleOnline()),
                Msg.number("max", Bukkit.getMaxPlayers()),
                Msg.text("up", ServerMetrics.formatUptime(metrics.uptimeSeconds()))));

        int shown = 0;
        for (Group group : GROUPS) {
            List<Entry> allowed = new ArrayList<>();
            for (Entry e : group.entries()) {
                if (mayUse(sender, e.command())) {
                    allowed.add(e);
                }
            }
            if (allowed.isEmpty()) {
                continue;
            }
            sender.sendMessage(Msg.mm("<green>" + group.title() + "</green>"));
            for (Entry e : allowed) {
                shown++;
                sender.sendMessage(Msg.mm("  <dark_gray>▪</dark_gray> <click:suggest_command:'/" + e.command()
                                + "'><hover:show_text:'<gray>In die Eingabe übernehmen</gray>'>"
                                + "<white><cmd></white></hover></click> <dark_gray>–</dark_gray> <gray><desc></gray>",
                        Msg.text("cmd", e.usage()), Msg.text("desc", e.description())));
            }
        }
        if (shown == 0) {
            Msg.send(sender, "<gray>Für dich ist derzeit kein Befehl freigeschaltet.</gray>");
        }
        sender.sendMessage(Msg.mm("<dark_gray>――――――――――――――――――</dark_gray>"));
        return true;
    }

    /** Der Befehl muss auf diesem Server registriert und für den Absender erlaubt sein. */
    private static boolean mayUse(CommandSender sender, String name) {
        PluginCommand cmd = Bukkit.getPluginCommand(name);
        if (cmd == null) {
            return false;
        }
        String perm = cmd.getPermission();
        return perm == null || perm.isBlank() || sender.hasPermission(perm);
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return Collections.emptyList();
    }
}
