from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from avp.models import Campaign, RdvPolicy, TimeWindow
from avp.orchestrator.calendar_fr import is_business_day
from avp.orchestrator.slots import compute_rdv_slots

PARIS = ZoneInfo("Europe/Paris")


def _check_rules(camp: Campaign, now: datetime, slots: list[datetime]) -> None:
    rdv = camp.rdv
    for s in slots:
        assert s.tzinfo is not None
        assert s.minute % 15 == 0 and s.second == 0
        assert is_business_day(s.date())
        assert s >= now + timedelta(days=rdv.delai_min_jours)
        assert s.date() <= (now + timedelta(days=rdv.horizon_jours)).date()
        end = s + timedelta(minutes=rdv.duree_min)
        assert any(w.debut <= s.time() and end.time() <= w.fin and s.isoweekday() in w.jours for w in rdv.plages)
    assert slots == sorted(slots)


def test_default_three_slots_spread(test_campaign: Campaign) -> None:
    now = datetime(2026, 10, 5, 10, 17, tzinfo=PARIS)  # lundi
    slots = compute_rdv_slots(test_campaign, now)
    assert len(slots) == 3
    _check_rules(test_campaign, now, slots)
    assert len({s.date() for s in slots}) == 3  # trois jours différents
    halves = {s.time() >= time(12, 0) for s in slots}
    assert halves == {True, False}  # au moins un matin et un après-midi
    assert slots[0].date() == date(2026, 10, 7)  # J+2


def test_skips_holidays_and_weekend(test_campaign: Campaign) -> None:
    # Mardi 10/11/2026 + 2 j = jeudi 12/11 ; le 11/11 est férié de toute façon.
    now = datetime(2026, 11, 9, 9, 0, tzinfo=PARIS)
    slots = compute_rdv_slots(test_campaign, now, n=5)
    assert date(2026, 11, 11) not in {s.date() for s in slots}
    assert all(s.isoweekday() <= 5 for s in slots)
    _check_rules(test_campaign, now, slots)


def test_busy_is_respected(test_campaign: Campaign) -> None:
    now = datetime(2026, 10, 5, 8, 0, tzinfo=PARIS)
    free = compute_rdv_slots(test_campaign, now)
    first = free[0]
    busy = [(first - timedelta(minutes=10), first + timedelta(minutes=20))]
    slots = compute_rdv_slots(test_campaign, now, busy=busy)
    for s in slots:
        assert not (s < busy[0][1] and busy[0][0] < s + timedelta(minutes=30))
    # Journée entière occupée : aucun créneau ce jour-là
    day = first.date()
    busy_day = [(datetime.combine(day, time(0, 0), tzinfo=PARIS), datetime.combine(day, time(23, 59), tzinfo=PARIS))]
    assert day not in {s.date() for s in compute_rdv_slots(test_campaign, now, busy=busy_day)}


def test_quarter_alignment_with_odd_window(make_campaign: Callable[..., Campaign]) -> None:
    camp = make_campaign(rdv=RdvPolicy(
        duree_min=45, creneaux_proposes=4, delai_min_jours=1, horizon_jours=10,
        plages=[TimeWindow(debut=time(9, 7), fin=time(11, 0)), TimeWindow(debut=time(15, 10), fin=time(16, 0))],
    ))
    now = datetime(2026, 10, 5, 16, 3, tzinfo=PARIS)
    slots = compute_rdv_slots(camp, now)
    assert len(slots) == 4
    _check_rules(camp, now, slots)
    assert all(s.time() in {time(9, 15), time(9, 30), time(9, 45), time(10, 0), time(10, 15), time(15, 15)}
               for s in slots)


def test_short_horizon_fills_both_halves(make_campaign: Callable[..., Campaign]) -> None:
    camp = make_campaign(rdv=RdvPolicy(creneaux_proposes=3, delai_min_jours=0, horizon_jours=0))
    now = datetime(2026, 10, 5, 7, 0, tzinfo=PARIS)  # un seul jour possible
    slots = compute_rdv_slots(camp, now)
    assert len(slots) == 3
    assert {s.date() for s in slots} == {date(2026, 10, 5)}
    _check_rules(camp, now, slots)
    for a, b in zip(slots, slots[1:], strict=False):
        assert b - a >= timedelta(minutes=30)


def test_no_slot_and_n_zero(make_campaign: Callable[..., Campaign]) -> None:
    camp = make_campaign(rdv=RdvPolicy(delai_min_jours=0, horizon_jours=1))
    saturday = datetime(2026, 10, 10, 9, 0, tzinfo=PARIS)
    assert compute_rdv_slots(camp, saturday) == []
    assert compute_rdv_slots(camp, datetime(2026, 10, 5, 9, 0, tzinfo=PARIS), n=0) == []


def test_utc_input_and_timezone(test_campaign: Campaign) -> None:
    now_utc = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("UTC"))
    slots = compute_rdv_slots(test_campaign, now_utc, timezone="Europe/Paris")
    assert all(s.utcoffset() == timedelta(hours=2) for s in slots)  # heure d'été


def test_extra_closed(test_campaign: Campaign) -> None:
    now = datetime(2026, 10, 5, 8, 0, tzinfo=PARIS)
    slots = compute_rdv_slots(test_campaign, now, extra_closed={date(2026, 10, 7)})
    assert date(2026, 10, 7) not in {s.date() for s in slots}
