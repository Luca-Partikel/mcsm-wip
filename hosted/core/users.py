"""Konten, Einladungscodes und Sitzungstoken des Hosted-Modus.

Ein Konto entsteht nur durch das Einlösen eines Einladungscodes, den der Betreiber ausgestellt hat –
es gibt keine offene Registrierung. Die Anmeldung über Discord verknüpft ein bestehendes Konto
(oder legt beim ersten Mal eines an, wenn ein Einladungscode dabei ist); der eigentliche
OAuth-Ablauf kommt später und ruft nur die Funktionen hier auf.

Sitzungstoken werden **nur als SHA256-Hash** gespeichert. Wer die Datei sessions.json in die Finger
bekommt, kann damit also keine Sitzung übernehmen. Das Klartext-Token gibt es genau einmal – als
Rückgabe von `create_session`.
"""
from __future__ import annotations

import hashlib
import re
import secrets

from . import store_hosted

ROLES = ("admin", "user")

NAME_MIN = 2
NAME_MAX = 32
# Anzeigename: Buchstaben (auch Umlaute), Ziffern, Leerzeichen, Punkt, Unterstrich, Bindestrich.
_NAME_RE = re.compile(r"[A-Za-zÄÖÜäöüß0-9](?:[A-Za-zÄÖÜäöüß0-9 ._\-]*[A-Za-zÄÖÜäöüß0-9._\-])?")
# Discord-Kennung ist eine „Snowflake“: nur Ziffern.
_DISCORD_RE = re.compile(r"[0-9]{15,22}")

# Zeichenvorrat der Einladungscodes – ohne I, O, 0 und 1, damit nichts verwechselt wird.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_GROUPS = 3
CODE_GROUP_LEN = 4
_CODE_RE = re.compile(r"[A-HJ-NP-Z2-9]{%d}" % (CODE_GROUPS * CODE_GROUP_LEN))

INVITE_DAYS_DEFAULT = 7
INVITE_DAYS_MAX = 365
INVITE_USES_MAX = 50

SESSION_DAYS_DEFAULT = 30
SESSION_DAYS_MAX = 90
# So oft wird „zuletzt gesehen“ höchstens fortgeschrieben (sonst schreibt jede Anfrage die Datei).
SESSION_TOUCH_SECONDS = 300


# ----------------------------------------------------------------------- Prüfungen

