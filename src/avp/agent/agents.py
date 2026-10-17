"""Les deux agents de l'appel et leurs outils.

- ``AgentAccueil``  : franchit le standard, obtient le décideur (ou un rappel), puis passe la main.
- ``AgentDecideur`` : qualifie, traite les objections, prend RDV / transfère.

Les docstrings des outils sont lues par Claude pour décider quand les appeler : elles font
partie du « prompt ». Les modifier change le comportement de l'agent.

Pas d'outil de prise de notes : contacts, rappel et réponses sont extraits de la transcription
après l'appel (postcall.apply_analysis). Avec ces outils, Claude oubliait de les appeler ou
retardait sa réplique (tests des 16 et 17/10).

Fin d'appel : un outil qui termine l'appel ne renvoie rien ; ``finish_call`` dit la formule
d'au revoir FIXE (``CLOSING_LINE``) si l'agent ne l'a pas déjà dite, puis raccroche.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from livekit import api
from livekit.agents import Agent, RunContext, StopResponse, ToolError, function_tool, get_job_context

from ..livekit_admin import participant_identity_for
from ..models import CallOutcome, Rdv
from ..prompts import CLOSING_LINE, build_instructions, format_slot_fr
from .logic import (
    END_OUTCOMES_ACCUEIL,
    END_OUTCOMES_DECIDEUR,
    CallUserData,
    coerce_outcome,
    is_goodbye,
    looks_like_email,
    looks_like_voicemail,
    match_slot,
    pick_acknowledgement,
    voicemail_window_open,
)

logger = logging.getLogger("avp.agent")

_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def hang_up(ud: CallUserData, reason: str, *, wait_s: float = 0.0) -> None:
    """Raccroche proprement : laisse finir la phrase en cours, puis arrête le job.

    L'arrêt du job déclenche les callbacks de fin (persistance) et la suppression de la room,
    ce qui raccroche la jambe SIP.
    """
    if ud.hangup_requested:
        return
    ud.hangup_requested = True
    if not ud.state.end_reason:
        ud.state.end_reason = reason
    if wait_s:
        await asyncio.sleep(wait_s)
    job = get_job_context(required=False)
    if job is not None:
        job.shutdown(reason=reason)


def _last_agent_text(session) -> str:
    try:
        msgs = [i for i in session.history.items if getattr(i, "type", "") == "message"]
    except Exception:  # pragma: no cover
        return ""
    for m in reversed(msgs):
        if m.role == "assistant":
            return m.text_content or ""
    return ""


async def finish_call(session, ud: CallUserData, reason: str) -> None:
    """Fin d'appel déterministe : formule d'au revoir fixe (si l'agent ne l'a pas dite), puis raccrochage.

    Appelée par terminer_appel / enregistrer_opposition, et par les filets de sécurité du worker
    (au revoir de l'agent, ou remerciement final du prospect suivi d'un silence).
    """
    if ud.hangup_requested or ud.closing:
        return
    ud.closing = True
    if not is_goodbye(_last_agent_text(session)):
        try:
            await session.say(CLOSING_LINE, allow_interruptions=False).wait_for_playout()
        except Exception:  # pragma: no cover - session déjà fermée (le prospect a raccroché)
            logger.debug("au revoir non dit", exc_info=True)
    await hang_up(ud, reason, wait_s=0.8)


async def _finish_after_playout(context: RunContext[CallUserData], reason: str) -> None:
    try:
        await context.wait_for_playout()
    except Exception:  # pragma: no cover - interruption pendant la dernière phrase
        pass
    await finish_call(context.session, context.userdata, reason)


def _instructions(ud: CallUserData, role: str) -> str:
    return build_instructions(
        role,  # type: ignore[arg-type]
        ud.campaign,
        ud.prospect,
        # Heure du début de l'appel, pas l'heure courante : les consignes restent identiques d'un
        # agent à l'autre et au préchauffage, donc le cache de prompt d'Anthropic sert (1er mot plus tôt).
        now=ud.started_at,
        attempt=ud.meta.attempt,
        rdv_slots=ud.meta.rdv_slots,
        prompts_dir=ud.settings.prompts_dir,
        timezone=ud.settings.timezone,
        transfer_available=bool(ud.settings.transfer_target),
    )


class _BaseProspectAgent(Agent):
    """Outils communs aux deux rôles."""

    role: str = ""

    def __init__(self, ud: CallUserData, **kwargs) -> None:
        super().__init__(instructions=_instructions(ud, self.role), **kwargs)
        self._ud = ud

    async def on_enter(self) -> None:
        if self.role not in self._ud.state.agent_path:
            self._ud.state.agent_path.append(self.role)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Acquiescement immédiat (« D'accord. ») pendant que Claude prépare sa réponse.

        Dit avant la réponse (file de parole de la session), hors historique : Claude ne le voit pas
        et prompts/base.md lui demande de ne pas commencer lui-même par un acquiescement.
        """
        ud = self._ud
        if not ud.settings.acquiescements or ud.hangup_requested or ud.closing:
            return
        ack = pick_acknowledgement(getattr(new_message, "text_content", "") or "", ud.last_ack)
        if ack:
            ud.last_ack = ack
            self.session.say(ack, allow_interruptions=True, add_to_chat_ctx=False)

    # -- Outils communs ------------------------------------------------------

    @function_tool
    async def enregistrer_opposition(self, context: RunContext[CallUserData], motif: str = "") -> None:
        """À appeler IMMÉDIATEMENT si l'interlocuteur demande de ne plus être appelé, de retirer son
        numéro, ou se plaint du démarchage. Le numéro est ajouté à la liste d'opposition et l'appel
        se termine. Avant d'appeler cet outil, dites dans la même réponse que c'est noté, que vous
        ne rappellerez plus, et excusez-vous du dérangement. L'au revoir est dit automatiquement.

        Args:
            motif: Ce que la personne a dit, résumé en quelques mots.
        """
        ud = context.userdata
        ud.state.optout = True
        ud.state.optout_reason = motif.strip() or "demande orale pendant l'appel"
        ud.state.outcome = CallOutcome.OPPOSITION
        logger.info("opposition enregistrée : %s", motif)
        _spawn(_finish_after_playout(context, "opposition"))
        return None


