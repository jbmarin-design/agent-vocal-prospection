"""Rapport hebdomadaire : statistiques d'appels, comparaison des versions de prompts,
objections, prospects chauds et recommandations de Claude.

Sorties : `data/reports/rapport-<AAAA>-S<ww>.md` et `.html` (HTML autonome, CSS inline).

Estimation des coûts
--------------------
Les tarifs ci-dessous sont des **ordres de grandeur en dollars US** (octobre 2026), à ajuster
dans `COST_RATES` selon vos contrats. Le calcul utilise `calls.usage_json` (métriques
LiveKit écrites par le worker) quand il est disponible, sinon une estimation par minute
d'appel (`FALLBACK_*`). L'analyse post-appel (Sonnet) est estimée par appel analysé.
"""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import logging
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import Settings, get_settings
from ..models import CallOutcome
from ..phone import to_national_fr

log = logging.getLogger("avp.rapport")

# ---------------------------------------------------------------------------
# Tarifs (USD) — paramétrables
# ---------------------------------------------------------------------------

COST_RATES: dict[str, float] = {
    "stt_usd_per_min": 0.0077,  # Deepgram nova-3 streaming
    "llm_realtime_in_usd_per_mtok": 1.0,  # Claude Haiku 4.5
    "llm_realtime_out_usd_per_mtok": 5.0,
    "llm_analysis_in_usd_per_mtok": 2.0,  # Claude Sonnet 5.5 (post-appel, rapport)
    "llm_analysis_out_usd_per_mtok": 10.0,
    "tts_cartesia_usd_per_1k_chars": 0.04,  # Cartesia sonic (ordre de grandeur)
    "tts_elevenlabs_usd_per_1k_chars": 0.06,  # ElevenLabs flash (ordre de grandeur)
}
# Estimations par minute de conversation quand usage_json est absent.
FALLBACK_LLM_IN_TOK_PER_MIN = 12_000  # le contexte complet est renvoyé à chaque tour
FALLBACK_LLM_OUT_TOK_PER_MIN = 250
FALLBACK_TTS_CHARS_PER_MIN = 600
# Analyse post-appel estimée par appel analysé.
ANALYSIS_IN_TOK_PER_CALL = 6_000
ANALYSIS_OUT_TOK_PER_CALL = 800

ANSWERED_EXCLUDED = {CallOutcome.NON_DECROCHE, CallOutcome.OCCUPE, CallOutcome.MAUVAIS_NUMERO, CallOutcome.ERREUR}

OUTCOME_LABELS: dict[str, str] = {
    "non_decroche": "Non décroché", "occupe": "Occupé", "mauvais_numero": "Mauvais numéro",
    "repondeur": "Répondeur", "svi": "Serveur vocal", "barrage": "Barrage standard",
    "rappel": "Rappel convenu", "refus": "Refus", "opposition": "Opposition",
    "non_qualifie": "Non qualifié", "qualifie": "Qualifié (sans RDV)", "rdv": "RDV",
    "transfert": "Transfert", "erreur": "Erreur technique", "": "Inconnue",
}

# ---------------------------------------------------------------------------
# Semaines
# ---------------------------------------------------------------------------


