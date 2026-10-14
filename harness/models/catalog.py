"""Credential-scoped discovery, one shared TTL cache, and explicit model metadata."""

import asyncio
import hashlib
import json
import logging
import time
from decimal import Decimal
from importlib.resources import files
from typing import Any

import httpx

from harness.coding.clients import OpenCode
from harness.config import Settings

# Conservative standard-tier prices; no cache/batch discounts. Unknown models use
# explicit configurable ceilings, not the cheap seed's rates.
PROFILES: dict[str, tuple[str, str, str]] = {
    "gpt-6-luna": ("0.25", "0.75", "lite"),
    "gpt-6.1-sol": ("5", "15", "balanced"),
    "gpt-6-sol": ("5", "15", "balanced"),
    "gpt-6-astra": ("25", "75", "frontier"),
    "claude-haiku-5-5": ("0.625", "2.5", "lite"),
    "claude-sonnet-5-5": ("2.5", "10", "balanced"),
    "claude-opus-5-5": ("5", "20", "frontier"),
    "claude-fable": ("12.5", "50", "frontier"),
    "claude-opus": ("6.25", "25", "frontier"),
    "claude-sonnet": ("3.75", "15", "balanced"),
    "claude-haiku": ("1.25", "5", "lite"),
}


def profile(provider: str, model: str, settings: Settings) -> dict[str, Any]:
    profiles = {
        **{
            k: (v["input"], v["output"], v["class"])
            for k, v in json.loads(
                files("harness.models").joinpath("prices.json").read_text()
            ).items()
        },
        **PROFILES,
    }
    found = next(
        (profiles[k] for k in sorted(profiles, key=len, reverse=True) if model.startswith(k)), None
    )
    if provider == "openai" and model == settings.openai_model_id:
        found = (
            str(settings.input_usd_per_million),
            str(settings.output_usd_per_million),
            found[2] if found else "unknown",
        )
    if model in settings.model_profiles:
        custom = settings.model_profiles[model]
        found = (str(custom["input"]), str(custom["output"]), str(custom.get("class", "unknown")))
    inp, out, capability = found or (
        str(settings.unknown_input_usd_per_million),
        str(settings.unknown_output_usd_per_million),
        "unknown",
    )
    cost = (
        "c1"
        if Decimal(inp) + Decimal(out) <= 4
        else "c2"
        if Decimal(inp) + Decimal(out) <= 30
        else "c3"
    )
    # Owner-selected peer tier; keep numeric price estimates independent of this label.
    if provider == "deepseek" and model == "deepseek-flash":
        cost, capability = "c1", "lite"
    return {
        "input": inp,
        "output": out,
        "class": capability,
        "cost": cost,
        "pricing_basis": "configured conservative estimate" if found else "configured ceiling",
    }


