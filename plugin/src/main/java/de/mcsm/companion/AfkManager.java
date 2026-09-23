package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;

/**
 * Verwaltet den AFK-Zustand: manuell über /afk, automatisch nach Untätigkeit. AFK-Spieler werden
 * beim Schlafen übersprungen und bekommen einen Zusatz im Tablist-Namen. Unsichtbare Betreiber
 * bleiben dabei außen vor, damit sie nicht doch in der Tablist auftauchen.
 */
public final class AfkManager implements Runnable {

    private static final String DEFAULT_SUFFIX = " <dark_gray>[</dark_gray><yellow>AFK</yellow><dark_gray>]</dark_gray>";
    private static final String DEFAULT_AFK = "<gray><name> ist jetzt AFK<reason>.</gray>";
    private static final String DEFAULT_BACK = "<gray><name> ist wieder da.</gray>";

    private final CompanionPlugin plugin;
    /** Letzte erkannte Aktivität je Spieler (Zeitstempel in Millisekunden). */
    private final Map<UUID, Long> lastActivity = new ConcurrentHashMap<>();
    /** AFK-Spieler mit Grund; leerer Text = kein Grund angegeben. */
    private final Map<UUID, String> afk = new ConcurrentHashMap<>();
    /** Seit wann der jeweilige Spieler AFK ist. */
    private final Map<UUID, Long> afkSince = new ConcurrentHashMap<>();

