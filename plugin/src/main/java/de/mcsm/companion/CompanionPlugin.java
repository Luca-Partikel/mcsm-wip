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
 * Chat-/Join-/Todesformate, Tablist, TPA, Homes, Spielmodus, stiller Betreiber-Join, MCSM-Hardcore
 * (Gräber + Wiederbelebung per Totem) und status.json.
 */
public final class CompanionPlugin extends JavaPlugin {

    private Config settings;
    private VanishManager vanish;
    private HomeStore homes;
    private TablistTask tablist;
    private StatusWriter status;
    private TpaCommand tpa;
    private HardcoreManager hardcore;

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

        PluginManager pm = Bukkit.getPluginManager();
        pm.registerEvents(new ChatListener(this), this);
        pm.registerEvents(new JoinQuitListener(this), this);
        pm.registerEvents(new DeathListener(this), this);
        pm.registerEvents(new PingListener(this), this);
        pm.registerEvents(new HardcoreListener(this), this);

        register(tpa, "tpa", "tpaccept", "tpdeny");
        register(new TeleportCommand(this), "tp");
        register(new GamemodeCommand(this), "gm");
        register(new HomeCommands(this), "sethome", "home", "delhome", "homes");
        register(new SpawnCommand(), "spawn");
        register(new AdminCommand(this), "admin");
        register(new HardcoreCommand(this), "hardcore");

        Bukkit.getScheduler().runTaskTimer(this, tablist, 20L, 100L);          // alle 5 s
        Bukkit.getScheduler().runTaskTimer(this, status::write, 20L, 1200L);   // alle 60 s
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
        // Unsichtbare Betreiber wieder sichtbar und verwundbar machen – hidePlayer-Zustände
        // hängen an diesem Plugin und würden sonst hängen bleiben.
        if (vanish != null) {
            for (Player v : vanish.vanishedPlayers()) {
                vanish.unvanish(v);
            }
        }
        Bukkit.getScheduler().cancelTasks(this);
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
        Player p = findPlayer(name);
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
}
