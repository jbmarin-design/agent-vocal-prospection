"""Fabrique de la session vocale : STT, LLM, TTS, VAD et détection de fin de tour.

Tous les choix de fournisseurs et de modèles viennent de `Settings` (.env) :
changer de voix ou de modèle ne demande aucune modification de code.
"""

from __future__ import annotations

import logging
from typing import Any

from livekit.agents import AgentSession
from livekit.plugins import anthropic, cartesia, deepgram, elevenlabs, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from ..config import Settings
from ..models import Campaign
from .logic import CallUserData

logger = logging.getLogger("avp.agent.session")


def load_vad() -> silero.VAD:
    """Chargé une fois par processus (prewarm), puis réutilisé par chaque appel."""
    # 0,35 s de silence suffit à clore un segment de parole ; le détecteur de fin de tour
    # (MultilingualModel) évite ensuite de couper la parole sur une simple pause.
    return silero.VAD.load(min_silence_duration=0.35, activation_threshold=0.5)


def build_stt(settings: Settings, campaign: Campaign) -> Any:
    if settings.stt_provider == "cartesia":
        # Transcription Cartesia (même compte et même facture que la voix). En français : ink-whisper.
        model = settings.stt_model if settings.stt_model.startswith("ink") else "ink-whisper"
        ckw: dict[str, Any] = {"model": model, "language": settings.stt_language}
        if settings.cartesia_api_key:
            ckw["api_key"] = settings.cartesia_api_key
        if campaign.mots_cles_stt:
            ckw["keyterm"] = list(campaign.mots_cles_stt)
        return cartesia.STT(**ckw)
    kwargs: dict[str, Any] = {
        "model": settings.stt_model,
        "language": settings.stt_language,
        "smart_format": True,
        "punctuate": True,
        "api_key": settings.deepgram_api_key or None,
    }
    if campaign.mots_cles_stt and settings.stt_model.startswith("nova-3"):
        kwargs["keyterm"] = list(campaign.mots_cles_stt)
    return deepgram.STT(**{k: v for k, v in kwargs.items() if v is not None})


def build_llm(settings: Settings) -> anthropic.LLM:
    return anthropic.LLM(
        model=settings.llm_realtime_model,
        api_key=settings.anthropic_api_key or None,
        temperature=0.4,
        max_tokens=200,  # réponses courtes : moins de texte = voix plus tôt
        caching="ephemeral",
    )


def build_tts(settings: Settings) -> Any:
    if settings.tts_provider == "elevenlabs":
        kwargs: dict[str, Any] = {"model": settings.elevenlabs_model, "language": "fr"}
        if settings.elevenlabs_voice_id:
            kwargs["voice_id"] = settings.elevenlabs_voice_id
        if settings.elevenlabs_api_key:
            kwargs["api_key"] = settings.elevenlabs_api_key
        return elevenlabs.TTS(**kwargs)
    kwargs = {"model": settings.cartesia_model, "language": "fr"}
    if settings.cartesia_voice_id:
        kwargs["voice"] = settings.cartesia_voice_id
    else:
        logger.warning("CARTESIA_VOICE_ID vide : voix par défaut de Cartesia (souvent anglophone)")
    if settings.cartesia_api_key:
        kwargs["api_key"] = settings.cartesia_api_key
    if settings.tts_speed is not None:
        kwargs["speed"] = settings.tts_speed
    return cartesia.TTS(**kwargs)


def build_session(ud: CallUserData, *, vad: silero.VAD | None = None) -> AgentSession[CallUserData]:
    s = ud.settings
    return AgentSession[CallUserData](
        stt=build_stt(s, ud.campaign),
        llm=build_llm(s),
        tts=build_tts(s),
        vad=vad or load_vad(),
        turn_handling={
            "turn_detection": MultilingualModel(),
            # Réactivité réglable dans le .env (ENDPOINTING_MIN_DELAY / ENDPOINTING_MAX_DELAY).
            "endpointing": {"min_delay": s.endpointing_min_delay, "max_delay": s.endpointing_max_delay},
            "interruption": {"resume_false_interruption": True, "false_interruption_timeout": 1.0},
            "preemptive_generation": {"enabled": True},
        },
        userdata=ud,
        max_tool_steps=4,
        # « away » après ce délai de silence : géré dans worker.py (relance puis raccrochage).
        user_away_timeout=float(s.silence_timeout_s),
    )
