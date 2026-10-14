"""Shared, bounded negative cache for unavailable retrievals; never cache model answers."""

import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from harness.models.openai import ProviderError


class RetrievalError(ProviderError):
    def __init__(self, detail: str, code: str, *, retry_after: int = 60, provider: bool = False):
        super().__init__(detail)
        self.code, self.retry_after, self.provider = code, retry_after, provider


class RetrievalGuard:
    def __init__(self) -> None:
        self.failures: dict[str, tuple[float, dict[str, Any]]] = {}

    async def run(
        self,
        key: str,
        provider_key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        now = time.monotonic()
        for candidate in (provider_key, key):
            cached = self.failures.get(candidate)
            if cached and cached[0] > now:
                return {
                    **cached[1],
                    "cached_error": True,
                    "retry_after_sec": max(1, int(cached[0] - now)),
                }
        try:
            return await operation()
        except RetrievalError as error:
            code, detail, delay = error.code, str(error), error.retry_after
            cache_key = provider_key if error.provider else key
        except ProviderError as error:
            code, detail, delay, cache_key = "ProviderError", str(error), 300, provider_key
        except (httpx.HTTPError, OSError) as error:
            code, delay, cache_key = type(error).__name__, 60, key
            detail = "Retrieval transport failed; try another source while this request cools down."
        except ValueError as error:
            code, delay, cache_key = "UNSUPPORTED_RETRIEVAL", 3600, key
            message = str(error)
            detail = (
                message
                if message.startswith(
                    ("Only ", "Private, ", "Web page ", "Compressed ", "Too many web", "Redirect ")
                )
                else "Invalid or unavailable retrieval request."
            )
        result = {
            "error": code,
            "detail": detail,
            "retry_after_sec": delay,
            "instruction": (
                "Do not repeat this request during cooldown. Use other evidence or approaches."
            ),
        }
        # Process-local state resets on deployment; failures retry after a finite cooldown.
        self.failures[cache_key] = (time.monotonic() + delay, result)
        if len(self.failures) > 256:
            self.failures.pop(next(iter(self.failures)))
        return result


RETRIEVAL_GUARD = RetrievalGuard()
