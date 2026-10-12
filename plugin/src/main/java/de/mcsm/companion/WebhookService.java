package de.mcsm.companion;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.logging.Level;

/**
 * Schickt einfache Server-Ereignisse (Beitritt, Verlassen, Tod, online/offline) an einen
 * Discord-Webhook. Alles läuft über einen eigenen Hintergrund-Thread und eine begrenzte Warteschlange
 * – der Hauptthread des Servers wird nie blockiert, und ein falscher oder überlasteter Webhook stört
 * den Betrieb nicht (alle Fehler werden still verschluckt). Die URL steht in der config.yml
 * ({@code discord_webhook}) und wird im Programm gesetzt.
 */
public final class WebhookService {

    private final CompanionPlugin plugin;
    private final BlockingQueue<String> warteschlange = new LinkedBlockingQueue<>(100);
    private volatile boolean laeuft = false;
    private Thread sender;
    private HttpClient client;

    public WebhookService(CompanionPlugin plugin) {
        this.plugin = plugin;
    }

    public void start() {
        if (laeuft) {
            return;
        }
        laeuft = true;
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(8)).build();
        sender = new Thread(this::schleife, "mcsm-webhook");
        sender.setDaemon(true);
        sender.start();
    }

    public void stop() {
        laeuft = false;
        if (sender != null) {
            sender.interrupt();
        }
    }

    /** Die konfigurierte Webhook-URL, oder leer. */
    private String url() {
        return plugin.settings().raw().getString("discord_webhook", "").trim();
    }

    /** Ist ein (plausibler) Discord-Webhook gesetzt? */
    public boolean aktiv() {
        return url().matches("https://\\S*discord(app)?\\.com/api/webhooks/\\S+");
    }

    // ------------------------------------------------------------------ Ereignisse
    public void beitritt(String name) {
        post("📥 **" + name + "** ist beigetreten.");
    }

    public void verlassen(String name) {
        post("📤 **" + name + "** hat den Server verlassen.");
    }

    public void tod(String nachricht) {
        post("💀 " + nachricht);
    }

    public void online() {
        post("🟢 Server ist **online**.");
    }

    /** Offline wird beim Herunterfahren noch schnell direkt gesendet (der Sender-Thread endet gleich). */
    public void offlineJetzt() {
        if (!aktiv()) {
            return;
        }
        sendeEinmal(baue("🔴 Server ist **offline**."), url());
    }

    // ------------------------------------------------------------------ Technik
    /** Eine Nachricht einreihen. Ist die Warteschlange voll, wird sie verworfen (kein Blockieren). */
    private void post(String content) {
        if (!aktiv() || content == null || content.isEmpty()) {
            return;
        }
        warteschlange.offer(baue(content));
    }

    private void schleife() {
        while (laeuft) {
            String json;
            try {
                json = warteschlange.take();
            } catch (InterruptedException ex) {
                break;
            }
            String ziel = url();
            if (ziel.isEmpty()) {
                continue;
            }
            int code = sendeEinmal(json, ziel);
            try {
                // Discord erlaubt nur ein paar Nachrichten je Sekunde – ein sanftes Grundlimit,
                // bei 429 etwas länger. Der genaue Retry-After-Wert ist für uns nebensächlich.
                Thread.sleep(code == 429 ? 2000 : 350);
            } catch (InterruptedException ex) {
                break;
            }
            if (code == 429) {
                warteschlange.offer(json);            // einmal nachreichen
            }
        }
    }

    /** Einen Versuch senden; Rückgabe HTTP-Status oder -1. Wirft nie. */
    private int sendeEinmal(String json, String ziel) {
        HttpClient c = client;
        if (c == null) {
            c = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(8)).build();
        }
        try {
            HttpRequest req = HttpRequest.newBuilder(URI.create(ziel))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                    .build();
            HttpResponse<Void> res = c.send(req, HttpResponse.BodyHandlers.discarding());
            return res.statusCode();
        } catch (IllegalArgumentException ex) {
            plugin.getLogger().log(Level.FINE, "Webhook-URL ungültig", ex);
            return -1;
        } catch (Exception ex) {                      // noqa – Netzfehler dürfen nichts stören
            return -1;
        }
    }

    /** JSON-Rumpf mit abgeschalteten Erwähnungen (kein @everyone etc. aus Spielernamen). */
    private static String baue(String content) {
        String text = content.length() > 1800 ? content.substring(0, 1800) : content;
        return "{\"content\":\"" + escape(text) + "\",\"allowed_mentions\":{\"parse\":[]}}";
    }

    private static String escape(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 16);
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            switch (ch) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (ch < 0x20) {
                        sb.append(String.format("\\u%04x", (int) ch));
                    } else {
                        sb.append(ch);
                    }
                }
            }
        }
        return sb.toString();
    }
}
