"""Fixtures partagées (toutes opt-in : aucune n'est `autouse`, aucun effet de bord global).

- `settings`      : Settings pointant vers `tmp_path` (data, campagnes, prompts) ; `get_settings`
                    est monkeypatché et son cache vidé, pour que tout le code (db, campagnes…)
                    utilise ces chemins. La base n'est PAS initialisée.
- `db_ready`      : idem + base SQLite initialisée. Retourne les Settings.
- `test_campaign` : campagne de test construite en Python (sans YAML).
- `make_campaign` : fabrique de campagnes avec surcharges (`make_campaign(id="x", max_tentatives=2)`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from datetime import time
from pathlib import Path
from typing import Any

import pytest

from avp import config as avp_config
from avp.models import AxonautMapping, Campaign, Offer, Target, TimeWindow


def _campaign(**over: Any) -> Campaign:
    data: dict[str, Any] = {
        "id": "test-ehpad",
        "nom": "Campagne de test EHPAD",
        "cible": Target(interlocuteur="directeur ou directrice d'établissement", alternatives=["cadre de santé"]),
        "offre": Offer(resume="Interconnexion appel malade / antifugue avec la téléphonie XiVO"),
        "accroche": "Nous aidons les EHPAD du Gers à relier l'appel malade aux téléphones des soignants.",
        "plages": [
            TimeWindow(debut=time(10, 0), fin=time(12, 30)),
            TimeWindow(debut=time(14, 0), fin=time(17, 30)),
        ],
        "max_tentatives": 3,
        "delai_entre_tentatives_h": 48,
        "axonaut": AxonautMapping(pipe="EHPAD 2026", etape_initiale="À contacter"),
    }
    data.update(over)
    return Campaign(**data)


@pytest.fixture
def make_campaign() -> Callable[..., Campaign]:
    return _campaign


@pytest.fixture
def test_campaign() -> Campaign:
    return _campaign()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[avp_config.Settings]:
    (tmp_path / "campaigns").mkdir()
    (tmp_path / "prompts").mkdir()
    s = avp_config.Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        campaigns_dir=tmp_path / "campaigns",
        prompts_dir=tmp_path / "prompts",
        dry_run=False,
        max_concurrent_calls=2,
        timezone="Europe/Paris",
    )
    s.ensure_dirs()
    original = avp_config.get_settings
    original.cache_clear()

    def fake_get_settings() -> avp_config.Settings:
        return s

    fake_get_settings._avp_test_fake = True  # type: ignore[attr-defined]
    monkeypatch.setattr(avp_config, "get_settings", fake_get_settings)
    # Les modules qui ont fait `from .config import get_settings` gardent leur propre référence :
    # on la remplace aussi (y compris une référence à un faux d'un test précédent, si le module a été
    # importé pour la première fois pendant ce test-là).
    import avp.campaigns  # noqa: F401
    import avp.db  # noqa: F401

    for mod_name, mod in list(sys.modules.items()):
        if not (mod_name == "avp" or mod_name.startswith("avp.")) or mod is avp_config:
            continue
        ref = getattr(mod, "get_settings", None)
        if ref is original or getattr(ref, "_avp_test_fake", False):
            monkeypatch.setattr(mod, "get_settings", fake_get_settings)
    yield s
    original.cache_clear()


@pytest.fixture
def db_ready(settings: avp_config.Settings) -> avp_config.Settings:
    from avp import db

    db.init_db(settings.db_path)
    return settings
