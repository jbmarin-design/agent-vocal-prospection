"""Calcul des créneaux de rendez-vous proposés par l'agent (fonction pure).

Les créneaux sont calculés **au moment de la dispatch** et transmis au worker
dans `CallMetadata.rdv_slots` : l'agent ne propose que ces créneaux-là.

Règles (section `rdv` de la campagne) :
- au plus tôt `delai_min_jours` jours calendaires après maintenant, au plus tard
  `horizon_jours` jours après ;
- uniquement dans les plages `rdv.plages` (jours ISO + heures), les jours ouvrés
  hors fériés français (et hors fermetures `extra_closed`) ;
- un créneau de `duree_min` minutes doit tenir entièrement dans la plage ;
- débuts alignés sur le quart d'heure ;
- aucun chevauchement avec les intervalles `busy` ;
- répartition : un créneau par jour en priorité, en alternant matin et
  après-midi, autour d'heures « naturelles » (10h00 le matin, 14h30 l'après-midi).

Agenda de JB
------------
`busy` contient les occupations de JB lues dans Google Agenda (`agenda.BusyCache`, branché par
`avp run` et `avp call test` quand `avp google auth` a été fait). Les jours `rdv.jours_a_confirmer`
donnent des créneaux supplémentaires, proposés seulement si aucun créneau direct ne convient.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..models import Campaign
from .calendar_fr import is_business_day

QUARTER = timedelta(minutes=15)
# Heures préférées pour un créneau du matin / de l'après-midi.
PREFERRED_MORNING = time(10, 0)
PREFERRED_AFTERNOON = time(14, 30)
NOON = time(12, 0)


def _ceil_quarter(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    rem = dt.minute % 15
    return dt if rem == 0 else dt + timedelta(minutes=15 - rem)


def _overlaps(start: datetime, end: datetime, busy: Sequence[tuple[datetime, datetime]]) -> bool:
    for b_start, b_end in busy:
        bs = b_start if b_start.tzinfo else b_start.replace(tzinfo=UTC)
        be = b_end if b_end.tzinfo else b_end.replace(tzinfo=UTC)
        if start < be and bs < end:
            return True
    return False


def candidate_slots_for_day(
    campaign: Campaign,
    day: date,
    tz: ZoneInfo,
    earliest: datetime,
    busy: Sequence[tuple[datetime, datetime]] = (),
    days_override: Collection[int] | None = None,
) -> list[datetime]:
    """Tous les débuts possibles (au quart d'heure) pour un jour donné.

    `days_override` remplace les jours des plages (heures conservées) : sert aux jours « à confirmer ».
    """
    rdv = campaign.rdv
    duration = timedelta(minutes=rdv.duree_min)
    out: list[datetime] = []
    for window in rdv.plages:
        allowed = days_override if days_override is not None else window.jours
        if day.isoweekday() not in allowed:
            continue
        start = _ceil_quarter(datetime.combine(day, window.debut, tzinfo=tz))
        end_limit = datetime.combine(day, window.fin, tzinfo=tz)
        cur = start
        while cur + duration <= end_limit:
            if cur >= earliest and not _overlaps(cur, cur + duration, busy):
                out.append(cur)
            cur += QUARTER
    return sorted(set(out))


def _pick(candidates: list[datetime], afternoon: bool) -> datetime | None:
    half = [c for c in candidates if (c.time() >= NOON) == afternoon]
    if not half:
        return None
    target = PREFERRED_AFTERNOON if afternoon else PREFERRED_MORNING

    def dist(c: datetime) -> int:
        return abs((c.hour * 60 + c.minute) - (target.hour * 60 + target.minute))

    return min(half, key=lambda c: (dist(c), c))


def _choose(per_day: list[list[datetime]], count: int, duration: timedelta,
            taken: Sequence[datetime] = ()) -> list[datetime]:
    """Choisit `count` créneaux : un par jour en alternant matin / après-midi, puis complète."""
    chosen: list[datetime] = []

    def free(c: datetime) -> bool:
        others = [*taken, *chosen]
        return not _overlaps(c, c + duration, [(x, x + duration) for x in others])

    # 1er passage : un créneau par jour, en alternant matin / après-midi.
    afternoon = False  # on commence par un matin
    for cands in per_day:
        if len(chosen) >= count:
            break
        cands = [c for c in cands if free(c)]
        pick = _pick(cands, afternoon) or _pick(cands, not afternoon)
        if pick is not None:
            chosen.append(pick)
            afternoon = pick.time() < NOON  # le suivant dans l'autre demi-journée
    # 2e passage (peu de jours disponibles) : l'autre demi-journée des jours déjà retenus.
    for cands in per_day:
        if len(chosen) >= count:
            break
        halves = {c.time() >= NOON for c in chosen if c.date() == cands[0].date()}
        for half in (False, True):
            if len(chosen) < count and half not in halves:
                pick = _pick([c for c in cands if free(c)], half)
                if pick is not None:
                    chosen.append(pick)
    # 3e passage : n'importe quel créneau libre restant, au plus tôt.
    for cands in per_day:
        for c in cands:
            if len(chosen) >= count:
                break
            if free(c):
                chosen.append(c)
    return sorted(chosen)[:count]


def compute_rdv_slots(
    campaign: Campaign,
    now: datetime,
    n: int | None = None,
    busy: Sequence[tuple[datetime, datetime]] = (),
    timezone: str = "Europe/Paris",
    *,
    extra_closed: Collection[date] = (),
    include_confirm: bool = True,
) -> list[datetime]:
    """Retourne les créneaux proposables (datetimes avec fuseau `timezone`), triés.

    - au plus `n` créneaux **directs** (jours des `rdv.plages`), `n` valant par défaut
      `campaign.rdv.creneaux_proposes` ;
    - plus, si `include_confirm`, au plus `rdv.creneaux_a_confirmer` créneaux sur les jours
      `rdv.jours_a_confirmer` (à confirmer par JB : voir `RdvPolicy.needs_confirmation`).

    Liste vide si aucun créneau n'est possible dans l'horizon.
    """
    tz = ZoneInfo(timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_local = now.astimezone(tz)
    rdv = campaign.rdv
    count = rdv.creneaux_proposes if n is None else n
    if count <= 0:
        return []

    earliest = now_local + timedelta(days=rdv.delai_min_jours)
    last_day = (now_local + timedelta(days=rdv.horizon_jours)).date()
    duration = timedelta(minutes=rdv.duree_min)

    def per_day(days_override: Collection[int] | None) -> list[list[datetime]]:
        out: list[list[datetime]] = []
        d = earliest.date()
        while d <= last_day:
            if is_business_day(d, extra_closed=extra_closed):
                cands = candidate_slots_for_day(campaign, d, tz, earliest, busy, days_override)
                if cands:
                    out.append(cands)
            d += timedelta(days=1)
        return out

    chosen = _choose(per_day(None), count, duration)
    if include_confirm and rdv.jours_a_confirmer and rdv.creneaux_a_confirmer > 0:
        direct_days = {d for w in rdv.plages for d in w.jours}
        confirm_days = [d for d in rdv.jours_a_confirmer if d not in direct_days]
        if confirm_days:
            chosen += _choose(per_day(confirm_days), rdv.creneaux_a_confirmer, duration, taken=chosen)
    return sorted(chosen)
