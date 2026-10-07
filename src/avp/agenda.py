"""Google Agenda et Gmail : disponibilités de JB, rendez-vous, emails de confirmation.

Fonctionnement :

- **Disponibilités** : `GoogleClient.freebusy()` lit les plages occupées de l'agenda de JB. Le
  planificateur les utilise pour ne proposer au prospect que des créneaux libres.
- **RDV direct** (mardi et jeudi par défaut, voir `rdv.plages` de la campagne) : après l'appel,
  un événement est créé dans l'agenda de JB avec le prospect en invité ; Google envoie l'invitation.
- **RDV à confirmer** (lundi et vendredi par défaut, `rdv.jours_a_confirmer`) : l'événement est créé
  avec le titre « [À CONFIRMER] … », sans invité, et JB reçoit un email. Pour confirmer, il retire
  « [À CONFIRMER] » du titre dans Google Agenda (même depuis son téléphone) : `watch_confirmations()`
  le détecte, ajoute le prospect en invité et Google envoie l'invitation. Pour refuser, il supprime
  l'événement.

Authentification : OAuth « Application de bureau » au nom de JB (jeton de rafraîchissement stocké
dans `data/google-token.json`, obtenu une fois avec `avp google auth`). Appels REST directs via
httpx : pas de dépendance aux bibliothèques Google, et des tests sans réseau.
"""

from __future__ import annotations

import base64
import json
import logging
import time as _time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings, get_settings
from .models import CallAnalysis, CallRecordFile, Campaign

log = logging.getLogger("avp.agenda")

SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/gmail.send",
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_BASE = "https://www.googleapis.com/calendar/v3"
GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

CONFIRM_PREFIX = "[À CONFIRMER]"
PROP_CALL = "avp_call_id"
PROP_STATUS = "avp_status"  # a_confirmer | confirme
PROP_EMAIL = "avp_prospect_email"
PROP_NAME = "avp_prospect_name"


class GoogleError(RuntimeError):
    pass


class GoogleNotConfigured(GoogleError):
    pass


# ---------------------------------------------------------------------------
# Jeton OAuth
# ---------------------------------------------------------------------------


def auth_url(settings: Settings, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode

    return AUTH_URL + "?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "login_hint": settings.axonaut_user_email,
        }
    )


