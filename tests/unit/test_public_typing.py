import json
from unittest.mock import AsyncMock

import httpx
import pytest

from harness.models.public_typing import COMPOSER, streamed_response


async def test_typing_only_for_public_post_arguments_and_preserves_final_usage():
    channel = "a" * 26
    hook = AsyncMock()
    events = [
        {
            "type": "response.output_item.added",
            "item": {"id": "private", "type": "function_call", "name": "read_channel"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "private",
            "delta": json.dumps({"channel_id": channel}),
        },
        {"type": "response.reasoning_summary_text.delta", "delta": "Private reasoning"},
        {
            "type": "response.output_item.added",
            "item": {"id": "public", "type": "function_call", "name": "post_channel"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "public",
            "delta": '{"channel_id":"' + channel + '",',
        },
        {"type": "response.function_call_arguments.done", "item_id": "public"},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [],
            },
        },
    ]

    async def server(request):
        assert json.loads(request.content)["stream"]
        return httpx.Response(200, text="\n\n".join("data: " + json.dumps(e) for e in events))

    token = COMPOSER.set(hook)
    try:
        async with httpx.AsyncClient(
            base_url="http://provider/", transport=httpx.MockTransport(server)
        ) as client:
            response = await streamed_response(client, {}, 5)
        assert response.json()["usage"]["output_tokens"] == 5
        assert hook.await_args_list[0].args == (channel, "")
        assert all(call.args == ("", "") for call in hook.await_args_list[1:])
    finally:
        COMPOSER.reset(token)


async def test_incomplete_transport_does_not_invent_usage_or_replay_request():
    calls = 0

    def server(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text='data: {"type":"response.created"}\n\n')

    async with httpx.AsyncClient(
        base_url="http://provider/", transport=httpx.MockTransport(server)
    ) as client:
        with pytest.raises(httpx.ReadError):
            await streamed_response(client, {}, 5)
    assert calls == 1
