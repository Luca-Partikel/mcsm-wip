package de.mcsm.companion;

import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import io.papermc.paper.datacomponent.item.ResolvableProfile;
import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.Chunk;
import org.bukkit.Color;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.World;
import org.bukkit.attribute.Attribute;
import org.bukkit.attribute.AttributeInstance;
import org.bukkit.block.Block;
import org.bukkit.block.BlockFace;
import org.bukkit.block.Skull;
import org.bukkit.block.data.BlockData;
import org.bukkit.block.data.Rotatable;
import org.bukkit.entity.Display;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Item;
import org.bukkit.entity.Player;
import org.bukkit.entity.TextDisplay;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.potion.PotionEffect;
import org.bukkit.potion.PotionEffectType;
import org.bukkit.scheduler.BukkitRunnable;

/**
 * MCSM-Hardcore (kein Vanilla-Hardcore): Wer im Überleben- oder Abenteuer-Modus stirbt, bekommt am
 * Todesort ein Grab (Spielerkopf + zwei Textzeilen) und bleibt als Zuschauer daran gebunden, bis
 * ein Mitspieler das Grab mit einem Totem der Unsterblichkeit anklickt oder eines davor ablegt.
 * Zustand in hardcore.yml ({@link GraveStore}), Ereignisse in {@link HardcoreListener}.
 */
public final class HardcoreManager {

    public static final String DEAD_HINT =
            "<red>Du bist tot – ein Mitspieler muss dich am Grab mit einem Totem wiederbeleben.</red>";
    public static final String DEATH_FORMAT =
            "<red>☠</red> <white><name></white> <gray>ist von uns gegangen</gray> "
            + "<dark_gray>(Koordinaten: X: <x> Y: <y> Z: <z>)</dark_gray>";
    public static final String REVIVE_FORMAT =
            "<green>♥</green> <white><name></white> <gray>wurde von</gray> <white><reviver></white> <gray>wiederbelebt!</gray>";
    public static final String REVIVE_SERVER_FORMAT =
            "<green>♥</green> <white><name></white> <gray>wurde wiederbelebt!</gray>";

    /** Sockel unter dem Kopf, wenn darunter kein fester Block liegt (Fall, Leere, Lava, Wasser). */
    static final Material BASE_BLOCK = Material.POLISHED_BLACKSTONE_BRICKS;
    /** Reichweite, in der ein abgelegtes Totem ein Grab wiederbelebt (Blöcke, ab Kopfmitte). */
    static final double TOTEM_RADIUS = 1.5;
    /** Wie lange ein abgelegtes Totem beobachtet wird (Ticks) und in welchem Abstand. */
    static final long TOTEM_TRACK_TICKS = 100L;
    static final long TOTEM_TRACK_PERIOD = 5L;

    private static final DateTimeFormatter DATE = DateTimeFormatter.ofPattern("dd.MM.yy - HH:mm");
    /** Kopf-Drehungen im Uhrzeigersinn ab Süden (Yaw 0), je 22,5 Grad. */
    private static final BlockFace[] ROTATIONS = {
        BlockFace.SOUTH, BlockFace.SOUTH_SOUTH_WEST, BlockFace.SOUTH_WEST, BlockFace.WEST_SOUTH_WEST,
        BlockFace.WEST, BlockFace.WEST_NORTH_WEST, BlockFace.NORTH_WEST, BlockFace.NORTH_NORTH_WEST,
        BlockFace.NORTH, BlockFace.NORTH_NORTH_EAST, BlockFace.NORTH_EAST, BlockFace.EAST_NORTH_EAST,
        BlockFace.EAST, BlockFace.EAST_SOUTH_EAST, BlockFace.SOUTH_EAST, BlockFace.SOUTH_SOUTH_EAST,
    };
    /** Suchreihenfolge für den Kopfblock: Todesort, bis 3 Blöcke höher, dann bis 3 Blöcke tiefer. */
    private static final int[] SEARCH_OFFSETS = {0, 1, 2, 3, -1, -2, -3};

    /** Blockposition als Schlüssel für Kopf- und Sockelblöcke. */
    record BlockPos(String world, int x, int y, int z) {
        static BlockPos of(Block b) {
            return new BlockPos(b.getWorld().getName(), b.getX(), b.getY(), b.getZ());
        }
    }

