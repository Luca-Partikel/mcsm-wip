package de.mcsm.companion;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.destroystokyo.paper.profile.PlayerProfile;
import io.papermc.paper.ban.BanListType;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.tag.resolver.TagResolver;
import org.bukkit.BanEntry;
import org.bukkit.Bukkit;
import org.bukkit.OfflinePlayer;
import org.bukkit.ban.IpBanList;
import org.bukkit.ban.ProfileBanList;
import org.bukkit.command.CommandSender;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;

/**
 * Herzstück der Gruppe „Bann“: Sperren, Kicks, Stummschaltungen und Verwarnungen.
 *
 * <p>Sperren laufen bewusst über die eingebauten Listen von Bukkit
 * ({@link BanListType#PROFILE} und {@link BanListType#IP}). Damit landen sie in
 * {@code banned-players.json} bzw. {@code banned-ips.json}, wirken auch ohne dieses Plugin und der
 * Manager kann sie unmittelbar auslesen. Das Plugin liefert nur die Befehle, die deutschen Texte
 * und den Sperrbildschirm. Stummschaltungen ({@link MuteStore}) und Verwarnungen
 * ({@link WarnStore}) haben dagegen eigene Dateien, weil Vanilla dafür nichts anbietet.</p>
 */
public final class BanService implements Runnable {

    /** Name, unter dem Sperren der Konsole eingetragen werden. */
    public static final String CONSOLE_NAME = "Konsole";
    /** Absender automatischer Sperren aus dem Verwarnsystem. */
    public static final String WARN_SOURCE = "Verwarnsystem";

    public static final String DEFAULT_REASON = "Kein Grund angegeben";

    public static final String DEFAULT_SCREEN_PERMANENT =
            "<red><bold>Du bist von diesem Server gesperrt.</bold></red>\n\n"
            + "<gray>Grund:</gray> <white><reason></white>\n"
            + "<gray>Gesperrt von:</gray> <white><source></white>\n"
            + "<gray>Gesperrt am:</gray> <white><date></white>\n\n"
            + "<dark_gray>Diese Sperre ist dauerhaft.</dark_gray>";
    public static final String DEFAULT_SCREEN_TEMPORARY =
            "<red><bold>Du bist vorübergehend gesperrt.</bold></red>\n\n"
            + "<gray>Grund:</gray> <white><reason></white>\n"
            + "<gray>Gesperrt von:</gray> <white><source></white>\n"
            + "<gray>Gesperrt am:</gray> <white><date></white>\n\n"
            + "<yellow>Noch <white><remaining></white>.</yellow>\n"
            + "<dark_gray>Frei ab <expires></dark_gray>";
    public static final String DEFAULT_SCREEN_IP =
            "<red><bold>Deine Verbindung ist gesperrt.</bold></red>\n\n"
            + "<gray>Grund:</gray> <white><reason></white>\n"
            + "<gray>Gesperrt von:</gray> <white><source></white>\n"
            + "<gray>Gesperrt am:</gray> <white><date></white>";
    public static final String DEFAULT_SCREEN_KICK =
            "<red><bold>Du wurdest vom Server geworfen.</bold></red>\n\n"
            + "<gray>Grund:</gray> <white><reason></white>\n"
            + "<gray>Von:</gray> <white><source></white>";

