"""Where settings come from, which source wins, and what an invalid value does."""

import os
from pathlib import Path

import pytest

from browser_tools_plugin.settings import (
    CONFIG_FILE,
    SETTING_NAMES,
    LoadedSettings,
    Settings,
    default_data_dir,
    load_settings,
)

EXPECTED_NAMES = [
    "headed",
    "profile",
    "throwaway_profile",
    "data_dir",
    "chrome_path",
    "groups",
    "allow_sites",
    "deny_sites",
    "allow_file_urls",
    "download_dir",
    "artifact_dir",
    "result_ceiling",
    "run_timeout",
    "capture_timeout",
    "quiet_wait",
]


def load(tmp_path: Path, *, config: str | None = None, env: dict[str, str] | None = None, flags=None) -> LoadedSettings:
    """Load with an explicit environment, so the real one never leaks in."""
    environment = {"PLUGIN_DATA": str(tmp_path), **(env or {})}
    if config is not None:
        (tmp_path / CONFIG_FILE).write_text(config)
    return load_settings(flags or {}, environment)


def test_there_are_fifteen_settings_with_the_spec_names():
    assert list(SETTING_NAMES) == EXPECTED_NAMES


def test_defaults_match_the_spec(tmp_path: Path):
    loaded = load(tmp_path)
    s = loaded.settings
    assert loaded.problems == ()
    assert (s.headed, s.profile, s.throwaway_profile, s.chrome_path) == (False, "default", False, None)
    assert s.data_dir == tmp_path
    assert s.groups == ("core", "debugging")
    assert (s.allow_sites, s.deny_sites, s.allow_file_urls) == ((), (), False)
    assert s.download_dir == tmp_path / "downloads"
    assert s.artifact_dir == tmp_path / "artifacts"
    assert (s.result_ceiling, s.run_timeout, s.capture_timeout, s.quiet_wait) == (60000, 45.0, 30.0, True)
    assert {name: source for name, source in loaded.sources.items() if name != "data_dir"} == dict.fromkeys(
        SETTING_NAMES[:3] + SETTING_NAMES[4:], "default"
    )
    assert loaded.sources["data_dir"] == "environment PLUGIN_DATA"


def test_the_bare_settings_class_has_the_same_defaults(tmp_path: Path):
    assert Settings(data_dir=tmp_path) == load(tmp_path).settings


def test_every_setting_is_read_from_the_file_but_data_dir(tmp_path: Path):
    config = f"""
headed = true
profile = "work"
throwaway_profile = true
chrome_path = "/opt/chrome"
groups = ["core"]
allow_sites = ["example.com"]
deny_sites = ["bad.example"]
allow_file_urls = true
download_dir = "{tmp_path / "dl"}"
artifact_dir = "{tmp_path / "art"}"
result_ceiling = 1000
run_timeout = 12.5
capture_timeout = 7
quiet_wait = false
"""
    loaded = load(tmp_path, config=config)
    s = loaded.settings
    assert loaded.problems == ()
    assert (s.headed, s.profile, s.throwaway_profile, s.chrome_path) == (True, "work", True, "/opt/chrome")
    assert s.groups == ("core",)
    assert (s.allow_sites, s.deny_sites, s.allow_file_urls) == (("example.com",), ("bad.example",), True)
    assert (s.download_dir, s.artifact_dir) == (tmp_path / "dl", tmp_path / "art")
    assert (s.result_ceiling, s.run_timeout, s.capture_timeout, s.quiet_wait) == (1000, 12.5, 7.0, False)
    assert loaded.sources["profile"] == CONFIG_FILE


def test_environment_overrides_the_file(tmp_path: Path):
    loaded = load(
        tmp_path,
        config='profile = "from-file"\nrun_timeout = 10\nheaded = true\n',
        env={"BROWSER_TOOLS_PROFILE": "from-env", "BROWSER_TOOLS_RUN_TIMEOUT": "20"},
    )
    assert loaded.settings.profile == "from-env"
    assert loaded.settings.run_timeout == 20.0
    assert loaded.settings.headed is True  # not in the environment: the file stands
    assert loaded.sources["profile"] == "environment BROWSER_TOOLS_PROFILE"
    assert loaded.sources["headed"] == CONFIG_FILE


def test_flags_override_the_environment(tmp_path: Path):
    loaded = load(
        tmp_path,
        config='profile = "from-file"\n',
        env={"BROWSER_TOOLS_PROFILE": "from-env", "BROWSER_TOOLS_HEADED": "true"},
        flags={"profile": "from-flag", "headed": False},
    )
    assert loaded.settings.profile == "from-flag"
    assert loaded.settings.headed is False
    assert loaded.sources["profile"] == "flag --profile"
    assert loaded.sources["headed"] == "flag --headed"


