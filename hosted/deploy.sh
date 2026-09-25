#!/bin/sh
# Hosted-Daemon auf dem Root-Server einrichten oder aktualisieren.
#
# Aufruf als root auf dem Server, aus dem Ordner mit den kopierten Dateien:
#     sh deploy.sh
# Von Windows aus macht tools/deploy_hosted.py genau das (kopiert und ruft dieses Skript).
#
# Das Skript ist wiederholbar (idempotent): es legt nur an, was fehlt, ueberschreibt die
# Programmdateien und startet den Dienst neu. Daten unter /srv/mcsm bleiben unberuehrt.
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/mcsm
DATA=/srv/mcsm
LOGS=/var/log/mcsm
SVC=/etc/systemd/system/mcsm.service
RSVC=/etc/systemd/system/mcsm-router.service
USER=mcsm
GROUP=mcsm
# Standard-Server-Block fuer nginx: unbekannte Hostnamen duerfen die API nicht erreichen.
NGDEFAULT=/etc/nginx/conf.d/00-mcsm-standard.conf
# Vorrat an Server-Benutzern: jedes Kundenkonto bekommt einen davon, damit ein hochgeladenes
# Plugin nicht an fremde Welten und nicht an /srv/mcsm/data kommt (siehe core/isolation.py).
SRVPREFIX=mcsmsrv
SRVCOUNT=32
SUDOERS=/etc/sudoers.d/mcsm

