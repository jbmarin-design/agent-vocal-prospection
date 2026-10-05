"""Administration de LiveKit (auto-hébergé) via l'API serveur.

- trunk SIP sortant LiveKit → XiVO (création, liste, suppression) ;
- lancement d'un appel : création de la room `avp-<call_id>` puis *dispatch* explicite
  de l'agent `settings.agent_name` avec le `CallMetadata` en JSON ;
- raccrochage (suppression de la room) et liste des rooms actives.

Ce module n'importe que `livekit.api` (paquet `livekit-api`), et **paresseusement**
(à l'intérieur des fonctions) : il reste importable, et testable, sans le paquet.

`LiveKitAPI` accepte une URL `ws://` ou `wss://` : le client Twirp la convertit
lui-même en `http://` / `https://`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .config import Settings, get_settings
from .models import CallMetadata

if TYPE_CHECKING:  # pragma: no cover
    from types import ModuleType

logger = logging.getLogger("avp.livekit_admin")

ROOM_PREFIX = "avp-"
PARTICIPANT_PREFIX = "prospect-"
OUTBOUND_TRUNK_NAME = "avp-xivo"

# En-tête SIP ajouté à l'INVITE (par le worker) pour que le dialplan XiVO nomme
# l'enregistrement MixMonitor avec l'identifiant d'appel avp. Voir xivo/extensions_livekit.conf.
CALL_ID_HEADER = "X-AVP-Call-ID"

# Paramètres de la room créée pour chaque appel.
ROOM_EMPTY_TIMEOUT_S = 120  # room supprimée si personne ne la rejoint (worker absent)
ROOM_DEPARTURE_TIMEOUT_S = 10  # room supprimée 10 s après le départ du dernier participant
ROOM_MAX_PARTICIPANTS = 4  # agent + prospect + éventuel humain (transfert) + marge


class LiveKitAdminError(RuntimeError):
    """Erreur de configuration ou d'appel à l'API LiveKit, avec un message en français."""


# ---------------------------------------------------------------------------
# Nommage (fonctions pures)
# ---------------------------------------------------------------------------


def room_name_for(call_id: str) -> str:
    """Nom de la room LiveKit d'un appel : `avp-<call_id>`."""
    return f"{ROOM_PREFIX}{call_id}"


def participant_identity_for(call_id: str) -> str:
    """Identité du participant SIP (le prospect) dans la room : `prospect-<call_id>`."""
    return f"{PARTICIPANT_PREFIX}{call_id}"


def call_id_from_room(room_name: str) -> str | None:
    """Inverse de `room_name_for` : `avp-abc` → `abc` ; None si la room n'est pas une room avp."""
    if room_name.startswith(ROOM_PREFIX) and len(room_name) > len(ROOM_PREFIX):
        return room_name[len(ROOM_PREFIX) :]
    return None


def sip_headers_for(call_id: str) -> dict[str, str]:
    """En-têtes SIP à passer dans `CreateSIPParticipantRequest.headers` (côté worker).

    Le dialplan XiVO lit `X-AVP-Call-ID` pour nommer l'enregistrement ; à défaut il
    utilise le Call-ID SIP.
    """
    return {CALL_ID_HEADER: call_id}


# ---------------------------------------------------------------------------
# Accès paresseux à livekit.api
# ---------------------------------------------------------------------------


def _api() -> ModuleType:
    try:
        from livekit import api  # import paresseux : paquet livekit-api
    except ImportError as e:  # pragma: no cover - dépend de l'environnement
        raise LiveKitAdminError(
            "Le paquet 'livekit-api' est introuvable : installez le projet (pip install -e .)"
        ) from e
    return api


def _settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


def _client(settings: Settings) -> Any:
    """Client `LiveKitAPI`, à utiliser en `async with` (ferme la session aiohttp)."""
    if not settings.livekit_url:
        raise LiveKitAdminError("LIVEKIT_URL n'est pas renseigné dans .env")
    if not settings.livekit_api_key or not settings.livekit_api_secret:
        raise LiveKitAdminError("LIVEKIT_API_KEY / LIVEKIT_API_SECRET ne sont pas renseignés dans .env")
    api = _api()
    return api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


