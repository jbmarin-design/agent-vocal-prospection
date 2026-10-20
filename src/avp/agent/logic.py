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
    ContactInfo,
    Prospect,
    TranscriptTurn,
)
from ..phone import InvalidPhoneNumber, normalize_phone

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
# Agent unique (AgentAppel) : toutes les issues de fin d'appel du standard et du décideur.
END_OUTCOMES: tuple[CallOutcome, ...] = tuple(dict.fromkeys((*END_OUTCOMES_DECIDEUR, *END_OUTCOMES_ACCUEIL)))


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
    closing: bool = False  # au revoir final en cours (finish_call)
    last_ack: str = ""  # dernier acquiescement dit (pour varier)
    strip_interjection: bool = False  # un acquiescement vient d'être dit : épurer le début de la réponse
    opening_done: bool = False  # phrase d'ouverture lancée : avant, Claude ne répond à rien
    finalized: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    # Pendant le « Allô ? » : levé dès que l'interlocuteur parle (voir worker._probe_presence).
    presence_event: asyncio.Event | None = None
    presence_text: str = ""
    user_turns: int = 0  # tours de parole du prospect (fenêtre de détection de messagerie)

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
# Formules propres aux messageries et serveurs vocaux. Volontairement strictes : une secrétaire
# dit aussi « il n'est pas disponible, rappelez plus tard » — ces formules-là ne suffisent JAMAIS.
_VOICEMAIL_PATTERNS = (
    r"messagerie", r"r[ée]pondeur", r"bo[iî]te vocale",
    r"laissez(?:-moi|-nous)? (?:un |votre )?message",  # impératif d'annonce (pas « lui laisser un message »)
    r"apr[eè]s le (?:bip|signal|top)", r"au (?:bip|signal) sonore",
    r"votre correspondant", r"le num[ée]ro (?:que vous avez compos[ée]|demand[ée]|de votre correspondant)",
    r"n'est (?:plus )?attribu[ée]", r"(?:tapez|appuyez sur|faites le) (?:le )?(?:\d|un|deux|trois|quatre|z[ée]ro|di[èe]se|étoile)",
)
# Au-delà, une réponse n'est plus une annonce de messagerie mais une conversation.
VOICEMAIL_WINDOW_S = 25.0
VOICEMAIL_WINDOW_TURNS = 2


def looks_like_voicemail(text: str) -> bool:
    """Vrai si la transcription contient une formule typique de messagerie ou de serveur vocal."""
    import re

    t = (text or "").lower()
    return any(re.search(p, t) for p in _VOICEMAIL_PATTERNS)


def realtime_llm_options(settings: Settings) -> dict[str, Any]:
    """Paramètres du LLM de conversation selon le modèle.

    Haiku 4.5 : température 0,4, pas de réflexion. Haiku 5.5 et suivants refusent toute température
    (erreur 400) et réfléchissent par défaut, ce qui retarde le 1er mot : on coupe la réflexion
    (`LLM_REALTIME_THINKING`) et on règle l'effort (`LLM_REALTIME_EFFORT`), passés tels quels dans
    le corps de la requête.
    """
    if settings.llm_realtime_model.startswith(("claude-haiku-4", "claude-3")):
        return {"temperature": 0.4}
    return {
        "extra_body": {
            "thinking": {"type": settings.llm_realtime_thinking},
            "output_config": {"effort": settings.llm_realtime_effort},
        }
    }


_GOODBYE_PATTERNS = (
    r"au revoir",
    r"bonne (?:fin de )?(?:journ[ée]e|soir[ée]e|semaine)",
    r"bon (?:apr[eè]s-midi|week-end|weekend)",
)
GOODBYE_TAIL_CHARS = 80
GOODBYE_GRACE_S = 3.0


def is_goodbye(text: str) -> bool:
    """Vrai si la réplique de l'agent se termine par une formule d'au revoir.

    Seule la fin de la réplique compte : « bonne journée » glissé en milieu de phrase ne vaut pas
    fin d'appel. Sert à ne pas répéter l'au revoir fixe, et au raccrochage de secours.
    """
    import re

    tail = (text or "").strip().lower()[-GOODBYE_TAIL_CHARS:]
    return any(re.search(p, tail) for p in _GOODBYE_PATTERNS)


_CLOSING_PATTERNS = (
    r"\bmerci\b",
    r"au revoir",
    r"bonne (?:fin de )?(?:journ[ée]e|soir[ée]e|semaine)",
    r"\bc'est (?:tout|bon|not[ée]|parfait)\b",
)
CLOSING_MAX_WORDS = 12


