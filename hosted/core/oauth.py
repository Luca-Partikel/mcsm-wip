"""Anmeldung über Discord für den Hosted-Modus (OAuth2, Authorization Code Flow).

Nur Standardbibliothek (``urllib``), Python 3.11. Das Modul kennt drei Wege in die Anmeldung:

* ``admin``  – die Weboberfläche unter ``https://admin.arcardia-nexus.de``
* ``app``    – das Programm auf dem PC, Rückleitung über ``https://api.arcardia-nexus.de``
* ``lokal``  – das Programm auf dem PC mit Rückleitung direkt auf ``http://127.0.0.1:<port>/cloud-callback``
  (bevorzugt, weil das Sitzungstoken dann ohne Zwischenablage im Programm landet)

Der Umfang (scope) ist ausschließlich ``identify``: Discord-Kennung, Name und Avatar. **Keine
E-Mail-Adresse**, keine Serverlisten, keine Freundeslisten. Das Zugriffstoken von Discord wird
nur für den einen Abruf von ``/users/@me`` benutzt, danach widerrufen und weggeworfen – es wird
nirgends gespeichert und nirgends protokolliert. Dasselbe gilt für das Anwendungsgeheimnis: Es
wird einmal aus ``/srv/mcsm/data/discord_secret`` gelesen, im Speicher gehalten und kommt in
keiner Meldung und in keinem Protokoll vor.

Fehlen die Zugangsdaten, wirft nur der Anmeldeweg einen Fehler – der Rest des Dienstes läuft
weiter (``eingerichtet()`` sagt, woran man ist, ``startmeldung()`` liefert einen Satz fürs
Protokoll).

Die Zustandswerte (state) und die Abholcodes liegen **nur im Arbeitsspeicher**. Ein Neustart des
Dienstes macht laufende Anmeldungen ungültig; das ist gewollt und wird dem Benutzer als
verständlicher Satz gemeldet.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from . import store_hosted, users

# --------------------------------------------------------------------------- Festwerte

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
IDENTITY_URL = "https://discord.com/api/v10/users/@me"
REVOKE_URL = "https://discord.com/api/v10/oauth2/token/revoke"
CDN_BASE = "https://cdn.discordapp.com"

# Nur dieser Umfang wird angefordert. Mehr brauchen wir nicht, mehr wollen wir nicht.
SCOPE = "identify"

USER_AGENT = "MinecraftServerManager/1.0 (+https://arcardia-nexus.de)"
HTTP_TIMEOUT = 10.0                  # Sekunden je Anfrage an Discord
MAX_ANTWORT_BYTES = 64 * 1024        # mehr schickt Discord für /users/@me nie

FLOW_ADMIN = "admin"
FLOW_APP = "app"
FLOW_LOKAL = "lokal"
FLOWS = (FLOW_ADMIN, FLOW_APP, FLOW_LOKAL)

REDIRECTS = {
    FLOW_ADMIN: "https://admin.arcardia-nexus.de/auth/discord/callback",
    FLOW_APP: "https://api.arcardia-nexus.de/auth/discord/callback",
}
LOCAL_HOST = "127.0.0.1"
LOCAL_PATH = "/cloud-callback"
LOCAL_PORT_MIN = 1024
LOCAL_PORT_MAX = 65535

# Gültigkeit eines Zustandswerts. Discord-Anmeldungen dauern selten länger als eine Minute;
# zehn Minuten sind großzügig und trotzdem kurz genug.
STATE_TTL_SECONDS = 600
STATES_MAX = 500                     # Obergrenze, damit niemand den Speicher volllaufen lässt

# Abholcode für das Programm auf dem PC (Notweg, wenn die Rückleitung auf 127.0.0.1 nicht geht).
ABHOL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # ohne I, O, 0, 1
ABHOL_LEN = 12
ABHOL_GRUPPE = 4
ABHOL_TTL_SECONDS = 300
ABHOL_MAX = 200
ABHOL_FEHLVERSUCHE_MAX = 10          # danach werden alle offenen Abholcodes verworfen
ABHOL_FENSTER_SECONDS = 300

CLIENT_ID_FILE = "discord_client_id"
SECRET_FILE = "discord_secret"
# Pfad der Zugangsdaten; sonst der Datenordner (MCSM_DATA bzw. /srv/mcsm/data).
CREDENTIALS_ENV = "MCSM_DISCORD_DIR"

AVATAR_SIZE = 128

_CLIENT_ID_RE = re.compile(r"[0-9]{15,22}")
_SECRET_RE = re.compile(r"[A-Za-z0-9_.\-]{16,200}")
_CODE_RE = re.compile(r"[A-Za-z0-9_.\-]{8,256}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]{8,512}")
_SNOWFLAKE_RE = re.compile(r"[0-9]{15,22}")
_AVATAR_RE = re.compile(r"(?:a_)?[0-9a-f]{32}")
_STATE_RE = re.compile(r"[A-Za-z0-9_\-]{16,128}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ABHOL_RE = re.compile(r"[A-HJ-NP-Z2-9]{%d}" % ABHOL_LEN)


# --------------------------------------------------------------------------- Fehler

class OAuthFehler(Exception):
    """Fehler der Discord-Anmeldung – mit fertigem deutschem Satz und HTTP-Status."""

    status = 400

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        if status is not None:
            self.status = int(status)


class OAuthNichtEingerichtet(OAuthFehler):
    """Client-ID oder Geheimnis fehlen auf dem Server."""
    status = 501


class OAuthZustandFehler(OAuthFehler):
    """Der Zustandswert (state) fehlt, ist abgelaufen oder wurde schon benutzt."""
    status = 400


class OAuthAbgebrochen(OAuthFehler):
    """Der Benutzer hat die Anmeldung bei Discord abgebrochen."""
    status = 400


class DiscordFehler(OAuthFehler):
    """Discord ist nicht erreichbar oder antwortet unerwartet."""
    status = 502


class EinladungNoetig(OAuthFehler):
    """Für dieses Discord-Konto gibt es noch kein Konto und es liegt kein Einladungscode vor."""
    status = 403


class KontoGesperrt(OAuthFehler):
    """Das verknüpfte Konto ist gesperrt."""
    status = 403


# --------------------------------------------------------------------------- Ergebnisobjekte

class Identitaet(NamedTuple):
    """Was wir von Discord übernehmen – und sonst nichts."""

    discord_id: str          # Kennung („Snowflake“), nur Ziffern
    benutzername: str        # eindeutiger Discord-Name (``username``)
    anzeigename: str         # ``global_name``, sonst der Benutzername
    avatar_url: str          # Bildadresse beim Discord-CDN (oder Standardbild)

    def as_dict(self) -> dict:
        return {"discord_id": self.discord_id, "benutzername": self.benutzername,
                "anzeigename": self.anzeigename, "avatar_url": self.avatar_url}


class Ergebnis(NamedTuple):
    """Abgeschlossener Discord-Ablauf samt der Angaben, die beim Start mitgegeben wurden."""

    identitaet: Identitaet
    flow: str
    invite_code: str
    port: int
    abhol_hash: str


class Konto(NamedTuple):
    """Ergebnis der Kontozuordnung."""

    user: dict
    neu: bool                # Konto wurde gerade angelegt
    admin_vergeben: bool     # dieses Konto wurde zum Betreiber gemacht


class Anmeldung(NamedTuple):
    """Fertige Anmeldung: Konto und frisches Sitzungstoken."""

    user: dict
    token: str
    expires_at: int
    neu: bool
    admin_vergeben: bool

    def as_dict(self) -> dict:
        """Antwort für die API – das Token steht hier drin, sonst nirgends."""
        return {"token": self.token, "expires_at": self.expires_at,
                "user": users.public_user(self.user), "neues_konto": self.neu,
                "admin_vergeben": self.admin_vergeben}


class Zugangsdaten(NamedTuple):
    """Client-ID und Anwendungsgeheimnis. Das Geheimnis taucht in keiner Textausgabe auf."""

    client_id: str
    secret: str

    def __repr__(self) -> str:           # niemals das Geheimnis zeigen
        return f"Zugangsdaten(client_id={self.client_id!r}, secret=<geheim>)"

    __str__ = __repr__


class _Zustand(NamedTuple):
    """Ein offener Anmeldevorgang (nur im Arbeitsspeicher)."""

    state: str
    flow: str
    invite_code: str
    port: int
    abhol_hash: str
    created_at: int
    expires_at: int


class _Abholung(NamedTuple):
    """Ein Sitzungstoken, das das Programm auf dem PC noch abholen darf."""

    code: str
    inhalt: dict
    claim_hash: str
    created_at: int
    expires_at: int


# --------------------------------------------------------------------------- Hilfsmittel

_creds_lock = threading.RLock()
_creds: Zugangsdaten | None = None
_creds_fehler = ""
_creds_quelle = ""

_states_lock = threading.RLock()
_states: dict[str, _Zustand] = {}

_abhol_lock = threading.RLock()
_abholungen: dict[str, _Abholung] = {}
_fehlversuche: list[int] = []


def _now(now: int | None = None) -> int:
    return store_hosted.now() if now is None else int(now)


def _sauber(text, grenze: int = 200) -> str:
    """Fremdtext für eine Fehlermeldung entschärfen: keine Steuerzeichen, gekürzt, ohne Geheimnis."""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()
    value = re.sub(r"\s+", " ", value)
    geheim = ""
    with _creds_lock:
        if _creds is not None:
            geheim = _creds.secret
    if geheim and geheim in value:
        value = value.replace(geheim, "<geheim>")
    return value[:grenze]


# --------------------------------------------------------------------------- Zugangsdaten

def credentials_dir() -> pathlib.Path:
    """Ordner mit ``discord_client_id`` und ``discord_secret``.

    Vorrang hat ``MCSM_DISCORD_DIR``; sonst der normale Datenordner. So laufen die Selbsttests
    ohne die echten Zugangsdaten.
    """
    raw = (os.environ.get(CREDENTIALS_ENV) or "").strip()
    return pathlib.Path(raw) if raw else store_hosted.data_dir()


def credentials_paths() -> tuple[pathlib.Path, pathlib.Path]:
    """Die beiden Dateipfade (Client-ID, Geheimnis)."""
    ordner = credentials_dir()
    return ordner / CLIENT_ID_FILE, ordner / SECRET_FILE


def _lese_datei(path: pathlib.Path, was: str) -> str:
    """Erste Zeile einer Zugangsdatei lesen. Fehler werden als deutscher Satz weitergegeben."""
    try:
        rohdaten = path.read_bytes()
    except FileNotFoundError:
        raise OAuthNichtEingerichtet(
            f"Die Anmeldung über Discord ist auf diesem Server noch nicht eingerichtet: "
            f"Die Datei „{path.name}“ mit {was} fehlt im Ordner „{path.parent}“.") from None
    except PermissionError:
        raise OAuthNichtEingerichtet(
            f"Die Datei „{path.name}“ mit {was} ist für den Dienstbenutzer nicht lesbar. "
            f"Besitzer und Rechte prüfen (Besitzer „mcsm“, Rechte 600).") from None
    except OSError as exc:
        raise OAuthNichtEingerichtet(
            f"Die Datei „{path.name}“ mit {was} kann nicht gelesen werden: {_sauber(exc)}") from None
    try:
        text = rohdaten.decode("utf-8")
    except UnicodeDecodeError:
        raise OAuthNichtEingerichtet(
            f"Die Datei „{path.name}“ mit {was} ist keine Textdatei in UTF-8.") from None
    zeile = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not zeile:
        raise OAuthNichtEingerichtet(
            f"Die Datei „{path.name}“ mit {was} ist leer.")
    return zeile


def _lade_zugangsdaten() -> Zugangsdaten:
    """Beide Dateien lesen und den Inhalt prüfen. Löst OAuthNichtEingerichtet aus."""
    id_path, secret_path = credentials_paths()
    client_id = _lese_datei(id_path, "der Client-ID")
    secret = _lese_datei(secret_path, "dem Anwendungsgeheimnis")
    if not _CLIENT_ID_RE.fullmatch(client_id):
        raise OAuthNichtEingerichtet(
            f"Die Client-ID in „{id_path.name}“ sieht nicht wie eine Discord-Kennung aus "
            f"(erwartet werden 15 bis 22 Ziffern).")
    if not _SECRET_RE.fullmatch(secret):
        # Absichtlich ohne den gelesenen Wert: das Geheimnis darf nirgends auftauchen.
        raise OAuthNichtEingerichtet(
            f"Das Anwendungsgeheimnis in „{secret_path.name}“ hat ein unerwartetes Format. "
            f"Erwartet werden 16 bis 200 Zeichen aus Buchstaben, Ziffern, Punkt, "
            f"Unterstrich und Bindestrich – ohne Leerzeichen.")
    return Zugangsdaten(client_id, secret)


def zugangsdaten(*, neu_lesen: bool = False) -> Zugangsdaten:
    """Zugangsdaten liefern – einmal gelesen, danach aus dem Speicher.

    Wechselt der Ordner (Selbsttests), wird automatisch neu gelesen.
    """
    global _creds, _creds_fehler, _creds_quelle
    quelle = str(credentials_dir())
    with _creds_lock:
        if neu_lesen or quelle != _creds_quelle:
            _creds, _creds_fehler, _creds_quelle = None, "", quelle
        if _creds is not None:
            return _creds
        if _creds_fehler:
            raise OAuthNichtEingerichtet(_creds_fehler)
        try:
            _creds = _lade_zugangsdaten()
        except OAuthNichtEingerichtet as exc:
            _creds_fehler = exc.message
            raise
        return _creds


def eingerichtet() -> bool:
    """Ob die Anmeldung über Discord benutzbar ist (ohne Ausnahme, für Übersichten)."""
    try:
        zugangsdaten()
        return True
    except OAuthNichtEingerichtet:
        return False


def vergessen() -> None:
    """Gelesene Zugangsdaten aus dem Speicher werfen (nach dem Austauschen der Dateien)."""
    global _creds, _creds_fehler, _creds_quelle
    with _creds_lock:
        _creds, _creds_fehler, _creds_quelle = None, "", ""


def startmeldung() -> str:
    """Ein Satz fürs Protokoll beim Start – nennt nie das Geheimnis."""
    id_path, secret_path = credentials_paths()
    try:
        daten = zugangsdaten()
    except OAuthNichtEingerichtet as exc:
        return (f"Discord-Anmeldung nicht aktiv: {exc.message} "
                f"Der Dienst läuft trotzdem; die Anmeldung mit Einladungscode bleibt möglich.")
    hinweise = []
    if os.name == "posix":
        for path in (id_path, secret_path):
            try:
                modus = path.stat().st_mode
            except OSError:
                continue
            if modus & 0o077:
                hinweise.append(f"„{path.name}“ ist auch für andere Benutzer lesbar – "
                                f"bitte auf 600 setzen")
    zusatz = (" Achtung: " + "; ".join(hinweise) + ".") if hinweise else ""
    return (f"Discord-Anmeldung aktiv (Client-ID {daten.client_id}, Umfang „{SCOPE}“, "
            f"Zugangsdaten aus „{id_path.parent}“).{zusatz}")


# --------------------------------------------------------------------------- Rückleitungen

def clean_port(raw) -> int:
    """Port der lokalen Rückleitung prüfen."""
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise OAuthFehler("Der Port der lokalen Rückleitung muss eine ganze Zahl sein.") from None
    if not LOCAL_PORT_MIN <= port <= LOCAL_PORT_MAX:
        raise OAuthFehler(f"Der Port der lokalen Rückleitung muss zwischen {LOCAL_PORT_MIN} "
                          f"und {LOCAL_PORT_MAX} liegen.")
    return port


def lokale_rueckleitung(port) -> str:
    """``http://127.0.0.1:<port>/cloud-callback`` – die Rückleitung ins Programm auf dem PC."""
    return f"http://{LOCAL_HOST}:{clean_port(port)}{LOCAL_PATH}"


