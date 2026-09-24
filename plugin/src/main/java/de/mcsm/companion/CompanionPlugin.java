package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;

import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;
import org.bukkit.plugin.PluginManager;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * MCSMCompanion – Begleit-Plugin des Minecraft Server Managers.
 *
 * <p>Enthält Chat-/Join-/Todesformate, Tablist, TPA, Homes, Spielmodus, den stillen
 * Betreiber-Join mit zwei getrennten Wartungsprofilen, den MCSM-Hardcore-Modus
 * (Gräber + Wiederbelebung per Totem), Teleport-Komfort (Aufwärmzeit, /back, /rtp, Warps,
 * /top), Sozialbefehle (private Nachrichten, AFK, Statistik, /near, Chat-Zusätze),
 * Server-Komfort (Schlafen, Begrüßung, Regeln, Seitenleiste, Hinweise), Moderation
 * (Sperren, Kicks, Stummschaltungen, Verwarnungen), die Freigabeliste, das Herunterfahren
 * mit Ansage und status.json für den Manager.</p>
 */
public final class CompanionPlugin extends JavaPlugin {

    private Config settings;
    private VanishManager vanish;
    private HomeStore homes;
    private TablistTask tablist;
    private StatusWriter status;
    private TpaCommand tpa;
    private HardcoreManager hardcore;
    /** Werkzeuge des Wartungszugangs; hier gehalten, damit der god-Zustand aufgeräumt wird. */
    private AdminTools adminTools;

    // Teleport und Navigation
    private TeleportService teleports;
    private TeleportHistory teleportHistory;
    private WarpStore warps;

    // Sozial, Chat und Statistik
    private PlayerStatsStore stats;
    private AfkManager afk;
    private SocialSpyCommand socialSpy;
    private MsgCommand msg;
    private ChatExtrasListener chatExtras;

    // Server-Komfort und Kennzahlen
    private ServerMetrics metrics;
    private SleepManager sleep;
    private SidebarManager sidebar;
    private ClockTask clock;
    private AutoBroadcastTask broadcast;

    // Moderation: Sperren, Kicks, Stummschaltungen, Verwarnungen
    private BanService bans;

    // Freigabeliste und geordnetes Herunterfahren
    private WhitelistService whitelist;
    private ShutdownService shutdown;

