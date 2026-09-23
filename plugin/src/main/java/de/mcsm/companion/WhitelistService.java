package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.TreeMap;
import java.util.logging.Level;
import java.util.regex.Pattern;

import com.destroystokyo.paper.profile.PlayerProfile;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import org.bukkit.Bukkit;
import org.bukkit.OfflinePlayer;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.player.PlayerKickEvent;

/**
 * Freigabeliste des Servers.
 *
 * <p>Gearbeitet wird ausschließlich mit der eingebauten Bukkit-Whitelist. Dadurch bleibt
 * {@code whitelist.json} im Vanilla-Format gültig und der Manager kann sie weiterhin lesen;
 * dieses Plugin führt bewusst keine eigene Liste.</p>
 *
 * <p>Das Auflösen eines noch nie dagewesenen Spielers geht über
 * {@link Bukkit#createProfile(String)} und {@code PlayerProfile.complete(false)} – also ohne das
 * veraltete {@code Bukkit.getOfflinePlayer(String)}. Weil dabei bei Mojang nachgefragt wird,
 * läuft dieser Schritt in einem Nebenthread; eingetragen wird erst wieder im Hauptthread.</p>
 */
public final class WhitelistService {

    /** Zeichen, die ein Minecraft-Name haben darf. */
    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9_]{1,16}");

    /** Mehrzeiliger Ablehnungstext beim Verbindungsversuch. */
    public static final List<String> DEFAULT_KICK = List.of(
            "<red><bold>Kein Zutritt</bold></red>",
            "",
            "<white>Du stehst nicht auf der Freigabeliste dieses Servers.</white>",
            "<gray>Frage das Team von <white><server_name></white>, ob es dich freischaltet.</gray>");

    /** Text für jemanden, dem die Freigabe im laufenden Spiel entzogen wird. */
    public static final List<String> DEFAULT_REMOVED_KICK = List.of(
            "<red><bold>Freigabe entzogen</bold></red>",
            "",
            "<white>Du stehst nicht mehr auf der Freigabeliste von <server_name>.</white>",
            "<gray>Bei Fragen wende dich bitte an das Team.</gray>");

    public static final String DEFAULT_ON = "<green>Die Freigabeliste ist jetzt <bold>aktiv</bold>.</green> "
            + "<gray>Nur freigegebene Spieler kommen noch auf <white><server_name></white>.</gray>";
    public static final String DEFAULT_OFF = "<yellow>Die Freigabeliste ist jetzt <bold>aus</bold>.</yellow> "
            + "<gray><white><server_name></white> steht wieder allen offen.</gray>";

    private final CompanionPlugin plugin;

    /**
     * Fertig gebauter Ablehnungstext. Der Whitelist-Prüfpunkt von Paper läuft nicht zwingend im
     * Hauptthread, darum wird der Text hier vorgehalten statt beim Ereignis aus der Datei gelesen.
     */
    private volatile Component kickMessage = Component.empty();

    public WhitelistService(CompanionPlugin plugin) {
        this.plugin = plugin;
        applyConfig();
    }

    /** Texte aus der Konfiguration neu aufbauen (Aufruf beim Start und nach /admin reload). */
    public void applyConfig() {
        kickMessage = lines("whitelist.kick_message", DEFAULT_KICK);
    }

    /** Ist das Modul eingeschaltet? Ausgeschaltet bleibt die Vanilla-Whitelist unberührt. */
    public boolean featureOn() {
        return plugin.settings().raw().getBoolean("features.whitelist", true);
    }

    /** Vorgefertigter Ablehnungstext für den Verbindungsversuch. */
    public Component kickMessage() {
        return kickMessage;
    }

    // ------------------------------------------------------------------ Zustand

    public boolean isOn() {
        return Bukkit.hasWhitelist();
    }

    /**
     * Freigabeliste an- oder ausschalten. Die Chatmeldung macht {@link WhitelistListener},
     * damit sie auch bei /minecraft:whitelist on|off und beim Manager erscheint.
     */
    public void setOn(boolean on) {
        Bukkit.setWhitelist(on);
    }

    /** whitelist.json neu einlesen. */
    public void reload() {
        Bukkit.reloadWhitelist();
    }

    /** Alle freigegebenen Namen, alphabetisch und ohne Leereinträge. */
    public List<String> names() {
        TreeMap<String, String> sorted = new TreeMap<>(String.CASE_INSENSITIVE_ORDER);
        for (OfflinePlayer op : Bukkit.getWhitelistedPlayers()) {
            String n = op.getName();
            if (n != null && !n.isBlank()) {
                sorted.put(n, n);
            }
        }
        return new ArrayList<>(sorted.values());
    }

    /** Steht dieser Name (Groß-/Kleinschreibung egal) bereits auf der Liste? */
    public boolean contains(String name) {
        for (String n : names()) {
            if (n.equalsIgnoreCase(name)) {
                return true;
            }
        }
        return false;
    }

    /** Freigegebener Spieler mit genau diesem Namen, sonst null. */
    private OfflinePlayer listed(String name) {
        for (OfflinePlayer op : Bukkit.getWhitelistedPlayers()) {
            String n = op.getName();
            if (n != null && n.equalsIgnoreCase(name)) {
                return op;
            }
        }
        return null;
    }

    // ------------------------------------------------------------------ Eintragen

    /**
     * Trägt einen Spieler ein – auch einen, der noch nie verbunden war. Die Rückmeldung geht an
     * den Absender und kann bei einer Mojang-Abfrage einen Moment auf sich warten lassen.
     */
    public void add(CommandSender sender, String rawName) {
        String name = rawName == null ? "" : rawName.trim();
        if (!validName(name)) {
            Msg.error(sender, "<n> ist kein gültiger Spielername.", Msg.text("n", name));
            return;
        }
        if (contains(name)) {
            Msg.send(sender, "<gray><white><n></white> steht bereits auf der Freigabeliste.</gray>",
                    Msg.text("n", name));
            return;
        }

        // Schnellweg: online oder im Namensspeicher des Servers – ohne Nachfrage bei Mojang.
        Player online = Bukkit.getPlayerExact(name);
        if (online != null) {
            finish(sender, online, online.getName());
            return;
        }
        OfflinePlayer cached = Bukkit.getOfflinePlayerIfCached(name);
        if (cached != null) {
            finish(sender, cached, cached.getName() == null ? name : cached.getName());
            return;
        }

        if (!plugin.isEnabled()) {
            Msg.error(sender, "Der Server fährt gerade herunter – bitte später erneut versuchen.");
            return;
        }
        Msg.send(sender, "<gray>Suche <white><n></white> …</gray>", Msg.text("n", name));
        Bukkit.getScheduler().runTaskAsynchronously(plugin, () -> resolveThenAdd(sender, name));
    }

    /** Nebenthread: Profil vervollständigen, danach zurück in den Hauptthread. */
    private void resolveThenAdd(CommandSender sender, String name) {
        String resolved = null;
        try {
            PlayerProfile profile = Bukkit.createProfile(name);
            if (profile.complete(false) && profile.getId() != null) {
                String fromProfile = profile.getName();
                resolved = fromProfile == null || fromProfile.isBlank() ? name : fromProfile;
            }
        } catch (RuntimeException ex) {
            plugin.getLogger().log(Level.WARNING, "Freigabeliste: Profil \"" + name + "\" konnte nicht geladen werden", ex);
        }
        final String finalName = resolved;
        if (!plugin.isEnabled()) {
            return;
        }
        Bukkit.getScheduler().runTask(plugin, () -> {
            if (finalName == null) {
                reply(sender, "<red>Es gibt keinen Spieler mit dem Namen <white><n></white>.</red>",
                        Msg.text("n", name));
                return;
            }
            OfflinePlayer found = Bukkit.getOfflinePlayerIfCached(finalName);
            if (found != null) {
                finish(sender, found, finalName);
                return;
            }
            // Offline-Modus: das Profil ist gültig, steht aber nicht im Namensspeicher. Dann trägt
            // der Vanilla-Befehl ein, damit in whitelist.json wirklich ein Name und keine leere
            // Zeichenkette landet. Der Name wird vorher erneut geprüft, weil er hier in eine
            // Befehlszeile eingesetzt wird.
            if (!validName(finalName)) {
                reply(sender, "<red><n> ist kein gültiger Spielername.</red>", Msg.text("n", finalName));
                return;
            }
            boolean ok;
            try {
                ok = Bukkit.dispatchCommand(Bukkit.getConsoleSender(), "minecraft:whitelist add " + finalName);
            } catch (RuntimeException ex) {
                plugin.getLogger().log(Level.WARNING,
                        "Freigabeliste: /minecraft:whitelist add " + finalName + " fehlgeschlagen", ex);
                ok = false;
            }
            if (ok && contains(finalName)) {
                announceAdded(sender, finalName);
            } else {
                reply(sender, "<red><n> konnte nicht eingetragen werden.</red> "
                        + "<gray>Der Spieler muss einmal verbunden gewesen sein oder ein gültiges Konto besitzen.</gray>",
                        Msg.text("n", finalName));
            }
        });
    }

    /** Hauptthread: eintragen und melden. */
    private void finish(CommandSender sender, OfflinePlayer target, String shownName) {
        try {
            target.setWhitelisted(true);
        } catch (RuntimeException ex) {
            plugin.getLogger().log(Level.WARNING, "Freigabeliste: \"" + shownName + "\" konnte nicht eingetragen werden", ex);
            reply(sender, "<red><n> konnte nicht eingetragen werden – Einzelheiten stehen im Protokoll.</red>",
                    Msg.text("n", shownName));
            return;
        }
        announceAdded(sender, shownName);
    }

    private void announceAdded(CommandSender sender, String shownName) {
        reply(sender, "<green><white><n></white> steht jetzt auf der Freigabeliste.</green>"
                + "<gray> Einträge: <white><c></white></gray>",
                Msg.text("n", shownName), Msg.number("c", names().size()));
        if (!isOn()) {
            reply(sender, "<gray>Hinweis: Die Freigabeliste ist zurzeit <white>aus</white> – "
                    + "einschalten mit </gray><click:suggest_command:'/wl an'><white>/wl an</white></click><gray>.</gray>");
        }
    }

    // ------------------------------------------------------------------ Austragen

    /**
     * Nimmt einen Spieler von der Liste und wirft ihn, falls er online ist, mit Hinweistext hinaus.
     * Liefert false, wenn der Name gar nicht auf der Liste stand.
     */
    public boolean remove(CommandSender sender, String rawName) {
        String name = rawName == null ? "" : rawName.trim();
        OfflinePlayer target = listed(name);
        if (target == null) {
            Msg.error(sender, "<n> steht nicht auf der Freigabeliste.", Msg.text("n", name));
            return false;
        }
        String shown = target.getName() == null ? name : target.getName();
        try {
            target.setWhitelisted(false);
        } catch (RuntimeException ex) {
            plugin.getLogger().log(Level.WARNING, "Freigabeliste: \"" + shown + "\" konnte nicht entfernt werden", ex);
            Msg.error(sender, "<n> konnte nicht entfernt werden – Einzelheiten stehen im Protokoll.",
                    Msg.text("n", shown));
            return false;
        }
        Msg.send(sender, "<yellow><white><n></white> wurde von der Freigabeliste genommen.</yellow>",
                Msg.text("n", shown));

        // Dieselbe Ausnahme wie in kickNotListed(): der Wartungszugang bleibt drin, auch wenn er
        // (noch) nicht ge-OPt ist – sonst käme er an der Vanilla-Freigabeliste nicht mehr vorbei.
        Player online = Bukkit.getPlayerExact(shown);
        if (online != null && isOn() && !online.isOp() && !plugin.settings().isAdmin(shown)) {
            kick(online);
            Msg.send(sender, "<gray><white><n></white> war online und wurde getrennt.</gray>", Msg.text("n", shown));
        }
        return true;
    }

    /** Wirft einen Spieler mit dem Text für entzogene Freigaben hinaus. */
    public void kick(Player player) {
        try {
            player.kick(lines("whitelist.removed_kick_message", DEFAULT_REMOVED_KICK),
                    PlayerKickEvent.Cause.WHITELIST);
        } catch (RuntimeException ex) {
            plugin.getLogger().log(Level.WARNING, "Freigabeliste: Trennen von " + player.getName() + " fehlgeschlagen", ex);
        }
    }

    /**
     * Wirft beim Einschalten alle Spieler hinaus, die nicht freigegeben sind (OPs bleiben).
     * Liefert die Anzahl der Getrennten.
     */
    public int kickNotListed() {
        int n = 0;
        for (Player p : new ArrayList<>(Bukkit.getOnlinePlayers())) {
            if (p.isOp() || p.isWhitelisted() || plugin.settings().isAdmin(p.getName())) {
                continue;
            }
            kick(p);
            n++;
        }
        return n;
    }

    // ------------------------------------------------------------------ Texte

    /** Meldung für das An- bzw. Ausschalten (MiniMessage, Platzhalter <server_name>). */
    public Component toggleMessage(boolean on) {
        String key = on ? "whitelist.on_format" : "whitelist.off_format";
        String def = on ? DEFAULT_ON : DEFAULT_OFF;
        String text = plugin.settings().raw().getString(key, def);
        return Msg.prefixed(text == null ? def : text, serverTag());
    }

    public boolean broadcastToggle() {
        return plugin.settings().raw().getBoolean("whitelist.broadcast", true);
    }

    public boolean kickOnEnable() {
        return plugin.settings().raw().getBoolean("whitelist.kick_not_listed_on_enable", false);
    }

    private TagResolver serverTag() {
        return Msg.text("server_name", plugin.settings().serverName);
    }

    /** Mehrzeiligen Text aus einer Liste bauen; leere Liste = mitgelieferter Standard. */
    private Component lines(String key, List<String> fallback) {
        List<String> raw = plugin.settings().raw().getStringList(key);
        List<String> use = raw.isEmpty() ? fallback : raw;
        return Msg.mm(String.join("\n", use), serverTag());
    }

    /** Antwort an den Absender; ist er zwischenzeitlich weg, geht sie in die Konsole. */
    private static void reply(CommandSender sender, String text, TagResolver... tags) {
        if (sender instanceof Player p && !p.isOnline()) {
            Bukkit.getConsoleSender().sendMessage(Msg.prefixed(text, tags));
            return;
        }
        Msg.send(sender, text, tags);
    }

    /** Trägt der Text nur Zeichen, die ein Minecraft-Name haben darf? */
    public static boolean validName(String name) {
        return name != null && NAME.matcher(name).matches();
    }

    /** "an"/"on"/"ein" = true, "aus"/"off" = false, sonst null. */
    public static Boolean parseSwitch(String raw) {
        switch (raw.toLowerCase(Locale.ROOT)) {
            case "an": case "on": case "ein": case "true":
                return Boolean.TRUE;
            case "aus": case "off": case "false":
                return Boolean.FALSE;
            default:
                return null;
        }
    }
}
