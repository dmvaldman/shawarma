import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from harness.config import Settings
from harness.models.openai import ProviderError
from harness.tools.core import TOOLS
from harness.tools.web import fetch_page, public_address, search_web


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1",
        "http://[::1]",
        "https://user:password@example.com",
        "http://example.com:8065",
    ],
)
async def test_fetch_rejects_nonpublic_or_credentialed_urls(url):
    with pytest.raises(ValueError):
        await public_address(httpx.URL(url))


async def test_fetch_pins_public_dns_keeps_tls_identity_and_never_sends_credentials(monkeypatch):
    lookup = AsyncMock(return_value="93.184.215.14")
    monkeypatch.setattr("harness.tools.web.public_address", lookup)

    def server(request):
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<h1>Evidence</h1><script>secret()</script><p>A &amp; B</p>",
        )

    result = await fetch_page("https://example.com", transport=httpx.MockTransport(server))
    assert result["url"] == "https://example.com"
    assert "Evidence" in result["text"] and "A & B" in result["text"]
    assert "secret" not in result["text"]
    assert result["source_type"] == "untrusted_web_content"


async def test_redirect_is_revalidated_and_large_pages_are_bounded(monkeypatch):
    async def resolve(url):
        if url.host == "127.0.0.1":
            raise ValueError("private address")
        return "93.184.215.14"

    monkeypatch.setattr("harness.tools.web.public_address", resolve)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    )
    with pytest.raises(ValueError, match="private"):
        await fetch_page("https://example.com", transport=transport)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"x" * 1_000_001
        )
    )
    with pytest.raises(ValueError, match="one-megabyte"):
        await fetch_page("https://example.com", transport=transport)


async def test_shared_search_metering_citations_and_fail_closed_usage(monkeypatch):
    settings = Settings(openai_api_key="test-key")
    metadata = {"provider": "openai", "api_id": "gpt-6-luna", "input": "0.25", "output": "0.75"}
    catalog = SimpleNamespace(resolve=AsyncMock(return_value=metadata))
    monkeypatch.setattr("harness.tools.web.get_catalog", lambda _: catalog)
    store = SimpleNamespace(reserve_cost=AsyncMock(return_value=uuid4()), finish=AsyncMock())
    missing_usage = False

    def server(request):
        import json

        body = json.loads(request.content)
        assert body["max_tool_calls"] == 1 and body["store"] is False
        assert body["tool_choice"] == {"type": "web_search"}
        return httpx.Response(
            200,
            json={
                "usage": None if missing_usage else {"input_tokens": 1000, "output_tokens": 100},
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {"sources": [{"url": "https://example.com"}]},
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Retrieved evidence.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com",
                                        "title": "Source",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        )

    result = await search_web(
        store, uuid4(), settings, "evidence", transport=httpx.MockTransport(server)
    )
    assert result["sources"] == [{"url": "https://example.com", "title": "Source"}]
    usage = store.finish.await_args.args[1]
    assert usage.cost_usd == Decimal("0.010325")
    assert store.reserve_cost.await_args.args[1] > usage.cost_usd
    assert store.reserve_cost.await_args.kwargs["provider_model_id"] == "openai:gpt-6-luna"
    missing_usage = True
    with pytest.raises(ProviderError, match="reliable usage"):
        await search_web(
            store, uuid4(), settings, "evidence", transport=httpx.MockTransport(server)
        )
    assert store.finish.await_args.args[1] is None
    assert {"web_search", "web_fetch"} <= {t.name for t in TOOLS}


async def test_search_cancellation_settles_reserved_usage(monkeypatch):
    monkeypatch.setattr(
        "harness.tools.web.get_catalog",
        lambda _: SimpleNamespace(
            resolve=AsyncMock(
                return_value={"api_id": "gpt-6-luna", "input": "0.25", "output": "0.75"}
            )
        ),
    )
    store = SimpleNamespace(reserve_cost=AsyncMock(return_value=uuid4()), finish=AsyncMock())

    async def server(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await search_web(
            store,
            uuid4(),
            Settings(openai_api_key="test-key"),
            "query",
            transport=httpx.MockTransport(server),
        )
    assert store.finish.await_args.args[1] is None


async def test_search_preserves_safe_provider_error_and_marks_shared_cooldown(monkeypatch):
    from harness.tools.retrieval_guard import RetrievalError

    monkeypatch.setattr(
        "harness.tools.web.get_catalog",
        lambda _: SimpleNamespace(
            resolve=AsyncMock(
                return_value={"api_id": "gpt-6-luna", "input": "0.25", "output": "0.75"}
            )
        ),
    )
    store = SimpleNamespace(reserve_cost=AsyncMock(return_value=uuid4()), finish=AsyncMock())
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            429,
            json={"error": {"code": "insufficient_quota", "message": "private-provider-message"}},
        )
    )
    with pytest.raises(RetrievalError) as caught:
        await search_web(
            store, uuid4(), Settings(openai_api_key="test-key"), "query", transport=transport
        )
    assert caught.value.code == "insufficient_quota"
    assert caught.value.provider and caught.value.retry_after == 3600
    assert "private-provider-message" not in str(caught.value)
    assert store.finish.await_args.args[1] is None


async def test_search_keeps_successful_result_above_estimate_with_multiple_calls(monkeypatch):
    settings = Settings(openai_api_key="test-key")
    metadata = {"api_id": "gpt-6-luna", "input": "0.25", "output": "0.75"}
    monkeypatch.setattr(
        "harness.tools.web.get_catalog",
        lambda _: SimpleNamespace(resolve=AsyncMock(return_value=metadata)),
    )
    store = SimpleNamespace(reserve_cost=AsyncMock(return_value=uuid4()), finish=AsyncMock())

    def server(request):
        return httpx.Response(
            200,
            json={
                "usage": {"input_tokens": 250000, "output_tokens": 100},
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {"sources": [{"url": "https://example.com"}]},
                    },
                    {"type": "web_search_call", "action": {}},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Useful finding."}],
                    },
                ],
            },
        )

    result = await search_web(
        store, uuid4(), settings, "evidence", transport=httpx.MockTransport(server)
    )
    assert result["text"] == "Useful finding."
    assert result["sources"][0]["url"] == "https://example.com"
    assert result["search_calls"] == 2
    store.finish.assert_awaited_once()
    usage = store.finish.await_args.args[1]
    assert usage.input_tokens == 250000
    assert usage.cost_usd == Decimal("0.082575")
    assert usage.cost_usd > store.reserve_cost.await_args.args[1]


async def test_pdf_is_parsed_from_bytes_and_returns_only_text(monkeypatch):
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (Concrete evidence) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    binary = io.BytesIO()
    writer.write(binary)
    monkeypatch.setattr("harness.tools.web.public_address", AsyncMock(return_value="93.184.215.14"))
    result = await fetch_page(
        "https://example.com/paper.pdf",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=binary.getvalue()
            )
        ),
    )
    assert result["text"] == "Concrete evidence"
    assert not result["truncated"]
    assert set(result) == {"url", "text", "truncated", "source_type"}


async def test_corrupted_pdf_fails_before_returning_attachment(monkeypatch):
    monkeypatch.setattr("harness.tools.web.public_address", AsyncMock(return_value="93.184.215.14"))
    with pytest.raises(ValueError, match="could not be parsed"):
        await fetch_page(
            "https://example.com/bad.pdf",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\ncorrupt"
                )
            ),
        )