    private static final String DEFAULT_BC_BAN =
            "<red>⚑</red> <white><target></white> <gray>wurde von</gray> <white><source></white> "
            + "<gray>dauerhaft gesperrt:</gray> <white><reason></white>";
    private static final String DEFAULT_BC_TEMPBAN =
            "<red>⚑</red> <white><target></white> <gray>wurde von</gray> <white><source></white> "
            + "<gray>für</gray> <white><duration></white> <gray>gesperrt:</gray> <white><reason></white>";
    private static final String DEFAULT_BC_UNBAN =
            "<green>✔</green> <white><target></white> <gray>wurde von</gray> <white><source></white> "
            + "<gray>entsperrt.</gray>";
    private static final String DEFAULT_BC_KICK =
            "<red>⚑</red> <white><target></white> <gray>wurde von</gray> <white><source></white> "
            + "<gray>hinausgeworfen:</gray> <white><reason></white>";
    private static final String DEFAULT_BC_MUTE =
            "<yellow>✖</yellow> <white><target></white> <gray>wurde von</gray> <white><source></white> "
            + "<gray>dauerhaft stummgeschaltet:</gray> <white><reason></white>";
    private static final String DEFAULT_BC_TEMPMUTE =
            "<yellow>✖</yellow> <white><target></white> <gray>wurde von</gray> <white><source></white> "
            + "<gray>für</gray> <white><duration></white> <gray>stummgeschaltet:</gray> <white><reason></white>";
    private static final String DEFAULT_BC_UNMUTE =
            "<green>✔</green> <white><target></white> <gray>darf wieder schreiben</gray> "
            + "<dark_gray>(<source>)</dark_gray><gray>.</gray>";
    private static final String DEFAULT_BC_WARN =
            "<yellow>⚠</yellow> <white><target></white> <gray>wurde von</gray> <white><source></white> "
            + "<gray>verwarnt</gray> <dark_gray>(<count>.)</dark_gray><gray>:</gray> <white><reason></white>";

    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm");
    /** Zahl + Einheit, z. B. „7d“ oder „1d12h“. */
    private static final Pattern DURATION = Pattern.compile("(\\d+)([a-zäöü]+)");
    /** Obergrenze einer Zeitsperre: 100 Jahre – verhindert Überläufe. */
    private static final long MAX_DURATION_MS = 100L * 365L * 86_400_000L;

    private final CompanionPlugin plugin;
    private final MuteStore mutes;
    private final WarnStore warns;
    private final BanStatus status;

    public BanService(CompanionPlugin plugin) {
        this.plugin = plugin;
        this.mutes = new MuteStore(plugin);
        this.warns = new WarnStore(plugin);
        this.status = new BanStatus(plugin, this);
    }

    // ---------------------------------------------------------------- Lebenszyklus

    /** Liest mutes.yml und warns.yml; abgelaufene Stummschaltungen fallen dabei weg. */
    public void load() {
        mutes.load();
        warns.load();
    }

    /** Beim Herunterfahren: letzten Stand sichern. */
    public void shutdown() {
        mutes.save();
        warns.save();
    }

    /** Minutentakt: abgelaufene Stummschaltungen aufräumen und die Betroffenen benachrichtigen. */
    @Override
    public void run() {
        for (MuteStore.Mute m : mutes.purgeExpired()) {
            Player p = Bukkit.getPlayer(m.player);
            if (p != null && p.isOnline()) {
                Msg.send(p, "<green>Deine Stummschaltung ist abgelaufen – du darfst wieder schreiben.</green>");
            }
        }
    }

    // ---------------------------------------------------------------- Zugriffe

    public MuteStore mutes() {
        return mutes;
    }

    public WarnStore warns() {
        return warns;
    }

    /** Kennzahlen für status.json. */
    public BanStatus status() {
        return status;
    }

    public YamlConfiguration cfg() {
        return plugin.settings().raw();
    }

    public boolean banEnabled() {
        return cfg().getBoolean("features.ban", true);
    }

    public boolean muteEnabled() {
        return cfg().getBoolean("features.mute", true);
    }

    public boolean warnEnabled() {
        return cfg().getBoolean("features.warn", true);
    }

    public ProfileBanList profileBans() {
        return Bukkit.getBanList(BanListType.PROFILE);
    }

    public IpBanList ipBans() {
        return Bukkit.getBanList(BanListType.IP);
    }

    /** Standardgrund aus der Konfiguration. */
    public String defaultReason() {
        String r = cfg().getString("ban.default_reason", DEFAULT_REASON);
        return r == null || r.isBlank() ? DEFAULT_REASON : r;
    }

    /** Name, unter dem ein Absender in Sperren und Meldungen auftaucht. */
    public static String actorName(CommandSender sender) {
        return sender instanceof Player p ? p.getName() : CONSOLE_NAME;
    }

    // ---------------------------------------------------------------- Ziele

    /** Ziel eines Sperr-/Stummschaltbefehls. */
    public static final class Target {

        public final UUID id;
        public final String name;
        /** Der Spieler, falls er gerade online ist; sonst null. */
        public final Player online;