    @Override
    public void onEnable() {
        settings = new Config(this);
        settings.load();
        vanish = new VanishManager(this);
        homes = new HomeStore(this);
        homes.load();
        tablist = new TablistTask(this);
        status = new StatusWriter(this);
        tpa = new TpaCommand(this);
        hardcore = new HardcoreManager(this);
        hardcore.load();
        adminTools = new AdminTools(this);

        // Teleport und Navigation
        teleports = new TeleportService(this);
        teleportHistory = new TeleportHistory(this);
        teleportHistory.load();
        warps = new WarpStore(this);
        warps.load();
        teleports.start();

        // Sozial, Chat und Statistik (Reihenfolge wichtig: socialSpy und afk vor msg)
        stats = new PlayerStatsStore(this);
        stats.load();
        afk = new AfkManager(this);
        socialSpy = new SocialSpyCommand(this);
        msg = new MsgCommand(this, socialSpy, afk);
        chatExtras = new ChatExtrasListener(this);

        // Server-Komfort und Kennzahlen
        metrics = new ServerMetrics(this);
        sleep = new SleepManager(this);
        sidebar = new SidebarManager(this, metrics);
        sidebar.load();
        clock = new ClockTask(this);
        clock.load();
        broadcast = new AutoBroadcastTask(this);

        // Moderation (mutes.yml und warns.yml lesen; die Sperren selbst liegen in banned-players.json)
        bans = new BanService(this);
        bans.load();

        // Freigabeliste und Herunterfahren mit Ansage
        whitelist = new WhitelistService(this);
        shutdown = new ShutdownService(this);

        PluginManager pm = Bukkit.getPluginManager();
        pm.registerEvents(new ChatListener(this), this);
        pm.registerEvents(new JoinQuitListener(this), this);
        pm.registerEvents(new DeathListener(this), this);
        pm.registerEvents(new PingListener(this), this);
        pm.registerEvents(new HardcoreListener(this), this);
        pm.registerEvents(new AdminProfileListener(this), this);
        pm.registerEvents(new TeleportListener(teleports, teleportHistory), this);
        pm.registerEvents(stats, this);
        pm.registerEvents(new AfkListener(this, afk), this);
        pm.registerEvents(socialSpy, this);
        pm.registerEvents(msg, this);
        pm.registerEvents(chatExtras, this);
        pm.registerEvents(new SleepListener(this, sleep), this);
        pm.registerEvents(new WelcomeListener(this), this);
        pm.registerEvents(new BanLoginListener(bans), this);
        pm.registerEvents(new MuteChatListener(bans), this);
        pm.registerEvents(new WhitelistListener(this, whitelist), this);

        register(tpa, "tpa", "tpaccept", "tpdeny");
        register(new TeleportCommand(this), "tp");
        register(new GamemodeCommand(this), "gm");
        register(new HomeCommands(this), "sethome", "home", "delhome", "homes");
        register(new SpawnCommand(this), "spawn");
        register(new AdminCommand(this), "admin");
        register(new HardcoreCommand(this), "hardcore");
        register(new BackCommand(this, teleports, teleportHistory), "back");
        register(new RtpCommand(this, teleports), "rtp");
        register(new TopCommand(this, teleports), "top");
        register(new WarpCommands(this, warps, teleports), "warp", "warps", "setwarp", "delwarp");
        register(msg, "msg", "r");
        register(socialSpy, "socialspy");
        register(new AfkCommand(afk), "afk");
        register(new PlaytimeCommand(this, stats), "playtime");
        register(new SeenCommand(this, stats, afk), "seen");
        register(new StatsCommand(this, stats, afk), "stats");
        register(new NearCommand(this, afk), "near");
        register(new RulesCommand(this), "rules");
        register(new McsmCommand(this, metrics), "mcsm");
        register(new SidebarCommand(this, sidebar), "sb");
        register(new ClockCommand(clock), "uhr");
        register(new BanCommands(this, bans), "ban", "tempban", "unban", "banlist", "kick");
        register(new MuteCommands(this, bans), "mute", "tempmute", "unmute", "mutelist");
        register(new WarnCommands(this, bans), "warn", "warns");
        register(new WhitelistCommand(this, whitelist), "wl");
        register(new ShutdownCommand(this, shutdown), "mcsmstop");

        Bukkit.getScheduler().runTaskTimer(this, tablist, 20L, 100L);                    // alle 5 s
        // Alle 30 s: die Spielerliste in status.json darf nicht älter als eine Minute werden,
        // sonst verwirft der Manager sie und fragt wieder über die Konsole nach ("list").
        Bukkit.getScheduler().runTaskTimer(this, status::write, 20L, 600L);              // alle 30 s
        Bukkit.getScheduler().runTaskTimer(this, chatExtras, 20L, 10L);                  // alle 0,5 s
        Bukkit.getScheduler().runTaskTimer(this, sleep, 40L, 20L);                       // jede Sekunde
        Bukkit.getScheduler().runTaskTimer(this, sidebar, 60L, 20L);                     // jede Sekunde
        Bukkit.getScheduler().runTaskTimer(this, clock, 40L, 20L);                       // Uhr in der Actionbar
        Bukkit.getScheduler().runTaskTimer(this, afk, 100L, 100L);                       // alle 5 s
        Bukkit.getScheduler().runTaskTimer(this, broadcast, 200L, 100L);                 // alle 5 s
        Bukkit.getScheduler().runTaskTimer(this, metrics, 200L, 100L);                   // alle 5 s
        Bukkit.getScheduler().runTaskTimer(this, stats, 6000L, 6000L);                   // alle 5 min
        Bukkit.getScheduler().runTaskTimer(this, teleportHistory::saveIfDirty, 6000L, 6000L); // alle 5 min
        Bukkit.getScheduler().runTaskTimer(this, bans, 1200L, 1200L);                    // jede Minute
        chatExtras.run();
        status.write();

        List<String> suspicious = status.suspiciousPlugins();
        if (!suspicious.isEmpty()) {
            getLogger().warning("Verdächtige Plugins (Spielerzahl/Ping/MOTD-Manipulation?): " + String.join(", ", suspicious));
        }
        getLogger().info("MCSMCompanion " + getPluginMeta().getVersion() + " aktiv – Server \"" + settings.serverName
                + "\", Modus " + settings.mode + ", Manager " + settings.managerVersion);
    }

