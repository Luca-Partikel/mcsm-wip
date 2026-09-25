package de.mcsm.companion;

import java.util.List;

import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import org.bukkit.Bukkit;
import org.bukkit.configuration.file.YamlConfiguration;

/**
 * Rotierende Hinweise im Chat. Das Intervall steht in der Konfiguration (Standard 10 Minuten) und
 * wird bei jedem Durchlauf neu gelesen, damit ein Neuladen sofort wirkt. Ohne Spieler auf dem
 * Server passiert nichts.
 */
public final class AutoBroadcastTask implements Runnable {

    /** Standardhinweise, falls in der Konfiguration keine stehen. */
    public static final List<String> DEFAULT_MESSAGES = List.of(
            "<gray>Tipp: Mit </gray><white>/sethome</white><gray> setzt du dir ein Zuhause "
                    + "und kommst mit </gray><white>/home</white><gray> zurück.</gray>",
            "<gray>Tipp: </gray><white>/tpa Name</white><gray> fragt jemanden, ob du zu ihm "
                    + "teleportieren darfst.</gray>",
            "<gray>Tipp: Die Serverregeln stehen in </gray><white>/rules</white><gray>.</gray>",
            "<gray>Tipp: Mit </gray><white>/sb</white><gray> blendest du die Seitenleiste ein oder aus.</gray>",
            "<gray>Tipp: </gray><white>/mcsm</white><gray> listet alle Befehle dieses Servers auf.</gray>");

    private final CompanionPlugin plugin;
    private long lastSent;
    private int next;

    public AutoBroadcastTask(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.lastSent = System.currentTimeMillis();
    }

    @Override
    public void run() {
        YamlConfiguration y = plugin.settings().raw();
        // „tips“ setzt der Manager (Schalter im Programm), features.broadcast bleibt als
        // zweiter Schalter für alle, die die Datei von Hand pflegen.
        if (!y.getBoolean("tips", true) || !y.getBoolean("features.broadcast", true)) {
            return;
        }
        long minutes = Math.max(1L, y.getLong("broadcast.interval_minutes", 10L));
        long now = System.currentTimeMillis();
        if (now - lastSent < minutes * 60_000L) {
            return;
        }
        if (Bukkit.getOnlinePlayers().isEmpty()) {
            // Ohne Zuhörer wird nichts verbraucht; der Zähler läuft trotzdem weiter.
            lastSent = now;
            return;
        }
        List<String> messages = y.getStringList("broadcast.messages");
        if (messages.isEmpty()) {
            messages = DEFAULT_MESSAGES;
        }
        lastSent = now;
        String text = messages.get(Math.floorMod(next, messages.size()));
        next = Math.floorMod(next + 1, messages.size());
        TagResolver tags = TagResolver.resolver(
                Msg.text("server_name", plugin.settings().serverName),
                Msg.number("online", plugin.vanish().visibleOnline()),
                Msg.number("max", Bukkit.getMaxPlayers()));
        Bukkit.broadcast(Msg.prefixed(text, tags));
    }
}