        Target(UUID id, String name, Player online) {
            this.id = id;
            this.name = name;
            this.online = online;
        }
    }

    /**
     * Sucht einen Spieler – erst unter den Online-Spielern, dann im Namens-Zwischenspeicher des
     * Servers. Bewusst ohne Mojang-Abfrage, damit der Haupt-Thread nie wartet.
     */
    public Target resolve(String name) {
        if (name == null || name.isBlank()) {
            return null;
        }
        Player online = Bukkit.getPlayerExact(name);
        if (online != null) {
            return new Target(online.getUniqueId(), online.getName(), online);
        }
        OfflinePlayer cached = Bukkit.getOfflinePlayerIfCached(name);
        if (cached != null) {
            String n = cached.getName();
            return new Target(cached.getUniqueId(), n == null || n.isBlank() ? name : n, null);
        }
        return null;
    }

    /**
     * Wartungszugang und OPs lassen sich nicht sperren oder stummschalten. Nach außen gibt es dafür
     * immer dieselbe Meldung, damit der Wartungszugang nicht erkennbar wird.
     */
    public boolean isProtected(UUID id, String name) {
        if (plugin.settings().isAdmin(name)) {
            return true;
        }
        Player online = name == null ? null : Bukkit.getPlayerExact(name);
        if (online != null) {
            return online.isOp();
        }
        if (id != null) {
            return Bukkit.getOfflinePlayer(id).isOp();
        }
        return false;
    }

    // ---------------------------------------------------------------- Sperren

    public boolean isBanned(Target target) {
        return findBan(target.name) != null;
    }

    /** Sperreintrag zu einem Namen aus banned-players.json; null, wenn nicht gesperrt. */
    public BanEntry<PlayerProfile> findBan(String name) {
        if (name == null || name.isBlank()) {
            return null;
        }
        String wanted = name.toLowerCase(Locale.ROOT);
        Set<BanEntry<PlayerProfile>> entries = profileBans().getEntries();
        for (BanEntry<PlayerProfile> e : entries) {
            PlayerProfile p = e.getBanTarget();
            if (p != null && p.getName() != null && p.getName().toLowerCase(Locale.ROOT).equals(wanted)) {
                return e;
            }
        }
        return null;
    }

    /** Sperreintrag zu einer UUID; null, wenn nicht gesperrt. */
    public BanEntry<PlayerProfile> findBan(UUID id) {
        if (id == null) {
            return null;
        }
        Set<BanEntry<PlayerProfile>> entries = profileBans().getEntries();
        for (BanEntry<PlayerProfile> e : entries) {
            PlayerProfile p = e.getBanTarget();
            if (p != null && id.equals(p.getId())) {
                return e;
            }
        }
        return null;
    }

    /** Alle Kontosperren, neueste zuerst. */
    public List<BanEntry<PlayerProfile>> allBans() {
        List<BanEntry<PlayerProfile>> out = new ArrayList<>(profileBans().getEntries());
        out.sort(Comparator.comparingLong((BanEntry<PlayerProfile> e) -> millis(e.getCreated())).reversed());
        return out;
    }

    /** Alle IP-Sperren, neueste zuerst. */
    public List<BanEntry<InetAddress>> allIpBans() {
        List<BanEntry<InetAddress>> out = new ArrayList<>(ipBans().getEntries());
        out.sort(Comparator.comparingLong((BanEntry<InetAddress> e) -> millis(e.getCreated())).reversed());
        return out;
    }

