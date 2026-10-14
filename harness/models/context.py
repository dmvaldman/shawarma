"""Replay provider checkpoints; prune only on a rejected DeepSeek request."""

from collections.abc import Sequence
from typing import Any


def openai_compaction(model: str) -> bool:
    return model.startswith(("gpt-6", "gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5.2"))


def anthropic_compaction(model: str) -> bool:
    return model.startswith(
        (
            "claude-fable-5",
            "claude-mythos-5",
            "claude-mythos-preview",
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-opus-4-8",
            "claude-opus-5",
            "claude-sonnet-4-6",
            "claude-sonnet-5",
            "claude-haiku-5-5",
        )
    )


def active_context(
    prompt: str, history: Sequence[dict[str, Any]], new: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    active = list(history)
    for index, item in enumerate(history):
        if item.get("type") in {"compaction", "anthropic_compaction"}:
            active = list(history[index:])
        elif item.get("type") == "context_reset":
            active = list(item["items"]) + list(history[index + 1 :])
    return [{"role": "system", "content": prompt}] + active + list(new)


def prune_earlier_exchanges(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]] | None:
    system = [item for item in items if item.get("role") == "system"]
    groups: list[list[dict[str, Any]]] = []
    pending: set[str] = set()
    for item in items:
        if item.get("role") == "system":
            continue
        if not groups or (
            not pending
            and (
                item.get("role") == "user"
                or item.get("type") == "function_call"
                or groups[-1][-1].get("type") == "function_call_output"
            )
        ):
            groups.append([])
        groups[-1].append(item)
        if item.get("type") == "function_call":
            pending.add(item["call_id"])
        elif item.get("type") == "function_call_output":
            pending.discard(item["call_id"])
    # Keep the latest exchange and incoming message together. No orphaned results.
    if len(groups) <= 2:
        return None
    drop = max(1, (len(groups) - 2 + 1) // 2)
    return system + [item for group in groups[drop:] for item in group]


def context_overflow(status: int, data: dict[str, Any]) -> bool:
    if status != 400:
        return False
    error = data.get("error", {})
    if not isinstance(error, dict):
        return False
    code = str(error.get("code", "")).lower()
    message = str(error.get("message", "")).lower()
    return code in {"context_length_exceeded", "context_window_exceeded", "too_many_tokens"} or (
        "too many tokens" in message
        or "maximum context length" in message
        or ("context" in message and any(word in message for word in ("exceed", "too long")))
    )