def clean_flow(raw) -> str:
    """Anmeldeweg prüfen: ``admin``, ``app`` oder ``lokal``."""
    flow = str(raw or "").strip().lower()
    if flow not in FLOWS:
        raise OAuthFehler("Unbekannter Anmeldeweg. Erlaubt sind „admin“ (Weboberfläche), "
                          "„app“ (Programm auf dem PC) und „lokal“ (Rückleitung auf 127.0.0.1).")
    return flow


def redirect_uri(flow: str, port: int = 0) -> str:
    """Rückleitung zu einem Anmeldeweg. Muss in der Discord-App genau so eingetragen sein."""
    flow = clean_flow(flow)
    if flow == FLOW_LOKAL:
        return lokale_rueckleitung(port)
    return REDIRECTS[flow]


# --------------------------------------------------------------------------- Zustandswerte

def zustaende_aufraeumen(*, now: int | None = None) -> int:
    """Abgelaufene Zustandswerte wegwerfen. Rückgabe: Anzahl."""
    stamp = _now(now)
    with _states_lock:
        alt = [s for s, z in _states.items() if stamp >= z.expires_at]
        for s in alt:
            _states.pop(s, None)
        if len(_states) > STATES_MAX:
            # Die ältesten zuerst – ein Ansturm soll keine Erinnerung an alles erzwingen.
            nach_alter = sorted(_states.values(), key=lambda z: z.created_at)
            for z in nach_alter[:len(_states) - STATES_MAX]:
                _states.pop(z.state, None)
                alt.append(z.state)
        return len(alt)