def is_closing_remark(text: str) -> bool:
    """Vrai si le prospect clôt l'échange : réplique courte de remerciement ou d'au revoir
    (« Ok super, merci », « Non merci », « Au revoir »). Une longue réplique contenant « merci »
    n'en est pas une : la personne continue de parler."""
    import re

    t = (text or "").strip().lower()
    if not t or len(t.split()) > CLOSING_MAX_WORDS:
        return False
    return any(re.search(p, t) for p in _CLOSING_PATTERNS)


def ends_with_question(text: str) -> bool:
    """Vrai si la réplique de l'agent attend une réponse (se termine par une question)."""
    return (text or "").rstrip().endswith("?")


# Petits mots neutres, sans jugement ni accord sur le fond : « Très bien », « Je vois », « Bien sûr »
# ponctuaient mal les phrases et sonnaient faux quand la réponse ne s'y prêtait pas (JB, 19/10).
ACKNOWLEDGEMENTS: tuple[str, ...] = ("Ok.", "Ah.", "D'accord.", "Ok, d'accord.")
_QUESTION_STARTS = (
    "qui ", "quoi", "quel", "quelle", "comment", "pourquoi", "combien", "où ", "quand ", "est-ce",
    "c'est quoi", "c'est qui", "vous êtes", "vous appelez", "c'est à quel", "c'est pour",
)
ACK_MIN_WORDS = 2  # « C'est moi-même » compte : un blanc de 3,5 s a suivi faute de petit mot (20/10)


def pick_acknowledgement(user_text: str, previous: str = "", *, choice=None) -> str | None:
    """Petit acquiescement à dire tout de suite après la phrase du prospect, ou None.

    Il comble l'attente de la réponse de Claude (0,6 à 2 s selon la charge de l'API, 17/10)
    sans sonner automatique : jamais après une question (on y répond directement), ni après une
    réplique très courte (« Oui », « Allô ? »), ni après un remerciement de fin ; jamais deux fois
    la même formule de suite.
    """
    import random

    t = (user_text or "").strip().lower()
    if not t or t.endswith("?") or t.startswith(_QUESTION_STARTS):
        return None
    if len(t.split()) < ACK_MIN_WORDS or is_closing_remark(t):
        return None
    options = [a for a in ACKNOWLEDGEMENTS if a != previous] or list(ACKNOWLEDGEMENTS)
    return (choice or random.choice)(options)


_LEADING_INTERJECTION = (
    r"^\s*(?:(?:d['’]accord|ok(?:ay)?|bien|très bien|tres bien|parfait|je vois|je comprends|entendu|ah|super)"
    r"(?:\s*[,.!]+\s*|\s+(?=[a-zàâçéèêëîïôûùüÿ])))+"
)
INTERJECTION_BUFFER_CHARS = 30


def strip_leading_interjection(text: str) -> str:
    """Retire les interjections en tête de la réponse de Claude (« D'accord, donc… » → « Donc… »).

    Utilisé quand un acquiescement automatique vient d'être dit : sinon on entendait « Ok. D'accord,
    donc… » (test du 19/10). La première lettre restante passe en majuscule.
    """
    import re

    rest = re.sub(_LEADING_INTERJECTION, "", text or "", count=1, flags=re.IGNORECASE)
    if rest == text:
        return text
    return rest[:1].upper() + rest[1:]


# Nom des lettres en français, écrit pour la synthèse vocale : une lettre isolée (« I ») était lue à
# l'anglaise quand Sarah épelait un email (test du 19/10).
LETTER_NAMES_FR: dict[str, str] = {
    # « e » écrit « euh » : « eu » s'entendait « u » au téléphone (JB, 20/10).
    "a": "a", "b": "bé", "c": "cé", "d": "dé", "e": "euh", "f": "èf", "g": "gé", "h": "hache", "i": "i",
    "j": "ji", "k": "ka", "l": "èl", "m": "èm", "n": "èn", "o": "o", "p": "pé", "q": "ku", "r": "èr",
    "s": "èss", "t": "té", "u": "u", "v": "vé", "w": "double vé", "x": "iks", "y": "i grec", "z": "zèd",
}
_SPELLED_LETTER = r"(?:(?<=[,:])|{start})\s*([A-Za-z])\s*(?=[,.])"
SPELLING_DELIMITERS = ",.!?;:"


# Sigles que la synthèse vocale épelle alors qu'ils se disent comme un mot (« EHPAD » lu
# « E, H, P, A, D » au lieu de « épade » : JB, 20/10). Clé : expression régulière ; valeur : écriture
# phonétique. Ajouter ici les autres sigles mal prononcés après écoute.
PRONUNCIATIONS_FR: dict[str, str] = {
    r"\bEHPAD\b": "épade",
    r"\bfr\b": "èf èr",  # domaine « .fr » : épelé à la française, comme on le dit au téléphone
}


