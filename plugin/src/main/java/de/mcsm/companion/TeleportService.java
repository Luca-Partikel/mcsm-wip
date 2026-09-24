package de.mcsm.companion;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import net.kyori.adventure.sound.Sound;
import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.HeightMap;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.entity.Player;
import org.bukkit.event.player.PlayerTeleportEvent;
import org.bukkit.scheduler.BukkitTask;

/**
 * Gemeinsamer Dienst für verzögerte Teleports: Aufwärmzeit mit Fortschritt in der Actionbar,
 * Abbruch bei Bewegung oder Schaden, Abklingzeit je Befehl und eine Hilfsmethode für sichere
 * Zielpositionen. Wird von /back, /rtp, /warp und /top benutzt und kann genauso von /home,
 * /spawn und /tpa verwendet werden.
 */
public final class TeleportService {

    /** Prüfintervall der Aufwärmzeit in Ticks. */
    private static final long STEP_TICKS = 2L;
    /** Erlaubte Abweichung von der Startposition in Blöcken. */
    private static final double MOVE_LIMIT = 0.5D;
    /** Breite des Fortschrittsbalkens in Zeichen. */
    private static final int BAR_WIDTH = 20;
    /** Recht, das Aufwärm- und Abklingzeit überspringt. */
    public static final String BYPASS = "mcsm.teleport.bypass";

    /** Blöcke, auf oder in denen niemand landen soll. */
    private static final Set<Material> DANGER = Set.of(
            Material.LAVA, Material.FIRE, Material.SOUL_FIRE, Material.CACTUS, Material.MAGMA_BLOCK,
            Material.CAMPFIRE, Material.SOUL_CAMPFIRE, Material.SWEET_BERRY_BUSH, Material.POWDER_SNOW,
            Material.WITHER_ROSE, Material.POINTED_DRIPSTONE, Material.NETHER_PORTAL, Material.END_PORTAL,
            Material.END_GATEWAY, Material.LAVA_CAULDRON);

    /** Ein laufender Teleport mit Aufwärmzeit. */
    private static final class Pending {
        private final Location target;
        private final Location origin;
        private final String command;
        private final String message;
        private final int totalTicks;
        private final int cooldownSeconds;
        private int leftTicks;

        Pending(Location target, Location origin, String command, String message,
                int totalTicks, int cooldownSeconds) {
            this.target = target;
            this.origin = origin;
            this.command = command;
            this.message = message;
            this.totalTicks = Math.max(1, totalTicks);
            this.cooldownSeconds = cooldownSeconds;
            this.leftTicks = this.totalTicks;
        }
    }

    private final CompanionPlugin plugin;
    private final Map<UUID, Pending> pending = new HashMap<>();
    /** Spieler -> Befehl -> Zeitpunkt (ms), ab dem der Befehl wieder frei ist. */
    private final Map<UUID, Map<String, Long>> cooldowns = new HashMap<>();
    private BukkitTask ticker;