def week_label(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-S{w:02d}"


def parse_week(spec: str, timezone: str = "Europe/Paris") -> tuple[datetime, datetime]:
    """« 2026-W41 » ou « 2026-S41 » → (lundi 00:00, lundi suivant 00:00) dans le fuseau local."""
    m = re.fullmatch(r"(\d{4})-?[WwSs](\d{1,2})", spec.strip())
    if not m:
        raise ValueError(f"semaine invalide {spec!r} (format attendu AAAA-Www, ex. 2026-W41)")
    year, week = int(m.group(1)), int(m.group(2))
    tz = ZoneInfo(timezone)
    monday = datetime.fromisocalendar(year, week, 1).replace(tzinfo=tz)
    return monday, monday + timedelta(days=7)


def previous_week(now: datetime | None = None, timezone: str = "Europe/Paris") -> tuple[datetime, datetime]:
    """Semaine ISO précédente (lundi → lundi), fuseau local."""
    tz = ZoneInfo(timezone)
    now = (now or db.utcnow()).astimezone(tz)
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return this_monday - timedelta(days=7), this_monday


# ---------------------------------------------------------------------------
# Lecture des appels
# ---------------------------------------------------------------------------


def _loads(s: str | None) -> dict[str, Any]:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


@dataclass
class CallRow:
    raw: dict[str, Any]
    state: dict[str, Any]
    analysis: dict[str, Any]
    usage: dict[str, Any]

    @property
    def outcome(self) -> CallOutcome | None:
        try:
            return CallOutcome(self.raw.get("outcome") or "")
        except ValueError:
            return None

    @property
    def answered(self) -> bool:
        o = self.outcome
        return o is not None and o not in ANSWERED_EXCLUDED

    @property
    def human(self) -> bool:
        o = self.outcome
        return o is not None and o.reached_human

    @property
    def decision_maker(self) -> bool:
        return bool(self.state.get("decision_maker_reached"))

    @property
    def score(self) -> str:
        return str(self.raw.get("score") or self.analysis.get("score") or "")

    @property
    def duration_s(self) -> float:
        try:
            return float(self.raw.get("duration_s") or 0.0)
        except (TypeError, ValueError):
            return 0.0


def _load_rows(start: datetime, end: datetime, campaign_id: str | None) -> list[CallRow]:
    rows = db.calls_between(start, end, campaign_id)
    out = []
    for r in rows:
        out.append(CallRow(r, _loads(r.get("state_json")), _loads(r.get("analysis_json")), _loads(r.get("usage_json"))))
    return out


# ---------------------------------------------------------------------------
# Coûts
# ---------------------------------------------------------------------------


def _usage_totals(usage: dict[str, Any]) -> dict[str, float] | None:
    """Extrait (tokens LLM entrée/sortie, caractères TTS, secondes STT) d'un usage_json.

    Formats acceptés : `UsageSummary` LiveKit à plat (llm_prompt_tokens, llm_completion_tokens,
    tts_characters_count, stt_audio_duration), liste `model_usage` (LLMModelUsage/TTSModelUsage/
    STTModelUsage sérialisés), ou clés simples (llm_input_tokens, llm_output_tokens,
    tts_characters, stt_seconds). Retourne None si rien d'exploitable.
    """
    if not usage:
        return None
    t = {"llm_in": 0.0, "llm_out": 0.0, "tts_chars": 0.0, "stt_s": 0.0}
    found = False
    items = usage.get("model_usage")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            typ = it.get("type")
            if typ == "llm_usage":
                t["llm_in"] += float(it.get("input_tokens") or 0)
                t["llm_out"] += float(it.get("output_tokens") or 0)
                found = True
            elif typ == "tts_usage":
                t["tts_chars"] += float(it.get("characters_count") or 0)
                found = True
            elif typ == "stt_usage":
                t["stt_s"] += float(it.get("audio_duration") or 0)
                found = True
    aliases = {
        "llm_in": ("llm_prompt_tokens", "llm_input_tokens"),
        "llm_out": ("llm_completion_tokens", "llm_output_tokens"),
        "tts_chars": ("tts_characters_count", "tts_characters"),
        "stt_s": ("stt_audio_duration", "stt_seconds"),
    }
    if not found:
        for key, names in aliases.items():
            for n in names:
                if usage.get(n) is not None:
                    try:
                        t[key] = float(usage[n])
                        found = True
                    except (TypeError, ValueError):
                        pass
                    break
    return t if found else None


def estimate_call_cost(row: CallRow, tts_provider: str = "cartesia") -> float:
    """Coût estimé (USD) d'un appel : temps réel (STT + LLM + TTS) + analyse post-appel."""
    r = COST_RATES
    tts_rate = r.get(f"tts_{tts_provider}_usd_per_1k_chars", r["tts_cartesia_usd_per_1k_chars"])
    totals = _usage_totals(row.usage)
    minutes = row.duration_s / 60.0
    if totals is None:
        # STT : tout le temps d'appel (sonnerie comprise une fois la room ouverte) ; LLM/TTS si décroché.
        conv_min = minutes if row.answered else 0.0
        totals = {
            "stt_s": row.duration_s,
            "llm_in": conv_min * FALLBACK_LLM_IN_TOK_PER_MIN,
            "llm_out": conv_min * FALLBACK_LLM_OUT_TOK_PER_MIN,
            "tts_chars": conv_min * FALLBACK_TTS_CHARS_PER_MIN,
        }
    cost = (
        totals["stt_s"] / 60.0 * r["stt_usd_per_min"]
        + totals["llm_in"] / 1e6 * r["llm_realtime_in_usd_per_mtok"]
        + totals["llm_out"] / 1e6 * r["llm_realtime_out_usd_per_mtok"]
        + totals["tts_chars"] / 1000.0 * tts_rate
    )
    if row.raw.get("analysis_status") == "fait":
        cost += (
            ANALYSIS_IN_TOK_PER_CALL / 1e6 * r["llm_analysis_in_usd_per_mtok"]
            + ANALYSIS_OUT_TOK_PER_CALL / 1e6 * r["llm_analysis_out_usd_per_mtok"]
        )
    return cost


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    appels: int = 0
    decroches: int = 0
    humains: int = 0
    decideurs: int = 0
    rdv: int = 0
    chauds: int = 0
    issues: Counter[str] = field(default_factory=Counter)
    scores: Counter[str] = field(default_factory=Counter)
    duree_totale_s: float = 0.0
    duree_n: int = 0
    qualite_somme: float = 0.0
    qualite_n: int = 0
    score_num_somme: float = 0.0
    score_num_n: int = 0
    cout_usd: float = 0.0

    def add(self, row: CallRow, tts_provider: str) -> None:
        self.appels += 1
        o = row.outcome
        self.issues[o.value if o else ""] += 1
        if row.answered:
            self.decroches += 1
            if row.duration_s:
                self.duree_totale_s += row.duration_s
                self.duree_n += 1
        if row.human:
            self.humains += 1
        if row.decision_maker:
            self.decideurs += 1
        if o is CallOutcome.RDV or row.analysis.get("rdv_confirme"):
            self.rdv += 1
        sc = row.score
        if sc:
            self.scores[sc] += 1
            if sc == "chaud":
                self.chauds += 1
        if isinstance(row.analysis.get("qualite_appel"), int | float):
            self.qualite_somme += float(row.analysis["qualite_appel"])
            self.qualite_n += 1
        if isinstance(row.analysis.get("score_num"), int | float):
            self.score_num_somme += float(row.analysis["score_num"])
            self.score_num_n += 1
        self.cout_usd += estimate_call_cost(row, tts_provider)

    @staticmethod
    def pct(n: int, d: int) -> str:
        return f"{100.0 * n / d:.0f} %" if d else "—"

    @property
    def duree_moyenne(self) -> str:
        if not self.duree_n:
            return "—"
        s = int(round(self.duree_totale_s / self.duree_n))
        return f"{s // 60} min {s % 60:02d} s"

    @property
    def qualite_moyenne(self) -> str:
        return f"{self.qualite_somme / self.qualite_n:.1f}/5" if self.qualite_n else "—"

    @property
    def score_moyen(self) -> str:
        return f"{self.score_num_somme / self.score_num_n:.0f}/100" if self.score_num_n else "—"

    @property
    def cout_par_appel(self) -> str:
        return f"{self.cout_usd / self.appels:.3f} $" if self.appels else "—"


def _norm_text(s: str) -> str:
    return " ".join(str(s).strip().rstrip(".").split()).lower()


def _top(items: Iterable[str], n: int) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for it in items:
        if not it or not str(it).strip():
            continue
        key = _norm_text(it)
        counter[key] += 1
        display.setdefault(key, str(it).strip())
    return [(display[k], c) for k, c in counter.most_common(n)]


@dataclass
class ReportData:
    start: datetime
    end: datetime
    campaign_id: str | None
    total: Stats
    by_campaign: dict[str, Stats]
    by_prompt: dict[tuple[str, str], Stats]
    objections: list[tuple[str, int]]
    suggestions: list[tuple[str, int]]
    hot: list[dict[str, Any]]
    tests_exclus: int
    ai_text: str = ""


def collect(start: datetime, end: datetime, campaign_id: str | None, settings: Settings) -> ReportData:
    rows = _load_rows(start, end, campaign_id)
    tests = [r for r in rows if r.raw.get("test_mode")]
    rows = [r for r in rows if not r.raw.get("test_mode")]
    total = Stats()
    by_campaign: dict[str, Stats] = defaultdict(Stats)
    by_prompt: dict[tuple[str, str], Stats] = defaultdict(Stats)
    objections: list[str] = []
    suggestions: list[str] = []
    hot: list[dict[str, Any]] = []
    prospect_cache: dict[int, dict[str, Any] | None] = {}
    for r in rows:
        tp = settings.tts_provider
        total.add(r, tp)
        by_campaign[r.raw["campaign_id"]].add(r, tp)
        by_prompt[(r.raw["campaign_id"], r.raw.get("prompt_version") or "inconnue")].add(r, tp)
        objections += [str(x) for x in r.analysis.get("objections") or []]
        suggestions += [str(x) for x in r.analysis.get("suggestions_script") or []]
        if r.score == "chaud" or r.outcome in (CallOutcome.RDV, CallOutcome.TRANSFERT):
            pid = r.raw.get("prospect_id")
            if pid is not None and pid not in prospect_cache:
                prospect_cache[pid] = db.get_prospect_row(pid)
            p = prospect_cache.get(pid) if pid is not None else None
            hot.append({
                "call_id": r.raw["id"],
                "campagne": r.raw["campaign_id"],
                "nom": (p or {}).get("name") or r.raw.get("phone"),
                "ville": (p or {}).get("city") or "",
                "telephone": to_national_fr(r.raw.get("phone") or ""),
                "interlocuteur": " — ".join(
                    x for x in (r.analysis.get("interlocuteur") or r.state.get("contact_name") or "",
                                r.analysis.get("fonction") or r.state.get("contact_role") or "") if x
                ),
                "issue": OUTCOME_LABELS.get(r.raw.get("outcome") or "", r.raw.get("outcome") or ""),
                "score": r.score or "—",
                "prochaine_action": r.analysis.get("prochaine_action") or "",
                "date_relance": (r.analysis.get("date_relance") or "")[:16].replace("T", " "),
                "resume": r.analysis.get("resume") or "",
            })
    return ReportData(
        start=start, end=end, campaign_id=campaign_id, total=total,
        by_campaign=dict(sorted(by_campaign.items())), by_prompt=dict(sorted(by_prompt.items())),
        objections=_top(objections, 10), suggestions=_top(suggestions, 15), hot=hot, tests_exclus=len(tests),
    )


# ---------------------------------------------------------------------------
# Synthèse IA
# ---------------------------------------------------------------------------

AI_SYSTEM = (
    "Tu es un coach commercial expert en prospection téléphonique B2B et en conception de scripts "
    "pour agents vocaux IA. Tu analyses le bilan hebdomadaire d'un agent vocal d'OpteoLink "
    "(intégrateur télécom : XiVO/Asterisk, interconnexion appel malade/antifugue en EHPAD, firewall/VPN, "
    "téléphonie et internet pour cabinets médicaux et mairies). Tu réponds en français, en Markdown."
)

AI_INSTRUCTIONS = """À partir des données ci-dessous, rédige des **recommandations concrètes et actionnables** :

1. **Constats clés** (3 à 5 puces, chiffrés).
2. **Modifications de prompts proposées** : pour chacune, indique le fichier visé
   (`prompts/accueil.md`, `prompts/decideur.md`, `prompts/repondeur.md` ou `campaigns/<id>.yaml`, champ précis :
   `accroche`, `objections`, `qualification`…), la formulation actuelle si elle est connue, et la
   **formulation proposée mot pour mot**, avec la raison.
3. **Réponses aux objections fréquentes** : une réponse courte proposée pour chaque objection du top.
4. **Réglages de campagne** (plages horaires, nombre de tentatives, délais) si les chiffres le justifient.
5. **Versions de prompts** : si plusieurs versions coexistent, laquelle garder et pourquoi
   (attention aux petits échantillons : signale quand l'écart n'est pas significatif).

Contraintes impératives : ne jamais proposer de supprimer l'annonce IA + enregistrement, de donner des prix,
de faire des promesses fermes, ni d'insister après un refus ou une demande d'opposition. Reste concis
(800 mots maximum)."""


def _call_ai(llm_client: Any, model: str, system: str, user: str) -> str:
    def _create() -> Any:
        return llm_client.messages.create(
            model=model, max_tokens=4000, system=system, messages=[{"role": "user", "content": user}]
        )

    result = _create()
    if inspect.isawaitable(result):  # client asynchrone : exécuté dans un fil dédié
        box: dict[str, Any] = {}

        def runner() -> None:
            try:
                box["v"] = asyncio.run(_await(result))
            except BaseException as e:  # transmis au fil appelant
                box["e"] = e

        th = threading.Thread(target=runner)
        th.start()
        th.join()
        if "e" in box:
            raise box["e"]
        result = box["v"]
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


async def _await(aw: Any) -> Any:
    return await aw


def _campaign_context(data: ReportData, settings: Settings) -> str:
    """Extraits des YAML de campagne et des prompts de rôle, pour que Claude cite les formulations actuelles."""
    chunks: list[str] = []
    for cid in data.by_campaign:
        p = settings.campaigns_dir / f"{cid}.yaml"
        if p.exists():
            chunks.append(f"### campaigns/{cid}.yaml\n```yaml\n{p.read_text(encoding='utf-8')[:6000]}\n```")
    for name in ("accueil.md", "decideur.md", "repondeur.md"):
        p = settings.prompts_dir / name
        if p.exists():
            chunks.append(f"### prompts/{name}\n```markdown\n{p.read_text(encoding='utf-8')[:5000]}\n```")
    return "\n\n".join(chunks)


def _ai_payload(data: ReportData, md_stats: str, settings: Settings) -> str:
    resumes = [
        f"- [{r['campagne']}] {r['nom']} : {r['issue']}, score {r['score']} — {r['resume']}" for r in data.hot[:15]
    ]
    return "\n\n".join(x for x in (
        AI_INSTRUCTIONS,
        "## Statistiques de la semaine\n" + md_stats,
        "## Prospects chauds (résumés)\n" + ("\n".join(resumes) or "aucun"),
        "## Configuration actuelle\n" + _campaign_context(data, settings),
    ) if x)


# ---------------------------------------------------------------------------
# Rendu Markdown
# ---------------------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(x: str) -> str:
        return str(x).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _stats_row(label: str, s: Stats) -> list[str]:
    return [
        label, str(s.appels), Stats.pct(s.decroches, s.appels), Stats.pct(s.humains, s.appels),
        Stats.pct(s.decideurs, s.humains), str(s.rdv), str(s.chauds), s.score_moyen, s.qualite_moyenne,
        s.duree_moyenne, f"{s.cout_usd:.2f} $",
    ]


STATS_HEADERS = [
    "", "Appels", "Décroché", "Humain joint", "Décideur (sur humains)", "RDV", "Chauds",
    "Score moyen", "Qualité agent", "Durée moy. (décrochés)", "Coût estimé",
]


def stats_markdown(data: ReportData) -> str:
    t = data.total
    parts = [
        "### Vue d'ensemble",
        _md_table(STATS_HEADERS, [_stats_row("Total", t)]),
        f"Coût moyen par appel : {t.cout_par_appel} (estimation, voir bas de page)."
        + (f" Appels de test exclus : {data.tests_exclus}." if data.tests_exclus else ""),
        "### Issues",
        _md_table(
            ["Issue", "Nombre", "Part"],
            [[OUTCOME_LABELS.get(k, k), str(v), Stats.pct(v, t.appels)] for k, v in t.issues.most_common()],
        ) if t.issues else "_Aucun appel._",
        "### Scores",
        _md_table(
            ["Score", "Nombre"], [[k, str(t.scores.get(k, 0))] for k in ("chaud", "tiede", "froid")]
        ),
        "### Par campagne",
        _md_table(STATS_HEADERS, [_stats_row(cid, s) for cid, s in data.by_campaign.items()])
        if data.by_campaign else "_Aucune campagne._",
        "### Par version de prompt",
        _md_table(
            ["Campagne / version"] + STATS_HEADERS[1:],
            [_stats_row(f"{cid} / {ver}", s) for (cid, ver), s in data.by_prompt.items()],
        ) if data.by_prompt else "_Aucune donnée._",
        "### Objections les plus fréquentes",
        "\n".join(f"{i}. {txt} (×{n})" for i, (txt, n) in enumerate(data.objections, 1)) or "_Aucune._",
        "### Suggestions de script (analyse post-appel)",
        "\n".join(f"- {txt} (×{n})" for txt, n in data.suggestions) or "_Aucune._",
    ]
    return "\n\n".join(parts)


def render_markdown(data: ReportData, generated_at: datetime, timezone: str) -> str:
    tz = ZoneInfo(timezone)
    s_local, e_local = data.start.astimezone(tz), (data.end - timedelta(seconds=1)).astimezone(tz)
    title = f"Rapport hebdomadaire {week_label(s_local)}"
    head = [
        f"# {title}",
        f"Période : du {s_local:%d/%m/%Y} au {e_local:%d/%m/%Y}"
        + (f" — campagne `{data.campaign_id}`" if data.campaign_id else " — toutes campagnes")
        + f". Généré le {generated_at.astimezone(tz):%d/%m/%Y à %H:%M}.",
        "## Statistiques",
        stats_markdown(data),
        "## Prospects chauds et prochaines actions",
        _md_table(
            ["Établissement", "Ville", "Téléphone", "Interlocuteur", "Issue", "Score", "Prochaine action",
             "Relance", "Appel"],
            [[h["nom"], h["ville"], h["telephone"], h["interlocuteur"], h["issue"], h["score"],
              h["prochaine_action"], h["date_relance"], h["call_id"]] for h in data.hot],
        ) if data.hot else "_Aucun prospect chaud cette semaine._",
    ]
    if data.ai_text:
        head += ["## Recommandations (synthèse Claude)", data.ai_text]
    head += [
        "---",
        "_Définitions : « décroché » = toute issue sauf non décroché, occupé, mauvais numéro, erreur ; "
        "« humain joint » = une personne a répondu (hors répondeur/SVI) ; « décideur » = rapporté au nombre "
        "d'humains joints. Coûts : estimation en dollars US (Deepgram, Claude Haiku temps réel, TTS, analyse "
        "Sonnet), d'après les métriques d'usage quand disponibles, sinon forfait par minute — hors "
        "opérateur télécom et hébergement. Tarifs paramétrables dans `src/avp/orchestrator/report.py`._",
    ]
    return "\n\n".join(head) + "\n"


# ---------------------------------------------------------------------------
# Rendu HTML (Markdown minimal → HTML autonome)
# ---------------------------------------------------------------------------

CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:1100px;margin:0 auto;
padding:24px 16px;color:#1f2933;background:#fff;line-height:1.5}
h1{font-size:1.6em;border-bottom:3px solid #0b5394;padding-bottom:6px}
h2{font-size:1.3em;margin-top:2em;color:#0b5394;border-bottom:1px solid #d9e2ec;padding-bottom:4px}
h3{font-size:1.05em;margin-top:1.5em}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.9em;margin:.5em 0}
th,td{border:1px solid #d9e2ec;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f0f4f8}tr:nth-child(even) td{background:#fafcfe}
code{background:#f0f4f8;padding:1px 4px;border-radius:3px;font-size:.9em}
pre{background:#f0f4f8;padding:10px;overflow-x:auto}
em{color:#52606d}hr{border:none;border-top:1px solid #d9e2ec;margin:2em 0}
@media print{body{max-width:none}}
"""


def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])[_*]([^_*]+)[_*](?![\w*])", r"<em>\1</em>", t)
    return t


def _split_row(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def markdown_to_html(md: str) -> str:
    """Convertisseur Markdown minimal (titres, paragraphes, listes, tableaux, code, gras/italique)."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if stripped == "---":
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{3,}", lines[i + 1].strip()):
            headers = _split_row(stripped)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(_split_row(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(h)}</th>" for h in headers)
            trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
            out.append(f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>')
            continue
        if re.match(r"^([-*]|\d+\.)\s+", stripped):
            ordered = bool(re.match(r"^\d+\.", stripped))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i]))
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        buf = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||```|---|[-*]\s|\d+\.\s)", lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")
    return "\n".join(out)


def render_html(md: str, title: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"fr\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>\n"
        + markdown_to_html(md)
        + "\n</body></html>\n"
    )


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def build_weekly_report(
    start: datetime,
    end: datetime,
    campaign_id: str | None = None,
    *,
    settings: Settings | None = None,
    llm_client: Any = None,
    with_ai: bool = True,
) -> Path:
    """Construit le rapport de la période [start, end) et retourne le chemin du fichier Markdown.

    Le fichier HTML est écrit à côté (même nom, extension `.html`). Si la synthèse IA échoue,
    le rapport est quand même produit, avec un avertissement à la place des recommandations.
    """
    s = settings or get_settings()
    s.ensure_dirs()
    tz = ZoneInfo(s.timezone)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    data = collect(start, end, campaign_id, s)

    if with_ai and data.total.appels:
        try:
            client = llm_client
            if client is None:
                import anthropic  # import paresseux : paquet absent des tests

                client = anthropic.Anthropic(api_key=s.anthropic_api_key or None)
            data.ai_text = _call_ai(client, s.llm_analysis_model, AI_SYSTEM, _ai_payload(data, stats_markdown(data), s))
        except Exception as e:
            log.error("Synthèse IA impossible : %s", e)
            data.ai_text = f"_Synthèse IA indisponible : {e}_"
    elif with_ai:
        data.ai_text = "_Aucun appel sur la période : pas de synthèse IA._"

    md = render_markdown(data, db.utcnow(), s.timezone)
    label = week_label(start.astimezone(tz))
    suffix = f"-{campaign_id}" if campaign_id else ""
    md_path = s.reports_dir / f"rapport-{label}{suffix}.md"
    md_path.write_text(md, encoding="utf-8")
    md_path.with_suffix(".html").write_text(render_html(md, f"Rapport {label}{suffix}"), encoding="utf-8")
    log.info("Rapport écrit : %s (+ .html)", md_path)
    return md_path
