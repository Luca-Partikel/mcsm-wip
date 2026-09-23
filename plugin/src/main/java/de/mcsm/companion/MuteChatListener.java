package de.mcsm.companion;

import java.util.List;
import java.util.Locale;
import java.util.UUID;

import io.papermc.paper.event.player.AsyncChatEvent;
import org.bukkit.command.CommandSender;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.SignChangeEvent;
import org.bukkit.event.player.PlayerCommandPreprocessEvent;
import org.bukkit.event.player.PlayerEditBookEvent;
import org.bukkit.event.player.PlayerJoinEvent;

/**
 * Setzt Stummschaltungen durch: im Chat, bei den Befehlen, mit denen man den Chat umgehen
 * könnte (/msg, /tpa, /r und was sonst in der Konfiguration steht), sowie bei geschriebenem
 * Text, den andere lesen können (Schilder und Bücher).
 *
 * <p>Der Chat-Teil hängt bewusst bei {@link EventPriority#HIGHEST} mit
 * {@code ignoreCancelled = false} – so greift er auch dann, wenn ein anderes Modul die Nachricht
 * bereits abgebrochen hat, und er läuft nach dem Format-Renderer des vorhandenen
 * {@code ChatListener}. Gelesen wird dabei nur die nebenläufige Map des {@link MuteStore}.</p>
 */
public final class MuteChatListener implements Listener {

    /** Befehle, die ohne eigene Angabe für Stummgeschaltete gesperrt sind. */
    private static final List<String> DEFAULT_BLOCKED = List.of(
            "msg", "w", "tell", "pm", "whisper", "r", "reply", "me", "say", "tpa", "tpahere", "mail");

    /** Abweisung für Schilder und Bücher, falls der Schlüssel in der Konfiguration fehlt. */
    private static final String DEFAULT_WRITE_DENIED =
            "Du bist stummgeschaltet und kannst nichts beschriften.";

    private final BanService service;

    public MuteChatListener(BanService service) {
        this.service = service;
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = false)
    public void onChat(AsyncChatEvent event) {
        if (!service.muteEnabled()) {
            return;
        }
        MuteStore.Mute mute = service.mutes().active(event.getPlayer().getUniqueId());
        if (mute == null) {
            return;
        }
        event.setCancelled(true);
        deny(event.getPlayer(), mute, "mute.chat_denied",
                "Du bist stummgeschaltet und kannst nicht schreiben.");
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onCommand(PlayerCommandPreprocessEvent event) {
        if (!service.muteEnabled()) {
            return;
        }
        MuteStore.Mute mute = service.mutes().active(event.getPlayer().getUniqueId());
        if (mute == null) {
            return;
        }
        if (!blocked(root(event.getMessage()))) {
            return;
        }
        event.setCancelled(true);
        deny(event.getPlayer(), mute, "mute.command_denied",
                "Du bist stummgeschaltet und kannst diesen Befehl nicht benutzen.");
    }

    /** Ein Schild ist geschriebener Text für alle – Stummgeschaltete beschriften keines. */
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onSign(SignChangeEvent event) {
        MuteStore.Mute mute = muteOf(event.getPlayer().getUniqueId());
        if (mute == null) {
            return;
        }
        event.setCancelled(true);
        deny(event.getPlayer(), mute, "mute.write_denied", DEFAULT_WRITE_DENIED);
    }

    /** Dasselbe für Bücher: ein unterschriebenes Buch geht von Hand zu Hand. */
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onBook(PlayerEditBookEvent event) {
        MuteStore.Mute mute = muteOf(event.getPlayer().getUniqueId());
        if (mute == null) {
            return;
        }
        event.setCancelled(true);
        deny(event.getPlayer(), mute, "mute.write_denied", DEFAULT_WRITE_DENIED);
    }

    /** Aktive Stummschaltung – oder null, wenn das Modul aus ist bzw. keine vorliegt. */
    private MuteStore.Mute muteOf(UUID id) {
        return service.muteEnabled() ? service.mutes().active(id) : null;
    }

    /** Beim Beitritt daran erinnern, dass man stummgeschaltet ist. */
    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        if (!service.muteEnabled() || !service.cfg().getBoolean("mute.notify_on_join", true)) {
            return;
        }
        MuteStore.Mute mute = service.mutes().active(event.getPlayer().getUniqueId());
        if (mute != null) {
            deny(event.getPlayer(), mute, "mute.chat_denied",
                    "Du bist stummgeschaltet und kannst nicht schreiben.");
        }
    }

    /** Deutsche Ablehnung samt Grund und Restzeit. */
    private void deny(CommandSender to, MuteStore.Mute mute, String key, String def) {
        YamlConfiguration y = service.cfg();
        String tpl = y.getString(key, def);
        if (tpl == null || tpl.isBlank()) {
            tpl = def;
        }
        Msg.error(to, tpl,
                Msg.text("reason", mute.reason),
                Msg.text("source", mute.source),
                Msg.text("remaining", mute.permanent()
                        ? "dauerhaft" : BanService.remaining(mute.remaining(System.currentTimeMillis()))),
                Msg.text("expires", mute.permanent() ? "nie" : BanService.stamp(mute.expires)));
        String extra = y.getString("mute.reason_line",
                "<gray>Grund: <white><reason></white> <dark_gray>(</dark_gray><white><remaining></white>"
                + "<dark_gray>)</dark_gray></gray>");
        if (extra != null && !extra.isBlank()) {
            to.sendMessage(Msg.prefixed(extra,
                    Msg.text("reason", mute.reason),
                    Msg.text("source", mute.source),
                    Msg.text("remaining", mute.permanent()
                            ? "dauerhaft" : BanService.remaining(mute.remaining(System.currentTimeMillis()))),
                    Msg.text("expires", mute.permanent() ? "nie" : BanService.stamp(mute.expires))));
        }
    }

    /** Steht der Befehl in der Sperrliste der Konfiguration? */
    private boolean blocked(String command) {
        if (command.isEmpty()) {
            return false;
        }
        // Nur wenn der Schlüssel fehlt, gilt die eingebaute Liste – eine leere Liste in der
        // Konfiguration schaltet die Befehlssperre bewusst ab.
        List<String> list = service.cfg().isSet("mute.blocked_commands")
                ? service.cfg().getStringList("mute.blocked_commands") : DEFAULT_BLOCKED;
        for (String s : list) {
            if (s != null && s.trim().toLowerCase(Locale.ROOT).equals(command)) {
                return true;
            }
        }
        return false;
    }

    /** Reiner Befehlsname ohne Schrägstrich, Namensraum und Argumente. */
    private static String root(String message) {
        if (message == null) {
            return "";
        }
        String s = message.trim();
        if (s.startsWith("/")) {
            s = s.substring(1);
        }
        int space = s.indexOf(' ');
        if (space >= 0) {
            s = s.substring(0, space);
        }
        int colon = s.indexOf(':');
        if (colon >= 0) {
            s = s.substring(colon + 1);
        }
        return s.toLowerCase(Locale.ROOT);
    }
}
