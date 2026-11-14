"""The plugin manifests: one metadata set in two files, one server definition, one version. No test starts Chrome."""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PLUGIN_NAME = "browser-tools"
REPOSITORY = "https://github.com/iamtokarev/browser-tools-plugin"
# Fields that only one of the two plugin.json files may hold.
STANDARD_ONLY = {"$schema"}
CLAUDE_ONLY = {"mcpServers"}
# Files and directories that the one-layout design makes unnecessary. Each one would be a second source of truth.
NOT_WANTED = (".mcp.json", ".codex-plugin", ".agents/plugins/marketplace.json")


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text())


def _package_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _server() -> dict:
    return _json("mcp.json")["mcpServers"][PLUGIN_NAME]


def test_the_two_plugin_manifests_carry_the_same_metadata():
    standard = {k: v for k, v in _json("plugin.json").items() if k not in STANDARD_ONLY}
    claude = {k: v for k, v in _json(".claude-plugin/plugin.json").items() if k not in CLAUDE_ONLY}
    assert standard == claude, "the two plugin.json files must carry the same metadata"


def test_each_plugin_manifest_holds_only_its_own_extra_fields():
    standard, claude = _json("plugin.json"), _json(".claude-plugin/plugin.json")
    assert standard.keys() >= STANDARD_ONLY
    assert not CLAUDE_ONLY & standard.keys()
    assert claude.keys() >= CLAUDE_ONLY
    assert not STANDARD_ONLY & claude.keys()


def test_the_claude_manifest_points_at_the_shared_server_file():
    assert _json(".claude-plugin/plugin.json")["mcpServers"] == "./mcp.json"
    assert (ROOT / "mcp.json").is_file()


def test_the_schema_versions_of_the_two_standard_files_agree():
    versions = {_json(f)["$schema"].split("/schemas/")[1].split("/")[0] for f in ("plugin.json", "mcp.json")}
    assert versions == {"1.0.0"}


@pytest.mark.parametrize("path", ["plugin.json", ".claude-plugin/plugin.json"])
def test_the_plugin_and_the_manifest_name_are_browser_tools(path: str):
    assert _json(path)["name"] == PLUGIN_NAME


def test_the_marketplace_lists_the_one_plugin_from_the_repository_root():
    marketplace = _json(".claude-plugin/marketplace.json")
    assert [(p["name"], p["source"]) for p in marketplace["plugins"]] == [(PLUGIN_NAME, "./")]


def test_the_marketplace_entry_repeats_the_plugin_description():
    (entry,) = _json(".claude-plugin/marketplace.json")["plugins"]
    assert entry["description"] == _json("plugin.json")["description"]


def test_the_server_is_named_browser_tools_and_starts_the_console_script():
    assert list(_json("mcp.json")["mcpServers"]) == [PLUGIN_NAME]
    server = _server()
    assert (server["type"], server["command"]) == ("stdio", "uvx")
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert server["args"][-1] in scripts


def test_the_server_starts_from_the_git_tag_of_the_package_version():
    assert _server()["args"] == ["--from", f"git+{REPOSITORY}@v{_package_version()}", PLUGIN_NAME]


@pytest.mark.parametrize("path", ["plugin.json", ".claude-plugin/plugin.json"])
def test_the_plugin_version_is_the_package_version(path: str):
    assert _json(path)["version"] == _package_version()


@pytest.mark.parametrize(
    "path", ["plugin.json", "mcp.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]
)
def test_no_manifest_holds_a_placeholder(path: str):
    assert "${" not in (ROOT / path).read_text()


@pytest.mark.parametrize("path", NOT_WANTED)
def test_the_files_the_layout_does_not_need_do_not_exist(path: str):
    assert not (ROOT / path).exists()


def test_the_skill_is_the_single_copy_under_skills():
    tracked = subprocess.run(["git", "ls-files", "*SKILL.md"], cwd=ROOT, capture_output=True, text=True, check=True)
    assert tracked.stdout.split() == ["skills/use-browser/SKILL.md"]
