# -*- coding: utf-8 -*-
"""Unterdomänen der Instanzen und die Zuordnungstabelle des Verteilers.

Jede Java-Instanz bekommt eine dauerhafte DNS-Marke: aus dem Namen abgeleitet, klein,
Umlaute als ``ae/oe/ue/ss``, nur ``a-z0-9-``. Der Spieler tippt damit nur noch
``mein-server.arcardia-nexus.de`` ein – ohne Port. Der Verteiler (``hosted/router.py``,
Dienst ``mcsm-router`` auf TCP 25565) schlägt die Marke in ``/srv/mcsm/data/routes.json``
nach und reicht die Verbindung an ``127.0.0.1:<port>`` weiter.

Regeln (siehe hosted/spec-router.md Abschnitt 6):

* Die Marke steht im Instanz-Datensatz (``marke``). Sie wird **einmal** vergeben und bleibt
  beim Umbenennen gleich, damit Spieler ihre Adresse behalten.
* Sie ist über **alle** Konten eindeutig – sonst landete ein Spieler beim fremden Server.
  Bei einer Dopplung wird die Instanzkennung angehängt, als Notnagel gilt die Kennung allein.
* Reservierte Marken (``server``, ``www``, ``api``, ``admin``, ``mc``, ``play``, ``status``,
  ``panel`` …) bleiben frei: dort sitzen die eigenen Dienste.
* Geschrieben wird **atomar** (Zwischendatei + ``os.replace``, Rechte 0600, Besitzer des
  Dienstes). Der Verteiler liest sonst irgendwann eine halbe Datei.

Bedrock läuft **nicht** über den Verteiler (UDP, kein Handshake mit Adresse). Dort bleibt es
bei der nackten Basisdomain plus Portangabe – ``adresse()`` liefert genau das.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import unicodedata

from . import instances as instances_mod
from . import store_hosted

#: Basisdomain. Verteiler und Daemon müssen denselben Wert benutzen (MCSM_DOMAIN).
DOMAIN_ENV = "MCSM_DOMAIN"
DEFAULT_DOMAIN = "arcardia-nexus.de"

#: Pfad der Zuordnungstabelle. Muss zu MCSM_ROUTES des Verteilers passen.
ROUTES_ENV = "MCSM_ROUTES"
ROUTES_FILE = "routes.json"

#: Aufgegebene Marken mit ihrer Schonfrist (der Verteiler liest diese Datei nicht).
SPERRLISTE_FILE = "marken.json"

#: So lange bleibt eine frei gewordene Marke für fremde Konten gesperrt.
SPERRE_TAGE = 30

#: Der Verteiler sitzt hier; kein Server darf diesen Port bekommen.
ROUTER_PORT = 25565
#: Eigner der Portbuchung für den Verteiler (muss zu ports._OWNER_RE passen: kein „@“ am Anfang).
ROUTER_OWNER = "verteiler"

#: Marken, die eigenen Diensten gehören und deshalb nie an eine Instanz gehen.
RESERVIERT = (
    "server", "www", "api", "admin", "mc", "play", "status", "panel",
    "standard", "verteiler", "router", "mail", "smtp", "imap", "ns", "ns1", "ns2",
    "cdn", "static", "ftp", "git", "dev", "test", "beta", "localhost", "arcardia",
)

#: So lang darf der aus dem Namen abgeleitete Teil werden (63 wäre die DNS-Grenze;
#: der Rest bleibt für „-<kennung>“ bei einer Dopplung frei).
MARKE_BASIS_MAX = 40

_UMLAUTE = {
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
    "å": "a", "æ": "ae", "ø": "oe", "œ": "oe", "ñ": "n", "ç": "c", "þ": "th", "ð": "d",
}
_ERSATZ_RE = re.compile(r"[^a-z0-9]+")


# ----------------------------------------------------------------------- Einstellungen

def domain() -> str:
    """Basisdomain, unter der die Server erreichbar sind."""
    raw = (os.environ.get(DOMAIN_ENV) or "").strip().lower().strip(".")
    return raw or DEFAULT_DOMAIN


def routes_path() -> pathlib.Path:
    """Wo die Zuordnungstabelle des Verteilers liegt."""
    raw = (os.environ.get(ROUTES_ENV) or "").strip()
    return pathlib.Path(raw) if raw else store_hosted.data_dir() / ROUTES_FILE


# ----------------------------------------------------------------------- Marken

def marke_aus_name(raw) -> str:
    """Aus einem Servernamen eine DNS-Marke machen („Lucas Welt Nr. 2“ → ``lucas-welt-nr-2``)."""
    text = str(raw or "").strip().lower()
    for zeichen, ersatz in _UMLAUTE.items():
        text = text.replace(zeichen, ersatz)
    # Alles, was sich zerlegen lässt, wird auf seinen Grundbuchstaben gekürzt (é → e).
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    text = _ERSATZ_RE.sub("-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)[:MARKE_BASIS_MAX].strip("-")
    return text


def belegte_marken(*, ausser: str = "") -> dict:
    """``{marke: instanzkennung}`` über alle Konten (ohne die Instanz ``ausser``)."""
    out: dict = {}
    for instance in instances_mod.all_instances():
        iid = str(instance.get("id") or "")
        if ausser and iid == str(ausser):
            continue
        marke = str(instance.get("marke") or "").strip().lower()
        if marke:
            out[marke] = iid
    return out


# ----------------------------------------------------------------------- Aufgegebene Marken

def sperrliste_pfad() -> pathlib.Path:
    """Wo die aufgegebenen Marken liegen (neben der Zuordnungstabelle)."""
    return store_hosted.data_dir() / SPERRLISTE_FILE


def _sperrliste_lesen() -> dict:
    try:
        with sperrliste_pfad().open("r", encoding="utf-8") as fh:
            daten = json.load(fh)
    except (OSError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def _sperrliste_schreiben(daten: dict) -> None:
    ziel = sperrliste_pfad()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(f"{ziel.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(daten, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, ziel)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def gesperrte_marken(*, now: int | None = None) -> dict:
    """``{marke: konto}`` der aufgegebenen Marken, die noch in der Schonfrist sind.

    Wird ein Server gelöscht oder auf den PC zurückgeholt, verschwindet sein Datensatz – und
    mit ihm die Marke. Ohne Schonfrist bekäme sie sofort der Nächste, dessen Servername
    dieselbe Marke ergibt: Alle Spieler, die ``familien-welt.arcardia-nexus.de`` im
    Serverbrowser stehen haben, landeten dann bei einem **fremden** Server. Das eigene Konto
    darf seine Marke dagegen jederzeit wieder bekommen.
    """
    stamp = store_hosted.now() if now is None else int(now)
    out: dict = {}
    for marke, eintrag in _sperrliste_lesen().items():
        if not isinstance(eintrag, dict):
            continue
        if int(eintrag.get("bis") or 0) <= stamp:
            continue
        out[str(marke).strip().lower()] = str(eintrag.get("konto") or "")
    return out


def marke_aufgeben(marke: str, konto: str = "", *, now: int | None = None) -> bool:
    """Eine frei gewordene Marke für ``SPERRE_TAGE`` Tage zurückhalten."""
    name = str(marke or "").strip().lower()
    if not name:
        return False
    stamp = store_hosted.now() if now is None else int(now)
    daten = _sperrliste_lesen()
    daten[name] = {"bis": stamp + SPERRE_TAGE * 86400, "konto": str(konto or ""),
                   "seit": stamp}
    try:
        _sperrliste_schreiben(_aufraeumen(daten, stamp))
    except OSError:
        return False
    return True


def marke_freigeben(marke: str) -> None:
    """Eine Marke aus der Sperrliste nehmen (sie gehört jetzt wieder einer Instanz)."""
    name = str(marke or "").strip().lower()
    daten = _sperrliste_lesen()
    if name not in daten:
        return
    daten.pop(name, None)
    try:
        _sperrliste_schreiben(daten)
    except OSError:
        pass


def _aufraeumen(daten: dict, stamp: int) -> dict:
    return {k: v for k, v in daten.items()
            if isinstance(v, dict) and int(v.get("bis") or 0) > stamp}


def sperrliste_aufraeumen(*, now: int | None = None) -> int:
    """Abgelaufene Einträge wegwerfen. Rückgabe: Anzahl."""
    stamp = store_hosted.now() if now is None else int(now)
    daten = _sperrliste_lesen()
    frisch = _aufraeumen(daten, stamp)
    if len(frisch) == len(daten):
        return 0
    try:
        _sperrliste_schreiben(frisch)
    except OSError:
        return 0
    return len(daten) - len(frisch)


def marke_fuer(instance: dict, *, belegt: dict | None = None, gesperrt: dict | None = None,
               now: int | None = None) -> str:
    """Die Marke, die diese Instanz bekommen soll – eindeutig und nicht reserviert.

    Reihenfolge: der Name, dann Name + Anfang der Kennung, dann die Kennung allein. Marken,
    die ein **anderes** Konto gerade aufgegeben hat, bleiben in der Schonfrist außen vor.
    """
    iid = str(instance.get("id") or "").strip().lower()
    konto = str(instance.get("owner") or "")
    if belegt is None:
        belegt = belegte_marken(ausser=iid)
    if gesperrt is None:
        gesperrt = gesperrte_marken(now=now)
    basis = marke_aus_name(instance.get("name"))
    kandidaten = []
    if basis:
        kandidaten.append(basis)
        if iid:
            kandidaten.append(f"{basis}-{iid[:6]}")
    if iid:
        kandidaten.append(iid)
    for kandidat in kandidaten:
        marke = str(kandidat or "").strip("-")[:instances_mod.MARKE_MAX].strip("-")
        if not marke or marke in RESERVIERT:
            continue
        try:
            marke = instances_mod.clean_marke(marke)
        except ValueError:
            continue
        eigner = belegt.get(marke)
        if eigner not in (None, iid):
            continue
        frueher = gesperrt.get(marke)
        if frueher is not None and frueher != konto:
            continue
        return marke
    # Notnagel: die Kennung mit Vorsatz. Kann nur greifen, wenn schon die Kennung belegt wäre.
    return instances_mod.clean_marke(f"s{iid}" if iid else "server-unbenannt")


def ensure_marke(instance: dict) -> str:
    """Marke sicherstellen und im Datensatz festschreiben. Rückgabe: die gültige Marke.

    Vorhandene Marken bleiben, solange sie gültig, nicht reserviert und nicht doppelt sind.
    """
    iid = str(instance.get("id") or "")
    if not iid:
        return ""
    with store_hosted.lock():
        frisch = instances_mod.get_instance(iid) or instance
        belegt = belegte_marken(ausser=iid)
        vorhanden = str(frisch.get("marke") or "").strip().lower()
        if vorhanden:
            try:
                vorhanden = instances_mod.clean_marke(vorhanden)
            except ValueError:
                vorhanden = ""
        if vorhanden and vorhanden not in RESERVIERT and vorhanden not in belegt:
            marke_freigeben(vorhanden)      # sie gehört wieder einer Instanz
            return vorhanden
        marke = marke_fuer(frisch, belegt=belegt)
        instances_mod.set_marke(iid, marke)
        marke_freigeben(marke)
        return marke


def subdomain(instance: dict) -> str:
    """Vollständiger Hostname der Instanz – bei Bedrock die nackte Basisdomain."""
    if str(instance.get("type") or "java") == "bedrock":
        return domain()
    marke = str(instance.get("marke") or "").strip().lower()
    return f"{marke}.{domain()}" if marke else ""


def adresse(instance: dict, *, ports_live: dict | None = None) -> str:
    """Die fertige Adresse zum Eintippen.

    * Java: ``mein-server.arcardia-nexus.de`` – **ohne** Port, der Verteiler hört auf 25565.
    * Bedrock: ``arcardia-nexus.de:25570`` – der Port des Verbindungsaufbaus gehört dazu,
      weil Bedrock (UDP/NetherNet) nicht über den Verteiler laufen kann.
    """
    gespeichert = {k: int(v) for k, v in (instance.get("ports") or {}).items() if v}
    live = {k: int(v) for k, v in (ports_live or {}).items() if v}
    if str(instance.get("type") or "java") == "bedrock":
        port = int(live.get("port") or gespeichert.get("java") or 0)
        return f"{domain()}:{port}" if port else domain()
    return subdomain(instance)


def java_port(instance: dict, *, ports_live: dict | None = None) -> int:
    """Der TCP-Port, auf den der Verteiler zeigen soll (0 = keiner bekannt)."""
    live = ports_live or {}
    for wert in (live.get("port"), (instance.get("ports") or {}).get("java")):
        try:
            port = int(wert or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return 0


# ----------------------------------------------------------------------- Tabelle

def tabelle(*, ports_live=None) -> dict:
    """Die Zuordnungstabelle für den Verteiler aufbauen.

    Aufgenommen werden Java-Instanzen im Zustand ``hosted`` mit bekanntem Port. **Gestoppte
    bleiben drin**: der Verteiler meldet dann „Dieser Server läuft gerade nicht“, und das ist
    für den Spieler die bessere Auskunft als „gibt es hier nicht“.

    ``ports_live(instanzkennung) -> dict`` liefert die gerade gebuchten Ports (der PortPool ist
    maßgeblich); fehlt der Rückruf, gilt der Wert aus dem Datensatz.
    """
    out: dict = {"_bemerkung": "Wird vom Dienst mcsm geschrieben – nicht von Hand ändern."}
    for instance in instances_mod.all_instances():
        if str(instance.get("state") or "") != "hosted":
            continue
        if str(instance.get("type") or "java") != "java":
            continue
        live = {}
        if ports_live is not None:
            try:
                live = ports_live(str(instance.get("id") or "")) or {}
            except Exception:                                   # noqa: BLE001 - nie den Ablauf stören
                live = {}
        port = java_port(instance, ports_live=live)
        if not port or port == ROUTER_PORT:
            continue
        marke = str(instance.get("marke") or "").strip().lower()
        if not marke or marke in out:
            continue
        out[marke] = {"port": port, "instanz": str(instance.get("id") or "")}
    return out


def schreibe_routen(*, ports_live=None, path: pathlib.Path | None = None) -> dict:
    """Tabelle atomar nach ``routes.json`` schreiben (0600). Rückgabe: was geschrieben wurde."""
    data = tabelle(ports_live=ports_live)
    ziel = pathlib.Path(path) if path is not None else routes_path()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(f"{ziel.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, ziel)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return data


def eintraege(data: dict) -> dict:
    """Nur die echten Einträge einer Tabelle (ohne Bemerkungszeilen mit ``_``)."""
    return {k: v for k, v in (data or {}).items() if not str(k).startswith("_")}
