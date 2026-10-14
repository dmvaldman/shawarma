from harness.models.context import active_context, context_overflow, prune_earlier_exchanges


def exchange(i):
    return [
        {"type": "function_call", "call_id": str(i), "name": "read_channel", "arguments": "{}"},
        {"type": "function_call_output", "call_id": str(i), "output": "result"},
    ]


def test_full_history_is_kept_until_provider_checkpoint():
    history = [{"role": "user", "content": "initial task"}] + exchange(0)
    for i in range(1, 50):
        history += exchange(i)
    assert (
        active_context("fixed", history, []) == [{"role": "system", "content": "fixed"}] + history
    )


def test_openai_checkpoint_is_preserved_verbatim_with_later_exchanges():
    block = {"type": "compaction", "id": "cmp", "encrypted_content": "opaque-state"}
    history = [{"role": "user", "content": "old"}] + exchange(0) + [block] + exchange(1)
    new = [{"role": "user", "content": "steering"}]
    assert active_context("fixed", history, new)[1:] == [block] + exchange(1) + new


def test_claude_checkpoint_and_deepseek_reset_survive_replay():
    block = {"type": "anthropic_compaction", "block": {"type": "compaction", "content": "summary"}}
    assert active_context("fixed", exchange(0) + [block] + exchange(1), [])[1:] == [
        block
    ] + exchange(1)
    kept = [{"role": "user", "content": "latest"}] + exchange(1)
    reset = {"type": "context_reset", "items": kept}
    assert active_context("fixed", exchange(0) + [reset] + exchange(2), [])[1:] == kept + exchange(
        2
    )


def test_deepseek_clears_earlier_exchanges_without_orphaning_parallel_tools():
    history = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "old"}]
    for i in range(8):
        history += exchange(i)
    # Preserve one parallel tool exchange and the new notification together.
    history += [exchange(8)[0], exchange(9)[0], exchange(8)[1], exchange(9)[1]]
    history += [{"role": "user", "content": "latest request"}]
    smaller = prune_earlier_exchanges(history)
    assert smaller and len(smaller) < len(history)
    assert smaller[0] == history[0] and smaller[-1] == history[-1]
    calls = {i["call_id"] for i in smaller if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in smaller if i.get("type") == "function_call_output"}
    assert calls == outputs and {"8", "9"} <= calls
    assert prune_earlier_exchanges([history[0]] + exchange(8) + [history[-1]]) is None


def test_only_explicit_context_overflow_is_retryable():
    assert context_overflow(400, {"error": {"code": "context_length_exceeded"}})
    assert context_overflow(400, {"error": {"message": "maximum context length is exceeded"}})
    assert not context_overflow(401, {"error": {"code": "context_length_exceeded"}})
    assert not context_overflow(400, {"error": {"message": "Invalid tool schema"}})