def zustaende_offen(*, now: int | None = None) -> int:
    """Anzahl der gerade offenen Anmeldevorgänge (für Übersichten des Betreibers)."""
    stamp = _now(now)
    with _states_lock:
        return sum(1 for z in _states.values() if stamp < z.expires_at)


def _zustand_anlegen(flow: str, invite_code: str, port: int, abhol_hash: str,
                     stamp: int) -> _Zustand:
    state = secrets.token_urlsafe(32)
    zustand = _Zustand(state=state, flow=flow, invite_code=invite_code, port=port,
                       abhol_hash=abhol_hash, created_at=stamp,
                       expires_at=stamp + STATE_TTL_SECONDS)
    with _states_lock:
        _states[state] = zustand
    zustaende_aufraeumen(now=stamp)
    return zustand


def zustand_einloesen(state: str, *, now: int | None = None) -> _Zustand:
    """Zustandswert prüfen und **verbrauchen** (genau einmal verwendbar, gegen CSRF)."""
    stamp = _now(now)
    wanted = str(state or "").strip()
    if not wanted or not _STATE_RE.fullmatch(wanted):
        raise OAuthZustandFehler("Die Rückleitung von Discord war unvollständig (es fehlt der "
                                 "Zustandswert). Bitte die Anmeldung erneut starten.")
    with _states_lock:
        gefunden = None
        for zustand in _states.values():
            # Zeitkonstant vergleichen: der Zustandswert ist ein Geheimnis.
            if secrets.compare_digest(zustand.state, wanted):
                gefunden = zustand
                break
        if gefunden is not None:
            _states.pop(gefunden.state, None)
    if gefunden is None:
        raise OAuthZustandFehler("Diese Anmeldung ist abgelaufen, wurde schon abgeschlossen oder "
                                 "gehört nicht zu diesem Server. Bitte die Anmeldung erneut "
                                 "starten.")
    if stamp >= gefunden.expires_at:
        raise OAuthZustandFehler(
            f"Diese Anmeldung ist abgelaufen (sie gilt {STATE_TTL_SECONDS // 60} Minuten). "
            f"Bitte erneut starten.")
    return gefunden