def test_environment_spellings(tmp_path: Path):
    env = {
        "BROWSER_TOOLS_THROWAWAY_PROFILE": "1",
        "BROWSER_TOOLS_ALLOW_FILE_URLS": "Yes",
        "BROWSER_TOOLS_QUIET_WAIT": "off",
        "BROWSER_TOOLS_GROUPS": "core",
        "BROWSER_TOOLS_ALLOW_SITES": "a.example, b.example",
        "BROWSER_TOOLS_RESULT_CEILING": "500",
        "BROWSER_TOOLS_CAPTURE_TIMEOUT": "2.5",
    }
    s = load(tmp_path, env=env).settings
    assert (s.throwaway_profile, s.allow_file_urls, s.quiet_wait) == (True, True, False)
    assert s.groups == ("core",)
    assert s.allow_sites == ("a.example", "b.example")
    assert (s.result_ceiling, s.capture_timeout) == (500, 2.5)


def test_an_empty_environment_value_counts_as_unset(tmp_path: Path):
    loaded = load(tmp_path, env={"BROWSER_TOOLS_PROFILE": ""})
    assert loaded.settings.profile == "default"
    assert loaded.problems == ()


def test_data_dir_order_is_flag_environment_plugin_data_claude_plugin_data_platform(tmp_path: Path):
    a, b, c, d = (tmp_path / n for n in "abcd")
    assert default_data_dir({"CLAUDE_PLUGIN_DATA": str(c)}) == c
    assert default_data_dir({"PLUGIN_DATA": str(b), "CLAUDE_PLUGIN_DATA": str(c)}) == b
    assert default_data_dir({}).name == "browser-tools"
    env = {"PLUGIN_DATA": str(b), "CLAUDE_PLUGIN_DATA": str(c)}
    assert load_settings({}, env).settings.data_dir == b
    assert load_settings({}, {**env, "BROWSER_TOOLS_DATA_DIR": str(d)}).settings.data_dir == d
    assert load_settings({"data_dir": str(a)}, {**env, "BROWSER_TOOLS_DATA_DIR": str(d)}).settings.data_dir == a
    loaded = load_settings({}, env)
    assert loaded.sources["data_dir"] == "environment PLUGIN_DATA"
    assert load_settings({}, {"CLAUDE_PLUGIN_DATA": str(c)}).sources["data_dir"] == "environment CLAUDE_PLUGIN_DATA"
    assert load_settings({}, {}).sources["data_dir"] == "default"


