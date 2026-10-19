"""Simulation d'appels en texte : Sarah face à un prospect joué par Claude.

But : tester la compréhension et la conduite de l'appel par lots, sans téléphone (une dizaine de
scénarios en une minute). Le téléphone reste nécessaire pour la voix, la latence et l'AMD.

- **Sarah** : même modèle (`LLM_REALTIME_MODEL`), mêmes consignes (rôle « appel »), même phrase
  d'ouverture, mêmes outils (`enregistrer_opposition`, `terminer_appel`) qu'en appel réel.
  Les schémas d'outils sont recopiés de `agent/agents.py` (qui importe LiveKit) : les garder alignés.
- **Le prospect** : Claude (`LLM_ANALYSIS_MODEL`) suit le scénario écrit dans
  `simulations/<campagne>.yaml` et parle comme au téléphone ; il raccroche en écrivant [RACCROCHE].
- **Après l'appel** : la même analyse post-appel qu'en réel (Sonnet), reportée dans l'état
  d'appel (`apply_analysis`), puis les contrôles automatiques du scénario (`attendu`).

Aucune écriture en base ni dans Axonaut. Rapport JSON dans `data/simulations/`.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .agent.logic import infer_outcome, is_goodbye, realtime_llm_options
from .config import Settings, get_settings
from .models import (
    CallMetadata,
    CallOutcome,
    CallRecordFile,
    CallState,
    Campaign,
    Prospect,
    TranscriptTurn,
)
from .postcall import analyze_call, apply_analysis
from .prompts import CLOSING_LINE, build_instructions, opening_line

HANGUP_MARK = "[RACCROCHE]"
PICKUP_LINE = "Allô ?"

# Recopiés des docstrings de agent/agents.py (AgentAppel) : Claude y lit quand appeler l'outil.
AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "enregistrer_opposition",
        "description": (
            "À appeler IMMÉDIATEMENT si l'interlocuteur demande de ne plus être appelé, de retirer son "
            "numéro, ou se plaint du démarchage. Le numéro est ajouté à la liste d'opposition et l'appel "
            "se termine. Avant d'appeler cet outil, dites dans la même réponse que c'est noté, que vous "
            "ne rappellerez plus, et excusez-vous du dérangement. L'au revoir est dit automatiquement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"motif": {"type": "string", "description": "Ce que la personne a dit, résumé."}},
        },
    },
    {
        "name": "terminer_appel",
        "description": (
            "Termine l'appel. Dans la même réponse, avant l'outil, remerciez en une phrase. Ne dites pas "
            "au revoir : la formule d'au revoir est dite automatiquement, puis l'appel raccroche."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "description": (
                        "L'une de : rdv (le décideur a accepté un créneau et confirmé son email), rappel "
                        "(la personne est à rappeler, ou un rappel est convenu), qualifie (le décideur montre "
                        "de l'intérêt, sans rendez-vous), barrage (le standard refuse de passer la personne, "
                        "sans rappel), refus (le décideur n'est pas intéressé), mauvais_numero (mauvais numéro "
                        "ou établissement inexistant), non_qualifie (établissement hors cible ou aucun besoin)."
                    ),
                },
                "resume": {"type": "string", "description": "Une phrase qui résume l'échange."},
            },
            "required": ["issue"],
        },
    },
]

PROSPECT_SYSTEM = """Tu joues un rôle dans une simulation d'appel téléphonique, pour tester un agent commercial.
Tu reçois un appel de démarchage d'OpteoLink, une entreprise télécom. Tu parles au téléphone, en français.

Ton rôle et le déroulé à suivre :
{persona}

Règles :
- Réponds comme à l'oral : une à trois phrases courtes, sans mise en forme, sans didascalie ni description.
- Suis ton rôle et réagis naturellement à ce que dit l'agent. N'invente pas d'informations contraires au rôle.
- Si on te demande d'épeler, épelle lettre par lettre, séparées par des espaces.
- Quand tu raccroches (conversation finie, ou tu en as assez), écris seulement : {hangup}
- Ne révèle jamais que c'est une simulation."""


class Attendu(BaseModel):
    """Contrôles automatiques d'un scénario (tous optionnels)."""

    issues: list[CallOutcome] = Field(default_factory=list, description="Issues acceptables")
    rdv: bool | None = Field(default=None, description="Un RDV doit (true) ou ne doit pas (false) être relevé")
    optout: bool | None = None
    contacts_min: int = 0
    requis: list[str] = Field(default_factory=list, description="Regex : chacune doit apparaître chez l'agent")
    interdit: list[str] = Field(default_factory=list, description="Regex : aucune ne doit apparaître chez l'agent")


class Scenario(BaseModel):
    id: str
    titre: str
    prospect: str = Field(description="Rôle et déroulé, donnés au Claude qui joue le prospect")
    contact: str = Field(default="", description="Décideur connu de la fiche prospect (vide : inconnu)")
    max_tours: int = 14
    attendu: Attendu = Field(default_factory=Attendu)


class Check(BaseModel):
    nom: str
    ok: bool
    detail: str = ""