class AgentAccueil(_BaseProspectAgent):
    role = "accueil"

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        ud = self._ud
        text = getattr(new_message, "text_content", "") or ""
        # Réponse au « Allô ? » de vérification : on la note, sans réponse du LLM
        # (le worker décide ensuite : phrase d'ouverture, ou messagerie).
        ev = ud.presence_event
        if ev is not None and not ev.is_set():
            ud.presence_text = text
            ev.set()
            raise StopResponse()
        # Filet de sécurité : messagerie ou serveur vocal non détecté par l'AMD.
        ud.user_turns += 1
        if (
            not ud.state.decision_maker_reached
            and not ud.hangup_requested
            and voicemail_window_open(ud.answered_at, datetime.now(UTC), ud.user_turns)
            and looks_like_voicemail(text)
        ):
            logger.info("messagerie détectée dans la transcription : %s", text[:120])
            ud.state.outcome = CallOutcome.REPONDEUR
            ud.state.amd_result = ud.state.amd_result or "machine-vm"
            ud.note("messagerie détectée en cours d'appel (annonce reconnue)")
            _spawn(hang_up(ud, "messagerie détectée", wait_s=0.3))
            raise StopResponse()
        # « Allô ? » du prospect pendant que l'AMD décide : la phrase d'ouverture fixe va y répondre.
        # Sans cela, Claude répondait avant elle (test du 17/10, 18 h 17).
        if not ud.opening_done:
            raise StopResponse()
        await super().on_user_turn_completed(turn_ctx, new_message)

    @function_tool
    async def passer_au_decideur(
        self, context: RunContext[CallUserData], nom: str = "", fonction: str = ""
    ) -> Agent:
        """À appeler dès que l'interlocuteur cible (ou une alternative acceptée par la campagne) est
        en ligne et accepte d'échanger, y compris si c'est la personne qui a décroché. Ne dites
        rien avant : l'agent décideur prend la suite et se présente.

        Args:
            nom: Nom de la personne (avec Madame ou Monsieur si connu).
            fonction: Sa fonction (directeur, IDEC, cadre de santé…).
        """
        ud = context.userdata
        ud.state.decision_maker_reached = True
        if nom:
            ud.state.contact_name = nom.strip()
        if fonction:
            ud.state.contact_role = fonction.strip()
        logger.info("passage au décideur : %s (%s)", nom, fonction)
        return AgentDecideur(ud, chat_ctx=self.chat_ctx.copy(exclude_instructions=True))

    @function_tool
    async def terminer_appel(self, context: RunContext[CallUserData], issue: str, resume: str = "") -> None:
        """Termine l'appel. Dans la même réponse, avant l'outil, remerciez en une phrase. Ne dites pas
        au revoir : la formule d'au revoir est dite automatiquement, puis l'appel raccroche.

        Args:
            issue: L'une de : rappel (la personne est à rappeler, ou un rappel est convenu), barrage (refus de passer la personne, sans rappel), refus (le décideur refuse dès l'accueil), mauvais_numero (mauvais numéro ou établissement inexistant), non_qualifie (établissement hors cible).
            resume: Une phrase qui résume l'échange.
        """
        ud = context.userdata
        outcome = coerce_outcome(issue, END_OUTCOMES_ACCUEIL)
        if outcome is None:
            raise ToolError(
                "Issue invalide. Choisissez parmi : " + ", ".join(o.value for o in END_OUTCOMES_ACCUEIL)
            )
        ud.state.outcome = outcome
        if resume:
            ud.note(resume.strip())
        _spawn(_finish_after_playout(context, f"fin : {outcome.value}"))
        return None


