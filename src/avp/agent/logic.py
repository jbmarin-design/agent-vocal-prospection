"""Logique pure du worker vocal (aucune dépendance LiveKit) : testable unitairement.

Contient :
- ``CallUserData`` : l'objet partagé par la session et les outils (userdata LiveKit) ;
- la classification des échecs SIP et des résultats AMD en ``CallOutcome`` ;
- la déduction de l'issue quand aucun outil ne l'a fixée ;
- la validation des valeurs passées par le LLM aux outils (créneaux, dates, questions) ;
- la conversion de l'historique de conversation en transcription ;
- la persistance de fin d'appel (fichier JSON + base SQLite + liste d'opposition + prospect).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import db, outcomes
from ..config import Settings
from ..models import (
    AnalysisStatus,
    CallMetadata,
    CallOutcome,
    CallRecordFile,
    CallState,
    Campaign,
    Prospect,
    TranscriptTurn,
)

logger = logging.getLogger("avp.agent.logic")

# Issues qu'un agent a le droit de passer à terminer_appel (les autres sont fixées par le code).
END_OUTCOMES_ACCUEIL: tuple[CallOutcome, ...] = (
    CallOutcome.RAPPEL,
    CallOutcome.BARRAGE,
    CallOutcome.REFUS,
    CallOutcome.MAUVAIS_NUMERO,
    CallOutcome.NON_QUALIFIE,
)
END_OUTCOMES_DECIDEUR: tuple[CallOutcome, ...] = (
    CallOutcome.RDV,
    CallOutcome.RAPPEL,
    CallOutcome.QUALIFIE,
    CallOutcome.NON_QUALIFIE,
    CallOutcome.REFUS,
)


@dataclass
class CallUserData:
    """État d'un appel, partagé entre le worker, la session et les outils."""

    meta: CallMetadata
    campaign: Campaign
    settings: Settings
    state: CallState = field(default_factory=CallState)
    prompt_version: str = ""
    console: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    proposed_slots: list[datetime] = field(default_factory=list)
    hangup_requested: bool = False
    finalized: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    # Pendant le « Allô ? » : levé dès que l'interlocuteur parle (voir worker._probe_presence).
    presence_event: asyncio.Event | None = None
    presence_text: str = ""

    @property
    def prospect(self) -> Prospect:
        return self.meta.prospect

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def note(self, text: str) -> None:
        self.state.notes.append(text)


# ---------------------------------------------------------------------------
# Échecs SIP et résultats AMD
# ---------------------------------------------------------------------------

_SIP_BUSY = {486, 600}
_SIP_WRONG_NUMBER = {404, 410, 484, 485, 604}
_SIP_NO_ANSWER = {408, 480, 487, 603}


def classify_sip_failure(status_code: int | None, *, timeout: bool = False) -> CallOutcome:
    """Traduit l'échec de numérotation (code SIP ou délai dépassé) en issue d'appel."""
    if timeout:
        return CallOutcome.NON_DECROCHE
    if status_code in _SIP_BUSY:
        return CallOutcome.OCCUPE
    if status_code in _SIP_WRONG_NUMBER:
        return CallOutcome.MAUVAIS_NUMERO
    if status_code in _SIP_NO_ANSWER:
        return CallOutcome.NON_DECROCHE
    return CallOutcome.ERREUR


def outcome_from_amd(category: str) -> CallOutcome | None:
    """Issue imposée par l'AMD, ou None si un humain (ou un doute) : la conversation continue."""
    return {
        "machine-vm": CallOutcome.REPONDEUR,
        "machine-ivr": CallOutcome.SVI,
        "machine-unavailable": CallOutcome.NON_DECROCHE,
    }.get(category)


PRESENCE_PROMPTS: tuple[str, ...] = ("Allô ?", "Allô, vous m'entendez ?")

# Formules typiques des messageries vocales françaises (opérateurs et annonces personnalisées).
_VOICEMAIL_PATTERNS = (
    r"messagerie", r"r[ée]pondeur", r"bo[iî]te vocale", r"laiss\w* (?:un |votre )?message",
    r"apr[eè]s le (?:bip|signal|top)", r"au (?:bip|signal) sonore", r"(?:n'est|ne suis|sommes) pas disponible",
    r"(?:est|suis|sommes) (?:actuellement )?(?:absent|indisponible|en ligne|en communication)",
    r"rappeler ult[ée]rieurement", r"votre correspondant", r"le num[ée]ro (?:que vous avez compos[ée]|demand[ée])",
    r"(?:nos|les) (?:bureaux|horaires) (?:sont|d'ouverture)", r"tapez \d", r"appuyez sur",
)


def looks_like_voicemail(text: str) -> bool:
    """Vrai si la transcription ressemble à une messagerie ou à un serveur vocal."""
    import re

    t = (text or "").lower()
    return any(re.search(p, t) for p in _VOICEMAIL_PATTERNS)


