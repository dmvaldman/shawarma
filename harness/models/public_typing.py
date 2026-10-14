"""Request-scoped public composer hints; never inspect or publish reasoning."""

import json
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import httpx

COMPOSER: ContextVar[Callable[[str, str], Awaitable[None]] | None] = ContextVar(
    "public_composer", default=None
)


async def streamed_response(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    timeout: float,  # noqa: ASYNC109
) -> httpx.Response:  # noqa: ASYNC109
    arguments: dict[str, str] = {}
    names: dict[str, str] = {}
    hook = COMPOSER.get()
    async with client.stream(
        "POST", "responses", json={**payload, "stream": True}, timeout=timeout
    ) as response:
        if response.is_error:
            await response.aread()
            return response
        async for line in response.aiter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            event = json.loads(line[5:])
            kind = event.get("type")
            if kind == "response.output_item.added":
                item = event["item"]
                if item.get("type") == "function_call":
                    names[item["id"]] = item.get("name", "")
            elif kind == "response.function_call_arguments.delta":
                key = event["item_id"]
                if names.get(key) != "post_channel":
                    continue
                arguments[key] = arguments.get(key, "") + event["delta"]
                channel = re.search(r'"channel_id"\s*:\s*"([a-z0-9]{26})"', arguments[key])
                root = re.search(r'"root_post_id"\s*:\s*"([a-z0-9]{26})"', arguments[key])
                if hook and channel:
                    await hook(channel[1], root[1] if root else "")
            elif kind == "response.function_call_arguments.done":
                if hook and names.get(event["item_id"]) == "post_channel":
                    await hook("", "")
            elif kind in {"response.completed", "response.incomplete"}:
                if hook:
                    await hook("", "")
                return httpx.Response(200, json=event["response"], request=response.request)
            elif kind == "response.failed":
                return httpx.Response(
                    400,
                    json={"error": event["response"].get("error", {})},
                    request=response.request,
                )
    raise httpx.ReadError("OpenAI stream ended without a final response")
