package de.mcsm.companion;

import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

import org.bukkit.Bukkit;
import org.bukkit.Chunk;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.WorldBorder;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/**
 * /rtp bzw. /wild – sucht eine sichere Zufallsposition in der eingestellten Welt.
 * Die Chunks werden asynchron geladen, pro Aufruf gilt ein festes Versuchsbudget.
 * Recht mcsm.rtp (Standard: alle).
 */
public final class RtpCommand implements TabExecutor {

    /** Kennung für die Abklingzeit im Teleportdienst. */
    private static final String KEY = "rtp";
    /** Sicherheitsabstand zur Weltgrenze in Blöcken. */
    private static final double BORDER_MARGIN = 48.0D;

    private final CompanionPlugin plugin;
    private final TeleportService teleports;
    /** Spieler, deren Suche gerade läuft – verhindert mehrere Suchen gleichzeitig. */
    private final Set<UUID> searching = new HashSet<>();

    public RtpCommand(CompanionPlugin plugin, TeleportService teleports) {
        this.plugin = plugin;
        this.teleports = teleports;
    }

    // ------------------------------------------------------------------ Konfiguration

    private World world(Player player) {
        String name = plugin.settings().raw().getString("teleport.rtp.world", "");
        if (name != null && !name.isBlank()) {
            World w = Bukkit.getWorld(name.trim());
            if (w != null) {
                return w;
            }
            plugin.getLogger().warning("teleport.rtp.world: Welt \"" + name.trim() + "\" gibt es nicht.");
        }
        List<World> worlds = Bukkit.getWorlds();
        return worlds.isEmpty() ? player.getWorld() : worlds.get(0);
    }

    private int radius() {
        return Math.max(64, plugin.settings().raw().getInt("teleport.rtp.radius", 5000));
    }

    private int minRadius() {
        return Math.max(0, plugin.settings().raw().getInt("teleport.rtp.min_radius", 500));
    }

    private int cooldown() {
        return Math.max(0, plugin.settings().raw().getInt("teleport.rtp.cooldown_seconds", 120));
    }

    private int attempts() {
        return Math.max(1, Math.min(200, plugin.settings().raw().getInt("teleport.rtp.attempts", 30)));
    }

    // ------------------------------------------------------------------ Befehl

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!plugin.settings().raw().getBoolean("features.rtp", true)) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        if (searching.contains(player.getUniqueId())) {
            Msg.error(player, "Deine Suche läuft bereits.");
            return true;
        }
        long left = teleports.cooldownLeft(player, KEY);
        if (left > 0L) {
            Msg.error(player, "Bitte warte noch <white><s></white> Sekunden.", Msg.number("s", left));
            return true;
        }
        World world = world(player);
        int max = attempts();
        searching.add(player.getUniqueId());
        Msg.send(player, "<gray>Suche einen sicheren Ort in <white><w></white> …</gray>",
                Msg.text("w", world.getName()));
        next(player, world, 1, max);
        return true;
    }

    /** Lädt den Chunk eines Zufallspunkts und prüft ihn, sonst folgt der nächste Versuch. */
    private void next(Player player, World world, int attempt, int max) {
        if (!player.isOnline()) {
            searching.remove(player.getUniqueId());
            return;
        }
        if (attempt > max) {
            searching.remove(player.getUniqueId());
            Msg.error(player, "Kein sicherer Ort gefunden. Versuch es gleich noch einmal.");
            return;
        }
        plugin.clock().suppress(player, 2000L);
        player.sendActionBar(Msg.mm("<gray>Suche einen sicheren Ort … <white><a></white><dark_gray>/</dark_gray><gray><m></gray>",
                Msg.number("a", attempt), Msg.number("m", max)));

        long[] xz = randomPoint(world);
        int x = (int) xz[0];
        int z = (int) xz[1];
        world.getChunkAtAsync(x >> 4, z >> 4, true).whenComplete((chunk, error) -> {
            if (!plugin.isEnabled()) {
                return;
            }
            Bukkit.getScheduler().runTask(plugin, () -> check(player, world, x, z, chunk, error, attempt, max));
        });
    }

    private void check(Player player, World world, int x, int z, Chunk chunk, Throwable error,
                       int attempt, int max) {
        if (!player.isOnline()) {
            searching.remove(player.getUniqueId());
            return;
        }
        if (error != null || chunk == null) {
            next(player, world, attempt + 1, max);
            return;
        }
        Location wish = new Location(world, x + 0.5D, world.getSeaLevel(), z + 0.5D,
                player.getLocation().getYaw(), player.getLocation().getPitch());
        Location safe = TeleportService.safeTarget(wish);
        if (safe == null) {
            next(player, world, attempt + 1, max);
            return;
        }
        searching.remove(player.getUniqueId());
        String where = safe.getBlockX() + ", " + safe.getBlockY() + ", " + safe.getBlockZ();
        teleports.request(player, safe, KEY,
                "<gray>Willkommen in der Wildnis (<white>" + where + "</white>).</gray>", 0, cooldown());
    }

    /** Zufälliger Punkt im Ring zwischen Mindest- und Höchstabstand, innerhalb der Weltgrenze. */
    private long[] randomPoint(World world) {
        WorldBorder border = world.getWorldBorder();
        Location center = border.getCenter();
        double limit = border.getSize() / 2.0D - BORDER_MARGIN;
        double max = Math.max(64.0D, Math.min(radius(), limit));
        double min = Math.max(0.0D, Math.min(minRadius(), max - 1.0D));

        ThreadLocalRandom rnd = ThreadLocalRandom.current();
        double angle = rnd.nextDouble() * Math.PI * 2.0D;
        double dist = Math.sqrt(rnd.nextDouble() * (max * max - min * min) + min * min);
        return new long[] {
                Math.round(center.getX() + Math.cos(angle) * dist),
                Math.round(center.getZ() + Math.sin(angle) * dist)
        };
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return Collections.emptyList();
    }
}
