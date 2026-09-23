package de.mcsm.companion;

import java.time.Duration;
import java.util.List;

import net.kyori.adventure.key.Key;
import net.kyori.adventure.sound.Sound;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import net.kyori.adventure.title.Title;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerTeleportEvent;

/**
 * Begrüßung beim Beitritt: Titel, kurzer Ton und Hinweis auf /rules und /mcsm. Beim allerersten
 * Beitritt gibt es – falls gewünscht – einen Teleport zum Spawn.
 */
public final class WelcomeListener implements Listener {

    public static final String DEFAULT_TITLE = "<gradient:#3ddc84:#8ff0b4><bold><server_name></bold></gradient>";
    public static final String DEFAULT_SUBTITLE = "<gray>Willkommen, <white><name></white>!</gray>";
    /** Kurzer, unaufdringlicher Ton. */
    private static final Key SOUND = Key.key("minecraft:block.note_block.chime");
    /** Verzögerung für den Teleport, damit der Client fertig geladen ist. */
    private static final long FIRST_JOIN_DELAY = 20L;

    private final CompanionPlugin plugin;

    public WelcomeListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        YamlConfiguration y = plugin.settings().raw();
        if (!y.getBoolean("features.welcome", true)) {
            return;
        }
        Player p = event.getPlayer();
        boolean first = !p.hasPlayedBefore();
        Bukkit.getScheduler().runTask(plugin, () -> {
            if (p.isOnline() && !plugin.vanish().isVanished(p)) {
                greet(p, y);
            }
        });
        if (first) {
            Bukkit.getScheduler().runTaskLater(plugin, () -> {
                if (p.isOnline()) {
                    firstJoin(p, y);
                }
            }, FIRST_JOIN_DELAY);
        }
    }

    private void greet(Player p, YamlConfiguration y) {
        Config cfg = plugin.settings();
        TagResolver tags = TagResolver.resolver(
                Msg.name("name", p),
                Msg.text("server_name", cfg.serverName),
                Msg.number("online", plugin.vanish().visibleOnline()),
                Msg.number("max", Bukkit.getMaxPlayers()));
        Component title = Msg.mm(str(y, "welcome.title", DEFAULT_TITLE), tags);
        Component subtitle = Msg.mm(str(y, "welcome.subtitle", DEFAULT_SUBTITLE), tags);
        p.showTitle(Title.title(title, subtitle,
                Title.Times.times(Duration.ofMillis(300L), Duration.ofSeconds(3L), Duration.ofMillis(700L))));
        if (y.getBoolean("welcome.sound", true)) {
            p.playSound(Sound.sound(SOUND, Sound.Source.MASTER, 0.6F, 1.2F));
        }
        if (y.getBoolean("welcome.hint", true)) {
            Msg.send(p, "<gray>Regeln: </gray><click:suggest_command:'/rules'><white>/rules</white></click>"
                    + " <dark_gray>•</dark_gray> <gray>Alle Befehle: </gray>"
                    + "<click:suggest_command:'/mcsm'><white>/mcsm</white></click>");
        }
    }

    private void firstJoin(Player p, YamlConfiguration y) {
        if (!y.getBoolean("welcome.first_join_spawn", true)) {
            return;
        }
        List<World> worlds = Bukkit.getWorlds();
        World main = worlds.isEmpty() ? p.getWorld() : worlds.get(0);
        Location spawn = main.getSpawnLocation();
        p.teleportAsync(spawn, PlayerTeleportEvent.TeleportCause.PLUGIN).thenAccept(ok -> {
            if (!Boolean.TRUE.equals(ok)) {
                plugin.getLogger().warning("Erst-Teleport zum Spawn fehlgeschlagen.");
            }
        });
    }

    private static String str(YamlConfiguration y, String key, String def) {
        String v = y.getString(key, def);
        return v == null ? def : v;
    }
}