    public TeleportService(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    /** Startet die wiederkehrende Prüfung der Aufwärmzeiten. Weitere Aufrufe sind wirkungslos. */
    public void start() {
        if (ticker == null) {
            ticker = Bukkit.getScheduler().runTaskTimer(plugin, this::tick, STEP_TICKS, STEP_TICKS);
        }
    }

    /** Bricht alle laufenden Teleports ab und beendet die Prüfung. */
    public void shutdown() {
        pending.clear();
        cooldowns.clear();
        if (ticker != null) {
            ticker.cancel();
            ticker = null;
        }
    }

    // ------------------------------------------------------------------ Konfiguration

    private int configWarmup() {
        return clamp(plugin.settings().raw().getInt("teleport.warmup_seconds", 3), 0, 60);
    }

    private int configCooldown() {
        return clamp(plugin.settings().raw().getInt("teleport.cooldown_seconds", 5), 0, 3600);
    }

    private boolean warmupEnabled() {
        return plugin.settings().raw().getBoolean("features.teleport_warmup", true);
    }

    private boolean cancelOnDamage() {
        return plugin.settings().raw().getBoolean("teleport.cancel_on_damage", true);
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }

    /** Spieler, für die weder Aufwärm- noch Abklingzeit gilt. */
    public boolean bypasses(Player player) {
        return player.isOp() || player.hasPermission(BYPASS);
    }

    // ------------------------------------------------------------------ Öffentliche API

    /**
     * Teleport mit den konfigurierten Standardzeiten anfordern.
     *
     * @param player  Spieler, der teleportiert wird
     * @param target  Ziel; eine Welt von {@code null} bricht mit Hinweis ab
     * @param command Kennung für die Abklingzeit, z. B. "home", "spawn", "tpa"
     * @param message MiniMessage-Text nach dem Teleport (ohne Präfix), darf {@code null} sein
     * @return true, wenn der Teleport läuft oder bereits ausgeführt wurde
     */
    public boolean request(Player player, Location target, String command, String message) {
        return request(player, target, command, message, -1, -1);
    }

    /**
     * Wie {@link #request(Player, Location, String, String)}, jedoch mit eigenen Zeiten.
     *
     * @param warmupSeconds   Aufwärmzeit in Sekunden, negativ = Wert aus der Konfiguration
     * @param cooldownSeconds Abklingzeit in Sekunden, negativ = Wert aus der Konfiguration
     */
    public boolean request(Player player, Location target, String command, String message,
                           int warmupSeconds, int cooldownSeconds) {
        if (target == null || target.getWorld() == null) {
            Msg.error(player, "Das Ziel liegt in einer Welt, die es nicht mehr gibt.");
            return false;
        }
        String key = key(command);
        if (pending.containsKey(player.getUniqueId())) {
            Msg.error(player, "Es läuft bereits ein Teleport. Beweg dich, um ihn abzubrechen.");
            return false;
        }
        long left = cooldownLeft(player, key);
        if (left > 0L) {
            Msg.error(player, "Bitte warte noch <white><s></white> Sekunden.", Msg.number("s", left));
            return false;
        }
        int cool = cooldownSeconds < 0 ? configCooldown() : cooldownSeconds;
        int warm = warmupSeconds < 0 ? configWarmup() : warmupSeconds;
        if (!warmupEnabled() || bypasses(player)) {
            warm = 0;
        }
        if (warm <= 0) {
            finish(player, target.clone(), key, message, cool);
            return true;
        }
        pending.put(player.getUniqueId(), new Pending(target.clone(), player.getLocation().clone(),
                key, message, warm * 20, cool));
        Msg.send(player, "<gray>Teleport in <white><s></white> Sekunden – bleib stehen.</gray>",
                Msg.number("s", warm));
        return true;
    }

    /** Läuft für diesen Spieler gerade eine Aufwärmzeit? */
    public boolean isPending(Player player) {
        return pending.containsKey(player.getUniqueId());
    }

    /**
     * Bricht einen laufenden Teleport ab.
     *
     * @param reason MiniMessage-Text für die Actionbar, {@code null} für keinen Hinweis
     * @return true, wenn tatsächlich etwas abgebrochen wurde
     */
    public boolean cancel(Player player, String reason) {
        if (pending.remove(player.getUniqueId()) == null) {
            return false;
        }
        if (reason != null && !reason.isBlank()) {
            plugin.clock().suppress(player, 2500L);
            player.sendActionBar(Msg.mm(reason));
        }
        return true;
    }

    /** Aufräumen beim Verlassen des Servers. */
    public void clear(Player player) {
        pending.remove(player.getUniqueId());
        cooldowns.remove(player.getUniqueId());
    }

    /** Vom Listener bei Schaden aufgerufen. */
    public void onDamage(Player player) {
        if (cancelOnDamage()) {
            cancel(player, "<red>Teleport abgebrochen – du hast Schaden genommen.</red>");
        }
    }

    /** Restliche Abklingzeit des Befehls in vollen Sekunden; 0 bedeutet frei. */
    public long cooldownLeft(Player player, String command) {
        if (bypasses(player)) {
            return 0L;
        }
        Map<String, Long> own = cooldowns.get(player.getUniqueId());
        if (own == null) {
            return 0L;
        }
        Long until = own.get(key(command));
        if (until == null) {
            return 0L;
        }
        long rest = until - System.currentTimeMillis();
        return rest <= 0L ? 0L : (rest + 999L) / 1000L;
    }

    /** Setzt die Abklingzeit eines Befehls von Hand (z. B. für eine misslungene Suche). */
    public void setCooldown(Player player, String command, int seconds) {
        if (seconds <= 0 || bypasses(player)) {
            return;
        }
        cooldowns.computeIfAbsent(player.getUniqueId(), k -> new HashMap<>())
                .put(key(command), System.currentTimeMillis() + seconds * 1000L);
    }

    private static String key(String command) {
        return command == null || command.isBlank() ? "teleport" : command.trim().toLowerCase(Locale.ROOT);
    }

    // ------------------------------------------------------------------ Ablauf

    private void tick() {
        if (pending.isEmpty()) {
            return;
        }
        List<Runnable> ready = new ArrayList<>();
        Iterator<Map.Entry<UUID, Pending>> it = pending.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<UUID, Pending> entry = it.next();
            Player player = Bukkit.getPlayer(entry.getKey());
            Pending job = entry.getValue();
            if (player == null || !player.isOnline()) {
                it.remove();
                continue;
            }
            if (moved(player, job.origin)) {
                it.remove();
                plugin.clock().suppress(player, 2500L);
                player.sendActionBar(Msg.mm("<red>Teleport abgebrochen – du hast dich bewegt.</red>"));
                continue;
            }
            job.leftTicks -= (int) STEP_TICKS;
            if (job.leftTicks > 0) {
                plugin.clock().suppress(player, 1500L);
                player.sendActionBar(bar(job));
                continue;
            }
            it.remove();
            ready.add(() -> finish(player, job.target, job.command, job.message, job.cooldownSeconds));
        }
        for (Runnable r : ready) {
            r.run();
        }
    }

