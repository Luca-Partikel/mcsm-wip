package de.mcsm.companion;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataType;

/**
 * Verwaltet den unsichtbaren Zustand der Betreiber: nicht in der Tablist, für andere Spieler
 * unsichtbar, lautlos, unverwundbar, keine Kollision, kein Aufsammeln.
 */
public final class VanishManager {

    private final CompanionPlugin plugin;
    private final Set<UUID> vanished = new HashSet<>();
    /** Merker in den Spielerdaten, damit beim nächsten Join alte Flags zurückgesetzt werden. */
    private final NamespacedKey key;

    public VanishManager(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.key = new NamespacedKey(plugin, "vanished");
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

    public void vanish(Player p) {
        vanished.add(p.getUniqueId());
        for (Player other : Bukkit.getOnlinePlayers()) {
            if (!other.equals(p)) {
                hideFrom(other, p);
            }
        }
        p.setGameMode(plugin.settings().adminVanishGamemode);
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
        p.setInvulnerable(false);
        p.setCollidable(true);
        p.setCanPickupItems(true);
        p.setSleepingIgnored(false);
        p.getPersistentDataContainer().remove(key);
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
