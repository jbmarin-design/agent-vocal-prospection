"""Post-appel : analyse de la transcription par Claude Sonnet puis synchronisation Axonaut.

Chaîne : `process_pending` → `process_call` → `analyze_call` (LLM) → `sync_axonaut` (CRM).

- L'analyse utilise un outil **forcé** (`tool_choice`) dont le schéma d'entrée est celui de
  `CallAnalysis` : la sortie est donc du JSON structuré, validé par Pydantic (un réessai si invalide).
- Aucune dépendance LiveKit ; `anthropic` n'est importé que si aucun client n'est injecté.
- La synchronisation Axonaut n'est **jamais** rejouée automatiquement (pour éviter les doublons
  d'événements) : en cas d'erreur partielle, `calls.axonaut_synced=0` et `calls.error` décrit le problème.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from . import db
from .axonaut import AxonautClient, AxonautError
from .campaigns import CampaignNotFound, load_campaign
from .config import Settings, get_settings
from .models import (
    AnalysisStatus,
    AxonautMapping,
    CallAnalysis,
    CallOutcome,
    CallRecordFile,
    Campaign,
)

log = logging.getLogger("avp.postcall")

ANALYSIS_TOOL_NAME = "enregistrer_analyse"
PROMPT_FILE = "analyse_post_appel.md"
MAX_TOKENS = 4096

OUTCOME_LABELS: dict[CallOutcome, str] = {
    CallOutcome.NON_DECROCHE: "non décroché",
    CallOutcome.OCCUPE: "occupé",
    CallOutcome.MAUVAIS_NUMERO: "mauvais numéro",
    CallOutcome.REPONDEUR: "répondeur",
    CallOutcome.SVI: "serveur vocal",
    CallOutcome.BARRAGE: "barrage standard",
    CallOutcome.RAPPEL: "rappel convenu",
    CallOutcome.REFUS: "refus",
    CallOutcome.OPPOSITION: "opposition",
    CallOutcome.NON_QUALIFIE: "hors cible",
    CallOutcome.QUALIFIE: "qualifié",
    CallOutcome.RDV: "RDV pris",
    CallOutcome.TRANSFERT: "transféré",
    CallOutcome.ERREUR: "erreur technique",
}
SCORE_LABELS = {"chaud": "chaud", "tiede": "tiède", "froid": "froid"}

# Issues sans échange : rien n'est écrit dans Axonaut.
_NO_SYNC_OUTCOMES = {
    CallOutcome.NON_DECROCHE,
    CallOutcome.OCCUPE,
    CallOutcome.REPONDEUR,
    CallOutcome.SVI,
    CallOutcome.ERREUR,
}
# Issues pour lesquelles on ne crée jamais d'opportunité.
_NO_OPPORTUNITY_OUTCOMES = {CallOutcome.OPPOSITION, CallOutcome.MAUVAIS_NUMERO}


class AnalysisError(RuntimeError):
    """L'analyse LLM n'a pas produit de résultat exploitable."""


# ---------------------------------------------------------------------------
# Mapping Axonaut (logique pure)
# ---------------------------------------------------------------------------


def target_step(outcome: CallOutcome, analysis: CallAnalysis | None, mapping: AxonautMapping) -> str | None:
    """Étape Axonaut cible : d'abord selon l'issue, sinon selon le score ; None si rien n'est mappé."""
    step = mapping.etapes.get(outcome.value)
    if step:
        return step
    if analysis is not None:
        step = mapping.etapes.get(analysis.score)
        if step:
            return step
    return None


# ---------------------------------------------------------------------------
# Construction des messages pour le LLM
# ---------------------------------------------------------------------------


