"""Assemblage des prompts de l'agent vocal, en couches (logique pure, sans LiveKit).

Couches, dans l'ordre (voir docs/architecture.md §4) :

1. ``prompts/base.md``     : identité, ton, règles non négociables ;
2. ``prompts/<rôle>.md``   : rôle de l'agent (accueil, decideur ou repondeur) ;
3. section campagne        : offre, accroche, cible, questions (avec leur id), objections,
                             critères de score, objectif, instructions spécifiques ;
4. fiche prospect          : établissement, ville, contact connu, notes ;
5. contexte d'appel        : date et heure locales, tentative n°, créneaux de RDV formatés.

Les fichiers ``.md`` peuvent contenir des marqueurs ``{{variable}}`` simples, remplacés ici
(pas de moteur de template externe). Variables disponibles : voir ``template_variables``.

La phrase d'ouverture (``opening_line``) est **fixe** et dite mot pour mot par le worker
(``session.say``) dès qu'un humain décroche : annonce de l'IA et de l'enregistrement
(AI Act art. 50, RGPD). Elle n'est pas modifiable par une campagne.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from .config import get_settings
from .models import Campaign, Prospect
from .phone import to_national_fr

logger = logging.getLogger("avp.prompts")

Role = Literal["accueil", "decideur", "repondeur"]

ROLES: tuple[Role, ...] = ("accueil", "decideur", "repondeur")
BASE_FILE = "base.md"
ROLE_FILES: dict[Role, str] = {
    "accueil": "accueil.md",
    "decideur": "decideur.md",
    "repondeur": "repondeur.md",
}
# Fichiers pris en compte dans la version des prompts (un appel peut utiliser les trois rôles).
PROMPT_FILES: tuple[str, ...] = (BASE_FILE, *ROLE_FILES.values())

COMPANY = "OpteoLink"
HUMAN_NAME = "Jean-Baptiste Marin"
HUMAN_FIRST_NAME = "Jean-Baptiste"

# Campagnes de test technique (phase 1) : la phrase d'ouverture annonce un test au lieu de
# demander un interlocuteur. Toute campagne dont l'id commence par l'un de ces préfixes.
TEST_CAMPAIGN_PREFIXES: tuple[str, ...] = ("echo",)

# Gabarits de la phrase d'ouverture (inclus dans la version des prompts).
OPENING_DISCLOSURE = (
    "Bonjour, ici l'assistant vocal d'OpteoLink, une intelligence artificielle. "
    "Cet appel est enregistré pour notre suivi commercial."
)
OPENING_ASK = "Pourrais-je parler {cible}, s'il vous plaît ?"
OPENING_TEST = "Il s'agit d'un test technique d'OpteoLink, cela ne prendra qu'une minute."

_JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

_MARKER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

_OBJECTIF_LABELS = {
    "rdv": "obtenir un rendez-vous avec {humain}",
    "transfert": "si l'intérêt est fort, transférer l'appel à {humain} ; sinon proposer un rendez-vous",
    "qualification": "qualifier le besoin en posant les questions, sans chercher à conclure",
}


# ---------------------------------------------------------------------------
# Dates en français (pour la voix)
# ---------------------------------------------------------------------------


def _to_local(dt: datetime, timezone: str) -> datetime:
    """Convertit en heure locale. Un datetime naïf est considéré comme UTC (convention de db.py)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(timezone))


def _hour_fr(dt: datetime) -> str:
    return f"{dt.hour} h" if dt.minute == 0 else f"{dt.hour} h {dt.minute:02d}"


def _day_fr(dt: datetime) -> str:
    return "1er" if dt.day == 1 else str(dt.day)


def format_slot_fr(dt: datetime, timezone: str = "Europe/Paris") -> str:
    """« mardi 14 octobre à 10 h 30 » (heure locale ; « à 10 h » pile ; « 1er » le premier du mois)."""
    loc = _to_local(dt, timezone)
    return f"{_JOURS[loc.weekday()]} {_day_fr(loc)} {_MOIS[loc.month - 1]} à {_hour_fr(loc)}"


