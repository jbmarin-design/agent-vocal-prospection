"""Tests de l'assemblage des prompts (src/avp/prompts.py) et des campagnes livrées."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from avp import prompts
from avp.campaigns import load_campaign
from avp.config import PROJECT_ROOT
from avp.models import Prospect

PROMPTS_DIR = PROJECT_ROOT / "prompts"
CAMPAIGNS_DIR = PROJECT_ROOT / "campaigns"
NOW = datetime(2026, 10, 5, 8, 32, tzinfo=UTC)  # lundi 5 octobre 2026, 10 h 32 à Paris


@pytest.fixture
def ehpad():
    return load_campaign("ehpad", CAMPAIGNS_DIR)


@pytest.fixture
def prospect():
    return Prospect(
        id=1, campaign_id="ehpad", name="EHPAD Les Tilleuls", phone="+33562000000", city="Auch",
        notes="Opportunité Axonaut : À contacter.",
    )


@pytest.mark.parametrize("cid", ["ehpad", "cabinets-medicaux", "echo"])
def test_campagnes_livrees_valides(cid):
    camp = load_campaign(cid, CAMPAIGNS_DIR)
    assert camp.id == cid
    assert camp.max_tentatives <= 4
    ids = [q.id for q in camp.qualification]
    assert len(ids) == len(set(ids)), "identifiants de questions en double"
    if camp.axonaut:
        assert camp.axonaut.pipe and camp.axonaut.etape_initiale


def test_format_slot_fr():
    assert prompts.format_slot_fr(datetime(2026, 10, 13, 8, 30, tzinfo=UTC)) == "mardi 13 octobre à 10 h 30"
    assert prompts.format_slot_fr(datetime(2026, 12, 1, 9, 0, tzinfo=UTC)) == "mardi 1er décembre à 10 h"


def test_opening_line_annonce_ia_et_enregistrement(ehpad, prospect):
    line = prompts.opening_line(ehpad, prospect)
    assert "intelligence artificielle" in line and "enregistré" in line
    assert "directeur" in line


def test_opening_line_test(prospect):
    echo = load_campaign("echo", CAMPAIGNS_DIR)
    line = prompts.opening_line(echo, prospect)
    assert "test technique" in line and "intelligence artificielle" in line


@pytest.mark.parametrize("role", ["accueil", "decideur", "repondeur"])
def test_build_instructions_couches(role, ehpad, prospect):
    slot = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    text = prompts.build_instructions(
        role, ehpad, prospect, now=NOW, attempt=2, rdv_slots=[slot], prompts_dir=PROMPTS_DIR
    )
    assert "{{" not in text, "marqueur non remplacé"
    assert "Règles non négociables" in text  # base.md
    assert "EHPAD Les Tilleuls" in text and "Auch" in text  # fiche
    assert "[appel_malade_systeme]" in text  # questions de la campagne
    assert "lundi 5 octobre 2026 à 10 h 32" in text  # contexte
    assert "Tentative d'appel n° 2" in text
    if role == "decideur":
        assert "jeudi 8 octobre à 14 h" in text and "2026-10-08T14:00:00+02:00" in text


def test_build_instructions_role_inconnu(ehpad, prospect):
    with pytest.raises(ValueError):
        prompts.build_instructions("vendeur", ehpad, prospect, now=NOW, prompts_dir=PROMPTS_DIR)  # type: ignore[arg-type]


def test_render_template_marqueur_inconnu():
    assert prompts.render_template("a {{x}} b {{inconnu}}", {"x": 1}) == "a 1 b "


def test_prompt_version_stable_et_sensible(tmp_path, ehpad):
    pdir = tmp_path / "prompts"
    cdir = tmp_path / "campaigns"
    pdir.mkdir()
    cdir.mkdir()
    for f in PROMPTS_DIR.glob("*.md"):
        (pdir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (cdir / "ehpad.yaml").write_text((CAMPAIGNS_DIR / "ehpad.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    v1 = prompts.prompt_version(ehpad, pdir, cdir)
    assert v1 == prompts.prompt_version(ehpad, pdir, cdir) and len(v1) == 12
    (pdir / "decideur.md").write_text("autre contenu", encoding="utf-8")
    v2 = prompts.prompt_version(ehpad, pdir, cdir)
    assert v2 != v1
    (cdir / "ehpad.yaml").write_text(
        (CAMPAIGNS_DIR / "ehpad.yaml").read_text(encoding="utf-8") + "\n# modif\n", encoding="utf-8"
    )
    assert prompts.prompt_version(ehpad, pdir, cdir) != v2


def test_transfert_indique_dans_le_contexte(ehpad, prospect):
    sans = prompts.build_instructions(
        "decideur", ehpad, prospect, now=NOW, prompts_dir=PROMPTS_DIR, transfer_available=False
    )
    avec = prompts.build_instructions(
        "decideur", ehpad, prospect, now=NOW, prompts_dir=PROMPTS_DIR, transfer_available=True
    )
    assert "Transfert vers un humain : indisponible" in sans
    assert "Transfert vers Jean-Baptiste Marin : disponible" in avec