    @Override
    public void onDisable() {
        // Noch verbundene Spieler freundlich verabschieden, bevor irgendetwas abgebaut wird.
        // Bewusst ganz vorn: die Verbindungen stehen hier noch (Bukkit.isStopping() ist bereits
        // true, PlayerList.removeAll() kommt erst nach disablePlugins()), und das dadurch
        // ausgelöste PlayerQuitEvent soll noch auf intakte Manager treffen – Statistik,
        // Wartungsprofile und Sichtbarkeit. Nach cancelTasks() käme der Kick zu spät.
        if (shutdown != null) {
            shutdown.disconnectAll();
        }
        // Unsichtbare Betreiber wieder sichtbar und verwundbar machen – hidePlayer-Zustände
        // hängen an diesem Plugin und würden sonst hängen bleiben. unvanish() wechselt bewusst
        // kein Profil, sonst landete das Normal-Profil in der echten Spielerdatei.
        if (vanish != null) {
            for (Player v : vanish.vanishedPlayers()) {
                vanish.unvanish(v);
            }
            vanish.profiles().shutdown();
        }
        if (afk != null) {
            afk.shutdown();
        }
        if (sidebar != null) {
            sidebar.clearAll();
        }
        if (sleep != null) {
            sleep.shutdown();
        }
        if (bans != null) {
            bans.shutdown();            // mutes.yml und warns.yml sichern
        }
        Bukkit.getScheduler().cancelTasks(this);
        if (teleports != null) {
            teleports.shutdown();
        }
        if (teleportHistory != null) {
            teleportHistory.saveIfDirty();
        }
        if (stats != null) {
            stats.shutdown();
        }
        if (hardcore != null) {
            hardcore.shutdown();
        }
        if (status != null) {
            status.write();
        }
        if (tablist != null && settings != null && settings.featTablist) {
            tablist.clear();
        }
    }

    private void register(TabExecutor executor, String... names) {
        for (String name : names) {
            PluginCommand cmd = getCommand(name);
            if (cmd == null) {
                getLogger().warning("Befehl /" + name + " fehlt in plugin.yml");
                continue;
            }
            cmd.setExecutor(executor);
            cmd.setTabCompleter(executor);
        }
    }

    /** Konfiguration neu lesen und abhängige Anzeigen auffrischen. */
    public void reloadAll() {
        settings.load();
        homes.load();
        hardcore.applyConfig();
        warps.load();
        teleportHistory.saveIfDirty();
        stats.run();
        sidebar.load();
        clock.load();
        bans.load();                 // mutes.yml und warns.yml neu lesen, abgelaufene aufräumen
        whitelist.applyConfig();     // Ablehnungstext der Freigabeliste neu bauen
        if (settings.featTablist) {
            tablist.run();
        } else {
            tablist.clear();
        }
        status.write();
    }

    /** Infozeile für Betreiber (Join-Bestätigung und /admin status). */
    public Component adminInfo(Player forPlayer) {
        int online = Bukkit.getOnlinePlayers().size();
        int plugins = Bukkit.getPluginManager().getPlugins().length;
        String hint;
        if (forPlayer == null) {
            hint = "";
        } else if (vanish.isVanished(forPlayer)) {
            hint = " <gray>/admin join</gray> <gray>macht dich sichtbar.</gray>";
        } else {
            hint = " <gray>/admin vanish</gray> <gray>macht dich unsichtbar.</gray>";
        }
        return Msg.mm("<green>MinecraftManager</green> <gray>|</gray> "
                        + "<gray>Dieser Server wird von deiner Software gesteuert! Läuft auf "
                        + "<white>Paper <mc></white> (<paper>), Manager <white><manager></white>, Modus <white><mode></white>, "
                        + "Spieler <white><online>/<max></white>, Plugins: <white><plugins></white>.</gray>" + hint,
                Msg.text("mc", Bukkit.getMinecraftVersion()),
                Msg.text("paper", shortPaperVersion()),
                Msg.text("manager", settings.managerVersion),
                Msg.text("mode", settings.mode),
                Msg.number("online", online),
                Msg.number("max", Bukkit.getMaxPlayers()),
                Msg.number("plugins", plugins));
    }