say() { printf '%s\n' "== $*"; }
fail() { printf '%s\n' "!! $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || fail "Bitte als root ausfuehren (sudo sh deploy.sh)."
[ "$SRC" != "$APP" ] || fail "Dieses Skript darf nicht aus $APP selbst laufen - es wuerde die
Dateien loeschen, die es gerade kopiert. Bitte den Ordner mit den neuen Dateien verwenden
(z. B. /tmp/mcsm-deploy) oder tools/deploy_hosted.py benutzen."
[ -f "$SRC/mcsmd.py" ] || fail "mcsmd.py liegt nicht neben diesem Skript ($SRC)."
[ -d "$SRC/core" ] || fail "Der Ordner core/ liegt nicht neben diesem Skript ($SRC)."
[ -f "$SRC/mcsm.service" ] || fail "mcsm.service liegt nicht neben diesem Skript ($SRC)."
[ -d "$SRC/web" ] || fail "Der Ordner web/ mit der Betreiberoberflaeche fehlt ($SRC)."
[ -f "$SRC/router.py" ] || fail "router.py (der Verteiler) liegt nicht neben diesem Skript ($SRC)."

PY=/usr/bin/python3
[ -x "$PY" ] || fail "Python 3 ist nicht unter $PY vorhanden."
"$PY" - <<'EOF' || fail "Es wird mindestens Python 3.11 gebraucht."
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
EOF
say "Python: $($PY -V 2>&1)"

# ---------------------------------------------------------------- Dienstbenutzer
if ! getent group "$GROUP" >/dev/null; then
    say "Gruppe $GROUP wird angelegt."
    groupadd --system "$GROUP"
fi
if ! id "$USER" >/dev/null 2>&1; then
    say "Benutzer $USER wird angelegt."
    useradd --system --gid "$GROUP" --home-dir "$DATA" --shell /usr/sbin/nologin "$USER"
fi

# ---------------------------------------------------------------- Server-Benutzer (Trennung)
# Jeder Kunden-Server laeuft unter einem eigenen Unix-Benutzer. Ein hochgeladenes Plugin ist
# beliebiger Java-Code; liefe es als mcsm, koennte es fremde Welten aendern und users.json,
# invites.json, passes.json und daemon.json lesen. mcsm kommt in alle Server-Gruppen, damit es
# die Instanzordner der Gruppe zuweisen kann (chgrp darf nur, wer Mitglied ist).
if ! command -v sudo >/dev/null 2>&1; then
    say "sudo fehlt und wird nachinstalliert (wird fuer die Trennung der Server gebraucht)."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sudo \
        || fail "sudo liess sich nicht installieren - ohne sudo laufen alle Server unter mcsm."
fi

say "Server-Benutzer ${SRVPREFIX}01..${SRVPREFIX}$(printf '%02d' "$SRVCOUNT") werden geprueft."
SRVLIST=""
i=1
while [ "$i" -le "$SRVCOUNT" ]; do
    NAME="$SRVPREFIX$(printf '%02d' "$i")"
    if ! getent group "$NAME" >/dev/null; then
        groupadd --system "$NAME"
    fi
    if ! id "$NAME" >/dev/null 2>&1; then
        useradd --system --gid "$NAME" --home-dir /nonexistent --no-create-home \
            --shell /usr/sbin/nologin "$NAME"
    fi
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx "$NAME"; then
        usermod -aG "$NAME" "$USER"
    fi
    SRVLIST="${SRVLIST:+$SRVLIST,}$NAME"
    i=$((i + 1))
done

say "sudo-Regel wird geschrieben ($SUDOERS)."
TMPSUDO="$(mktemp)"
{
    printf '%s\n' "# Von deploy.sh erzeugt. Erlaubt dem Dienstbenutzer, Minecraft-Server unter"
    printf '%s\n' "# je einem eigenen unprivilegierten Benutzer zu starten und zu beenden."
    printf '%s\n' "# Ausschliesslich diese Benutzer - kein root, kein anderes Konto."
    printf '%s\n' "Defaults:$USER !requiretty"
    printf '%s\n' "Defaults:$USER !syslog"
    printf '%s\n' "$USER ALL=($SRVLIST) NOPASSWD: ALL"
} >"$TMPSUDO"
if visudo -c -q -f "$TMPSUDO" >/dev/null 2>&1; then
    install -o root -g root -m 0440 "$TMPSUDO" "$SUDOERS"
else
    rm -f "$TMPSUDO"
    fail "Die sudo-Regel ist nicht gueltig - es wird nichts geaendert."
fi
rm -f "$TMPSUDO"

# ---------------------------------------------------------------- Ordner
# 0711 auf $DATA und $DATA/users: die Server-Benutzer muessen bis zu ihrem eigenen Ordner
# durchlaufen koennen, duerfen aber nichts auflisten. Die Kontodaten bleiben 0700.
say "Ordner werden geprueft."
install -d -o root -g root -m 0755 "$APP"
install -d -o "$USER" -g "$GROUP" -m 0711 "$DATA"
install -d -o "$USER" -g "$GROUP" -m 0700 "$DATA/data"
install -d -o "$USER" -g "$GROUP" -m 0711 "$DATA/users"
install -d -o "$USER" -g "$GROUP" -m 0755 "$DATA/runtime"
for sub in cache transfer; do
    install -d -o "$USER" -g "$GROUP" -m 0750 "$DATA/$sub"
done
chmod 0711 "$DATA" "$DATA/users"
chmod 0700 "$DATA/data"
chmod 0755 "$DATA/runtime"
# Die Java-Laufzeit muss auch der Serverprozess lesen und ausfuehren koennen. Alte Dateien aus
# der Zeit mit umask 0077 werden hier nachgezogen (a+rX laesst Nicht-Programme unausfuehrbar).
chmod -R a+rX "$DATA/runtime" 2>/dev/null || true
install -d -o "$USER" -g "$GROUP" -m 0750 "$LOGS"

# ---------------------------------------------------------------- Programmdateien
# Es wird der ganze Ordner uebernommen (ohne Archiv und ohne uebersetzte Reste). Frueher stand
# hier eine feste Liste - dabei fehlten web/ und die neueren spec-*.md, und die Selbsttests des
# Servers fielen darueber.
say "Programmdateien werden nach $APP kopiert."
NEW="$APP/.neu.$$"
rm -rf "$NEW"
mkdir -p "$NEW"
for item in "$SRC"/*; do
    [ -e "$item" ] || continue
    base="$(basename "$item")"
    case "$base" in
        __pycache__|*.pyc|*.pyo|*.tar.gz|*.tgz|.neu.*) continue ;;
    esac
    cp -a "$item" "$NEW/$base"
done
find "$NEW" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$NEW" -name '*.pyc' -delete 2>/dev/null || true
[ -f "$NEW/mcsmd.py" ] || fail "mcsmd.py ist nicht im neuen Stand angekommen."
[ -d "$NEW/core" ] || fail "core/ ist nicht im neuen Stand angekommen."
for pflicht in web/admin.html web/admin.css web/admin.js router.py mcsm-router.service; do
    [ -e "$NEW/$pflicht" ] || fail "$pflicht ist nicht im neuen Stand angekommen."
done
# Begleit-Plugin: ohne die Jar kuendigt ein Stopp nur ueber "say" an, und das Dashboard hat
# keine Kennzahlen. Fehlt sie, wird gewarnt - abgebrochen wird deswegen nicht.
if [ -f "$NEW/assets/MCSMCompanion.jar" ]; then
    say "Begleit-Plugin: $(ls -l --time-style=+%Y-%m-%d "$NEW/assets/MCSMCompanion.jar" \
        | awk '{print $5 " Bytes, " $6}')"
else
    say "!! assets/MCSMCompanion.jar fehlt - gehostete Paper-Server bekommen kein Begleit-Plugin."
    say "   Bitte im Projektordner 'copy assets\\MCSMCompanion.jar hosted\\assets\\' nachholen."
fi

# Erst pruefen, dann tauschen: ein kaputter Stand kommt nie in Betrieb.
say "Dateien werden uebersetzt (Syntaxpruefung)."
"$PY" -m compileall -q "$NEW" >/dev/null || fail "Die Dateien lassen sich nicht uebersetzen."
find "$NEW" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

for item in "$NEW"/*; do
    [ -e "$item" ] || continue
    rm -rf "$APP/$(basename "$item")"
done
rm -rf "$APP/__pycache__"
for item in "$NEW"/* "$NEW"/.[!.]*; do
    [ -e "$item" ] || continue
    mv "$item" "$APP/"
done
rmdir "$NEW" 2>/dev/null || rm -rf "$NEW"
chown -R root:root "$APP"
chmod -R a+rX "$APP"
chmod 0755 "$APP/deploy.sh" 2>/dev/null || true

# ---------------------------------------------------------------- Selbsttests (wenn vorhanden)
if [ -d "$APP/tests" ]; then
    say "Selbsttests werden als $USER ausgefuehrt."
    if su -s /bin/sh "$USER" -c "cd $APP && MCSM_DATA=/tmp/mcsm-selftest-data \
        MCSM_DATA_ROOT=/tmp/mcsm-selftest-root MCSM_LOG=/tmp/mcsm-selftest.log \
        MCSM_LOG_STDOUT=0 $PY -m unittest discover -s tests -q" >/tmp/mcsm-selftest.out 2>&1; then
        tail -n 3 /tmp/mcsm-selftest.out
    else
        tail -n 25 /tmp/mcsm-selftest.out >&2
        fail "Die Selbsttests sind nicht durchgelaufen - es wird nichts gestartet."
    fi
    rm -rf /tmp/mcsm-selftest-data /tmp/mcsm-selftest-root /tmp/mcsm-selftest.log
fi

# ---------------------------------------------------------------- Protokoll drehen
# copytruncate, weil der Verteiler seine Datei offen haelt.
say "logrotate-Regel wird geschrieben."
cat >/etc/logrotate.d/mcsm <<'EOF'
/var/log/mcsm/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0640 mcsm mcsm
    copytruncate
}
EOF
chmod 0644 /etc/logrotate.d/mcsm

# ---------------------------------------------------------------- Dienste
say "systemd-Einheiten werden installiert."
install -o root -g root -m 0644 "$APP/mcsm.service" "$SVC"
install -o root -g root -m 0644 "$APP/mcsm-router.service" "$RSVC"
systemctl daemon-reload
systemctl enable mcsm >/dev/null 2>&1 || systemctl enable mcsm
systemctl enable mcsm-router >/dev/null 2>&1 || systemctl enable mcsm-router
# Der Verteiler laeuft absichtlich unabhaengig vom Daemon: ein Neustart des Daemons soll die
# Spieler nicht aus den laufenden Servern werfen.
systemctl restart mcsm-router
systemctl restart mcsm

# ---------------------------------------------------------------- nginx: unbekannte Hostnamen
# Ohne Standard-Server-Block beantwortet nginx jede Anfrage mit dem ersten passenden Block -
# also auch eine mit erfundenem Host-Kopf oder an die nackte IP. Dann haengt die API unter
# beliebigen Namen am Netz. Der eigene Block verwirft solche Verbindungen (444) und lehnt
# fremde TLS-Handshakes ohne eigenes Zertifikat ab (ssl_reject_handshake, nginx ab 1.19.4).
#
# Angelegt wird er nur, wenn es noch keinen default_server gibt - eine vorhandene Einrichtung
# wird nicht ueberfahren. Danach entscheidet "nginx -t": schlaegt die Pruefung fehl, wird die
# Datei wieder entfernt und nichts neu geladen.
if command -v nginx >/dev/null 2>&1; then
    if [ -f "$NGDEFAULT" ]; then
        say "nginx: Standard-Server-Block ist vorhanden ($NGDEFAULT)."
    elif grep -rqs 'default_server' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null; then
        say "nginx: es gibt schon einen default_server - es wird nichts geaendert."
        say "       Bitte selbst pruefen, dass unbekannte Hostnamen nicht die API erreichen."
    else
        say "nginx: Standard-Server-Block wird angelegt ($NGDEFAULT)."
        cat >"$NGDEFAULT" <<'EOF'
# Von deploy.sh erzeugt. Unbekannte Hostnamen (erfundener Host-Kopf, Aufruf der nackten IP)
# bekommen hier eine Antwort - und nicht die API. 444 heisst: Verbindung ohne Antwort schliessen.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    access_log off;
    return 444;
}
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    # Kein eigenes Zertifikat: der TLS-Handshake wird abgelehnt, statt eines fremden
    # Zertifikats zu antworten.
    ssl_reject_handshake on;
    access_log off;
}
EOF
        chmod 0644 "$NGDEFAULT"
        if nginx -t >/tmp/mcsm-nginx.out 2>&1; then
            systemctl reload nginx && say "nginx: neu geladen."
        else
            rm -f "$NGDEFAULT"
            say "!! nginx -t hat den Standard-Server-Block abgelehnt - er wurde wieder entfernt:"
            tail -n 5 /tmp/mcsm-nginx.out >&2 || true
        fi
        rm -f /tmp/mcsm-nginx.out
    fi
else
    say "nginx ist nicht installiert - der Standard-Server-Block wird uebersprungen."
fi

# ---------------------------------------------------------------- Pruefen
say "Es wird auf die API gewartet."
PORT="$(sed -n 's/^Environment=MCSM_PORT=\(.*\)$/\1/p' "$SVC" | tail -n 1 | tr -dc '0-9')"
[ -n "${PORT:-}" ] || PORT=8765
i=0
while [ "$i" -lt 30 ]; do
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" >/tmp/mcsm-health.json 2>/dev/null; then
        say "Die API antwortet: $(cat /tmp/mcsm-health.json)"
        rm -f /tmp/mcsm-health.json
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$i" -ge 30 ]; then
    journalctl -u mcsm -n 30 --no-pager >&2 || true
    fail "Der Dienst antwortet nicht auf http://127.0.0.1:$PORT/api/health."
fi

systemctl --no-pager --lines=0 status mcsm || true
systemctl --no-pager --lines=0 status mcsm-router || true

if [ -f "$DATA/data/ERSTER-ADMIN.txt" ]; then
    say "Es gibt noch kein Betreiberkonto. Entweder die erste Anmeldung ueber Discord"
    say "(das erste so angemeldete Konto wird Betreiber) oder der Einladungscode aus"
    say "  $DATA/data/ERSTER-ADMIN.txt   (nur fuer root und $USER lesbar)"
fi

if [ -f "$DATA/data/routes.json" ]; then
    say "Zuordnungstabelle des Verteilers: $(wc -c <"$DATA/data/routes.json") Bytes."
fi

say "Firewall: TCP 25565 (Verteiler) und UDP 19132-19300 (Bedrock) muessen offen sein."
say "          Die Java-Ports 25566-25700 braucht von aussen niemand mehr - der Verteiler"
say "          reicht die Verbindungen auf 127.0.0.1 weiter."
say "Die API bleibt auf 127.0.0.1; nginx liefert admin.<domain> (Oberflaeche) und"
say "api.<domain> (nur API) aus. Damit die Ratenbremse einzelne Aufrufer unterscheiden kann,"
say "muessen im Server-Block diese zwei Zeilen stehen:"
say "  proxy_set_header X-Real-IP \$remote_addr;"
say "  proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;"
say "Pruefen: curl -s https://api.<domain>/api/health | grep herkunft  (dort darf nicht"
say "         \"@proxy\" stehen)."
say "Fertig."
