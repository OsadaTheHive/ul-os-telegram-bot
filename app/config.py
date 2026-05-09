"""
Konfiguracja bota - czytane z env vars.
"""

from __future__ import annotations

from pydantic import Field, field_validator
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

    @field_validator("admin_chat_ids", mode="before")
    @classmethod
    def _parse_admin_chat_ids(cls, v):
        # accept "id1,id2,id3" string z .env, lub list/tuple/frozenset, lub pojedynczy int
        if v is None or v == "":
            return frozenset()
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(int(x) for x in v if str(x).strip())
        if isinstance(v, int):
            return frozenset([v])
        if isinstance(v, str):
            return frozenset(int(x.strip()) for x in v.split(",") if x.strip())
        return v

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

    # MCP server (UL OS Knowledge bridge)
    mcp_base_url: str = Field(default="https://mcp.bidbee.pl")
    mcp_bearer_token: str = Field(default="")

    # Storage
    b2_endpoint: str = Field(default="")
    b2_bucket_archive: str = Field(default="ul-os-archive")
    b2_application_key_id: str = Field(default="")
    b2_application_key: str = Field(default="")

    # Tenant (per ADR-008)
    tenant_id: str = Field(default="hivelive_ecosystem")


# Singleton dla całej aplikacji
settings = Settings()  # type: ignore[call-arg]
