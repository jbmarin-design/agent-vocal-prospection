from __future__ import annotations

import asyncio
import types
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from avp import db
from avp.config import Settings
from avp.models import CallMetadata, Campaign, Prospect
from avp.orchestrator import scheduler as sched_mod
from avp.orchestrator.scheduler import CallBlocked, DispatchError, Scheduler, launch_call

PARIS = ZoneInfo("Europe/Paris")
MONDAY_11H = datetime(2026, 10, 5, 11, 0, tzinfo=PARIS)


@pytest.fixture(autouse=True)
def _fake_livekit_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """livekit_admin (brique A) remplacé : seul room_name_for est utilisé hors dispatch injecté."""
    mod = types.ModuleType("fake_livekit_admin")
    mod.room_name_for = lambda call_id: f"avp-{call_id}"  # type: ignore[attr-defined]

    async def _no_dispatch(meta: CallMetadata, settings: Any = None) -> str:
        raise AssertionError("dispatch réel appelé")

    mod.dispatch_call = _no_dispatch  # type: ignore[attr-defined]
    monkeypatch.setattr(sched_mod, "load_module", lambda name: mod)


class FakeDispatch:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.metas: list[CallMetadata] = []

    async def __call__(self, meta: CallMetadata, settings: Any = None) -> str:
        if self.fail:
            raise ConnectionError("LiveKit injoignable")
        self.metas.append(meta)
        return f"avp-{meta.call_id}"


def _add_prospects(campaign_id: str, n: int, start: int = 1) -> list[int]:
    ids = []
    for i in range(start, start + n):
        pid, _ = db.upsert_prospect(Prospect(campaign_id=campaign_id, name=f"EHPAD {i}", phone=f"+3356200{i:04d}"))
        ids.append(pid)
    # upsert pose next_attempt_at à l'heure réelle : on le vide pour que les tests ne dépendent pas de la date.
    with db.connect() as c:
        c.execute("UPDATE prospects SET next_attempt_at=NULL")
    return ids


def _prospect_row(pid: int) -> dict[str, Any]:
    row = db.get_prospect_row(pid)
    assert row is not None
    return row


async def test_launch_call_ok(db_ready: Settings, test_campaign: Campaign) -> None:
    (pid,) = _add_prospects(test_campaign.id, 1)
    p = db.get_prospect(pid)
    assert p is not None
    disp = FakeDispatch()
    call_id = await launch_call(p, test_campaign, attempt=2, now=MONDAY_11H, settings=db_ready, dispatch=disp)
    meta = disp.metas[0]
    assert meta.call_id == call_id and meta.attempt == 2 and not meta.test_mode
    assert meta.prospect.id == pid and meta.campaign_id == test_campaign.id
    assert len(meta.rdv_slots) == 3 and all(s >= MONDAY_11H + timedelta(days=2) for s in meta.rdv_slots)
    row = db.get_call(call_id)
    assert row is not None
    assert row["status"] == "en_cours" and row["room_name"] == f"avp-{call_id}"
    assert row["attempt"] == 2 and row["prospect_id"] == pid


async def test_launch_call_optout(db_ready: Settings, test_campaign: Campaign) -> None:
    (pid,) = _add_prospects(test_campaign.id, 1)
    p = db.get_prospect(pid)
    assert p is not None
    db.add_optout(p.phone, "test")
    disp = FakeDispatch()
    with pytest.raises(CallBlocked):
        await launch_call(p, test_campaign, attempt=1, now=MONDAY_11H, settings=db_ready, dispatch=disp)
    assert disp.metas == []
    assert _prospect_row(pid)["status"] == "exclu"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


async def test_launch_call_dispatch_failure(db_ready: Settings, test_campaign: Campaign) -> None:
    (pid,) = _add_prospects(test_campaign.id, 1)
    db.claim_due_prospects(test_campaign.id, MONDAY_11H, 5, 3)
    p = db.get_prospect(pid)
    assert p is not None
    with pytest.raises(DispatchError) as exc:
        await launch_call(p, test_campaign, attempt=1, now=MONDAY_11H, settings=db_ready, dispatch=FakeDispatch(True))
    row = db.get_call(exc.value.call_id)
    assert row is not None
    assert row["status"] == "erreur" and row["outcome"] == "erreur" and "injoignable" in row["error"]
    assert row["analysis_status"] == "ignore"
    pr = _prospect_row(pid)
    assert pr["status"] == "a_rappeler" and pr["attempts"] == 0
    assert db.parse_dt(pr["next_attempt_at"]) == MONDAY_11H.astimezone(UTC) + timedelta(minutes=15)


