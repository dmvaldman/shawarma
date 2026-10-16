from decimal import Decimal
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARNESS_", env_file=".env", extra="ignore", allow_inf_nan=False
    )

    mattermost_url: HttpUrl
    mattermost_public_url: str = ""
    gitea_public_url: str = ""
    opencode_public_url: str = ""
    analytics_public_url: str = ""
    mattermost_server_id: str = "local-server"
    mattermost_admin_token: SecretStr = SecretStr("")
    mattermost_owner_id: str = ""
    database_url: str
    avatars_enabled: bool = True
    avatar_model: str = "gpt-image-2.5-flare"
    avatar_base_url: str = ""
    avatar_api_key: SecretStr = SecretStr("")
    seed_model: str = ""
    max_active_agents: int = Field(default=100, gt=0)
    max_concurrent_llm_calls: int = Field(default=10, gt=0)
    max_tokens_per_turn: int = Field(default=4096, gt=0)
    openai_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="OPENAI_API_KEY")
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="ANTHROPIC_API_KEY"
    )
    deepseek_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="DEEPSEEK_API_KEY")
    openai_admin_key: SecretStr = Field(default=SecretStr(""), validation_alias="OPENAI_ADMIN_KEY")
    anthropic_admin_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="ANTHROPIC_ADMIN_KEY"
    )
    anthropic_base_url: HttpUrl = HttpUrl("https://api.anthropic.com/v1")
    deepseek_base_url: HttpUrl = HttpUrl("https://api.deepseek.com")
    model_cache_ttl_sec: int = Field(default=3600, gt=0)
    unknown_input_usd_per_million: Decimal = Field(default=Decimal("100"), gt=0)
    unknown_output_usd_per_million: Decimal = Field(default=Decimal("500"), gt=0)
    model_profiles: dict[str, dict[str, str]] = Field(default_factory=dict)
    web_search_model_id: str = "gpt-6-luna"
    channel_delivery_min_sec: float = Field(default=0.5, ge=0)
    channel_delivery_max_sec: float = Field(default=3, ge=0)
    typing_yield_max_sec: float = Field(default=3, ge=0)
    analytics_port: int = Field(default=8066, ge=1, le=65535)
    analytics_bind_host: str = "127.0.0.1"
    analytics_proxy_hosts: list[str] = Field(default_factory=list)
    slash_bind_host: str = "0.0.0.0"
    slash_port: int = Field(default=8766, ge=1, le=65535)
    slash_callback_url: HttpUrl = HttpUrl("http://host.docker.internal:8766/mattermost/commands")
    owner_prompt_revision: str = ""
    workspace_name: str = "My Swarm"
    coordination_channel_name: str = "town-square"
    openai_base_url: HttpUrl = HttpUrl("https://api.openai.com/v1")
    openai_model_id: str = "gpt-6-luna"
    openai_model_label: str = "OpenAI gpt-6-luna"
    # Conservative upper prices: long-context standard tier, including cache-write uplift.
    input_usd_per_million: Decimal = Field(default=Decimal("0.25"), gt=0)
    output_usd_per_million: Decimal = Field(default=Decimal("0.75"), gt=0)
    idle_channel_poll_sec: float = Field(default=30, gt=0)
    notification_batch_size: int = Field(default=10, gt=0, le=50)
    max_tool_calls_per_turn: int = Field(default=12, gt=0)
    llm_request_timeout_sec: float = Field(default=120, gt=0)
    tool_timeout_sec: float = Field(default=90, gt=0)
    coding_enabled: bool = False
    agent_engine: Literal["provider", "opencode"] = "provider"
    swarm_tool_bind_host: str = "0.0.0.0"
    swarm_tool_port: int = Field(default=8767, ge=1, le=65535)
    swarm_tool_url: HttpUrl = HttpUrl("http://host.docker.internal:8767")
    swarm_tool_key: SecretStr = SecretStr("")
    coding_backend: str = "docker"
    opencode_url: HttpUrl = HttpUrl("http://127.0.0.1:4096")
    opencode_password: SecretStr = SecretStr("")
    coding_container: str = "swarm-coding-worker"
    coding_timeout_sec: float = Field(default=600, gt=0)
    gitea_url: HttpUrl = HttpUrl("http://127.0.0.1:3000")
    gitea_worker_url: HttpUrl = HttpUrl("http://gitea:3000")
    gitea_token: SecretStr = SecretStr("")
    gitea_owner: str = "swarm"
    gitea_repo: str = "workspace"
    agent37_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="AGENT37_API_KEY")
    agent37_base_url: HttpUrl = HttpUrl("https://api.agent37.com/v1")
    agent37_instance_id: str = ""

    @property
    def seed_model_id(self) -> str:
        return self.seed_model or self.openai_model_id

    @model_validator(mode="after")
    def coherent_limits(self) -> "Settings":
        if self.channel_delivery_max_sec < self.channel_delivery_min_sec:
            raise ValueError("channel delivery maximum must exceed minimum")
        for rates in self.model_profiles.values():
            for field in ("input", "output"):
                rate = Decimal(rates[field])
                if not rate.is_finite() or rate <= 0:
                    raise ValueError("model rates must be finite and positive")
        return self