def is_silent_pickup(category: str, reason: str, transcript: str) -> bool:
    """Vrai si l'appel a été décroché mais que personne n'a parlé pendant la détection."""
    return category == "uncertain" and (reason == "no_speech_timeout" or not transcript.strip())


def should_leave_voicemail(campaign: Campaign) -> bool:
    return campaign.repondeur.action == "message"


# ---------------------------------------------------------------------------
# Issue de l'appel
# ---------------------------------------------------------------------------


def infer_outcome(state: CallState, *, answered: bool) -> CallOutcome:
    """Issue finale. Priorité à ce que les outils ont établi, puis déduction prudente."""
    if state.optout:
        return CallOutcome.OPPOSITION
    if state.rdv is not None:
        return CallOutcome.RDV
    if state.transferred:
        return CallOutcome.TRANSFERT
    if state.outcome is not None:
        return state.outcome
    if state.callback is not None:
        return CallOutcome.RAPPEL
    amd = outcome_from_amd(state.amd_result)
    if amd is not None:
        return amd
    if not answered:
        return CallOutcome.NON_DECROCHE
    if state.decision_maker_reached:
        return CallOutcome.QUALIFIE if state.answers else CallOutcome.REFUS
    # Un humain a répondu mais rien n'a abouti (raccroché au standard, barrage silencieux).
    return CallOutcome.BARRAGE


def analysis_status_for(outcome: CallOutcome, *, test_mode: bool, console: bool) -> AnalysisStatus:
    if console:
        return AnalysisStatus.IGNORE
    if outcome.reached_human:
        return AnalysisStatus.EN_ATTENTE
    return AnalysisStatus.IGNORE


# ---------------------------------------------------------------------------
# Validation des arguments donnés par le LLM aux outils
# ---------------------------------------------------------------------------


