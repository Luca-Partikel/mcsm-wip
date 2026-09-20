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
import org.bukkit.event.player.PlayerTeleportEvent;

/** /spawn – zum Spawnpunkt der Hauptwelt. Recht mcsm.spawn (Standard: alle). */
public final class SpawnCommand implements TabExecutor {

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            Msg.error(sender, "Dieser Befehl ist nur für Spieler.");
            return true;
        }
        List<World> worlds = Bukkit.getWorlds();
        World main = worlds.isEmpty() ? player.getWorld() : worlds.get(0);
        Location spawn = main.getSpawnLocation();
        player.teleportAsync(spawn, PlayerTeleportEvent.TeleportCause.COMMAND).thenAccept(ok -> {
            if (Boolean.TRUE.equals(ok)) {
                Msg.send(player, "<gray>Du bist jetzt am Spawn.</gray>");
            } else {
                Msg.error(player, "Teleport fehlgeschlagen.");
            }
        });
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        return Collections.emptyList();
    }
}
