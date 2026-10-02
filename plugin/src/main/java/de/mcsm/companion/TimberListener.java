package de.mcsm.companion;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ThreadLocalRandom;

import org.bukkit.GameMode;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.Registry;
import org.bukkit.block.Block;
import org.bukkit.block.data.type.Leaves;
import org.bukkit.enchantments.Enchantment;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.Damageable;
import org.bukkit.inventory.meta.ItemMeta;

/**
 * Holzfäller: Mit einer <b>Goldaxt</b> fällt ein zerschlagener Stamm den ganzen Baum auf einmal.
 *
 * <p>Der entscheidende Schutz, damit niemals ein Haus zerlegt wird, ist mehrstufig und bewusst
 * streng – im Zweifel wird <b>nicht</b> gefällt:</p>
 * <ol>
 *   <li>Werkzeug ist genau eine Goldaxt.</li>
 *   <li>Der Block ist ein <b>naturbelassener Stamm</b>: Name endet auf {@code _LOG} und beginnt
 *       nicht mit {@code STRIPPED_}. Entrindete Stämme, {@code _WOOD}/{@code _HYPHAE} (sechsseitige
 *       Rinde) und die Nether-{@code _STEM} fallen also raus – das sind so gut wie immer Bauten.</li>
 *   <li>Eingesammelt werden nur Stämme <b>derselben Holzart</b> (ein Baum ist einartig), mit einer
 *       Obergrenze.</li>
 *   <li><b>Das wichtigste Kennzeichen:</b> Am zusammenhängenden Stamm müssen echte Baumblätter
 *       derselben Art hängen, die <b>nicht persistent</b> sind. Von Spielern gesetzte Blätter sind
 *       immer {@code persistent=true}; nicht-persistente Blätter entstehen nur natürlich. Ein
 *       Holzhaus hat keine – es besteht den Check also nie.</li>
 * </ol>
 *
 * <p>Wer beim Schlagen <b>schleicht</b>, fällt nicht – dann bricht nur der eine Block. Abschaltbar
 * über {@code features.timber}; Grenzen über {@code timber.max_logs} und {@code timber.min_leaves}.</p>
 */
public final class TimberListener implements Listener {

    private final CompanionPlugin plugin;

    public TimberListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onBreak(BlockBreakEvent event) {
        if (!plugin.settings().raw().getBoolean("features.timber", true)) {
            return;
        }
        Player player = event.getPlayer();
        if (player.isSneaking() || !player.hasPermission("mcsm.timber")) {
            return;                               // Schleichen fällt nur den einen Block.
        }
        ItemStack axt = player.getInventory().getItemInMainHand();
        if (axt == null || axt.getType() != Material.GOLDEN_AXE) {
            return;
        }
        Block start = event.getBlock();
        if (!istNaturStamm(start.getType())) {
            return;
        }
        Material stamm = start.getType();
        Material blatt = passendesBlatt(stamm);
        if (blatt == null) {
            return;                               // Keine passende Blattart (z. B. Nether) – dann nicht fällen.
        }

        int maxLogs = clamp(plugin.settings().raw().getInt("timber.max_logs", 256), 8, 2048);
        int minBlatt = clamp(plugin.settings().raw().getInt("timber.min_leaves", 4), 1, 64);

        List<Block> staemme = sammleStamm(start, stamm, maxLogs);
        if (!hatGenugBlaetter(staemme, blatt, minBlatt)) {
            return;                               // Ohne echte Baumblätter: kein Baum -> Finger weg.
        }

        // Es ist ein Baum. Den ursprünglichen Block bricht das Ereignis selbst; alle weiteren
        // Stämme fällen wir hier. breakNaturally löst kein neues BlockBreakEvent aus – keine
        // Rückkopplung, keine Endlosschleife.
        for (Block b : staemme) {
            if (b.equals(start)) {
                continue;
            }
            if (b.getType() != stamm) {
                continue;                         // Zwischenzeitlich verändert – überspringen.
            }
            b.breakNaturally(axt);
            if (!nutzeAb(player, axt)) {
                break;                            // Axt zerbrochen: der Rest bleibt stehen.
            }
        }
    }

