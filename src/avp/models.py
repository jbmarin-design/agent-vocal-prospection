"""Contrats de données partagés entre l'orchestrateur, le worker et le post-appel.

Ne pas renommer un champ sans mettre à jour toutes les briques : ces modèles
transitent en JSON (dispatch LiveKit, fichiers de transcription, base SQLite).
"""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Énumérations
# ---------------------------------------------------------------------------


class CallOutcome(StrEnum):
    """Issue normalisée d'un appel (une seule par appel)."""

    NON_DECROCHE = "non_decroche"  # sonne dans le vide / timeout
    OCCUPE = "occupe"
    MAUVAIS_NUMERO = "mauvais_numero"  # numéro invalide, faux numéro
    REPONDEUR = "repondeur"
    SVI = "svi"  # serveur vocal non franchi
    BARRAGE = "barrage"  # standard qui refuse de passer le décideur, sans rappel
    RAPPEL = "rappel"  # rappel convenu (date/heure ou "plus tard")
    REFUS = "refus"  # décideur pas intéressé
    OPPOSITION = "opposition"  # demande explicite de ne plus être appelé
    NON_QUALIFIE = "non_qualifie"  # échange fait, mais hors cible
    QUALIFIE = "qualifie"  # échange fait, intérêt, sans RDV
    RDV = "rdv"  # rendez-vous confirmé
    TRANSFERT = "transfert"  # transféré à un humain d'OpteoLink
    ERREUR = "erreur"  # erreur technique

    @property
    def is_final(self) -> bool:
        """Vrai si le prospect ne doit plus être rappelé automatiquement dans cette campagne."""
        return self in {
            CallOutcome.MAUVAIS_NUMERO,
            CallOutcome.REFUS,
            CallOutcome.OPPOSITION,
            CallOutcome.NON_QUALIFIE,
            CallOutcome.QUALIFIE,
            CallOutcome.RDV,
            CallOutcome.TRANSFERT,
        }

    @property
    def reached_human(self) -> bool:
        return self not in {
            CallOutcome.NON_DECROCHE,
            CallOutcome.OCCUPE,
            CallOutcome.MAUVAIS_NUMERO,
            CallOutcome.REPONDEUR,
            CallOutcome.SVI,
            CallOutcome.ERREUR,
        }


class ProspectStatus(StrEnum):
    NOUVEAU = "nouveau"
    A_RAPPELER = "a_rappeler"
    EN_COURS = "en_cours"  # appel en cours (verrou)
    TERMINE = "termine"
    EXCLU = "exclu"  # opposition ou numéro invalide


class AnalysisStatus(StrEnum):
    EN_ATTENTE = "en_attente"
    FAIT = "fait"
    ERREUR = "erreur"
    IGNORE = "ignore"  # rien à analyser (non décroché, répondeur sans message…)


Score = Literal["chaud", "tiede", "froid"]

# ---------------------------------------------------------------------------
# Campagne (campaigns/<id>.yaml)
# ---------------------------------------------------------------------------


class TimeWindow(BaseModel):
    """Plage horaire d'appel. jours : 1=lundi … 7=dimanche (ISO)."""

    jours: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    debut: time
    fin: time

    @field_validator("jours")
    @classmethod
    def _check_days(cls, v: list[int]) -> list[int]:
        if not v or any(d < 1 or d > 7 for d in v):
            raise ValueError("jours doit contenir des entiers 1..7 (1=lundi)")
        return v

    def contains(self, dt: datetime) -> bool:
        return dt.isoweekday() in self.jours and self.debut <= dt.time() < self.fin


