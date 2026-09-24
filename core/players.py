"""Spielerverwaltung: Freigabeliste, gesperrte Spieler und wer gerade online ist.

Läuft der Server, gehen alle Änderungen als Konsolenbefehl an ihn (whitelist/allowlist, ban, pardon,
kick) – so greifen sie sofort und der Server schreibt seine Dateien selbst. Ist der Server gestoppt,
liest und schreibt der Manager die Dateien direkt: whitelist.json und banned-players.json (Java)
bzw. allowlist.json (Bedrock), im Format, das Minecraft selbst verwendet.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import time
import urllib.parse

from . import companion, manager, sources, store

MOJANG_PROFILE = "https://api.mojang.com/users/profiles/minecraft/"

# Java-Namen: 3–16 Zeichen, Buchstaben/Ziffern/Unterstrich. Bedrock-Gamertags dürfen Leerzeichen haben.
JAVA_NAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
BEDROCK_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._\-]{0,29}[A-Za-z0-9])?")
# Bedrock-Spieler kommen ueber Geyser/Floodgate mit einem Praefix (Standard ".") auf den Java-Server
# und duerfen Leerzeichen im Namen haben – sonst wuerden sie aus der Online-Liste herausfallen.
FLOODGATE_NAME_RE = re.compile(r"[.*+_\-]?[A-Za-z0-9](?:[A-Za-z0-9 ._\-]{0,30})?")

ACTIONS = ("whitelist_on", "whitelist_off", "whitelist_add", "whitelist_remove", "ban", "unban", "kick")
DURATIONS = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800, "30d": 2592000}
SETTLE = 0.8              # kurz warten, bis der laufende Server seine Dateien geschrieben hat
LIST_WAIT = 3.0           # so lange auf die Antwort des Konsolenbefehls „list“ warten
KICK_WAIT = 1.5           # so lange auf die Bestätigung des Konsolenbefehls „kick“ warten
# Bestätigung eines Rauswurfs in der Konsole (Begleit-Plugin bzw. Vanilla).
_KICK_OK_RE = re.compile(r"wurde hinausgeworfen|Kicked ", re.I)
ONLINE_CACHE = 30.0       # so lange gilt eine geholte Spielerliste als aktuell genug
PLUGIN_MAX_AGE = 60       # ältere status.json des Begleit-Plugins nicht mehr als „online“ werten


# ---------------------------------------------------------------- Grundlagen

def is_bedrock(cfg: dict) -> bool:
    return cfg.get("type") == "bedrock"


def applies(cfg: dict) -> bool:
    """Bedrock (allowlist.json) und Java mit Paper (whitelist.json). Modpack-Server bleiben außen vor."""
    return is_bedrock(cfg) or companion.applies(cfg)


def _dir(cfg: dict) -> pathlib.Path:
    return store.server_dir(cfg["id"])


def _props(cfg: dict) -> pathlib.Path:
    return _dir(cfg) / "server.properties"


def allow_path(cfg: dict) -> pathlib.Path:
    return _dir(cfg) / ("allowlist.json" if is_bedrock(cfg) else "whitelist.json")


def ban_path(cfg: dict) -> pathlib.Path:
    return _dir(cfg) / "banned-players.json"


def ban_ip_path(cfg: dict) -> pathlib.Path:
    return _dir(cfg) / "banned-ips.json"


def _unreadable(path: pathlib.Path) -> ValueError:
    return ValueError(f"Die Datei {path.name} ist beschädigt oder nur halb geschrieben "
                      "(das passiert, wenn der Server hart beendet wurde). Der Manager rührt sie nicht an, "
                      "damit die bisherigen Einträge nicht verloren gehen – bitte den Server einmal starten "
                      "und wieder stoppen, er schreibt die Datei dann neu.")


def _read_list(path: pathlib.Path, strict: bool = False) -> list[dict]:
    """Liste aus einer der Minecraft-Dateien – eine fehlende Datei gilt als leer.

    Eine vorhandene, aber unlesbare Datei ist etwas anderes als eine leere: mit strict=True meldet
    sie einen Fehler, damit nicht aus Versehen alle bisherigen Einträge überschrieben werden."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as exc:
        if strict:
            raise _unreadable(path) from exc
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        if strict:
            raise _unreadable(path) from exc
        return []
    if not isinstance(data, list):
        if strict:
            raise _unreadable(path)
        return []
    return [e for e in data if isinstance(e, dict)]


