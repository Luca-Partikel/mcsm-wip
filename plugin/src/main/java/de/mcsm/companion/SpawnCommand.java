package de.mcsm.companion;

import java.util.Collections;
import java.util.List;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabExecutor;
import org.bukkit.entity.Player;

/** /spawn – zum Spawnpunkt der Hauptwelt. Recht mcsm.spawn (Standard: alle). */
public final class SpawnCommand implements TabExecutor {

    private final CompanionPlugin plugin;

    public SpawnCommand(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        List<World> worlds = Bukkit.getWorlds();
        World main = worlds.isEmpty() ? player.getWorld() : worlds.get(0);
        Location spawn = main.getSpawnLocation();
        plugin.teleports().request(player, spawn, "spawn", "<gray>Du bist jetzt am Spawn.</gray>");
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return Collections.emptyList();
    }
}
