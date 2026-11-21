"""Bloc `telephonie:` des campagnes : valeurs propres, sinon le `.env`."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from avp import config as avp_config
from avp.campaigns import list_campaigns
from avp.config import PROJECT_ROOT
from avp.models import Campaign, Telephonie
from avp.telephonie import telephonie_effective


def _settings(**over: object) -> avp_config.Settings:
    data: dict[str, object] = {
        "sip_outbound_trunk_id": "ST_defaut",
        "sip_caller_number": "+33500000000",
        "transfer_target": "sip:1001@xivo",
        "max_concurrent_calls": 4,
    }
    data.update(over)
    return avp_config.Settings(_env_file=None, **data)  # type: ignore[arg-type]


def test_sans_bloc_le_env_s_applique(test_campaign: Campaign) -> None:
    t = telephonie_effective(test_campaign, _settings())
    assert t.trunk_sortant == "ST_defaut"
    assert t.numero_presente == ""  # LiveKit garde le numéro du trunk
    assert t.transfert == "sip:1001@xivo" and t.transfert_disponible
    assert t.appels_simultanes_max == 4


def test_le_bloc_de_la_campagne_prime(make_campaign: Callable[..., Campaign]) -> None:
    camp = make_campaign(telephonie=Telephonie(
        trunk_sortant="ST_mairies", numero_presente="+33562000000",
        transfert="tel:+33600000000", appels_simultanes_max=1,
    ))
    t = telephonie_effective(camp, _settings())
    assert (t.trunk_sortant, t.numero_presente, t.transfert, t.appels_simultanes_max) == (
        "ST_mairies", "+33562000000", "tel:+33600000000", 1,
    )


def test_plafond_de_campagne_borne_par_le_plafond_global(make_campaign: Callable[..., Campaign]) -> None:
    camp = make_campaign(telephonie=Telephonie(appels_simultanes_max=10))
    assert telephonie_effective(camp, _settings(max_concurrent_calls=2)).appels_simultanes_max == 2


def test_transfert_desactive_si_rien_nulle_part(test_campaign: Campaign) -> None:
    assert not telephonie_effective(test_campaign, _settings(transfer_target="")).transfert_disponible


@pytest.mark.parametrize(
    "champs",
    [
        {"trunk_sortant": "trunk-xivo"},
        {"numero_presente": "0562000000"},
        {"transfert": "1001"},
        {"appels_simultanes_max": 0},
    ],
)
def test_valeurs_refusees(champs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Telephonie.model_validate(champs)


def test_campagnes_du_depot_inchangees() -> None:
    """Les campagnes versionnées n'ont pas (encore) de bloc : tout vient du `.env`."""
    for camp in list_campaigns(PROJECT_ROOT / "campaigns"):
        assert camp.telephonie == Telephonie(), camp.id