def exchange_code(settings: Settings, code: str, redirect_uri: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Échange le code d'autorisation contre un jeton (avec refresh_token) et l'enregistre."""
    with httpx.Client(transport=transport, timeout=20) as c:
        r = c.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if r.status_code != 200:
        raise GoogleError(f"échange du code refusé : {r.status_code} {r.text[:300]}")
    tok = r.json()
    if "refresh_token" not in tok:
        raise GoogleError("Google n'a pas renvoyé de refresh_token (relancer avec prompt=consent)")
    save_token(settings.google_token_file, tok)
    return tok


def save_token(path: Path, tok: dict) -> None:
    data = dict(tok)
    if "expires_in" in data:
        data["expires_at"] = int(_time.time()) + int(data.pop("expires_in")) - 60
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Client REST
# ---------------------------------------------------------------------------


class GoogleClient:
    """Client minimal Calendar + Gmail. Utilisable en `async with`."""

    def __init__(self, settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings or get_settings()
        if not (self.s.google_client_id and self.s.google_client_secret):
            raise GoogleNotConfigured("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET absents du .env")
        if not self.s.google_token_file.exists():
            raise GoogleNotConfigured("jeton Google absent : lancer `avp google auth`")
        self._tok = json.loads(self.s.google_token_file.read_text(encoding="utf-8"))
        self._http = httpx.AsyncClient(transport=transport, timeout=20)

    async def __aenter__(self) -> GoogleClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._tok.get("access_token") and int(self._tok.get("expires_at", 0)) > _time.time():
            return self._tok["access_token"]
        r = await self._http.post(
            TOKEN_URL,
            data={
                "client_id": self.s.google_client_id,
                "client_secret": self.s.google_client_secret,
                "refresh_token": self._tok["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if r.status_code != 200:
            raise GoogleError(f"rafraîchissement du jeton refusé ({r.status_code}) : relancer `avp google auth`")
        new = r.json()
        new.setdefault("refresh_token", self._tok["refresh_token"])
        save_token(self.s.google_token_file, new)
        self._tok = json.loads(self.s.google_token_file.read_text(encoding="utf-8"))
        return self._tok["access_token"]

    async def _req(self, method: str, url: str, **kw: Any) -> dict:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        r = await self._http.request(method, url, headers=headers, **kw)
        if r.status_code >= 400:
            raise GoogleError(f"{method} {url.split('?')[0]} → {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    @property
    def _cal(self) -> str:
        from urllib.parse import quote

        return f"{CAL_BASE}/calendars/{quote(self.s.google_calendar_id, safe='')}"

    # -- Agenda ------------------------------------------------------------

    async def freebusy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        data = await self._req(
            "POST",
            f"{CAL_BASE}/freeBusy",
            json={
                "timeMin": start.astimezone(UTC).isoformat(),
                "timeMax": end.astimezone(UTC).isoformat(),
                "items": [{"id": self.s.google_calendar_id}],
            },
        )
        cal = (data.get("calendars") or {}).get(self.s.google_calendar_id) or next(
            iter((data.get("calendars") or {}).values()), {}
        )
        if cal.get("errors"):
            raise GoogleError(f"freebusy : {cal['errors']}")
        return [
            (datetime.fromisoformat(b["start"].replace("Z", "+00:00")), datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
            for b in cal.get("busy", [])
        ]

    async def insert_event(self, body: dict, *, send_updates: str = "none") -> dict:
        return await self._req("POST", f"{self._cal}/events", params={"sendUpdates": send_updates}, json=body)

    async def patch_event(self, event_id: str, body: dict, *, send_updates: str = "none") -> dict:
        return await self._req(
            "PATCH", f"{self._cal}/events/{event_id}", params={"sendUpdates": send_updates}, json=body
        )

    async def list_events(self, *, private: dict[str, str], time_min: datetime, show_deleted: bool = True) -> list[dict]:
        params: list[tuple[str, str]] = [
            ("timeMin", time_min.astimezone(UTC).isoformat()),
            ("singleEvents", "true"),
            ("showDeleted", "true" if show_deleted else "false"),
            ("maxResults", "250"),
        ]
        params += [("privateExtendedProperty", f"{k}={v}") for k, v in private.items()]
        data = await self._req("GET", f"{self._cal}/events", params=params)
        return data.get("items", [])

    async def calendar_info(self) -> dict:
        return await self._req("GET", self._cal)

    # -- Gmail --------------------------------------------------------------

    async def send_mail(self, to: str, subject: str, body: str, *, sender_name: str = "Agent vocal OpteoLink") -> dict:
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = formataddr((sender_name, self.s.axonaut_user_email))
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return await self._req("POST", GMAIL_SEND, json={"raw": raw})


# ---------------------------------------------------------------------------
# Logique pure : événements et emails
# ---------------------------------------------------------------------------


def _fmt(dt: datetime, tz: ZoneInfo) -> str:
    from .prompts import format_slot_fr

    return format_slot_fr(dt, str(tz))


def build_rdv_event(record: CallRecordFile, analysis: CallAnalysis | None, campaign: Campaign,
                    settings: Settings, *, to_confirm: bool) -> dict:
    """Corps d'un événement Google Agenda pour le RDV pris pendant l'appel."""
    rdv = record.state.rdv
    assert rdv is not None
    p = record.metadata.prospect
    start = rdv.start if rdv.start.tzinfo else rdv.start.replace(tzinfo=UTC)
    end = start + timedelta(minutes=rdv.duree_min)
    title = f"RDV OpteoLink — {p.name}"
    if to_confirm:
        title = f"{CONFIRM_PREFIX} {title}"
    lines = [
        f"Rendez-vous pris par l'agent vocal OpteoLink ({campaign.nom}).",
        f"Établissement : {p.name}{f', {p.city}' if p.city else ''}",
        f"Interlocuteur : {rdv.avec or record.state.contact_name or '-'}"
        f"{f' ({record.state.contact_role})' if record.state.contact_role else ''}",
        f"Email : {rdv.email or '-'}",
        f"Téléphone : {p.phone}",
        f"Mode : {rdv.mode or campaign.rdv.mode}",
    ]
    if analysis:
        lines += ["", f"Résumé de l'appel : {analysis.resume}"]
        if analysis.besoin:
            lines.append(f"Besoin : {analysis.besoin}")
        if analysis.equipement_actuel:
            lines.append(f"Équipement actuel : {analysis.equipement_actuel}")
    lines += ["", f"Réf. appel : {record.metadata.call_id}"]
    private = {
        PROP_CALL: record.metadata.call_id,
        PROP_STATUS: "a_confirmer" if to_confirm else "confirme",
        PROP_EMAIL: rdv.email,
        PROP_NAME: rdv.avec or record.state.contact_name,
    }
    body: dict[str, Any] = {
        "summary": title,
        "description": "\n".join(lines),
        "start": {"dateTime": start.isoformat(), "timeZone": settings.timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.timezone},
        "extendedProperties": {"private": private},
        "reminders": {"useDefault": True},
    }
    if to_confirm:
        body["status"] = "tentative"
    elif rdv.email:
        body["attendees"] = [{"email": rdv.email, "displayName": rdv.avec or p.name}]
    return body


def build_confirmation_email(record: CallRecordFile, analysis: CallAnalysis | None, event: dict,
                             settings: Settings, *, reason: str) -> tuple[str, str]:
    rdv = record.state.rdv
    assert rdv is not None
    tz = ZoneInfo(settings.timezone)
    p = record.metadata.prospect
    when = _fmt(rdv.start, tz)
    subject = f"RDV à confirmer : {p.name}, {when}"
    body = f"""Bonjour Jean-Baptiste,

L'agent vocal a obtenu un rendez-vous qui demande ta confirmation ({reason}).

Établissement : {p.name}{f' ({p.city})' if p.city else ''}
Interlocuteur : {rdv.avec or record.state.contact_name or '-'}
Email : {rdv.email or '-'}
Téléphone : {p.phone}
Créneau : {when} ({rdv.duree_min} min, {rdv.mode or 'mode à préciser'})
"""
    if analysis:
        body += f"\nRésumé : {analysis.resume}\nScore : {analysis.score} ({analysis.score_num}/100)\n"
    body += f"""
Le créneau est réservé dans ton agenda avec le titre « {CONFIRM_PREFIX} … ».

- Pour CONFIRMER : retire « {CONFIRM_PREFIX} » du titre de l'événement (Google Agenda, même sur téléphone).
  L'invitation part alors automatiquement à {rdv.email or "l'interlocuteur"}.
- Pour REFUSER : supprime l'événement, puis rappelle le prospect pour proposer un autre créneau.

Événement : {event.get('htmlLink', '(lien indisponible)')}
Réf. appel : {record.metadata.call_id}
"""
    return subject, body


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(start < b_end and b_start < end for b_start, b_end in busy)


# ---------------------------------------------------------------------------
# Synchronisation après l'appel
# ---------------------------------------------------------------------------


async def sync_agenda(record: CallRecordFile, analysis: CallAnalysis | None, campaign: Campaign, *,
                      google: GoogleClient, settings: Settings | None = None) -> dict:
    """Crée l'événement du RDV pris pendant l'appel. Retourne un récapitulatif."""
    settings = settings or get_settings()
    rdv = record.state.rdv
    if rdv is None:
        return {"skipped": "pas de RDV"}
    if record.metadata.test_mode:
        return {"skipped": "appel de test (test_mode)"}
    if settings.dry_run:
        return {"skipped": "DRY_RUN"}

    start = rdv.start if rdv.start.tzinfo else rdv.start.replace(tzinfo=UTC)
    end = start + timedelta(minutes=rdv.duree_min)
    reasons: list[str] = []
    if rdv.a_confirmer or campaign.rdv.needs_confirmation(start, settings.timezone):
        reasons.append("jour hors mardi/jeudi")
    if not rdv.email:
        reasons.append("email du prospect manquant")
    try:
        if _overlaps(start, end, await google.freebusy(start, end)):
            reasons.append("conflit avec un autre événement de l'agenda")
    except GoogleError as exc:
        log.warning("vérification des disponibilités impossible : %s", exc)

    to_confirm = bool(reasons)
    body = build_rdv_event(record, analysis, campaign, settings, to_confirm=to_confirm)
    event = await google.insert_event(body, send_updates="none" if to_confirm else "all")
    result: dict[str, Any] = {"event_id": event.get("id"), "a_confirmer": to_confirm}
    if to_confirm:
        subject, text = build_confirmation_email(record, analysis, event, settings, reason=", ".join(reasons))
        await google.send_mail(settings.confirm_email, subject, text)
        result["email"] = settings.confirm_email
    log.info("RDV %s posé dans l'agenda (%s)", record.metadata.call_id,
             "à confirmer : " + ", ".join(reasons) if to_confirm else "invitation envoyée")
    return result


async def watch_confirmations(google: GoogleClient, settings: Settings | None = None) -> dict[str, int]:
    """Traite les RDV « à confirmer » : dès que JB retire le préfixe du titre, l'invitation part au prospect."""
    settings = settings or get_settings()
    counts = {"confirmes": 0, "en_attente": 0}
    # Un événement supprimé par JB (refus) disparaît de la liste : rien à faire.
    events = await google.list_events(
        private={PROP_STATUS: "a_confirmer"}, time_min=datetime.now(UTC) - timedelta(days=1), show_deleted=False
    )
    for ev in events:
        props = (ev.get("extendedProperties") or {}).get("private") or {}
        if ev.get("status") == "cancelled":
            continue
        if (ev.get("summary") or "").startswith(CONFIRM_PREFIX):
            counts["en_attente"] += 1
            continue  # pas encore confirmé
        patch: dict[str, Any] = {
            "status": "confirmed",
            "extendedProperties": {"private": {PROP_STATUS: "confirme"}},
        }
        email = props.get(PROP_EMAIL)
        if email:
            attendees = [a for a in ev.get("attendees", []) if a.get("email") != email]
            patch["attendees"] = [*attendees, {"email": email, "displayName": props.get(PROP_NAME) or email}]
        await google.patch_event(ev["id"], patch, send_updates="all" if email else "none")
        counts["confirmes"] += 1
        log.info("RDV %s confirmé par JB, invitation envoyée à %s", props.get(PROP_CALL), email or "(aucun email)")
    return counts


class BusyCache:
    """Disponibilités de JB pour le planificateur, mises en cache quelques minutes."""

    def __init__(self, settings: Settings | None = None, ttl_s: int = 300, horizon_days: int = 21) -> None:
        self.s = settings or get_settings()
        self.ttl_s = ttl_s
        self.horizon = timedelta(days=horizon_days)
        self._at = 0.0
        self._busy: list[tuple[datetime, datetime]] = []

    async def __call__(self, now: datetime) -> list[tuple[datetime, datetime]]:
        if _time.monotonic() - self._at < self.ttl_s:
            return self._busy
        async with GoogleClient(self.s) as g:
            self._busy = await g.freebusy(now, now + self.horizon)
        self._at = _time.monotonic()
        return self._busy
