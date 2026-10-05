"""Tests de `avp.outcomes.next_step` (règles de suite après un appel)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from avp.models import Callback, CallOutcome, Campaign, ProspectStatus
from avp.outcomes import RETRY_DELAYS_H, counts_as_attempt, next_step

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("outcome", [CallOutcome.OPPOSITION, CallOutcome.MAUVAIS_NUMERO])
def test_excluded(outcome: CallOutcome, test_campaign: Campaign) -> None:
    assert next_step(outcome, test_campaign, 1, NOW) == (ProspectStatus.EXCLU, None)


@pytest.mark.parametrize(
    "outcome", [CallOutcome.REFUS, CallOutcome.RDV, CallOutcome.TRANSFERT, CallOutcome.QUALIFIE,
                CallOutcome.NON_QUALIFIE]
)
def test_final(outcome: CallOutcome, test_campaign: Campaign) -> None:
    assert next_step(outcome, test_campaign, 1, NOW) == (ProspectStatus.TERMINE, None)


def test_callback_with_date(test_campaign: Campaign) -> None:
    when = NOW + timedelta(days=3)
    assert next_step(CallOutcome.RAPPEL, test_campaign, 3, NOW, Callback(when=when)) == (
        ProspectStatus.A_RAPPELER, when)


@pytest.mark.parametrize("cb", [None, Callback(), Callback(when=NOW - timedelta(hours=1))])
def test_callback_without_valid_date(cb: Callback | None, test_campaign: Campaign) -> None:
    status, when = next_step(CallOutcome.RAPPEL, test_campaign, 1, NOW, cb)
    assert status is ProspectStatus.A_RAPPELER
    assert when == NOW + timedelta(hours=test_campaign.delai_entre_tentatives_h)


def test_retry_delays(test_campaign: Campaign) -> None:
    half = test_campaign.delai_entre_tentatives_h // 2  # 24 h
    assert next_step(CallOutcome.OCCUPE, test_campaign, 1, NOW) == (ProspectStatus.A_RAPPELER, NOW + timedelta(hours=2))
    assert next_step(CallOutcome.ERREUR, test_campaign, 1, NOW) == (ProspectStatus.A_RAPPELER, NOW + timedelta(hours=1))
    st, when = next_step(CallOutcome.NON_DECROCHE, test_campaign, 1, NOW)
    assert when == NOW + timedelta(hours=max(RETRY_DELAYS_H[CallOutcome.NON_DECROCHE], half))
    st, when = next_step(CallOutcome.BARRAGE, test_campaign, 2, NOW)
    assert st is ProspectStatus.A_RAPPELER and when == NOW + timedelta(hours=96)


def test_minimum_delay_follows_campaign(make_campaign: Callable[..., Campaign]) -> None:
    camp = make_campaign(delai_entre_tentatives_h=120)
    _, when = next_step(CallOutcome.NON_DECROCHE, camp, 1, NOW)
    assert when == NOW + timedelta(hours=60)


def test_quota_reached(test_campaign: Campaign) -> None:
    n = test_campaign.max_tentatives
    assert next_step(CallOutcome.NON_DECROCHE, test_campaign, n, NOW) == (ProspectStatus.TERMINE, None)
    assert next_step(CallOutcome.REPONDEUR, test_campaign, n + 1, NOW) == (ProspectStatus.TERMINE, None)
    # Une erreur technique ne clôt jamais le prospect
    assert next_step(CallOutcome.ERREUR, test_campaign, n, NOW)[0] is ProspectStatus.A_RAPPELER


def test_counts_as_attempt() -> None:
    assert not counts_as_attempt(CallOutcome.RAPPEL)
    assert not counts_as_attempt(CallOutcome.ERREUR)
    assert counts_as_attempt(CallOutcome.NON_DECROCHE)
    assert counts_as_attempt(CallOutcome.REFUS)


def test_outcome_properties() -> None:
    assert CallOutcome.RDV.is_final and not CallOutcome.RAPPEL.is_final
    assert CallOutcome.BARRAGE.reached_human and not CallOutcome.REPONDEUR.reached_human
