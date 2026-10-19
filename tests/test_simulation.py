"""Tests de la simulation d'appels en texte (clients factices, aucun appel réseau)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from avp.campaigns import load_campaign
from avp.config import PROJECT_ROOT, Settings
from avp.models import CallOutcome
from avp.postcall import ANALYSIS_TOOL_NAME
from avp.prompts import CLOSING_LINE
from avp.simulation import Attendu, Scenario, load_scenarios, run_scenario

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
SLOT = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def text(t: str) -> Any:
    return SimpleNamespace(type="text", text=t)


def tool(name: str, **inp: Any) -> Any:
    return SimpleNamespace(type="tool_use", id=f"toolu_{name}", name=name, input=inp)


class FakeMessages:
    """Répond selon le modèle : Sarah (temps réel), le prospect, ou l'analyse post-appel."""

    def __init__(self, agent: list[list[Any]], prospect: list[str], analysis: dict[str, Any]) -> None:
        self.agent, self.prospect, self.analysis = list(agent), list(prospect), analysis
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if kw.get("tools") and kw["tools"][0].get("name") == ANALYSIS_TOOL_NAME:
            return SimpleNamespace(content=[tool(ANALYSIS_TOOL_NAME, **self.analysis)], stop_reason="tool_use")
        if kw.get("tools"):  # Sarah
            return SimpleNamespace(content=self.agent.pop(0), stop_reason="end_turn")
        return SimpleNamespace(content=[text(self.prospect.pop(0))], stop_reason="end_turn")


def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, prompts_dir=PROJECT_ROOT / "prompts",
                    campaigns_dir=PROJECT_ROOT / "campaigns", llm_realtime_model="claude-haiku-4-5-20251001")


async def test_rdv_de_bout_en_bout(tmp_path: Path):
    llm = SimpleNamespace(messages=FakeMessages(
        agent=[
            [text("Bonjour Madame, je suis Sarah, l'assistante virtuelle d'OpteoLink, une intelligence "
                  "artificielle. Vous avez deux minutes ?")],
            [text("Merci beaucoup, à mardi."), tool("terminer_appel", issue="rdv", resume="RDV mardi")],
        ],
        prospect=["Oui, c'est moi la directrice.", "Oui, mardi dix heures, i.marin@laposte.net, c'est bon."],
        analysis={"score": "chaud", "score_num": 85, "resume": "RDV pris.",
                  "rdv": {"start": "2026-10-20T10:00:00+02:00", "email": "i.marin@laposte.net"}},
    ))
    sc = Scenario(id="t", titre="test", prospect="directrice",
                  attendu=Attendu(issues=[CallOutcome.RDV], rdv=True, requis=["intelligence artificielle"],
                                  interdit=[r"\d+ ?euros"]))
    res = await run_scenario(sc, load_campaign("ehpad", PROJECT_ROOT / "campaigns"), client=llm,
                             settings=settings(tmp_path), slots=[SLOT], now=NOW)
    assert res.ok, [c for c in res.checks if not c.ok]
    assert res.fin == "terminer_appel (rdv)" and res.record.state.rdv is not None
    assert res.record.transcript[-1].text == CLOSING_LINE  # au revoir fixe ajouté
    agent_call = next(c for c in llm.messages.calls if c.get("tools"))
    assert agent_call["model"] == "claude-haiku-4-5-20251001"
    assert agent_call["messages"][0] == {"role": "user", "content": "Allô ?"}


async def test_prospect_raccroche_et_controle_ko(tmp_path: Path):
    llm = SimpleNamespace(messages=FakeMessages(
        agent=[[text("Vous économiserez trente pour cent sur vos factures.")]],
        prospect=["Bonjour ?", "Non merci. [RACCROCHE]"],
        analysis={"score": "froid", "score_num": 10, "resume": "Refus."},
    ))
    sc = Scenario(id="t", titre="test", prospect="pressé",
                  attendu=Attendu(interdit=[r"économis\w*[^.?!]{0,50}pour ?cent"]))
    res = await run_scenario(sc, load_campaign("ehpad", PROJECT_ROOT / "campaigns"), client=llm,
                             settings=settings(tmp_path), slots=[SLOT], now=NOW)
    assert res.fin == "le prospect a raccroché"
    interdit = next(c for c in res.checks if c.nom.startswith("interdit"))
    assert not res.ok and not interdit.ok and "trente pour cent" in interdit.detail


def test_scenarios_ehpad_valides():
    st = Settings(_env_file=None, simulations_dir=PROJECT_ROOT / "simulations")
    ids = [s.id for s in load_scenarios("ehpad", st)]
    assert "directrice_rdv" in ids and "opposition" in ids and len(ids) == len(set(ids))