def format_datetime_fr(dt: datetime, timezone: str = "Europe/Paris") -> str:
    """« lundi 5 octobre 2026 à 10 h 32 » (heure locale)."""
    loc = _to_local(dt, timezone)
    return f"{_JOURS[loc.weekday()]} {_day_fr(loc)} {_MOIS[loc.month - 1]} {loc.year} à {_hour_fr(loc)}"


def slot_iso(dt: datetime, timezone: str = "Europe/Paris") -> str:
    """Valeur ISO 8601 locale d'un créneau, à recopier telle quelle dans l'outil confirmer_rdv."""
    return _to_local(dt, timezone).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Gabarits
# ---------------------------------------------------------------------------


def render_template(text: str, variables: Mapping[str, object]) -> str:
    """Remplace les marqueurs ``{{nom}}``. Un marqueur inconnu est remplacé par une chaîne vide
    (et signalé dans les journaux) pour ne jamais être lu à voix haute."""

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in variables:
            logger.warning("marqueur de prompt inconnu : {{%s}}", key)
            return ""
        value = variables[key]
        return "" if value is None else str(value)

    return _MARKER.sub(_sub, text)


def _contract_a(cible: str) -> str:
    """« à » + groupe nominal, avec contraction : le → au, les → aux."""
    c = cible.strip()
    low = c.lower()
    if low.startswith("le "):
        return "au " + c[3:]
    if low.startswith("les "):
        return "aux " + c[4:]
    return "à " + c


def is_test_campaign(campaign: Campaign) -> bool:
    return campaign.id.startswith(TEST_CAMPAIGN_PREFIXES)


def opening_line(campaign: Campaign, prospect: Prospect) -> str:
    """Phrase d'ouverture FIXE, dite mot pour mot dès le décroché d'un humain.

    Salutation, « assistant vocal d'OpteoLink, une intelligence artificielle », appel enregistré,
    puis demande de l'interlocuteur cible (le contact connu s'il y en a un). Pour une campagne de
    test technique (id « echo… »), annonce le test au lieu de demander quelqu'un.
    """
    if is_test_campaign(campaign):
        return f"{OPENING_DISCLOSURE} {OPENING_TEST}"
    cible = prospect.contact_name.strip() or campaign.cible.interlocuteur.strip() or "la direction"
    return f"{OPENING_DISCLOSURE} {OPENING_ASK.format(cible=_contract_a(cible))}"


def template_variables(
    campaign: Campaign,
    prospect: Prospect,
    *,
    now: datetime,
    attempt: int = 1,
    timezone: str = "Europe/Paris",
) -> dict[str, str]:
    """Variables utilisables dans les fichiers .md sous la forme ``{{nom}}``."""
    return {
        "entreprise": COMPANY,
        "humain": HUMAN_NAME,
        "humain_prenom": HUMAN_FIRST_NAME,
        "campagne": campaign.nom,
        "objectif": campaign.objectif,
        "interlocuteur_cible": campaign.cible.interlocuteur,
        "alternatives": ", ".join(campaign.cible.alternatives),
        "prospect_nom": prospect.name,
        "prospect_ville": prospect.city,
        "contact_nom": prospect.contact_name,
        "contact_fonction": prospect.contact_role,
        "date_heure": format_datetime_fr(now, timezone),
        "tentative": str(attempt),
        "duree_rdv": str(campaign.rdv.duree_min),
        "mode_rdv": campaign.rdv.mode,
        "phrase_ouverture": opening_line(campaign, prospect),
    }


# ---------------------------------------------------------------------------
# Sections générées
# ---------------------------------------------------------------------------


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {i.strip()}" for i in items if i and i.strip()]