    public AfkManager(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    private YamlConfiguration raw() {
        return plugin.settings().raw();
    }

    public boolean enabled() {
        return raw().getBoolean("features.afk", true);
    }

    /** Untätigkeit in Millisekunden bis zum automatischen AFK; 0 oder weniger schaltet das ab. */
    private long timeoutMillis() {
        return Math.max(0L, raw().getInt("afk.timeout_seconds", 300)) * 1000L;
    }

    public boolean isAfk(UUID id) {
        return afk.containsKey(id);
    }

    public boolean isAfk(Player p) {
        return afk.containsKey(p.getUniqueId());
    }

    /** Angegebener Grund oder leerer Text. */
    public String reason(UUID id) {
        String r = afk.get(id);
        return r == null ? "" : r;
    }

    /** Wie lange der Spieler schon AFK ist, in Millisekunden; 0, wenn er es nicht ist. */
    public long afkMillis(UUID id) {
        Long since = afkSince.get(id);
        return since == null ? 0L : Math.max(0L, System.currentTimeMillis() - since);
    }

    /** Alle AFK-Spieler, die der Absender sehen darf. */
    public List<Player> afkPlayers(org.bukkit.command.CommandSender viewer) {
        List<Player> out = new ArrayList<>();
        for (Player p : plugin.visiblePlayers(viewer)) {
            if (isAfk(p)) {
                out.add(p);
            }
        }
        return out;
    }

    /** Merkt sich den Zeitpunkt, ohne einen bestehenden AFK-Zustand zu beenden (z. B. beim Join). */
    public void touch(Player p) {
        lastActivity.put(p.getUniqueId(), System.currentTimeMillis());
    }

    /** Aktivität gemeldet: Zeit merken und einen bestehenden AFK-Zustand beenden. */
    public void activity(Player p) {
        lastActivity.put(p.getUniqueId(), System.currentTimeMillis());
        if (isAfk(p)) {
            setAfk(p, false, "");
        }
    }

    public void forget(UUID id) {
        lastActivity.remove(id);
        afk.remove(id);
        afkSince.remove(id);
    }

    /** Ändert den Grund eines bereits AFK gemeldeten Spielers, ohne neue Broadcast-Meldung. */
    public void updateReason(Player p, String reason) {
        UUID id = p.getUniqueId();
        if (!isAfk(id)) {
            return;
        }
        String r = reason == null ? "" : reason.trim();
        afk.put(id, r);
        Msg.send(p, r.isEmpty()
                        ? "<gray>AFK-Grund entfernt.</gray>"
                        : "<gray>AFK-Grund geändert: <white><reason></white></gray>",
                Msg.text("reason", r));
    }

    /** Schaltet den AFK-Zustand um und liefert den neuen Wert. */
    public boolean toggle(Player p, String reason) {
        boolean next = !isAfk(p);
        setAfk(p, next, reason);
        return next;
    }

    /**
     * Setzt den AFK-Zustand. Meldungen und Tablist-Zusatz entfallen für unsichtbare Betreiber,
     * damit deren Anwesenheit nicht verraten wird.
     */
    public void setAfk(Player p, boolean value, String reason) {
        UUID id = p.getUniqueId();
        if (value == isAfk(id)) {
            return;
        }
        boolean hidden = plugin.vanish().isVanished(p);
        if (value) {
            afk.put(id, reason == null ? "" : reason.trim());
            afkSince.put(id, System.currentTimeMillis());
        } else {
            afk.remove(id);
            afkSince.remove(id);
            lastActivity.put(id, System.currentTimeMillis());
        }
        // Schlafende Mitspieler sollen nicht auf AFK-Spieler warten müssen.
        p.setSleepingIgnored(value || hidden);
        applyTablist(p, value, hidden);

        String r = reason == null ? "" : reason.trim();
        if (value) {
            Msg.send(p, r.isEmpty()
                            ? "<gray>Du bist jetzt AFK. Bewegung beendet den Zustand.</gray>"
                            : "<gray>Du bist jetzt AFK: <white><reason></white></gray>",
                    Msg.text("reason", r));
        } else {
            Msg.send(p, "<gray>Willkommen zurück – AFK beendet.</gray>");
        }
        if (hidden || !raw().getBoolean("afk.broadcast", true)) {
            return;
        }
        String format = value
                ? str("afk.afk_format", DEFAULT_AFK)
                : str("afk.back_format", DEFAULT_BACK);
        Bukkit.broadcast(Msg.mm(format,
                Msg.name("name", p),
                Msg.text("reason", r.isEmpty() ? "" : " (" + r + ")")));
    }

    /** Hängt den AFK-Zusatz an den Tablist-Namen bzw. nimmt ihn wieder weg. */
    private void applyTablist(Player p, boolean value, boolean hidden) {
        if (!value) {
            // Zurücksetzen immer, auch für unsichtbare Betreiber: sonst bleibt der Zusatz hängen,
            // wenn jemand während des AFK-Zustands unsichtbar geworden ist.
            p.playerListName(null);
            return;
        }
        if (hidden || !raw().getBoolean("afk.tablist_mark", true)) {
            return;
        }
        Component suffix = Msg.mm(str("afk.tablist_suffix", DEFAULT_SUFFIX));
        p.playerListName(Component.text(p.getName()).append(suffix));
    }

    /** Prüft im Zeitgeber, wer zu lange untätig war. */
    @Override
    public void run() {
        if (!enabled()) {
            return;
        }
        long timeout = timeoutMillis();
        long now = System.currentTimeMillis();
        for (Player p : Bukkit.getOnlinePlayers()) {
            UUID id = p.getUniqueId();
            if (isAfk(id)) {
                continue;
            }
            Long last = lastActivity.get(id);
            if (last == null) {
                lastActivity.put(id, now);
                continue;
            }
            if (timeout > 0L && now - last >= timeout) {
                setAfk(p, true, "");
            }
        }
        // Karteileichen entfernen (Spieler ist offline).
        lastActivity.keySet().removeIf(id -> Bukkit.getPlayer(id) == null);
    }

    /** Beim Plugin-Ende: Tablist-Namen und Schlaf-Flag aller AFK-Spieler zurücksetzen. */
    public void shutdown() {
        for (UUID id : new ArrayList<>(afk.keySet())) {
            Player p = Bukkit.getPlayer(id);
            if (p != null && p.isOnline()) {
                boolean hidden = plugin.vanish().isVanished(p);
                p.setSleepingIgnored(hidden);
                applyTablist(p, false, hidden);
            }
        }
        afk.clear();
        afkSince.clear();
        lastActivity.clear();
    }

    private String str(String key, String def) {
        String v = raw().getString(key, def);
        return v == null || v.isEmpty() ? def : v;
    }
}