def _tz(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _local(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz)


def _fmt_dt(dt: datetime | None, tz: ZoneInfo) -> str:
    loc = _local(dt, tz)
    return loc.strftime("%d/%m/%Y %H:%M") if loc else "inconnue"


def outcome_of(record: CallRecordFile) -> CallOutcome:
    return record.state.outcome or CallOutcome.ERREUR


def call_duration_s(record: CallRecordFile) -> float | None:
    start = record.answered_at or record.started_at
    if start and record.ended_at:
        return max(0.0, (record.ended_at - start).total_seconds())
    return None


def format_transcript(record: CallRecordFile, tz: ZoneInfo) -> str:
    """Transcription lisible : `[hh:mm:ss] Agent (accueil) : …` / `Prospect : …`."""
    lines: list[str] = []
    for turn in record.transcript:
        stamp = ""
        loc = _local(turn.ts, tz)
        if loc:
            stamp = f"[{loc.strftime('%H:%M:%S')}] "
        if turn.role == "agent":
            who = f"Agent IA ({turn.agent})" if turn.agent else "Agent IA"
        elif turn.role == "prospect":
            who = "Prospect"
        else:
            who = "Système"
        text = " ".join(turn.text.split())
        if text:
            lines.append(f"{stamp}{who} : {text}")
    return "\n".join(lines) if lines else "(transcription vide)"


def build_system_prompt(campaign: Campaign, settings: Settings) -> str:
    """Prompt système : prompts/analyse_post_appel.md + contexte de la campagne."""
    path = Path(settings.prompts_dir) / PROMPT_FILE
    base = path.read_text(encoding="utf-8").strip() if path.exists() else _FALLBACK_PROMPT
    parts = [base, "", f"# Campagne : {campaign.nom} (`{campaign.id}`)", f"Objectif de l'appel : {campaign.objectif}"]
    cible = campaign.cible.interlocuteur
    if campaign.cible.alternatives:
        cible += f" (ou : {', '.join(campaign.cible.alternatives)})"
    parts.append(f"Interlocuteur cible : {cible}")
    parts.append(f"Offre : {campaign.offre.resume}")
    if campaign.scoring:
        parts.append("\n## Critères de score propres à la campagne")
        for key in ("chaud", "tiede", "froid"):
            if key in campaign.scoring:
                parts.append(f"- **{SCORE_LABELS[key]}** : {campaign.scoring[key]}")  # type: ignore[index]
    if campaign.qualification:
        parts.append("\n## Questions de qualification (id : question — but)")
        for q in campaign.qualification:
            flag = " [obligatoire]" if q.obligatoire else ""
            but = f" — {q.but}" if q.but else ""
            parts.append(f"- `{q.id}` : {q.question}{but}{flag}")
    if campaign.objections:
        parts.append("\n## Objections attendues et réponses prévues au script")
        for o in campaign.objections:
            parts.append(f"- « {o.objection} » → {o.reponse}")
    parts.append(
        f"\nRends ton analyse **uniquement** en appelant l'outil `{ANALYSIS_TOOL_NAME}`, en français."
    )
    return "\n".join(parts)


def build_user_message(record: CallRecordFile, settings: Settings) -> str:
    tz = _tz(settings)
    p = record.metadata.prospect
    dur = call_duration_s(record)
    fiche = [
        f"- Établissement : {p.name}",
        f"- Ville : {p.city or 'inconnue'}",
        f"- Contact connu : {p.contact_name or 'aucun'}{f' ({p.contact_role})' if p.contact_role else ''}",
        f"- Tentative n° : {record.metadata.attempt}",
    ]
    if p.notes:
        fiche.append(f"- Notes / historique : {p.notes}")
    state_json = record.state.model_dump_json(indent=2)
    return "\n".join(
        [
            "# Appel à analyser",
            f"Date de l'appel ({settings.timezone}) : {_fmt_dt(record.started_at or record.answered_at, tz)}",
            f"Durée de conversation : {round(dur) if dur is not None else 'inconnue'} s",
            f"Issue enregistrée par l'agent : {outcome_of(record).value}",
            "",
            "## Fiche prospect",
            *fiche,
            "",
            "## État enregistré par les outils de l'agent (CallState)",
            "```json",
            state_json,
            "```",
            "",
            "## Transcription",
            format_transcript(record, tz),
        ]
    )


def analysis_tool() -> dict[str, Any]:
    return {
        "name": ANALYSIS_TOOL_NAME,
        "description": "Enregistre l'analyse structurée de l'appel de prospection (score, résumé, objections…).",
        "input_schema": CallAnalysis.model_json_schema(),
    }


# ---------------------------------------------------------------------------
# Appel LLM
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _find_tool_use(response: Any) -> Any | None:
    for block in _get(response, "content", None) or []:
        if _get(block, "type") == "tool_use" and _get(block, "name") == ANALYSIS_TOOL_NAME:
            return block
    return None


def _make_anthropic_client(settings: Settings) -> Any:
    try:
        import anthropic  # import paresseux : non requis pour les tests
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise AnalysisError("paquet `anthropic` non installé (pip install anthropic)") from exc
    if not settings.anthropic_api_key:
        raise AnalysisError("ANTHROPIC_API_KEY absente du .env")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _postprocess(analysis: CallAnalysis, record: CallRecordFile, tz: ZoneInfo) -> CallAnalysis:
    """Ajustements déterministes : fuseau de date_relance, cohérence avec l'état des outils."""
    updates: dict[str, Any] = {}
    if analysis.date_relance is not None and analysis.date_relance.tzinfo is None:
        updates["date_relance"] = analysis.date_relance.replace(tzinfo=tz)
    # Le RDV confirmé par l'outil fait foi.
    if record.state.rdv is not None and outcome_of(record) is CallOutcome.RDV and not analysis.rdv_confirme:
        updates["rdv_confirme"] = True
    return analysis.model_copy(update=updates) if updates else analysis


async def analyze_call(
    record: CallRecordFile, campaign: Campaign, *, client: Any = None, settings: Settings | None = None
) -> CallAnalysis:
    """Analyse un appel avec Claude (modèle `settings.llm_analysis_model`) et renvoie un `CallAnalysis`.

    `client` : objet compatible `anthropic.AsyncAnthropic` (`await client.messages.create(...)`).
    Lève `AnalysisError` si deux tentatives ne donnent pas de résultat valide.
    """
    settings = settings or get_settings()
    owns_client = client is None
    if client is None:
        client = _make_anthropic_client(settings)
    tz = _tz(settings)
    system = build_system_prompt(campaign, settings)
    tool = analysis_tool()
    messages: list[dict[str, Any]] = [{"role": "user", "content": build_user_message(record, settings)}]
    last_error = ""
    try:
        for attempt in (1, 2):
            response = await client.messages.create(
                model=settings.llm_analysis_model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": ANALYSIS_TOOL_NAME},
                messages=messages,
            )
            block = _find_tool_use(response)
            if block is None:
                last_error = "aucun appel à l'outil d'analyse dans la réponse"
                log.warning("analyse %s (essai %d) : %s", record.metadata.call_id, attempt, last_error)
                messages = [
                    *messages[:1],
                    {"role": "assistant", "content": "(réponse sans appel d'outil)"},
                    {"role": "user", "content": f"Appelle obligatoirement l'outil `{ANALYSIS_TOOL_NAME}`."},
                ]
                continue
            raw_input = _get(block, "input", {})
            if isinstance(raw_input, str):  # robustesse : certains clients renvoient une chaîne JSON
                try:
                    raw_input = json.loads(raw_input)
                except ValueError:
                    pass
            try:
                analysis = CallAnalysis.model_validate(raw_input)
            except ValidationError as exc:
                last_error = f"sortie invalide : {exc}"
                log.warning("analyse %s (essai %d) : %s", record.metadata.call_id, attempt, last_error)
                tool_id = _get(block, "id", "toolu_analyse")
                messages = [
                    *messages[:1],
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": tool_id, "name": ANALYSIS_TOOL_NAME, "input": raw_input}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "is_error": True,
                                "content": (
                                    "Ces données ne respectent pas le schéma. Corrige et rappelle l'outil.\n"
                                    f"Erreurs : {exc.errors(include_url=False)}"
                                ),
                            }
                        ],
                    },
                ]
                continue
            return _postprocess(analysis, record, tz)
    finally:
        if owns_client and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - fermeture best-effort
                pass
    raise AnalysisError(f"analyse impossible après 2 essais : {last_error}")


