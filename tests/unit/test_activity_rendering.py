"""Exercise the pure display formatter without browser automation or model calls."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_reasoning_display_distinguishes_missing_summaries_from_readable_text():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for dashboard formatter gate")
    source = next(
        line
        for line in Path("harness/api/dashboard.html").read_text().splitlines()
        if line.startswith("function payloadText(")
    )
    script = (
        source
        + """
const assert = require('node:assert/strict');
const exposed={provider:'openai',part:{type:'reasoning',text:'Summary text'}};
const empty={provider:'openai',part:{type:'reasoning',text:''}};
assert.equal(payloadText(exposed,'completed'),'Summary text');
assert.match(payloadText(empty,'running'),/in progress/);
empty.part.metadata={openai:{reasoningEncryptedContent:'opaque'}};
assert.match(payloadText(empty,'completed'),/No readable summary/);
empty.provider='anthropic';delete empty.part.metadata;
assert.match(payloadText(empty,'completed'),/did not expose/);
"""
    )
    result = subprocess.run([node], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_empty_completed_openai_reasoning_is_not_displayed_as_a_summary():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for dashboard formatter gate")
    source = next(
        line
        for line in Path("harness/api/dashboard.html").read_text().splitlines()
        if line.startswith("function visibleActivity(")
    )
    script = (
        source
        + """
const assert = require('node:assert/strict');
const empty={phase:'completed',payload:{provider:'openai',part:{type:'reasoning',text:''}}};
assert.equal(visibleActivity(empty),false);
assert.equal(visibleActivity({...empty,phase:'running'}),true);
assert.equal(visibleActivity({...empty,payload:{provider:'openai',part:{type:'reasoning',text:'Summary'}}}),true);
assert.equal(visibleActivity({phase:'completed',payload:{part:{type:'tool'}}}),true);
assert.equal(visibleActivity({...empty,payload:{part:{type:'reasoning',text:'',metadata:{openai:{itemId:'rs_1'}}}}}),false);
assert.equal(visibleActivity({...empty,payload:{provider:'anthropic',part:{type:'reasoning',text:''}}}),true);
"""
    )
    result = subprocess.run([node], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
