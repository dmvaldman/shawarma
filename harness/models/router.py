"""Canonical local history routed to explicit provider IDs without substitution."""

import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import httpx

from harness.config import Settings
from harness.core.types import ModelResponse, ToolSpec, Usage
from harness.models.catalog import ModelCatalog, get_catalog
from harness.models.context import anthropic_compaction, context_overflow, prune_earlier_exchanges
from harness.models.openai import OpenAIProvider, response_error


def chat_messages(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            if not result or result[-1].get("role") != "assistant":
                result.append({"role": "assistant", "content": None})
            result[-1].setdefault("tool_calls", []).append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"],
                    },
                }
            )
        elif kind == "function_call_output":
            result.append(
                {"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]}
            )
        elif item.get("role"):
            content = item.get("content", "")
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content)
            message = {"role": item["role"], "content": content}
            if item.get("provider_reasoning"):
                message["reasoning_content"] = item["provider_reasoning"]
            result.append(message)
    return result


def anthropic_messages(items: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    messages: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("type")
        if kind == "anthropic_compaction":
            role, block = "assistant", item["block"]
        elif kind == "function_call":
            role, block = (
                "assistant",
                {
                    "type": "tool_use",
                    "id": item["call_id"],
                    "name": item["name"],
                    "input": json.loads(item["arguments"]),
                },
            )
        elif kind == "function_call_output":
            role, block = (
                "user",
                {"type": "tool_result", "tool_use_id": item["call_id"], "content": item["output"]},
            )
        elif item.get("role"):
            content = item.get("content", "")
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content)
            if item["role"] == "system":
                system.append(content)
                continue
            role, block = item["role"], {"type": "text", "text": content or "[No text]"}
        else:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": role, "content": [block]})
    return "\n\n".join(system), messages


class ProviderRouter:
    def __init__(
        self,
        settings: Settings,
        catalog: ModelCatalog | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings, self.catalog = settings, catalog or get_catalog(settings)
        self.http = httpx.AsyncClient(transport=transport)
        self.openai: dict[str, OpenAIProvider] = {}
        self.transport = transport
        self.profiles: dict[str, dict[str, Any]] = {}

    async def prepare(self, model: str) -> dict[str, Any]:
        metadata = await self.catalog.resolve(model)
        self.profiles[model] = metadata
        return metadata

    async def verify_model(self) -> None:
        await self.prepare(self.settings.seed_model_id)

    async def close(self) -> None:
        await self.http.aclose()
        for provider in self.openai.values():
            await provider.close()

    def bound(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        model_id: str,
    ) -> Decimal:
        size = len(json.dumps(list(messages), ensure_ascii=False).encode())
        size += len(json.dumps([t.model_dump() for t in tools], ensure_ascii=False).encode())
        meta = self.profiles[model_id]
        return (
            Decimal(size + 4096) * Decimal(meta["input"])
            + Decimal(max_tokens) * Decimal(meta["output"])
        ) / 1_000_000

    async def generate(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec],
        model_id: str,
        max_tokens: int,
        timeout: float,  # noqa: ASYNC109
    ) -> ModelResponse:  # noqa: ASYNC109
        meta = await self.prepare(model_id)
        name, api_id = meta["provider"], meta["api_id"]
        self.bound(messages, tools, max_tokens, model_id)
        if (
            name == "openai"
            and not api_id.startswith(("gpt-3.5", "gpt-4-", "ft:"))
            and api_id != "gpt-4"
        ):
            if model_id not in self.openai:
                copied = self.settings.model_copy(
                    update={
                        "openai_model_id": api_id,
                        "input_usd_per_million": Decimal(meta["input"]),
                        "output_usd_per_million": Decimal(meta["output"]),
                    }
                )
                self.openai[model_id] = OpenAIProvider(copied, self.transport)
            return await self.openai[model_id].generate(
                messages, tools, api_id, max_tokens, timeout
            )
        url, headers = self.catalog.providers()[name]
        if name == "anthropic":
            system, translated = anthropic_messages(messages)
            payload = {
                "model": api_id,
                "system": system,
                "messages": translated,
                "max_tokens": max_tokens,
                "cache_control": {"type": "ephemeral"},
                "tools": [
                    {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                    for t in tools
                ],
            }
            if anthropic_compaction(api_id):
                headers = {**headers, "anthropic-beta": "compact-2026-01-12"}
                payload["context_management"] = {"edits": [{"type": "compact_20260112"}]}
            endpoint = "/messages"
        else:
            payload = {
                "model": api_id,
                "messages": chat_messages(messages),
                "max_tokens": max_tokens,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.input_schema,
                        },
                    }
                    for t in tools
                ],
            }
            endpoint = "/chat/completions"
            if name == "deepseek":
                payload["thinking"] = {"type": "disabled"}
        retained = list(messages)
        pruned = False
        rate_retries = 0
        while True:
            response = await self.http.post(
                url + endpoint, headers=headers, json=payload, timeout=timeout
            )
            if (
                response.status_code == 429
                and rate_retries < 2
                and response_error(response, name).code
                not in {"billing_or_usage_limit", "insufficient_quota"}
            ):
                await asyncio.sleep(2**rate_retries)
                rate_retries += 1
                continue
            if name == "deepseek" and response.status_code == 400:
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                if context_overflow(response.status_code, error):
                    smaller = prune_earlier_exchanges(retained)
                    if smaller is not None:
                        retained = smaller
                        payload["messages"] = chat_messages(retained)
                        pruned = True
                        continue
            if response.is_error:
                raise response_error(response, name)
            break
        data = response.json()
        output: list[dict[str, Any]] = []
        if pruned:
            output.append(
                {
                    "type": "context_reset",
                    "provider": name,
                    "items": [item for item in retained if item.get("role") != "system"],
                }
            )
        if name == "anthropic":
            for block in data["content"]:
                if block["type"] == "compaction" and block.get("content"):
                    output.append({"type": "anthropic_compaction", "block": block})
                elif block["type"] == "tool_use":
                    output.append(
                        {
                            "type": "function_call",
                            "call_id": block["id"],
                            "name": block["name"],
                            "arguments": json.dumps(block["input"]),
                        }
                    )
                elif block["type"] == "text":
                    output.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": block["text"]}],
                        }
                    )
            u = data["usage"]
            inp = (
                u["input_tokens"]
                + u.get("cache_creation_input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
            )
            out = u["output_tokens"]
        else:
            msg = data["choices"][0]["message"]
            if msg.get("content") or msg.get("reasoning_content"):
                output.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": msg.get("content") or ""}],
                        "provider_reasoning": msg.get("reasoning_content", ""),
                    }
                )
            for call in msg.get("tool_calls", []):
                output.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                )
            inp, out = data["usage"]["prompt_tokens"], data["usage"]["completion_tokens"]
        return ModelResponse(
            message={"id": data["id"], "output": output, "status": "completed"},
            usage=Usage(
                provider_usage=data["usage"],
                input_tokens=inp,
                output_tokens=out,
                cost_usd=(
                    Decimal(inp) * Decimal(meta["input"]) + Decimal(out) * Decimal(meta["output"])
                )
                / 1_000_000,
            ),
        )
