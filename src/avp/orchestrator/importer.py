"""Import des prospects dans la base locale : depuis Axonaut (pipeline d'opportunités) ou depuis un CSV.

Règles communes :
- numéro normalisé en E.164 (`normalize_phone`) ; numéro absent ou invalide → ignoré (raison listée) ;
- dédoublonnage par numéro dans un même import (premier gardé) ; un numéro déjà en base pour la
  campagne est **mis à jour** (fiche), sans toucher au statut ni aux tentatives (`db.upsert_prospect`) ;
- numéro présent dans la liste d'opposition → ignoré ;
- `dry_run=True` : rien n'est écrit, le rapport indique ce qui serait créé / mis à jour.
"""

from __future__ import annotations

import csv
import io
import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db
from ..models import Campaign, Prospect
from ..phone import InvalidPhoneNumber, normalize_phone
from . import load_module

log = logging.getLogger("avp.import")

# Fonctions recherchées pour le contact décideur, par ordre de priorité (comparaison sans accents).
DECISION_ROLE_KEYWORDS: tuple[str, ...] = (
    "directeur", "directrice", "direction",
    "gerant", "gerante", "president", "presidente", "dirigeant", "dirigeante",
    "maire", "dgs", "secretaire general", "secretaire de mairie",
    "dsi", "informatique", "systemes d'information",
    "responsable administratif", "responsable administrative", "administrateur", "administratrice",
    "medecin coordonnateur", "cadre de sante", "medecin", "associe", "associee",
    "responsable",
)

# Valeur spéciale de `step_name` : toutes les étapes du pipeline.
ALL_STEPS = "*"


@dataclass
class ImportReport:
    """Bilan d'un import. Les listes contiennent des libellés lisibles (nom + numéro)."""

    source: str = ""
    dry_run: bool = False
    crees: list[str] = field(default_factory=list)
    mis_a_jour: list[str] = field(default_factory=list)
    ignores: list[tuple[str, str]] = field(default_factory=list)  # (libellé, raison)
    erreurs: list[str] = field(default_factory=list)

    @property
    def total_traite(self) -> int:
        return len(self.crees) + len(self.mis_a_jour) + len(self.ignores) + len(self.erreurs)

    def summary(self) -> str:
        prefix = "[simulation] " if self.dry_run else ""
        lines = [
            f"{prefix}Import {self.source} : {len(self.crees)} créé(s), {len(self.mis_a_jour)} mis à jour, "
            f"{len(self.ignores)} ignoré(s), {len(self.erreurs)} erreur(s)."
        ]
        if self.ignores:
            lines.append("Ignorés :")
            lines += [f"  - {label} : {reason}" for label, reason in self.ignores]
        if self.erreurs:
            lines.append("Erreurs :")
            lines += [f"  - {e}" for e in self.erreurs]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outils communs
# ---------------------------------------------------------------------------


def _fold(s: str) -> str:
    """Minuscules sans accents, espaces normalisés."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.lower().replace("_", " ").split())


def _role_rank(role: str) -> int | None:
    folded = _fold(role)
    if not folded:
        return None
    for i, kw in enumerate(DECISION_ROLE_KEYWORDS):
        if kw in folded:
            return i
    return None


def _employee_name(emp: dict[str, Any]) -> str:
    first = emp.get("firstname") or emp.get("first_name") or ""
    last = emp.get("lastname") or emp.get("last_name") or ""
    civ = emp.get("civility") or ""
    name = " ".join(x for x in (str(first).strip(), str(last).strip()) if x)
    if not name:
        name = str(emp.get("name") or emp.get("fullname") or "").strip()
    if name and civ and not first:
        name = f"{civ} {name}"
    return name


def _employee_role(emp: dict[str, Any]) -> str:
    for key in ("job", "function", "fonction", "position", "role", "title"):
        v = emp.get(key)
        if v:
            return str(v).strip()
    return ""


def pick_decision_maker(employees: list[dict[str, Any]] | None) -> tuple[str, str]:
    """Choisit le contact dont la fonction ressemble le plus à un décideur. Retourne (nom, fonction)."""
    best: tuple[int, str, str] | None = None
    for emp in employees or []:
        if emp.get("is_deleted") or emp.get("deleted"):
            continue
        role = _employee_role(emp)
        rank = _role_rank(role)
        name = _employee_name(emp)
        if rank is None or not name:
            continue
        if best is None or rank < best[0]:
            best = (rank, name, role)
    return (best[1], best[2]) if best else ("", "")


def _existing_phones(campaign_id: str) -> set[str]:
    with db.connect() as c:
        return {r[0] for r in c.execute("SELECT phone FROM prospects WHERE campaign_id=?", (campaign_id,))}


class _Collector:
    """Accumule les prospects d'un import en appliquant dédoublonnage et opposition."""

    def __init__(self, campaign: Campaign, report: ImportReport, dry_run: bool) -> None:
        self.campaign = campaign
        self.report = report
        self.dry_run = dry_run
        self.seen: set[str] = set()
        self.existing = _existing_phones(campaign.id)

    def add(self, prospect: Prospect) -> None:
        label = f"{prospect.name} ({prospect.phone})"
        if prospect.phone in self.seen:
            self.report.ignores.append((label, "doublon (numéro déjà présent dans cet import)"))
            return
        self.seen.add(prospect.phone)
        if db.is_opted_out(prospect.phone):
            self.report.ignores.append((label, "numéro dans la liste d'opposition"))
            return
        if self.dry_run:
            (self.report.mis_a_jour if prospect.phone in self.existing else self.report.crees).append(label)
            return
        try:
            _, created = db.upsert_prospect(prospect)
        except Exception as e:  # erreur SQLite inattendue : on continue l'import
            self.report.erreurs.append(f"{label} : {e}")
            return
        (self.report.crees if created else self.report.mis_a_jour).append(label)