def pronounce_fr(text: str) -> str:
    """Remplace les sigles mal prononcés par leur écriture phonétique, avant la synthèse vocale."""
    import re

    def fix(word: str):
        def sub(m: re.Match) -> str:
            before = m.string[: m.start()].rstrip()
            sentence_start = not before or before[-1] in ".!?:"
            return word[:1].upper() + word[1:] if sentence_start else word
        return sub

    for pattern, word in PRONUNCIATIONS_FR.items():
        text = re.sub(pattern, fix(word), text or "", flags=re.IGNORECASE)
    return text


def spell_letters_fr(text: str, *, at_boundary: bool = True) -> str:
    """Remplace les lettres épelées (« i, point, m, a, r, i, n ») par leur nom en français.

    Une lettre est « épelée » si elle est seule entre deux virgules (ou après « : », ou en début de
    morceau quand `at_boundary`) et suivie d'une virgule ou d'un point. « il y a, bien sûr » n'est
    pas touché (« a » n'est pas précédé d'une virgule).
    """
    import re

    pattern = _SPELLED_LETTER.format(start="^" if at_boundary else "(?!)")

    def name(m: re.Match) -> str:
        return m.group(0).replace(m.group(1), LETTER_NAMES_FR[m.group(1).lower()])

    return re.sub(pattern, name, text or "")


_COMPLAINT_PATTERNS = (
    r"d[ée]marchage", r"d[ée]march(?:é|ée|és|ées)\b", r"p[ée]nible", r"harc[eè]l\w*", r"arr[êe]tez (?:de|d['’])", r"on en re[çc]oit",
    r"ras[- ]le[- ]bol", r"ne (?:m['’]|nous )appelez plus", r"retirez", r"bloctel", r"liste rouge",
    r"ne plus (?:[êe]tre )?(?:appel|d[ée]march)",
)
COMPLAINT_HINT = (
    "La personne se plaint du démarchage ou ne veut plus être appelée. Ne vous présentez pas, "
    "ne proposez rien, aucun argument : excusez-vous du dérangement en une phrase courte, dites que vous "
    "ne la rappellerez plus, et appelez enregistrer_opposition dans la même réponse."
)


_OPTOUT_PATTERNS = (
    r"ne (?:m['’]|nous )(?:appelez|rappelez|contactez) plus", r"(?:retirez|supprimez|effacez|rayez) (?:mon|notre|nos|le) "
    r"(?:num[ée]ro|coordonn[ée]es|fiche|ligne)", r"bloctel", r"liste (?:rouge|d['’]opposition)",
    r"(?:ne plus|plus jamais) (?:[êe]tre )?(?:appel[ée]e?s?|d[ée]march[ée]e?s?|contact[ée]e?s?)",
    r"(?:veux|souhaite) plus (?:[êe]tre )?(?:appel[ée]e?s?|d[ée]march[ée]e?s?|contact[ée]e?s?)",
    r"arr[êe]tez (?:de (?:m['’]|nous )|d['’])appeler",
)
OPTOUT_LINE = "Je comprends, excusez-moi du dérangement. C'est noté, nous ne vous rappellerons plus."


def is_optout_request(text: str) -> bool:
    """Vrai si le prospect demande explicitement à ne plus être appelé (règle 6, obligation légale).

    Traité sans Claude (phrase fixe, opposition, raccrochage) : malgré la consigne ciblée, Haiku
    glissait parfois encore une proposition (simulations du 19/10). Une plainte plus floue
    (« encore du démarchage ») reste traitée par Claude avec COMPLAINT_HINT (`is_complaint`).
    """
    import re

    t = (text or "").lower()
    return any(re.search(p, t) for p in _OPTOUT_PATTERNS)


def is_complaint(text: str) -> bool:
    """Vrai si le prospect se plaint du démarchage ou demande à ne plus être appelé (règle 6).

    Haiku enchaînait sur sa présentation malgré la consigne (simulations du 19/10) : on lui donne
    alors une consigne ciblée pour cette réplique (agents.AgentAppel, simulation).
    """
    import re

    t = (text or "").lower()
    return any(re.search(p, t) for p in _COMPLAINT_PATTERNS)


_DICTATION_PATTERNS = (r"[ée]pel", r"e-?mail", r"adresse (?:mail|[ée]lectronique)", r"arobase", r"c['’]est bien [çc]a")


def asks_for_dictation(text: str) -> bool:
    """Vrai si la réplique de Sarah appelle une dictée (email, épellation, vérification d'adresse)."""
    import re

    t = (text or "").lower()
    return any(re.search(p, t) for p in _DICTATION_PATTERNS)


_SELF_ID_PATTERN = (
    r"c['’]est (?:bien )?(?:moi|lui|elle)\b|moi-m[êe]me|lui-m[êe]me|elle-m[êe]me|\bmoi m[êe]me\b|"
    r"je suis (?:le|la) (?:directeur|directrice)"
)
_SELF_ID_BLOCKERS = r"\bmais\b|\bpas\b|\bplus\b|\bmon\b|\bma\b|\?"
SELF_ID_MAX_WORDS = 7