def clean_name(raw) -> str:
    """Anzeigenamen prüfen und aufräumen. Ungültige Namen lösen einen ValueError aus."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if len(name) < NAME_MIN or len(name) > NAME_MAX:
        raise ValueError(f"Der Name muss zwischen {NAME_MIN} und {NAME_MAX} Zeichen lang sein.")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("Im Namen sind nur Buchstaben, Ziffern, Leerzeichen, Punkt, "
                         "Unterstrich und Bindestrich erlaubt.")
    return name


def clean_discord_id(raw) -> str:
    """Discord-Kennung prüfen. Leere Angabe ist erlaubt (Konto ohne Discord-Verknüpfung)."""
    value = str(raw or "").strip()
    if not value:
        return ""
    if not _DISCORD_RE.fullmatch(value):
        raise ValueError("Die Discord-Kennung besteht nur aus Ziffern (15 bis 22 Stellen).")
    return value


def clean_role(raw) -> str:
    role = str(raw or "").strip().lower()
    if role not in ROLES:
        raise ValueError("Die Rolle muss „admin“ oder „user“ sein.")
    return role


def normalize_code(raw) -> str:
    """Einladungscode auf die Vergleichsform bringen: Großbuchstaben, ohne Trennzeichen."""
    value = re.sub(r"[\s\-_.]", "", str(raw or "")).upper()
    if not _CODE_RE.fullmatch(value):
        raise ValueError("Das ist kein gültiger Einladungscode – erwartet werden 12 Zeichen, "
                         "z. B. ABCD-EFGH-JKLM.")
    return value


def format_code(code: str) -> str:
    """Code für die Anzeige in Vierergruppen zerlegen: ABCDEFGHJKLM -> ABCD-EFGH-JKLM."""
    return "-".join(code[i:i + CODE_GROUP_LEN] for i in range(0, len(code), CODE_GROUP_LEN))


# ----------------------------------------------------------------------- Konten

def all_users() -> list[dict]:
    return store_hosted.load("users")


def get_user(user_id: str) -> dict | None:
    return store_hosted.get_by("users", "id", str(user_id or ""))


def find_by_name(name: str) -> dict | None:
    """Konto über den Anzeigenamen finden (Groß-/Kleinschreibung spielt keine Rolle)."""
    wanted = re.sub(r"\s+", " ", str(name or "")).strip().casefold()
    for user in all_users():
        if str(user.get("name", "")).casefold() == wanted:
            return user
    return None


def find_by_discord(discord_id: str) -> dict | None:
    value = str(discord_id or "").strip()
    if not value:
        return None
    return store_hosted.get_by("users", "discord_id", value)


def count_admins(*, exclude: str = "") -> int:
    """Anzahl der nutzbaren Admin-Konten (gesperrte zählen nicht mit)."""
    return sum(1 for u in all_users()
               if u.get("role") == "admin" and not u.get("blocked") and u.get("id") != exclude)


def create_user(name: str, *, discord_id: str = "", role: str = "user", now: int | None = None) -> dict:
    """Neues Konto anlegen. Name und Discord-Kennung dürfen noch nicht belegt sein."""
    name = clean_name(name)
    discord_id = clean_discord_id(discord_id)
    role = clean_role(role)
    stamp = store_hosted.now() if now is None else int(now)

    def apply(records: list[dict]) -> dict:
        for existing in records:
            if str(existing.get("name", "")).casefold() == name.casefold():
                raise ValueError(f"Den Namen „{name}“ gibt es schon – bitte einen anderen wählen.")
            if discord_id and existing.get("discord_id") == discord_id:
                raise ValueError("Dieses Discord-Konto ist bereits mit einem Konto verknüpft.")
        user = {
            "id": store_hosted.new_id(),
            "name": name,
            "discord_id": discord_id,
            "role": role,
            "created_at": stamp,
            "blocked": False,
            "blocked_reason": "",
        }
        records.append(user)
        return dict(user)

    with store_hosted.lock():
        return store_hosted.update("users", apply)


def rename_user(user_id: str, name: str) -> dict:
    """Anzeigenamen ändern."""
    name = clean_name(name)
    with store_hosted.lock():
        other = find_by_name(name)
        if other and other.get("id") != user_id:
            raise ValueError(f"Den Namen „{name}“ gibt es schon – bitte einen anderen wählen.")
        return store_hosted.patch_by("users", "id", user_id, {"name": name}, _missing(user_id))


def set_role(user_id: str, role: str) -> dict:
    """Rolle ändern. Das letzte Admin-Konto kann sich nicht selbst entmachten."""
    role = clean_role(role)
    with store_hosted.lock():
        user = get_user(user_id)
        if not user:
            raise ValueError(_missing(user_id))
        if user.get("role") == "admin" and role != "admin" and count_admins(exclude=user_id) == 0:
            raise ValueError("Das ist das letzte Admin-Konto – erst ein weiteres anlegen, "
                             "dann die Rolle ändern.")
        return store_hosted.patch_by("users", "id", user_id, {"role": role}, _missing(user_id))


_SERVER_USER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def set_server_user(user_id: str, name: str) -> dict:
    """Den Unix-Benutzer merken, unter dem die Server dieses Kontos laufen (siehe core/isolation).

    Steht nur im Datensatz, nicht in `public_user`: es ist eine Sache des Betriebssystems und
    gehört nicht in die Anzeige des Programms auf dem PC.
    """
    wanted = str(name or "").strip()
    if wanted and not _SERVER_USER_RE.match(wanted):
        raise ValueError("Ungültiger Name für den Server-Benutzer.")
    return store_hosted.patch_by("users", "id", user_id, {"server_user": wanted},
                                 _missing(user_id))


def set_blocked(user_id: str, blocked: bool, reason: str = "") -> dict:
    """Konto sperren oder entsperren. Ein gesperrtes Konto kann keine Server starten."""
    blocked = bool(blocked)
    text = re.sub(r"[\x00-\x1f]", " ", str(reason or "")).strip()[:200]
    with store_hosted.lock():
        user = get_user(user_id)
        if not user:
            raise ValueError(_missing(user_id))
        if blocked and user.get("role") == "admin" and count_admins(exclude=user_id) == 0:
            raise ValueError("Das letzte Admin-Konto kann nicht gesperrt werden.")
        changes = {"blocked": blocked, "blocked_reason": text if blocked else ""}
        result = store_hosted.patch_by("users", "id", user_id, changes, _missing(user_id))
        if blocked:
            revoke_all_sessions(user_id)
        return result


def link_discord(user_id: str, discord_id: str) -> dict:
    """Discord-Konto mit einem bestehenden Konto verknüpfen."""
    discord_id = clean_discord_id(discord_id)
    if not discord_id:
        raise ValueError("Es wurde keine Discord-Kennung übergeben.")
    with store_hosted.lock():
        other = find_by_discord(discord_id)
        if other and other.get("id") != user_id:
            raise ValueError("Dieses Discord-Konto ist bereits mit einem anderen Konto verknüpft.")
        return store_hosted.patch_by("users", "id", user_id, {"discord_id": discord_id}, _missing(user_id))


def user_for_discord(discord_id: str, name: str = "", *, invite_code: str = "",
                     now: int | None = None) -> dict:
    """Schnittstelle für die Discord-Anmeldung: Konto finden oder – mit Einladungscode – anlegen.

    Ohne Einladungscode wird kein Konto angelegt; so kann sich niemand allein durch eine
    Discord-Anmeldung Zugang verschaffen. Der eigentliche OAuth-Ablauf (Zustand, Code-Tausch,
    Abruf des Profils) kommt später und liefert nur `discord_id` und `name` hierher.
    """
    discord_id = clean_discord_id(discord_id)
    if not discord_id:
        raise ValueError("Die Discord-Anmeldung hat keine Kennung geliefert.")
    with store_hosted.lock():
        user = find_by_discord(discord_id)
        if user:
            if user.get("blocked"):
                raise ValueError("Dieses Konto ist gesperrt. Bitte beim Betreiber melden.")
            return user
        if not invite_code:
            raise ValueError("Für diese Discord-Anmeldung gibt es noch kein Konto – "
                             "bitte zuerst einen Einladungscode einlösen.")
        wanted = str(name or "").strip()
        return redeem_invite(invite_code, wanted, discord_id=discord_id, now=now)


def delete_user(user_id: str) -> None:
    """Konto samt Sitzungen löschen. Pässe und Instanzen räumt der Daemon vorher ab."""
    with store_hosted.lock():
        user = get_user(user_id)
        if not user:
            raise ValueError(_missing(user_id))
        if user.get("role") == "admin" and count_admins(exclude=user_id) == 0:
            raise ValueError("Das letzte Admin-Konto kann nicht gelöscht werden.")
        revoke_all_sessions(user_id)
        store_hosted.remove_by("users", "id", user_id)


def public_user(user: dict) -> dict:
    """Konto für die API – ohne Felder, die niemanden außerhalb angehen."""
    return {
        "id": user.get("id", ""),
        "name": user.get("name", ""),
        "role": user.get("role", "user"),
        "created_at": int(user.get("created_at") or 0),
        "blocked": bool(user.get("blocked")),
        "blocked_reason": user.get("blocked_reason", "") or "",
        "discord_linked": bool(user.get("discord_id")),
    }


def _missing(user_id: str) -> str:
    return f"Es gibt kein Konto mit der Kennung „{user_id}“."


# ----------------------------------------------------------------------- Einladungen

def new_code() -> str:
    """Neuen Einladungscode erzeugen (Vergleichsform, ohne Trennstriche)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUPS * CODE_GROUP_LEN))