    private final CompanionPlugin plugin;
    private final GraveStore store;
    /** Markierung der Anzeige-Entities: mcsm:grave = UUID des Toten. */
    private final NamespacedKey graveKey = new NamespacedKey("mcsm", "grave");
    /** Geschützte Blöcke (Kopf und Sockel) mit dem zugehörigen Toten. */
    private final Map<BlockPos, UUID> blocks = new HashMap<>();
    /** True, während das Plugin selbst den Spielmodus eines Toten setzt (Sperre umgehen). */
    private boolean internalChange;

    public HardcoreManager(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.store = new GraveStore(plugin);
    }

    /** Beim Aktivieren: Zustand lesen, config.yml übernehmen, Anzeigen in geladenen Chunks prüfen. */
    public void load() {
        store.load();
        rebuildIndex();
        applyConfig();
        for (GraveStore.Grave g : store.graves()) {
            ensureDisplays(g);
        }
        store.save();
    }

    public void shutdown() {
        store.save();
    }

    /**
     * Übernimmt den Schlüssel "hardcore" aus config.yml, aber nur, wenn der Manager ihn seit dem
     * letzten Mal geändert hat; dazwischen gilt, was per /hardcore geschaltet wurde.
     */
    public void applyConfig() {
        boolean want = plugin.settings().hardcore;
        Boolean seen = store.configSeen();
        if (seen != null && seen.booleanValue() == want) {
            return;
        }
        store.setConfigSeen(want);
        if (want) {
            if (!store.isEnabled()) {
                store.setEnabled(true);
                plugin.getLogger().info("Hardcore-Modus laut config.yml aktiviert.");
            }
            store.save();
            writeStatus();
        } else if (store.isEnabled() || !store.isEmpty()) {
            int n = disable();
            plugin.getLogger().info("Hardcore-Modus laut config.yml deaktiviert, " + n + " Spieler wiederbelebt.");
        } else {
            store.save();
        }
    }

    public boolean isEnabled() {
        return store.isEnabled();
    }

    /** Schaltet ein; ohne Wirkung auf frühere Tode. Liefert false, wenn bereits aktiv. */
    public boolean enable() {
        if (store.isEnabled()) {
            return false;
        }
        store.setEnabled(true);
        store.save();
        writeStatus();
        return true;
    }

    /** Schaltet aus, belebt alle Toten wieder und entfernt alle Gräber. Liefert die Zahl der Wiederbelebten. */
    public int disable() {
        store.setEnabled(false);
        int n = 0;
        for (GraveStore.Grave g : new ArrayList<>(store.graves())) {
            revive(g, null);
            n++;
        }
        store.save();
        writeStatus();
        return n;
    }

    public boolean isDead(UUID player) {
        return store.contains(player);
    }

    public GraveStore.Grave grave(UUID player) {
        return store.get(player);
    }

    public Collection<GraveStore.Grave> graves() {
        return store.graves();
    }

    public List<String> deadNames() {
        List<String> out = new ArrayList<>();
        for (GraveStore.Grave g : store.graves()) {
            out.add(g.name);
        }
        return out;
    }

    public boolean isInternalChange() {
        return internalChange;
    }

    // ---- Tod und Grab -------------------------------------------------------------------------

    /** Zählt dieser Tod? Modus aktiv, Überleben/Abenteuer, kein unsichtbarer Betreiber, nicht schon tot. */
    public boolean appliesTo(Player victim) {
        if (!store.isEnabled() || isDead(victim.getUniqueId()) || plugin.vanish().isVanished(victim)) {
            return false;
        }
        GameMode gm = victim.getGameMode();
        return gm == GameMode.SURVIVAL || gm == GameMode.ADVENTURE;
    }

    /**
     * Legt das Grab am Todesort an, merkt den Spieler als tot, lässt ihn im nächsten Tick ohne
     * Todesbildschirm wiedererscheinen (PlayerRespawnEvent setzt Grab und Zuschauer) und liefert
     * die Meldung für alle.
     */
    public Component handleDeath(Player victim) {
        Location death = victim.getLocation();
        GraveStore.Grave g = new GraveStore.Grave(victim.getUniqueId());
        g.name = victim.getName();
        g.world = death.getWorld().getName();
        g.x = death.getX();
        g.y = death.getY();
        g.z = death.getZ();
        g.time = System.currentTimeMillis() / 1000L;
        placeGrave(g, victim, death);
        store.put(g);
        index(g);
        store.save();
        writeStatus();
        Bukkit.getScheduler().runTask(plugin, () -> {
            if (victim.isOnline() && victim.isDead()) {
                victim.spigot().respawn();
            }
        });
        return Msg.mm(DEATH_FORMAT, Msg.name("name", victim),
                Msg.number("x", death.getBlockX()), Msg.number("y", death.getBlockY()), Msg.number("z", death.getBlockZ()));
    }