# --------------------------------------------------------------------------- Anmeldung beginnen

def _clean_invite(raw) -> str:
    """Einladungscode früh prüfen – lieber jetzt meckern als nach dem Umweg über Discord."""
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        return users.normalize_code(value)
    except ValueError as exc:
        raise OAuthFehler(str(exc)) from None


def _clean_hash(raw) -> str:
    """Optionaler SHA256-Hex des Abholgeheimnisses, das das Programm auf dem PC behält."""
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if not _HASH_RE.fullmatch(value):
        raise OAuthFehler("Das Abholmerkmal muss ein SHA256-Wert als 64 Hex-Zeichen sein.")
    return value


def start(flow: str, *, invite_code: str = "", port: int = 0, abhol_hash: str = "",
          prompt: str = "", now: int | None = None) -> dict:
    """Anmeldung beginnen: Zustandswert erzeugen und die Discord-Adresse zusammenbauen.

    Rückgabe enthält ``url`` (dorthin schickt man den Browser), ``state`` und die Gültigkeit.
    Ein mitgegebener Einladungscode wird am Zustandswert gemerkt, damit der Benutzer ihn nach
    der Rückleitung nicht erneut eintippen muss.
    """
    daten = zugangsdaten()
    flow = clean_flow(flow)
    ziel = redirect_uri(flow, port)
    code = _clean_invite(invite_code)
    merkmal = _clean_hash(abhol_hash)
    prompt = str(prompt or "").strip().lower()
    if prompt not in ("", "none", "consent"):
        raise OAuthFehler("Für „prompt“ sind nur „none“ und „consent“ erlaubt.")
    stamp = _now(now)
    zustand = _zustand_anlegen(flow, code, clean_port(port) if flow == FLOW_LOKAL else 0,
                               merkmal, stamp)
    felder = {
        "response_type": "code",
        "client_id": daten.client_id,
        "scope": SCOPE,
        "redirect_uri": ziel,
        "state": zustand.state,
    }
    if prompt:
        felder["prompt"] = prompt
    return {
        "url": AUTHORIZE_URL + "?" + urllib.parse.urlencode(felder),
        "state": zustand.state,
        "flow": flow,
        "redirect_uri": ziel,
        "scope": SCOPE,
        "expires_at": zustand.expires_at,
        "gueltig_sekunden": STATE_TTL_SECONDS,
    }


# --------------------------------------------------------------------------- Netzwerk

def _urlopen(request: urllib.request.Request, timeout: float):
    """Die einzige Stelle, an der wirklich ins Netz gegangen wird.

    Die Selbsttests ersetzen genau diese Funktion und kommen so ohne Discord aus.
    """
    return urllib.request.urlopen(request, timeout=timeout)     # noqa: S310 – feste Adressen


_FEHLERTEXTE = {
    "invalid_grant": "Der Anmeldecode von Discord ist nicht mehr gültig – er gilt nur wenige "
                     "Minuten und nur einmal. Bitte die Anmeldung erneut starten.",
    "invalid_client": "Discord lehnt die Zugangsdaten dieses Servers ab. Bitte Client-ID und "
                      "Anwendungsgeheimnis in der Discord-App prüfen.",
    "invalid_request": "Discord hat die Anfrage abgelehnt (unvollständige Angaben). Stimmt die "
                       "Rückleitung in der Discord-App genau mit der hier eingetragenen überein?",
    "invalid_scope": f"Discord hat den Umfang „{SCOPE}“ abgelehnt. Bitte die Einstellungen der "
                     f"Discord-App prüfen.",
    "unsupported_grant_type": "Discord hat die Art der Anfrage abgelehnt. Bitte den Betreiber "
                              "informieren.",
    "access_denied": "Die Anmeldung bei Discord wurde abgebrochen. Es wurden keine Daten "
                     "übernommen.",
}


