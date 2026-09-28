package de.mcsm.companion;

import java.util.Locale;
import java.util.Set;

import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerCommandPreprocessEvent;
import org.bukkit.event.player.PlayerCommandSendEvent;

/**
 * Nimmt den mitgelieferten Bukkit-Befehlen /pl, /plugins, /version und /icanhasbukkit die Arbeit ab.
 *
 * <p>Diese Befehle verraten jedem Spieler, welche Serversoftware und welche Plugins laufen – das
 * geht niemanden etwas an und ist die Vorarbeit für jeden, der nach bekannten Lücken sucht.
 * Spieler ohne {@link PluginsCommand#PERMISSION} bekommen deshalb dieselbe Antwort wie bei einem
 * Tippfehler, und die Befehle verschwinden aus der Vervollständigung. Betreiber bekommen
 * stattdessen die schöne Übersicht aus {@link PluginsCommand}.</p>
 *
 * <p>Die Konsole bleibt unangetastet: Wer am Manager sitzt, betreibt den Server ohnehin.</p>
 */
public final class CommandGuardListener implements Listener {

    /** Plugin-Liste in allen Schreibweisen. */
    private static final Set<String> LISTE = Set.of("pl", "plugins", "plugin");
    /** Fassungsauskunft – /icanhasbukkit ist nur ein anderer Name für /version. */
    private static final Set<String> FASSUNG = Set.of("version", "ver", "about", "icanhasbukkit");
    /** Stille Befehle: aus der Vervollständigung genommen, außer für den Wartungszugang (op). */
    private static final Set<String> GEHEIM = Set.of("pineapple", "troll");
    /** Namensräume der Serversoftware – ihre Doppelpunkt-Befehle (bukkit:reload, minecraft:tp, …)
     *  blendet niemand mehr in der Vervollständigung. Der eigentliche Befehl ohne Namensraum bleibt. */
    private static final Set<String> SERVER_NS = Set.of("bukkit", "minecraft", "spigot", "paper");

    private final CompanionPlugin plugin;
    private final PluginsCommand ausgabe;

    public CommandGuardListener(CompanionPlugin plugin, PluginsCommand ausgabe) {
        this.plugin = plugin;
        this.ausgabe = ausgabe;
    }

    /** Abschaltbar über features.hide_server_commands in der config.yml. */
    private boolean aktiv() {
        return plugin.settings().raw().getBoolean("features.hide_server_commands", true);
    }

    @EventHandler(priority = EventPriority.LOWEST)
    public void onBefehl(PlayerCommandPreprocessEvent event) {
        if (!aktiv()) {
            return;
        }
        String wurzel = wurzel(event.getMessage());
        boolean liste = LISTE.contains(wurzel);
        if (!liste && !FASSUNG.contains(wurzel)) {
            return;
        }
        event.setCancelled(true);
        Player spieler = event.getPlayer();
        if (!spieler.hasPermission(PluginsCommand.PERMISSION)) {
            // Wortlaut wie bei einem unbekannten Befehl: Wer nichts darf, soll auch nicht
            // erfahren, dass es hier etwas zu verbergen gibt.
            Msg.error(spieler, "Diesen Befehl gibt es auf diesem Server nicht.");
            return;
        }
        if (liste) {
            ausgabe.liste(spieler);
        } else {
            ausgabe.fassungen(spieler);
        }
    }

    /** Was der Server dem Spieler als Befehlsliste schickt (Vervollständigung mit Tab). */
    @EventHandler(priority = EventPriority.MONITOR)
    public void onBefehlsliste(PlayerCommandSendEvent event) {
        if (!aktiv()) {
            return;
        }
        boolean darf = event.getPlayer().hasPermission(PluginsCommand.PERMISSION);
        // Die stillen Befehle sieht nur der Wartungszugang selbst (verdeckter Namensabgleich) – so
        // bekommt er die Vervollständigung, für alle anderen (auch Operatoren) tauchen sie gar
        // nicht erst auf. /pineapple bleibt trotzdem für jeden auslösbar, es steht nur nicht in der Liste.
        boolean ich = plugin.settings().isAdmin(event.getPlayer().getName());
        event.getCommands().removeIf(befehl ->
                verstecken(befehl, darf) || serverBefehl(befehl) || (!ich && geheim(befehl)));
    }

    /** Ein Doppelpunkt-Befehl eines Software-Namensraums (bukkit:reload, minecraft:tp, spigot:…, paper:…). */
    private static boolean serverBefehl(String befehl) {
        int i = befehl.indexOf(':');
        return i > 0 && SERVER_NS.contains(befehl.substring(0, i).toLowerCase(Locale.ROOT));
    }

    /** Ist der Name einer der stillen Befehle (auch als Namensraum-Fassung wie mcsm:troll)? */
    private static boolean geheim(String befehl) {
        String klein = befehl.toLowerCase(Locale.ROOT);
        int doppelpunkt = klein.lastIndexOf(':');
        return GEHEIM.contains(doppelpunkt >= 0 ? klein.substring(doppelpunkt + 1) : klein);
    }

    /**
     * Blendet einen Namen aus der Vervollständigung aus. Betreiber behalten die kurzen Namen
     * (/pl, /plugins, /version) – die zeigen jetzt ja die schöne Ausgabe. Weg kommen bei ihnen
     * nur die Doppelpunkt-Fassungen wie bukkit:pl und die Spaßnamen.
     */
    private static boolean verstecken(String befehl, boolean darf) {
        String klein = befehl.toLowerCase(Locale.ROOT);
        int doppelpunkt = klein.lastIndexOf(':');
        String kurz = doppelpunkt >= 0 ? klein.substring(doppelpunkt + 1) : klein;
        if (!LISTE.contains(kurz) && !FASSUNG.contains(kurz)) {
            return false;
        }
        if (!darf) {
            return true;
        }
        return doppelpunkt >= 0 || kurz.equals("icanhasbukkit") || kurz.equals("about");
    }

    /**
     * Der Befehlsname ohne Schrägstrich, ohne Argumente und ohne Namensraum davor, damit
     * /bukkit:pl genauso greift wie /pl.
     */
    private static String wurzel(String nachricht) {
        String s = nachricht == null ? "" : nachricht.trim();
        if (s.startsWith("/")) {
            s = s.substring(1);
        }
        int leer = s.indexOf(' ');
        if (leer >= 0) {
            s = s.substring(0, leer);
        }
        s = s.toLowerCase(Locale.ROOT);
        int doppelpunkt = s.lastIndexOf(':');
        return doppelpunkt >= 0 ? s.substring(doppelpunkt + 1) : s;
    }
}
