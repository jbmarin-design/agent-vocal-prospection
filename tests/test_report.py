from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from avp import db
from avp.config import Settings
from avp.models import Prospect
from avp.orchestrator import report as rep
from avp.orchestrator.report import (
    CallRow,
    build_weekly_report,
    estimate_call_cost,
    markdown_to_html,
    parse_week,
    previous_week,
)

PARIS = ZoneInfo("Europe/Paris")


def _call(cid: str, *, outcome: str | None, score: str | None = None, prompt: str = "v1", dm: bool = False,
          duration: float = 0, analysis: dict[str, Any] | None = None, usage: dict[str, Any] | None = None,
          test: bool = False, campaign: str = "test-ehpad", prospect_id: int | None = None,
          created: datetime | None = None) -> None:
    db.create_call(cid, campaign, "+33562000001", f"avp-{cid}", prospect_id, test_mode=test)
    fields: dict[str, Any] = {"status": "termine", "prompt_version": prompt, "duration_s": duration,
                              "state_json": {"decision_maker_reached": dm}}
    if outcome:
        fields["outcome"] = outcome
    if score:
        fields["score"] = score
    if analysis is not None:
        fields["analysis_json"] = analysis
        fields["analysis_status"] = "fait"
    if usage is not None:
        fields["usage_json"] = usage
    db.update_call(cid, **fields)
    if created:
        with db.connect() as c:
            c.execute("UPDATE calls SET created_at=? WHERE id=?", (created.astimezone(UTC).isoformat(), cid))


def test_parse_week() -> None:
    start, end = parse_week("2026-W41")
    assert start == datetime(2026, 10, 5, tzinfo=PARIS) and end - start == timedelta(days=7)
    assert parse_week("2026-S41") == (start, end)
    with pytest.raises(ValueError):
        parse_week("41-2026")
    s, e = previous_week(datetime(2026, 10, 7, 15, 0, tzinfo=PARIS))
    assert s == datetime(2026, 9, 28, tzinfo=PARIS) and e == datetime(2026, 10, 5, tzinfo=PARIS)


def test_cost_estimation() -> None:
    row = CallRow({"outcome": "refus", "duration_s": 120, "analysis_status": "en_attente"}, {}, {}, {})
    fallback = estimate_call_cost(row)
    assert 0.01 < fallback < 0.2
    nr = CallRow({"outcome": "non_decroche", "duration_s": 60}, {}, {}, {})
    assert estimate_call_cost(nr) == pytest.approx(0.0077)
    usage = {"model_usage": [
        {"type": "llm_usage", "provider": "anthropic", "model": "haiku", "input_tokens": 1_000_000, "output_tokens": 0},
        {"type": "stt_usage", "provider": "deepgram", "model": "nova-3", "audio_duration": 60.0},
        {"type": "tts_usage", "provider": "cartesia", "model": "sonic", "characters_count": 1000},
    ]}
    r2 = CallRow({"outcome": "refus", "duration_s": 999}, {}, {}, usage)
    assert estimate_call_cost(r2) == pytest.approx(1.0 + 0.0077 + 0.04)
    flat = CallRow({"outcome": "refus"}, {}, {}, {"llm_prompt_tokens": 0, "llm_completion_tokens": 200_000,
                                                  "tts_characters_count": 0, "stt_audio_duration": 0})
    assert estimate_call_cost(flat) == pytest.approx(1.0)