def _broken(*paths: pathlib.Path) -> list[str]:
    """Namen der Dateien, die da sind, sich aber nicht lesen lassen – ihre Listen wirken sonst leer."""
    out = []
    for path in paths:
        try:
            _read_list(path, strict=True)
        except ValueError:
            out.append(path.name)
    return out


def _write_list(path: pathlib.Path, entries: list[dict]) -> None:
    """Atomar schreiben (.tmp + Umbenennen) – ein Abbruch darf keine halbe Datei hinterlassen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _stamp(ts: float) -> str:
    """Zeitformat der Minecraft-Dateien: „yyyy-MM-dd HH:mm:ss Z“, z. B. 2026-09-23 18:04:11 +0200."""
    return datetime.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def check_name(cfg: dict, raw) -> str:
    """Spielername prüfen und aufräumen (mehrfache Leerzeichen zusammenfassen)."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not name:
        raise ValueError("Bitte einen Spielernamen eingeben.")
    if is_bedrock(cfg):
        if not BEDROCK_NAME_RE.fullmatch(name):
            raise ValueError("Dieser Name passt nicht: erlaubt sind Buchstaben, Ziffern, Leerzeichen, Punkt, "
                             "Bindestrich und Unterstrich (höchstens 31 Zeichen).")
    elif not JAVA_NAME_RE.fullmatch(name):
        if not (cfg.get("geyser") and FLOODGATE_NAME_RE.fullmatch(name)):
            raise ValueError("Java-Namen haben 3 bis 16 Zeichen und bestehen aus Buchstaben, Ziffern "
                             "und Unterstrich – bitte die Schreibweise prüfen.")
    return name