    private static boolean moved(Player player, Location origin) {
        Location now = player.getLocation();
        World a = now.getWorld();
        World b = origin.getWorld();
        if (a == null || b == null || !a.equals(b)) {
            return true;
        }
        return now.distanceSquared(origin) > MOVE_LIMIT * MOVE_LIMIT;
    }

    private static Component bar(Pending job) {
        int done = (int) Math.round((double) (job.totalTicks - job.leftTicks) / job.totalTicks * BAR_WIDTH);
        done = clamp(done, 0, BAR_WIDTH);
        long seconds = (job.leftTicks + 19) / 20;
        StringBuilder sb = new StringBuilder(96);
        sb.append("<green>").append("▊".repeat(done)).append("</green>");
        sb.append("<dark_gray>").append("▊".repeat(BAR_WIDTH - done)).append("</dark_gray>");
        sb.append(" <gray>noch <white>").append(seconds).append("</white> s</gray>");
        return Msg.mm(sb.toString());
    }

    private void finish(Player player, Location target, String command, String message, int cooldownSeconds) {
        if (!player.isOnline()) {
            return;
        }
        if (target.getWorld() == null) {
            Msg.error(player, "Das Ziel liegt in einer Welt, die es nicht mehr gibt.");
            return;
        }
        player.teleportAsync(target, PlayerTeleportEvent.TeleportCause.COMMAND).thenAccept(ok -> {
            if (!Boolean.TRUE.equals(ok)) {
                Msg.error(player, "Teleport fehlgeschlagen.");
                return;
            }
            setCooldown(player, command, cooldownSeconds);
            if (message != null && !message.isBlank()) {
                Msg.send(player, message);
            }
            player.playSound(Sound.sound(org.bukkit.Sound.ENTITY_ENDERMAN_TELEPORT,
                    Sound.Source.PLAYER, 0.6F, 1.4F));
        });
    }

    // ------------------------------------------------------------------ Sichere Zielposition

    /**
     * Sucht an der gewünschten X/Z-Stelle die oberste Position, auf der man gefahrlos stehen kann:
     * fester Boden darunter, zwei freie Blöcke darüber, kein Lava, Feuer, Kaktus, Pulverschnee oder
     * Portal und weder Himmel noch Leere.
     *
     * @return sichere Position in Blockmitte oder {@code null}, wenn es dort keine gibt
     */
    public static Location safeTarget(Location wish) {
        if (wish == null) {
            return null;
        }
        World world = wish.getWorld();
        if (world == null) {
            return null;
        }
        int x = wish.getBlockX();
        int z = wish.getBlockZ();
        int min = world.getMinHeight() + 1;
        int max = world.getMaxHeight() - 2;
        int start;
        if (world.getEnvironment() == World.Environment.NETHER) {
            // Im Nether beginnt die Suche unter der Bedrock-Decke, sonst landet man oben darauf.
            start = Math.min(max, 120);
        } else {
            start = Math.min(max, world.getHighestBlockYAt(x, z, HeightMap.MOTION_BLOCKING) + 1);
        }
        for (int y = start; y >= min; y--) {
            if (standable(world, x, y, z)) {
                return new Location(world, x + 0.5D, y, z + 0.5D, wish.getYaw(), wish.getPitch());
            }
        }
        return null;
    }

    /**
     * Wie {@link #safeTarget(Location)}, sucht bei Misserfolg ringförmig im Umkreis weiter.
     *
     * @param radius Suchradius in Blöcken (0 = nur die eigene Säule)
     */
    public static Location safeTarget(Location wish, int radius) {
        Location direct = safeTarget(wish);
        if (direct != null || wish == null || wish.getWorld() == null || radius <= 0) {
            return direct;
        }
        World world = wish.getWorld();
        for (int r = 1; r <= radius; r++) {
            for (int dx = -r; dx <= r; dx++) {
                for (int dz = -r; dz <= r; dz++) {
                    if (Math.max(Math.abs(dx), Math.abs(dz)) != r) {
                        continue;
                    }
                    Location probe = new Location(world, wish.getX() + dx, wish.getY(), wish.getZ() + dz,
                            wish.getYaw(), wish.getPitch());
                    Location found = safeTarget(probe);
                    if (found != null) {
                        return found;
                    }
                }
            }
        }
        return null;
    }

    private static boolean standable(World world, int x, int y, int z) {
        if (y - 1 < world.getMinHeight() || y + 1 > world.getMaxHeight() - 1) {
            return false;
        }
        Block floor = world.getBlockAt(x, y - 1, z);
        if (!floor.isSolid() || DANGER.contains(floor.getType())) {
            return false;
        }
        return free(world.getBlockAt(x, y, z)) && free(world.getBlockAt(x, y + 1, z));
    }

    private static boolean free(Block block) {
        return block.isPassable() && !block.isLiquid() && !DANGER.contains(block.getType());
    }
}