def all_invites() -> list[dict]:
    return store_hosted.load("invites")


def create_invite(created_by: str, *, uses_max: int = 1, days: int = INVITE_DAYS_DEFAULT,
                  note: str = "", now: int | None = None) -> dict:
    """Einladungscode ausstellen. `days = 0` bedeutet: läuft nicht ab."""
    try:
        uses_max = int(uses_max)
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("Verwendungen und Gültigkeit müssen ganze Zahlen sein.") from None
    if not 1 <= uses_max <= INVITE_USES_MAX:
        raise ValueError(f"Die Zahl der Verwendungen muss zwischen 1 und {INVITE_USES_MAX} liegen.")
    if not 0 <= days <= INVITE_DAYS_MAX:
        raise ValueError(f"Die Gültigkeit muss zwischen 0 und {INVITE_DAYS_MAX} Tagen liegen "
                         f"(0 = läuft nicht ab).")
    stamp = store_hosted.now() if now is None else int(now)

    def apply(records: list[dict]) -> dict:
        used = {r.get("code") for r in records}
        code = new_code()
        while code in used:
            code = new_code()
        invite = {
            "code": code,
            "created_by": str(created_by or ""),
            "created_at": stamp,
            "uses_max": uses_max,
            "expires_at": (stamp + days * 86400) if days else 0,
            "revoked_at": 0,
            "note": re.sub(r"[\x00-\x1f]", " ", str(note or "")).strip()[:120],
            "redeemed_by": [],
        }
        records.append(invite)
        return dict(invite)

    with store_hosted.lock():
        return store_hosted.update("invites", apply)


