package de.mcsm.companion;

/**
 * Kennzahlen der Gruppe „Bann“ für den Manager: Anzahl Kontosperren, IP-Sperren, aktiver
 * Stummschaltungen und verwarnter Spieler.
 *
 * <p>{@link #appendJson(StringBuilder)} liefert die Zeilen für status.json im selben Stil wie
 * {@code ServerMetrics.appendJson(StringBuilder)} – mit abschließendem Komma, damit der
 * StatusWriter die Zeilen einfach einschieben kann.</p>
 */
public final class BanStatus {

    private final CompanionPlugin plugin;
    private final BanService service;

    BanStatus(CompanionPlugin plugin, BanService service) {
        this.plugin = plugin;
        this.service = service;
    }

    /** Anzahl gesperrter Konten (banned-players.json). */
    public int banCount() {
        try {
            return service.profileBans().getEntries().size();
        } catch (RuntimeException ex) {
            return 0;
        }
    }

    /** Anzahl gesperrter Verbindungen (banned-ips.json). */
    public int ipBanCount() {
        try {
            return service.ipBans().getEntries().size();
        } catch (RuntimeException ex) {
            return 0;
        }
    }

    /** Anzahl aktiver Stummschaltungen (mutes.yml). */
    public int muteCount() {
        return service.mutes().size();
    }

    /** Anzahl Spieler mit mindestens einer Verwarnung (warns.yml). */
    public int warnedCount() {
        return service.warns().playerCount();
    }

    /** Eine Zeile für das Protokoll bzw. für Übersichten im Spiel. */
    public String summary() {
        return banCount() + " Sperren, " + ipBanCount() + " IP-Sperren, "
                + muteCount() + " Stummschaltungen, " + warnedCount() + " verwarnte Spieler";
    }

    /** Zeilen für status.json; bei abgeschalteter Funktion wird nichts geschrieben. */
    public void appendJson(StringBuilder sb) {
        if (!plugin.settings().raw().getBoolean("features.ban", true)) {
            return;
        }
        sb.append("  \"moderation\": {\"bans\": ").append(banCount())
          .append(", \"ip_bans\": ").append(ipBanCount())
          .append(", \"mutes\": ").append(muteCount())
          .append(", \"warned\": ").append(warnedCount())
          .append("},\n");
    }
}