    private void placeGrave(GraveStore.Grave g, Player victim, Location death) {
        World w = death.getWorld();
        int bx = death.getBlockX();
        int bz = death.getBlockZ();
        int minY = w.getMinHeight() + 1;
        int maxY = w.getMaxHeight() - 1;
        int by = Math.max(minY, Math.min(maxY, death.getBlockY()));
        Block head = null;
        boolean needBase = false;
        // 1. Ersetzbarer Block (Luft, Wasser, Gras, Schneeschicht usw.) mit festem Block darunter.
        for (int off : SEARCH_OFFSETS) {
            int y = by + off;
            if (y < minY || y > maxY) {
                continue;
            }
            Block b = w.getBlockAt(bx, y, bz);
            if (b.isReplaceable() && b.getRelative(BlockFace.DOWN).getType().isSolid()) {
                head = b;
                break;
            }
        }
        // 2. Sonst (Fall, Leere, Lava, offenes Wasser): erster ersetzbarer Block, darunter ein Sockel.
        if (head == null) {
            for (int off : SEARCH_OFFSETS) {
                int y = by + off;
                if (y < minY || y > maxY) {
                    continue;
                }
                Block b = w.getBlockAt(bx, y, bz);
                if (b.isReplaceable()) {
                    head = b;
                    needBase = true;
                    break;
                }
            }
        }
        // 3. Notfall (z. B. Tod im Fels): Block direkt am Todesort ersetzen.
        if (head == null) {
            head = w.getBlockAt(bx, by, bz);
            needBase = !head.getRelative(BlockFace.DOWN).getType().isSolid();
        }
        g.headWorld = w.getName();
        g.headX = head.getX();
        g.headY = head.getY();
        g.headZ = head.getZ();
        g.replacedBlock = head.getType().name();
        if (needBase) {
            Block base = head.getRelative(BlockFace.DOWN);
            g.base = true;
            g.baseReplaced = base.getType().name();
            base.setType(BASE_BLOCK, false);
        }
        head.setType(Material.PLAYER_HEAD, false);
        BlockData data = head.getBlockData();
        if (data instanceof Rotatable rot) {
            // Wie beim Platzieren von Hand: das Gesicht schaut dorthin, woher der Spieler blickte.
            rot.setRotation(ROTATIONS[Math.floorMod(Math.round((death.getYaw() + 180f) / 22.5f), ROTATIONS.length)]);
            head.setBlockData(data, false);
        }
        if (head.getState() instanceof Skull skull) {
            skull.setProfile(ResolvableProfile.resolvableProfile(victim.getPlayerProfile()));
            skull.update(true, false);
        }
        spawnDisplays(g);
    }

    private void spawnDisplays(GraveStore.Grave g) {
        World w = Bukkit.getWorld(g.headWorld);
        if (w == null) {
            return;
        }
        double cx = g.headX + 0.5;
        double cz = g.headZ + 0.5;
        String date = DATE.format(Instant.ofEpochSecond(g.time).atZone(ZoneId.systemDefault()));
        TextDisplay line1 = spawnLine(w, new Location(w, cx, g.headY + 1.15, cz), g.victim,
                Msg.mm("<white><bold>R.I.P <name></bold></white>", Msg.text("name", g.name)));
        TextDisplay line2 = spawnLine(w, new Location(w, cx, g.headY + 0.85, cz), g.victim,
                Msg.mm("<gray><date></gray>", Msg.text("date", date)));
        g.displays.clear();
        g.displays.add(line1.getUniqueId());
        g.displays.add(line2.getUniqueId());
    }

