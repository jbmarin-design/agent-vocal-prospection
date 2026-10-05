"""Règles de suite à donner après un appel (fonction pure, partagée worker/orchestrateur).

Décide du nouveau statut du prospect et de la date de la prochaine tentative
en fonction de l'issue, de la campagne et du nombre de tentatives.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import Callback, CallOutcome, Campaign, ProspectStatus

# Délais de relance par défaut selon l'issue (heures), avant application des plages horaires.
RETRY_DELAYS_H: dict[CallOutcome, int] = {
    CallOutcome.NON_DECROCHE: 24,
    CallOutcome.OCCUPE: 2,
    CallOutcome.REPONDEUR: 48,
    CallOutcome.SVI: 72,
    CallOutcome.BARRAGE: 96,
    CallOutcome.ERREUR: 1,
}


def next_step(
    outcome: CallOutcome,
    campaign: Campaign,
    attempt: int,
    now: datetime,
    callback: Callback | None = None,
) -> tuple[ProspectStatus, datetime | None]:
    """Retourne (statut, prochaine tentative).

    - Opposition ou mauvais numéro : EXCLU, plus jamais rappelé.
    - Issue finale (refus, RDV, transfert, qualifié, non qualifié) : TERMINE.
    - Rappel convenu : A_RAPPELER à la date donnée (ou délai campagne si pas de date).
      Un rappel convenu ne consomme pas le quota de tentatives (géré par l'appelant).
    - Échec technique / non joint : A_RAPPELER après délai, TERMINE si quota atteint.
    """
    if outcome in (CallOutcome.OPPOSITION, CallOutcome.MAUVAIS_NUMERO):
        return ProspectStatus.EXCLU, None
    if outcome.is_final:
        return ProspectStatus.TERMINE, None
    if outcome is CallOutcome.RAPPEL:
        when = callback.when if callback and callback.when else None
        if when is None or when <= now:
            when = now + timedelta(hours=campaign.delai_entre_tentatives_h)
        return ProspectStatus.A_RAPPELER, when

    # Non joint / barrage / erreur
    if outcome is not CallOutcome.ERREUR and attempt >= campaign.max_tentatives:
        return ProspectStatus.TERMINE, None
    delay = RETRY_DELAYS_H.get(outcome, campaign.delai_entre_tentatives_h)
    delay = max(delay, 1) if outcome in (CallOutcome.OCCUPE, CallOutcome.ERREUR) else max(
        delay, campaign.delai_entre_tentatives_h // 2
    )
    return ProspectStatus.A_RAPPELER, now + timedelta(hours=delay)


def counts_as_attempt(outcome: CallOutcome) -> bool:
    """Un rappel convenu ou une erreur technique ne consomment pas une tentative."""
    return outcome not in (CallOutcome.RAPPEL, CallOutcome.ERREUR)