def test_weekly_report_without_ai(db_ready: Settings) -> None:
    pid, _ = db.upsert_prospect(Prospect(campaign_id="test-ehpad", name="EHPAD Les Tilleuls", phone="+33562000001",
                                         city="Auch"))
    t = datetime(2026, 10, 6, 10, 0, tzinfo=PARIS)
    an_hot = {"score": "chaud", "score_num": 85, "resume": "Intéressée par l'antifugue", "interlocuteur": "Mme Martin",
              "fonction": "Directrice", "objections": ["Pas le temps", "Déjà un prestataire"],
              "prochaine_action": "Préparer le RDV", "rdv_confirme": True, "qualite_appel": 4,
              "suggestions_script": ["Raccourcir l'accroche"], "date_relance": "2026-10-12T10:00:00+02:00"}
    an_cold = {"score": "froid", "score_num": 10, "resume": "Pas intéressé", "objections": ["pas le temps."],
               "qualite_appel": 3, "suggestions_script": ["raccourcir l'accroche"]}
    _call("c1", outcome="rdv", score="chaud", dm=True, duration=240, analysis=an_hot, prospect_id=pid, created=t)
    _call("c2", outcome="refus", score="froid", prompt="v2", dm=True, duration=60, analysis=an_cold, created=t)
    _call("c3", outcome="non_decroche", created=t)
    _call("c4", outcome="repondeur", duration=20, created=t)
    _call("c5", outcome="rdv", score="chaud", test=True, created=t)  # test : exclu
    _call("c6", outcome="rdv", created=t - timedelta(days=10))  # hors période

    start, end = parse_week("2026-W41")
    path = build_weekly_report(start, end, settings=db_ready, with_ai=False)
    assert path.name == "rapport-2026-S41.md"
    md = path.read_text(encoding="utf-8")
    assert "Rapport hebdomadaire 2026-S41" in md
    assert "| Total | 4 | 75 % | 50 % | 100 % | 1 | 1 |" in md
    assert "test-ehpad / v1" in md and "test-ehpad / v2" in md
    assert "Pas le temps (×2)" in md
    assert "Raccourcir l'accroche (×2)" in md
    assert "EHPAD Les Tilleuls" in md and "Préparer le RDV" in md and "Mme Martin — Directrice" in md
    assert "Appels de test exclus : 1" in md
    assert "synthèse Claude" not in md
    html = path.with_suffix(".html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "<table>" in html and "<style>" in html
    assert "EHPAD Les Tilleuls" in html


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessages:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return type("R", (), {"content": [_FakeBlock("### Constats\n- Remplacer l'accroche par « … »")]})()


class _FakeClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


def test_weekly_report_with_injected_ai(db_ready: Settings) -> None:
    (db_ready.campaigns_dir / "test-ehpad.yaml").write_text("id: test-ehpad\naccroche: Bonjour\n", encoding="utf-8")
    _call("c1", outcome="refus", score="froid", analysis={"score": "froid", "score_num": 5, "resume": "non",
                                                          "objections": ["trop cher"]})
    now = db.utcnow()
    client = _FakeClient()
    path = build_weekly_report(now - timedelta(days=1), now + timedelta(days=1), "test-ehpad", settings=db_ready,
                               llm_client=client)
    assert path.name.endswith("-test-ehpad.md")
    md = path.read_text(encoding="utf-8")
    assert "Recommandations (synthèse Claude)" in md and "Remplacer l'accroche" in md
    assert client.messages.kwargs["model"] == db_ready.llm_analysis_model
    prompt = client.messages.kwargs["messages"][0]["content"]
    assert "trop cher" in prompt and "accroche: Bonjour" in prompt


def test_weekly_report_ai_failure_is_not_fatal(db_ready: Settings) -> None:
    _call("c1", outcome="refus")

    class Boom:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw: Any) -> Any:
                raise RuntimeError("quota dépassé")

    now = db.utcnow()
    path = build_weekly_report(now - timedelta(days=1), now + timedelta(days=1), settings=db_ready, llm_client=Boom())
    assert "Synthèse IA indisponible : quota dépassé" in path.read_text(encoding="utf-8")


def test_markdown_to_html_basics() -> None:
    h = markdown_to_html("# T\n\nTexte **gras** et `code` <b>\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 \\| 3 |\n")
    assert "<h1>T</h1>" in h and "<strong>gras</strong>" in h and "<code>code</code>" in h
    assert "&lt;b&gt;" in h and "<ul><li>a</li><li>b</li></ul>" in h and "<td>2 | 3</td>" in h


def test_usage_totals_ignores_garbage() -> None:
    assert rep._usage_totals({}) is None
    assert rep._usage_totals({"foo": 1}) is None
    assert rep._usage_totals(json.loads('{"model_usage": [1, "x"]}')) is None