    private TextDisplay spawnLine(World w, Location at, UUID victim, Component text) {
        return w.spawn(at, TextDisplay.class, d -> {
            d.text(text);
            d.setBillboard(Display.Billboard.CENTER);
            d.setBackgroundColor(Color.fromARGB(96, 0, 0, 0));
            d.setSeeThrough(false);
            d.setShadowed(true);
            d.setPersistent(true);
            d.setInvulnerable(true);
            d.getPersistentDataContainer().set(graveKey, PersistentDataType.STRING, victim.toString());
        });
    }

    /** Prüft die beiden Anzeigen eines Grabes (nur bei geladenem Chunk) und erzeugt sie bei Bedarf neu. */
    public void ensureDisplays(GraveStore.Grave g) {
        ensureDisplays(g, Collections.emptySet());
    }

    /**
     * @param known UUIDs von Entities, die gerade erst geladen wurden (EntitiesLoadEvent) und über
     *              Bukkit.getEntity eventuell noch nicht auffindbar sind
     */
    private void ensureDisplays(GraveStore.Grave g, Set<UUID> known) {
        if (g.pendingRevive) {
            return;                                    // Grab ist bereits aufgelöst, nur der Beitritt steht aus
        }
        World w = Bukkit.getWorld(g.headWorld);
        int cx = g.headX >> 4;
        int cz = g.headZ >> 4;
        if (w == null || !w.isChunkLoaded(cx, cz) || !w.getChunkAt(cx, cz).isEntitiesLoaded()) {
            return;                                    // Entities kommen später (EntitiesLoadEvent)
        }
        int ok = 0;
        for (UUID id : g.displays) {
            Entity e = Bukkit.getEntity(id);
            if (known.contains(id) || (e instanceof TextDisplay && e.isValid())) {
                ok++;
            }
        }
        if (ok == 2 && g.displays.size() == 2) {
            return;
        }
        removeDisplays(g, w);
        spawnDisplays(g);
        store.save();
        plugin.getLogger().info("Grab-Anzeige von " + g.name + " neu erzeugt.");
    }

    /**
     * Nach dem Laden der Entities eines Chunks: Anzeigen der Gräber darin prüfen und verwaiste
     * Anzeigen entfernen (Grab inzwischen aufgelöst, während der Chunk nicht geladen war).
     */
    public void checkChunk(Chunk chunk, List<Entity> loaded) {
        Set<UUID> known = new HashSet<>();
        for (Entity e : loaded) {
            known.add(e.getUniqueId());
        }
        String world = chunk.getWorld().getName();
        for (GraveStore.Grave g : store.graves()) {
            if (world.equals(g.headWorld) && (g.headX >> 4) == chunk.getX() && (g.headZ >> 4) == chunk.getZ()) {
                ensureDisplays(g, known);
            }
        }
        for (Entity e : loaded) {
            if (!isGraveDisplay(e)) {
                continue;
            }
            GraveStore.Grave g = ownerGrave(e.getPersistentDataContainer().get(graveKey, PersistentDataType.STRING));
            if (g == null || !g.displays.contains(e.getUniqueId())) {
                e.remove();
            }
        }
    }

    private GraveStore.Grave ownerGrave(String uuid) {
        if (uuid == null) {
            return null;
        }
        try {
            return store.get(UUID.fromString(uuid));
        } catch (IllegalArgumentException ex) {
            return null;
        }
    }

    /** Anzeigen aller Gräber dieser Welt prüfen (WorldLoadEvent). */
    public void checkWorld(World world) {
        for (GraveStore.Grave g : store.graves()) {
            if (world.getName().equals(g.headWorld)) {
                ensureDisplays(g);
            }
        }
    }

    private void removeDisplays(GraveStore.Grave g, World w) {
        for (UUID id : g.displays) {
            Entity e = Bukkit.getEntity(id);
            if (e != null) {
                e.remove();
            }
        }
        g.displays.clear();
        // Verwaiste Anzeigen desselben Grabes in der Nähe (z. B. nach Absturz ohne gespeicherte UUIDs).
        Location c = new Location(w, g.headX + 0.5, g.headY + 1.0, g.headZ + 0.5);
        String victim = g.victim.toString();
        for (Entity e : w.getNearbyEntities(c, 2, 3, 2)) {
            if (e instanceof TextDisplay
                    && victim.equals(e.getPersistentDataContainer().get(graveKey, PersistentDataType.STRING))) {
                e.remove();
            }
        }
    }