def get_invite(code: str) -> dict | None:
    """Einladung zu einem Code finden. Der Vergleich ist zeitkonstant (der Code ist ein Geheimnis)."""
    try:
        wanted = normalize_code(code)
    except ValueError:
        return None
    for invite in all_invites():
        if secrets.compare_digest(str(invite.get("code", "")), wanted):
            return invite
    return None


def invite_state(invite: dict, *, now: int | None = None) -> str:
    """Zustand einer Einladung: ``open``, ``used``, ``expired`` oder ``revoked``."""
    stamp = store_hosted.now() if now is None else int(now)
    if invite.get("revoked_at"):
        return "revoked"
    if len(invite.get("redeemed_by") or []) >= int(invite.get("uses_max") or 1):
        return "used"
    expires = int(invite.get("expires_at") or 0)
    if expires and stamp >= expires:
        return "expired"
    return "open"


def invite_state_text(invite: dict, *, now: int | None = None) -> str:
    """Zustand einer Einladung als deutscher Satzteil."""
    return {
        "open": "offen",
        "used": "aufgebraucht",
        "expired": "abgelaufen",
        "revoked": "widerrufen",
    }[invite_state(invite, now=now)]


def revoke_invite(code: str, *, now: int | None = None) -> dict:
    """Einladung zurückziehen; bereits angelegte Konten bleiben bestehen."""
    stamp = store_hosted.now() if now is None else int(now)
    with store_hosted.lock():
        invite = get_invite(code)
        if not invite:
            raise ValueError("Diesen Einladungscode gibt es nicht.")
        if invite.get("revoked_at"):
            return invite
        return store_hosted.patch_by("invites", "code", invite["code"], {"revoked_at": stamp})


def redeem_invite(code: str, name: str, *, discord_id: str = "", now: int | None = None) -> dict:
    """Einladung einlösen und dabei das Konto anlegen. Rückgabe ist das neue Konto."""
    stamp = store_hosted.now() if now is None else int(now)
    with store_hosted.lock():
        invite = get_invite(code)
        if not invite:
            raise ValueError("Dieser Einladungscode ist unbekannt. Bitte genau so eintragen, "
                             "wie er vom Betreiber kommt.")
        state = invite_state(invite, now=stamp)
        if state == "revoked":
            raise ValueError("Dieser Einladungscode wurde zurückgezogen.")
        if state == "expired":
            raise ValueError("Dieser Einladungscode ist abgelaufen. Bitte beim Betreiber einen "
                             "neuen anfordern.")
        if state == "used":
            raise ValueError("Dieser Einladungscode ist schon aufgebraucht.")
        user = create_user(name, discord_id=discord_id, role="user", now=stamp)
        entry = {"user_id": user["id"], "at": stamp}
        try:
            redeemed = list(invite.get("redeemed_by") or []) + [entry]
            store_hosted.patch_by("invites", "code", invite["code"], {"redeemed_by": redeemed})
        except Exception:
            store_hosted.remove_by("users", "id", user["id"])   # Konto nicht halb angelegt liegen lassen
            raise
        return user


# ----------------------------------------------------------------------- Sitzungen

def token_hash(token: str) -> str:
    """SHA256 des Tokens – nur dieser Wert wird gespeichert."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def all_sessions() -> list[dict]:
    return store_hosted.load("sessions")


def create_session(user_id: str, *, days: int = SESSION_DAYS_DEFAULT, note: str = "",
                   now: int | None = None) -> tuple[str, dict]:
    """Sitzung anlegen. Rückgabe: (Klartext-Token, Datensatz) – das Token gibt es nur hier."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("Die Gültigkeit der Sitzung muss eine ganze Zahl von Tagen sein.") from None
    if not 1 <= days <= SESSION_DAYS_MAX:
        raise ValueError(f"Die Gültigkeit einer Sitzung muss zwischen 1 und {SESSION_DAYS_MAX} Tagen liegen.")
    stamp = store_hosted.now() if now is None else int(now)
    with store_hosted.lock():
        user = get_user(user_id)
        if not user:
            raise ValueError(_missing(user_id))
        if user.get("blocked"):
            raise ValueError("Dieses Konto ist gesperrt. Bitte beim Betreiber melden.")
        token = secrets.token_urlsafe(32)
        session = {
            "token_hash": token_hash(token),
            "user_id": user["id"],
            "created_at": stamp,
            "expires_at": stamp + days * 86400,
            "last_seen": stamp,
            "revoked_at": 0,
            "note": re.sub(r"[\x00-\x1f]", " ", str(note or "")).strip()[:80],
        }

        def apply(records: list[dict]) -> None:
            records.append(session)

        store_hosted.update("sessions", apply)
        return token, dict(session)


