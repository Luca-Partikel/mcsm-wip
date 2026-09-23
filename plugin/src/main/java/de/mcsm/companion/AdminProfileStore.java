package de.mcsm.companion;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.logging.Level;

import org.bukkit.GameMode;
import org.bukkit.attribute.Attribute;
import org.bukkit.attribute.AttributeInstance;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.InvalidConfigurationException;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.potion.PotionEffect;

/**
 * Speichert die beiden Wartungsprofile (unsichtbar / normal) in
 * plugins/MCSMCompanion/admin-profiles.yml.
 *
 * <p>Ein Profil enthält Inventar samt Rüstung und Zweithand, den gewählten Hotbar-Platz,
 * Erfahrung, Leben, Hunger, Sättigung, Spielmodus, Flugzustand und aktive Trank-Effekte.
 * Die Endertruhe gehört bewusst NICHT dazu – sie bleibt für beide Profile dieselbe.</p>
 */
public final class AdminProfileStore {

    /** Profil, das gilt, solange der Wartungszugang unsichtbar ist. */
    public static final String SLOT_VANISH = "vanish";
    /** Profil, das gilt, sobald der Wartungszugang über /admin join sichtbar mitspielt. */
    public static final String SLOT_NORMAL = "normal";

    /** Anzahl der Rüstungsplätze (Stiefel, Hose, Brust, Helm). */
    private static final int ARMOR_SLOTS = 4;

    private final CompanionPlugin plugin;
    private YamlConfiguration data = new YamlConfiguration();
    /** True, wenn die Datei beim Start unlesbar war – dann wird nichts mehr geschrieben. */
    private boolean broken;

