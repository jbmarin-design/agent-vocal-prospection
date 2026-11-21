"""Réglages téléphoniques effectifs d'une campagne : son bloc `telephonie:`, sinon le `.env`.

Module pur (sans LiveKit), utilisé par le worker, l'orchestrateur et la CLI.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import Campaign


@dataclass(frozen=True)
class TelephonieEffective:
    trunk_sortant: str  # ID ST_xxx ; vide = non configuré
    numero_presente: str  # vide = numéro du trunk (on ne force rien dans la requête)
    transfert: str  # vide = transfert désactivé
    appels_simultanes_max: int  # plafond de la campagne, jamais au-dessus du plafond global

    @property
    def transfert_disponible(self) -> bool:
        return bool(self.transfert)


def telephonie_effective(campaign: Campaign, settings: Settings) -> TelephonieEffective:
    t = campaign.telephonie
    plafond = settings.max_concurrent_calls
    if t.appels_simultanes_max is not None:
        plafond = min(plafond, t.appels_simultanes_max)
    return TelephonieEffective(
        trunk_sortant=t.trunk_sortant or settings.sip_outbound_trunk_id,
        # Seul le numéro de la campagne est passé à LiveKit : sinon le trunk présente le sien.
        numero_presente=t.numero_presente,
        transfert=t.transfert or settings.transfer_target,
        appels_simultanes_max=plafond,
    )