def session_for_token(token: str, *, now: int | None = None) -> dict | None:
    """Gültige Sitzung zu einem Token finden – oder None (abgelaufen, widerrufen, unbekannt)."""
    if not token:
        return None
    stamp = store_hosted.now() if now is None else int(now)
    wanted = token_hash(token)
    for session in all_sessions():
        if not secrets.compare_digest(str(session.get("token_hash", "")), wanted):
            continue
        if session.get("revoked_at"):
            return None
        if stamp >= int(session.get("expires_at") or 0):
            return None
        return session
    return None


def user_for_token(token: str, *, now: int | None = None) -> dict | None:
    """Konto zu einem Sitzungstoken. None, wenn die Sitzung nicht gilt oder das Konto gesperrt ist."""
    stamp = store_hosted.now() if now is None else int(now)
    session = session_for_token(token, now=stamp)
    if not session:
        return None
    user = get_user(session.get("user_id", ""))
    if not user or user.get("blocked"):
        return None
    if stamp - int(session.get("last_seen") or 0) >= SESSION_TOUCH_SECONDS:
        try:
            store_hosted.patch_by("sessions", "token_hash", session["token_hash"], {"last_seen": stamp})
        except ValueError:
            pass                  # inzwischen abgemeldet – das Konto stimmt trotzdem
    return user


def revoke_session(token: str, *, now: int | None = None) -> bool:
    """Sitzung beenden (Abmelden). Rückgabe: ob es die Sitzung gab."""
    stamp = store_hosted.now() if now is None else int(now)
    wanted = token_hash(token)

    def apply(records: list[dict]) -> bool:
        for record in records:
            if secrets.compare_digest(str(record.get("token_hash", "")), wanted):
                if not record.get("revoked_at"):
                    record["revoked_at"] = stamp
                return True
        return False

    return store_hosted.update("sessions", apply)


def revoke_all_sessions(user_id: str, *, now: int | None = None) -> int:
    """Alle Sitzungen eines Kontos beenden. Rückgabe: Anzahl."""
    stamp = store_hosted.now() if now is None else int(now)

    def apply(records: list[dict]) -> int:
        count = 0
        for record in records:
            if record.get("user_id") == user_id and not record.get("revoked_at"):
                record["revoked_at"] = stamp
                count += 1
        return count

    return store_hosted.update("sessions", apply)


def sessions_of(user_id: str, *, now: int | None = None, only_valid: bool = True) -> list[dict]:
    """Sitzungen eines Kontos – ohne Token-Hash, damit nichts davon nach außen gerät."""
    stamp = store_hosted.now() if now is None else int(now)
    out = []
    for session in all_sessions():
        if session.get("user_id") != user_id:
            continue
        valid = not session.get("revoked_at") and stamp < int(session.get("expires_at") or 0)
        if only_valid and not valid:
            continue
        out.append({
            "user_id": session.get("user_id", ""),
            "created_at": int(session.get("created_at") or 0),
            "expires_at": int(session.get("expires_at") or 0),
            "last_seen": int(session.get("last_seen") or 0),
            "note": session.get("note", "") or "",
            "valid": valid,
        })
    return out


def purge_sessions(*, now: int | None = None, keep_days: int = 7) -> int:
    """Abgelaufene und widerrufene Sitzungen aufräumen. Rückgabe: Anzahl der gelöschten Einträge."""
    stamp = store_hosted.now() if now is None else int(now)
    cutoff = stamp - max(0, int(keep_days)) * 86400

    def apply(records: list[dict]) -> int:
        keep = []
        for record in records:
            expired = int(record.get("expires_at") or 0) <= cutoff
            revoked = record.get("revoked_at") and int(record["revoked_at"]) <= cutoff
            if expired or revoked:
                continue
            keep.append(record)
        removed = len(records) - len(keep)
        records[:] = keep
        return removed

    return store_hosted.update("sessions", apply)