async def test_launch_test_call_without_prospect_id(db_ready: Settings, test_campaign: Campaign) -> None:
    p = Prospect(id=None, campaign_id=test_campaign.id, name="EHPAD test", phone="+33612345678")
    disp = FakeDispatch()
    call_id = await launch_call(p, test_campaign, attempt=1, test_mode=True, settings=db_ready, dispatch=disp)
    row = db.get_call(call_id)
    assert row is not None
    assert row["test_mode"] == 1 and row["prospect_id"] is None and disp.metas[0].test_mode


async def test_launch_call_dry_run(db_ready: Settings, test_campaign: Campaign) -> None:
    db_ready.dry_run = True
    (pid,) = _add_prospects(test_campaign.id, 1)
    p = db.get_prospect(pid)
    assert p is not None
    call_id = await launch_call(p, test_campaign, attempt=1, now=MONDAY_11H, settings=db_ready,
                                dispatch=FakeDispatch())
    row = db.get_call(call_id)
    assert row is not None and row["status"] == "termine" and "simulation" in row["error"]
    pr = _prospect_row(pid)
    assert pr["status"] == "a_rappeler" and pr["attempts"] == 0


def _scheduler(settings: Settings, campaigns: list[Campaign], disp: FakeDispatch, **kw: Any) -> Scheduler:
    return Scheduler(settings=settings, dispatch=disp, campaign_loader=lambda: campaigns,
                     postcall_fn=_noop_postcall, launch_spacing_s=0, **kw)


async def _noop_postcall() -> dict[str, int]:
    return {}


async def test_tick_capacity_and_attempts(db_ready: Settings, test_campaign: Campaign) -> None:
    ids = _add_prospects(test_campaign.id, 5)
    with db.connect() as c:
        c.execute("UPDATE prospects SET attempts=1 WHERE id=?", (ids[0],))
    disp = FakeDispatch()
    s = _scheduler(db_ready, [test_campaign], disp)
    launched = await s.tick(MONDAY_11H.astimezone(UTC))
    assert len(launched) == 2  # max_concurrent_calls=2
    assert {m.prospect.id for m in disp.metas} <= set(ids)
    attempts = {m.prospect.id: m.attempt for m in disp.metas}
    if ids[0] in attempts:
        assert attempts[ids[0]] == 2
    # Capacité saturée : rien de plus
    assert await s.tick(MONDAY_11H.astimezone(UTC)) == []
    # Un appel se termine → une place
    db.update_call(launched[0], status="termine")
    assert len(await s.tick(MONDAY_11H.astimezone(UTC))) == 1


