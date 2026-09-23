package de.mcsm.companion;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.UUID;
import java.util.logging.Level;

import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.entity.Player;

/**
 * Zwei getrennte Spielerprofile für den Wartungszugang: eines für den unsichtbaren Betrieb
 * und eines für das sichtbare Mitspielen nach /admin join.
 *
 * <p>Beim Wechsel wird immer zuerst der aktuelle Zustand in das bisher geltende Profil
 * gesichert und erst danach das Zielprofil angewendet. Dadurch geht nichts verloren und
 * nichts wird verdoppelt. Welches Profil gilt, steht dauerhaft in admin-profiles.yml,
 * damit auch ein Serverneustart die Zuordnung nicht durcheinanderbringt.</p>
 */
public final class AdminProfiles {

    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("dd.MM.yy HH:mm");

    private final CompanionPlugin plugin;
    private final AdminProfileStore store;

    public AdminProfiles(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.store = new AdminProfileStore(plugin);
        this.store.load();
    }

    public AdminProfileStore store() {
        return store;
    }

    /** Feature-Schalter features.admin_profiles (Standard: an). */
    public boolean enabled() {
        return plugin.settings().raw().getBoolean("features.admin_profiles", true);
    }

    /** Gilt die Profilverwaltung für diesen Spieler? Nur für den Wartungszugang. */
    public boolean handles(Player p) {
        return enabled() && !store.isBroken() && plugin.settings().isAdmin(p.getName());
    }

    /** Standard-Spielmodus, wenn das Unsichtbar-Profil noch nicht existiert. */
    public GameMode defaultVanishMode() {
        return Config.parseGameMode(plugin.settings().raw().getString("admin.vanish_gamemode"),
                plugin.settings().adminVanishGamemode);
    }

    /** Standard-Spielmodus, wenn das Normal-Profil noch nicht existiert. */
    public GameMode defaultNormalMode() {
        return Config.parseGameMode(plugin.settings().raw().getString("admin.normal_gamemode"), GameMode.SURVIVAL);
    }

    /** Soll /admin join immer auf Überleben schalten? */
    public boolean joinForcesSurvival() {
        return plugin.settings().raw().getBoolean("admin.join_force_survival", true);
    }

    private GameMode defaultMode(String slot) {
        return AdminProfileStore.SLOT_VANISH.equals(slot) ? defaultVanishMode() : defaultNormalMode();
    }

    /** Welches Profil gilt gerade? */
    public String active(Player p) {
        return store.activeSlot(p.getUniqueId());
    }

    /**
     * Wechselt auf das Zielprofil: aktuellen Zustand ins alte Profil sichern, Zielprofil anwenden.
     * Gilt bereits das Zielprofil, wird nur der gespeicherte Stand aufgefrischt.
     *
     * @param forced erzwungener Spielmodus (Überleben bei /admin join) oder null
     * @return true, wenn tatsächlich gewechselt wurde
     */
    public boolean switchTo(Player p, String target, GameMode forced) {
        String slot = AdminProfileStore.slot(target);
        if (!handles(p)) {
            if (forced != null) {
                p.setGameMode(forced);
            }
            return false;
        }
        UUID id = p.getUniqueId();
        String current = store.activeSlot(id);
        boolean changed = !current.equals(slot);
        if (changed) {
            // Erst den alten Stand auf die Platte bringen – danach darf das Inventar geleert werden.
            store.capture(p, current);
            store.save();
            try {
                store.apply(p, slot, defaultMode(slot), forced);
            } catch (RuntimeException ex) {
                plugin.getLogger().log(Level.WARNING,
                        "Wartungsprofil " + slot + " konnte nicht vollständig angewendet werden", ex);
                // Nach einem Fehlschlag nichts mehr schreiben: der halb angewendete Zustand würde
                // sonst das Zielprofil überschreiben. Stattdessen zurück auf das alte Profil.
                try {
                    store.apply(p, current, defaultMode(current), null);
                } catch (RuntimeException back) {
                    plugin.getLogger().log(Level.SEVERE,
                            "Auch das bisherige Wartungsprofil " + current + " ließ sich nicht wiederherstellen", back);
                }
                Msg.error(p, "Profilwechsel fehlgeschlagen – dein Profil "
                        + AdminProfileStore.slotName(current) + " bleibt aktiv.");
                return false;
            }
            store.setActiveSlot(id, slot);
        } else if (forced != null && p.getGameMode() != forced) {
            p.setGameMode(forced);
        }
        // Stand nach dem Wechsel festhalten, damit die Datei den echten Zustand zeigt.
        store.capture(p, slot);
        store.save();
        return changed;
    }

    /** Sichert den aktuellen Zustand in das gerade geltende Profil (Verlassen, Plugin-Ende). */
    public void captureActive(Player p) {
        if (!handles(p)) {
            return;
        }
        store.capture(p, store.activeSlot(p.getUniqueId()));
        store.save();
    }

    /** Alle online anwesenden Wartungszugänge sichern und die Datei schreiben. */
    public void shutdown() {
        for (Player p : Bukkit.getOnlinePlayers()) {
            if (handles(p)) {
                store.capture(p, store.activeSlot(p.getUniqueId()));
            }
        }
        store.save();
    }

    /** Profil-Übersicht für /admin profile. */
    public String describe(Player p, String slot) {
        UUID id = p.getUniqueId();
        String name = AdminProfileStore.slotName(slot);
        if (!store.exists(id, slot)) {
            return name + ": noch nicht angelegt";
        }
        long at = store.savedAt(id, slot);
        String when = at <= 0 ? "unbekannt" : STAMP.format(
                LocalDateTime.ofInstant(Instant.ofEpochMilli(at), ZoneId.systemDefault()));
        return name + ": " + store.storedItems(id, slot) + " Gegenstände, gesichert " + when;
    }
}