def read_name(cfg: dict, raw) -> str | None:
    """Name, wie ihn der Server selbst meldet (Online-Liste). Hier wird nicht auf die Schreibweise
    bestanden – Bedrock-Spieler über Geyser tragen ein Präfix und dürfen Leerzeichen haben. Abgewiesen
    wird nur, was in einem Konsolenbefehl gefährlich wäre."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not name or len(name) > 40:
        return None
    if any(ch < " " or ch in "\"\\" for ch in name):
        return None
    return name


def _clean_reason(raw, fallback: str) -> str:
    text = re.sub(r"[\x00-\x1f]", " ", str(raw or "")).strip()
    return text[:120] or fallback


def _duration_seconds(raw) -> int:
    """Dauer einer Sperre; leer bedeutet dauerhaft."""
    key = str(raw or "").strip().lower()
    if key in ("", "forever", "dauerhaft"):
        return 0
    if key not in DURATIONS:
        raise ValueError("Unbekannte Dauer – möglich sind 1h, 6h, 1d, 7d, 30d oder leer für dauerhaft.")
    return DURATIONS[key]


# ---------------------------------------------------------------- UUID zu einem Namen

def _dashed(raw: str) -> str:
    h = raw.replace("-", "").lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _known_profile(cfg: dict, name: str) -> tuple[str, str]:
    """UUID aus den Dateien des Servers (usercache.json, Freigabeliste, Sperrliste)."""
    low = name.lower()
    for path in (_dir(cfg) / "usercache.json", allow_path(cfg), ban_path(cfg)):
        for entry in _read_list(path):
            if str(entry.get("name") or "").lower() != low:
                continue
            raw = str(entry.get("uuid") or "")
            if re.fullmatch(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}", raw):
                return _dashed(raw), str(entry.get("name") or name)
    return "", name


def profile_for(cfg: dict, name: str) -> tuple[str, str]:
    """(UUID, richtig geschriebener Name) – erst aus den Server-Dateien, sonst von der Mojang-API."""
    uuid, found = _known_profile(cfg, name)
    if uuid:
        return uuid, found
    unknown = f"Den Spieler „{name}“ kennt Mojang nicht – bitte die Schreibweise prüfen."
    try:
        data = sources.fetch_json(MOJANG_PROFILE + urllib.parse.quote(name))
    except sources.SourceError as exc:
        detail = str(exc).split("nicht laden: ", 1)[-1].strip()
        if "404" in detail:                            # Mojang kennt diesen Namen nicht
            raise ValueError(unknown) from exc
        raise ValueError(f"Der Name „{name}“ konnte gerade nicht bei Mojang nachgeschlagen werden ({detail}). "
                         "Bitte später erneut versuchen – oder den Server starten, dann erledigt er es selbst.") from exc
    raw = str(data.get("id") or "") if isinstance(data, dict) else ""
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        raise ValueError(unknown)
    return _dashed(raw), str(data.get("name") or name)


# ---------------------------------------------------------------- Listen lesen

def _flag_key(cfg: dict) -> str:
    return "allow-list" if is_bedrock(cfg) else "white-list"


def list_enabled(cfg: dict) -> bool:
    values = dict(manager.read_properties(_props(cfg)))
    return str(values.get(_flag_key(cfg), "")).strip().lower() == "true"


def _set_enabled(cfg: dict, enabled: bool) -> None:
    """server.properties nachziehen. Java setzt den Wert bei „whitelist on“ selbst – dann bleibt die
    Datei unberührt; Bedrock merkt sich den Befehl nicht, dort ist dieser Schritt nötig."""
    want = "true" if enabled else "false"
    path = _props(cfg)
    if dict(manager.read_properties(path)).get(_flag_key(cfg)) != want:
        manager.patch_properties(path, {_flag_key(cfg): want})


def allowed(cfg: dict) -> list[dict]:
    out = []
    for entry in _read_list(allow_path(cfg)):
        name = str(entry.get("name") or "").strip()
        if name:
            out.append({"name": name, "uuid": str(entry.get("uuid") or "")})
    out.sort(key=lambda e: e["name"].lower())
    return out


def _expired(raw: str) -> bool:
    """Ist eine Sperre auf Zeit schon abgelaufen? Der Server räumt solche Einträge erst beim
    nächsten Start bzw. Anmeldeversuch weg – in der Datei stehen sie bis dahin weiter."""
    text = str(raw or "").strip()
    if not text or text.lower() == "forever":
        return False
    try:
        when = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return False                                   # unbekanntes Format: lieber als gültig zeigen
    return when.timestamp() <= time.time()


def banned(cfg: dict) -> list[dict]:
    if is_bedrock(cfg):
        return []                                      # der Bedrock-Server führt keine Sperrliste
    out = []
    for entry in _read_list(ban_path(cfg)):
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        expires = str(entry.get("expires") or "forever")
        out.append({"name": name, "uuid": str(entry.get("uuid") or ""),
                    "reason": str(entry.get("reason") or ""), "source": str(entry.get("source") or ""),
                    "created": str(entry.get("created") or ""),
                    "expires": expires, "expired": _expired(expires)})
    out.sort(key=lambda e: e["name"].lower())
    return out


def banned_ips(cfg: dict) -> list[dict]:
    """IP-Sperren aus banned-ips.json – nur zur Anzeige. Das Begleit-Plugin legt sie mit an, wenn
    ban.also_ban_ip eingeschaltet ist, und nimmt sie mit seinem /unban wieder heraus."""
    if is_bedrock(cfg):
        return []
    out = []
    for entry in _read_list(ban_ip_path(cfg)):
        ip = str(entry.get("ip") or "").strip()
        if not ip:
            continue
        expires = str(entry.get("expires") or "forever")
        out.append({"ip": ip, "reason": str(entry.get("reason") or ""),
                    "source": str(entry.get("source") or ""),
                    "expires": expires, "expired": _expired(expires)})
    out.sort(key=lambda e: e["ip"])
    return out


# ---------------------------------------------------------------- Wer ist online?

# „There are 2 of a max of 10 players online: Anna, Ben“ (Java) bzw. „There are 1/10 players online:“ (Bedrock).
_LIST_RE = re.compile(r"There are (\d+)(?:/| of a max(?:imum)? of )(\d+) players? online:?\s*(.*)$")
_PREFIX_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*:?\s*)+")


def _names_from(text: str, cfg: dict) -> list[str]:
    names = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        clean = read_name(cfg, part)
        if clean:
            names.append(clean)                        # Zusatztext der Serverantwort fällt hier heraus
    return names


def _parse_list(lines: list[str], cfg: dict) -> list[str] | None:
    """Antwort auf „list“ auswerten – None, solange sie noch nicht da ist."""
    for i, line in enumerate(lines):
        m = _LIST_RE.search(line)
        if not m:
            continue
        rest = m.group(3).strip()
        if not rest and int(m.group(1)) > 0 and i + 1 < len(lines):
            rest = _PREFIX_RE.sub("", lines[i + 1]).strip()     # Bedrock schreibt die Namen in die nächste Zeile
        return _names_from(rest, cfg)
    return None


def _from_plugin(cfg: dict) -> list[str] | None:
    """Spielerliste aus plugins/MCSMCompanion/status.json, falls das Plugin sie dort hinterlegt."""
    if not companion.applies(cfg):
        return None
    info = companion.status(cfg)
    raw = (info.get("status") or {}).get("players")
    age = info.get("age")
    if not isinstance(raw, list) or age is None or age > PLUGIN_MAX_AGE:
        return None
    names = []
    for item in raw:
        value = item.get("name") if isinstance(item, dict) else item
        clean = read_name(cfg, value)
        if clean:
            names.append(clean)
    return names


_online_cache: dict[str, tuple[float, list[str], bool]] = {}


def online(cfg: dict, fresh: bool = True) -> tuple[list[str], bool]:
    """(Namen, verlässlich?) – aus dem Begleit-Plugin, sonst über den Konsolenbefehl „list“.
    fresh=False nimmt eine kurz zuvor geholte Antwort, damit nicht jede Änderung ein „list“ in die
    Konsole schreibt."""
    if not manager.is_running(cfg["id"]):
        _online_cache.pop(cfg["id"], None)
        return [], True
    names = _from_plugin(cfg)
    if names is not None:
        return names, True
    cached = _online_cache.get(cfg["id"])
    if cached and not fresh and time.time() - cached[0] < ONLINE_CACHE:
        return cached[1], cached[2]
    inst = manager.instance(cfg)
    mark = inst.console(0)["next"]
    try:
        inst.send("list")
    except (RuntimeError, OSError):
        return [], False
    deadline = time.time() + LIST_WAIT
    while time.time() < deadline:
        time.sleep(0.2)
        found = _parse_list(inst.console(mark)["lines"], cfg)
        if found is not None:
            _online_cache[cfg["id"]] = (time.time(), found, True)
            return found, True
    return [], False


# ---------------------------------------------------------------- Gesamtbild und Aktionen

def overview(cfg: dict, message: str = "", warn: bool = False, fresh: bool = True) -> dict:
    names, known = online(cfg, fresh)
    paths = [allow_path(cfg)] + ([] if is_bedrock(cfg) else [ban_path(cfg), ban_ip_path(cfg)])
    return {
        "kind": "bedrock" if is_bedrock(cfg) else "java",
        "running": manager.is_running(cfg["id"]),
        "enabled": list_enabled(cfg),
        "allowed": allowed(cfg),
        "banned": banned(cfg),
        "banned_ips": banned_ips(cfg),
        "bans": not is_bedrock(cfg),
        # Dateien, die da sind, sich aber nicht lesen lassen – ohne Hinweis sähen die Listen
        # darunter einfach leer aus.
        "broken": _broken(*paths),
        "online": names,
        "online_known": known,
        "message": message,
        "warn": warn,
    }


def _send(cfg: dict, *commands: str) -> None:
    inst = manager.instance(cfg)
    try:
        for command in commands:
            inst.send(command)
    except (RuntimeError, OSError) as exc:
        raise ValueError(f"Der Befehl konnte nicht an den Server geschickt werden: {exc}") from exc
    time.sleep(SETTLE)                                 # der Server schreibt seine Dateien gleich danach


def _has(entries: list[dict], name: str) -> bool:
    return any(e["name"].lower() == name.lower() for e in entries)


def _kicked(inst, mark: int) -> bool:
    """Hat der Server den Rauswurf bestätigt? Das Begleit-Plugin meldet „… wurde hinausgeworfen“,
    Vanilla „Kicked …“. Bleibt beides aus, hat der Befehl nichts bewirkt – das Plugin lehnt ihn ab,
    wenn features.ban ausgeschaltet ist oder der Spieler geschützt bzw. gar nicht online ist."""
    deadline = time.time() + KICK_WAIT
    while True:
        if any(_KICK_OK_RE.search(line) for line in inst.console(mark)["lines"]):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def _arg(name: str) -> str:
    """Name als Befehlsargument – Bedrock-Gamertags mit Leerzeichen gehören in Anführungszeichen."""
    return f'"{name}"' if " " in name else name


def apply_action(cfg: dict, action: str, name="", reason="", duration="") -> dict:
    """Eine Änderung ausführen und das neue Gesamtbild zurückgeben."""
    if action not in ACTIONS:
        raise ValueError("Unbekannte Aktion.")
    bedrock = is_bedrock(cfg)
    running = manager.is_running(cfg["id"])
    word = "allowlist" if bedrock else "whitelist"
    label = "Erlaubnisliste" if bedrock else "Freigabeliste"

    def done(message: str, warn: bool = False) -> dict:
        # Nur Hinauswerfen und Sperren ändern, wer gerade spielt – sonst genügt die zuletzt geholte Liste.
        return overview(cfg, message, warn, fresh=action in ("kick", "ban"))

    if action in ("ban", "unban") and bedrock:
        raise ValueError("Der Bedrock-Server führt keine eigene Sperrliste. Nimm den Spieler aus der "
                         "Erlaubnisliste und schalte sie ein – dann kommt er nicht mehr herein.")

    # -- Liste ein- oder ausschalten
    if action in ("whitelist_on", "whitelist_off"):
        on = action == "whitelist_on"
        if running:
            _send(cfg, f"{word} {'on' if on else 'off'}", f"{word} reload")
        _set_enabled(cfg, on)
        return done(f"{label} ist jetzt {'eingeschaltet' if on else 'ausgeschaltet'}."
                    + ("" if running else " Es gilt ab dem nächsten Start."))

    clean = check_name(cfg, name)

    # -- Freigabeliste
    if action == "whitelist_add":
        if running:
            _send(cfg, f"{word} add {_arg(clean)}")
            if not _has(allowed(cfg), clean):
                return done(f"Der Server hat „{clean}“ nicht aufgenommen – die Konsole zeigt, woran es lag "
                            "(meist ein Tippfehler im Namen).", True)
            return done(f"„{clean}“ steht jetzt auf der {label}.")
        # strict: eine beschädigte Datei darf nicht als leere Liste durchgehen, sonst wären nach
        # dem Schreiben alle bisherigen Einträge weg.
        entries = _read_list(allow_path(cfg), strict=True)
        if any(str(e.get("name") or "").lower() == clean.lower() for e in entries):
            return done(f"„{clean}“ steht bereits auf der {label}.")
        if bedrock:
            entries.append({"ignoresPlayerLimit": False, "name": clean})
        else:
            if not JAVA_NAME_RE.fullmatch(clean):
                return done(f"„{clean}“ sieht nach einem Bedrock-Spieler aus (Crossplay). Dafür muss der "
                            "Server laufen – bitte starten und es dann noch einmal versuchen.", True)
            uuid, clean = profile_for(cfg, clean)
            entries.append({"uuid": uuid, "name": clean})
        _write_list(allow_path(cfg), entries)
        return done(f"„{clean}“ steht jetzt auf der {label}.")

    if action == "whitelist_remove":
        if running:
            _send(cfg, f"{word} remove {_arg(clean)}")
        else:
            entries = _read_list(allow_path(cfg), strict=True)
            kept = [e for e in entries if str(e.get("name") or "").lower() != clean.lower()]
            if len(kept) == len(entries):
                return done(f"„{clean}“ stand nicht auf der {label}.")
            _write_list(allow_path(cfg), kept)
        return done(f"„{clean}“ steht nicht mehr auf der {label}.")

    # -- Sperrliste (nur Java)
    if action == "ban":
        text = _clean_reason(reason, "Vom Serverbesitzer gesperrt.")
        seconds = _duration_seconds(duration)
        if running:
            if seconds:
                raise ValueError("Eine Sperre auf Zeit kann der laufende Server nicht selbst setzen. "
                                 "Sperre dauerhaft – oder stoppe den Server, dann trägt der Manager die Frist ein.")
            _send(cfg, f"ban {_arg(clean)} {text}")
            if not _has(banned(cfg), clean):
                return done(f"Der Server hat „{clean}“ nicht gesperrt – die Konsole zeigt, woran es lag "
                            "(der Name muss dem Server bekannt sein).", True)
            return done(f"„{clean}“ ist gesperrt.")
        uuid, clean = profile_for(cfg, clean)
        entries = [e for e in _read_list(ban_path(cfg), strict=True)
                   if str(e.get("name") or "").lower() != clean.lower()]
        now = time.time()
        entries.append({"uuid": uuid, "name": clean, "created": _stamp(now), "source": "Server Manager",
                        "expires": _stamp(now + seconds) if seconds else "forever", "reason": text})
        _write_list(ban_path(cfg), entries)
        return done(f"„{clean}“ ist gesperrt." + (" Die Sperre endet von allein." if seconds else ""))

    if action == "unban":
        if running:
            # Zuerst der Befehl des Begleit-Plugins: sein /unban nimmt auch die IP-Sperre heraus,
            # die beim Sperren mit ban.also_ban_ip angelegt wurde. Vanilla-pardon kennt nur
            # banned-players.json und ließe den Spieler weiter draußen.
            plugin_here = companion.applies(cfg)
            if plugin_here:
                _send(cfg, f"unban {_arg(clean)}")
            # Ohne Plugin – oder wenn es den Befehl abgelehnt hat – bleibt der Vanilla-Weg.
            if not plugin_here or _has(banned(cfg), clean):
                _send(cfg, f"pardon {_arg(clean)}")
            if _has(banned(cfg), clean):
                return done(f"Der Server hat die Sperre für „{clean}“ nicht aufgehoben – die Konsole "
                            "zeigt, woran es lag.", True)
        else:
            entries = _read_list(ban_path(cfg), strict=True)
            kept = [e for e in entries if str(e.get("name") or "").lower() != clean.lower()]
            if len(kept) == len(entries):
                return done(f"„{clean}“ war nicht gesperrt.")
            _write_list(ban_path(cfg), kept)
        return done(f"„{clean}“ darf wieder mitspielen.")

    # -- Hinauswerfen (nur solange der Server läuft)
    if not running:
        raise ValueError("Der Server läuft nicht – es ist niemand da, der hinausgeschickt werden könnte.")
    inst = manager.instance(cfg)
    mark = inst.console(0)["next"]
    _send(cfg, f"kick {_arg(clean)} {_clean_reason(reason, 'Vom Serverbesitzer hinausgeschickt.')}")
    # Auf Paper belegt das Begleit-Plugin /kick und kann den Befehl ablehnen – dann darf hier keine
    # Erfolgsmeldung stehen. Der Bedrock-Server hat dieses Plugin nicht.
    if companion.applies(cfg) and not _kicked(inst, mark):
        return done(f"Der Server hat „{clean}“ nicht hinausgeworfen – die Konsole zeigt, woran es lag "
                    "(der Spieler ist nicht online, ist geschützt, oder das Begleit-Plugin lässt es "
                    "nicht zu).", True)
    return done(f"„{clean}“ wurde vom Server geschickt.")
