"""The plugin manifests: one metadata set in two files, one server definition, one version. No test starts Chrome."""

import importlib.util
import json
import re
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


# --- The licence ---------------------------------------------------------------------------


def test_the_licence_is_mit_in_every_place_that_can_hold_one():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    licences = {
        "pyproject.toml": pyproject["license"],
        "plugin.json": _json("plugin.json")["license"],
        ".claude-plugin/plugin.json": _json(".claude-plugin/plugin.json")["license"],
    }
    assert set(licences.values()) == {"MIT"}, licences


def test_the_licence_file_is_the_mit_licence_of_the_author():
    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Vasilii Tokarev" in text


# --- The README ----------------------------------------------------------------------------


def _readme() -> str:
    return (ROOT / "README.md").read_text()


def test_the_readme_names_every_setting_in_all_three_spellings():
    from browser_tools_plugin.settings import SETTING_NAMES, env_name, flag_name

    readme = _readme()
    missing = [
        spelling
        for name in SETTING_NAMES
        for spelling in (f"`{name}`", f"`{env_name(name)}`", f"`{flag_name(name)}`")
        if spelling not in readme
    ]
    assert not missing, f"the README does not show: {missing}"


def test_the_readme_install_commands_use_the_tag_of_the_package_version():
    tag = f"v{_package_version()}"
    assert f"git+{REPOSITORY}@{tag} browser-tools doctor" in _readme()
    stale = {t for t in re.findall(r"@(v\d+\.\d+\.\d+)", _readme())} - {tag}
    assert not stale, f"the README shows an old tag: {stale}"


def test_the_readme_installs_the_plugin_from_the_marketplace():
    assert "/plugin marketplace add iamtokarev/browser-tools-plugin" in _readme()
    assert "/plugin install browser-tools@browser-tools-plugin" in _readme()


@pytest.mark.parametrize("heading", ["## Limits", "## Install", "## Settings", "## Release"])
def test_the_readme_has_the_section(heading: str):
    assert heading in _readme().splitlines()


# --- The release step ----------------------------------------------------------------------

RELEASE_FILES = ("pyproject.toml", "plugin.json", ".claude-plugin/plugin.json", "mcp.json", "README.md", "uv.lock")


def _bump_module():
    spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def copy_of_release_files(tmp_path: Path) -> Path:
    for relative in RELEASE_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text())
    return tmp_path


def test_the_release_step_moves_every_version_together(copy_of_release_files: Path):
    root = copy_of_release_files
    old = _package_version()
    _bump_module().bump(root, "9.8.7")
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == "9.8.7"
    for path in ("plugin.json", ".claude-plugin/plugin.json"):
        assert json.loads((root / path).read_text())["version"] == "9.8.7"
    assert json.loads((root / "mcp.json").read_text())["mcpServers"][PLUGIN_NAME]["args"][1].endswith("@v9.8.7")
    assert "@v9.8.7 browser-tools doctor" in (root / "README.md").read_text()
    lock = (root / "uv.lock").read_text()
    assert 'name = "browser-tools-plugin"\nversion = "9.8.7"' in lock
    for relative in RELEASE_FILES:
        assert f"@v{old}" not in (root / relative).read_text()


def test_the_release_step_changes_nothing_else(copy_of_release_files: Path):
    root = copy_of_release_files
    old = _package_version()
    _bump_module().bump(root, "9.8.7")
    for relative in RELEASE_FILES:
        before = (ROOT / relative).read_text().replace(old, "9.8.7")
        assert (root / relative).read_text() == before, relative


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0-rc1", "", "one"])
def test_the_release_step_refuses_a_version_that_is_not_x_y_z(copy_of_release_files: Path, bad: str):
    with pytest.raises(ValueError, match=r"X\.Y\.Z"):
        _bump_module().bump(copy_of_release_files, bad)


def test_the_release_step_refuses_the_current_version(copy_of_release_files: Path):
    with pytest.raises(ValueError, match="already"):
        _bump_module().bump(copy_of_release_files, _package_version())


def test_the_release_step_stops_before_it_writes_when_a_file_has_no_version(copy_of_release_files: Path):
    root = copy_of_release_files
    (root / "mcp.json").write_text('{"mcpServers": {}}')
    with pytest.raises(ValueError, match=r"mcp\.json"):
        _bump_module().bump(root, "9.8.7")
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == _package_version()