    private void removeGrave(GraveStore.Grave g) {
        unindex(g);
        World w = Bukkit.getWorld(g.headWorld);
        if (w == null) {
            return;
        }
        // Chunk laden, damit Kopf und Anzeigen auch ohne Spieler in der Nähe verschwinden.
        w.getChunkAt(g.headX >> 4, g.headZ >> 4).load();
        Block head = w.getBlockAt(g.headX, g.headY, g.headZ);
        if (head.getType() == Material.PLAYER_HEAD) {
            // Lava und Feuer kommen nicht zurück, sonst stürbe der Wiederbelebte sofort erneut; der Sockel bleibt als Standfläche.
            Material back = material(g.replacedBlock);
            if (back == Material.LAVA || back == Material.FIRE || back == Material.SOUL_FIRE) {
                back = Material.AIR;
            }
            head.setType(back, true);
        }
        removeDisplays(g, w);
    }

    private static Material material(String name) {
        Material m = name == null ? null : Material.matchMaterial(name);
        return m != null && m.isBlock() ? m : Material.AIR;
    }

    // ---- Zuschauer-Sperre ---------------------------------------------------------------------

    /** Wiedererscheinungspunkt: einen Block über dem Kopf, mittig. Null, wenn die Welt fehlt. */
    public Location respawnLocation(GraveStore.Grave g) {
        World w = Bukkit.getWorld(g.headWorld);
        return w == null ? null : new Location(w, g.headX + 0.5, g.headY + 1.0, g.headZ + 0.5);
    }

    /**
     * Standort nach der Wiederbelebung: erster Platz ab der ehemaligen Kopfposition mit zwei freien
     * Blöcken (kein Fels, keine Flüssigkeit). Wird erst nach removeGrave aufgerufen, sieht also den
     * wiederhergestellten Block.
     */
    private Location reviveLocation(GraveStore.Grave g) {
        World w = Bukkit.getWorld(g.headWorld);
        if (w == null) {
            return null;
        }
        int top = Math.min(g.headY + 8, w.getMaxHeight() - 2);
        for (int y = g.headY; y <= top; y++) {
            Block feet = w.getBlockAt(g.headX, y, g.headZ);
            if (feet.isPassable() && !feet.isLiquid() && feet.getRelative(BlockFace.UP).isPassable()) {
                return new Location(w, g.headX + 0.5, y, g.headZ + 0.5);
            }
        }
        return new Location(w, g.headX + 0.5, g.headY + 1.0, g.headZ + 0.5);
    }

    public void lockSpectator(Player p) {
        if (p.getGameMode() != GameMode.SPECTATOR) {
            setModeInternal(p, GameMode.SPECTATOR);
        }
    }

    private void setModeInternal(Player p, GameMode mode) {
        internalChange = true;
        try {
            p.setGameMode(mode);
        } finally {
            internalChange = false;
        }
    }

    /** Beim Beitritt eines Toten: ausstehende Wiederbelebung anwenden, sonst Zuschauer am Grab. */
    public void handleJoin(Player p) {
        GraveStore.Grave g = store.get(p.getUniqueId());
        if (g == null) {
            return;
        }
        if (g.pendingRevive) {
            applyRevive(p, g, true);
            return;
        }
        Location at = respawnLocation(g);
        if (at != null) {
            p.teleport(at);
        }
        lockSpectator(p);
        Msg.send(p, DEAD_HINT);
    }

    // ---- Wiederbelebung -----------------------------------------------------------------------

    /** Belebt den Spieler wieder; reviver null = Server (/hardcore off). False, wenn er nicht tot ist. */
    public boolean revive(UUID victim, String reviver) {
        GraveStore.Grave g = store.get(victim);
        return g != null && revive(g, reviver);
    }

    /** True, wenn das Grab aufgelöst wurde (sofort oder ausstehend); false, wenn es schon aufgelöst war. */
    private boolean revive(GraveStore.Grave g, String reviver) {
        if (g.pendingRevive) {
            return false;                              // Grab ist schon weg, Spieler offline
        }
        removeGrave(g);
        g.reviver = reviver;
        Player p = Bukkit.getPlayer(g.victim);
        if (p != null && p.isOnline()) {
            applyRevive(p, g, false);
            return true;
        }
        g.pendingRevive = true;
        store.save();
        writeStatus();
        Bukkit.broadcast(reviveText(g).append(Msg.mm(" <dark_gray>(offline – wirksam beim nächsten Beitritt)</dark_gray>")));
        return true;
    }