    private static String shortPaperVersion() {
        String v = Bukkit.getVersion();
        int cut = v.indexOf(" (");
        return cut > 0 ? v.substring(0, cut) : v;
    }

    /** Spieler nach Namen (auch Teilstück), unabhängig von der Sichtbarkeit. */
    public Player findPlayer(String name) {
        Player exact = Bukkit.getPlayerExact(name);
        return exact != null ? exact : Bukkit.getPlayer(name);
    }

    /** Spieler nach Namen – unsichtbare Betreiber nur für Betreiber/OPs auffindbar. */
    public Player findVisible(CommandSender viewer, String name) {
        return visible(viewer, findPlayer(name));
    }

    /**
     * Wie {@link #findVisible}, aber nur bei genau passendem Namen. Für Eingriffe wie /kick, bei
     * denen ein Teilstück sonst den Falschen treffen könnte („Anna“ statt „AnnaBelle“).
     */
    public Player findVisibleExact(CommandSender viewer, String name) {
        return visible(viewer, name == null ? null : Bukkit.getPlayerExact(name));
    }

    private Player visible(CommandSender viewer, Player p) {
        if (p != null && vanish.isVanished(p) && !canSeeVanished(viewer)) {
            return null;
        }
        return p;
    }

    public boolean canSeeVanished(CommandSender viewer) {
        return !(viewer instanceof Player) || viewer.isOp() || settings.isAdmin(viewer.getName())
                || vanish.isVanished((Player) viewer);
    }

    /** Online-Spieler, die der Absender sehen darf (für Tab-Vervollständigung). */
    public List<Player> visiblePlayers(CommandSender viewer) {
        boolean all = canSeeVanished(viewer);
        List<Player> out = new ArrayList<>();
        for (Player p : Bukkit.getOnlinePlayers()) {
            if (all || !vanish.isVanished(p)) {
                out.add(p);
            }
        }
        return out;
    }

    public Config settings() {
        return settings;
    }

    public VanishManager vanish() {
        return vanish;
    }

    /** Getrennte Spielerprofile des Wartungszugangs (unsichtbar / normal). */
    public AdminProfiles adminProfiles() {
        return vanish.profiles();
    }

    public HomeStore homes() {
        return homes;
    }

    public TablistTask tablist() {
        return tablist;
    }

    public StatusWriter status() {
        return status;
    }

    public TpaCommand tpa() {
        return tpa;
    }

    public HardcoreManager hardcore() {
        return hardcore;
    }

    /** Werkzeuge des Wartungszugangs (Heilen, Flug, Unverwundbarkeit, Neustart-Countdown). */
    public AdminTools adminTools() {
        return adminTools;
    }

    public TeleportService teleports() {
        return teleports;
    }

    public TeleportHistory teleportHistory() {
        return teleportHistory;
    }

    public WarpStore warps() {
        return warps;
    }

    public PlayerStatsStore stats() {
        return stats;
    }

    public AfkManager afk() {
        return afk;
    }

    public SocialSpyCommand socialSpy() {
        return socialSpy;
    }

    public MsgCommand msg() {
        return msg;
    }

    public ChatExtrasListener chatExtras() {
        return chatExtras;
    }

    public ServerMetrics metrics() {
        return metrics;
    }

    public SleepManager sleep() {
        return sleep;
    }

    public SidebarManager sidebar() {
        return sidebar;
    }

    public ClockTask clock() {
        return clock;
    }

    /** Moderation: Sperren, Kicks, Stummschaltungen und Verwarnungen. */
    public BanService bans() {
        return bans;
    }

    /** Freigabeliste (eingebaute Bukkit-Whitelist mit eigenen deutschen Texten). */
    public WhitelistService whitelist() {
        return whitelist;
    }

    /** Herunterfahren mit Ansage und freundlicher Abschied beim Plugin-Ende. */
    public ShutdownService shutdown() {
        return shutdown;
    }
}
