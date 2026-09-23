package de.mcsm.companion;

import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;

/**
 * Kennzahlen des laufenden Servers: TPS, MSPT, Betriebszeit seit Plugin-Start, geladene Chunks
 * und Entities. {@link #appendJson(StringBuilder)} liefert die Zeilen für status.json, damit der
 * Manager sie im Dashboard anzeigen kann. Zusätzlich meldet die Klasse dem Wartungszugang, wenn
 * die TPS längere Zeit unter dem Warnwert liegen.
 */
public final class ServerMetrics implements Runnable {

    /** Kürzester Abstand zwischen zwei Warnungen (Millisekunden). */
    private static final long WARN_COOLDOWN_MS = 300_000L;

    private final CompanionPlugin plugin;
    private final long startedAt = System.currentTimeMillis();
    /** Zeitpunkt, seit dem die TPS unter dem Warnwert liegen; 0 = alles in Ordnung. */
    private long lowSince;
    private long lastWarn;

    public ServerMetrics(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    /** TPS der letzten Minute, auf 20 gedeckelt. */
    public double tps() {
        double[] t = Bukkit.getTPS();
        return t.length == 0 ? 20.0D : Math.min(20.0D, t[0]);
    }

    /** Durchschnittliche Tickdauer in Millisekunden. */
    public double mspt() {
        return Bukkit.getAverageTickTime();
    }

    /** Betriebszeit seit dem Start des Plugins in Sekunden. */
    public long uptimeSeconds() {
        return Math.max(0L, (System.currentTimeMillis() - startedAt) / 1000L);
    }

    public int loadedChunks() {
        int n = 0;
        for (World w : Bukkit.getWorlds()) {
            n += w.getLoadedChunks().length;
        }
        return n;
    }

    public int entities() {
        int n = 0;
        for (World w : Bukkit.getWorlds()) {
            n += w.getEntityCount();
        }
        return n;
    }

    /** Farbiger TPS-Wert für Anzeigen (MiniMessage). */
    public String tpsColored() {
        double v = tps();
        String color = v >= 18.0D ? "<green>" : v >= 15.0D ? "<yellow>" : "<red>";
        String close = v >= 18.0D ? "</green>" : v >= 15.0D ? "</yellow>" : "</red>";
        return color + number(v) + close;
    }

    /** Betriebszeit als "3d 4h 12m" bzw. "12m 30s". */
    public static String formatUptime(long seconds) {
        long s = Math.max(0L, seconds);
        long d = s / 86400L;
        long h = (s % 86400L) / 3600L;
        long m = (s % 3600L) / 60L;
        if (d > 0L) {
            return d + "d " + h + "h " + m + "m";
        }
        if (h > 0L) {
            return h + "h " + m + "m";
        }
        return m + "m " + (s % 60L) + "s";
    }

    /**
     * Hängt die Kennzahlen als JSON-Felder an – jede Zeile endet mit Komma, passt also mitten in
     * ein Objekt. Wird von StatusWriter.build() benutzt.
     */
    public void appendJson(StringBuilder sb) {
        if (!plugin.settings().raw().getBoolean("features.metrics", true)) {
            return;                                 // abgeschaltet: keine Kennzahlen in status.json
        }
        sb.append("  \"tps\": ").append(number(tps())).append(",\n");
        sb.append("  \"mspt\": ").append(number(mspt())).append(",\n");
        sb.append("  \"uptime\": ").append(uptimeSeconds()).append(",\n");
        sb.append("  \"loaded_chunks\": ").append(loadedChunks()).append(",\n");
        sb.append("  \"entities\": ").append(entities()).append(",\n");
    }

    /** Prüfung im Hintergrund (alle 5 Sekunden). */
    @Override
    public void run() {
        YamlConfiguration y = plugin.settings().raw();
        if (!y.getBoolean("features.metrics", true)) {
            lowSince = 0L;
            return;
        }
        double warnAt = y.getDouble("metrics.tps_warn", 15.0D);
        long after = Math.max(5L, y.getLong("metrics.warn_after_seconds", 60L)) * 1000L;
        long now = System.currentTimeMillis();
        double current = tps();
        if (current >= warnAt) {
            lowSince = 0L;
            return;
        }
        if (lowSince == 0L) {
            lowSince = now;
            return;
        }
        if (now - lowSince < after || now - lastWarn < WARN_COOLDOWN_MS) {
            return;
        }
        lastWarn = now;
        warnMaintenance(current, (now - lowSince) / 1000L);
    }

    /** Warnung nur an den Wartungszugang und ins Serverprotokoll. */
    private void warnMaintenance(double current, long forSeconds) {
        plugin.getLogger().warning("Leistungswarnung: TPS " + number(current) + " seit " + forSeconds
                + " s, MSPT " + number(mspt()) + ", Chunks " + loadedChunks() + ", Entities " + entities() + ".");
        for (Player p : Bukkit.getOnlinePlayers()) {
            if (!plugin.settings().isAdmin(p.getName())) {
                continue;
            }
            Msg.error(p, "Leistungswarnung: TPS <white><tps></white> seit <white><sec></white> s, "
                            + "MSPT <white><mspt></white>, Chunks <white><chunks></white>, "
                            + "Entities <white><entities></white>.",
                    Msg.text("tps", number(current)),
                    Msg.number("sec", forSeconds),
                    Msg.text("mspt", number(mspt())),
                    Msg.number("chunks", loadedChunks()),
                    Msg.number("entities", entities()));
        }
    }

    /** Zahl mit zwei Nachkommastellen und Punkt als Trenner (JSON-tauglich). */
    public static String number(double v) {
        return String.format(Locale.ROOT, "%.2f", v);
    }
}
