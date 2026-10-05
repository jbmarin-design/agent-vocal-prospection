"""Point d'entrée du worker vocal LiveKit.

Usage :
    python -m avp.agent.worker start           # production (conteneur agent-worker)
    python -m avp.agent.worker dev             # développement, rechargement auto
    python -m avp.agent.worker console         # test au micro, sans téléphone (AVP_CONSOLE_CAMPAIGN=ehpad)
    python -m avp.agent.worker download-files  # télécharge les modèles VAD / fin de tour

Le worker ne reçoit que des dispatches explicites (agent_name) créées par l'orchestrateur
(`avp call test`, `avp campaign run`, `avp run`), avec un `CallMetadata` en JSON.

Déroulé d'un appel :
  1. lecture des métadonnées, chargement de la campagne, construction de la session ;
  2. démarrage de la session avec AgentAccueil (muet) ;
  3. AMD démarré AVANT la numérotation, puis create_sip_participant(wait_until_answered) ;
  4. humain → phrase d'ouverture fixe (annonce IA + enregistrement) ; répondeur → message ou raccrochage ;
  5. conversation (accueil → décideur), garde-fous durée et silence ;
  6. fin : transcription + état écrits dans data/transcripts, base mise à jour, room supprimée.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from dotenv import load_dotenv
from livekit import api
from livekit.agents import AMD, AgentServer, JobContext, JobProcess, cli

from .. import db
from ..campaigns import load_campaign
from ..config import get_settings
from ..livekit_admin import participant_identity_for, sip_headers_for
from ..models import CallMetadata, CallOutcome
from ..prompts import opening_line, prompt_version
from .agents import AgentAccueil, AgentRepondeur, hang_up
from .logic import (
    CallUserData,
    classify_sip_failure,
    console_metadata,
    dump_usage,
    finalize_call,
    history_to_transcript,
    outcome_from_amd,
    should_leave_voicemail,
)
from .session import build_session, load_vad

load_dotenv()
logger = logging.getLogger("avp.agent.worker")


def prewarm(proc: JobProcess) -> None:
    """Exécuté une fois par processus : charge le VAD (quelques centaines de ms) hors appel."""
    proc.userdata["vad"] = load_vad()


server = AgentServer(setup_fnc=prewarm)


def _session_transcript(session) -> list:
    items = []
    for item in session.history.items:
        if getattr(item, "type", "") != "message":
            continue
        items.append(
            {
                "role": item.role,
                "text": item.text_content or "",
                "created_at": getattr(item, "created_at", None),
            }
        )
    return history_to_transcript(items)


async def _duration_guard(ud: CallUserData, session) -> None:
    """Prévient 30 s avant la durée maximale, puis raccroche à la durée maximale."""
    limit = ud.settings.max_call_duration_s
    await asyncio.sleep(max(limit - 30, 10))
    if ud.hangup_requested:
        return
    logger.info("durée maximale bientôt atteinte : conclusion demandée")
    session.generate_reply(
        instructions="Le temps est écoulé. Concluez maintenant en une phrase : proposez que Jean-Baptiste "
        "rappelle si besoin, remerciez, dites au revoir, puis appelez terminer_appel."
    )
    await asyncio.sleep(30)
    if not ud.hangup_requested:
        ud.note("durée maximale atteinte")
        await hang_up(ud, "durée maximale")


@server.rtc_session(agent_name=get_settings().agent_name)
async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    settings.ensure_dirs()

    # 1. Métadonnées ------------------------------------------------------------
    raw = (ctx.job.metadata or "").strip()
    console = not raw
    if console:
        meta = console_metadata(os.getenv("AVP_CONSOLE_CAMPAIGN", "ehpad"))
    else:
        meta = CallMetadata.model_validate_json(raw)
    ctx.log_context_fields = {"call_id": meta.call_id, "campagne": meta.campaign_id}

    campaign = load_campaign(meta.campaign_id, settings.campaigns_dir)
    ud = CallUserData(meta=meta, campaign=campaign, settings=settings, console=console)
    try:
        ud.prompt_version = prompt_version(campaign, settings.prompts_dir, settings.campaigns_dir)
    except Exception:
        logger.exception("calcul de la version de prompt impossible")

    vad = ctx.proc.userdata.get("vad")
    session = build_session(ud, vad=vad)
    tasks: list[asyncio.Task] = []
    away_count = 0

    # 6. Fin d'appel (toujours exécuté, même après une exception) -----------------
    async def on_shutdown(reason: str = "") -> None:
        for t in tasks:
            t.cancel()
        ud.ended_at = ud.ended_at or datetime.now(UTC)
        try:
            ud.usage = dump_usage(getattr(session, "usage", None))
        except Exception:
            pass
        try:
            transcript = _session_transcript(session)
        except Exception:
            logger.exception("lecture de l'historique impossible")
            transcript = []
        path = await asyncio.to_thread(finalize_call, ud, transcript)
        logger.info("appel terminé : issue=%s, transcription=%s", ud.state.outcome, path)
        if not console:
            try:
                await ctx.delete_room()
            except Exception:
                pass

    ctx.add_shutdown_callback(on_shutdown)

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        nonlocal away_count
        if ev.new_state != "away" or ud.answered_at is None or ud.hangup_requested:
            return
        away_count += 1
        if away_count == 1:
            session.generate_reply(instructions="L'interlocuteur ne répond plus. Demandez poliment s'il est toujours là.")
        else:
            ud.note("silence prolongé")
            session.say("Je n'ai plus de réponse, je vais raccrocher. Bonne journée.", allow_interruptions=False)
            tasks.append(asyncio.create_task(hang_up(ud, "silence prolongé", wait_s=4.0)))

    @session.on("close")
    def _on_close(ev) -> None:
        # Le prospect a raccroché (ou erreur fatale STT/LLM/TTS) : on termine le job.
        if getattr(ev, "error", None) is not None:
            ud.note(f"erreur de session : {ev.error}")
        ctx.shutdown("session fermée")

    # 2. Démarrage de la session -------------------------------------------------
    await session.start(agent=AgentAccueil(ud), room=ctx.room)

    if console:
        ud.answered_at = datetime.now(UTC)
        ud.state.amd_result = "human"
        session.say(opening_line(campaign, meta.prospect), allow_interruptions=False)
        tasks.append(asyncio.create_task(_duration_guard(ud, session)))
        return

    if settings.dry_run:
        logger.warning("DRY_RUN : aucune numérotation")
        ud.state.outcome = CallOutcome.ERREUR
        ud.state.end_reason = "dry_run"
        ctx.shutdown("dry_run")
        return
    if not settings.sip_outbound_trunk_id:
        logger.error("SIP_OUTBOUND_TRUNK_ID vide : lancez `avp trunk create` et renseignez .env")
        ud.state.outcome = CallOutcome.ERREUR
        ud.state.end_reason = "trunk non configuré"
        ctx.shutdown("trunk non configuré")
        return

    identity = participant_identity_for(meta.call_id)
    if session.room_io:
        session.room_io.set_participant(identity)

    await asyncio.to_thread(db.update_call, meta.call_id, db_path=settings.db_path, status="en_cours",
                            started_at=ud.started_at)

    # 3. AMD puis numérotation -----------------------------------------------------
    async with AMD(
        session,
        llm=None,  # réutilise Claude Haiku de la session (pas de service LiveKit Cloud)
        stt=None,  # réutilise la transcription Deepgram de la session
        participant_identity=identity,
        ivr_detection=False,  # un SVI = on raccroche (pas de navigation automatique)
        suppress_compatibility_warning=True,
    ) as detector:
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=settings.sip_outbound_trunk_id,
                    sip_call_to=meta.prospect.phone,
                    participant_identity=identity,
                    participant_name=meta.prospect.name[:60],
                    headers=sip_headers_for(meta.call_id),
                    wait_until_answered=True,
                ),
                timeout=settings.ring_timeout_s + 15,
            )
        except api.SipCallError as e:
            ud.state.outcome = classify_sip_failure(e.sip_status_code)
            ud.state.end_reason = f"SIP {e.sip_status_code} {e.sip_status or ''}".strip()
            logger.info("appel non abouti : %s", ud.state.end_reason)
            ctx.shutdown("non abouti")
            return
        except TimeoutError:
            ud.state.outcome = CallOutcome.NON_DECROCHE
            ud.state.end_reason = "délai de sonnerie dépassé"
            ctx.shutdown("non décroché")
            return

        if ctx.room.remote_participants.get(identity) is None:
            ud.state.outcome = CallOutcome.NON_DECROCHE
            ud.state.end_reason = "participant absent après décroché"
            ctx.shutdown("participant absent")
            return

        ud.answered_at = datetime.now(UTC)
        await asyncio.to_thread(db.update_call, meta.call_id, db_path=settings.db_path, answered_at=ud.answered_at)
        tasks.append(asyncio.create_task(_duration_guard(ud, session)))

        result = await detector.execute()

    # 4. Résultat AMD -------------------------------------------------------------
    category = str(getattr(result, "category", "uncertain"))
    ud.state.amd_result = category
    logger.info("AMD : %s", category)
    amd_outcome = outcome_from_amd(category)

    if amd_outcome is None:  # humain ou incertain
        session.say(opening_line(campaign, meta.prospect), allow_interruptions=False)
        return  # la conversation se déroule ; la fin passe par les outils ou les garde-fous

    ud.state.outcome = amd_outcome
    if amd_outcome is CallOutcome.REPONDEUR and should_leave_voicemail(campaign):
        session.update_agent(AgentRepondeur(ud))
        handle = session.generate_reply(instructions="Laissez maintenant votre message, puis arrêtez-vous.")
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=40)
        except TimeoutError:
            pass
        ud.note("message laissé sur le répondeur")
    elif amd_outcome is CallOutcome.SVI:
        ud.note("serveur vocal non franchi")
    await hang_up(ud, f"AMD : {category}", wait_s=0.5)


if __name__ == "__main__":
    cli.run_app(server)
