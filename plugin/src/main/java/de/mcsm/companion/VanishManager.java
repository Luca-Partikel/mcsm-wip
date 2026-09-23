package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataType;

/**
 * Verwaltet den unsichtbaren Zustand der Betreiber: nicht in der Tablist, für andere Spieler
 * unsichtbar, lautlos, unverwundbar, keine Kollision, kein Aufsammeln.
 *
 * <p>Weil jeder Wechsel der Sichtbarkeit zugleich ein Wechsel des Wartungsprofils ist, hängt
 * die Profilverwaltung ({@link AdminProfiles}) hier und ist über {@code plugin.vanish().profiles()}
 * erreichbar.</p>
 */
public final class VanishManager {

    private final CompanionPlugin plugin;
    /**
     * Nebenläufig sicher: der Server-List-Ping (PingListener) liest diese Menge aus einem
     * Netty-Thread, während der Haupt-Thread Einträge hinzufügt oder entfernt.
     */
    private final Set<UUID> vanished = ConcurrentHashMap.newKeySet();
    /** Merker in den Spielerdaten, damit beim nächsten Join alte Flags zurückgesetzt werden. */
    private final NamespacedKey key;
    /** Getrennte Spielerprofile für den unsichtbaren und den sichtbaren Betrieb. */
    private final AdminProfiles profiles;

    public VanishManager(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.key = new NamespacedKey(plugin, "vanished");
        this.profiles = new AdminProfiles(plugin);
    }

    /** Profilverwaltung des Wartungszugangs. */
    public AdminProfiles profiles() {
        return profiles;
    }

    public boolean isVanished(Player p) {
        return vanished.contains(p.getUniqueId());
    }

    public boolean isVanished(UUID id) {
        return vanished.contains(id);
    }

    /** Alle derzeit unsichtbaren Spieler, die online sind. */
    public List<Player> vanishedPlayers() {
        List<Player> out = new ArrayList<>();
        for (UUID id : vanished) {
            Player p = Bukkit.getPlayer(id);
            if (p != null && p.isOnline()) {
                out.add(p);
            }
        }
        return out;
    }

    /** Anzahl der Spieler, die normale Spieler tatsächlich sehen. */
    public int visibleOnline() {
        int n = 0;
        for (Player p : Bukkit.getOnlinePlayers()) {
            if (!isVanished(p)) {
                n++;
            }
        }
        return n;
    }

    /**
     * Macht den Spieler unsichtbar und schaltet dabei auf das Unsichtbar-Profil um – auch beim
     * stillen Beitritt, der diese Methode ebenfalls aufruft.
     */
    public void vanish(Player p) {
        vanished.add(p.getUniqueId());
        for (Player other : Bukkit.getOnlinePlayers()) {
            if (!other.equals(p)) {
                hideFrom(other, p);
            }
        }
        if (profiles.handles(p)) {
            profiles.switchTo(p, AdminProfileStore.SLOT_VANISH, null);
        } else {
            p.setGameMode(plugin.settings().adminVanishGamemode);
        }
        p.setSilent(true);
        p.setInvulnerable(true);
        p.setCollidable(false);
        p.setCanPickupItems(false);
        p.setSleepingIgnored(true);
        p.getPersistentDataContainer().set(key, PersistentDataType.BYTE, (byte) 1);
    }

    /** Macht den Spieler wieder sichtbar; der Spielmodus bleibt unverändert. */
    public void unvanish(Player p) {
        vanished.remove(p.getUniqueId());
        for (Player other : Bukkit.getOnlinePlayers()) {
            if (!other.equals(p)) {
                showTo(other, p);
            }
        }
        p.setSilent(false);
        // Ein per /admin god gesetzter Schutz und ein laufender AFK-Zustand gelten weiter –
        // sie hängen nicht an der Unsichtbarkeit.
        AdminTools tools = plugin.adminTools();
        p.setInvulnerable(tools != null && tools.hasGod(p));
        p.setCollidable(true);
        p.setCanPickupItems(true);
        p.setSleepingIgnored(plugin.afk() != null && plugin.afk().isAfk(p));
        p.getPersistentDataContainer().remove(key);
    }

    /**
     * /admin join: wieder sichtbar werden und auf das Normal-Profil wechseln. Der Spielmodus
     * wird dabei auf Überleben gesetzt, sofern admin.join_force_survival nicht abgeschaltet ist.
     *
     * @return true, wenn das Profil tatsächlich gewechselt wurde
     */
    public boolean joinNormal(Player p) {
        unvanish(p);
        GameMode forced = profiles.joinForcesSurvival() ? GameMode.SURVIVAL : null;
        return profiles.switchTo(p, AdminProfileStore.SLOT_NORMAL, forced);
    }

    /** Beim Verlassen des Servers nur den Merker im Speicher löschen. */
    public void forget(Player p) {
        vanished.remove(p.getUniqueId());
    }

    /** True, wenn der Spieler beim letzten Verlassen unsichtbar war (alte Flags in den Spielerdaten). */
    public boolean hasStaleState(Player p) {
        return p.getPersistentDataContainer().has(key, PersistentDataType.BYTE);
    }

    /**
     * Setzt hängen gebliebene Flags eines früher unsichtbaren Spielers zurück, der jetzt normal
     * joint (stiller Join deaktiviert, Plugin zwischenzeitlich fehlend).
     */
    public void repairStaleState(Player p) {
        if (!hasStaleState(p)) {
            return;
        }
        unvanish(p);
        if (Bukkit.getDefaultGameMode() == GameMode.SURVIVAL && p.getGameMode() == plugin.settings().adminVanishGamemode) {
            p.setGameMode(GameMode.SURVIVAL);
        }
        plugin.getLogger().info("Alte Unsichtbarkeits-Flags von " + p.getName() + " zurückgesetzt.");
    }

    /** Neu beigetretene Spieler dürfen bereits unsichtbare Betreiber nicht sehen. */
    public void hideVanishedFrom(Player newcomer) {
        for (Player v : vanishedPlayers()) {
            if (!v.equals(newcomer)) {
                hideFrom(newcomer, v);
            }
        }
    }

    private void hideFrom(Player viewer, Player target) {
        try {
            viewer.unlistPlayer(target);
        } catch (IllegalStateException ignored) {
            // bereits unsichtbar – dann ist er ohnehin nicht gelistet
        }
        viewer.hidePlayer(plugin, target);
    }

    private void showTo(Player viewer, Player target) {
        viewer.showPlayer(plugin, target);
        try {
            viewer.listPlayer(target);
        } catch (IllegalStateException ignored) {
            // wird beim nächsten Sichtbarmachen erneut versucht
        }
    }
}