def parse_tool_datetime(value: str, tz: ZoneInfo) -> datetime | None:
    """ISO 8601 → datetime avec fuseau (heure locale si absent). None si illisible ou vide."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def match_slot(value: str, slots: Sequence[datetime], tz: ZoneInfo, tolerance_min: int = 2) -> datetime | None:
    """Retrouve le créneau proposé correspondant à la valeur ISO donnée par le LLM."""
    dt = parse_tool_datetime(value, tz)
    if dt is None:
        return None
    for s in slots:
        s_aware = s if s.tzinfo else s.replace(tzinfo=UTC)
        if abs((s_aware - dt).total_seconds()) <= tolerance_min * 60:
            return s_aware
    return None


def question_ids(campaign: Campaign) -> set[str]:
    return {q.id for q in campaign.qualification}


def coerce_outcome(value: str, allowed: Iterable[CallOutcome]) -> CallOutcome | None:
    v = (value or "").strip().lower().replace("é", "e").replace(" ", "_")
    for o in allowed:
        if o.value == v:
            return o
    return None


def looks_like_email(value: str) -> bool:
    v = (value or "").strip()
    return "@" in v and "." in v.split("@")[-1] and " " not in v


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def history_to_transcript(items: Iterable[Mapping[str, Any]]) -> list[TranscriptTurn]:
    """Convertit des messages simplifiés {role, text, created_at, agent} en tours de transcription.

    Rôles : assistant → agent, user → prospect ; system/developer ignorés.
    `created_at` est un horodatage Unix (secondes) comme dans LiveKit Agents.
    """
    turns: list[TranscriptTurn] = []
    for it in items:
        role = it.get("role")
        text = (it.get("text") or "").strip()
        if not text:
            continue
        if role == "assistant":
            mapped = "agent"
        elif role == "user":
            mapped = "prospect"
        else:
            continue
        ts = it.get("created_at")
        turns.append(
            TranscriptTurn(
                role=mapped,  # type: ignore[arg-type]
                text=text,
                agent=str(it.get("agent") or ""),
                ts=datetime.fromtimestamp(ts, UTC) if isinstance(ts, int | float) else None,
            )
        )
    return turns


# ---------------------------------------------------------------------------
# Persistance de fin d'appel
# ---------------------------------------------------------------------------


def build_record(ud: CallUserData, transcript: list[TranscriptTurn]) -> CallRecordFile:
    return CallRecordFile(
        metadata=ud.meta,
        state=ud.state,
        transcript=transcript,
        prompt_version=ud.prompt_version,
        started_at=ud.started_at,
        answered_at=ud.answered_at,
        ended_at=ud.ended_at,
        usage=ud.usage,
    )


def write_record(record: CallRecordFile, transcripts_dir: Path) -> Path:
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    path = transcripts_dir / f"{record.metadata.call_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def finalize_call(ud: CallUserData, transcript: list[TranscriptTurn], *, error: str | None = None) -> Path:
    """Fin d'appel : fixe l'issue, écrit le JSON, met à jour la base. Idempotent (une seule fois).

    Fonction synchrone : depuis le worker, l'appeler via ``asyncio.to_thread``.
    """
    if ud.finalized:
        return ud.settings.transcripts_dir / f"{ud.meta.call_id}.json"
    ud.finalized = True
    ud.ended_at = ud.ended_at or datetime.now(UTC)

    outcome = CallOutcome.ERREUR if error and ud.state.outcome is None else infer_outcome(
        ud.state, answered=ud.answered_at is not None
    )
    ud.state.outcome = outcome
    if error and not ud.state.end_reason:
        ud.state.end_reason = error

    record = build_record(ud, transcript)
    path = write_record(record, ud.settings.transcripts_dir)

    if ud.console:
        logger.info("mode console : transcription écrite dans %s (pas de base de données)", path)
        return path

    meta = ud.meta
    duration = (ud.ended_at - ud.answered_at).total_seconds() if ud.answered_at else 0.0
    try:
        db.update_call(
            meta.call_id,
            db_path=ud.settings.db_path,
            status="erreur" if outcome is CallOutcome.ERREUR else "termine",
            started_at=ud.started_at,
            answered_at=ud.answered_at,
            ended_at=ud.ended_at,
            duration_s=round(duration, 1),
            amd_result=ud.state.amd_result,
            outcome=outcome,
            state_json=ud.state,
            transcript_path=str(path),
            prompt_version=ud.prompt_version,
            usage_json=ud.usage,
            analysis_status=analysis_status_for(outcome, test_mode=meta.test_mode, console=ud.console),
            error=error,
        )
    except Exception:
        logger.exception("mise à jour de l'appel %s impossible", meta.call_id)

    if ud.state.optout:
        try:
            db.add_optout(meta.prospect.phone, ud.state.optout_reason, source="agent", db_path=ud.settings.db_path)
        except Exception:
            logger.exception("ajout à la liste d'opposition impossible")

    if meta.prospect.id is not None:
        try:
            status, next_at = outcomes.next_step(
                outcome, ud.campaign, meta.attempt, datetime.now(UTC), ud.state.callback
            )
            db.release_prospect(
                meta.prospect.id,
                status,
                next_at,
                outcome=outcome,
                increment_attempts=outcomes.counts_as_attempt(outcome),
                db_path=ud.settings.db_path,
            )
        except Exception:
            logger.exception("libération du prospect %s impossible", meta.prospect.id)
    return path


def console_metadata(campaign_id: str, now: datetime | None = None) -> CallMetadata:
    """Métadonnées factices pour le mode console (test au micro, sans téléphone)."""
    now = now or datetime.now(UTC)
    slots = [
        (now + timedelta(days=d)).replace(hour=h, minute=0, second=0, microsecond=0)
        for d, h in ((2, 8), (2, 13), (3, 9))  # heures UTC ≈ 10 h, 15 h, 11 h à Paris
    ]
    return CallMetadata(
        call_id=f"console-{now.strftime('%Y%m%d-%H%M%S')}",
        campaign_id=campaign_id,
        prospect=Prospect(
            campaign_id=campaign_id,
            name="EHPAD Les Tilleuls",
            phone="+33562000000",
            city="Auch",
            notes="Prospect fictif pour le mode console.",
        ),
        attempt=1,
        test_mode=True,
        rdv_slots=slots,
    )


def dump_usage(summary: Any) -> dict[str, Any]:
    """Convertit le résumé d'usage LiveKit (objet pydantic ou dataclass) en dict JSON-sérialisable."""
    if summary is None:
        return {}
    for attr in ("model_dump", "dict"):
        fn = getattr(summary, attr, None)
        if callable(fn):
            try:
                return json.loads(json.dumps(fn(), default=str))
            except Exception:
                pass
    try:
        return json.loads(json.dumps(summary.__dict__, default=str))
    except Exception:
        return {"brut": str(summary)}


# ---------------------------------------------------------------------------
# Mesure de la latence (journal du worker)
# ---------------------------------------------------------------------------

_LATENCY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "user": (("end_of_turn_delay", "fin de tour"), ("transcription_delay", "transcription")),
    "assistant": (("llm_node_ttft", "1er mot LLM"), ("tts_node_ttfb", "1er son TTS")),
}


def format_latency(role: str, metrics: Mapping[str, Any] | None) -> str | None:
    """Résumé lisible des délais d'un tour (``ChatMessage.metrics`` de LiveKit), ou None.

    Côté prospect : délai de décision de fin de tour et de transcription.
    Côté agent : délai du premier mot de Claude et du premier son de la voix.
    """
    if not metrics:
        return None
    parts = [
        f"{label} {float(metrics[key]):.2f} s"
        for key, label in _LATENCY_FIELDS.get(role, ())
        if isinstance(metrics.get(key), int | float)
    ]
    return ", ".join(parts) or None