    private void applyRevive(Player p, GraveStore.Grave g, boolean onJoin) {
        World w = Bukkit.getWorld(g.headWorld);
        if (w != null && !g.displays.isEmpty()) {
            removeDisplays(g, w);                      // Vorsichtshalber, falls das Grab noch Anzeigen hat
        }
        unindex(g);
        store.remove(g.victim);
        Location at = reviveLocation(g);
        if (at != null) {
            at.setYaw(p.getLocation().getYaw());
            p.teleport(at);
        }
        setModeInternal(p, GameMode.SURVIVAL);
        AttributeInstance max = p.getAttribute(Attribute.MAX_HEALTH);
        p.setHealth(max == null ? 20.0 : max.getValue());
        p.setFoodLevel(20);
        p.setSaturation(5f);
        p.setFireTicks(0);
        p.setFallDistance(0f);
        p.setRemainingAir(p.getMaximumAir());
        Material back = material(g.replacedBlock);
        if (g.base || back == Material.LAVA || back == Material.WATER) {
            // Lava/Wasser fließt in die alte Kopfposition zurück: kurzer Schutz, bis der Spieler weg ist.
            p.addPotionEffect(new PotionEffect(PotionEffectType.FIRE_RESISTANCE, 15 * 20, 0));
            p.addPotionEffect(new PotionEffect(PotionEffectType.WATER_BREATHING, 15 * 20, 0));
        }
        store.save();
        writeStatus();
        if (onJoin) {
            if (g.reviver == null) {
                Msg.send(p, "<green>Du wurdest wiederbelebt – der Hardcore-Modus wurde beendet.</green>");
            } else {
                Msg.send(p, "<green>Du wurdest von <white><reviver></white> wiederbelebt!</green>", Msg.text("reviver", g.reviver));
            }
        } else {
            Bukkit.broadcast(reviveText(g));
        }
    }

    private static Component reviveText(GraveStore.Grave g) {
        if (g.reviver == null) {
            return Msg.mm(REVIVE_SERVER_FORMAT, Msg.text("name", g.name));
        }
        return Msg.mm(REVIVE_FORMAT, Msg.text("name", g.name), Msg.text("reviver", g.reviver));
    }

    /**
     * Rechtsklick auf einen Grabkopf mit Totem in Haupt- oder Nebenhand: Totem verbrauchen und
     * wiederbeleben. True, wenn eine Wiederbelebung ausgelöst wurde (Ereignis abbrechen).
     */
    public boolean reviveByClick(Player reviver, Block clicked) {
        GraveStore.Grave g = graveAtHead(clicked);
        if (g == null || isDead(reviver.getUniqueId())) {
            return false;
        }
        PlayerInventory inv = reviver.getInventory();
        ItemStack main = inv.getItemInMainHand();
        ItemStack off = inv.getItemInOffHand();
        boolean useMain = main.getType() == Material.TOTEM_OF_UNDYING;
        if (!useMain && off.getType() != Material.TOTEM_OF_UNDYING) {
            return false;
        }
        ItemStack totem = useMain ? main : off;
        if (!revive(g, reviver.getName())) {
            return false;
        }
        // Genau ein Totem je erfolgreicher Wiederbelebung.
        if (reviver.getGameMode() != GameMode.CREATIVE) {
            ItemStack rest = null;
            if (totem.getAmount() > 1) {
                rest = totem.clone();
                rest.setAmount(totem.getAmount() - 1);
            }
            if (useMain) {
                inv.setItemInMainHand(rest);
            } else {
                inv.setItemInOffHand(rest);
            }
        }
        return true;
    }

    /** Abgelegtes Totem bis zu 5 s beobachten: landet es neben einem Grabkopf, wird der Tote wiederbelebt. */
    public void trackTotem(Player dropper, Item item) {
        if (store.isEmpty() || isDead(dropper.getUniqueId())) {
            return;
        }
        new TotemTracker(dropper.getUniqueId(), dropper.getName(), item)
                .runTaskTimer(plugin, TOTEM_TRACK_PERIOD, TOTEM_TRACK_PERIOD);
    }

    private final class TotemTracker extends BukkitRunnable {
        private final UUID dropper;
        private final String name;
        private final Item item;
        private long ticks;

        TotemTracker(UUID dropper, String name, Item item) {
            this.dropper = dropper;
            this.name = name;
            this.item = item;
        }