def _http_fehler(exc: urllib.error.HTTPError, was: str) -> OAuthFehler:
    """Fehlerantwort von Discord in einen deutschen Satz übersetzen."""
    try:
        rohdaten = exc.read(MAX_ANTWORT_BYTES)
    except Exception:                        # Leitung weg, während wir lesen
        rohdaten = b""
    kennung, beschreibung = "", ""
    try:
        inhalt = json.loads(rohdaten.decode("utf-8", "replace")) if rohdaten else {}
        if isinstance(inhalt, dict):
            kennung = str(inhalt.get("error") or "").strip().lower()
            beschreibung = str(inhalt.get("error_description") or inhalt.get("message") or "")
    except ValueError:
        beschreibung = rohdaten.decode("utf-8", "replace")
    if kennung in _FEHLERTEXTE:
        status = 400 if kennung in ("invalid_grant", "access_denied") else 502
        if kennung == "invalid_client":
            status = 500                     # unser Fehler, nicht der des Benutzers
        return OAuthFehler(_FEHLERTEXTE[kennung], status)
    if exc.code == 401:
        return DiscordFehler("Discord hat das Zugriffsrecht nicht angenommen. Bitte die "
                             "Anmeldung erneut starten.", 400)
    if exc.code == 429:
        return DiscordFehler("Discord bremst uns gerade (zu viele Anfragen). Bitte in einer "
                             "Minute erneut versuchen.", 503)
    text = _sauber(beschreibung)
    ende = f": {text}" if text else "."
    return DiscordFehler(f"{was} hat die Anfrage abgelehnt (HTTP {int(exc.code)}){ende}")


