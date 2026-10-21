"""L'agent de l'appel et ses outils.

- ``AgentAppel``     : franchit le standard, puis échange avec le décideur, qualifie, traite les
  objections, propose un RDV (créneaux dans ses consignes) ou un rappel, transfère si configuré.
- ``AgentRepondeur`` : laisse un message sur un répondeur (sans outil).

Les docstrings des outils sont lues par Claude pour décider quand les appeler : elles font
partie du « prompt ». Les modifier change le comportement de l'agent.

Pas d'outil de prise de notes : contacts, rappel, réponses et rendez-vous sont extraits de la
transcription après l'appel (postcall.apply_analysis). Avec ces outils, Claude oubliait de les appeler ou
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
from ..models import CallOutcome
from ..prompts import CLOSING_LINE, build_instructions, decider_hook_line
from .logic import (
    COMPLAINT_HINT,
    END_OUTCOMES,
    INTERJECTION_BUFFER_CHARS,
    NEW_PERSON_HINT,
    OPTOUT_LINE,
    SPELLING_DELIMITERS,
    CallUserData,
    coerce_outcome,
    is_complaint,
    is_goodbye,
    is_hold_line,
    is_optout_request,
    is_self_identification,
    looks_like_voicemail,
    pick_acknowledgement,
    pronounce_fr,
    spell_letters_fr,
    strip_leading_interjection,
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
        text = getattr(new_message, "text_content", "") or ""
        if is_optout_request(text) and not (ud.hangup_requested or ud.closing):
            # « Ne m'appelez plus » : obligation légale, traitée sans Claude.
            ud.state.optout, ud.state.outcome = True, CallOutcome.OPPOSITION
            ud.state.optout_reason = text.strip()[:200]
            logger.info("opposition demandée : %s", text[:120])
            handle = self.session.say(OPTOUT_LINE, allow_interruptions=False)
            _spawn(self._finish_after(handle, "opposition"))
            raise StopResponse()
        after_hold = is_hold_line(_last_agent_text(self.session))
        if after_hold:
            # Après un transfert : nouvelle personne, présentation IA obligatoire (consigne ciblée).
            turn_ctx.add_message(role="system", content=NEW_PERSON_HINT)
        if is_complaint(text):
            # Plainte contre le démarchage : consigne ciblée pour cette réplique, pas de petit mot.
            turn_ctx.add_message(role="system", content=COMPLAINT_HINT)
            return
        if (is_self_identification(text) and not after_hold and not ud.state.decision_maker_reached
                and ud.user_turns <= 2):
            # Le décideur a décroché : réplique fixe immédiate, Claude prend la suite à la réponse.
            ud.state.decision_maker_reached = True
            self.session.say(decider_hook_line(ud.campaign), allow_interruptions=True)
            logger.info("décideur en ligne dès le décroché : réplique fixe")
            raise StopResponse()
        if not ud.settings.acquiescements or ud.hangup_requested or ud.closing:
            return
        ack = pick_acknowledgement(text, ud.last_ack)
        if ack:
            ud.last_ack = ack
            ud.strip_interjection = True
            self.session.say(ack, allow_interruptions=True, add_to_chat_ctx=False)

    async def _finish_after(self, handle, reason: str) -> None:
        try:
            await handle.wait_for_playout()
        except Exception:  # pragma: no cover - session fermée
            pass
        await finish_call(self.session, self._ud, reason)

    def tts_node(self, text, model_settings):
        """Avant la synthèse vocale :
        - si un acquiescement vient d'être dit, retire les interjections en tête de la réponse de
          Claude (« D'accord, donc… »), pour ne pas en entendre deux de suite ;
        - écrit en français le nom des lettres épelées (sinon « I » est lu à l'anglaise) ;
        - écrit phonétiquement les sigles qui se disent comme un mot (« EHPAD » → « épade »).
        """
        return Agent.default.tts_node(self, self._epure(text), model_settings)

    async def _epure(self, text):
        buf, head_done, at_boundary = "", False, True
        async for chunk in text:
            buf += chunk
            if not head_done:
                if len(buf) < INTERJECTION_BUFFER_CHARS:
                    continue
                buf, head_done = self._epure_head(buf), True
            # On ne retient que la fin non terminée (après la dernière virgule ou ponctuation) :
            # une lettre épelée se reconnaît à ses deux délimiteurs.
            cut = max(buf.rfind(c) for c in SPELLING_DELIMITERS) + 1
            if cut:
                piece, buf = buf[:cut], buf[cut:]
                yield pronounce_fr(spell_letters_fr(piece, at_boundary=at_boundary))
                at_boundary = piece.rstrip()[-1:] in (",", ":")
        if buf:
            if not head_done:
                buf = self._epure_head(buf)
            yield pronounce_fr(spell_letters_fr(buf, at_boundary=at_boundary))

    def _epure_head(self, head: str) -> str:
        ud = self._ud
        if head.strip() == ud.last_ack or not ud.strip_interjection:
            return head  # l'acquiescement lui-même, ou réponse sans acquiescement avant
        ud.strip_interjection = False
        return strip_leading_interjection(head)

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


class AgentAppel(_BaseProspectAgent):
    """L'agent unique de l'appel : franchit le standard, puis échange avec le décideur et propose un
    rendez-vous. Toutes les consignes (standard, décideur, créneaux) sont présentes dès le départ.

    Il y avait deux agents (accueil → décideur, avec l'outil passer_au_decideur) : Claude oubliait
    la bascule et restait « accueil », sans les créneaux (test du 19/10). Un seul agent supprime
    cette étape.
    """

    role = "appel"

    def __init__(self, ud: CallUserData, **kwargs) -> None:
        super().__init__(ud, **kwargs)
        if not ud.settings.transfer_target:
            # Transfert désactivé (TRANSFER_TARGET vide) : l'outil n'est même pas proposé à Claude.
            self._tools = [t for t in self._tools if getattr(t, "id", "") != "transferer_a_un_humain"]

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
            not ud.hangup_requested
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
            return "Le transfert a échoué. Excusez-vous, puis proposez un rendez-vous ou un rappel."
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
            issue: L'une de : rdv (le décideur a accepté un créneau et confirmé son email), rappel (la personne est à rappeler, ou un rappel est convenu), qualifie (le décideur montre de l'intérêt, sans rendez-vous), barrage (le standard refuse de passer la personne, sans rappel), refus (le décideur n'est pas intéressé), mauvais_numero (mauvais numéro ou établissement inexistant), non_qualifie (établissement hors cible ou aucun besoin).
            resume: Une phrase qui résume l'échange.
        """
        ud = context.userdata
        outcome = coerce_outcome(issue, END_OUTCOMES)
        if outcome is None:
            raise ToolError("Issue invalide. Choisissez parmi : " + ", ".join(o.value for o in END_OUTCOMES))
        ud.state.outcome = outcome
        if resume:
            ud.note(resume.strip())
        _spawn(_finish_after_playout(context, f"fin : {outcome.value}"))
        return None


class AgentRepondeur(Agent):
    """Agent minimal pour laisser un message sur un répondeur (aucun outil)."""

    def __init__(self, ud: CallUserData) -> None:
        super().__init__(instructions=_instructions(ud, "repondeur"))