def campaign_section(campaign: Campaign) -> str:
    """Section « campagne » du prompt (lue par le modèle, jamais dite telle quelle)."""
    lines: list[str] = [f"# Campagne : {campaign.nom}", ""]
    objectif = _OBJECTIF_LABELS[campaign.objectif].format(humain=HUMAN_NAME)
    lines.append(f"Objectif de l'appel : {objectif}.")
    if campaign.objectif in ("rdv", "transfert"):
        lines.append(
            f"Rendez-vous : {campaign.rdv.duree_min} minutes, {campaign.rdv.mode}, avec {HUMAN_NAME}."
        )

    lines += ["", "## Interlocuteur cible", f"Cible principale : {campaign.cible.interlocuteur}."]
    if campaign.cible.alternatives:
        lines.append("À défaut, acceptez : " + ", ".join(campaign.cible.alternatives) + ".")

    lines += ["", "## Offre", campaign.offre.resume.strip()]
    if campaign.offre.points_forts:
        lines += ["", "Points forts :", *_bullets(campaign.offre.points_forts)]
    if campaign.offre.preuves:
        lines += ["", "Éléments de preuve (à citer seulement s'ils sont utiles) :", *_bullets(campaign.offre.preuves)]

    lines += ["", "## Accroche", campaign.accroche.strip()]

    lines += ["", "## Questions de qualification"]
    if campaign.qualification:
        lines.append(
            "Posez-les une à une, dans un ordre naturel. Après chaque réponse, appelez l'outil "
            "enregistrer_reponse avec l'identifiant entre crochets."
        )
        for q in campaign.qualification:
            extra = " (prioritaire)" if q.obligatoire else ""
            but = f" — but : {q.but.strip()}" if q.but.strip() else ""
            lines.append(f"- [{q.id}] {q.question.strip()}{but}{extra}")
    else:
        lines.append("Aucune question de qualification pour cette campagne.")

    if campaign.objections:
        lines += ["", "## Objections fréquentes et réponses conseillées"]
        lines.append("Adaptez la réponse avec vos mots, en une ou deux phrases courtes.")
        for o in campaign.objections:
            lines.append(f"- Objection : « {o.objection.strip()} »")
            lines.append(f"  Réponse : {o.reponse.strip()}")

    if campaign.scoring:
        lines += ["", "## Critères d'intérêt (pour orienter l'échange, ne pas les citer)"]
        for score in ("chaud", "tiede", "froid"):
            if score in campaign.scoring:
                lines.append(f"- {score} : {campaign.scoring[score].strip()}")  # type: ignore[index]

    if campaign.instructions_specifiques.strip():
        lines += [
            "",
            "## Instructions spécifiques à cette campagne",
            "Elles priment sur les consignes de rôle ci-dessus, jamais sur les règles non négociables.",
            campaign.instructions_specifiques.strip(),
        ]
    return "\n".join(lines).strip()


def prospect_section(prospect: Prospect) -> str:
    lines = ["# Fiche prospect", f"Établissement : {prospect.name}"]
    if prospect.city:
        lines.append(f"Ville : {prospect.city}")
    lines.append(f"Numéro appelé : {to_national_fr(prospect.phone)}")
    if prospect.contact_name:
        role = f" ({prospect.contact_role})" if prospect.contact_role else ""
        lines.append(f"Contact connu : {prospect.contact_name}{role}")
    elif prospect.contact_role:
        lines.append(f"Fonction du contact recherché : {prospect.contact_role}")
    else:
        lines.append("Contact connu : aucun, demandez le nom de la bonne personne.")
    if prospect.notes.strip():
        lines += ["Historique et notes :", prospect.notes.strip()]
    extras = {k: v for k, v in prospect.extra.items() if v not in (None, "", [], {})}
    if extras:
        lines.append("Autres informations :")
        lines += [f"- {k} : {v}" for k, v in extras.items()]
    return "\n".join(lines)