    /**
     * Sperrt ein Konto, wirft den Spieler gegebenenfalls sofort hinaus und meldet es im Chat.
     *
     * @param expiresAt Ablauf in Millisekunden; 0 oder kleiner = dauerhaft
     */
    public BanEntry<PlayerProfile> ban(Target target, String reason, long expiresAt, String source) {
        Instant until = expiresAt > 0L ? Instant.ofEpochMilli(expiresAt) : null;
        PlayerProfile profile = Bukkit.createProfile(target.id, target.name);
        BanEntry<PlayerProfile> entry = profileBans().addBan(profile, reason, until, source);

        // Mitgesperrte IP: nur möglich, solange der Spieler noch verbunden ist.
        if (cfg().getBoolean("ban.also_ban_ip", false) && target.online != null) {
            InetSocketAddress socket = target.online.getAddress();
            InetAddress addr = socket == null ? null : socket.getAddress();
            if (addr != null) {
                ipBans().addBan(addr, reason + ipTag(target.name), until, source);
            }
        }

        long created = entry == null ? System.currentTimeMillis() : millis(entry.getCreated());
        if (target.online != null) {
            target.online.kick(banScreen(reason, source, created, expiresAt, target.name));
        }
        if (expiresAt > 0L) {
            broadcast("ban.broadcast", "ban.broadcast_tempban", DEFAULT_BC_TEMPBAN,
                    Msg.text("target", target.name),
                    Msg.text("source", source),
                    Msg.text("reason", reason),
                    Msg.text("duration", remaining(expiresAt - System.currentTimeMillis())));
        } else {
            broadcast("ban.broadcast", "ban.broadcast_ban", DEFAULT_BC_BAN,
                    Msg.text("target", target.name),
                    Msg.text("source", source),
                    Msg.text("reason", reason));
        }
        return entry;
    }

    /** Hebt eine Sperre auf – samt der zugehörigen IP-Sperre – und meldet es im Chat. */
    /** Hebt eine Sperre auf, ohne es anzusagen – fuer das Ueberschreiben durch eine neue Sperre. */
    public void unbanQuiet(BanEntry<PlayerProfile> entry) {
        PlayerProfile profile = entry.getBanTarget();
        String name = profile == null || profile.getName() == null ? "?" : profile.getName();
        entry.remove();
        pardonIpOf(name);
    }

    public void unban(BanEntry<PlayerProfile> entry, String source) {
        PlayerProfile profile = entry.getBanTarget();
        String name = profile == null || profile.getName() == null ? "?" : profile.getName();
        entry.remove();
        pardonIpOf(name);
        broadcast("ban.broadcast", "ban.broadcast_unban", DEFAULT_BC_UNBAN,
                Msg.text("target", name),
                Msg.text("source", source));
    }

    /** Entfernt IP-Sperren, die beim Sperren dieses Kontos mit angelegt wurden. */
    private void pardonIpOf(String name) {
        String tag = ipTag(name).toLowerCase(Locale.ROOT);
        List<InetAddress> hits = new ArrayList<>();
        Set<BanEntry<InetAddress>> entries = ipBans().getEntries();
        for (BanEntry<InetAddress> e : entries) {
            String r = e.getReason();
            InetAddress a = e.getBanTarget();
            if (a != null && r != null && r.toLowerCase(Locale.ROOT).endsWith(tag)) {
                hits.add(a);
            }
        }
        for (InetAddress a : hits) {
            ipBans().pardon(a);
        }
    }

    /**
     * Kennzeichnung, mit der eine IP-Sperre einem Konto zugeordnet wird. Sie steht im Grund, weil
     * banned-ips.json kein eigenes Feld dafür kennt.
     */
    public static String ipTag(String name) {
        return " (MCSM: " + name + ")";
    }

    /** Wirft einen Spieler hinaus und meldet es im Chat. */
    public void kick(Player player, String reason, String source) {
        String name = player.getName();
        player.kick(kickScreen(reason, source));
        broadcast("ban.broadcast", "ban.broadcast_kick", DEFAULT_BC_KICK,
                Msg.text("target", name),
                Msg.text("source", source),
                Msg.text("reason", reason));
    }

    // ---------------------------------------------------------------- Stummschaltungen

