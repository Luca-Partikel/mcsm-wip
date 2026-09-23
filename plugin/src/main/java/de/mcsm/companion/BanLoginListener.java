package de.mcsm.companion;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.util.UUID;

import com.destroystokyo.paper.profile.PlayerProfile;
import io.papermc.paper.connection.PlayerConnection;
import io.papermc.paper.connection.PlayerLoginConnection;
import io.papermc.paper.event.connection.PlayerConnectionValidateLoginEvent;
import org.bukkit.BanEntry;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;

/**
 * Ersetzt den nüchternen Vanilla-Sperrbildschirm durch den mehrzeiligen deutschen Text aus der
 * Konfiguration – mit Grund, Sperrer, Sperrzeitpunkt und bei Zeitsperren der Restzeit.
 *
 * <p>Der Server weist gesperrte Verbindungen selbst ab; dieser Listener greift nur in die bereits
 * abgewiesene Anmeldung ein und tauscht die Nachricht aus. Abweisungen aus anderen Gründen
 * (Weißliste, voller Server) bleiben unangetastet. Gearbeitet wird mit
 * {@link PlayerConnectionValidateLoginEvent}; das ältere {@code PlayerLoginEvent} ist in Paper
 * veraltet und würde die Übersetzung mit -Werror scheitern lassen.</p>
 */
public final class BanLoginListener implements Listener {

    private final BanService service;

    public BanLoginListener(BanService service) {
        this.service = service;
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onValidateLogin(PlayerConnectionValidateLoginEvent event) {
        if (!service.banEnabled() || event.isAllowed()) {
            return;                                 // erlaubt oder Funktion aus: nichts zu tun
        }
        PlayerConnection connection = event.getConnection();

        UUID id = null;
        String name = null;
        if (connection instanceof PlayerLoginConnection login) {
            PlayerProfile profile = profileOf(login);
            if (profile != null) {
                id = profile.getId();
                name = profile.getName();
            }
        }

        BanEntry<PlayerProfile> entry = id == null ? null : service.findBan(id);
        if (entry == null && name != null) {
            entry = service.findBan(name);
        }
        if (entry != null) {
            event.kickMessage(service.banScreen(entry, name == null || name.isBlank() ? "?" : name));
            return;
        }

        InetSocketAddress socket = connection.getClientAddress();
        InetAddress addr = socket == null ? null : socket.getAddress();
        if (addr == null) {
            return;
        }
        BanEntry<InetAddress> ipEntry = service.ipBans().getBanEntry(addr);
        if (ipEntry != null) {
            event.kickMessage(service.ipScreen(ipEntry));
        }
    }

    /** Geprüftes Profil, sonst das ungeprüfte; null, wenn beides nicht verfügbar ist. */
    private static PlayerProfile profileOf(PlayerLoginConnection login) {
        try {
            PlayerProfile p = login.getAuthenticatedProfile();
            if (p != null) {
                return p;
            }
        } catch (RuntimeException ex) {
            // Vor der Anmeldung (Offline-Modus, Proxy) gibt es noch kein geprüftes Profil.
        }
        try {
            return login.getUnsafeProfile();
        } catch (RuntimeException ex) {
            return null;
        }
    }
}
