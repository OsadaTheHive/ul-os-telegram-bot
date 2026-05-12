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
    # Raw CSV string z .env - parsowany przez admin_user_ids property.
    # Bezpośrednio jako frozenset[int] nie da rady bo pydantic-settings probuje JSON.
    admin_chat_ids: str = Field(default="")

    @property
    def admin_user_ids(self) -> frozenset[int]:
        """Sparsowany ADMIN_CHAT_IDS jako frozenset[int]. Pusty string → empty."""
        return frozenset(
            int(x.strip()) for x in self.admin_chat_ids.split(",") if x.strip()
        )

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

    # Storage — Hetzner Object Storage (per ADR-001 z 2026-05-09, supersedes B2 plan)
    s3_endpoint: str = Field(default="")  # np. https://nbg1.your-objectstorage.com
    s3_bucket: str = Field(default="ul-os-storage")  # bucket startowy w Nuremberg
    s3_access_key_id: str = Field(default="")
    s3_secret_access_key: str = Field(default="")
    s3_region: str = Field(default="nbg1")
    # Prefix do ktorego bot uploaduje pliki. Worker (mode=hos) polluje ten sam prefix.
    s3_inbox_prefix: str = Field(default="inbox/")
    # Prefix dla wygenerowanych dokumentow (DOCX/PDF z /generate)
    s3_generated_prefix: str = Field(default="generated/")

    # Tenant (per ADR-008)
    tenant_id: str = Field(default="hivelive_ecosystem")

    # === External AI services (Sprint 1.7-1.9) ===
    # Perplexity Sonar Deep Research (https://docs.perplexity.ai)
    perplexity_api_key: str = Field(default="")
    perplexity_model: str = Field(default="sonar-deep-research")
    # Anthropic (Conversational /ask z dostepem do MCP tools)
    anthropic_api_key: str = Field(default="")
    anthropic_model: str = Field(default="claude-haiku-4-5")
    # OpenAI Whisper (voice transcription)
    openai_api_key: str = Field(default="")
    openai_whisper_model: str = Field(default="whisper-1")

    # Notifier thresholds
    notifier_interval_seconds: int = Field(default=14400)  # 4h


# Singleton dla całej aplikacji
settings = Settings()  # type: ignore[call-arg]
