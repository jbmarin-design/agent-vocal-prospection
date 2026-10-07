from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from avp import cli, db
from avp.config import Settings
from avp.models import CallMetadata, CallRecordFile, CallState, Prospect, TranscriptTurn
from avp.orchestrator import scheduler as sched_mod

CAMPAIGN_YAML = """\
id: test-ehpad
nom: Campagne de test EHPAD
cible:
  interlocuteur: directeur d'établissement
offre:
  resume: Interconnexion appel malade et téléphonie
accroche: Nous relions l'appel malade aux téléphones des soignants.
axonaut:
  pipe: EHPAD 2026
"""


class FakeLivekit(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("fake_livekit_admin")
        self.dispatched: list[CallMetadata] = []
        self.deleted: list[str] = []

    def room_name_for(self, call_id: str) -> str:
        return f"avp-{call_id}"

    async def dispatch_call(self, meta: CallMetadata, settings: Any = None) -> str:
        self.dispatched.append(meta)
        return f"avp-{meta.call_id}"

    async def list_active_rooms(self, settings: Any = None) -> list[str]:
        return ["avp-x"]

    async def create_outbound_trunk(self, settings: Any = None) -> str:
        return "ST_test123"

    async def list_outbound_trunks(self, settings: Any = None) -> list[dict]:
        return [{"id": "ST_test123", "name": "xivo", "address": "10.0.0.10", "transport": "udp",
                 "numbers": ["+33587140500"]}]

    async def delete_trunk(self, trunk_id: str, settings: Any = None) -> None:
        self.deleted.append(trunk_id)


class FakePrompts(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("fake_prompts")

    def build_instructions(self, role: str, campaign: Any, prospect: Any, *, now: datetime, attempt: int = 1,
                           rdv_slots: Any = (), prompts_dir: Any = None, timezone: str = "Europe/Paris") -> str:
        return f"INSTRUCTIONS {role} pour {prospect.name} ({len(rdv_slots)} créneaux)"

    def opening_line(self, campaign: Any, prospect: Any) -> str:
        return "Bonjour, je suis l'assistant vocal IA d'OpteoLink ; cet appel est enregistré."

    def prompt_version(self, campaign: Any, prompts_dir: Any = None, campaigns_dir: Any = None) -> str:
        return "abc1234"

    def format_slot_fr(self, dt: datetime, timezone: str = "Europe/Paris") -> str:
        return dt.strftime("le %d/%m à %H:%M")


class FakePostcall(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("fake_postcall")
        self.pending_calls: list[int] = []

    async def process_pending(self, limit: int = 20, *, llm_client: Any = None, axonaut: Any = None,
                              settings: Any = None) -> dict[str, int]:
        self.pending_calls.append(limit)
        return {"analyses": 1, "erreurs": 0, "synchro": 1}

    async def process_call(self, call_id: str, **kw: Any) -> Any:
        return None


@pytest.fixture
def fakes(db_ready: Settings, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    (db_ready.campaigns_dir / "test-ehpad.yaml").write_text(CAMPAIGN_YAML, encoding="utf-8")
    mods: dict[str, Any] = {"livekit_admin": FakeLivekit(), "prompts": FakePrompts(), "postcall": FakePostcall()}

    def loader(name: str) -> Any:
        if name not in mods:
            raise ImportError(name)
        return mods[name]

    monkeypatch.setattr(cli, "load_module", loader)
    monkeypatch.setattr(sched_mod, "load_module", loader)
    return mods


def test_parser_structure() -> None:
    p = cli.build_parser()
    a = p.parse_args(["call", "test", "--number", "0612345678", "--campaign", "ehpad", "--live-crm"])
    assert a.func is cli.cmd_call_test and a.live_crm and a.name == "EHPAD test"
    a = p.parse_args(["campaign", "import", "ehpad", "--axonaut", "--limit", "5", "--dry-run"])
    assert a.axonaut and a.limit == 5 and a.dry_run and a.csv is None
    a = p.parse_args(["campaign", "run", "ehpad", "mairies"])
    assert a.campaign_ids == ["ehpad", "mairies"]
    a = p.parse_args(["report", "weekly", "--week", "2026-W41", "--no-ai"])
    assert a.week == "2026-W41" and a.no_ai
    a = p.parse_args(["prompt", "show", "ehpad", "--role", "decideur"])
    assert a.role == "decideur"
    assert p.parse_args(["purge"]).days == 183


def test_usage_errors_return_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["campaign", "import", "ehpad"]) == 2  # --axonaut ou --csv requis
    assert cli.main(["prompt", "show", "x", "--role", "inconnu"]) == 2
    assert cli.main([]) == 2
    assert cli.main(["--help"]) == 0
    assert "commandes" in capsys.readouterr().out


def test_init(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init"]) == 0
    assert settings.db_path.exists()
    assert "Base initialisée" in capsys.readouterr().out


def test_optout_cycle(db_ready: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["optout", "add", "05 62 00 00 01", "--reason", "demande orale"]) == 0
    assert db.is_opted_out("+33562000001")
    assert cli.main(["optout", "list"]) == 0
    out = capsys.readouterr().out
    assert "05 62 00 00 01" in out and "demande orale" in out
    assert cli.main(["optout", "remove", "+33562000001"]) == 0
    assert not db.is_opted_out("+33562000001")
    assert cli.main(["optout", "remove", "+33562000001"]) == 1
    assert cli.main(["optout", "add", "123"]) == 1
    assert "numéro invalide" in capsys.readouterr().err


def test_call_test(fakes: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["call", "test", "--number", "06 12 34 56 78", "--campaign", "test-ehpad"]) == 0
    meta = fakes["livekit_admin"].dispatched[0]
    assert meta.test_mode and meta.prospect.id is None and meta.prospect.phone == "+33612345678"
    assert meta.prospect.name == "EHPAD test"
    out = capsys.readouterr().out
    assert "Appel de test lancé" in out and "TEST" in out
    assert cli.main(["call", "test", "--number", "0612345678", "--campaign", "test-ehpad", "--live-crm"]) == 0
    assert not fakes["livekit_admin"].dispatched[1].test_mode
    # Opposition respectée même en test
    db.add_optout("+33612345678")
    assert cli.main(["call", "test", "--number", "0612345678", "--campaign", "test-ehpad"]) == 1
    assert "opposition" in capsys.readouterr().err
    assert cli.main(["call", "test", "--number", "0612345678", "--campaign", "inconnue"]) == 1


def test_trunk_commands(fakes: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["trunk", "create"]) == 0
    assert "SIP_OUTBOUND_TRUNK_ID=ST_test123" in capsys.readouterr().out
    assert cli.main(["trunk", "list"]) == 0
    assert "10.0.0.10" in capsys.readouterr().out
    assert cli.main(["trunk", "delete", "ST_old"]) == 0
    assert fakes["livekit_admin"].deleted == ["ST_old"]


def test_campaign_list_show_import_csv_stats(fakes: dict[str, Any], tmp_path: Path,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["campaign", "list"]) == 0
    assert "test-ehpad" in capsys.readouterr().out
    assert cli.main(["campaign", "show", "test-ehpad"]) == 0
    out = capsys.readouterr().out
    assert "EHPAD 2026" in out and "abc1234" in out
    f = tmp_path / "p.csv"
    f.write_text("nom;telephone;ville\nEHPAD A;0562000001;Auch\nEHPAD B;0562000002;Gimont\n", encoding="utf-8")
    assert cli.main(["campaign", "import", "test-ehpad", "--csv", str(f)]) == 0
    assert "2 créé(s)" in capsys.readouterr().out
    assert cli.main(["campaign", "stats", "test-ehpad"]) == 0
    out = capsys.readouterr().out
    assert "nouveau" in out and "Prochains appels" in out
    assert cli.main(["campaign", "import", "test-ehpad", "--csv", str(tmp_path / "absent.csv")]) == 1


def test_prompt_show(fakes: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["prompt", "show", "test-ehpad", "--role", "decideur"]) == 0
    out = capsys.readouterr().out
    assert "INSTRUCTIONS decideur" in out and "abc1234" in out and "assistant vocal IA" in out
    assert "(3 créneaux)" in out


def test_postcall_run(fakes: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["postcall", "run", "--limit", "5"]) == 0
    assert fakes["postcall"].pending_calls == [5]
    assert "analyses=1" in capsys.readouterr().out
    assert cli.main(["postcall", "call", "inconnu"]) == 1


def test_calls_list_and_show(db_ready: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    pid, _ = db.upsert_prospect(Prospect(campaign_id="test-ehpad", name="EHPAD Les Tilleuls", phone="+33562000001"))
    db.create_call("abc", "test-ehpad", "+33562000001", "avp-abc", pid)
    path = db_ready.transcripts_dir / "abc.json"
    rec = CallRecordFile(
        metadata=CallMetadata(call_id="abc", campaign_id="test-ehpad",
                              prospect=Prospect(id=pid, campaign_id="test-ehpad", name="EHPAD Les Tilleuls",
                                                phone="+33562000001")),
        state=CallState(contact_name="Mme Martin"),
        transcript=[TranscriptTurn(role="agent", agent="accueil", text="Bonjour, assistant vocal IA d'OpteoLink."),
                    TranscriptTurn(role="prospect", text="Bonjour, c'est à quel sujet ?")],
    )
    path.write_text(rec.model_dump_json(), encoding="utf-8")
    db.update_call("abc", status="termine", outcome="refus", score="froid", transcript_path=str(path),
                   state_json=rec.state, duration_s=42.0)
    assert cli.main(["calls", "list", "--last", "5"]) == 0
    out = capsys.readouterr().out
    assert "abc" in out and "refus" in out and "05 62 00 00 01" in out
    assert cli.main(["calls", "show", "abc"]) == 0
    out = capsys.readouterr().out
    assert "Agent/accueil : Bonjour" in out and "Prospect : Bonjour" in out and "Mme Martin" in out
    assert cli.main(["calls", "show", "nope"]) == 1


def test_report_purge_backup(db_ready: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["report", "weekly", "--week", "2026-W41", "--no-ai"]) == 0
    assert (db_ready.reports_dir / "rapport-2026-S41.md").exists()
    assert (db_ready.reports_dir / "rapport-2026-S41.html").exists()
    assert cli.main(["report", "weekly", "--week", "mauvais"]) == 1
    assert cli.main(["purge", "--days", "30", "--dry-run"]) == 0
    assert "[simulation]" in capsys.readouterr().out
    assert cli.main(["backup", str(tmp_path / "bk")]) == 0
    assert len(list((tmp_path / "bk").glob("avp-*.db"))) == 1


def test_check_reports_errors(fakes: dict[str, Any], db_ready: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["check"])
    out = capsys.readouterr().out
    assert rc == 1  # clés absentes dans les settings de test
    assert "LiveKit joignable" in out and "1 room(s)" in out
    assert "Campagne test-ehpad" in out and "Trunk SIP sortant" in out


def test_run_scheduler_for_campaign(fakes: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class FakeScheduler:
        def __init__(self, campaign_ids: Any, *, settings: Any, **hooks: Any) -> None:
            seen["ids"] = campaign_ids

        async def run(self) -> None:
            seen["ran"] = True

    monkeypatch.setattr(sched_mod, "Scheduler", FakeScheduler)
    assert cli.main(["campaign", "run", "test-ehpad"]) == 0
    assert seen == {"ids": ["test-ehpad"], "ran": True}
    assert cli.main(["campaign", "run", "inconnue"]) == 1
    assert cli.main(["run"]) == 0 and seen["ids"] is None


def test_purge_transcripts_real(db_ready: Settings) -> None:
    import os
    from datetime import UTC, timedelta

    from avp.orchestrator.maintenance import purge_transcripts

    old_file = db_ready.transcripts_dir / "old.json"
    new_file = db_ready.transcripts_dir / "new.json"
    orphan = db_ready.transcripts_dir / "orphan.json"
    for f in (old_file, new_file, orphan):
        f.write_text("{}", encoding="utf-8")
    db.create_call("old", "test-ehpad", "+33562000001", "avp-old", None)
    db.create_call("new", "test-ehpad", "+33562000001", "avp-new", None)
    db.update_call("old", transcript_path=str(old_file))
    db.update_call("new", transcript_path="transcripts/new.json")  # chemin relatif à data/
    old_iso = (db.utcnow() - timedelta(days=200)).astimezone(UTC).isoformat()
    with db.connect() as c:
        c.execute("UPDATE calls SET created_at=? WHERE id='old'", (old_iso,))
    ts = (db.utcnow() - timedelta(days=200)).timestamp()
    os.utime(orphan, (ts, ts))

    assert purge_transcripts(183, settings=db_ready, dry_run=True) == 2
    assert old_file.exists() and orphan.exists()
    assert purge_transcripts(183, settings=db_ready) == 2
    assert not old_file.exists() and not orphan.exists() and new_file.exists()
    row = db.get_call("old")
    assert row is not None and row["transcript_path"] is None
    row = db.get_call("new")
    assert row is not None and row["transcript_path"] == "transcripts/new.json"
    with pytest.raises(ValueError):
        purge_transcripts(0, settings=db_ready)
