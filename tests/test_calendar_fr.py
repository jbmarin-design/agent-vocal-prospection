from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from avp.orchestrator.calendar_fr import (
    business_days,
    easter_sunday,
    holiday_name,
    holidays,
    is_business_day,
    is_holiday,
    load_closed_days,
    next_business_day,
)


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)),
     (2027, date(2027, 3, 28)), (2038, date(2038, 4, 25)), (2000, date(2000, 4, 23))],
)
def test_easter(year: int, expected: date) -> None:
    assert easter_sunday(year) == expected


def test_holidays_2026() -> None:
    h = holidays(2026)
    assert len(h) == 11
    assert h[date(2026, 4, 6)] == "Lundi de Pâques"
    assert h[date(2026, 5, 14)] == "Ascension"
    assert h[date(2026, 5, 25)] == "Lundi de Pentecôte"
    for d in (date(2026, 1, 1), date(2026, 5, 1), date(2026, 5, 8), date(2026, 7, 14), date(2026, 8, 15),
              date(2026, 11, 1), date(2026, 11, 11), date(2026, 12, 25)):
        assert is_holiday(d)


def test_options() -> None:
    assert not is_holiday(date(2026, 5, 25), pentecost_monday=False)
    assert is_holiday(date(2026, 4, 3), alsace_moselle=True)  # Vendredi saint
    assert holiday_name(date(2026, 12, 26), alsace_moselle=True) == "Saint-Étienne"
    assert not is_holiday(date(2026, 12, 26))


def test_business_days() -> None:
    assert is_business_day(date(2026, 10, 5))  # lundi
    assert not is_business_day(date(2026, 10, 10))  # samedi
    assert not is_business_day(date(2026, 11, 11))  # mercredi férié
    assert not is_business_day(date(2026, 10, 6), extra_closed={date(2026, 10, 6)})
    assert next_business_day(date(2026, 11, 10)) == date(2026, 11, 12)
    assert next_business_day(date(2026, 12, 24)) == date(2026, 12, 28)
    assert list(business_days(date(2026, 5, 11), date(2026, 5, 15))) == [
        date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 15)
    ]


def test_load_closed_days(tmp_path: Path) -> None:
    f = tmp_path / "jours_fermes.txt"
    assert load_closed_days(f) == set()
    f.write_text("# congés\n2026-08-10..2026-08-12\n\n2026-12-24  # réveillon\n", encoding="utf-8")
    assert load_closed_days(f) == {date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12), date(2026, 12, 24)}
    f.write_text("24/12/2026\n", encoding="utf-8")
    with pytest.raises(ValueError, match="AAAA-MM-JJ"):
        load_closed_days(f)
