"""
Konfiguracja bota - czytane z env vars.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Telegram
    telegram_bot_token: str = Field(...)
    admin_chat_ids: frozenset[int] = Field(default=frozenset())

    # Mode
    use_webhook: bool = Field(default=False)
    webhook_url: str = Field(default="")
    webhook_path: str = Field(default="/telegram/webhook")
    webhook_port: int = Field(default=8080)
    webhook_secret: str = Field(default="")

    # External services
    directus_url: str = Field(default="https://cms.osadathehive.pl")
    directus_token: str = Field(default="")
    worker_url: str = Field(default="http://ul-os-worker:3000")
    worker_secret: str = Field(default="")

    # Storage
    b2_endpoint: str = Field(default="")
    b2_bucket_archive: str = Field(default="ul-os-archive")
    b2_application_key_id: str = Field(default="")
    b2_application_key: str = Field(default="")

    # Tenant (per ADR-008)
    tenant_id: str = Field(default="hivelive_ecosystem")


# Singleton dla całej aplikacji
settings = Settings()  # type: ignore[call-arg]