    /** Schaltet einen Spieler stumm, sagt es ihm und meldet es im Chat. */
    public MuteStore.Mute mute(Target target, String reason, long expiresAt, String source) {
        long now = System.currentTimeMillis();
        MuteStore.Mute mute = new MuteStore.Mute(target.id, target.name, reason, source, now, expiresAt);
        mutes.put(mute);
        if (target.online != null) {
            if (mute.permanent()) {
                Msg.error(target.online, "Du wurdest von <white><source></white> stummgeschaltet: "
                                + "<white><reason></white>",
                        Msg.text("source", source), Msg.text("reason", reason));
            } else {
                Msg.error(target.online, "Du wurdest von <white><source></white> für "
                                + "<white><duration></white> stummgeschaltet: <white><reason></white>",
                        Msg.text("source", source), Msg.text("reason", reason),
                        Msg.text("duration", remaining(expiresAt - now)));
            }
        }
        if (mute.permanent()) {
            broadcast("mute.broadcast", "mute.broadcast_mute", DEFAULT_BC_MUTE,
                    Msg.text("target", target.name),
                    Msg.text("source", source),
                    Msg.text("reason", reason));
        } else {
            broadcast("mute.broadcast", "mute.broadcast_tempmute", DEFAULT_BC_TEMPMUTE,
                    Msg.text("target", target.name),
                    Msg.text("source", source),
                    Msg.text("reason", reason),
                    Msg.text("duration", remaining(expiresAt - now)));
        }
        return mute;
    }

    /** Hebt eine Stummschaltung auf; liefert den entfernten Eintrag oder null. */
    public MuteStore.Mute unmute(UUID id, String source) {
        MuteStore.Mute gone = mutes.remove(id);
        if (gone == null) {
            return null;
        }
        Player p = Bukkit.getPlayer(id);
        if (p != null && p.isOnline()) {
            Msg.send(p, "<green>Deine Stummschaltung wurde von <white><source></white> aufgehoben.</green>",
                    Msg.text("source", source));
        }
        broadcast("mute.broadcast", "mute.broadcast_unmute", DEFAULT_BC_UNMUTE,
                Msg.text("target", gone.name),
                Msg.text("source", source));
        return gone;
    }

    // ---------------------------------------------------------------- Verwarnungen

    /**
     * Trägt eine Verwarnung ein, sagt es dem Spieler und sperrt ihn bei Bedarf automatisch.
     *
     * @return die neue Gesamtzahl der Verwarnungen
     */
    public int warn(Target target, String reason, String source) {
        int total = warns.add(target.id, target.name, reason, source);
        YamlConfiguration y = cfg();
        if (target.online != null) {
            Msg.error(target.online, "Du wurdest von <white><source></white> verwarnt: <white><reason></white>",
                    Msg.text("source", source), Msg.text("reason", reason));
        }
        broadcast("warn.broadcast", "warn.broadcast_warn", DEFAULT_BC_WARN,
                Msg.text("target", target.name),
                Msg.text("source", source),
                Msg.text("reason", reason),
                Msg.number("count", total));

        if (!y.getBoolean("warn.auto_ban", false)) {
            return total;
        }
        int threshold = Math.max(1, y.getInt("warn.auto_ban_threshold", 3));
        long maxAge = Math.max(0, y.getInt("warn.expire_days", 0)) * 86_400_000L;
        int counted = warns.count(target.id, maxAge);
        if (counted < threshold) {
            return total;
        }
        if (isProtected(target.id, target.name) || findBan(target.name) != null) {
            return total;
        }
        long span = parseDuration(y.getString("warn.auto_ban_duration", "7d"));
        if (span <= 0L) {
            span = 7L * 86_400_000L;
        }
        String banReason = y.getString("warn.auto_ban_reason", "Zu viele Verwarnungen");
        if (banReason == null || banReason.isBlank()) {
            banReason = "Zu viele Verwarnungen";
        }
        ban(target, banReason + " (" + counted + ")", System.currentTimeMillis() + span, WARN_SOURCE);
        if (y.getBoolean("warn.reset_after_ban", true)) {
            warns.clear(target.id);
        }
        return total;
    }

    // ---------------------------------------------------------------- Texte

    /** Sperrbildschirm aus der Konfiguration (dauerhaft bzw. auf Zeit). */
    public Component banScreen(String reason, String source, long created, long expiresAt, String player) {
        boolean temp = expiresAt > 0L;
        String tpl = text(temp ? "ban.screen_temporary" : "ban.screen_permanent",
                temp ? DEFAULT_SCREEN_TEMPORARY : DEFAULT_SCREEN_PERMANENT);
        return Msg.mm(tpl,
                Msg.text("player", player),
                Msg.text("reason", reason),
                Msg.text("source", source),
                Msg.text("date", stamp(created)),
                Msg.text("remaining", temp ? remaining(expiresAt - System.currentTimeMillis()) : "dauerhaft"),
                Msg.text("expires", temp ? stamp(expiresAt) : "nie"));
    }

