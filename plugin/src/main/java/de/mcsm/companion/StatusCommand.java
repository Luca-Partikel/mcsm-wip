package de.mcsm.companion;

import java.io.File;
import java.lang.management.ManagementFactory;
import java.lang.management.OperatingSystemMXBean;
import java.lang.management.RuntimeMXBean;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;

/**
 * /tps und /status – wie es dem Server gerade geht.
 *
 * <p>/tps ist die Kurzfassung für alle: Ticks pro Sekunde und Rechenzeit je Tick.
 * /status ist die ausführliche Übersicht für Betreiber: dazu Arbeitsspeicher, Prozessorlast,
 * Plattenplatz, Laufzeit, geladene Bereiche und Wesen sowie die beteiligten Versionen.
 */
public final class StatusCommand implements TabExecutor {

    private final CompanionPlugin plugin;
    private final ServerMetrics metrics;

    public StatusCommand(CompanionPlugin plugin, ServerMetrics metrics) {
        this.plugin = plugin;
        this.metrics = metrics;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if ("tps".equalsIgnoreCase(command.getName())) {
            kurz(sender);
            return true;
        }
        lang(sender);
        return true;
    }

    /** /tps – die drei Mittelwerte und die Rechenzeit je Tick. */
    private void kurz(CommandSender to) {
        double[] tps = Bukkit.getTPS();
        StringBuilder sb = new StringBuilder();
        String[] namen = {"1 Min", "5 Min", "15 Min"};
        for (int i = 0; i < tps.length && i < 3; i++) {
            if (i > 0) {
                sb.append(" <dark_gray>·</dark_gray> ");
            }
            sb.append("<gray>").append(namen[i]).append("</gray> ").append(tpsFarbe(tps[i]));
        }
        Msg.send(to, sb.toString());
        Msg.line(to, "<gray>Rechenzeit je Tick</gray> " + msptFarbe(metrics.mspt())
                + " <dark_gray>(unter 50 ms ist gut)</dark_gray>");
    }

    /** /status – das ganze Bild. */
    private void lang(CommandSender to) {
        Runtime rt = Runtime.getRuntime();
        long maxMb = rt.maxMemory() / 1048576L;
        long belegtMb = (rt.totalMemory() - rt.freeMemory()) / 1048576L;
        long reserviertMb = rt.totalMemory() / 1048576L;

        Msg.send(to, "<gray>Zustand von</gray> <white><name></white>",
                Msg.text("name", plugin.settings().serverName));

        double[] tps = Bukkit.getTPS();
        Msg.line(to, "<green>❖</green> <gray>Leistung</gray>");
        Msg.line(to, "  <gray>TPS</gray> " + tpsFarbe(tps.length > 0 ? tps[0] : 20.0)
                + " <dark_gray>/</dark_gray> " + tpsFarbe(tps.length > 1 ? tps[1] : 20.0)
                + " <dark_gray>/</dark_gray> " + tpsFarbe(tps.length > 2 ? tps[2] : 20.0)
                + " <dark_gray>(1/5/15 Min)</dark_gray>");
        Msg.line(to, "  <gray>Rechenzeit je Tick</gray> " + msptFarbe(metrics.mspt()));
        String last = prozessorlast();
        if (!last.isEmpty()) {
            Msg.line(to, "  <gray>Prozessor</gray> " + last);
        }

        Msg.line(to, "<green>❖</green> <gray>Arbeitsspeicher</gray>");
        Msg.line(to, "  <gray>belegt</gray> " + speicherFarbe(belegtMb, maxMb)
                + " <gray>von</gray> <white>" + maxMb + " MB</white>"
                + " <dark_gray>(vom Betriebssystem geholt: " + reserviertMb + " MB)</dark_gray>");

        File ordner = plugin.getDataFolder().getAbsoluteFile();
        long freiMb = ordner.getUsableSpace() / 1048576L;
        long gesamtMb = ordner.getTotalSpace() / 1048576L;
        if (gesamtMb > 0) {
            Msg.line(to, "<green>❖</green> <gray>Platte</gray>");
            Msg.line(to, "  <gray>frei</gray> " + platteFarbe(freiMb, gesamtMb)
                    + " <gray>von</gray> <white>" + gb(gesamtMb) + "</white>");
        }

        Msg.line(to, "<green>❖</green> <gray>Server</gray>");
        Msg.line(to, "  <gray>Spieler</gray> <white>" + plugin.vanish().visibleOnline() + "</white>"
                + "<dark_gray>/</dark_gray><gray>" + Bukkit.getMaxPlayers() + "</gray>"
                + " <dark_gray>·</dark_gray> <gray>Welten</gray> <white>" + Bukkit.getWorlds().size() + "</white>");
        Msg.line(to, "  <gray>Geladene Bereiche</gray> <white>" + metrics.loadedChunks() + "</white>"
                + " <dark_gray>·</dark_gray> <gray>Wesen</gray> <white>" + metrics.entities() + "</white>");
        Msg.line(to, "  <gray>Läuft seit</gray> <white>" + ServerMetrics.formatUptime(metrics.uptimeSeconds())
                + "</white> <dark_gray>(Java: " + ServerMetrics.formatUptime(javaLaufzeit()) + ")</dark_gray>");

        Msg.line(to, "<green>❖</green> <gray>Fassungen</gray>");
        Msg.line(to, "  <gray>Minecraft</gray> <white>" + Bukkit.getMinecraftVersion() + "</white>"
                + " <dark_gray>·</dark_gray> <gray>Plugins</gray> <white>"
                + Bukkit.getPluginManager().getPlugins().length + "</white>");
        Msg.line(to, "  <gray>Begleit-Plugin</gray> <white>" + plugin.getPluginMeta().getVersion() + "</white>"
                + " <dark_gray>·</dark_gray> <gray>Manager</gray> <white>"
                + plugin.settings().managerVersion + "</white>"
                + " <dark_gray>·</dark_gray> <gray>Betrieb</gray> <white>"
                + ("hosted".equals(plugin.settings().mode) ? "Root-Server" : "eigener PC") + "</white>");
    }