        @Override
        public void run() {
            ticks += TOTEM_TRACK_PERIOD;
            if (!item.isValid() || ticks > TOTEM_TRACK_TICKS || item.getItemStack().getType() != Material.TOTEM_OF_UNDYING) {
                cancel();
                return;
            }
            GraveStore.Grave g = graveNear(item.getLocation(), TOTEM_RADIUS);
            if (g == null) {
                return;
            }
            cancel();
            if (isDead(dropper) || !revive(g, name)) {
                return;                                // Totem bleibt liegen
            }
            ItemStack stack = item.getItemStack();
            if (stack.getAmount() > 1) {
                stack.setAmount(stack.getAmount() - 1);
                item.setItemStack(stack);
            } else {
                item.remove();
            }
        }
    }

    private GraveStore.Grave graveNear(Location loc, double radius) {
        World w = loc.getWorld();
        if (w == null) {
            return null;
        }
        double r2 = radius * radius;
        for (GraveStore.Grave g : store.graves()) {
            if (g.pendingRevive || !w.getName().equals(g.headWorld)) {
                continue;
            }
            double dx = loc.getX() - (g.headX + 0.5);
            double dy = loc.getY() - (g.headY + 0.5);
            double dz = loc.getZ() - (g.headZ + 0.5);
            if (dx * dx + dy * dy + dz * dz <= r2) {
                return g;
            }
        }
        return null;
    }

    // ---- Schutz / Nachschlagen ----------------------------------------------------------------

    private void rebuildIndex() {
        blocks.clear();
        for (GraveStore.Grave g : store.graves()) {
            index(g);
        }
    }

    private void index(GraveStore.Grave g) {
        if (g.pendingRevive) {
            return;                                    // Blöcke sind schon wiederhergestellt
        }
        blocks.put(new BlockPos(g.headWorld, g.headX, g.headY, g.headZ), g.victim);
        if (g.base) {
            blocks.put(new BlockPos(g.headWorld, g.headX, g.headY - 1, g.headZ), g.victim);
        }
    }

    private void unindex(GraveStore.Grave g) {
        blocks.remove(new BlockPos(g.headWorld, g.headX, g.headY, g.headZ));
        blocks.remove(new BlockPos(g.headWorld, g.headX, g.headY - 1, g.headZ));
    }

    /** Ist der Block Kopf oder Sockel eines Grabes? */
    public boolean isGraveBlock(Block b) {
        return !blocks.isEmpty() && blocks.containsKey(BlockPos.of(b));
    }

    public GraveStore.Grave graveAt(Block b) {
        if (blocks.isEmpty()) {
            return null;
        }
        UUID id = blocks.get(BlockPos.of(b));
        return id == null ? null : store.get(id);
    }

    /** Nur der Kopfblock (nicht der Sockel). */
    public GraveStore.Grave graveAtHead(Block b) {
        GraveStore.Grave g = graveAt(b);
        return g != null && g.headY == b.getY() && b.getType() == Material.PLAYER_HEAD ? g : null;
    }

    /** Berührt ein Kolben (Schieben oder Ziehen in Richtung dir) einen Grabblock? */
    public boolean pistonTouchesGrave(Block piston, BlockFace dir, List<Block> moved) {
        if (blocks.isEmpty()) {
            return false;
        }
        if (isGraveBlock(piston.getRelative(dir))) {
            return true;
        }
        for (Block b : moved) {
            if (isGraveBlock(b) || isGraveBlock(b.getRelative(dir))) {
                return true;
            }
        }
        return false;
    }

    public boolean isGraveDisplay(Entity e) {
        return e instanceof TextDisplay && e.getPersistentDataContainer().has(graveKey, PersistentDataType.STRING);
    }

    /** Zeile für /hardcore status. */
    public static String describe(GraveStore.Grave g) {
        String when = DATE.format(Instant.ofEpochSecond(g.time).atZone(ZoneId.systemDefault()));
        String state = g.pendingRevive ? ", Wiederbelebung ausstehend" : "";
        return g.name + " (" + g.headWorld + " " + g.headX + " " + g.headY + " " + g.headZ + ", " + when + state + ")";
    }

    private void writeStatus() {
        StatusWriter s = plugin.status();
        if (s != null) {
            s.write();
        }
    }
}