def test_the_file_is_read_from_the_data_dir_and_paths_follow_it(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    (other / CONFIG_FILE).write_text('profile = "elsewhere"\n')
    loaded = load_settings({"data_dir": str(other)}, {"PLUGIN_DATA": str(tmp_path)})
    assert loaded.settings.profile == "elsewhere"
    assert loaded.settings.download_dir == other / "downloads"
    assert loaded.config_path == other / CONFIG_FILE


def test_data_dir_in_the_file_is_a_problem_that_names_the_file(tmp_path: Path):
    loaded = load(tmp_path, config=f'data_dir = "{tmp_path / "x"}"\n')
    assert loaded.settings.data_dir == tmp_path
    (problem,) = loaded.problems
    assert problem.setting == "data_dir"
    assert str(tmp_path / CONFIG_FILE) in problem.describe()
    assert "BROWSER_TOOLS_DATA_DIR" in problem.describe()


@pytest.mark.parametrize(
    ("config", "setting"),
    [
        ('headed = "yes"', "headed"),
        ("run_timeout = 0", "run_timeout"),
        ("run_timeout = -3", "run_timeout"),
        ('run_timeout = "fast"', "run_timeout"),
        ("capture_timeout = true", "capture_timeout"),
        ("result_ceiling = 1.5", "result_ceiling"),
        ("result_ceiling = 0", "result_ceiling"),
        ('profile = "../escape"', "profile"),
        ('profile = ""', "profile"),
        ('groups = ["core", "nope"]', "groups"),
        ('groups = "core"', "groups"),
        ("allow_sites = [1]", "allow_sites"),
        ('allow_sites = ["example.com", "example.com/path"]', "allow_sites"),
        ('deny_sites = ["https://example.com/"]', "deny_sites"),
        ('deny_sites = ["ftp://example.com"]', "deny_sites"),
        ('allow_sites = ["*"]', "allow_sites"),
        ('download_dir = "relative/dir"', "download_dir"),
        ('chrome_path = "chrome"', "chrome_path"),
        ("colour = 1", "colour"),
    ],
)
def test_an_invalid_file_value_is_a_problem_naming_setting_and_file(tmp_path: Path, config: str, setting: str):
    loaded = load(tmp_path, config=config + "\n")
    (problem,) = loaded.problems
    assert problem.setting == setting
    text = problem.describe()
    assert setting in text
    assert str(tmp_path / CONFIG_FILE) in text
    # The bad value is dropped; the default stands.
    assert loaded.settings == Settings(data_dir=tmp_path)


def test_a_broken_file_is_one_problem_naming_the_file(tmp_path: Path):
    loaded = load(tmp_path, config="this is [not toml")
    (problem,) = loaded.problems
    assert str(tmp_path / CONFIG_FILE) in problem.describe()
    assert loaded.settings == Settings(data_dir=tmp_path)


def test_a_file_that_cannot_be_read_is_a_problem(tmp_path: Path):
    (tmp_path / CONFIG_FILE).mkdir()
    (problem,) = load_settings({}, {"PLUGIN_DATA": str(tmp_path)}).problems
    assert str(tmp_path / CONFIG_FILE) in problem.describe()


def test_an_invalid_environment_value_names_the_variable(tmp_path: Path):
    loaded = load(tmp_path, env={"BROWSER_TOOLS_RUN_TIMEOUT": "soon", "BROWSER_TOOLS_HEADED": "maybe"})
    texts = sorted(p.describe() for p in loaded.problems)
    assert len(texts) == 2
    assert "BROWSER_TOOLS_HEADED" in texts[0]
    assert "BROWSER_TOOLS_RUN_TIMEOUT" in texts[1]


def test_an_invalid_flag_value_names_the_flag_and_keeps_the_lower_source(tmp_path: Path):
    loaded = load(tmp_path, env={"BROWSER_TOOLS_RUN_TIMEOUT": "20"}, flags={"run_timeout": "-1"})
    (problem,) = loaded.problems
    assert "--run-timeout" in problem.describe()
    assert loaded.settings.run_timeout == 20.0


def test_a_relative_data_dir_is_a_problem_and_the_server_never_uses_the_working_directory(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    loaded = load_settings({"data_dir": "rel"}, {})
    (problem,) = loaded.problems
    assert problem.setting == "data_dir"
    assert loaded.settings.data_dir.is_absolute()
    assert loaded.settings.data_dir != tmp_path / "rel"


def test_a_relative_path_never_resolves_against_the_working_directory(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    loaded = load(tmp_path, env={"BROWSER_TOOLS_ARTIFACT_DIR": "art"})
    assert loaded.problems[0].setting == "artifact_dir"
    assert loaded.settings.artifact_dir == tmp_path / "artifacts"


def test_home_is_expanded_in_path_settings(tmp_path: Path):
    loaded = load(tmp_path, env={"BROWSER_TOOLS_DOWNLOAD_DIR": "~/somewhere"})
    assert loaded.problems == ()
    assert loaded.settings.download_dir == Path("~/somewhere").expanduser()


def test_the_problem_message_for_a_tool_error_names_every_problem(tmp_path: Path):
    loaded = load(tmp_path, config="run_timeout = 0\nprofile = 3\n")
    message = loaded.problem_message()
    assert "run_timeout" in message
    assert "profile" in message
    assert str(tmp_path / CONFIG_FILE) in message
    assert "restart" in message


def test_settings_are_frozen(tmp_path: Path):
    with pytest.raises(AttributeError):
        load(tmp_path).settings.headed = True  # ty: ignore[invalid-assignment]


def test_the_real_environment_does_not_leak_into_these_tests():
    assert not [k for k in os.environ if k.startswith("BROWSER_TOOLS_")]
    assert "PLUGIN_DATA" not in os.environ
    assert "CLAUDE_PLUGIN_DATA" not in os.environ


def test_a_path_in_a_site_entry_is_a_problem_that_says_what_is_wrong(tmp_path: Path):
    loaded = load(tmp_path, env={"BROWSER_TOOLS_ALLOW_SITES": "example.com, other.test/page"})
    (problem,) = loaded.problems
    assert problem.setting == "allow_sites"
    assert "BROWSER_TOOLS_ALLOW_SITES" in problem.describe()
    assert "'other.test/page'" in problem.describe()
    assert "no path" in problem.describe()
    assert loaded.settings.allow_sites == ()


def test_valid_site_entries_are_kept_as_written(tmp_path: Path):
    entries = '["example.com", "*.example.com", "localhost:3000", "https://example.com:8443"]'
    loaded = load(tmp_path, config=f"deny_sites = {entries}\n")
    assert loaded.problems == ()
    assert loaded.settings.deny_sites == ("example.com", "*.example.com", "localhost:3000", "https://example.com:8443")
