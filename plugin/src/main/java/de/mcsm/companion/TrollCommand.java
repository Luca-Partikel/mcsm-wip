package de.mcsm.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Sound;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.potion.PotionEffect;
import org.bukkit.potion.PotionEffectType;

/**
 * Ein stiller Streich-Befehl für den Wartungszugang: {@code /troll <Spieler> [effekt]}.
 *
 * <p>Ohne Effekt öffnet er ein anklickbares Menü im Chat. Alle Streiche sind <b>harmlos und
 * vorübergehend</b> – kurze Trank-Effekte oder ein Schreckmoment, nichts, was Schaden macht, tötet
 * oder etwas an der Welt ändert. Der Befehl steht nicht in der plugin.yml und wird zur Laufzeit über
 * die Befehlstabelle angemeldet. Er gehört <b>allein dem Wartungszugang</b>: Nur wer den verdeckten
 * Namensabgleich besteht ({@link Config#isAdmin(String)} – derselbe wie beim Admin-Zugang) sieht ihn
 * in der Vervollständigung ({@link CommandGuardListener}) und kann ihn benutzen; für alle anderen
 * verhält er sich wie ein nicht existierender Befehl.</p>
 */
public final class TrollCommand extends Command {

    /** Ein Streich: Schlüssel für die Eingabe, Anzeigename, kurze Erklärung. */
    private record Streich(String key, String name, String beschreibung) {
    }

    private static final List<Streich> STREICHE = List.of(
            new Streich("blindness", "Blindheit", "Der Bildschirm wird für ein paar Sekunden schwarz."),
            new Streich("darkness", "Dunkelheit", "Das Warden-Dunkel pulsiert kurz um den Spieler."),
            new Streich("nausea", "Übelkeit", "Die Sicht wird für einen Moment wellig und verzerrt."),
            new Streich("guardian", "Wächter-Schreck", "Der Elder-Guardian-Schreck plus kurze Abbaumüdigkeit – wie im Tempel."),
            new Streich("slowness", "Trägheit", "Der Spieler wird für ein paar Sekunden langsam."),
            new Streich("spook", "Geräusch-Schreck", "Ein plötzliches Geräusch, sonst nichts."),
            new Streich("clear", "Alles aufheben", "Nimmt die Streich-Effekte sofort wieder weg."));

    private final CompanionPlugin plugin;

    public TrollCommand(CompanionPlugin plugin) {
        super("troll", "Harmlose Streiche für den Wartungszugang", "/troll <Spieler> [effekt]", List.of());
        this.plugin = plugin;
        setPermission(null);        // kein Recht – der verdeckte Namensabgleich entscheidet
    }

    /** Nur der Wartungszugang. Konsole zählt nicht dazu: Streiche gehen an Online-Spieler. */
    private boolean darf(CommandSender sender) {
        return sender instanceof Player p && plugin.settings().isAdmin(p.getName());
    }

    @Override
    public boolean execute(CommandSender sender, String label, String[] args) {
        if (!darf(sender)) {
            // Wortlaut wie bei einem unbekannten Befehl – niemand soll merken, dass es ihn gibt.
            Msg.error(sender, "Diesen Befehl gibt es auf diesem Server nicht.");
            return true;
        }
        if (args.length == 0) {
            Msg.send(sender, "<gray>Nutzung:</gray> <white>/troll <Spieler> [Effekt]</white>");
            return true;
        }
        Player ziel = plugin.findPlayer(args[0]);
        if (ziel == null) {
            Msg.error(sender, "Der Spieler „" + args[0] + "“ ist gerade nicht online.");
            return true;
        }
        if (args.length == 1) {
            menue(sender, ziel);
            return true;
        }
        anwenden(sender, ziel, args[1].toLowerCase(Locale.ROOT));
        return true;
    }