def _is_not_found(err: BaseException) -> bool:
    """Vrai si l'erreur Twirp est un 404 / not_found (room ou trunk déjà supprimé)."""
    return getattr(err, "code", None) == "not_found" or getattr(err, "status", None) == 404


def _transport_value(api: Any, transport: str) -> Any:
    mapping = {
        "udp": api.SIP_TRANSPORT_UDP,
        "tcp": api.SIP_TRANSPORT_TCP,
        "tls": api.SIP_TRANSPORT_TLS,
        "auto": api.SIP_TRANSPORT_AUTO,
    }
    try:
        return mapping[transport.lower()]
    except KeyError as e:
        raise LiveKitAdminError(f"transport SIP inconnu : {transport!r} (udp, tcp, tls)") from e


def _transport_name(api: Any, value: Any) -> str:
    names = {
        api.SIP_TRANSPORT_UDP: "udp",
        api.SIP_TRANSPORT_TCP: "tcp",
        api.SIP_TRANSPORT_TLS: "tls",
        api.SIP_TRANSPORT_AUTO: "auto",
    }
    return names.get(value, str(value))


# ---------------------------------------------------------------------------
# Trunk SIP sortant (LiveKit → XiVO)
# ---------------------------------------------------------------------------


def build_outbound_trunk_info(settings: Settings, api: Any | None = None) -> Any:
    """Construit le `SIPOutboundTrunkInfo` à partir des paramètres (sans appel réseau)."""
    api = api or _api()
    if not settings.xivo_sip_address:
        raise LiveKitAdminError("XIVO_SIP_ADDRESS n'est pas renseigné dans .env (IP ou FQDN du XiVO)")
    if not settings.sip_caller_number:
        raise LiveKitAdminError("SIP_CALLER_NUMBER n'est pas renseigné dans .env (numéro présenté, E.164)")
    if bool(settings.sip_trunk_username) != bool(settings.sip_trunk_password):
        raise LiveKitAdminError("SIP_TRUNK_USERNAME et SIP_TRUNK_PASSWORD vont ensemble (les deux ou aucun)")

    kwargs: dict[str, Any] = {
        "name": OUTBOUND_TRUNK_NAME,
        "address": settings.xivo_sip_address,
        "transport": _transport_value(api, settings.xivo_sip_transport),
        "numbers": [settings.sip_caller_number],
        "metadata": "agent-vocal-prospection",
    }
    if settings.sip_trunk_username:
        kwargs["auth_username"] = settings.sip_trunk_username
        kwargs["auth_password"] = settings.sip_trunk_password
    return api.SIPOutboundTrunkInfo(**kwargs)


def _trunk_to_dict(api: Any, t: Any) -> dict[str, Any]:
    return {
        "id": t.sip_trunk_id,
        "name": t.name,
        "address": t.address,
        "transport": _transport_name(api, t.transport),
        "numbers": list(t.numbers),
        "auth_username": t.auth_username,
        "metadata": t.metadata,
    }