# ---------------------------------------------------------------------------
# Synchronisation Axonaut
# ---------------------------------------------------------------------------


def _id(resp: Any) -> Any:
    return resp.get("id") if isinstance(resp, dict) else None


def _event_title(outcome: CallOutcome, analysis: CallAnalysis | None) -> str:
    score = SCORE_LABELS.get(analysis.score, analysis.score) if analysis else "non analysé"
    if analysis:
        score = f"{score} ({analysis.score_num}/100)"
    return f"Appel IA — {OUTCOME_LABELS.get(outcome, outcome.value)} — {score}"


def _event_content(
    record: CallRecordFile, analysis: CallAnalysis | None, campaign: Campaign, tz: ZoneInfo, transcript_path: str
) -> str:
    st = record.state
    lines: list[str] = []
    if analysis:
        lines += [f"Résumé : {analysis.resume}", ""]
    contact = analysis.interlocuteur if analysis and analysis.interlocuteur else st.contact_name
    fonction = analysis.fonction if analysis and analysis.fonction else st.contact_role
    if contact or fonction:
        lines.append(f"Interlocuteur : {contact}{f' ({fonction})' if fonction else ''}")
    if st.contact_email:
        lines.append(f"Email : {st.contact_email}")
    if analysis:
        for label, val in (
            ("Besoin", analysis.besoin),
            ("Équipement actuel", analysis.equipement_actuel),
            ("Prestataire actuel", analysis.prestataire_actuel),
            ("Échéance contrat", analysis.echeance_contrat),
        ):
            if val:
                lines.append(f"{label} : {val}")
    if st.answers:
        questions = {q.id: q.question for q in campaign.qualification}
        lines += ["", "Réponses de qualification :"]
        for qid, ans in st.answers.items():
            lines.append(f"- {questions.get(qid, qid)} → {ans}")
    if analysis and analysis.objections:
        lines += ["", "Objections :", *[f"- {o}" for o in analysis.objections]]
    if analysis and analysis.points_cles:
        lines += ["", "Points clés :", *[f"- {p}" for p in analysis.points_cles]]
    lines.append("")
    if analysis and analysis.prochaine_action:
        lines.append(f"Prochaine action : {analysis.prochaine_action}")
    if analysis and analysis.date_relance:
        lines.append(f"Date de relance : {_fmt_dt(analysis.date_relance, tz)}")
    if st.rdv:
        lines.append(
            f"RDV : {_fmt_dt(st.rdv.start, tz)} ({st.rdv.duree_min} min"
            f"{', ' + st.rdv.mode if st.rdv.mode else ''}){f' avec {st.rdv.avec}' if st.rdv.avec else ''}"
        )
    if st.callback:
        when = _fmt_dt(st.callback.when, tz) if st.callback.when else "date non fixée"
        lines.append(f"Rappel : {when}{f' — demander {st.callback.ask_for}' if st.callback.ask_for else ''}")
    if st.optout:
        lines.append(f"Opposition au démarchage{f' : {st.optout_reason}' if st.optout_reason else ''}")
    lines += [
        "",
        f"Campagne : {campaign.nom} — tentative {record.metadata.attempt} — appel {record.metadata.call_id}",
        f"Transcription locale : {transcript_path}",
    ]
    return "\n".join(lines).strip()


