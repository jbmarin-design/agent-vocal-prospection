"""Tests du post-appel : analyse LLM (client factice), synchro Axonaut (faux client), bout en bout SQLite."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from avp import db
from avp.config import PROJECT_ROOT, Settings
from avp.models import (
    AxonautMapping,
    CallAnalysis,
    Callback,
    CallMetadata,
    CallOutcome,
    CallRecordFile,
    CallState,
    Campaign,
    Prospect,
    Rdv,
    TranscriptTurn,
)
from avp.postcall import (
    ANALYSIS_TOOL_NAME,
    AnalysisError,
    analyze_call,
    build_system_prompt,
    process_call,
    process_pending,
    sync_axonaut,
    target_step,
)

START = datetime(2026, 10, 5, 8, 30, tzinfo=UTC)  # 10:30 à Paris

CAMPAIGN_DATA: dict[str, Any] = {
    "id": "ehpad",
    "nom": "EHPAD appel malade",
    "cible": {"interlocuteur": "directeur", "alternatives": ["responsable technique"]},
    "offre": {"resume": "Interconnexion appel malade / antifugue avec la téléphonie"},
    "accroche": "Bonjour",
    "qualification": [
        {"id": "nb_lits", "question": "Combien de lits ?"},
        {"id": "systeme_appel_malade", "question": "Quel système d'appel malade ?", "obligatoire": True},
    ],
    "objections": [{"objection": "Déjà équipé", "reponse": "Demander l'échéance du contrat"}],
    "scoring": {"chaud": "RDV ou projet < 6 mois", "tiede": "intérêt sans échéance", "froid": "aucun besoin"},
    "axonaut": {
        "pipe": "EHPAD 2026",
        "etape_initiale": "À contacter",
        "etapes": {"rdv": "RDV pris", "rappel": "À rappeler", "refus": "Perdu", "chaud": "Qualifié", "tiede": "Intéressé"},
        "montant_defaut": 1500.0,
    },
}


def campaign(**over: Any) -> Campaign:
    data = {**CAMPAIGN_DATA, **over}
    return Campaign.model_validate(data)


def record(outcome: CallOutcome, *, test_mode: bool = False, company_id: int | None = 555,
           opp_id: int | None = None, **state: Any) -> CallRecordFile:
    return CallRecordFile(
        metadata=CallMetadata(
            call_id="abc123",
            campaign_id="ehpad",
            test_mode=test_mode,
            prospect=Prospect(
                id=1, campaign_id="ehpad", name="EHPAD Les Tilleuls", phone="+33562000000", city="Auch",
                contact_name="M. Durand", axonaut_company_id=company_id, axonaut_opportunity_id=opp_id,
            ),
        ),
        state=CallState(outcome=outcome, answers={"nb_lits": "80"}, contact_name="Mme Martin",
                        contact_role="directrice", **state),
        transcript=[
            TranscriptTurn(role="agent", agent="accueil", text="Bonjour, je suis l'assistant vocal IA d'OpteoLink…",
                           ts=START),
            TranscriptTurn(role="prospect", text="Oui, bonjour.", ts=START + timedelta(seconds=5)),
        ],
        started_at=START,
        answered_at=START,
        ended_at=START + timedelta(seconds=150),
    )


def analysis(**over: Any) -> CallAnalysis:
    base = {"score": "tiede", "score_num": 50, "resume": "Directrice intéressée, rappel en novembre.",
            "objections": ["Budget contraint"], "prochaine_action": "Rappeler en novembre"}
    return CallAnalysis.model_validate({**base, **over})


def test_target_step_priority():
    m = AxonautMapping(pipe="P", etapes={"rdv": "RDV pris", "chaud": "Qualifié"})
    assert target_step(CallOutcome.RDV, analysis(score="chaud", score_num=90), m) == "RDV pris"
    assert target_step(CallOutcome.QUALIFIE, analysis(score="chaud", score_num=90), m) == "Qualifié"
    assert target_step(CallOutcome.QUALIFIE, analysis(), m) is None
    assert target_step(CallOutcome.QUALIFIE, None, m) is None


# ---------------------------------------------------------------------------
# analyze_call
# ---------------------------------------------------------------------------


class FakeMessages:
    def __init__(self, inputs: list[Any]) -> None:
        self.inputs = inputs
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        data = self.inputs[len(self.calls) - 1]
        if data is None:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="oups")])
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", id=f"toolu_{len(self.calls)}", name=ANALYSIS_TOOL_NAME,
                                     input=data)]
        )


class FakeLLM:
    def __init__(self, *inputs: Any) -> None:
        self.messages = FakeMessages(list(inputs))


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        campaigns_dir=tmp_path / "campaigns",
        prompts_dir=PROJECT_ROOT / "prompts",
        axonaut_api_key="",
        anthropic_api_key="",
        dry_run=False,
    )


GOOD = {"score": "chaud", "score_num": 85, "resume": "RDV pris.", "date_relance": "2026-11-17T10:00:00",
        "qualite_appel": 4, "suggestions_script": ["Raccourcir l'accroche"]}


async def test_analyze_call_success(tmp_path: Path):
    llm = FakeLLM(GOOD)
    st = settings_for(tmp_path)
    res = await analyze_call(record(CallOutcome.RDV, rdv=Rdv(start=START + timedelta(days=7))), campaign(),
                             client=llm, settings=st)
    assert res.score == "chaud" and res.score_num == 85
    assert res.rdv_confirme is True  # forcé par l'outil confirmer_rdv
    assert res.date_relance is not None and res.date_relance.utcoffset() == timedelta(hours=1)
    call = llm.messages.calls[0]
    assert call["model"] == st.llm_analysis_model
    assert call["tool_choice"] == {"type": "tool", "name": ANALYSIS_TOOL_NAME}
    assert call["tools"][0]["input_schema"] == CallAnalysis.model_json_schema()
    assert "analyste commercial" in call["system"] and "Combien de lits" in call["system"]
    user = call["messages"][0]["content"]
    assert "EHPAD Les Tilleuls" in user and "Agent IA (accueil)" in user and '"nb_lits": "80"' in user
    assert "[10:30:00]" in user


async def test_analyze_call_retry_on_invalid(tmp_path: Path):
    llm = FakeLLM({"score": "brulant", "score_num": 200, "resume": "x"}, GOOD)
    res = await analyze_call(record(CallOutcome.QUALIFIE), campaign(), client=llm, settings=settings_for(tmp_path))
    assert res.score == "chaud"
    assert len(llm.messages.calls) == 2
    retry_msgs = llm.messages.calls[1]["messages"]
    assert retry_msgs[-1]["content"][0]["type"] == "tool_result"
    assert retry_msgs[-1]["content"][0]["is_error"] is True


async def test_analyze_call_fails_after_two(tmp_path: Path):
    llm = FakeLLM(None, {"score": "x"})
    with pytest.raises(AnalysisError):
        await analyze_call(record(CallOutcome.QUALIFIE), campaign(), client=llm, settings=settings_for(tmp_path))


def test_system_prompt_without_file(tmp_path: Path):
    st = settings_for(tmp_path).model_copy(update={"prompts_dir": tmp_path})
    txt = build_system_prompt(campaign(), st)
    assert "OpteoLink" in txt and "RDV ou projet" in txt


# ---------------------------------------------------------------------------
# sync_axonaut
# ---------------------------------------------------------------------------


class FakeAxonaut:
    dry_run = False

    def __init__(self, existing: dict | None = None) -> None:
        self.existing = existing
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._n = 100

    def _next(self) -> dict:
        self._n += 1
        return {"id": self._n}

    async def find_opportunity(self, company_id: int, pipe_name: str) -> dict | None:
        self.calls.append(("find_opportunity", {"company_id": company_id, "pipe_name": pipe_name}))
        return self.existing

    async def get_opportunity(self, opportunity_id: int) -> dict:
        self.calls.append(("get_opportunity", {"id": opportunity_id}))
        return {"id": opportunity_id, "comments": "ancien commentaire"}

    async def create_event(self, **kw: Any) -> dict:
        self.calls.append(("create_event", kw))
        return self._next()

    async def create_opportunity(self, **kw: Any) -> dict:
        self.calls.append(("create_opportunity", kw))
        return self._next()

    async def update_opportunity(self, opportunity_id: int, **kw: Any) -> dict:
        self.calls.append(("update_opportunity", {"id": opportunity_id, **kw}))
        return {"id": opportunity_id}

    async def create_task(self, **kw: Any) -> dict:
        self.calls.append(("create_task", kw))
        return self._next()

    async def list_company_employees(self, company_id: int) -> list[dict]:
        self.calls.append(("list_company_employees", {"company_id": company_id}))
        return [{"firstname": "Paul", "lastname": "Durand", "email": "p.durand@tilleuls.fr"}]

    async def create_employee(self, **kw: Any) -> dict:
        self.calls.append(("create_employee", kw))
        return self._next()

    async def aclose(self) -> None:
        pass

    def of(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]


async def test_sync_rdv_creates_opportunity_events_task(tmp_path: Path):
    ax = FakeAxonaut()
    rdv_start = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    rec = record(CallOutcome.RDV, rdv=Rdv(start=rdv_start, duree_min=30, avec="Mme Martin", mode="visio"))
    res = await sync_axonaut(rec, analysis(score="chaud", score_num=85, rdv_confirme=True), campaign(),
                             axonaut=ax, settings=settings_for(tmp_path))
    assert res["erreurs"] == []
    opp = ax.of("create_opportunity")[0]
    assert opp["pipe_name"] == "EHPAD 2026" and opp["step_name"] == "RDV pris"
    assert opp["amount"] == 1500.0 and opp["probability"] == 85.0
    events = ax.of("create_event")
    # Le RDV va dans Google Agenda (avp.agenda), pas dans l'agenda Axonaut : un seul événement « appel ».
    (call_ev,) = events
    assert call_ev["nature"] == 3 and call_ev["title"] == "Appel IA — RDV pris — chaud (85/100)"
    assert call_ev["duration_min"] == 3 and call_ev["date"] == START and call_ev["opportunity_id"] == res["opportunity_id"]
    assert "Combien de lits ? → 80" in call_ev["content"] and "Budget contraint" in call_ev["content"]
    assert str(tmp_path / "transcripts" / "abc123.json") in call_ev["content"]
    assert "RDV :" in call_ev["content"]
    assert ax.of("create_task") == [] and "rdv_event_id" not in res


async def test_sync_echeance_lointaine_cree_une_relance(tmp_path: Path):
    from datetime import date

    ax = FakeAxonaut()
    an = analysis(score="froid", score_num=15, date_decision="2028-06-30",
                  objet_decision="contrat de maintenance appel malade")
    res = await sync_axonaut(record(CallOutcome.REFUS), an, campaign(), axonaut=ax, settings=settings_for(tmp_path))
    (task,) = ax.of("create_task")
    assert task["title"].startswith("Relance échéance : contrat de maintenance appel malade")
    assert task["due"] == date(2028, 6, 30) - timedelta(days=90)
    assert res["echeance"] == "2028-06-30"


async def test_sync_rappel_updates_existing_opportunity(tmp_path: Path):
    ax = FakeAxonaut(existing={"id": 77, "comments": "historique"})
    when = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    rec = record(CallOutcome.RAPPEL, callback=Callback(when=when, ask_for="M. Durand"))
    res = await sync_axonaut(rec, analysis(), campaign(), axonaut=ax, settings=settings_for(tmp_path))
    assert not ax.of("create_opportunity")
    upd = ax.of("update_opportunity")[0]
    assert upd["id"] == 77 and upd["step_name"] == "À rappeler" and upd["probability"] == 50.0
    assert upd["comments"].endswith("historique") and "appel IA" in upd["comments"]
    task = ax.of("create_task")[0]
    assert task["title"] == "Rappeler M. Durand — EHPAD Les Tilleuls" and str(task["due"]) == "2026-11-03"
    assert ax.of("create_event")[0]["opportunity_id"] == 77
    assert res["opportunity_action"] == "maj"


async def test_sync_known_opportunity_id_reads_comment(tmp_path: Path):
    ax = FakeAxonaut()
    await sync_axonaut(record(CallOutcome.REFUS, opp_id=42), analysis(score="froid", score_num=10), campaign(),
                       axonaut=ax, settings=settings_for(tmp_path))
    assert not ax.of("find_opportunity")
    upd = ax.of("update_opportunity")[0]
    assert upd["id"] == 42 and upd["step_name"] == "Perdu" and upd["comments"].endswith("ancien commentaire")


async def test_sync_opposition_no_opportunity(tmp_path: Path):
    ax = FakeAxonaut()
    rec = record(CallOutcome.OPPOSITION, optout=True, optout_reason="ne veut plus être appelé")
    res = await sync_axonaut(rec, analysis(score="froid", score_num=0), campaign(), axonaut=ax,
                             settings=settings_for(tmp_path))
    assert not ax.of("create_opportunity") and not ax.of("create_task")
    titles = [e["title"] for e in ax.of("create_event")]
    assert titles[1] == "Opposition au démarchage"
    assert res["optout_event_id"]


async def test_sync_skips(tmp_path: Path):
    ax = FakeAxonaut()
    st = settings_for(tmp_path)
    r1 = await sync_axonaut(record(CallOutcome.RDV, test_mode=True), analysis(), campaign(), axonaut=ax, settings=st)
    r2 = await sync_axonaut(record(CallOutcome.RDV), analysis(), campaign(axonaut=None), axonaut=ax, settings=st)
    r3 = await sync_axonaut(record(CallOutcome.RDV, company_id=None), analysis(), campaign(), axonaut=ax, settings=st)
    r4 = await sync_axonaut(record(CallOutcome.REPONDEUR), None, campaign(), axonaut=ax, settings=st)
    assert all("skipped" in r for r in (r1, r2, r3, r4))
    assert ax.calls == []


# ---------------------------------------------------------------------------
# process_call / process_pending (bout en bout SQLite)
# ---------------------------------------------------------------------------


def setup_db(tmp_path: Path, rec: CallRecordFile, *, test_mode: bool = False) -> tuple[Settings, str, int]:
    st = settings_for(tmp_path)
    st.ensure_dirs()
    (tmp_path / "campaigns").mkdir(exist_ok=True)
    (tmp_path / "campaigns" / "ehpad.yaml").write_text(yaml.safe_dump(CAMPAIGN_DATA, allow_unicode=True),
                                                       encoding="utf-8")
    db.init_db(st.db_path)
    pid, _ = db.upsert_prospect(rec.metadata.prospect, st.db_path)
    call_id = rec.metadata.call_id
    db.create_call(call_id, "ehpad", rec.metadata.prospect.phone, f"avp-{call_id}", pid, test_mode=test_mode,
                   db_path=st.db_path)
    path = st.transcripts_dir / f"{call_id}.json"
    path.write_text(rec.model_dump_json(), encoding="utf-8")
    db.update_call(call_id, st.db_path, status="termine", ended_at=rec.ended_at, transcript_path=str(path),
                   outcome=rec.state.outcome)
    return st, call_id, pid


async def test_process_call_end_to_end(tmp_path: Path):
    rec = record(CallOutcome.RAPPEL, callback=Callback(when=datetime(2026, 11, 3, 9, 0, tzinfo=UTC)))
    st, call_id, pid = setup_db(tmp_path, rec)
    ax = FakeAxonaut()
    res = await process_call(call_id, llm_client=FakeLLM(GOOD), axonaut=ax, settings=st)  # type: ignore[arg-type]
    assert res is not None and res.score == "chaud"
    row = db.get_call(call_id, st.db_path)
    assert row and row["analysis_status"] == "fait" and row["score"] == "chaud"
    assert row["axonaut_synced"] == 1 and row["error"] is None
    assert json.loads(row["analysis_json"])["score_num"] == 85
    assert db.get_prospect_row(pid, st.db_path)["last_score"] == "chaud"  # type: ignore[index]
    assert str(tmp_path / "transcripts" / "abc123.json") in ax.of("create_event")[0]["content"]


async def test_process_call_no_human_no_llm(tmp_path: Path):
    rec = record(CallOutcome.REPONDEUR)
    st, call_id, _ = setup_db(tmp_path, rec)
    llm = FakeLLM()
    ax = FakeAxonaut()
    assert await process_call(call_id, llm_client=llm, axonaut=ax, settings=st) is None  # type: ignore[arg-type]
    assert llm.messages.calls == [] and ax.calls == []
    row = db.get_call(call_id, st.db_path)
    assert row and row["analysis_status"] == "ignore" and row["axonaut_synced"] == 0


async def test_process_call_llm_error_and_missing_transcript(tmp_path: Path):
    rec = record(CallOutcome.QUALIFIE)
    st, call_id, _ = setup_db(tmp_path, rec)
    assert await process_call(call_id, llm_client=FakeLLM(None, None), axonaut=FakeAxonaut(),  # type: ignore[arg-type]
                              settings=st) is None
    row = db.get_call(call_id, st.db_path)
    assert row and row["analysis_status"] == "erreur" and "analyse" in row["error"]

    (st.transcripts_dir / f"{call_id}.json").unlink()
    db.update_call(call_id, st.db_path, analysis_status="en_attente", error=None)
    assert await process_call(call_id, llm_client=FakeLLM(GOOD), settings=st) is None
    row = db.get_call(call_id, st.db_path)
    assert row and row["analysis_status"] == "erreur" and "introuvable" in row["error"]


async def test_process_call_test_mode_column(tmp_path: Path):
    rec = record(CallOutcome.QUALIFIE)
    st, call_id, _ = setup_db(tmp_path, rec, test_mode=True)
    ax = FakeAxonaut()
    res = await process_call(call_id, llm_client=FakeLLM(GOOD), axonaut=ax, settings=st)  # type: ignore[arg-type]
    assert res is not None and ax.calls == []
    row = db.get_call(call_id, st.db_path)
    assert row and row["analysis_status"] == "fait" and row["axonaut_synced"] == 0


async def test_process_pending_counts(tmp_path: Path):
    rec = record(CallOutcome.QUALIFIE)
    st, call_id, _ = setup_db(tmp_path, rec)
    out = await process_pending(10, llm_client=FakeLLM(GOOD), axonaut=FakeAxonaut(), settings=st)  # type: ignore[arg-type]
    assert out == {"analyses": 1, "erreurs": 0, "synchro": 1}
    assert await process_pending(10, llm_client=FakeLLM(), settings=st) == {"analyses": 0, "erreurs": 0, "synchro": 0}


async def test_sync_barrage_cree_les_contacts_recueillis(tmp_path: Path):
    from avp.models import ContactInfo

    ax = FakeAxonaut()
    contacts = [
        ContactInfo(prenom="Claire", nom="Martin", fonction="directrice", telephone="+33612345678",
                    email="c.martin@tilleuls.fr", disponibilites="le mardi matin"),
        ContactInfo(nom="Durand", fonction="agent technique"),  # déjà connu dans Axonaut
        ContactInfo(fonction="secrétariat", disponibilites="9 h - 12 h"),  # sans nom : non créé
    ]
    res = await sync_axonaut(record(CallOutcome.BARRAGE, contacts=contacts), analysis(), campaign(),
                             axonaut=ax, settings=settings_for(tmp_path))
    assert res["erreurs"] == []
    (emp,) = ax.of("create_employee")
    assert emp["lastname"] == "Martin" and emp["firstname"] == "Claire" and emp["job"] == "directrice"
    assert emp["cellphone_number"] == "06 12 34 56 78" and emp["phone_number"] == ""
    content = ax.of("create_event")[0]["content"]
    assert "Contacts recueillis :" in content
    assert "Claire Martin (directrice) — 06 12 34 56 78 — c.martin@tilleuls.fr — dispo : le mardi matin" in content
    assert "(nom non donné) (secrétariat) — dispo : 9 h - 12 h" in content


async def test_sync_opposition_ne_cree_pas_de_contact(tmp_path: Path):
    from avp.models import ContactInfo

    ax = FakeAxonaut()
    rec = record(CallOutcome.OPPOSITION, optout=True, contacts=[ContactInfo(nom="Martin", fonction="directrice")])
    await sync_axonaut(rec, analysis(), campaign(), axonaut=ax, settings=settings_for(tmp_path))
    assert ax.of("create_employee") == []
