"""Jours fériés français et jours ouvrés (fonctions pures, sans dépendance externe).

Jours fériés légaux en France métropolitaine (Code du travail, art. L3133-1) :
1er janvier, lundi de Pâques, 1er mai, 8 mai, Ascension (Pâques + 39 j),
lundi de Pentecôte (Pâques + 50 j), 14 juillet, 15 août, 1er novembre,
11 novembre, 25 décembre.

- Le lundi de Pentecôte est souvent la « journée de solidarité » (beaucoup
  d'établissements travaillent). Par prudence, il est **considéré comme férié**
  par défaut : on n'appelle pas ce jour-là. Paramètre `pentecost_monday=False`
  pour l'ignorer.
- Alsace-Moselle : Vendredi saint et 26 décembre en plus (`alsace_moselle=True`).
- Fermetures propres à OpteoLink (congés, ponts) : fichier texte optionnel
  (une date AAAA-MM-JJ par ligne, `#` pour les commentaires), voir
  `load_closed_days()` ; le planificateur lit `data/jours_fermes.txt`.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path


def easter_sunday(year: int) -> date:
    """Dimanche de Pâques (calendrier grégorien, algorithme de Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=64)
def _holidays_cached(year: int, pentecost_monday: bool, alsace_moselle: bool) -> dict[date, str]:
    easter = easter_sunday(year)
    days: dict[date, str] = {
        date(year, 1, 1): "Jour de l'an",
        easter + timedelta(days=1): "Lundi de Pâques",
        date(year, 5, 1): "Fête du Travail",
        date(year, 5, 8): "Victoire 1945",
        easter + timedelta(days=39): "Ascension",
        date(year, 7, 14): "Fête nationale",
        date(year, 8, 15): "Assomption",
        date(year, 11, 1): "Toussaint",
        date(year, 11, 11): "Armistice 1918",
        date(year, 12, 25): "Noël",
    }
    if pentecost_monday:
        days[easter + timedelta(days=50)] = "Lundi de Pentecôte"
    if alsace_moselle:
        days[easter - timedelta(days=2)] = "Vendredi saint"
        days[date(year, 12, 26)] = "Saint-Étienne"
    return days


def holidays(year: int, *, pentecost_monday: bool = True, alsace_moselle: bool = False) -> dict[date, str]:
    """Jours fériés de l'année : {date: libellé}, triés par date."""
    return dict(sorted(_holidays_cached(year, pentecost_monday, alsace_moselle).items()))


def holiday_name(d: date, *, pentecost_monday: bool = True, alsace_moselle: bool = False) -> str | None:
    """Libellé du jour férié, ou None si `d` n'est pas férié."""
    return _holidays_cached(d.year, pentecost_monday, alsace_moselle).get(d)


def is_holiday(d: date, *, pentecost_monday: bool = True, alsace_moselle: bool = False) -> bool:
    """Vrai si `d` est un jour férié français."""
    return holiday_name(d, pentecost_monday=pentecost_monday, alsace_moselle=alsace_moselle) is not None


def is_business_day(
    d: date,
    *,
    extra_closed: Collection[date] = (),
    pentecost_monday: bool = True,
    alsace_moselle: bool = False,
) -> bool:
    """Jour ouvré : du lundi au vendredi, hors férié et hors fermeture (`extra_closed`)."""
    if d.isoweekday() > 5:
        return False
    if d in extra_closed:
        return False
    return not is_holiday(d, pentecost_monday=pentecost_monday, alsace_moselle=alsace_moselle)


def next_business_day(d: date, *, extra_closed: Collection[date] = ()) -> date:
    """Premier jour ouvré strictement après `d`."""
    n = d + timedelta(days=1)
    while not is_business_day(n, extra_closed=extra_closed):
        n += timedelta(days=1)
    return n


def business_days(start: date, end: date, *, extra_closed: Collection[date] = ()) -> Iterable[date]:
    """Jours ouvrés de `start` à `end` inclus."""
    d = start
    while d <= end:
        if is_business_day(d, extra_closed=extra_closed):
            yield d
        d += timedelta(days=1)


def load_closed_days(path: Path) -> set[date]:
    """Lit un fichier de fermetures (une date AAAA-MM-JJ par ligne, ou une plage AAAA-MM-JJ..AAAA-MM-JJ).

    Les lignes vides et ce qui suit `#` sont ignorés. Fichier absent : ensemble vide.
    Une ligne invalide lève ValueError (mieux vaut une erreur visible qu'un appel un jour de congé).
    """
    if not path.exists():
        return set()
    out: set[date] = set()
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            if ".." in line:
                a, b = (date.fromisoformat(x.strip()) for x in line.split("..", 1))
                d = a
                while d <= b:
                    out.add(d)
                    d += timedelta(days=1)
            else:
                out.add(date.fromisoformat(line))
        except ValueError as e:
            raise ValueError(f"{path}:{n} : date invalide {raw!r} (format attendu AAAA-MM-JJ)") from e
    return out