def _opportunity_comment(outcome: CallOutcome, analysis: CallAnalysis | None, when: datetime, tz: ZoneInfo) -> str:
    loc = _local(when, tz) or when
    head = f"[{loc.strftime('%d/%m/%Y')} appel IA] {OUTCOME_LABELS.get(outcome, outcome.value)}"
    if analysis:
        head += f" — {SCORE_LABELS.get(analysis.score, analysis.score)} {analysis.score_num}/100 — {analysis.resume}"
        if analysis.prochaine_action:
            head += f" Prochaine action : {analysis.prochaine_action}"
    return head


def _date_in_tz(dt: datetime, tz: ZoneInfo) -> date:
    loc = _local(dt, tz)
    assert loc is not None
    return loc.date()


async def sync_axonaut(
    record: CallRecordFile,
    analysis: CallAnalysis | None,
    campaign: Campaign,
    *,
    axonaut: AxonautClient,
    transcript_path: str | Path | None = None,
    settings: Settings | None = None,
) -> dict:
    """Écrit le résultat de l'appel dans Axonaut. Retourne un récapitulatif des ids créés/mis à jour.

    Clés possibles : `skipped`, `event_id`, `opportunity_id`, `opportunity_action` (cree|maj),
    `rdv_event_id`, `optout_event_id`, `task_ids`, `erreurs` (liste de messages, vide si tout est passé).
    """
    meta = record.metadata
    if meta.test_mode:
        return {"skipped": "appel de test (test_mode)"}
    if campaign.axonaut is None:
        return {"skipped": "pas de mapping Axonaut dans la campagne"}
    company_id = meta.prospect.axonaut_company_id
    if not company_id:
        return {"skipped": "prospect sans axonaut_company_id"}
    outcome = outcome_of(record)
    if outcome in _NO_SYNC_OUTCOMES:
        return {"skipped": f"aucun échange ({outcome.value})"}

    mapping = campaign.axonaut
    settings = settings or get_settings()
    tz = _tz(settings)
    st = record.state
    p = meta.prospect
    call_date = record.started_at or record.answered_at or db.utcnow()
    dur = call_duration_s(record)
    duration_min = max(1, math.ceil(dur / 60)) if dur is not None else None
    transcript_path = str(transcript_path or settings.transcripts_dir / f"{meta.call_id}.json")
    result: dict[str, Any] = {"task_ids": [], "erreurs": []}

    def fail(what: str, exc: Exception) -> None:
        msg = f"{what} : {exc}"
        log.error("Axonaut, appel %s — %s", meta.call_id, msg)
        result["erreurs"].append(msg)

    # 1) Opportunité (traitée d'abord pour pouvoir y rattacher les événements)
    opp_id: int | None = p.axonaut_opportunity_id
    existing: dict | None = None
    try:
        if opp_id is None and hasattr(axonaut, "find_opportunity"):
            existing = await axonaut.find_opportunity(company_id, mapping.pipe)
            if existing and existing.get("id"):
                opp_id = int(existing["id"])
    except AxonautError as exc:
        fail("recherche d'opportunité", exc)

    step = target_step(outcome, analysis, mapping)
    probability = float(analysis.score_num) if analysis else None
    comment_line = _opportunity_comment(outcome, analysis, call_date, tz)
    try:
        if opp_id is not None:
            # PATCH `comments` remplace le commentaire existant : on préfixe l'historique lu au préalable,
            # et on n'écrit pas de commentaire si on n'a pas pu le relire.
            comments: str | None = None
            old: Any = existing.get("comments") if existing else None
            if existing is None and hasattr(axonaut, "get_opportunity"):
                try:
                    old = (await axonaut.get_opportunity(opp_id)).get("comments")
                    existing = {}
                except AxonautError as exc:
                    log.warning("lecture de l'opportunité %s impossible : %s", opp_id, exc)
            if existing is not None:
                comments = f"{comment_line}\n{old}".strip() if old else comment_line
            if step or probability is not None or comments:
                await axonaut.update_opportunity(opp_id, step_name=step, probability=probability, comments=comments)
                result["opportunity_action"] = "maj"
            result["opportunity_id"] = opp_id
        elif outcome not in _NO_OPPORTUNITY_OUTCOMES:
            created = await axonaut.create_opportunity(
                company_id=company_id,
                pipe_name=mapping.pipe,
                step_name=step or mapping.etape_initiale,
                name=f"{p.name} — {campaign.nom}",
                amount=mapping.montant_defaut,
                probability=probability,
                comments=comment_line,
            )
            new_id = _id(created)
            opp_id = int(new_id) if new_id else None
            result["opportunity_id"] = opp_id
            result["opportunity_action"] = "cree"
    except AxonautError as exc:
        fail("opportunité", exc)

    # 2) Événement « appel »
    try:
        ev = await axonaut.create_event(
            company_id=company_id,
            title=_event_title(outcome, analysis),
            content=_event_content(record, analysis, campaign, tz, transcript_path),
            date=call_date,
            nature=3,
            duration_min=duration_min,
            opportunity_id=opp_id,
            is_done=True,
        )
        result["event_id"] = _id(ev)
    except AxonautError as exc:
        fail("événement d'appel", exc)

    today = datetime.now(tz).date()

    # 3) Suites : RDV, rappel, relance, opposition
    if outcome is CallOutcome.OPPOSITION or st.optout:
        try:
            ev = await axonaut.create_event(
                company_id=company_id,
                title="Opposition au démarchage",
                content=(
                    f"Demande de ne plus être appelé, exprimée lors de l'appel IA du {_fmt_dt(call_date, tz)}"
                    f"{f' : {st.optout_reason}' if st.optout_reason else ''}.\n"
                    f"Numéro {p.phone} ajouté à la liste d'opposition locale. Ne plus démarcher."
                ),
                date=call_date,
                nature=6,
                is_done=True,
            )
            result["optout_event_id"] = _id(ev)
        except AxonautError as exc:
            fail("événement d'opposition", exc)
        return result

    if st.rdv is not None and (outcome is CallOutcome.RDV or (analysis and analysis.rdv_confirme)):
        # Le RDV est posé dans Google Agenda (avp.agenda.sync_agenda), synchronisé avec Axonaut :
        # rien à écrire dans l'agenda Axonaut. L'événement « Appel IA » ci-dessus le mentionne.
        pass

    elif outcome is CallOutcome.RAPPEL or st.callback is not None:
        cb = st.callback
        person = (cb.ask_for if cb and cb.ask_for else "") or st.contact_name or p.contact_name or "le décideur"
        when = (cb.when if cb else None) or (analysis.date_relance if analysis else None)
        if when:
            due = max(today, _date_in_tz(when, tz))
        else:
            due = today + timedelta(days=max(1, campaign.delai_entre_tentatives_h // 24))
        try:
            task = await axonaut.create_task(
                title=f"Rappeler {person} — {p.name}"[:255],
                company_id=company_id,
                description=(
                    f"Rappel convenu lors de l'appel IA du {_fmt_dt(call_date, tz)}"
                    f"{f', le {_fmt_dt(when, tz)}' if when else ' (date non fixée)'}.\n"
                    + (f"Notes : {cb.notes}\n" if cb and cb.notes else "")
                    + "La campagne replanifie l'appel automatiquement ; cette tâche sert au suivi."
                ),
                due=due,
                priority="normale",
            )
            result["task_ids"].append(_id(task))
        except AxonautError as exc:
            fail("tâche de rappel", exc)

    elif outcome in (CallOutcome.QUALIFIE, CallOutcome.TRANSFERT) and analysis and analysis.date_relance:
        try:
            task = await axonaut.create_task(
                title=f"Relancer {p.name}"[:255],
                company_id=company_id,
                description=f"{analysis.prochaine_action or 'Relance commerciale'}\nRésumé : {analysis.resume}",
                due=max(today, _date_in_tz(analysis.date_relance, tz)),
                priority="haute" if analysis.score == "chaud" else "normale",
            )
            result["task_ids"].append(_id(task))
        except AxonautError as exc:
            fail("tâche de relance", exc)

    # 4) Échéance lointaine (fin de contrat, décision dans 1 ou 2 ans) : tâche de relance anticipée
    if analysis and analysis.date_decision and analysis.date_decision > today + timedelta(days=30):
        due = max(today + timedelta(days=7),
                  analysis.date_decision - timedelta(days=mapping.relance_avant_echeance_jours))
        objet = analysis.objet_decision or "échéance"
        try:
            task = await axonaut.create_task(
                title=f"Relance échéance : {objet} — {p.name}"[:255],
                company_id=company_id,
                description=(
                    f"Échéance mentionnée lors de l'appel IA du {_fmt_dt(call_date, tz)} : "
                    f"{objet}, le {analysis.date_decision.strftime('%d/%m/%Y')}.\n"
                    f"Relance prévue {mapping.relance_avant_echeance_jours} jours avant.\n"
                    f"Résumé : {analysis.resume}"
                ),
                due=due,
                priority="normale",
            )
            result["task_ids"].append(_id(task))
            result["echeance"] = analysis.date_decision.isoformat()
        except AxonautError as exc:
            fail("tâche d'échéance", exc)

    return result


# ---------------------------------------------------------------------------
# Traitement d'un appel / de la file
# ---------------------------------------------------------------------------


class _LazyAnthropic:
    """Crée le client Anthropic au premier usage (évite l'import si aucun appel n'est à analyser)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None

    @property
    def messages(self) -> Any:
        if self._client is None:
            self._client = _make_anthropic_client(self._settings)
        return self._client.messages

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()


def _load_record(row: dict[str, Any], settings: Settings) -> tuple[CallRecordFile, Path]:
    path = Path(row["transcript_path"]) if row.get("transcript_path") else settings.transcripts_dir / f"{row['id']}.json"
    if not path.is_absolute() and not path.exists():
        candidate = settings.data_dir / path
        if candidate.exists():
            path = candidate
    if not path.exists():
        raise FileNotFoundError(f"transcription introuvable : {path}")
    return CallRecordFile.model_validate_json(path.read_text(encoding="utf-8")), path


async def process_call(
    call_id: str,
    *,
    llm_client: Any = None,
    axonaut: AxonautClient | None = None,
    settings: Settings | None = None,
) -> CallAnalysis | None:
    """Analyse (si un humain a été joint) puis synchronise Axonaut pour un appel ; met à jour la base.

    Retourne l'analyse, ou None (pas d'humain joint, ou erreur — consignée dans `calls.error`).
    """
    settings = settings or get_settings()
    db_path = settings.db_path
    row = db.get_call(call_id, db_path)
    if row is None:
        raise LookupError(f"appel inconnu : {call_id}")

    try:
        record, path = _load_record(row, settings)
        campaign = load_campaign(row["campaign_id"], settings.campaigns_dir)
    except (FileNotFoundError, CampaignNotFound, ValidationError, ValueError) as exc:
        log.error("post-appel %s : %s", call_id, exc)
        db.update_call(call_id, db_path, analysis_status=AnalysisStatus.ERREUR, error=f"post-appel : {exc}")
        return None

    if record.state.outcome is None and row.get("outcome"):
        try:
            record.state.outcome = CallOutcome(row["outcome"])
        except ValueError:
            pass
    if row.get("test_mode") and not record.metadata.test_mode:
        record.metadata.test_mode = True
    outcome = outcome_of(record)

    analysis: CallAnalysis | None = None
    if outcome.reached_human:
        try:
            analysis = await analyze_call(record, campaign, client=llm_client, settings=settings)
        except Exception as exc:  # noqa: BLE001 - toute erreur LLM est consignée, l'appel reste rejouable
            log.exception("analyse %s en échec", call_id)
            db.update_call(call_id, db_path, analysis_status=AnalysisStatus.ERREUR, error=f"analyse : {exc}")
            return None

    # Synchronisation Axonaut
    sync: dict[str, Any]
    own_axonaut = False
    client = axonaut
    try:
        if client is None and not record.metadata.test_mode and campaign.axonaut is not None:
            if settings.axonaut_api_key or settings.dry_run:
                client = AxonautClient(
                    api_key=settings.axonaut_api_key,
                    base_url=settings.axonaut_base_url,
                    dry_run=settings.dry_run,
                    user_email=settings.axonaut_user_email,
                    timezone=settings.timezone,
                )
                own_axonaut = True
        if client is None:
            sync = {"skipped": "clé API Axonaut absente" if campaign.axonaut else "pas de mapping Axonaut"}
            if record.metadata.test_mode:
                sync = {"skipped": "appel de test (test_mode)"}
        else:
            sync = await sync_axonaut(
                record, analysis, campaign, axonaut=client, transcript_path=path, settings=settings
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("synchronisation Axonaut %s en échec", call_id)
        sync = {"erreurs": [f"synchronisation Axonaut : {exc}"]}
    finally:
        if own_axonaut and client is not None:
            await client.aclose()

    # Google Agenda : RDV pris pendant l'appel (direct ou à confirmer par JB)
    if record.state.rdv is not None and not record.metadata.test_mode:
        try:
            agenda = await _sync_agenda(record, analysis, campaign, settings)
            sync["agenda"] = agenda
        except Exception as exc:  # noqa: BLE001
            log.exception("agenda %s en échec", call_id)
            sync.setdefault("erreurs", []).append(f"Google Agenda : {exc}")

    errors = sync.get("erreurs") or []
    synced = "skipped" not in sync and not errors and not getattr(client, "dry_run", False)
    fields: dict[str, Any] = {
        "analysis_status": AnalysisStatus.FAIT if analysis else AnalysisStatus.IGNORE,
        "axonaut_synced": synced,
        "error": "; ".join(errors) if errors else None,
    }
    if analysis:
        fields["analysis_json"] = analysis
        fields["score"] = analysis.score
    db.update_call(call_id, db_path, **fields)
    if analysis and row.get("prospect_id"):
        db.set_prospect_score(int(row["prospect_id"]), analysis.score, db_path)
    log.info("post-appel %s : issue=%s score=%s axonaut=%s", call_id, outcome.value,
             analysis.score if analysis else "-", sync)
    return analysis


async def _sync_agenda(record: CallRecordFile, analysis: CallAnalysis | None, campaign: Campaign,
                       settings: Settings) -> dict:
    from .agenda import GoogleClient, sync_agenda

    if not settings.google_enabled:
        return {"skipped": "Google Agenda non configuré (avp google auth)"}
    async with GoogleClient(settings) as g:
        return await sync_agenda(record, analysis, campaign, google=g, settings=settings)


async def process_pending(
    limit: int = 20,
    *,
    llm_client: Any = None,
    axonaut: AxonautClient | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Traite les appels terminés en attente d'analyse. Retourne {"analyses", "erreurs", "synchro"}."""
    settings = settings or get_settings()
    rows = db.calls_to_analyze(limit, settings.db_path)
    counts = {"analyses": 0, "erreurs": 0, "synchro": 0}
    if not rows:
        return counts
    lazy = _LazyAnthropic(settings) if llm_client is None else None
    try:
        for row in rows:
            try:
                analysis = await process_call(
                    row["id"], llm_client=llm_client or lazy, axonaut=axonaut, settings=settings
                )
            except Exception:  # noqa: BLE001 - un appel en échec ne bloque pas la file
                log.exception("post-appel %s : erreur inattendue", row["id"])
                counts["erreurs"] += 1
                continue
            after = db.get_call(row["id"], settings.db_path) or {}
            if analysis is not None:
                counts["analyses"] += 1
            if after.get("analysis_status") == AnalysisStatus.ERREUR.value or after.get("error"):
                counts["erreurs"] += 1
            if after.get("axonaut_synced"):
                counts["synchro"] += 1
    finally:
        if lazy is not None:
            await lazy.close()
    return counts


_FALLBACK_PROMPT = (
    "Tu es analyste commercial chez OpteoLink. Analyse la transcription d'un appel de prospection "
    "passé par un agent vocal IA et remplis l'outil d'analyse sans rien inventer."
)