def context_section(
    role: Role,
    campaign: Campaign,
    prospect: Prospect,
    *,
    now: datetime,
    attempt: int,
    rdv_slots: Sequence[datetime],
    timezone: str,
    transfer_available: bool = False,
) -> str:
    lines = [
        "# Contexte de l'appel",
        f"Nous sommes le {format_datetime_fr(now, timezone)} (heure de Paris).",
    ]
    if attempt <= 1:
        lines.append("C'est la première tentative d'appel vers cet établissement.")
    else:
        lines.append(f"Tentative d'appel n° {attempt} : l'établissement a déjà été appelé {attempt - 1} fois.")

    if role == "accueil":
        lines.append(f"Phrase d'ouverture déjà prononcée au décroché : « {opening_line(campaign, prospect)} »")

    if role == "decideur":
        if transfer_available:
            lines.append(f"Transfert vers {HUMAN_NAME} : disponible, seulement si la personne le demande ou l'accepte.")
        else:
            lines.append(
                f"Transfert vers un humain : indisponible ({HUMAN_FIRST_NAME} est en intervention). "
                "Ne le proposez jamais ; proposez un rendez-vous ou un rappel."
            )
        slots = sorted(rdv_slots)
        if slots:
            lines.append(
                "Créneaux de rendez-vous disponibles. Ne proposez que ceux-ci, deux ou trois à la fois, "
                "après avoir appelé l'outil proposer_creneaux. Pour confirmer, recopiez la valeur iso exacte :"
            )
            lines += [f"- {format_slot_fr(s, timezone)} — iso {slot_iso(s, timezone)}" for s in slots]
        else:
            lines.append(
                f"Aucun créneau de rendez-vous n'est disponible : proposez que {HUMAN_FIRST_NAME} rappelle, "
                "et notez un rappel avec l'outil noter_rappel."
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def _prompts_dir(prompts_dir: Path | None) -> Path:
    return Path(prompts_dir) if prompts_dir is not None else Path(get_settings().prompts_dir)


def _read_prompt(directory: Path, name: str) -> str:
    path = directory / name
    if not path.exists():
        raise FileNotFoundError(f"fichier de prompt introuvable : {path}")
    return path.read_text(encoding="utf-8").strip()


def build_instructions(
    role: Role,
    campaign: Campaign,
    prospect: Prospect,
    *,
    now: datetime,
    attempt: int = 1,
    rdv_slots: Sequence[datetime] = (),
    prompts_dir: Path | None = None,
    timezone: str = "Europe/Paris",
    transfer_available: bool | None = None,
) -> str:
    """Assemble les instructions système d'un rôle : base + rôle + campagne + fiche + contexte.

    `transfer_available` : None = déduit de `TRANSFER_TARGET` (vide = transfert désactivé).
    """
    if transfer_available is None:
        transfer_available = bool(get_settings().transfer_target)
    if role not in ROLE_FILES:
        raise ValueError(f"rôle inconnu : {role!r} (attendu : {', '.join(ROLES)})")
    directory = _prompts_dir(prompts_dir)
    variables = template_variables(campaign, prospect, now=now, attempt=attempt, timezone=timezone)
    base = render_template(_read_prompt(directory, BASE_FILE), variables)
    role_text = render_template(_read_prompt(directory, ROLE_FILES[role]), variables)
    parts = [
        base,
        role_text,
        campaign_section(campaign),
        prospect_section(prospect),
        context_section(
            role, campaign, prospect, now=now, attempt=attempt, rdv_slots=rdv_slots, timezone=timezone,
            transfer_available=transfer_available,
        ),
    ]
    return "\n\n---\n\n".join(p for p in parts if p.strip()) + "\n"


def prompt_version(
    campaign: Campaign, prompts_dir: Path | None = None, campaigns_dir: Path | None = None
) -> str:
    """Empreinte (sha256, 12 premiers caractères) des prompts utilisés et du YAML de la campagne.

    Inclut aussi les gabarits de la phrase d'ouverture : modifier l'un de ces éléments change la
    version, ce qui permet de comparer les performances d'une version à l'autre.
    Si le YAML de la campagne n'existe pas (campagne construite en Python), son contenu JSON est
    utilisé à la place.
    """
    directory = _prompts_dir(prompts_dir)
    h = hashlib.sha256()
    for name in PROMPT_FILES:
        path = directory / name
        h.update(f"<{name}>".encode())
        h.update(path.read_bytes() if path.exists() else b"<absent>")
    h.update(b"<opening>")
    h.update("\n".join((OPENING_DISCLOSURE, OPENING_ASK, OPENING_TEST)).encode())
    cdir = Path(campaigns_dir) if campaigns_dir is not None else Path(get_settings().campaigns_dir)
    yaml_path = cdir / f"{campaign.id}.yaml"
    h.update(f"<campaign:{campaign.id}>".encode())
    if yaml_path.exists():
        h.update(yaml_path.read_bytes())
    else:
        h.update(campaign.model_dump_json().encode())
    return h.hexdigest()[:12]