    /** Prozessorlast, soweit die Laufzeitumgebung sie herausgibt. */
    private static String prozessorlast() {
        OperatingSystemMXBean os = ManagementFactory.getOperatingSystemMXBean();
        StringBuilder sb = new StringBuilder();
        // getProcessCpuLoad/getCpuLoad gibt es nur auf der Sun-Erweiterung – ohne harte Abhängigkeit
        // darauf zugreifen, damit das Plugin auch auf anderen Laufzeitumgebungen lädt.
        double prozess = versucheDouble(os, "getProcessCpuLoad");
        double system = versucheDouble(os, "getCpuLoad");
        if (prozess >= 0) {
            sb.append("<white>").append(prozent(prozess)).append("</white> <gray>dieser Server</gray>");
        }
        if (system >= 0) {
            if (sb.length() > 0) {
                sb.append(" <dark_gray>·</dark_gray> ");
            }
            sb.append("<white>").append(prozent(system)).append("</white> <gray>Maschine</gray>");
        }
        double schnitt = os.getSystemLoadAverage();
        int kerne = Math.max(1, os.getAvailableProcessors());
        if (schnitt >= 0) {
            if (sb.length() > 0) {
                sb.append(" <dark_gray>·</dark_gray> ");
            }
            sb.append("<gray>Last</gray> <white>").append(ServerMetrics.number(schnitt))
              .append("</white><dark_gray>/").append(kerne).append(" Kerne</dark_gray>");
        }
        return sb.toString();
    }

    private static double versucheDouble(Object ziel, String name) {
        try {
            Object wert = ziel.getClass().getMethod(name).invoke(ziel);
            if (wert instanceof Double d && !d.isNaN() && d >= 0) {
                return d;
            }
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // Nicht vorhanden oder nicht zugänglich – dann zeigen wir die Zeile eben nicht.
        }
        return -1;
    }

    private static long javaLaufzeit() {
        RuntimeMXBean rt = ManagementFactory.getRuntimeMXBean();
        return Math.max(0L, rt.getUptime() / 1000L);
    }

    private static String prozent(double anteil) {
        return Math.round(Math.max(0, Math.min(1, anteil)) * 100) + " %";
    }

    private static String gb(long mb) {
        return mb >= 1024 ? ServerMetrics.number(mb / 1024.0) + " GB" : mb + " MB";
    }

    private static String tpsFarbe(double v) {
        String farbe = v >= 19.0 ? "green" : v >= 15.0 ? "yellow" : "red";
        return "<" + farbe + ">" + ServerMetrics.number(Math.min(20.0, v)) + "</" + farbe + ">";
    }

    private static String msptFarbe(double v) {
        String farbe = v <= 35.0 ? "green" : v <= 50.0 ? "yellow" : "red";
        return "<" + farbe + ">" + ServerMetrics.number(v) + " ms</" + farbe + ">";
    }

    private static String speicherFarbe(long belegt, long max) {
        double anteil = max > 0 ? (double) belegt / max : 0;
        String farbe = anteil < 0.75 ? "green" : anteil < 0.9 ? "yellow" : "red";
        return "<" + farbe + ">" + belegt + " MB</" + farbe + ">";
    }

    private static String platteFarbe(long frei, long gesamt) {
        double anteil = gesamt > 0 ? (double) frei / gesamt : 1;
        String farbe = anteil > 0.15 ? "green" : anteil > 0.07 ? "yellow" : "red";
        return "<" + farbe + ">" + gb(frei) + "</" + farbe + ">";
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<World> unused = Bukkit.getWorlds();
        return unused.isEmpty() ? Collections.emptyList() : new ArrayList<>();
    }
}