@pytest.mark.parametrize(
    ("when", "reason"),
    [
        (datetime(2026, 10, 5, 13, 0, tzinfo=PARIS), "hors plage"),
        (datetime(2026, 10, 5, 8, 0, tzinfo=PARIS), "hors plage"),
        (datetime(2026, 10, 10, 11, 0, tzinfo=PARIS), "week-end"),
        (datetime(2026, 11, 11, 11, 0, tzinfo=PARIS), "férié"),
        (datetime(2026, 5, 25, 11, 0, tzinfo=PARIS), "férié"),  # lundi de Pentecôte
    ],
)
async def test_tick_respects_windows_and_holidays(
    db_ready: Settings, test_campaign: Campaign, when: datetime, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    _add_prospects(test_campaign.id, 2)
    disp = FakeDispatch()
    s = _scheduler(db_ready, [test_campaign], disp)
    with caplog.at_level("INFO", logger="avp.planificateur"):
        assert await s.tick(when) == []
    assert disp.metas == []
    assert reason in caplog.text


async def test_tick_closed_days_file(db_ready: Settings, test_campaign: Campaign) -> None:
    _add_prospects(test_campaign.id, 1)
    (db_ready.data_dir / "jours_fermes.txt").write_text("2026-10-05\n", encoding="utf-8")
    disp = FakeDispatch()
    assert await _scheduler(db_ready, [test_campaign], disp).tick(MONDAY_11H) == []


async def test_tick_skips_optout_inactive_and_filtered(
    db_ready: Settings, test_campaign: Campaign, make_campaign: Callable[..., Campaign]
) -> None:
    other = make_campaign(id="autre")
    inactive = make_campaign(id="inactive", actif=False)
    ids = _add_prospects(test_campaign.id, 2)
    _add_prospects("autre", 1, start=50)
    _add_prospects("inactive", 1, start=60)
    p0 = db.get_prospect(ids[0])
    assert p0 is not None
    db.add_optout(p0.phone)
    disp = FakeDispatch()
    s = _scheduler(db_ready, [test_campaign, other, inactive], disp, campaign_ids=["test-ehpad", "inactive"])
    db_ready.max_concurrent_calls = 10
    await s.tick(MONDAY_11H)
    assert [m.prospect.id for m in disp.metas] == [ids[1]]


async def test_tick_dispatch_failure_requeues(db_ready: Settings, test_campaign: Campaign) -> None:
    (pid,) = _add_prospects(test_campaign.id, 1)
    s = _scheduler(db_ready, [test_campaign], FakeDispatch(fail=True))
    assert await s.tick(MONDAY_11H) == []
    pr = _prospect_row(pid)
    assert pr["status"] == "a_rappeler" and pr["attempts"] == 0
    assert db.count_active_calls() == 0
    # Pas relancé avant 15 min
    assert await s.tick(MONDAY_11H + timedelta(minutes=5)) == []


async def test_maintenance_stale(db_ready: Settings, test_campaign: Campaign) -> None:
    (pid,) = _add_prospects(test_campaign.id, 1)
    db.create_call("old", test_campaign.id, "+33562000001", "avp-old", pid)
    with db.connect() as c:
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        c.execute("UPDATE calls SET created_at=?", (old,))
        c.execute("UPDATE prospects SET status='en_cours', updated_at=?", (old,))
    s = _scheduler(db_ready, [], FakeDispatch())
    await s.maintenance(datetime.now(UTC))
    row = db.get_call("old")
    assert row is not None and row["status"] == "erreur"
    assert _prospect_row(pid)["status"] == "a_rappeler"


async def test_run_stops_and_runs_postcall(db_ready: Settings, test_campaign: Campaign) -> None:
    calls: list[int] = []

    async def postcall() -> dict[str, int]:
        calls.append(1)
        raise RuntimeError("panne Anthropic")  # jamais fatal

    s = Scheduler(settings=db_ready, dispatch=FakeDispatch(), campaign_loader=lambda: [test_campaign],
                  postcall_fn=postcall, tick_s=0.01, postcall_interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(s.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(calls) >= 2


async def test_busy_provider_used(db_ready: Settings, test_campaign: Campaign) -> None:
    _add_prospects(test_campaign.id, 1)
    disp = FakeDispatch()
    seen: list[datetime] = []

    def busy(now: datetime) -> list[tuple[datetime, datetime]]:
        seen.append(now)
        d = date(2026, 10, 7)
        return [(datetime(d.year, d.month, d.day, 0, 0, tzinfo=PARIS), datetime(d.year, d.month, d.day, 23, 0,
                                                                                tzinfo=PARIS))]

    await _scheduler(db_ready, [test_campaign], disp, busy_provider=busy).tick(MONDAY_11H)
    assert seen and date(2026, 10, 7) not in {s.date() for s in disp.metas[0].rdv_slots}


async def test_launch_does_not_override_worker_status(db_ready: Settings, test_campaign: Campaign) -> None:
    (pid,) = _add_prospects(test_campaign.id, 1)
    p = db.get_prospect(pid)
    assert p is not None

    async def fast_worker(meta: CallMetadata, settings: Any = None) -> str:
        db.update_call(meta.call_id, status="termine", outcome="non_decroche")
        return f"avp-{meta.call_id}"

    call_id = await launch_call(p, test_campaign, attempt=1, now=MONDAY_11H, settings=db_ready, dispatch=fast_worker)
    row = db.get_call(call_id)
    assert row is not None and row["status"] == "termine"
