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

Point d'extension `busy` — agenda de JB
---------------------------------------
Aujourd'hui `busy` est vide par défaut. Pour éviter de proposer un créneau où JB
est déjà pris, il suffira de fournir la liste de ses occupations
(`[(début, fin), ...]`, datetimes avec fuseau), par exemple lue depuis Google
Agenda (API `freebusy.query`) ou un export ICS, et de la passer à
`compute_rdv_slots(..., busy=...)`. Le planificateur accepte pour cela un
`busy_provider` (voir `Scheduler`), appelé avant chaque lancement d'appel.
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
) -> list[datetime]:
    """Tous les débuts possibles (au quart d'heure) pour un jour donné."""
    rdv = campaign.rdv
    duration = timedelta(minutes=rdv.duree_min)
    out: list[datetime] = []
    for window in rdv.plages:
        if day.isoweekday() not in window.jours:
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


def compute_rdv_slots(
    campaign: Campaign,
    now: datetime,
    n: int | None = None,
    busy: Sequence[tuple[datetime, datetime]] = (),
    timezone: str = "Europe/Paris",
    *,
    extra_closed: Collection[date] = (),
) -> list[datetime]:
    """Retourne au plus `n` créneaux (datetimes avec fuseau `timezone`), triés.

    `n` vaut par défaut `campaign.rdv.creneaux_proposes`. Liste vide si aucun
    créneau n'est possible dans l'horizon.
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

    per_day: list[list[datetime]] = []
    d = earliest.date()
    while d <= last_day:
        if is_business_day(d, extra_closed=extra_closed):
            cands = candidate_slots_for_day(campaign, d, tz, earliest, busy)
            if cands:
                per_day.append(cands)
        d += timedelta(days=1)

    duration = timedelta(minutes=rdv.duree_min)
    chosen: list[datetime] = []

    def free(c: datetime) -> bool:
        return not _overlaps(c, c + duration, [(x, x + duration) for x in chosen])

    # 1er passage : un créneau par jour, en alternant matin / après-midi.
    afternoon = False  # on commence par un matin
    for cands in per_day:
        if len(chosen) >= count:
            break
        pick = _pick(cands, afternoon) or _pick(cands, not afternoon)
        if pick is not None:
            chosen.append(pick)
            afternoon = pick.time() < NOON  # le suivant dans l'autre demi-journée
    # 2e passage (peu de jours disponibles) : l'autre demi-journée des jours déjà retenus.
    for cands in per_day:
        if len(chosen) >= count:
            break
        taken = {c.time() >= NOON for c in chosen if c.date() == cands[0].date()}
        for half in (False, True):
            if len(chosen) < count and half not in taken:
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