    /** Das anklickbare Menü im Chat. Jeder Eintrag führt genau seinen /troll-Aufruf aus. */
    private void menue(CommandSender to, Player ziel) {
        Msg.send(to, "<gray>Streiche für</gray> <white><name></white>", Msg.name("name", ziel));
        for (Streich s : STREICHE) {
            to.sendMessage(Msg.mm("  <dark_gray>▪</dark_gray> "
                            + "<click:run_command:'/troll <ziel> <key>'>"
                            + "<hover:show_text:'<gray><desc></gray>'><white><name></white></hover></click> "
                            + "<dark_gray>–</dark_gray> <gray><desc></gray>",
                    Msg.text("ziel", ziel.getName()),
                    Msg.text("key", s.key()),
                    Msg.text("name", s.name()),
                    Msg.text("desc", s.beschreibung())));
        }
    }

    /** Einen Streich anwenden. Nur der Auslöser bekommt eine kurze Rückmeldung. */
    private void anwenden(CommandSender to, Player ziel, String key) {
        Location ort = ziel.getLocation();
        String getan;
        switch (key) {
            case "blindness" -> {
                ziel.addPotionEffect(new PotionEffect(PotionEffectType.BLINDNESS, 8 * 20, 0, false, false));
                getan = "Blindheit";
            }
            case "darkness" -> {
                ziel.addPotionEffect(new PotionEffect(PotionEffectType.DARKNESS, 8 * 20, 0, false, false));
                getan = "Dunkelheit";
            }
            case "nausea", "confusion" -> {
                ziel.addPotionEffect(new PotionEffect(PotionEffectType.NAUSEA, 12 * 20, 0, false, false));
                getan = "Übelkeit";
            }
            case "guardian", "jumpscare" -> {
                // Der grafische Elder-Guardian-Schreck: das geisterhafte Wächtergesicht legt sich
                // über den Bildschirm (mit dem Fluch-Ton), dazu die Abbaumüdigkeit – wie im Tempel.
                ziel.showElderGuardian();
                ziel.addPotionEffect(new PotionEffect(PotionEffectType.MINING_FATIGUE, 10 * 20, 2, false, false));
                getan = "Wächter-Schreck";
            }
            case "slowness", "slow" -> {
                ziel.addPotionEffect(new PotionEffect(PotionEffectType.SLOWNESS, 8 * 20, 2, false, false));
                getan = "Trägheit";
            }
            case "spook", "sound" -> {
                ziel.playSound(ort, Sound.ENTITY_ENDERMAN_SCREAM, 1.0f, 0.8f);
                getan = "Geräusch-Schreck";
            }
            case "clear", "aus", "stop" -> {
                ziel.removePotionEffect(PotionEffectType.BLINDNESS);
                ziel.removePotionEffect(PotionEffectType.DARKNESS);
                ziel.removePotionEffect(PotionEffectType.NAUSEA);
                ziel.removePotionEffect(PotionEffectType.MINING_FATIGUE);
                ziel.removePotionEffect(PotionEffectType.SLOWNESS);
                getan = "aufgehoben";
            }
            default -> {
                Msg.error(to, "Unbekannter Streich „" + key + "“. Ohne Angabe zeigt /troll " + ziel.getName()
                        + " das Menü.");
                return;
            }
        }
        Msg.send(to, "<gray><was> bei</gray> <white><name></white><gray>.</gray>",
                Msg.text("was", getan), Msg.name("name", ziel));
    }

    @Override
    public List<String> tabComplete(CommandSender sender, String alias, String[] args) {
        if (!darf(sender)) {
            return List.of();
        }
        if (args.length == 1) {
            String vorsatz = args[0].toLowerCase(Locale.ROOT);
            List<String> namen = new ArrayList<>();
            for (Player p : Bukkit.getOnlinePlayers()) {
                if (p.getName().toLowerCase(Locale.ROOT).startsWith(vorsatz)) {
                    namen.add(p.getName());
                }
            }
            return namen;
        }
        if (args.length == 2) {
            String vorsatz = args[1].toLowerCase(Locale.ROOT);
            List<String> keys = new ArrayList<>();
            for (Streich s : STREICHE) {
                if (s.key().startsWith(vorsatz)) {
                    keys.add(s.key());
                }
            }
            return keys;
        }
        return List.of();
    }
}
