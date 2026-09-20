package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.TextReplacementConfig;
import net.kyori.adventure.text.TranslatableComponent;
import net.kyori.adventure.text.TranslationArgument;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;

/**
 * Todesmeldungen: Vanilla-Ursache bleibt erhalten, wird aber grau eingefärbt, der Name des
 * Gestorbenen weiß hervorgehoben und ein Präfix vorangestellt.
 */
public final class DeathListener implements Listener {

    private static final PlainTextComponentSerializer PLAIN = PlainTextComponentSerializer.plainText();

    private final CompanionPlugin plugin;

    public DeathListener(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onDeath(PlayerDeathEvent event) {
        Config cfg = plugin.settings();
        if (!cfg.featDeath) {
            return;
        }
        Component original = event.deathMessage();
        if (original == null) {
            return;
        }
        Player victim = event.getPlayer();
        Component styled = restyle(original, victim.getName());
        event.deathMessage(Msg.mm(cfg.deathPrefix).append(styled));
    }

    static Component restyle(Component original, String victimName) {
        Component gray = original.color(NamedTextColor.GRAY);
        Component nameWhite = Component.text(victimName, NamedTextColor.WHITE);

        if (gray instanceof TranslatableComponent tc) {
            List<Component> args = new ArrayList<>();
            boolean changed = false;
            for (TranslationArgument arg : tc.arguments()) {
                Component c = arg.asComponent();
                if (victimName.equals(PLAIN.serialize(c))) {
                    args.add(nameWhite.style(c.style()).color(NamedTextColor.WHITE));
                    changed = true;
                } else {
                    args.add(c);
                }
            }
            if (changed) {
                return tc.arguments(args);
            }
        }
        return gray.replaceText(TextReplacementConfig.builder()
                .matchLiteral(victimName)
                .replacement(nameWhite)
                .build());
    }
}
