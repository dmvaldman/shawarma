from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentIdentity(BaseModel):
    """Birth record. Runtime state and tool versions live separately from this record."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    workspace_id: UUID
    tenant_id: UUID | None = None
    parent_id: UUID | None = None
    mattermost_user_id: str
    mattermost_username: str
    system_prompt: str
    template_revision: str
    role_instructions: str


class Post(BaseModel):
    id: str
    user_id: str
    channel_id: str
    message: str
    root_id: str = ""
    create_at: int
    delete_at: int = 0
    update_at: int = 0
    type: str = ""
    content_hash: str = ""
    props: dict[str, Any] = Field(default_factory=dict)


class Notification(BaseModel):
    id: UUID
    recipient_agent_id: UUID
    server_id: str
    post: Post
    kind: str
    state: Literal["unread", "reserved", "consumed"] = "unread"


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    version: int = Field(default=1, gt=0)


class Usage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    provider_usage: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    message: dict[str, Any]
    usage: Usage