class ModelCatalog:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings, self.transport = settings, transport
        self.lock = asyncio.Lock()
        self.expires = 0.0
        self.result: dict[str, Any] = {}

    def providers(self) -> dict[str, tuple[str, dict[str, str]]]:
        s = self.settings
        configured = {}
        for name, url, secret in (
            ("openai", str(s.openai_base_url), s.openai_api_key),
            ("anthropic", str(s.anthropic_base_url), s.anthropic_api_key),
            ("deepseek", str(s.deepseek_base_url), s.deepseek_api_key),
        ):
            key = secret.get_secret_value()
            if key:
                headers = (
                    {"x-api-key": key, "anthropic-version": "2023-06-01"}
                    if name == "anthropic"
                    else {"Authorization": "Bearer " + key}
                )
                configured[name] = (url.rstrip("/"), headers)
        return configured

    async def list(self) -> dict[str, Any]:
        if time.monotonic() < self.expires:
            return {**self.result, "cached": True}
        async with self.lock:
            if time.monotonic() < self.expires:
                return {**self.result, "cached": True}
            async with httpx.AsyncClient(transport=self.transport, timeout=15) as http:

                async def fetch(
                    name: str, url: str, headers: dict[str, str]
                ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
                    raw = []
                    params: dict[str, Any] = {"limit": 1000} if name == "anthropic" else {}
                    try:
                        while True:
                            r = await http.get(url + "/models", headers=headers, params=params)
                            r.raise_for_status()
                            page = r.json()
                            raw.extend(page["data"])
                            if not page.get("has_more"):
                                break
                            params["after_id"] = page["last_id"]
                        models = []
                        for m in raw:
                            mid = m["id"]
                            text = name != "openai" or (
                                mid.startswith(("gpt-", "chat", "o1", "o3", "o4", "ft:gpt-"))
                                and not any(
                                    x in mid
                                    for x in (
                                        "audio",
                                        "image",
                                        "realtime",
                                        "transcrib",
                                        "tts",
                                        "instruct",
                                        "search",
                                        "deep-research",
                                    )
                                )
                            )
                            models.append(
                                {
                                    "id": name + ":" + mid,
                                    "provider": name,
                                    "api_id": mid,
                                    "name": m.get("display_name", m.get("name", mid)),
                                    "agent_candidate": text,
                                    "verified": False,
                                    "context_window": m.get(
                                        "max_input_tokens", m.get("context_window")
                                    ),
                                    "max_output_tokens": m.get(
                                        "max_tokens", m.get("max_output_tokens")
                                    ),
                                    **profile(name, mid, self.settings),
                                }
                            )
                        return models, {"status": "ok", "count": len(models)}
                    except (httpx.HTTPError, ValueError, KeyError):
                        logging.getLogger(__name__).warning(
                            "Model catalog retrieval failed for provider %s", name
                        )
                        return [], {"status": "unavailable"}

                names = self.providers()
                results = await asyncio.gather(*(fetch(n, *value) for n, value in names.items()))
            if self.settings.agent_engine == "opencode" and self.settings.coding_enabled:
                client = OpenCode(self.settings)
                try:
                    runtime = await client.request("GET", "/provider")
                finally:
                    await client.close()
                available = {
                    p["id"] + ":" + mid
                    for p in runtime["all"]
                    if p["id"] in runtime["connected"]
                    for mid, meta in p["models"].items()
                    if meta.get("limit", {}).get("context", 0) > 0
                    and meta.get("capabilities", {}).get("toolcall", True)
                }
                for models, _ in results:
                    for model in models:
                        model["agent_candidate"] &= model["id"] in available
            self.result = {
                "models": sorted(
                    [m for models, _ in results for m in models], key=lambda m: m["id"]
                ),
                "providers": {n: result[1] for n, result in zip(names, results, strict=True)},
                "ttl_sec": self.settings.model_cache_ttl_sec,
            }
            self.expires = time.monotonic() + self.settings.model_cache_ttl_sec
            return {**self.result, "cached": False}

    async def resolve(self, value: str) -> dict[str, Any]:
        # Accept an unambiguous provider/model spelling; never invent or substitute a model.
        provider, separator, api_id = value.partition("/")
        if separator and provider in {"openai", "anthropic", "deepseek"}:
            value = provider + ":" + api_id
        models = (await self.list())["models"]
        found = [m for m in models if value in {m["id"], m["api_id"]}]
        if len(found) != 1 or not found[0]["agent_candidate"]:
            raise ValueError(
                "MODEL_NOT_ALLOWED: choose an available text agent model from list_models"
            )
        return dict(found[0])


_catalogs: dict[str, ModelCatalog] = {}


def get_catalog(settings: Settings) -> ModelCatalog:
    fields = [
        str(settings.openai_base_url),
        str(settings.anthropic_base_url),
        str(settings.deepseek_base_url),
        settings.openai_api_key.get_secret_value(),
        settings.anthropic_api_key.get_secret_value(),
        settings.deepseek_api_key.get_secret_value(),
        settings.model_dump_json(),
    ]
    key = hashlib.sha256("\0".join(fields).encode()).hexdigest()
    if key not in _catalogs:
        _catalogs[key] = ModelCatalog(settings)
    return _catalogs[key]