def _anfrage(url: str, *, felder: dict | None = None, kopfzeilen: dict | None = None,
             was: str = "Discord") -> dict:
    """Eine Anfrage an Discord stellen und die JSON-Antwort liefern."""
    rumpf = urllib.parse.urlencode(felder).encode("utf-8") if felder is not None else None
    req = urllib.request.Request(url, data=rumpf, method="POST" if rumpf is not None else "GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if rumpf is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for name, wert in (kopfzeilen or {}).items():
        req.add_header(name, wert)
    try:
        with _urlopen(req, HTTP_TIMEOUT) as antwort:
            rohdaten = antwort.read(MAX_ANTWORT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise _http_fehler(exc, was) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        grund = _sauber(getattr(exc, "reason", exc))
        raise DiscordFehler(f"Discord ist gerade nicht erreichbar ({grund}). Bitte später "
                            f"erneut versuchen.") from None
    if len(rohdaten) > MAX_ANTWORT_BYTES:
        raise DiscordFehler("Discord hat unerwartet viele Daten geschickt. Die Anmeldung wurde "
                            "abgebrochen.")
    try:
        inhalt = json.loads(rohdaten.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DiscordFehler("Die Antwort von Discord war nicht lesbar (kein gültiges JSON). "
                            "Bitte später erneut versuchen.") from None
    if not isinstance(inhalt, dict):
        raise DiscordFehler("Die Antwort von Discord hatte einen unerwarteten Aufbau.")
    return inhalt


def token_holen(code: str, ziel: str) -> str:
    """Aus dem Anmeldecode ein Zugriffstoken machen. Das Token wird **nicht** gespeichert."""
    daten = zugangsdaten()
    code = str(code or "").strip()
    if not code or not _CODE_RE.fullmatch(code):
        raise OAuthFehler("Der Anmeldecode von Discord sieht nicht richtig aus. Bitte die "
                          "Anmeldung erneut starten.")
    antwort = _anfrage(TOKEN_URL, felder={
        "client_id": daten.client_id,
        "client_secret": daten.secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ziel,
    }, was="Discord")
    token = str(antwort.get("access_token") or "").strip()
    if not token or not _TOKEN_RE.fullmatch(token):
        raise DiscordFehler("Discord hat kein brauchbares Zugriffsrecht geliefert. Bitte die "
                            "Anmeldung erneut starten.")
    art = str(antwort.get("token_type") or "Bearer").strip()
    if art.lower() != "bearer":
        raise DiscordFehler("Discord hat ein unerwartetes Zugriffsrecht geliefert. Die Anmeldung "
                            "wurde abgebrochen.")
    umfaenge = str(antwort.get("scope") or SCOPE).split()
    if SCOPE not in umfaenge:
        raise DiscordFehler(f"Discord hat den Umfang „{SCOPE}“ nicht bestätigt. Die Anmeldung "
                            f"wurde abgebrochen.")
    return token


def _avatar_url(discord_id: str, avatar, discriminator) -> str:
    """Bildadresse zusammenbauen. Ohne eigenes Bild das Standardbild von Discord."""
    hash_wert = str(avatar or "").strip().lower()
    if hash_wert and _AVATAR_RE.fullmatch(hash_wert):
        endung = "gif" if hash_wert.startswith("a_") else "png"
        return f"{CDN_BASE}/avatars/{discord_id}/{hash_wert}.{endung}?size={AVATAR_SIZE}"
    nummer = str(discriminator or "").strip()
    if nummer.isdigit() and nummer != "0":
        index = int(nummer) % 5                       # alte Konten mit #1234
    else:
        index = (int(discord_id) >> 22) % 6           # neue Konten
    return f"{CDN_BASE}/embed/avatars/{index}.png"


def _text(raw, grenze: int = 64) -> str:
    """Namen von Discord entschärfen: keine Steuerzeichen, gekürzt."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(raw or ""))).strip()[:grenze]


def identitaet_holen(token: str) -> Identitaet:
    """``/users/@me`` abfragen und daraus das Ergebnisobjekt bauen."""
    antwort = _anfrage(IDENTITY_URL, kopfzeilen={"Authorization": f"Bearer {token}"},
                       was="Discord")
    discord_id = str(antwort.get("id") or "").strip()
    if not _SNOWFLAKE_RE.fullmatch(discord_id):
        raise DiscordFehler("Discord hat keine brauchbare Kennung geliefert. Bitte später erneut "
                            "versuchen.")
    benutzername = _text(antwort.get("username"))
    anzeigename = _text(antwort.get("global_name")) or benutzername
    if not benutzername:
        raise DiscordFehler("Discord hat keinen Benutzernamen geliefert. Bitte später erneut "
                            "versuchen.")
    # „email“ und alles andere aus der Antwort wird bewusst nicht übernommen.
    return Identitaet(discord_id=discord_id, benutzername=benutzername, anzeigename=anzeigename,
                      avatar_url=_avatar_url(discord_id, antwort.get("avatar"),
                                             antwort.get("discriminator")))


def token_wegwerfen(token: str) -> bool:
    """Das Zugriffstoken bei Discord widerrufen. Klappt das nicht, ist das kein Beinbruch."""
    if not token:
        return False
    try:
        daten = zugangsdaten()
    except OAuthNichtEingerichtet:
        return False
    try:
        _anfrage(REVOKE_URL, felder={"client_id": daten.client_id, "client_secret": daten.secret,
                                     "token": token, "token_type_hint": "access_token"},
                 was="Discord")
        return True
    except OAuthFehler:
        return False            # Token läuft von selbst ab; wir haben es ohnehin nicht behalten


def complete(state: str, code: str, *, error: str = "", error_description: str = "",
             now: int | None = None) -> Ergebnis:
    """Rückleitung von Discord auswerten: Zustand prüfen, Token holen, Profil lesen, Token wegwerfen."""
    fehler = str(error or "").strip().lower()
    if fehler:
        try:
            zustand_einloesen(state, now=now)       # Vorgang in jedem Fall abschließen
        except OAuthZustandFehler:
            pass
        if fehler == "access_denied":
            raise OAuthAbgebrochen(_FEHLERTEXTE["access_denied"])
        text = _sauber(error_description)
        ende = f" ({text})" if text else ""
        raise OAuthFehler(f"Discord hat die Anmeldung abgelehnt{ende}. Bitte erneut versuchen.")
    zustand = zustand_einloesen(state, now=now)
    if not str(code or "").strip():
        raise OAuthFehler("Die Rückleitung von Discord enthielt keinen Anmeldecode. Bitte die "
                          "Anmeldung erneut starten.")
    ziel = redirect_uri(zustand.flow, zustand.port)
    token = token_holen(code, ziel)
    try:
        identitaet = identitaet_holen(token)
    finally:
        token_wegwerfen(token)                     # in jedem Fall: wir behalten es nicht
    return Ergebnis(identitaet=identitaet, flow=zustand.flow, invite_code=zustand.invite_code,
                    port=zustand.port, abhol_hash=zustand.abhol_hash)


# --------------------------------------------------------------------------- Konto und Sitzung

def freier_name(identitaet: Identitaet) -> str:
    """Einen gültigen, noch freien Anzeigenamen aus den Discord-Namen ableiten."""
    vorschlaege = [identitaet.anzeigename, identitaet.benutzername]
    kandidaten: list[str] = []
    for roh in vorschlaege:
        # Nur Zeichen behalten, die `users.clean_name` erlaubt.
        sauber = re.sub(r"[^A-Za-zÄÖÜäöüß0-9 ._\-]", "", str(roh or ""))
        sauber = re.sub(r"\s+", " ", sauber).strip(" ._-").strip()[:users.NAME_MAX]
        if len(sauber) >= users.NAME_MIN:
            try:
                kandidaten.append(users.clean_name(sauber))
            except ValueError:
                continue
    def mit_anhang(basis: str, anhang: str) -> str:
        return basis[:users.NAME_MAX - len(anhang)].strip(" ._-") + anhang

    for basis in kandidaten:
        if users.find_by_name(basis) is None:
            return basis
    if kandidaten:
        # Der Wunschname ist belegt – lieber „Luca 2“ als etwas völlig Fremdes.
        for nummer in range(2, 100):
            name = mit_anhang(kandidaten[0], f" {nummer}")
            if users.find_by_name(name) is None:
                return name
    ersatz = "Spieler " + identitaet.discord_id[-4:]
    if users.find_by_name(ersatz) is None:
        return ersatz
    basis = kandidaten[0] if kandidaten else "Spieler"
    name = mit_anhang(basis, " " + store_hosted.new_id(3))
    try:
        return users.clean_name(name)
    except ValueError:
        return "Spieler " + store_hosted.new_id(3)


#: Der Einladungscode aus ``ERSTER-ADMIN.txt``. Der Dienst hinterlegt ihn beim Start
#: (``mcsmd.ensure_first_admin``) und löscht ihn, sobald es einen Betreiber gibt. Nur im
#: Arbeitsspeicher – auf Platte steht er in der Datei, die allein root und mcsm lesen dürfen.
_erstzugang: dict = {"code": ""}


def erstzugang_setzen(code: str) -> None:
    """Den Code für das erste Betreiberkonto hinterlegen (leer = es gibt keinen mehr)."""
    with _states_lock:
        try:
            _erstzugang["code"] = users.normalize_code(code) if code else ""
        except ValueError:
            _erstzugang["code"] = ""


def erstzugang_offen() -> bool:
    """Wartet noch ein Erstzugang auf seinen Code?"""
    with _states_lock:
        return bool(_erstzugang.get("code"))


def erster_admin_faellig(*, user_id: str = "") -> bool:
    """Ob ein Konto jetzt zum Betreiber gemacht werden darf.

    Nur wenn es **kein** nutzbares Admin-Konto gibt **und** außer diesem Konto überhaupt kein
    weiteres existiert. Sonst könnte sich ein Fremder in einer Anlage ohne Admin einfach das
    Betreiberkonto holen.
    """
    if users.count_admins() > 0:
        return False
    andere = [u for u in users.all_users() if u.get("id") != str(user_id or "")]
    return not andere


def erstanmeldung_als_admin(user_id: str, *, now: int | None = None) -> tuple[dict, bool]:
    """Prüfen, ob es noch keinen Betreiber gibt – und dieses Konto dann zum Betreiber machen.

    Rückgabe: (Konto, ob gerade zum Admin gemacht). Gibt es schon einen Admin, bleibt alles wie
    es ist; danach kommt niemand mehr ohne Einladungscode herein.
    """
    with store_hosted.lock():
        user = users.get_user(user_id)
        if not user:
            raise OAuthFehler(f"Es gibt kein Konto mit der Kennung „{user_id}“.", 404)
        if not erster_admin_faellig(user_id=user_id):
            return user, False
        return users.set_role(user_id, "admin"), True


def konto_fuer(identitaet: Identitaet, *, invite_code: str = "",
               now: int | None = None) -> Konto:
    """Konto zur Discord-Kennung finden – oder anlegen (erstes Konto = Betreiber, sonst Einladung)."""
    stamp = _now(now)
    discord_id = str(identitaet.discord_id or "").strip()
    if not _SNOWFLAKE_RE.fullmatch(discord_id):
        raise DiscordFehler("Die Discord-Anmeldung hat keine brauchbare Kennung geliefert.")
    code = _clean_invite(invite_code)
    with store_hosted.lock():
        vorhanden = users.find_by_discord(discord_id)
        if vorhanden:
            if vorhanden.get("blocked"):
                grund = str(vorhanden.get("blocked_reason") or "").strip()
                ende = f" Grund: {grund}" if grund else ""
                raise KontoGesperrt(f"Dieses Konto ist gesperrt. Bitte beim Betreiber "
                                    f"melden.{ende}")
            user, admin_vergeben = erstanmeldung_als_admin(vorhanden["id"], now=stamp)
            return Konto(user=user, neu=False, admin_vergeben=admin_vergeben)

        name = freier_name(identitaet)
        if erster_admin_faellig():
            # Allererste Anmeldung auf einem frischen Server: dieses Konto wird Betreiber –
            # aber **nur** mit dem Einladungscode aus ERSTER-ADMIN.txt. Ohne diese Prüfung
            # bekäme schlicht der Schnellste das Betreiberkonto: die Discord-Wege sind über
            # nginx öffentlich erreichbar, und zwischen „Dienst läuft“ und „Betreiber hat sich
            # zum ersten Mal angemeldet“ liegen erfahrungsgemäß Stunden. Der Schutz durch die
            # 0600-Datei liefe sonst ins Leere.
            erwartet = ""
            with _states_lock:
                erwartet = str(_erstzugang.get("code") or "")
            if not erwartet:
                raise EinladungNoetig(
                    "Auf diesem Server ist noch kein Betreiberkonto eingerichtet, und der Dienst "
                    "hat dafür keinen Einladungscode hinterlegt. Bitte den Dienst neu starten – "
                    "er legt den Code dann in ERSTER-ADMIN.txt ab.")
            if not code or not secrets.compare_digest(erwartet, code):
                raise EinladungNoetig(
                    "Das erste Betreiberkonto wird nur mit dem Einladungscode aus der Datei "
                    "ERSTER-ADMIN.txt angelegt (sie liegt im Datenordner des Root-Servers und ist "
                    "nur für root und den Dienstbenutzer lesbar). Bitte diesen Code eintragen.")
            try:
                user = users.redeem_invite(code, name, discord_id=discord_id, now=stamp)
            except ValueError as exc:
                raise OAuthFehler(str(exc)) from None
            user = users.set_role(user["id"], "admin")
            return Konto(user=user, neu=True, admin_vergeben=True)
        if not code:
            raise EinladungNoetig("Für dieses Discord-Konto gibt es hier noch kein Konto. "
                                  "Bitte zuerst einen Einladungscode des Betreibers einlösen.")
        try:
            user = users.redeem_invite(code, name, discord_id=discord_id, now=stamp)
        except ValueError as exc:
            raise OAuthFehler(str(exc)) from None
        return Konto(user=user, neu=True, admin_vergeben=False)


def anmelden(ergebnis: Ergebnis | Identitaet, *, invite_code: str | None = None,
             note: str = "", days: int | None = None, now: int | None = None) -> Anmeldung:
    """Kompletter zweiter Teil: Konto zuordnen und eine Sitzung anlegen."""
    if isinstance(ergebnis, Ergebnis):
        identitaet = ergebnis.identitaet
        code = ergebnis.invite_code if invite_code is None else invite_code
    else:
        identitaet = ergebnis
        code = invite_code or ""
    konto = konto_fuer(identitaet, invite_code=code, now=now)
    zusatz = {} if days is None else {"days": int(days)}
    try:
        token, sitzung = users.create_session(konto.user["id"], note=note, now=now, **zusatz)
    except ValueError as exc:
        raise OAuthFehler(str(exc)) from None
    return Anmeldung(user=konto.user, token=token, expires_at=int(sitzung.get("expires_at") or 0),
                     neu=konto.neu, admin_vergeben=konto.admin_vergeben)


def verknuepfen(user_id: str, identitaet: Identitaet) -> dict:
    """Ein bestehendes Konto nachträglich mit einem Discord-Konto verbinden."""
    try:
        return users.link_discord(user_id, identitaet.discord_id)
    except ValueError as exc:
        raise OAuthFehler(str(exc), 409 if "verknüpft" in str(exc) else 400) from None


# --------------------------------------------------------------------------- Abholcodes

def abholcode_formatieren(code: str) -> str:
    """Code für die Anzeige in Vierergruppen zerlegen."""
    code = str(code or "")
    return "-".join(code[i:i + ABHOL_GRUPPE] for i in range(0, len(code), ABHOL_GRUPPE))


def abholcode_normalisieren(raw) -> str:
    """Eingetippten Abholcode auf die Vergleichsform bringen (Großbuchstaben, ohne Trennzeichen)."""
    value = re.sub(r"[\s\-_.]", "", str(raw or "")).upper()
    if not _ABHOL_RE.fullmatch(value):
        raise OAuthFehler(f"Das ist kein gültiger Abholcode – erwartet werden {ABHOL_LEN} "
                          f"Zeichen, z. B. {abholcode_formatieren('ABCDEFGHJKLM')}.")
    return value


def abholcodes_aufraeumen(*, now: int | None = None) -> int:
    """Abgelaufene Abholcodes wegwerfen. Rückgabe: Anzahl."""
    stamp = _now(now)
    with _abhol_lock:
        alt = [c for c, a in _abholungen.items() if stamp >= a.expires_at]
        for code in alt:
            _abholungen.pop(code, None)
        if len(_abholungen) > ABHOL_MAX:
            nach_alter = sorted(_abholungen.values(), key=lambda a: a.created_at)
            for a in nach_alter[:len(_abholungen) - ABHOL_MAX]:
                _abholungen.pop(a.code, None)
                alt.append(a.code)
        _fehlversuche[:] = [t for t in _fehlversuche if stamp - t < ABHOL_FENSTER_SECONDS]
        return len(alt)


def abholcode_anlegen(inhalt: dict, *, claim_hash: str = "", now: int | None = None) -> tuple[str, int]:
    """Sitzungstoken zum Abholen hinterlegen. Rückgabe: (Abholcode, Ablaufzeit).

    Der Code gilt kurz und genau einmal. Ist ein ``claim_hash`` hinterlegt, muss das Programm
    beim Abholen zusätzlich das passende Geheimnis vorlegen.
    """
    if not isinstance(inhalt, dict) or not inhalt:
        raise OAuthFehler("Es gibt nichts zum Abholen.")
    merkmal = _clean_hash(claim_hash)
    stamp = _now(now)
    with _abhol_lock:
        code = "".join(secrets.choice(ABHOL_ALPHABET) for _ in range(ABHOL_LEN))
        while code in _abholungen:
            code = "".join(secrets.choice(ABHOL_ALPHABET) for _ in range(ABHOL_LEN))
        _abholungen[code] = _Abholung(code=code, inhalt=dict(inhalt), claim_hash=merkmal,
                                      created_at=stamp, expires_at=stamp + ABHOL_TTL_SECONDS)
    # Erst nach dem Eintragen aufräumen, damit die Obergrenze wirklich eingehalten wird
    # (der frische Code ist der jüngste und bleibt dabei stehen).
    abholcodes_aufraeumen(now=stamp)
    return code, stamp + ABHOL_TTL_SECONDS


def abholcode_einloesen(code: str, *, geheimnis: str = "", now: int | None = None) -> dict:
    """Abholcode einlösen und den hinterlegten Inhalt liefern (genau einmal möglich)."""
    stamp = _now(now)
    abholcodes_aufraeumen(now=stamp)
    wanted = abholcode_normalisieren(code)
    with _abhol_lock:
        if len(_fehlversuche) >= ABHOL_FEHLVERSUCHE_MAX:
            _abholungen.clear()
            _fehlversuche.clear()
            raise OAuthZustandFehler("Es wurden zu viele falsche Abholcodes eingereicht. Alle "
                                     "offenen Anmeldungen wurden verworfen – bitte die Anmeldung "
                                     "erneut starten.", 429)
        gefunden = None
        for abholung in _abholungen.values():
            if secrets.compare_digest(abholung.code, wanted):
                gefunden = abholung
                break
        if gefunden is None or stamp >= gefunden.expires_at:
            _abholungen.pop(getattr(gefunden, "code", ""), None)
            _fehlversuche.append(stamp)
            raise OAuthZustandFehler(
                f"Dieser Abholcode ist unbekannt, schon benutzt oder abgelaufen (er gilt "
                f"{ABHOL_TTL_SECONDS // 60} Minuten). Bitte die Anmeldung erneut starten.")
        if gefunden.claim_hash:
            vorgelegt = hashlib.sha256(str(geheimnis or "").encode("utf-8")).hexdigest()
            if not secrets.compare_digest(gefunden.claim_hash, vorgelegt):
                _fehlversuche.append(stamp)
                raise OAuthZustandFehler("Zu dieser Abholung gehört ein anderes Programm. Bitte "
                                         "die Anmeldung im Programm erneut starten.", 403)
        _abholungen.pop(gefunden.code, None)
        return dict(gefunden.inhalt)


def abholungen_offen(*, now: int | None = None) -> int:
    """Anzahl der Sitzungstoken, die gerade noch abgeholt werden können."""
    stamp = _now(now)
    with _abhol_lock:
        return sum(1 for a in _abholungen.values() if stamp < a.expires_at)


def alles_leeren() -> None:
    """Zustandswerte, Abholcodes und den Erstzugang verwerfen (Dienstneustart, Selbsttests).

    Den Erstzugang hinterlegt der Dienst gleich danach wieder (`mcsmd.ensure_first_admin`),
    sofern es noch kein Betreiberkonto gibt.
    """
    with _states_lock:
        _states.clear()
        _erstzugang["code"] = ""
    with _abhol_lock:
        _abholungen.clear()
        _fehlversuche.clear()