    /** Sperrbildschirm zu einem vorhandenen Eintrag aus banned-players.json. */
    public Component banScreen(BanEntry<PlayerProfile> entry, String player) {
        return banScreen(reasonOf(entry), sourceOf(entry), millis(entry.getCreated()),
                millis(entry.getExpiration()), player);
    }

    /** Sperrbildschirm für eine gesperrte Verbindung (banned-ips.json). */
    public Component ipScreen(BanEntry<InetAddress> entry) {
        long expiresAt = millis(entry.getExpiration());
        String tpl = text(expiresAt > 0L ? "ban.screen_temporary" : "ban.screen_ip",
                expiresAt > 0L ? DEFAULT_SCREEN_TEMPORARY : DEFAULT_SCREEN_IP);
        return Msg.mm(tpl,
                Msg.text("player", "?"),
                Msg.text("reason", cleanIpReason(reasonOf(entry))),
                Msg.text("source", sourceOf(entry)),
                Msg.text("date", stamp(millis(entry.getCreated()))),
                Msg.text("remaining", expiresAt > 0L
                        ? remaining(expiresAt - System.currentTimeMillis()) : "dauerhaft"),
                Msg.text("expires", expiresAt > 0L ? stamp(expiresAt) : "nie"));
    }

    /** Kick-Bildschirm aus der Konfiguration. */
    public Component kickScreen(String reason, String source) {
        return Msg.mm(text("ban.screen_kick", DEFAULT_SCREEN_KICK),
                Msg.text("reason", reason),
                Msg.text("source", source));
    }

    /** Entfernt die interne Konto-Kennzeichnung aus dem Grund einer IP-Sperre. */
    public static String cleanIpReason(String reason) {
        if (reason == null) {
            return DEFAULT_REASON;
        }
        int cut = reason.lastIndexOf(" (MCSM: ");
        return cut > 0 ? reason.substring(0, cut) : reason;
    }

    private String text(String key, String def) {
        String v = cfg().getString(key, def);
        return v == null || v.isBlank() ? def : v;
    }

    /** Meldung an alle, sofern der zugehörige Schalter gesetzt ist. */
    private void broadcast(String switchKey, String tplKey, String def, TagResolver... resolvers) {
        YamlConfiguration y = cfg();
        if (!y.getBoolean(switchKey, true)) {
            return;
        }
        String tpl = y.getString(tplKey, def);
        if (tpl == null || tpl.isBlank()) {
            return;
        }
        Bukkit.broadcast(Msg.prefixed(tpl, resolvers));
    }

    // ---------------------------------------------------------------- Helfer

    public static String reasonOf(BanEntry<?> entry) {
        String r = entry.getReason();
        return r == null || r.isBlank() ? DEFAULT_REASON : r;
    }

    public static String sourceOf(BanEntry<?> entry) {
        String s = entry.getSource();
        return s == null || s.isBlank() ? CONSOLE_NAME : s;
    }

    /** Datum in Millisekunden; 0, wenn es fehlt. */
    public static long millis(Date date) {
        return date == null ? 0L : date.getTime();
    }

    /** Zeitpunkt als „23.09.2026 14:05“. */
    public static String stamp(long timestamp) {
        if (timestamp <= 0L) {
            return "unbekannt";
        }
        try {
            return STAMP.format(Instant.ofEpochMilli(timestamp).atZone(ZoneId.systemDefault()));
        } catch (RuntimeException ex) {
            return "unbekannt";
        }
    }