def is_self_identification(text: str) -> bool:
    """Vrai si la personne qui a décroché dit simplement être le décideur (« C'est moi-même »).

    Réplique courte sans autre contenu (ni « mais », ni négation, ni question) : la suite
    (« merci, je vous appelle de la part de… deux minutes ? ») est alors dite sans attendre Claude
    (agents.AgentAppel). Tolère les transcriptions approximatives (« oui pour moi-même »).
    """
    import re

    t = (text or "").strip().lower()
    if not t or len(t.split()) > SELF_ID_MAX_WORDS or re.search(_SELF_ID_BLOCKERS, t):
        return False
    return bool(re.search(_SELF_ID_PATTERN, t))


def voicemail_window_open(answered_at: datetime | None, now: datetime, user_turns: int) -> bool:
    """La détection par la transcription ne joue qu'au tout début de l'appel (annonce d'accueil),
    jamais au milieu d'un échange avec une secrétaire."""
    if user_turns > VOICEMAIL_WINDOW_TURNS:
        return False
    return answered_at is None or (now - answered_at).total_seconds() <= VOICEMAIL_WINDOW_S


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
            if ud.state.contacts and not ud.state.optout:
                decider = pick_decision_contact(ud.state.contacts)
                db.enrich_prospect_contact(
                    meta.prospect.id,
                    contact_name=decider.nom_complet if decider else "",
                    contact_role=decider.fonction if decider else "",
                    note=contacts_note(ud.state.contacts, datetime.now(ud.tz)),
                    db_path=ud.settings.db_path,
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


# ---------------------------------------------------------------------------
# Contacts recueillis
# ---------------------------------------------------------------------------


def _same_person(a: ContactInfo, b: ContactInfo) -> bool:
    if a.email and b.email:
        return a.email.lower() == b.email.lower()
    if a.nom and b.nom:
        return a.nom.lower() == b.nom.lower() and (not a.prenom or not b.prenom or a.prenom.lower() == b.prenom.lower())
    # sans nom : même fonction (ex. « agent technique » complété en deux fois)
    return bool(not a.nom and not b.nom and a.fonction and a.fonction.lower() == b.fonction.lower())


def merge_contact(state: CallState, new: ContactInfo) -> ContactInfo:
    """Ajoute un contact à l'état d'appel, ou complète la fiche existante de la même personne.

    Le téléphone est normalisé en E.164 quand c'est possible (sinon gardé tel que dicté).
    Les champs vides ne remplacent jamais une valeur déjà connue.
    """
    data = {k: (v.strip() if isinstance(v, str) else v) for k, v in new.model_dump().items()}
    if data["telephone"]:
        try:
            data["telephone"] = normalize_phone(data["telephone"])
        except InvalidPhoneNumber:
            pass
    if data["email"] and not looks_like_email(data["email"]):
        data["notes"] = " ".join(x for x in (data["notes"], f"email dicté : {data['email']}") if x)
        data["email"] = ""
    contact = ContactInfo(**data)
    for i, old in enumerate(state.contacts):
        if _same_person(old, contact):
            merged = old.model_dump()
            for k, v in contact.model_dump().items():
                if v and (k != "notes" or v not in merged[k]):
                    merged[k] = v if k != "notes" or not merged[k] else f"{merged[k]} ; {v}"
            state.contacts[i] = ContactInfo(**merged)
            return state.contacts[i]
    state.contacts.append(contact)
    return contact


_DECIDER_WORDS = ("direct", "gérant", "gerant", "responsable d'établissement", "président", "president")


def pick_decision_contact(contacts: Sequence[ContactInfo]) -> ContactInfo | None:
    """Le contact à demander au prochain appel : un directeur ou une directrice nommé(e) en priorité,
    sinon le premier contact nommé."""
    named = [c for c in contacts if c.nom_complet]
    for c in named:
        if any(w in c.fonction.lower() for w in _DECIDER_WORDS):
            return c
    return named[0] if named else None


def contacts_note(contacts: Sequence[ContactInfo], now: datetime) -> str:
    """Note de fiche prospect résumant les contacts recueillis lors d'un appel."""
    lines = []
    for c in contacts:
        bits = [c.nom_complet or "?", f"({c.fonction})" if c.fonction else ""]
        if c.telephone:
            bits.append(c.telephone)
        if c.email:
            bits.append(c.email)
        if c.disponibilites:
            bits.append(f"dispo : {c.disponibilites}")
        lines.append(" ".join(b for b in bits if b))
    return f"Contacts recueillis le {now:%d/%m/%Y} : " + " ; ".join(lines)
