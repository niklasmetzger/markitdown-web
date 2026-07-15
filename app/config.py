"""Configuration loaded from environment variables."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "MarkItDown Web"
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    debug: bool = False
    base_url: str = "http://localhost:8000"

    # Storage
    data_dir: Path = Path("./data")
    users_file: Path = Path("./data/users.json")
    max_upload_size: int = 100 * 1024 * 1024  # 100 MB

    # Data retention — converted markdown is held in process memory only.
    # Multi-use: downloads do NOT purge the job. Jobs expire on idle TTL
    # (sliding by default) or via explicit DELETE.
    data_retention_seconds: int = 600       # 10 min default
    sliding_ttl: bool = True                # refresh TTL on every access
    reaper_interval_seconds: int = 60       # how often to scan for expired jobs

    # Local auth
    local_auth_enabled: bool = True
    session_ttl_hours: int = 12

    # OIDC / Authentik
    oidc_enabled: bool = False
    oidc_issuer: Optional[str] = None  # z.B. https://authentik.example.com
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_scopes: str = "openid profile email"
    oidc_auto_create_users: bool = True  # User aus OIDC automatisch anlegen

    # Bootstrap
    bootstrap_user: Optional[str] = None  # "admin" — beim ersten Start anlegen
    bootstrap_password: Optional[str] = None

    @property
    def oidc_configured(self) -> bool:
        return bool(self.oidc_enabled and self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
