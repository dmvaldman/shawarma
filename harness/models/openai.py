import asyncio
import json
import re
from collections.abc import Sequence
from decimal import Decimal
from hashlib import sha256
from typing import Any

import httpx

from harness.config import Settings
from harness.core.types import ModelResponse, ToolSpec, Usage
from harness.models.context import openai_compaction
from harness.models.public_typing import COMPOSER, streamed_response


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "provider_error",
        policy_rejected: bool = False,
    ):
        self.status, self.code = status, code
        self.policy_rejected = policy_rejected
        super().__init__(message)


def response_error(response: httpx.Response, provider: str) -> ProviderError:
    try:
        error = response.json().get("error", {})
        code = error.get("code") or error.get("type") or "request_failed"
        message = str(error.get("message", "")).lower()
    except (ValueError, AttributeError):
        code, message = "request_failed", ""
    if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", code):
        code = "request_failed"
    if any(
        x in message for x in ("usage limit", "credit balance", "billing", "insufficient quota")
    ):
        code = "billing_or_usage_limit"
    policy_rejected = "flagged" in message and "policy" in message
    detail = "; provider flagged the prompt under its usage policy" if policy_rejected else ""
    return ProviderError(
        f"{provider} HTTP {response.status_code}: {code}{detail}; no model substitution",
        status=response.status_code,
        code=code,
        policy_rejected=policy_rejected,
    )


class OpenAIProvider:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        if not settings.openai_api_key.get_secret_value():
            raise ProviderError("OPENAI_API_KEY is missing")
        self.http = httpx.AsyncClient(
            base_url=str(settings.openai_base_url).rstrip("/") + "/",
            headers={"Authorization": "Bearer " + settings.openai_api_key.get_secret_value()},
            transport=transport,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def verify_model(self) -> None:
        r = await self.http.get("models/" + self.settings.openai_model_id, timeout=30)
        if r.is_error:
            raise ProviderError(
                f"Configured model {self.settings.openai_model_id} unavailable "
                f"(HTTP {r.status_code}); no substitute"
            )

    def bound(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[ToolSpec], max_tokens: int
    ) -> Decimal:
        # UTF-8 bytes bound BPE input tokens; include structural overhead and tool schemas.
        size = len(json.dumps(list(messages), ensure_ascii=False).encode())
        schemas = len(json.dumps([t.model_dump() for t in tools], ensure_ascii=False).encode())
        return (
            Decimal(size + schemas + 4096) * self.settings.input_usd_per_million
            + Decimal(max_tokens) * self.settings.output_usd_per_million
        ) / Decimal(1_000_000)

    async def generate(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec],
        model_id: str,
        max_tokens: int,
        timeout: float,  # noqa: ASYNC109 - transport deadline
    ) -> ModelResponse:  # noqa: ASYNC109
        if model_id != self.settings.openai_model_id:
            raise ProviderError("Model is outside the configured allowlist")
        self.bound(messages, tools, max_tokens)
        payload = {
            "model": model_id,
            "input": list(messages),
            "store": False,
            "max_output_tokens": max_tokens,
            "service_tier": "default",
            "reasoning": {"effort": "none"},
            "tools": [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    "strict": False,
                }
                for t in tools
            ],
        }
        if model_id.startswith(("gpt-5", "gpt-6", "gpt-4.1", "gpt-4o", "o1", "o3", "o4")):
            # Stable across turns, separate per agent via its immutable system identity.
            prefix = json.dumps(
                [item for item in messages if item.get("role") in {"system", "developer"}],
                sort_keys=True,
            )
            payload["prompt_cache_key"] = (
                "swarm:" + sha256((model_id + prefix).encode()).hexdigest()[:40]
            )
        if openai_compaction(model_id):
            payload["context_management"] = [{"type": "compaction", "compact_threshold": 200000}]
            payload["include"] = ["reasoning.encrypted_content"]
        if model_id != "gpt-6-luna":
            payload.pop("reasoning")
        for attempt in range(3):
            r = (
                await streamed_response(self.http, payload, timeout)
                if COMPOSER.get() is not None
                else await self.http.post("responses", json=payload, timeout=timeout)
            )
            if (
                r.status_code != 429
                or attempt == 2
                or response_error(r, "OpenAI").code
                in {"billing_or_usage_limit", "insufficient_quota"}
            ):
                break
            # Only explicit rate-limit rejection is retried. Ambiguous sends fail closed.
            await asyncio.sleep(2**attempt)
        if r.is_error:
            raise response_error(r, "OpenAI")
        data = r.json()
        raw_usage = data.get("usage")
        if not raw_usage:
            raise ProviderError("No reliable usage returned; charge reservation conservatively")
        usage = Usage(
            provider_usage=raw_usage,
            input_tokens=raw_usage["input_tokens"],
            output_tokens=raw_usage["output_tokens"],
            cost_usd=(
                Decimal(raw_usage["input_tokens"]) * self.settings.input_usd_per_million
                + Decimal(raw_usage["output_tokens"]) * self.settings.output_usd_per_million
            )
            / Decimal(1_000_000),
        )
        return ModelResponse(
            message={"id": data["id"], "output": data["output"], "status": data["status"]},
            usage=usage,
        )
