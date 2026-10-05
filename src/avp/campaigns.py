"""Chargement des campagnes depuis campaigns/<id>.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import get_settings
from .models import Campaign


class CampaignNotFound(LookupError):
    pass


def campaign_path(campaign_id: str, campaigns_dir: Path | None = None) -> Path:
    d = campaigns_dir or get_settings().campaigns_dir
    return d / f"{campaign_id}.yaml"


def load_campaign(campaign_id: str, campaigns_dir: Path | None = None) -> Campaign:
    path = campaign_path(campaign_id, campaigns_dir)
    if not path.exists():
        raise CampaignNotFound(f"campagne introuvable : {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    camp = Campaign.model_validate(data)
    if camp.id != campaign_id:
        raise ValueError(f"{path.name} : l'id interne {camp.id!r} ne correspond pas au nom du fichier")
    return camp


def list_campaigns(campaigns_dir: Path | None = None) -> list[Campaign]:
    d = campaigns_dir or get_settings().campaigns_dir
    return [load_campaign(p.stem, d) for p in sorted(d.glob("*.yaml"))]
