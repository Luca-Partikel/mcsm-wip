package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /near [Radius] – zeigt Mitspieler in der Nähe mit Entfernung und Himmelsrichtung. */
public final class NearCommand implements TabExecutor {

    private static final String[] COMPASS = {"S", "SO", "O", "NO", "N", "NW", "W", "SW"};

    private final CompanionPlugin plugin;
    private final AfkManager afk;

    public NearCommand(CompanionPlugin plugin, AfkManager afk) {
        this.plugin = plugin;
        this.afk = afk;
    }

    private boolean enabled() {
        return plugin.settings().raw().getBoolean("features.near", true);
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        if (!enabled()) {
            Msg.error(player, "Diese Funktion ist auf diesem Server deaktiviert.");
            return true;
        }
        int def = Math.max(1, plugin.settings().raw().getInt("social.near_radius", 100));
        int max = Math.max(def, plugin.settings().raw().getInt("social.near_max_radius", 500));
        int radius = def;
        if (args.length >= 1) {
            try {
                radius = Integer.parseInt(args[0].trim());
            } catch (NumberFormatException ex) {
                Msg.error(player, "<white><value></white> ist keine Zahl.", Msg.text("value", args[0]));
                return true;
            }
            if (radius < 1) {
                radius = 1;
            }
            if (radius > max && !player.isOp() && !player.hasPermission("mcsm.near.unlimited")) {
                radius = max;
                Msg.send(player, "<gray>Der Radius wurde auf <white><max></white> Blöcke begrenzt.</gray>",
                        Msg.number("max", max));
            }
        }
        Location origin = player.getLocation();
        List<Player> found = new ArrayList<>();
        double limitSq = (double) radius * radius;
        for (Player other : plugin.visiblePlayers(player)) {
            if (other.equals(player) || !other.getWorld().equals(player.getWorld())) {
                continue;
            }
            if (other.getLocation().distanceSquared(origin) <= limitSq) {
                found.add(other);
            }
        }
        if (found.isEmpty()) {
            Msg.send(player, "<gray>Im Umkreis von <white><radius></white> Blöcken ist niemand.</gray>",
                    Msg.number("radius", radius));
            return true;
        }
        found.sort((a, b) -> Double.compare(a.getLocation().distanceSquared(origin),
                b.getLocation().distanceSquared(origin)));
        Msg.send(player, "<gray>Im Umkreis von <white><radius></white> Blöcken: <white><count></white></gray>",
                Msg.number("radius", radius), Msg.number("count", found.size()));
        for (Player other : found) {
            long dist = Math.round(other.getLocation().distance(origin));
            String tail = afk.isAfk(other) ? " <yellow>[AFK]</yellow>" : "";
            Msg.send(player, "<dark_gray>•</dark_gray> <white><name></white> <gray>–</gray> "
                            + "<white><dist></white> <gray>Blöcke</gray> <dark_gray>(<dir>)</dark_gray>" + tail,
                    Msg.name("name", other),
                    Msg.number("dist", dist),
                    Msg.text("dir", direction(origin, other.getLocation())));
        }
        return true;
    }

    /** Grobe Himmelsrichtung vom Betrachter zum Ziel. */
    private static String direction(Location from, Location to) {
        double dx = to.getX() - from.getX();
        double dz = to.getZ() - from.getZ();
        if (dx == 0.0 && dz == 0.0) {
            return "hier";
        }
        double angle = Math.toDegrees(Math.atan2(dx, dz));
        int index = (int) Math.round(((angle % 360.0) + 360.0) % 360.0 / 45.0) % 8;
        return COMPASS[index];
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length == 1) {
            String prefix = args[0].toLowerCase(Locale.ROOT);
            for (String s : List.of("25", "50", "100", "250")) {
                if (s.startsWith(prefix)) {
                    out.add(s);
                }
            }
        }
        return out;
    }
}