async def list_outbound_trunks(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Liste les trunks SIP sortants déclarés dans LiveKit (sans les mots de passe)."""
    s = _settings(settings)
    api = _api()
    async with _client(s) as lk:
        resp = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
    return [_trunk_to_dict(api, t) for t in resp.items]


async def create_outbound_trunk(settings: Settings | None = None, *, reuse_existing: bool = True) -> str:
    """Crée le trunk SIP sortant vers le XiVO et retourne son ID (`ST_...`).

    Si `reuse_existing` (défaut) et qu'un trunk `avp-xivo` pointant vers la même adresse
    existe déjà, son ID est retourné sans en créer un second (commande rejouable).
    """
    s = _settings(settings)
    api = _api()
    info = build_outbound_trunk_info(s, api)
    async with _client(s) as lk:
        if reuse_existing:
            resp = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
            for t in resp.items:
                if t.name == OUTBOUND_TRUNK_NAME and t.address == s.xivo_sip_address:
                    logger.warning(
                        "trunk sortant %s déjà présent (%s → %s) : réutilisé",
                        t.sip_trunk_id, t.name, t.address,
                    )
                    return str(t.sip_trunk_id)
        created = await lk.sip.create_outbound_trunk(api.CreateSIPOutboundTrunkRequest(trunk=info))
    logger.info(
        "trunk sortant créé : %s → %s (%s), numéro présenté %s",
        created.sip_trunk_id, s.xivo_sip_address, s.xivo_sip_transport, s.sip_caller_number,
    )
    return str(created.sip_trunk_id)


async def delete_trunk(trunk_id: str, settings: Settings | None = None) -> None:
    """Supprime un trunk SIP (entrant ou sortant). Sans effet s'il n'existe plus."""
    s = _settings(settings)
    api = _api()
    async with _client(s) as lk:
        try:
            await lk.sip.delete_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=trunk_id))
        except Exception as e:
            if _is_not_found(e):
                logger.warning("trunk %s introuvable (déjà supprimé ?)", trunk_id)
                return
            raise
    logger.info("trunk %s supprimé", trunk_id)


# ---------------------------------------------------------------------------
# Appels : room + dispatch explicite de l'agent
# ---------------------------------------------------------------------------


def build_dispatch_requests(meta: CallMetadata, settings: Settings, api: Any | None = None) -> tuple[Any, Any]:
    """Construit (CreateRoomRequest, CreateAgentDispatchRequest) pour un appel (sans réseau)."""
    api = api or _api()
    room = room_name_for(meta.call_id)
    room_req = api.CreateRoomRequest(
        name=room,
        empty_timeout=ROOM_EMPTY_TIMEOUT_S,
        departure_timeout=ROOM_DEPARTURE_TIMEOUT_S,
        max_participants=ROOM_MAX_PARTICIPANTS,
        metadata=json.dumps({"call_id": meta.call_id, "campaign_id": meta.campaign_id}),
    )
    dispatch_req = api.CreateAgentDispatchRequest(
        agent_name=settings.agent_name,
        room=room,
        metadata=meta.model_dump_json(),
    )
    return room_req, dispatch_req


async def dispatch_call(meta: CallMetadata, settings: Settings | None = None) -> str:
    """Lance un appel : crée la room `avp-<call_id>` puis la dispatch de l'agent.

    Le worker reçoit le job avec `meta` en JSON (`ctx.job.metadata`) et compose lui-même
    le numéro (CreateSIPParticipant sur le trunk sortant). Retourne le nom de la room.

    En mode `dry_run`, LiveKit n'est pas contacté : l'appel est seulement journalisé.
    """
    s = _settings(settings)
    room = room_name_for(meta.call_id)
    if s.dry_run:
        logger.info(
            "[dry_run] appel non lancé : room=%s agent=%s numéro=%s campagne=%s",
            room, s.agent_name, meta.prospect.phone, meta.campaign_id,
        )
        return room

    api = _api()
    room_req, dispatch_req = build_dispatch_requests(meta, s, api)
    async with _client(s) as lk:
        await lk.room.create_room(room_req)
        try:
            dispatch = await lk.agent_dispatch.create_dispatch(dispatch_req)
        except Exception:
            # Pas de room orpheline si la dispatch échoue (agent_name inconnu, serveur indisponible…).
            logger.exception("échec de la dispatch pour %s : suppression de la room", room)
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room))
            except Exception:  # nettoyage au mieux
                logger.warning("suppression de la room %s impossible après échec de dispatch", room)
            raise
    logger.info(
        "appel lancé : room=%s dispatch=%s numéro=%s",
        room, getattr(dispatch, "id", "?"), meta.prospect.phone,
    )
    return room


async def hangup_room(room_name: str, settings: Settings | None = None) -> None:
    """Raccroche un appel en supprimant sa room (LiveKit SIP envoie alors le BYE)."""
    s = _settings(settings)
    if s.dry_run:
        logger.info("[dry_run] suppression de la room %s ignorée", room_name)
        return
    api = _api()
    async with _client(s) as lk:
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception as e:
            if _is_not_found(e):
                logger.info("room %s déjà fermée", room_name)
                return
            raise
    logger.info("room %s supprimée", room_name)


async def list_active_rooms(settings: Settings | None = None) -> list[str]:
    """Noms des rooms actives créées par avp (`avp-*`). Liste vide en mode `dry_run`."""
    s = _settings(settings)
    if s.dry_run:
        return []
    api = _api()
    async with _client(s) as lk:
        resp = await lk.room.list_rooms(api.ListRoomsRequest())
    return sorted(r.name for r in resp.rooms if r.name.startswith(ROOM_PREFIX))