class AgentDecideur(_BaseProspectAgent):
    role = "decideur"

    def __init__(self, ud: CallUserData, **kwargs) -> None:
        super().__init__(ud, **kwargs)
        if not ud.settings.transfer_target:
            # Transfert désactivé (TRANSFER_TARGET vide) : l'outil n'est même pas proposé à Claude.
            self._tools = [t for t in self._tools if getattr(t, "id", "") != "transferer_a_un_humain"]

    async def on_enter(self) -> None:
        await super().on_enter()
        # Le décideur prend la parole tout de suite (présentation + accroche, cf. prompts/decideur.md).
        self.session.generate_reply(
            instructions="Présentez-vous brièvement comme l'assistante commerciale virtuelle "
            "d'OpteoLink, de la part de Jean-Baptiste Marin, puis dites l'accroche et demandez deux minutes."
        )

    @function_tool
    async def proposer_creneaux(self, context: RunContext[CallUserData]) -> str:
        """Donne les créneaux de rendez-vous disponibles avec Jean-Baptiste. À appeler avant de proposer
        des créneaux à voix haute. Proposez-en deux ou trois, pas plus."""
        ud = context.userdata
        slots = sorted(ud.meta.rdv_slots)
        if not slots:
            return "Aucun créneau disponible. Proposez que Jean-Baptiste rappelle, et demandez quand."
        ud.proposed_slots = slots
        tz_name = ud.settings.timezone

        def fmt(x) -> str:
            return f"{format_slot_fr(x, tz_name)} (iso {x.astimezone(ud.tz).isoformat()})"

        direct = [fmt(x) for x in slots if not ud.campaign.rdv.needs_confirmation(x, tz_name)]
        later = [fmt(x) for x in slots if ud.campaign.rdv.needs_confirmation(x, tz_name)]
        out = "Créneaux confirmés immédiatement : " + (" ; ".join(direct) or "aucun") + "."
        if later:
            out += (
                " Seulement si aucun ne convient, créneaux sous réserve (Jean-Baptiste confirmera par email) : "
                + " ; ".join(later) + "."
            )
        return out

    @function_tool
    async def confirmer_rdv(
        self, context: RunContext[CallUserData], date_iso: str, email: str, nom: str = ""
    ) -> str:
        """Confirme le rendez-vous choisi par l'interlocuteur, après avoir vérifié son email.

        Args:
            date_iso: La valeur iso EXACTE du créneau choisi, recopiée depuis proposer_creneaux.
            email: L'adresse email de la personne pour l'invitation, vérifiée avec elle.
            nom: Le nom de la personne qui sera présente au rendez-vous.
        """
        ud = context.userdata
        slots = ud.proposed_slots or list(ud.meta.rdv_slots)
        slot = match_slot(date_iso, slots, ud.tz)
        if slot is None:
            raise ToolError("Ce créneau ne fait pas partie des créneaux proposés. Appelez proposer_creneaux.")
        if not looks_like_email(email):
            raise ToolError("Email invalide. Faites-le épeler et répétez-le pour vérifier.")
        to_confirm = ud.campaign.rdv.needs_confirmation(slot, ud.settings.timezone)
        ud.state.rdv = Rdv(
            start=slot,
            duree_min=ud.campaign.rdv.duree_min,
            mode=ud.campaign.rdv.mode,
            avec=(nom or ud.state.contact_name).strip(),
            email=email.strip().lower(),
            a_confirmer=to_confirm,
        )
        ud.state.contact_email = email.strip().lower()
        if nom and not ud.state.contact_name:
            ud.state.contact_name = nom.strip()
        logger.info("RDV confirmé : %s (%s)", slot.isoformat(), email)
        if to_confirm:
            return (
                f"Rendez-vous noté sous réserve pour le {format_slot_fr(slot, ud.settings.timezone)}. "
                "Dites que Jean-Baptiste le confirmera par email dans la journée, à l'adresse donnée. "
                "Récapitulez brièvement, remerciez, puis appelez terminer_appel avec l'issue rdv."
            )
        return (
            f"Rendez-vous enregistré pour le {format_slot_fr(slot, ud.settings.timezone)}. "
            "Dites qu'une invitation va arriver par email. "
            "Récapitulez brièvement, remerciez, puis appelez terminer_appel avec l'issue rdv."
        )

    @function_tool
    async def transferer_a_un_humain(self, context: RunContext[CallUserData], motif: str = "") -> str | None:
        """Transfère l'appel à Jean-Baptiste, uniquement si l'interlocuteur l'a accepté. Avant d'appeler
        cet outil, annoncez dans la même réponse : « Je vous mets en relation, ne quittez pas. »

        Args:
            motif: Pourquoi le transfert (demande de la personne, fort intérêt…).
        """
        ud = context.userdata
        target = ud.settings.transfer_target
        if not target:
            raise ToolError("Le transfert n'est pas configuré. Proposez un rendez-vous ou un rappel.")
        if ud.console:
            ud.state.transferred = True
            return "Mode console : transfert simulé. Dites au revoir."
        try:
            await context.wait_for_playout()
        except Exception:  # pragma: no cover
            pass
        job = get_job_context()
        try:
            await job.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=job.room.name,
                    participant_identity=participant_identity_for(ud.meta.call_id),
                    transfer_to=target,
                    play_dialtone=False,
                )
            )
        except Exception as e:  # SipCallError ou autre
            logger.warning("transfert échoué : %s", e)
            ud.note(f"transfert échoué : {e}")
            return (
                "Le transfert a échoué. Excusez-vous, puis proposez un rendez-vous avec proposer_creneaux "
                "ou un rappel."
            )
        ud.state.transferred = True
        ud.state.outcome = CallOutcome.TRANSFERT
        ud.note(f"transféré : {motif}".strip())
        _spawn(hang_up(ud, "transfert", wait_s=1.0))
        return None

    @function_tool
    async def terminer_appel(self, context: RunContext[CallUserData], issue: str, resume: str = "") -> None:
        """Termine l'appel. Dans la même réponse, avant l'outil, remerciez en une phrase. Ne dites pas
        au revoir : la formule d'au revoir est dite automatiquement, puis l'appel raccroche.

        Args:
            issue: L'une de : rdv (RDV confirmé avec confirmer_rdv), rappel (rappel convenu), qualifie (intérêt sans RDV), non_qualifie (hors cible, pas de besoin), refus (pas intéressé).
            resume: Une phrase qui résume l'échange.
        """
        ud = context.userdata
        outcome = coerce_outcome(issue, END_OUTCOMES_DECIDEUR)
        if outcome is None:
            raise ToolError(
                "Issue invalide. Choisissez parmi : " + ", ".join(o.value for o in END_OUTCOMES_DECIDEUR)
            )
        if outcome is CallOutcome.RDV and ud.state.rdv is None:
            raise ToolError("Pour l'issue rdv, appelez d'abord confirmer_rdv.")
        ud.state.outcome = outcome
        if resume:
            ud.note(resume.strip())
        _spawn(_finish_after_playout(context, f"fin : {outcome.value}"))
        return None


class AgentRepondeur(Agent):
    """Agent minimal pour laisser un message sur un répondeur (aucun outil)."""

    def __init__(self, ud: CallUserData) -> None:
        super().__init__(instructions=_instructions(ud, "repondeur"))
