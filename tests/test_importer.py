from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from avp import db
from avp.config import Settings
from avp.models import Campaign
from avp.orchestrator import importer
from avp.orchestrator.importer import import_from_axonaut, import_from_csv, pick_decision_maker


def _fake_axonaut_module() -> types.ModuleType:
    """Remplace avp.axonaut (brique C) : mapping simple et prévisible."""
    m = types.ModuleType("fake_axonaut")

    def company_phone(company: dict, employees: list[dict] | None = None) -> str | None:
        if company.get("phone_number"):
            return company["phone_number"]
        for e in employees or []:
            if e.get("phone_number"):
                return e["phone_number"]
        return None

    def company_city(company: dict) -> str:
        return (company.get("address") or {}).get("city", "")

    m.company_phone = company_phone  # type: ignore[attr-defined]
    m.company_city = company_city  # type: ignore[attr-defined]
    return m


@pytest.fixture
def fake_ax_module(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _fake_axonaut_module()
    monkeypatch.setattr(importer, "load_module", lambda name: mod)


class FakeAxonaut:
    def __init__(self, opps: list[dict], companies: dict[int, dict], employees: dict[int, list[dict]]) -> None:
        self.opps, self.companies, self.employees = opps, companies, employees
        self.calls: list[tuple[str | None, str | None]] = []

    async def iter_opportunities(self, *, pipe_name: str | None = None, step_name: str | None = None) -> Any:
        self.calls.append((pipe_name, step_name))
        for o in self.opps:
            if pipe_name and o.get("pipe_name") != pipe_name:
                continue
            if step_name and o.get("pipe_step_name") != step_name:
                continue
            yield o

    async def get_company(self, company_id: int) -> dict:
        if company_id == 99:
            raise RuntimeError("HTTP 500")
        return self.companies[company_id]

    async def list_company_employees(self, company_id: int) -> list[dict]:
        return self.employees.get(company_id, [])


def _opp(i: int, company_id: int | None, name: str, step: str = "À contacter", comments: str = "") -> dict:
    return {
        "id": 1000 + i, "name": f"Opp {name}", "comments": comments, "pipe_name": "EHPAD 2026",
        "pipe_step_name": step, "company": {"id": company_id, "name": name} if company_id else None, "employees": [],
    }


def _fixture_axonaut() -> FakeAxonaut:
    opps = [
        _opp(1, 1, "EHPAD Les Tilleuls", comments="Vu au salon"),
        _opp(2, 2, "EHPAD Sans Tel"),
        _opp(3, 3, "EHPAD Doublon"),  # même numéro que 1
        _opp(4, 4, "EHPAD Opposé"),
        _opp(5, 1, "EHPAD Les Tilleuls"),  # même société, 2e opportunité
        _opp(6, None, "Orpheline"),
        _opp(7, 5, "EHPAD Autre étape", step="Gagné"),
        _opp(8, 6, "EHPAD Numéro faux"),
        _opp(9, 99, "EHPAD Erreur API"),
        _opp(10, 7, "EHPAD Via contact"),
    ]
    companies = {
        1: {"id": 1, "name": "EHPAD Les Tilleuls", "phone_number": "05 62 00 00 01", "address": {"city": "Auch"}},
        2: {"id": 2, "name": "EHPAD Sans Tel", "address": {"city": "Gimont"}},
        3: {"id": 3, "name": "EHPAD Doublon", "phone_number": "+33562000001"},
        4: {"id": 4, "name": "EHPAD Opposé", "phone_number": "0562000004"},
        5: {"id": 5, "name": "EHPAD Autre étape", "phone_number": "0562000005"},
        6: {"id": 6, "name": "EHPAD Numéro faux", "phone_number": "12"},
        7: {"id": 7, "name": "EHPAD Via contact", "address": {"city": "L'Isle-Jourdain"}},
    }
    employees = {
        1: [
            {"firstname": "Paul", "lastname": "Durand", "job": "Agent d'entretien"},
            {"firstname": "Claire", "lastname": "Martin", "job": "Directrice"},
            {"firstname": "Luc", "lastname": "Bernard", "job": "Cadre de santé"},
        ],
        7: [{"firstname": "Anne", "lastname": "Roy", "job": "Infirmière", "phone_number": "05 62 00 00 07"}],
    }
    return FakeAxonaut(opps, companies, employees)


def test_pick_decision_maker() -> None:
    assert pick_decision_maker([]) == ("", "")
    assert pick_decision_maker([{"firstname": "A", "lastname": "B", "job": "Comptable"}]) == ("", "")
    emps = [
        {"firstname": "Luc", "lastname": "B", "job": "Médecin coordonnateur"},
        {"firstname": "Éva", "lastname": "C", "function": "DIRECTRICE ADJOINTE"},
    ]
    assert pick_decision_maker(emps) == ("Éva C", "DIRECTRICE ADJOINTE")
    assert pick_decision_maker([{"name": "M. le Maire", "job": "Maire"}]) == ("M. le Maire", "Maire")


async def test_import_axonaut(db_ready: Settings, test_campaign: Campaign, fake_ax_module: None) -> None:
    db.add_optout("+33562000004", "demande", "test")
    ax = _fixture_axonaut()
    rep = await import_from_axonaut(test_campaign, axonaut=ax)
    assert ax.calls == [("EHPAD 2026", "À contacter")]
    assert rep.crees == ["EHPAD Les Tilleuls (+33562000001)", "EHPAD Via contact (+33562000007)"]
    reasons = dict(rep.ignores)
    assert "aucun numéro" in reasons["EHPAD Sans Tel"]
    assert "doublon" in reasons["EHPAD Doublon (+33562000001)"]
    assert "opposition" in reasons["EHPAD Opposé (+33562000004)"]
    assert "plusieurs opportunités" in reasons["EHPAD Les Tilleuls"]
    assert "sans société" in reasons["Opp Orpheline"]
    assert "invalide" in reasons["EHPAD Numéro faux"]
    assert len(rep.erreurs) == 1 and "HTTP 500" in rep.erreurs[0]
    assert "EHPAD Autre étape" not in rep.summary()

    with db.connect() as c:
        rows = {r["phone"]: dict(r) for r in c.execute("SELECT * FROM prospects")}
    p = rows["+33562000001"]
    assert p["campaign_id"] == "test-ehpad"
    assert p["city"] == "Auch"
    assert (p["contact_name"], p["contact_role"]) == ("Claire Martin", "Directrice")
    assert p["axonaut_company_id"] == 1 and p["axonaut_opportunity_id"] == 1001
    assert "Vu au salon" in p["notes"] and "À contacter" in p["notes"]
    assert rows["+33562000007"]["city"] == "L'Isle-Jourdain"

    # Second import : mise à jour, pas de doublon en base
    rep2 = await import_from_axonaut(test_campaign, axonaut=_fixture_axonaut())
    assert rep2.crees == [] and len(rep2.mis_a_jour) == 2


async def test_import_axonaut_all_steps_limit_dry_run(db_ready: Settings, test_campaign: Campaign,
                                                      fake_ax_module: None) -> None:
    ax = _fixture_axonaut()
    rep = await import_from_axonaut(test_campaign, axonaut=ax, step_name="*", limit=1, dry_run=True)
    assert ax.calls == [("EHPAD 2026", None)]
    assert rep.dry_run and rep.crees == ["EHPAD Les Tilleuls (+33562000001)"]
    assert db.campaign_stats(test_campaign.id) == {}
    assert rep.summary().startswith("[simulation]")


async def test_import_axonaut_requires_pipe(db_ready: Settings, make_campaign: Any, fake_ax_module: None) -> None:
    with pytest.raises(ValueError, match="pipeline"):
        await import_from_axonaut(make_campaign(axonaut=None), axonaut=_fixture_axonaut())


def test_import_csv_semicolon_bom(db_ready: Settings, test_campaign: Campaign, tmp_path: Path) -> None:
    db.add_optout("+33562000009")
    f = tmp_path / "p.csv"
    f.write_text(
        "﻿Nom;Téléphone;Ville;Contact;Fonction;axonaut_company_id;Notes\n"
        "Mairie de Gimont;05 62 67 70 02;Gimont;M. Dupont;Maire;123;Projet fibre\n"
        "Cabinet A;0562000002;Auch;;;;\n"
        "Cabinet doublon;+33 5 62 00 00 02;Auch;;;;\n"
        ";0562000003;;;;;\n"
        "Cabinet B;pas un numéro;;;;;\n"
        "Cabinet opposé;05 62 00 00 09;;;;;\n"
        "Cabinet C;0562000010;;;;abc;\n"
        ";;;;;;\n",
        encoding="utf-8",
    )
    rep = import_from_csv(test_campaign, f)
    assert len(rep.crees) == 2
    reasons = [r for _, r in rep.ignores]
    assert any("doublon" in r for r in reasons)
    assert any("nom manquant" in r for r in reasons)
    assert any("invalide" in r for r in reasons)
    assert any("opposition" in r for r in reasons)
    assert len(rep.erreurs) == 1 and "non numérique" in rep.erreurs[0]
    with db.connect() as c:
        r = dict(c.execute("SELECT * FROM prospects WHERE phone='+33562677002'").fetchone())
    assert r["name"] == "Mairie de Gimont" and r["contact_role"] == "Maire"
    assert r["axonaut_company_id"] == 123 and r["notes"] == "Projet fibre"


def test_import_csv_comma_and_errors(db_ready: Settings, test_campaign: Campaign, tmp_path: Path) -> None:
    f = tmp_path / "c.csv"
    f.write_text('nom,telephone,ville\n"EHPAD, Le Parc",0562000011,Auch\n', encoding="utf-8")
    rep = import_from_csv(test_campaign, f, dry_run=True)
    assert rep.crees == ["EHPAD, Le Parc (+33562000011)"]
    assert db.campaign_stats(test_campaign.id) == {}

    bad = tmp_path / "bad.csv"
    bad.write_text("raison;ville\nX;Y\n", encoding="utf-8")
    assert "colonnes obligatoires" in import_from_csv(test_campaign, bad).erreurs[0]

    latin = tmp_path / "latin.csv"
    latin.write_bytes("nom;telephone\nMairie d'Aubiet é;0562000012\n".encode("latin-1"))
    assert "UTF-8" in import_from_csv(test_campaign, latin).erreurs[0]
