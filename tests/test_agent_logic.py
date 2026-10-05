"""Tests de la logique pure du worker (src/avp/agent/logic.py) — sans LiveKit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from avp import db
from avp.agent import logic
from avp.config import Settings
from avp.models import (
    AnalysisStatus,
    Callback,
    CallMetadata,
    CallOutcome,
    CallState,
    Prospect,
    ProspectStatus,
    Rdv,
)

PARIS = ZoneInfo("Europe/Paris")


def test_classify_sip_failure():
    assert logic.classify_sip_failure(486) is CallOutcome.OCCUPE
    assert logic.classify_sip_failure(404) is CallOutcome.MAUVAIS_NUMERO
    assert logic.classify_sip_failure(480) is CallOutcome.NON_DECROCHE
    assert logic.classify_sip_failure(None, timeout=True) is CallOutcome.NON_DECROCHE
    assert logic.classify_sip_failure(503) is CallOutcome.ERREUR


def test_outcome_from_amd():
    assert logic.outcome_from_amd("machine-vm") is CallOutcome.REPONDEUR
    assert logic.outcome_from_amd("machine-ivr") is CallOutcome.SVI
    assert logic.outcome_from_amd("machine-unavailable") is CallOutcome.NON_DECROCHE
    assert logic.outcome_from_amd("human") is None
    assert logic.outcome_from_amd("uncertain") is None


@pytest.mark.parametrize(
    "state,answered,expected",
    [
        (CallState(optout=True, rdv=None), True, CallOutcome.OPPOSITION),
        (CallState(rdv=Rdv(start=datetime(2026, 10, 8, 8, tzinfo=UTC))), True, CallOutcome.RDV),
        (CallState(transferred=True), True, CallOutcome.TRANSFERT),
        (CallState(outcome=CallOutcome.REFUS), True, CallOutcome.REFUS),
        (CallState(callback=Callback(notes="jeudi")), True, CallOutcome.RAPPEL),
        (CallState(amd_result="machine-vm"), True, CallOutcome.REPONDEUR),
        (CallState(), False, CallOutcome.NON_DECROCHE),
        (CallState(decision_maker_reached=True, answers={"a": "b"}), True, CallOutcome.QUALIFIE),
        (CallState(decision_maker_reached=True), True, CallOutcome.REFUS),
        (CallState(amd_result="human"), True, CallOutcome.BARRAGE),
    ],
)
def test_infer_outcome(state, answered, expected):
    assert logic.infer_outcome(state, answered=answered) is expected


def test_opposition_prime_sur_rdv():
    st = CallState(optout=True, rdv=Rdv(start=datetime(2026, 10, 8, 8, tzinfo=UTC)))
    assert logic.infer_outcome(st, answered=True) is CallOutcome.OPPOSITION


def test_analysis_status():
    assert logic.analysis_status_for(CallOutcome.RDV, test_mode=False, console=False) is AnalysisStatus.EN_ATTENTE
    assert logic.analysis_status_for(CallOutcome.REPONDEUR, test_mode=False, console=False) is AnalysisStatus.IGNORE
    assert logic.analysis_status_for(CallOutcome.RDV, test_mode=True, console=True) is AnalysisStatus.IGNORE


def test_parse_and_match_slot():
    slot = datetime(2026, 10, 13, 8, 30, tzinfo=UTC)  # 10 h 30 à Paris
    assert logic.match_slot("2026-10-13T10:30:00+02:00", [slot], PARIS) == slot
    assert logic.match_slot("2026-10-13T10:30:00", [slot], PARIS) == slot  # sans fuseau = heure locale
    assert logic.match_slot("2026-10-13T11:30:00+02:00", [slot], PARIS) is None
    assert logic.match_slot("demain", [slot], PARIS) is None
    assert logic.parse_tool_datetime("", PARIS) is None


def test_coerce_outcome_and_email():
    assert logic.coerce_outcome("Rappel", logic.END_OUTCOMES_ACCUEIL) is CallOutcome.RAPPEL
    assert logic.coerce_outcome("rdv", logic.END_OUTCOMES_ACCUEIL) is None
    assert logic.coerce_outcome("non qualifié", logic.END_OUTCOMES_DECIDEUR) is CallOutcome.NON_QUALIFIE
    assert logic.looks_like_email("direction@ehpad-tilleuls.fr")
    assert not logic.looks_like_email("direction arobase ehpad")


def test_history_to_transcript():
    items = [
        {"role": "system", "text": "instructions", "created_at": 1.0},
        {"role": "assistant", "text": "Bonjour", "created_at": 1_760_000_000.0},
        {"role": "user", "text": " Allô ? ", "created_at": 1_760_000_002.0},
        {"role": "user", "text": "", "created_at": 1_760_000_003.0},
    ]
    turns = logic.history_to_transcript(items)
    assert [(t.role, t.text) for t in turns] == [("agent", "Bonjour"), ("prospect", "Allô ?")]
    assert turns[0].ts is not None and turns[0].ts.tzinfo is not None


def test_console_metadata():
    meta = logic.console_metadata("ehpad", now=datetime(2026, 10, 5, 8, tzinfo=UTC))
    assert meta.call_id.startswith("console-")
    assert meta.prospect.id is None and meta.test_mode
    assert len(meta.rdv_slots) == 3 and all(s > datetime(2026, 10, 5, tzinfo=UTC) for s in meta.rdv_slots)


def test_dump_usage():
    class U:
        def model_dump(self):
            return {"llm_tokens": 12, "when": datetime(2026, 1, 1)}

    assert logic.dump_usage(U())["llm_tokens"] == 12
    assert logic.dump_usage(None) == {}


# ---------------------------------------------------------------------------
# finalize_call (fichier + base SQLite)
# ---------------------------------------------------------------------------


def _setup(tmp_path, make_campaign, *, with_prospect=True):
    settings = Settings(data_dir=tmp_path, campaigns_dir=tmp_path, prompts_dir=tmp_path)
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    campaign = make_campaign()
    pid = None
    if with_prospect:
        pid, _ = db.upsert_prospect(
            Prospect(campaign_id=campaign.id, name="EHPAD Test", phone="+33562000000"), settings.db_path
        )
    prospect = Prospect(id=pid, campaign_id=campaign.id, name="EHPAD Test", phone="+33562000000")
    meta = CallMetadata(call_id="abc123", campaign_id=campaign.id, prospect=prospect, attempt=1)
    db.create_call("abc123", campaign.id, prospect.phone, "avp-abc123", pid, db_path=settings.db_path)
    ud = logic.CallUserData(meta=meta, campaign=campaign, settings=settings, prompt_version="v1")
    return settings, ud, pid


def test_finalize_rdv(tmp_path, make_campaign):
    settings, ud, pid = _setup(tmp_path, make_campaign)
    ud.answered_at = datetime.now(UTC) - timedelta(minutes=3)
    ud.state.decision_maker_reached = True
    ud.state.rdv = Rdv(start=datetime(2026, 10, 13, 8, 30, tzinfo=UTC), email="a@b.fr")
    turns = logic.history_to_transcript([{"role": "assistant", "text": "Bonjour", "created_at": 1.0}])

    path = logic.finalize_call(ud, turns)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["state"]["outcome"] == "rdv" and data["prompt_version"] == "v1"
    call = db.get_call("abc123", settings.db_path)
    assert call["status"] == "termine" and call["outcome"] == "rdv"
    assert call["analysis_status"] == "en_attente" and call["duration_s"] > 100
    row = db.get_prospect_row(pid, settings.db_path)
    assert row["status"] == ProspectStatus.TERMINE.value and row["attempts"] == 1
    # idempotent
    assert logic.finalize_call(ud, turns) == path


def test_finalize_opposition_et_non_decroche(tmp_path, make_campaign):
    settings, ud, pid = _setup(tmp_path, make_campaign)
    ud.answered_at = datetime.now(UTC)
    ud.state.optout = True
    ud.state.optout_reason = "ne plus appeler"
    logic.finalize_call(ud, [])
    assert db.is_opted_out("+33562000000", settings.db_path)
    assert db.get_prospect_row(pid, settings.db_path)["status"] == ProspectStatus.EXCLU.value


def test_finalize_non_decroche_reprogramme(tmp_path, make_campaign):
    settings, ud, pid = _setup(tmp_path, make_campaign)
    logic.finalize_call(ud, [])
    call = db.get_call("abc123", settings.db_path)
    assert call["outcome"] == "non_decroche" and call["analysis_status"] == "ignore"
    row = db.get_prospect_row(pid, settings.db_path)
    assert row["status"] == ProspectStatus.A_RAPPELER.value and row["next_attempt_at"]


def test_finalize_erreur_sans_prospect(tmp_path, make_campaign):
    settings, ud, _ = _setup(tmp_path, make_campaign, with_prospect=False)
    logic.finalize_call(ud, [], error="crash STT")
    call = db.get_call("abc123", settings.db_path)
    assert call["status"] == "erreur" and call["error"] == "crash STT"


def test_finalize_console_sans_base(tmp_path, make_campaign):
    settings = Settings(data_dir=tmp_path)
    meta = logic.console_metadata("test")
    ud = logic.CallUserData(meta=meta, campaign=make_campaign(), settings=settings, console=True)
    path = logic.finalize_call(ud, [])
    assert path.exists() and not settings.db_path.exists()