class QualificationQuestion(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    question: str
    but: str = ""
    obligatoire: bool = False


class Objection(BaseModel):
    objection: str
    reponse: str


class Target(BaseModel):
    interlocuteur: str
    alternatives: list[str] = Field(default_factory=list)


class Offer(BaseModel):
    resume: str
    points_forts: list[str] = Field(default_factory=list)
    preuves: list[str] = Field(default_factory=list)


class VoicemailPolicy(BaseModel):
    action: Literal["message", "raccrocher"] = "raccrocher"
    message: str = ""


class RdvPolicy(BaseModel):
    duree_min: int = 30
    mode: str = "visio ou téléphone"
    creneaux_proposes: int = 3
    delai_min_jours: int = 2
    horizon_jours: int = 15
    plages: list[TimeWindow] = Field(
        default_factory=lambda: [
            TimeWindow(debut=time(9, 30), fin=time(12, 0)),
            TimeWindow(debut=time(14, 0), fin=time(17, 30)),
        ]
    )


class AxonautMapping(BaseModel):
    """Pipeline Axonaut et étape cible selon l'issue/score."""

    pipe: str
    etape_initiale: str = "À contacter"
    # clé = CallOutcome (valeur) ou "chaud"/"tiede"/"froid" ; valeur = nom d'étape Axonaut
    etapes: dict[str, str] = Field(default_factory=dict)
    montant_defaut: float | None = None


class Campaign(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    nom: str
    actif: bool = True
    objectif: Literal["rdv", "transfert", "qualification"] = "rdv"
    cible: Target
    offre: Offer
    accroche: str
    qualification: list[QualificationQuestion] = Field(default_factory=list)
    objections: list[Objection] = Field(default_factory=list)
    scoring: dict[Score, str] = Field(default_factory=dict)
    mots_cles_stt: list[str] = Field(default_factory=list, description="Termes à faire reconnaître par le STT")
    repondeur: VoicemailPolicy = Field(default_factory=VoicemailPolicy)
    # Par prudence, alignées sur les plages légales du démarchage des consommateurs
    # (lun-ven 10h-13h / 14h-20h) même si la règle vise les consommateurs et non le B2B.
    plages: list[TimeWindow] = Field(
        default_factory=lambda: [
            TimeWindow(debut=time(10, 0), fin=time(12, 30)),
            TimeWindow(debut=time(14, 0), fin=time(17, 30)),
        ]
    )
    max_tentatives: int = Field(default=3, le=4, description="≤ 4 tentatives / 30 jours")
    delai_entre_tentatives_h: int = 48
    rdv: RdvPolicy = Field(default_factory=RdvPolicy)
    axonaut: AxonautMapping | None = None
    instructions_specifiques: str = ""

    def in_window(self, dt: datetime) -> bool:
        return any(w.contains(dt) for w in self.plages)


# ---------------------------------------------------------------------------
# Prospect et métadonnées d'appel
# ---------------------------------------------------------------------------


class Prospect(BaseModel):
    id: int | None = None  # id SQLite (None avant insertion)
    campaign_id: str
    name: str  # raison sociale / nom de l'établissement
    phone: str  # E.164, ex. +33562000000
    city: str = ""
    contact_name: str = ""  # décideur connu, si disponible
    contact_role: str = ""
    axonaut_company_id: int | None = None
    axonaut_opportunity_id: int | None = None
    notes: str = ""  # historique / contexte injecté dans la fiche
    extra: dict[str, Any] = Field(default_factory=dict)


class CallMetadata(BaseModel):
    """JSON transmis au worker dans la dispatch LiveKit (contrat orchestrateur → worker)."""

    call_id: str
    campaign_id: str
    prospect: Prospect
    attempt: int = 1
    test_mode: bool = False  # appel de test : pas de mise à jour Axonaut
    rdv_slots: list[datetime] = Field(default_factory=list, description="Créneaux proposables, calculés à la dispatch")


# ---------------------------------------------------------------------------
# État accumulé pendant l'appel (outils du worker)
# ---------------------------------------------------------------------------


class Rdv(BaseModel):
    start: datetime
    duree_min: int = 30
    mode: str = ""
    avec: str = ""  # nom de la personne
    email: str = ""
    notes: str = ""


class Callback(BaseModel):
    when: datetime | None = None  # None = "rappeler plus tard" sans date
    ask_for: str = ""  # personne à demander
    notes: str = ""


class CallState(BaseModel):
    """Rempli par les outils pendant l'appel, sérialisé en fin d'appel."""

    outcome: CallOutcome | None = None
    amd_result: str = ""  # human / machine-vm / machine-ivr / machine-unavailable / uncertain
    decision_maker_reached: bool = False
    contact_name: str = ""
    contact_role: str = ""
    contact_email: str = ""
    answers: dict[str, str] = Field(default_factory=dict)  # id question → réponse
    rdv: Rdv | None = None
    callback: Callback | None = None
    optout: bool = False
    optout_reason: str = ""
    transferred: bool = False
    agent_path: list[str] = Field(default_factory=list)  # ex. ["accueil", "decideur"]
    notes: list[str] = Field(default_factory=list)
    end_reason: str = ""


class TranscriptTurn(BaseModel):
    role: Literal["agent", "prospect", "systeme"]
    text: str
    agent: str = ""  # accueil / decideur
    ts: datetime | None = None


class CallRecordFile(BaseModel):
    """Contenu de data/transcripts/<call_id>.json, écrit par le worker en fin d'appel."""

    metadata: CallMetadata
    state: CallState
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    prompt_version: str = ""
    started_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    usage: dict[str, Any] = Field(default_factory=dict)  # métriques STT/LLM/TTS


# ---------------------------------------------------------------------------
# Analyse post-appel (Claude Sonnet)
# ---------------------------------------------------------------------------


class CallAnalysis(BaseModel):
    score: Score
    score_num: int = Field(ge=0, le=100)
    resume: str
    interlocuteur: str = ""
    fonction: str = ""
    besoin: str = ""
    equipement_actuel: str = ""
    prestataire_actuel: str = ""
    echeance_contrat: str = ""
    objections: list[str] = Field(default_factory=list)
    points_cles: list[str] = Field(default_factory=list)
    prochaine_action: str = ""
    date_relance: datetime | None = None
    rdv_confirme: bool = False
    qualite_appel: int = Field(default=3, ge=1, le=5, description="Qualité de conduite de l'appel par l'agent")
    suggestions_script: list[str] = Field(default_factory=list)
