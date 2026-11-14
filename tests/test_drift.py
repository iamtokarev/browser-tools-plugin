"""Drift tests: an upstream change to the tool contract must not enter the repo unnoticed.

A failure here means a pinned package changed. Read the change, then update the copy
on purpose. Do not update the copy only to make the test pass.
"""

import hashlib
import json
from pathlib import Path

import pytest
import stagehand_middleware
from mcp import Client

from browser_tools_plugin.facade import upstream_tool
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings

TOOLS = ("snapshot", "run", "screenshot")
SCHEMA_COPY = Path(__file__).parent / "data" / "upstream_tool_schemas.json"

# SHA-256 of the _bundle.json in stagehand-middleware 0.1.2.
BUNDLE_SHA256 = "5da7dac1f593d21fcbe1f2f47797ee5b898fa5caccda44348ce7e4d05e366b0e"


def _copy() -> dict[str, dict]:
    return json.loads(SCHEMA_COPY.read_text())


def test_the_copy_holds_the_three_core_tools():
    assert sorted(_copy()) == sorted(TOOLS)


@pytest.mark.parametrize("name", TOOLS)
def test_the_installed_middleware_has_the_argument_schema_we_copied(name: str):
    assert upstream_tool(name)["inputSchema"] == _copy()[name], (
        f"upstream changed the {name} argument schema; review it, then update {SCHEMA_COPY.name}"
    )


@pytest.mark.parametrize("name", TOOLS)
async def test_the_server_advertises_the_copied_schema(name: str, tmp_path: Path):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == name)
    assert tool.input_schema == _copy()[name]


def test_the_middleware_bundle_has_the_checksum_we_reviewed():
    bundle = Path(stagehand_middleware.__file__).parent / "facade" / "_bundle.json"
    actual = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert actual == BUNDLE_SHA256, (
        "the stagehand-middleware _bundle.json changed; review the change, then update BUNDLE_SHA256"
    )