    public AdminProfileStore(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    /** Gültiger Profilname; alles Unbekannte gilt als Normalprofil. */
    public static String slot(String raw) {
        return raw != null && SLOT_VANISH.equals(raw.toLowerCase(Locale.ROOT)) ? SLOT_VANISH : SLOT_NORMAL;
    }

    /** Deutscher Anzeigename eines Profils. */
    public static String slotName(String slot) {
        return SLOT_VANISH.equals(slot) ? "Unsichtbar" : "Normal";
    }

    public File file() {
        return new File(plugin.getDataFolder(), "admin-profiles.yml");
    }

    /**
     * True, wenn die Datei beschädigt war. Dann bleibt die Profilverwaltung für diesen Start
     * abgeschaltet, damit der leere Zustand nicht sofort über die gesicherten Profile geschrieben wird.
     */
    public boolean isBroken() {
        return broken;
    }

    /**
     * Liest die Datei. Anders als {@code YamlConfiguration.loadConfiguration} wird eine kaputte
     * Datei nicht als leer durchgewunken: sie wird beiseite gelegt und die Profilverwaltung
     * bis zum nächsten Start abgeschaltet.
     */
    public void load() {
        broken = false;
        data = new YamlConfiguration();
        File f = file();
        if (!f.isFile()) {
            return;
        }
        try {
            data.load(f);
        } catch (IOException | InvalidConfigurationException ex) {
            broken = true;
            data = new YamlConfiguration();
            File aside = new File(f.getParentFile(), f.getName() + ".broken-" + System.currentTimeMillis());
            boolean moved = f.renameTo(aside);
            plugin.getLogger().log(Level.SEVERE, "admin-profiles.yml ist beschädigt – die getrennten "
                    + "Wartungsprofile bleiben bis zum nächsten Start abgeschaltet, damit nichts überschrieben wird"
                    + (moved ? " (gesichert als " + aside.getName() + ")" : " (Umbenennen fehlgeschlagen)"), ex);
        }
    }

    /**
     * Schreibt die Datei atomar über admin-profiles.yml.tmp und Umbenennen – ein Absturz mitten
     * im Schreiben darf die gesicherten Inventare nicht zerstören. Fehler werden protokolliert.
     */
    public void save() {
        if (broken) {
            return;                                             // beschädigte Datei nicht überschreiben
        }
        File target = file();
        File dir = target.getParentFile();
        if (dir != null && !dir.isDirectory() && !dir.mkdirs()) {
            plugin.getLogger().warning("Plugin-Ordner konnte nicht angelegt werden: " + dir);
            return;
        }
        Path tmp = new File(dir, target.getName() + ".tmp").toPath();
        Path dst = target.toPath();
        try {
            Files.writeString(tmp, data.saveToString(), StandardCharsets.UTF_8);
            try {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (IOException atomicFailed) {
                Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException ex) {
            plugin.getLogger().log(Level.WARNING, "admin-profiles.yml konnte nicht gespeichert werden", ex);
        }
    }

    private static String path(UUID id, String slot) {
        return "profil." + id + "." + slot;
    }

    /** Welches Profil gilt für diesen Spieler gerade? Standard: Normalprofil. */
    public String activeSlot(UUID id) {
        return slot(data.getString("aktiv." + id, SLOT_NORMAL));
    }

    public void setActiveSlot(UUID id, String slot) {
        data.set("aktiv." + id, slot(slot));
    }

    /** True, wenn für diesen Spieler bereits ein Stand dieses Profils gespeichert ist. */
    public boolean exists(UUID id, String slot) {
        return data.isConfigurationSection(path(id, slot(slot)));
    }

    /** Zeitpunkt der letzten Sicherung in Millisekunden, 0 wenn unbekannt. */
    public long savedAt(UUID id, String slot) {
        return data.getLong(path(id, slot(slot)) + ".gespeichert", 0L);
    }

    /** Anzahl belegter Plätze im gespeicherten Inventar (nur zur Anzeige). */
    public int storedItems(UUID id, String slot) {
        ConfigurationSection sec = data.getConfigurationSection(path(id, slot(slot)) + ".inhalt");
        if (sec == null) {
            return 0;
        }
        int n = 0;
        for (String key : sec.getKeys(false)) {
            if (key.startsWith("s")) {
                n++;
            }
        }
        return n;
    }

    /** Sichert den aktuellen Zustand des Spielers in das angegebene Profil (ohne zu speichern). */
    public void capture(Player p, String slot) {
        String base = path(p.getUniqueId(), slot(slot));
        data.set(base, null);                                   // alten Stand vollständig ersetzen
        ConfigurationSection sec = data.createSection(base);
        PlayerInventory inv = p.getInventory();

        writeItems(sec.createSection("inhalt"), inv.getContents());
        writeItems(sec.createSection("ruestung"), inv.getArmorContents());
        ItemStack off = inv.getItemInOffHand();
        if (off != null && !off.isEmpty()) {
            sec.set("zweithand", off.clone());
        }
        sec.set("hotbar", inv.getHeldItemSlot());
        sec.set("level", p.getLevel());
        sec.set("exp", (double) p.getExp());
        sec.set("leben", p.getHealth());
        sec.set("hunger", p.getFoodLevel());
        sec.set("saettigung", (double) p.getSaturation());
        // Bei einem hardcore-toten Spieler ist der Zuschauermodus erzwungen. Der gehört nicht ins
        // Profil, sonst klebt er nach der Wiederbelebung an jedem weiteren Profilwechsel.
        if (plugin.hardcore() == null || !plugin.hardcore().isDead(p.getUniqueId())) {
            sec.set("spielmodus", p.getGameMode().name());
        }
        sec.set("flug_erlaubt", p.getAllowFlight());
        sec.set("fliegt", p.isFlying());
        sec.set("effekte", new ArrayList<PotionEffect>(p.getActivePotionEffects()));
        sec.set("gespeichert", System.currentTimeMillis());
    }

    /**
     * Wendet ein Profil vollständig auf den Spieler an. Fehlt der Eintrag, startet der Spieler
     * mit leerem Inventar im Standard-Spielmodus.
     *
     * @param fallbackMode Spielmodus, wenn das Profil fehlt oder keinen gespeichert hat
     * @param forcedMode   erzwungener Spielmodus (z. B. Überleben bei /admin join) oder null
     * @return true, wenn ein gespeicherter Stand vorlag
     */
    public boolean apply(Player p, String slot, GameMode fallbackMode, GameMode forcedMode) {
        ConfigurationSection sec = data.getConfigurationSection(path(p.getUniqueId(), slot(slot)));
        PlayerInventory inv = p.getInventory();

        // Erst restlos leeren, damit nichts aus dem alten Profil hängen bleibt.
        inv.clear();
        inv.setArmorContents(new ItemStack[ARMOR_SLOTS]);
        inv.setItemInOffHand(null);
        p.setItemOnCursor(null);
        for (PotionEffect e : new ArrayList<>(p.getActivePotionEffects())) {
            p.removePotionEffect(e.getType());
        }
        p.setFireTicks(0);
        p.setFreezeTicks(0);
        p.setFallDistance(0f);
        p.setRemainingAir(p.getMaximumAir());

        if (sec == null) {
            double max = maxHealth(p);
            GameMode mode = forcedMode != null ? forcedMode : fallbackMode;
            p.setLevel(0);
            p.setExp(0f);
            p.setHealth(max);
            p.setFoodLevel(20);
            p.setSaturation(5f);
            inv.setHeldItemSlot(0);
            setMode(p, mode);
            setFlight(p, mode, mode == GameMode.CREATIVE || mode == GameMode.SPECTATOR, false);
            p.updateInventory();
            return false;
        }

        int size = inv.getSize();
        ItemStack[] main = new ItemStack[size];
        ConfigurationSection items = sec.getConfigurationSection("inhalt");
        if (items != null) {
            int stored = Math.min(size, Math.max(0, items.getInt("groesse", size)));
            for (int i = 0; i < stored; i++) {
                main[i] = items.getItemStack("s" + i);
            }
        }
        inv.setContents(main);

        ItemStack[] armor = new ItemStack[ARMOR_SLOTS];
        ConfigurationSection worn = sec.getConfigurationSection("ruestung");
        if (worn != null) {
            for (int i = 0; i < ARMOR_SLOTS; i++) {
                armor[i] = worn.getItemStack("s" + i);
            }
        }
        inv.setArmorContents(armor);
        inv.setItemInOffHand(sec.getItemStack("zweithand"));
        inv.setHeldItemSlot(Math.max(0, Math.min(8, sec.getInt("hotbar", 0))));

        p.setLevel(Math.max(0, sec.getInt("level", 0)));
        p.setExp(clamp((float) sec.getDouble("exp", 0.0), 0f, 0.9999f));
        // Maximalleben erst jetzt lesen: die Rüstung des Profils kann es per Attribut verändern.
        double max = maxHealth(p);
        p.setHealth(Math.min(max, Math.max(Math.min(0.5, max), sec.getDouble("leben", max))));
        p.setFoodLevel(Math.max(0, Math.min(20, sec.getInt("hunger", 20))));
        p.setSaturation(clamp((float) sec.getDouble("saettigung", 5.0), 0f, 20f));

        GameMode stored = Config.parseGameMode(sec.getString("spielmodus"), fallbackMode);
        GameMode mode = forcedMode != null ? forcedMode : stored;
        setMode(p, mode);
        boolean allow = sec.getBoolean("flug_erlaubt", mode == GameMode.CREATIVE || mode == GameMode.SPECTATOR);
        if ((mode == GameMode.SURVIVAL || mode == GameMode.ADVENTURE)
                && (stored == GameMode.CREATIVE || stored == GameMode.SPECTATOR)) {
            // Flug aus dem Kreativmodus nicht in einen erzwungenen Überleben-Modus mitnehmen.
            allow = false;
        }
        setFlight(p, mode, allow, sec.getBoolean("fliegt", false));

        List<?> raw = sec.getList("effekte");
        if (raw != null) {
            for (Object o : raw) {
                if (o instanceof PotionEffect effect) {
                    p.addPotionEffect(effect);
                }
            }
        }
        p.updateInventory();
        return true;
    }

    /** Entfernt beide Profile eines Spielers (z. B. nach beschädigten Daten). */
    public void reset(UUID id) {
        data.set("profil." + id, null);
        data.set("aktiv." + id, null);
    }

    /** Maximales Leben laut Attribut; 20 als Rückfallwert. */
    public static double maxHealth(Player p) {
        AttributeInstance a = p.getAttribute(Attribute.MAX_HEALTH);
        return a == null ? 20.0 : a.getValue();
    }

    /**
     * Setzt den Spielmodus und meldet es, wenn ein anderes Modul den Wechsel abgelehnt hat
     * (z. B. die Hardcore-Zuschauersperre). Sonst bliebe die Ablehnung unbemerkt.
     */
    private static void setMode(Player p, GameMode mode) {
        p.setGameMode(mode);
        if (p.getGameMode() != mode) {
            Msg.send(p, "<gray>Spielmodus <white><m></white> wurde abgelehnt – du bleibst im Modus "
                            + "<white><a></white>.</gray>",
                    Msg.text("m", Config.gameModeName(mode)),
                    Msg.text("a", Config.gameModeName(p.getGameMode())));
        }
    }

    private static void setFlight(Player p, GameMode mode, boolean allow, boolean flying) {
        boolean wanted = allow || mode == GameMode.CREATIVE || mode == GameMode.SPECTATOR;
        try {
            p.setAllowFlight(wanted);
        } catch (IllegalArgumentException ignored) {
            // Im Kreativmodus lässt sich der Flug nicht abschalten – dann bleibt er eben an.
        }
        if (wanted && flying) {
            try {
                p.setFlying(true);
            } catch (IllegalArgumentException ignored) {
                // Flug im falschen Moment – wird beim nächsten Wechsel erneut versucht.
            }
        }
    }

    private static void writeItems(ConfigurationSection sec, ItemStack[] items) {
        sec.set("groesse", items.length);
        for (int i = 0; i < items.length; i++) {
            ItemStack s = items[i];
            if (s != null && !s.isEmpty()) {
                sec.set("s" + i, s.clone());
            }
        }
    }

    private static float clamp(float value, float min, float max) {
        if (Float.isNaN(value)) {
            return min;
        }
        return Math.max(min, Math.min(max, value));
    }
}
