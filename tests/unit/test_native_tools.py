from harness.coding.tool_source import tool_source
from harness.tools.core import TOOLS


def test_native_tools_preserve_schemas_and_bind_identity_from_session():
    for manifest in TOOLS:
        source = tool_source(manifest.name, manifest.description, manifest.input_schema)
        assert "context.sessionID" in source
        assert "context.abort" in source
        assert "crypto.randomUUID()" in source
        assert 'fetch(process.env.SWARM_TOOL_URL + "/tools"' in source
        assert "agent_id:context" not in source
        assert source.count("await fetch(") == 1


def test_native_agent_has_no_coding_handoff_tool():
    assert "code_task" not in {t.name for t in TOOLS}
