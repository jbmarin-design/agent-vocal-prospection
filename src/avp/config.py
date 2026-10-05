"""Paramètres de l'application, lus depuis l'environnement et le fichier `.env`.

Toutes les briques (worker, orchestrateur, CLI) passent par `get_settings()`.
Aucune clé ne doit être écrite en dur ailleurs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Chemins ---------------------------------------------------------------
    data_dir: Path = Field(default=PROJECT_ROOT / "data", description="Base SQLite, transcriptions, rapports")
    campaigns_dir: Path = Field(default=PROJECT_ROOT / "campaigns")
    prompts_dir: Path = Field(default=PROJECT_ROOT / "prompts")

    # --- LiveKit (auto-hébergé) -----------------------------------------------
    livekit_url: str = "ws://127.0.0.1:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "change-me-secret-de-32-caracteres-min"
    agent_name: str = "avp-prospection"

    # --- Trunk SIP sortant (LiveKit → XiVO) -------------------------------------
    sip_outbound_trunk_id: str = Field(default="", description="ID ST_xxx créé par `avp trunk create`")
    xivo_sip_address: str = Field(default="", description="IP ou FQDN du XiVO, ex. 10.0.0.10 ou xivo.opteolink.fr")
    xivo_sip_transport: Literal["udp", "tcp"] = "udp"
    sip_caller_number: str = Field(default="", description="Numéro présenté (E.164), ex. +33587140500")
    sip_trunk_username: str = ""
    sip_trunk_password: str = ""

    # --- Transfert vers un humain ---------------------------------------------
    transfer_target: str = Field(
        default="", description="URI SIP ou tel: de JB pour le transfert à chaud, ex. sip:1001@10.0.0.10"
    )

    # --- IA ------------------------------------------------------------------
    anthropic_api_key: str = ""
    llm_realtime_model: str = "claude-haiku-4-5-20251001"
    llm_analysis_model: str = "claude-sonnet-5-5"
    deepgram_api_key: str = ""
    stt_model: str = "nova-3"
    stt_language: str = "fr"
    tts_provider: Literal["cartesia", "elevenlabs"] = "cartesia"
    cartesia_api_key: str = ""
    cartesia_model: str = "sonic-3"
    cartesia_voice_id: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_voice_id: str = ""

    # --- Axonaut --------------------------------------------------------------
    axonaut_api_key: str = ""
    axonaut_base_url: str = "https://axonaut.com/api/v2"
    axonaut_user_email: str = Field(default="jbmarin@opteolink.fr", description="Responsable des opportunités")

    # --- Garde-fous d'appel ----------------------------------------------------
    timezone: str = "Europe/Paris"
    max_call_duration_s: int = 360
    ring_timeout_s: int = 40
    silence_timeout_s: int = 20
    max_concurrent_calls: int = 2
    dry_run: bool = Field(default=False, description="Si vrai : aucun appel réel, aucune écriture Axonaut")

    # --- Chemins dérivés ---------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "avp.db"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.transcripts_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