# ---------------------------------------------------------------------------
# Import Axonaut
# ---------------------------------------------------------------------------


def _named(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name")
    return str(value or "").strip()


def _opp_pipe(opp: dict[str, Any]) -> str:
    return _named(opp.get("pipe_name") or opp.get("pipe"))


def _opp_step(opp: dict[str, Any]) -> str:
    return _named(opp.get("pipe_step_name") or opp.get("pipe_step") or opp.get("step"))


def _opp_company(opp: dict[str, Any]) -> tuple[int | None, str]:
    comp = opp.get("company")
    if isinstance(comp, dict):
        cid, name = comp.get("id"), comp.get("name") or ""
    else:
        cid, name = opp.get("company_id"), opp.get("company_name") or ""
    try:
        return (int(cid) if cid not in (None, "") else None), str(name)
    except (TypeError, ValueError):
        return None, str(name)


def _opportunity_notes(opp: dict[str, Any]) -> str:
    parts: list[str] = []
    name = str(opp.get("name") or "").strip()
    step = _opp_step(opp)
    pipe = _opp_pipe(opp)
    if name:
        parts.append(f"Opportunité Axonaut : {name}")
    if pipe or step:
        parts.append(f"Pipeline « {pipe} », étape « {step} »")
    comments = str(opp.get("comments") or "").strip()
    if comments:
        parts.append(f"Commentaires : {comments}")
    return "\n".join(parts)


async def import_from_axonaut(
    campaign: Campaign,
    *,
    axonaut: Any,
    pipe_name: str | None = None,
    step_name: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Importe les opportunités d'un pipeline Axonaut comme prospects de la campagne.

    - `axonaut` : instance ouverte d'`AxonautClient` (ou faux client en test).
    - `pipe_name` : par défaut `campaign.axonaut.pipe`.
    - `step_name` : par défaut `campaign.axonaut.etape_initiale` (ex. « À contacter ») ;
      `"*"` pour toutes les étapes.
    - `limit` : nombre maximal d'opportunités examinées.
    """
    ax_mod = load_module("axonaut")
    mapping = campaign.axonaut
    pipe = pipe_name or (mapping.pipe if mapping else None)
    if not pipe:
        raise ValueError(
            f"campagne {campaign.id} : aucun pipeline Axonaut (section `axonaut.pipe` du YAML ou option --pipe)"
        )
    step: str | None = step_name if step_name is not None else (mapping.etape_initiale if mapping else None)
    if step == ALL_STEPS:
        step = None

    report = ImportReport(source=f"Axonaut « {pipe} »" + (f" / « {step} »" if step else ""), dry_run=dry_run)
    collector = _Collector(campaign, report, dry_run)
    seen_companies: set[int] = set()
    n = 0
    async for opp in axonaut.iter_opportunities(pipe_name=pipe, step_name=step):
        if limit is not None and n >= limit:
            break
        n += 1
        company_id, comp_name = _opp_company(opp)
        label = str(comp_name or opp.get("name") or f"opportunité {opp.get('id')}")
        if company_id is None:
            report.ignores.append((label, "opportunité sans société"))
            continue
        if company_id in seen_companies:
            report.ignores.append((label, "société déjà importée (plusieurs opportunités)"))
            continue
        seen_companies.add(company_id)
        try:
            company = await axonaut.get_company(company_id)
            employees = await axonaut.list_company_employees(company_id)
        except Exception as e:
            report.erreurs.append(f"{label} (société {company_id}) : lecture Axonaut impossible : {e}")
            continue
        name = str(company.get("name") or label).strip()
        raw_phone = ax_mod.company_phone(company, employees)
        if not raw_phone:
            report.ignores.append((name, "aucun numéro de téléphone dans Axonaut"))
            continue
        try:
            phone = normalize_phone(str(raw_phone))
        except InvalidPhoneNumber as e:
            report.ignores.append((name, f"numéro invalide : {e}"))
            continue
        contact_name, contact_role = pick_decision_maker(employees)
        opp_id = opp.get("id")
        prospect = Prospect(
            campaign_id=campaign.id,
            name=name,
            phone=phone,
            city=ax_mod.company_city(company) or "",
            contact_name=contact_name,
            contact_role=contact_role,
            axonaut_company_id=company_id,
            axonaut_opportunity_id=int(opp_id) if opp_id else None,
            notes=_opportunity_notes(opp),
            extra={
                "source": "axonaut",
                "pipe_name": _opp_pipe(opp) or pipe,
                "pipe_step_name": _opp_step(opp) or step or "",
            },
        )
        collector.add(prospect)
    log.info(report.summary().splitlines()[0])
    return report


# ---------------------------------------------------------------------------
# Import CSV
# ---------------------------------------------------------------------------

# Colonnes reconnues (en-têtes comparés sans accents ni casse) → champ.
CSV_COLUMNS: dict[str, str] = {
    "nom": "name", "name": "name", "etablissement": "name", "raison sociale": "name", "societe": "name",
    "telephone": "phone", "tel": "phone", "phone": "phone", "numero": "phone",
    "ville": "city", "city": "city", "commune": "city",
    "contact": "contact_name", "contact name": "contact_name", "interlocuteur": "contact_name",
    "fonction": "contact_role", "role": "contact_role", "poste": "contact_role",
    "axonaut company id": "axonaut_company_id", "axonaut id": "axonaut_company_id",
    "notes": "notes", "note": "notes", "commentaire": "notes", "commentaires": "notes",
}


def _detect_delimiter(sample: str) -> str:
    first = sample.splitlines()[0] if sample else ""
    return ";" if first.count(";") >= first.count(",") and first.count(";") > 0 else ","


def import_from_csv(campaign: Campaign, path: Path | str, *, dry_run: bool = False) -> ImportReport:
    """Importe un CSV (colonnes : nom, telephone, ville, contact, fonction, axonaut_company_id, notes).

    Séparateur `;` ou `,` détecté automatiquement ; UTF-8 avec ou sans BOM. `nom` et `telephone`
    sont obligatoires ; les autres colonnes sont facultatives.
    """
    path = Path(path)
    report = ImportReport(source=f"CSV {path.name}", dry_run=dry_run)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        report.erreurs.append(f"{path} n'est pas en UTF-8 : réenregistrer le fichier en « CSV UTF-8 ».")
        return report
    reader = csv.DictReader(io.StringIO(text), delimiter=_detect_delimiter(text))
    headers = {h: CSV_COLUMNS.get(_fold(h or "")) for h in (reader.fieldnames or [])}
    if "name" not in headers.values() or "phone" not in headers.values():
        report.erreurs.append(
            f"colonnes obligatoires absentes (nom, telephone) ; colonnes lues : {list(reader.fieldnames or [])}"
        )
        return report
    collector = _Collector(campaign, report, dry_run)
    for line_no, row in enumerate(reader, start=2):
        rec: dict[str, str] = {}
        for h, value in row.items():
            key = headers.get(h) if h is not None else None
            if key and value is not None:
                rec[key] = str(value).strip()
        name = rec.get("name", "")
        label = f"ligne {line_no} ({name or 'sans nom'})"
        if not name and not rec.get("phone"):
            continue  # ligne vide
        if not name:
            report.ignores.append((label, "nom manquant"))
            continue
        try:
            phone = normalize_phone(rec.get("phone", ""))
        except InvalidPhoneNumber as e:
            report.ignores.append((label, f"numéro invalide : {e}"))
            continue
        company_id: int | None = None
        if rec.get("axonaut_company_id"):
            try:
                company_id = int(float(rec["axonaut_company_id"]))
            except ValueError:
                report.erreurs.append(f"{label} : axonaut_company_id non numérique ({rec['axonaut_company_id']!r})")
                continue
        collector.add(
            Prospect(
                campaign_id=campaign.id,
                name=name,
                phone=phone,
                city=rec.get("city", ""),
                contact_name=rec.get("contact_name", ""),
                contact_role=rec.get("contact_role", ""),
                axonaut_company_id=company_id,
                notes=rec.get("notes", ""),
                extra={"source": "csv", "fichier": path.name},
            )
        )
    log.info(report.summary().splitlines()[0])
    return report