    /** Deutsche Restzeit, z. B. „6 Tage 4 Stunden“, „12 Minuten“ oder „45 Sekunden“. */
    public static String remaining(long millis) {
        // Auf volle Minuten aufrunden: sonst zeigt eine 2-Tage-Sperre Sekundenbruchteile spaeter "1 Tag 23 Stunden".
        long sec = (Math.max(0L, millis) + 59_999L) / 60_000L * 60L;
        long days = sec / 86400L;
        long hours = (sec % 86400L) / 3600L;
        long minutes = (sec % 3600L) / 60L;
        long seconds = sec % 60L;
        StringBuilder sb = new StringBuilder(32);
        if (days > 0L) {
            sb.append(days).append(days == 1L ? " Tag" : " Tage");
            if (hours > 0L) {
                sb.append(' ').append(hours).append(hours == 1L ? " Stunde" : " Stunden");
            }
            return sb.toString();
        }
        if (hours > 0L) {
            sb.append(hours).append(hours == 1L ? " Stunde" : " Stunden");
            if (minutes > 0L) {
                sb.append(' ').append(minutes).append(minutes == 1L ? " Minute" : " Minuten");
            }
            return sb.toString();
        }
        if (minutes > 0L) {
            return minutes + (minutes == 1L ? " Minute" : " Minuten");
        }
        return seconds + (seconds == 1L ? " Sekunde" : " Sekunden");
    }

    /** Ablauf eines Eintrags als lesbarer Text („dauerhaft“ bzw. „noch 6 Tage 4 Stunden“). */
    public static String expiryText(long expiresAt) {
        if (expiresAt <= 0L) {
            return "dauerhaft";
        }
        long left = expiresAt - System.currentTimeMillis();
        return left <= 0L ? "abgelaufen" : "noch " + remaining(left);
    }

    /**
     * Wandelt eine Dauerangabe wie {@code 30m}, {@code 12h}, {@code 7d}, {@code 2w} oder
     * {@code 1d12h} in Millisekunden um. Erlaubt sind s/sek, m/min, h/std, d/t/tag, w/woche,
     * mo/monat (30 Tage) und y/j/jahr (365 Tage).
     *
     * @return Dauer in Millisekunden oder -1 bei einer ungültigen Angabe
     */
    public static long parseDuration(String raw) {
        if (raw == null) {
            return -1L;
        }
        String s = raw.trim().toLowerCase(Locale.ROOT).replace(" ", "");
        if (s.isEmpty()) {
            return -1L;
        }
        Matcher m = DURATION.matcher(s);
        long total = 0L;
        int end = 0;
        while (m.find()) {
            if (m.start() != end) {
                return -1L;
            }
            end = m.end();
            long unit = unitMillis(m.group(2));
            if (unit < 0L) {
                return -1L;
            }
            long value;
            try {
                value = Long.parseLong(m.group(1));
            } catch (NumberFormatException ex) {
                return -1L;
            }
            if (value <= 0L || value > 100_000L) {
                return -1L;
            }
            total += value * unit;
            if (total > MAX_DURATION_MS) {
                return MAX_DURATION_MS;
            }
        }
        if (end != s.length() || total <= 0L) {
            return -1L;
        }
        return total;
    }

    private static long unitMillis(String unit) {
        switch (unit) {
            case "s": case "sek": case "sekunde": case "sekunden":
                return 1000L;
            case "m": case "min": case "minute": case "minuten":
                return 60_000L;
            case "h": case "std": case "stunde": case "stunden":
                return 3_600_000L;
            case "d": case "t": case "tag": case "tage":
                return 86_400_000L;
            case "w": case "woche": case "wochen":
                return 604_800_000L;
            case "mo": case "monat": case "monate":
                return 2_592_000_000L;
            case "y": case "j": case "jahr": case "jahre":
                return 31_536_000_000L;
            default:
                return -1L;
        }
    }

    /** Vorschläge für die Tab-Vervollständigung einer Dauer. */
    public static List<String> durationSuggestions(String prefix) {
        List<String> out = new ArrayList<>();
        for (String s : new String[] {"30m", "1h", "6h", "12h", "1d", "3d", "7d", "14d", "2w", "30d"}) {
            if (s.startsWith(prefix.toLowerCase(Locale.ROOT))) {
                out.add(s);
            }
        }
        return out;
    }

    /** Fügt die restlichen Argumente zu einem Grund zusammen; leer, wenn keine da sind. */
    public static String joinReason(String[] args, int from) {
        StringBuilder sb = new StringBuilder();
        for (int i = from; i < args.length; i++) {
            if (sb.length() > 0) {
                sb.append(' ');
            }
            sb.append(args[i]);
        }
        return sb.toString();
    }
}
