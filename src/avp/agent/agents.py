"""Les deux agents de l'appel et leurs outils.

- ``AgentAccueil``  : franchit le standard, obtient le décideur (ou un rappel), puis passe la main.
- ``AgentDecideur`` : qualifie, traite les objections, prend RDV / transfère / note un rappel.

Les docstrings des outils sont lues par Claude pour décider quand les appeler : elles font
partie du « prompt ». Les modifier change le comportement de l'agent.

Convention : un outil qui termine l'appel ne renvoie rien (aucune nouvelle réponse vocale),
l'au revoir ayant été dit par l'agent juste avant l'appel de l'outil (voir prompts/base.md).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from livekit import api
from livekit.agents import Agent, RunContext, StopResponse, ToolError, function_tool, get_job_context

from ..livekit_admin import participant_identity_for
from ..models import Callback, CallOutcome, Rdv
from ..prompts import build_instructions, format_slot_fr
from .logic import (
    END_OUTCOMES_ACCUEIL,
    END_OUTCOMES_DECIDEUR,
    CallUserData,
    coerce_outcome,
    looks_like_email,
    looks_like_voicemail,
    match_slot,
    parse_tool_datetime,
    question_ids,
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


async def _ensure_reply(session, *, delay_s: float = 1.2) -> None:
    """Filet de sécurité des outils « silencieux » (qui ne relancent pas Claude).

    Si Claude a appelé l'outil sans rien dire, l'agent resterait muet : après un court délai,
    si l'agent écoute et que le dernier message de l'historique est encore celui du prospect,
    on demande une réponse.
    """
    await asyncio.sleep(delay_s)
    try:
        if session.agent_state != "listening" or session.user_state == "speaking":
            return
        msgs = [i for i in session.history.items if getattr(i, "type", "") == "message"]
        if msgs and msgs[-1].role == "user":
            session.generate_reply()
    except Exception:
        logger.debug("vérification de réponse impossible", exc_info=True)


async def _hang_up_after_playout(context: RunContext[CallUserData], reason: str) -> None:
    try:
        await context.wait_for_playout()
    except Exception:  # pragma: no cover - interruption pendant l'au revoir
        pass
    await hang_up(context.userdata, reason, wait_s=0.8)


def _instructions(ud: CallUserData, role: str) -> str:
    return build_instructions(
        role,  # type: ignore[arg-type]
        ud.campaign,
        ud.prospect,
        now=datetime.now(UTC),
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

    # -- Outils communs ------------------------------------------------------

    @function_tool
    async def noter_rappel(
        self, context: RunContext[CallUserData], moment: str, personne: str = "", date_iso: str = ""
    ) -> str:
        """Enregistre un rappel convenu avec l'interlocuteur (la personne recherchée est absente,
        occupée, ou préfère être rappelée), ou une demande d'envoi d'information.

        Args:
            moment: Le moment convenu, en clair, tel que dit par l'interlocuteur (ex. « jeudi après-midi », « après le 15 »). Peut aussi contenir une ligne directe ou un email donné.
            personne: Le nom et la fonction de la personne à demander au rappel, si connus.
            date_iso: Si un jour et une heure précis ont été convenus, la date au format ISO 8601 avec fuseau (ex. 2026-10-08T14:30:00+02:00). Sinon laisser vide.
        """
        ud = context.userdata
        when = parse_tool_datetime(date_iso, ud.tz)
        if when is not None and when <= datetime.now(UTC):
            when = None
        ud.state.callback = Callback(when=when, ask_for=personne.strip(), notes=moment.strip())
        if personne and not ud.state.contact_name:
            ud.state.contact_name = personne.strip()
        logger.info("rappel noté : %s (%s)", moment, when)
        return "Rappel noté. Remerciez, dites au revoir, puis appelez terminer_appel avec l'issue rappel."

    @function_tool
    async def enregistrer_opposition(self, context: RunContext[CallUserData], motif: str = "") -> None:
        """À appeler IMMÉDIATEMENT si l'interlocuteur demande de ne plus être appelé, de retirer son
        numéro, ou se plaint du démarchage. Le numéro est ajouté à la liste d'opposition et l'appel
        se termine. Avant d'appeler cet outil, dites dans la même réponse que c'est noté, que vous
        ne rappellerez plus, excusez-vous du dérangement et dites au revoir.

        Args:
            motif: Ce que la personne a dit, résumé en quelques mots.
        """
        ud = context.userdata
        ud.state.optout = True
        ud.state.optout_reason = motif.strip() or "demande orale pendant l'appel"
        ud.state.outcome = CallOutcome.OPPOSITION
        logger.info("opposition enregistrée : %s", motif)
        _spawn(_hang_up_after_playout(context, "opposition"))
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
        if not ud.state.decision_maker_reached and looks_like_voicemail(text) and not ud.hangup_requested:
            logger.info("messagerie détectée dans la transcription : %s", text[:120])
            ud.state.outcome = CallOutcome.REPONDEUR
            ud.state.amd_result = ud.state.amd_result or "machine-vm"
            ud.note("messagerie détectée en cours d'appel (annonce reconnue)")
            _spawn(hang_up(ud, "messagerie détectée", wait_s=0.3))
            raise StopResponse()

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
        """Termine l'appel. Dites TOUJOURS au revoir dans la même réponse, avant d'appeler cet outil.

        Args:
            issue: L'une de : rappel (rappel convenu), barrage (refus de passer la personne, sans rappel), refus (le décideur refuse dès l'accueil), mauvais_numero (mauvais numéro ou établissement inexistant), non_qualifie (établissement hors cible).
            resume: Une phrase qui résume l'échange.
        """
        ud = context.userdata
        outcome = coerce_outcome(issue, END_OUTCOMES_ACCUEIL)
        if outcome is None:
            raise ToolError(
                "Issue invalide. Choisissez parmi : " + ", ".join(o.value for o in END_OUTCOMES_ACCUEIL)
            )
        if outcome is CallOutcome.RAPPEL and ud.state.callback is None:
            raise ToolError("Pour l'issue rappel, appelez d'abord noter_rappel.")
        ud.state.outcome = outcome
        if resume:
            ud.note(resume.strip())
        _spawn(_hang_up_after_playout(context, f"fin : {outcome.value}"))
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
    async def enregistrer_reponse(self, context: RunContext[CallUserData], question_id: str, reponse: str) -> None:
        """Note une information de qualification donnée par l'interlocuteur. Outil silencieux :
        appelez-le dans la même réponse que votre phrase, jamais seul. Ne vous arrêtez pas pour lui.

        Args:
            question_id: L'identifiant de la question, tel qu'indiqué entre crochets dans la campagne.
            reponse: La réponse de l'interlocuteur, résumée fidèlement, sans rien inventer.
        """
        ud = context.userdata
        qid = question_id.strip().strip("[]")
        valid = question_ids(ud.campaign)
        if qid not in valid:
            raise ToolError(f"Identifiant inconnu. Identifiants valides : {', '.join(sorted(valid))}")
        ud.state.answers[qid] = reponse.strip()
        # Rien en retour : pas de second aller-retour vers Claude, donc pas de latence ajoutée.
        _spawn(_ensure_reply(context.session))
        return None

    @function_tool
    async def proposer_creneaux(self, context: RunContext[CallUserData]) -> str:
        """Donne les créneaux de rendez-vous disponibles avec Jean-Baptiste. À appeler avant de proposer
        des créneaux à voix haute. Proposez-en deux ou trois, pas plus."""
        ud = context.userdata
        slots = sorted(ud.meta.rdv_slots)
        if not slots:
            return "Aucun créneau disponible. Proposez que Jean-Baptiste rappelle et utilisez noter_rappel."
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
                "Récapitulez brièvement, remerciez, dites au revoir, puis appelez terminer_appel avec l'issue rdv."
            )
        return (
            f"Rendez-vous enregistré pour le {format_slot_fr(slot, ud.settings.timezone)}. "
            "Dites qu'une invitation va arriver par email. "
            "Récapitulez brièvement, remerciez, dites au revoir, puis appelez terminer_appel avec l'issue rdv."
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
                "ou un rappel avec noter_rappel."
            )
        ud.state.transferred = True
        ud.state.outcome = CallOutcome.TRANSFERT
        ud.note(f"transféré : {motif}".strip())
        _spawn(hang_up(ud, "transfert", wait_s=1.0))
        return None

    @function_tool
    async def terminer_appel(self, context: RunContext[CallUserData], issue: str, resume: str = "") -> None:
        """Termine l'appel. Dites TOUJOURS au revoir dans la même réponse, avant d'appeler cet outil.

        Args:
            issue: L'une de : rdv (RDV confirmé avec confirmer_rdv), rappel (rappel noté avec noter_rappel), qualifie (intérêt sans RDV), non_qualifie (hors cible, pas de besoin), refus (pas intéressé).
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
        if outcome is CallOutcome.RAPPEL and ud.state.callback is None:
            raise ToolError("Pour l'issue rappel, appelez d'abord noter_rappel.")
        ud.state.outcome = outcome
        if resume:
            ud.note(resume.strip())
        _spawn(_hang_up_after_playout(context, f"fin : {outcome.value}"))
        return None


class AgentRepondeur(Agent):
    """Agent minimal pour laisser un message sur un répondeur (aucun outil)."""

    def __init__(self, ud: CallUserData) -> None:
        super().__init__(instructions=_instructions(ud, "repondeur"))