    /** Naturbelassener Stamm: endet auf _LOG, ist nicht entrindet. Kein _WOOD/_HYPHAE/_STEM. */
    private static boolean istNaturStamm(Material m) {
        String name = m.name();
        return name.endsWith("_LOG") && !name.startsWith("STRIPPED_");
    }

    /** Aus dem Stamm die passende Blattart ableiten (OAK_LOG -> OAK_LEAVES, DARK_OAK_LOG -> …). */
    private static Material passendesBlatt(Material stamm) {
        String name = stamm.name();
        String art = name.substring(0, name.length() - "_LOG".length());   // "OAK", "DARK_OAK", …
        return Material.getMaterial(art + "_LEAVES");
    }

    /**
     * Zusammenhängende Stämme derselben Art einsammeln (26er-Nachbarschaft, damit auch schräge
     * Äste erfasst werden), bis zur Obergrenze. Nur über {@code start} erreichbare Stämme.
     */
    private static List<Block> sammleStamm(Block start, Material stamm, int maxLogs) {
        List<Block> gefunden = new ArrayList<>();
        Set<Block> gesehen = new HashSet<>();
        ArrayDeque<Block> rand = new ArrayDeque<>();
        rand.add(start);
        gesehen.add(start);
        while (!rand.isEmpty() && gefunden.size() < maxLogs) {
            Block b = rand.poll();
            gefunden.add(b);
            for (int dx = -1; dx <= 1; dx++) {
                for (int dy = -1; dy <= 1; dy++) {
                    for (int dz = -1; dz <= 1; dz++) {
                        if (dx == 0 && dy == 0 && dz == 0) {
                            continue;
                        }
                        Block n = b.getRelative(dx, dy, dz);
                        if (n.getType() == stamm && gesehen.add(n)) {
                            rand.add(n);
                        }
                    }
                }
            }
        }
        return gefunden;
    }

    /** Hängen an den Stämmen genug nicht-persistente Blätter der passenden Art? (Haus-Schutz) */
    private static boolean hatGenugBlaetter(List<Block> staemme, Material blatt, int minBlatt) {
        Set<Block> gezaehlt = new HashSet<>();
        int treffer = 0;
        for (Block b : staemme) {
            for (int dx = -1; dx <= 1; dx++) {
                for (int dy = -1; dy <= 1; dy++) {
                    for (int dz = -1; dz <= 1; dz++) {
                        if (dx == 0 && dy == 0 && dz == 0) {
                            continue;
                        }
                        Block n = b.getRelative(dx, dy, dz);
                        if (n.getType() != blatt || !gezaehlt.add(n)) {
                            continue;
                        }
                        if (n.getBlockData() instanceof Leaves l && !l.isPersistent()) {
                            treffer++;
                            if (treffer >= minBlatt) {
                                return true;
                            }
                        }
                    }
                }
            }
        }
        return false;
    }

    /**
     * Die Axt um einen Punkt abnutzen (Haltbarkeit), Verzauberung „Haltbarkeit“ beachtet. Rückgabe
     * {@code false}, wenn die Axt dabei zerbricht. Im Kreativmodus passiert nichts.
     */
    @SuppressWarnings({"deprecation", "removal"})
    private boolean nutzeAb(Player player, ItemStack axt) {
        if (player.getGameMode() == GameMode.CREATIVE) {
            return true;
        }
        Enchantment haltbarkeit = Registry.ENCHANTMENT.get(NamespacedKey.minecraft("unbreaking"));
        int stufe = haltbarkeit == null ? 0 : axt.getEnchantmentLevel(haltbarkeit);
        if (stufe > 0 && ThreadLocalRandom.current().nextInt(stufe + 1) != 0) {
            return true;                          // Dieser Schlag kostet dank Haltbarkeit nichts.
        }
        ItemMeta meta = axt.getItemMeta();
        if (!(meta instanceof Damageable dmg)) {
            return true;
        }
        int neu = dmg.getDamage() + 1;
        if (neu >= axt.getType().getMaxDurability()) {
            player.getInventory().setItemInMainHand(null);
            player.getWorld().playSound(player.getLocation(), org.bukkit.Sound.ENTITY_ITEM_BREAK, 1.0f, 1.0f);
            return false;
        }
        dmg.setDamage(neu);
        axt.setItemMeta(meta);
        return true;
    }

    private static int clamp(int v, int min, int max) {
        return Math.max(min, Math.min(max, v));
    }
}