class SimulationResult(BaseModel):
    scenario: Scenario
    record: CallRecordFile
    fin: str = ""
    score: str = ""
    resume: str = ""
    checks: list[Check] = Field(default_factory=list)
    erreur: str = ""
    longues: list[str] = Field(default_factory=list, description="Répliques de l'agent de plus de 40 mots")

    @property
    def ok(self) -> bool:
        return not self.erreur and all(c.ok for c in self.checks)


def load_scenarios(campaign_id: str, settings: Settings | None = None) -> list[Scenario]:
    settings = settings or get_settings()
    path = Path(settings.simulations_dir) / f"{campaign_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"pas de scénarios pour la campagne {campaign_id} : {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Scenario.model_validate(s) for s in data.get("scenarios", [])]


def _texts(content: Any) -> str:
    return " ".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text").strip()


def _tool_uses(content: Any) -> list[Any]:
    return [b for b in content if getattr(b, "type", "") == "tool_use"]


def _block_dicts(content: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in content:
        t = getattr(b, "type", "")
        if t == "text" and getattr(b, "text", ""):
            out.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def evaluate(result: SimulationResult) -> list[Check]:
    """Contrôles automatiques à partir de l'état final (après analyse) et des répliques de l'agent."""
    a, st = result.scenario.attendu, result.record.state
    agent = [t.text for t in result.record.transcript if t.role == "agent"]
    checks: list[Check] = []
    outcome = st.outcome
    if a.issues:
        checks.append(Check(nom="issue", ok=outcome in a.issues,
                            detail=f"{outcome.value if outcome else '—'} (attendu : {', '.join(o.value for o in a.issues)})"))
    if a.rdv is not None:
        got = st.rdv is not None
        detail = "aucun" if not got else f"{st.rdv.start.isoformat()} {st.rdv.email or '(sans email)'}" + (
            " à confirmer" if st.rdv.a_confirmer else "")
        checks.append(Check(nom="rdv", ok=got == a.rdv, detail=detail))
    if a.optout is not None:
        checks.append(Check(nom="opposition", ok=st.optout == a.optout, detail=str(st.optout)))
    if a.contacts_min:
        names = ", ".join(c.nom_complet or c.fonction or "?" for c in st.contacts) or "aucun"
        checks.append(Check(nom="contacts", ok=len(st.contacts) >= a.contacts_min, detail=names))
    for rx in a.requis:
        hit = any(re.search(rx, t, re.IGNORECASE) for t in agent)
        checks.append(Check(nom=f"requis /{rx}/", ok=hit))
    for rx in a.interdit:
        bad = next((t for t in agent if re.search(rx, t, re.IGNORECASE)), "")
        checks.append(Check(nom=f"interdit /{rx}/", ok=not bad, detail=bad[:120]))
    return checks


async def run_scenario(
    scenario: Scenario,
    campaign: Campaign,
    *,
    client: Any,
    settings: Settings,
    slots: list[datetime],
    analyse: bool = True,
    now: datetime | None = None,
) -> SimulationResult:
    """Joue un appel complet en texte, puis l'analyse post-appel et les contrôles."""
    now = now or datetime.now(UTC)
    prospect = Prospect(
        id=None, campaign_id=campaign.id, name="EHPAD Les Tilleuls (simulation)", phone="+33562000000",
        city="Auch", contact_name=scenario.contact, contact_role=campaign.cible.interlocuteur if scenario.contact else "",
    )
    instructions = build_instructions(
        "appel", campaign, prospect, now=now, attempt=1, rdv_slots=slots,
        prompts_dir=settings.prompts_dir, timezone=settings.timezone, transfer_available=False,
    )
    opening = opening_line(campaign, prospect, announce_recording=not settings.annonce_enregistrement_par_xivo)
    options = realtime_llm_options(settings)
    extra_body = {**options.get("extra_body", {}), **({"temperature": options["temperature"]} if "temperature" in options else {})}

    transcript: list[TranscriptTurn] = []
    clock = [now]

    def say(role: str, text: str) -> None:
        clock[0] += timedelta(seconds=5)
        transcript.append(TranscriptTurn(role=role, text=text, agent="appel" if role == "agent" else "", ts=clock[0]))

    agent_msgs: list[dict[str, Any]] = [{"role": "user", "content": PICKUP_LINE},
                                        {"role": "assistant", "content": opening}]
    prospect_msgs: list[dict[str, Any]] = [{"role": "user", "content": "(Le téléphone sonne, tu décroches.)"},
                                           {"role": "assistant", "content": PICKUP_LINE},
                                           {"role": "user", "content": opening}]
    say("prospect", PICKUP_LINE)
    say("agent", opening)
    state = CallState(amd_result="human", agent_path=["appel"])
    fin, erreur = "", ""
    prospect_system = PROSPECT_SYSTEM.format(persona=scenario.prospect.strip(), hangup=HANGUP_MARK)

    try:
        for _ in range(scenario.max_tours):
            # --- le prospect répond
            resp = await client.messages.create(
                model=settings.llm_analysis_model, max_tokens=1024, system=prospect_system,
                messages=prospect_msgs, extra_body={"output_config": {"effort": "low"}},
            )
            reply = _texts(resp.content)
            hung_up = HANGUP_MARK in reply or getattr(resp, "stop_reason", "") == "refusal"
            reply = reply.replace(HANGUP_MARK, "").strip()
            if not reply and not hung_up:
                reply = "(silence)"
            if reply:
                say("prospect", reply)
                prospect_msgs.append({"role": "assistant", "content": reply})
                agent_msgs.append({"role": "user", "content": reply})
            if hung_up:
                fin = "le prospect a raccroché"
                break
            # --- Sarah répond (avec un éventuel aller-retour si un outil renvoie une erreur)
            for _attempt in range(2):
                resp = await client.messages.create(
                    model=settings.llm_realtime_model, max_tokens=200,
                    system=[{"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}],
                    tools=AGENT_TOOLS, messages=agent_msgs, extra_body=extra_body or None,
                )
                text, uses = _texts(resp.content), _tool_uses(resp.content)
                agent_msgs.append({"role": "assistant", "content": _block_dicts(resp.content) or text or "…"})
                if text:
                    say("agent", text)
                    prospect_msgs.append({"role": "user", "content": text})
                if not uses:
                    break
                results, retry = [], False
                for u in uses:
                    if u.name == "enregistrer_opposition":
                        state.optout, state.outcome = True, CallOutcome.OPPOSITION
                        state.optout_reason = str((u.input or {}).get("motif", "")).strip()
                        fin = "opposition enregistrée"
                    elif u.name == "terminer_appel":
                        issue = str((u.input or {}).get("issue", "")).strip().lower()
                        try:
                            state.outcome = CallOutcome(issue)
                            fin = f"terminer_appel ({issue})"
                        except ValueError:
                            results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                            "content": f"Issue invalide : {issue}"})
                            retry = True
                            continue
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": "ok"})
                if fin:
                    break
                agent_msgs.append({"role": "user", "content": results})
                if not retry:
                    break
            if fin:
                if not is_goodbye(text):
                    say("agent", CLOSING_LINE)
                break
        else:
            fin = f"limite de {scenario.max_tours} tours atteinte"
    except Exception as exc:  # noqa: BLE001 - une simulation en échec ne doit pas arrêter le lot
        erreur = f"{type(exc).__name__} : {exc}"

    state.outcome = state.outcome or infer_outcome(state, answered=True)
    record = CallRecordFile(
        metadata=CallMetadata(call_id=f"sim-{scenario.id}", campaign_id=campaign.id, prospect=prospect,
                              test_mode=True, rdv_slots=slots),
        state=state, transcript=transcript, started_at=now, answered_at=now, ended_at=clock[0],
    )
    result = SimulationResult(scenario=scenario, record=record, fin=fin, erreur=erreur)
    if analyse and not erreur:
        try:
            analysis = await analyze_call(record, campaign, client=client, settings=settings)
            apply_analysis(record.state, analysis, campaign=campaign, slots=slots, timezone=settings.timezone)
            result.score, result.resume = f"{analysis.score} {analysis.score_num}", analysis.resume
        except Exception as exc:  # noqa: BLE001
            result.erreur = f"analyse : {exc}"
    result.longues = [t.text for t in transcript if t.role == "agent" and len(t.text.split()) > 40]
    result.checks = evaluate(result)
    return result


async def run_simulations(
    scenarios: list[Scenario],
    campaign: Campaign,
    *,
    client: Any,
    settings: Settings,
    slots: list[datetime],
    analyse: bool = True,
    parallel: int = 3,
) -> list[SimulationResult]:
    sem = asyncio.Semaphore(max(1, parallel))

    async def one(s: Scenario) -> SimulationResult:
        async with sem:
            return await run_scenario(s, campaign, client=client, settings=settings, slots=slots, analyse=analyse)

    return list(await asyncio.gather(*(one(s) for s in scenarios)))


def format_result(r: SimulationResult) -> str:
    head = "✅" if r.ok else "❌"
    lines = [f"{head} {r.scenario.id} — {r.scenario.titre}",
             f"   fin : {r.fin or '—'} ; issue : {r.record.state.outcome.value if r.record.state.outcome else '—'}"
             + (f" ; score : {r.score}" if r.score else "")]
    if r.erreur:
        lines.append(f"   ERREUR : {r.erreur}")
    for c in r.checks:
        lines.append(f"   {'ok ' if c.ok else 'KO '} {c.nom}{f' : {c.detail}' if c.detail else ''}")
    for t in r.longues:
        lines.append(f"   ⚠ réplique longue ({len(t.split())} mots) : {t[:100]}…")
    lines.append("   --- transcription")
    for t in r.record.transcript:
        who = "Sarah   " if t.role == "agent" else "Prospect"
        lines.append(f"   {who} : {t.text}")
    if r.resume:
        lines.append(f"   --- analyse : {r.resume}")
    return "\n".join(lines)


def save_report(results: list[SimulationResult], settings: Settings) -> Path:
    out_dir = Path(settings.data_dir) / "simulations"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    payload = [json.loads(r.model_dump_json()) | {"ok": r.ok} for r in results]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
