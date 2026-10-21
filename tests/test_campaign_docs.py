"""Les modèles de docs/campagnes/ et les campagnes du dépôt restent valides."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from avp.campaigns import list_campaigns
from avp.models import Campaign
from avp.simulation import Scenario

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "campagnes"


def test_modele_campagne_valide():
    data = yaml.safe_load((DOCS / "modele-campagne.yaml").read_text(encoding="utf-8"))
    camp = Campaign.model_validate(data)
    assert not camp.actif  # un modèle copié ne doit jamais partir en appel par erreur


def test_modele_simulations_valide():
    data = yaml.safe_load((DOCS / "modele-simulations.yaml").read_text(encoding="utf-8"))
    scenarios = [Scenario.model_validate(s) for s in data["scenarios"]]
    assert len({s.id for s in scenarios}) == len(scenarios)


def test_campagnes_du_depot_valides():
    ids = {c.id for c in list_campaigns(ROOT / "campaigns")}
    assert "ehpad" in ids


@pytest.mark.parametrize("path", sorted((ROOT / "simulations").glob("*.yaml")), ids=lambda p: p.stem)
def test_simulations_du_depot_valides(path: Path):
    assert (ROOT / "campaigns" / path.name).exists(), "scénarios sans campagne du même nom"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    scenarios = [Scenario.model_validate(s) for s in data["scenarios"]]
    assert len({s.id for s in scenarios}) == len(scenarios)
