"""The fifteen settings, and how they are read.

Four sources, each overriding the one before: built-in defaults, `config.toml` in the data
directory, `BROWSER_TOOLS_*` environment variables, command-line flags. They are read once,
at server start. An invalid value never stops the server: it is kept as a problem, and the
server reports it in every tool call. See the spec, section 4.1.
"""

import math
import os
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "browser-tools"
CONFIG_FILE = "config.toml"
ENV_PREFIX = "BROWSER_TOOLS_"
GROUP_NAMES = ("core", "debugging")


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """`PLUGIN_DATA`, then `CLAUDE_PLUGIN_DATA`, then the platform user-data directory."""
    environ = os.environ if environ is None else environ
    for variable in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        value = environ.get(variable)
        if value:
            return Path(value)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(environ.get("APPDATA") or home / "AppData" / "Roaming") / APP_NAME
    return Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_NAME


_DERIVED = Path("<derived from data_dir>")


@dataclass(frozen=True, slots=True)
class Settings:
    headed: bool = False
    profile: str = "default"
    throwaway_profile: bool = False
    data_dir: Path = field(default_factory=default_data_dir)
    chrome_path: str | None = None
    groups: tuple[str, ...] = GROUP_NAMES
    allow_sites: tuple[str, ...] = ()
    deny_sites: tuple[str, ...] = ()
    allow_file_urls: bool = False
    download_dir: Path = _DERIVED
    artifact_dir: Path = _DERIVED
    result_ceiling: int = 60_000
    run_timeout: float = 45.0
    capture_timeout: float = 30.0
    quiet_wait: bool = True

    def __post_init__(self) -> None:
        # `download_dir` and `artifact_dir` default to a folder in `data_dir`.
        if self.download_dir is _DERIVED:
            object.__setattr__(self, "download_dir", self.data_dir / "downloads")
        if self.artifact_dir is _DERIVED:
            object.__setattr__(self, "artifact_dir", self.data_dir / "artifacts")

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profiles" / self.profile


# --- Parsing one value --------------------------------------------------------------------
#
# A parser takes the raw value and whether it came from text (an environment variable or a
# flag) and returns the typed value, or raises ValueError with a short reason. A TOML value
# must already have the right type: the string "true" is not a boolean in the file.

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _parse_bool(value: Any, from_text: bool) -> bool:
    if isinstance(value, bool):
        return value
    if from_text and isinstance(value, str):
        if value.strip().lower() in _TRUE:
            return True
        if value.strip().lower() in _FALSE:
            return False
    raise ValueError("must be true or false")


def _parse_number(value: Any, from_text: bool) -> float:
    if from_text and isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError("must be a number above 0") from None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ValueError("must be a number above 0")
    return float(value)


def _parse_count(value: Any, from_text: bool) -> int:
    if from_text and isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise ValueError("must be a whole number above 0") from None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("must be a whole number above 0")
    return value


def _parse_profile(value: Any, from_text: bool) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not isinstance(value, str) or not value or value in {".", ".."} or not set(value) <= allowed:
        raise ValueError("must be a name of letters, digits, dots, hyphens and underscores")
    return value


def _parse_path(value: Any, from_text: bool) -> Path:
    # A relative path would depend on the working directory, which differs between clients.
    if not isinstance(value, str) or not value:
        raise ValueError("must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("must be an absolute path")
    return path


def _parse_chrome_path(value: Any, from_text: bool) -> str:
    return str(_parse_path(value, from_text))


def _parse_list(value: Any, from_text: bool) -> tuple[str, ...]:
    if from_text and isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError("must be a list of text values")
    return tuple(item.strip() for item in value)


def _parse_groups(value: Any, from_text: bool) -> tuple[str, ...]:
    groups = _parse_list(value, from_text)
    unknown = [name for name in groups if name not in GROUP_NAMES]
    if unknown or not groups:
        raise ValueError(f"must list one or more of: {', '.join(GROUP_NAMES)}")
    return groups


@dataclass(frozen=True, slots=True)
class _Spec:
    name: str
    parse: Callable[[Any, bool], Any]
    in_file: bool = True

    @property
    def env_name(self) -> str:
        return ENV_PREFIX + self.name.upper()

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")


_SPECS = (
    _Spec("headed", _parse_bool),
    _Spec("profile", _parse_profile),
    _Spec("throwaway_profile", _parse_bool),
    _Spec("data_dir", _parse_path, in_file=False),
    _Spec("chrome_path", _parse_chrome_path),
    _Spec("groups", _parse_groups),
    _Spec("allow_sites", _parse_list),
    _Spec("deny_sites", _parse_list),
    _Spec("allow_file_urls", _parse_bool),
    _Spec("download_dir", _parse_path),
    _Spec("artifact_dir", _parse_path),
    _Spec("result_ceiling", _parse_count),
    _Spec("run_timeout", _parse_number),
    _Spec("capture_timeout", _parse_number),
    _Spec("quiet_wait", _parse_bool),
)
SETTING_NAMES = tuple(spec.name for spec in _SPECS)
BOOLEAN_SETTINGS = ("headed", "throwaway_profile", "allow_file_urls", "quiet_wait")
SPEC_BY_NAME = {spec.name: spec for spec in _SPECS}


def flag_name(setting: str) -> str:
    return SPEC_BY_NAME[setting].flag


def env_name(setting: str) -> str:
    return SPEC_BY_NAME[setting].env_name


# --- Loading ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingProblem:
    """One setting, or the file itself, that the server cannot use."""

    setting: str | None
    where: str  # for example "config.toml at /path", "environment variable X", "flag --x"
    reason: str

    def describe(self) -> str:
        if self.setting is None:
            return f"{self.where}: {self.reason}"
        return f"Setting {self.setting} ({self.where}): {self.reason}"


def problems_message(problems: Sequence[SettingProblem]) -> str:
    """The text of the one tool error every tool call returns while a setting is invalid."""
    lines = "\n".join(f"- {problem.describe()}" for problem in problems)
    return (
        f"The browser tools cannot work, because their settings are not valid:\n{lines}\n"
        "Correct the setting, then restart the server. Until then, no tool works."
    )


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    settings: Settings
    sources: Mapping[str, str]
    problems: tuple[SettingProblem, ...]
    config_path: Path

    def problem_message(self) -> str:
        return problems_message(self.problems)


def _read_config(path: Path, problems: list[SettingProblem]) -> dict[str, Any]:
    where = f"{CONFIG_FILE} at {path}"
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        problems.append(SettingProblem(None, f"Cannot read {where}", f"{error}. Correct the file."))
    except OSError as error:
        problems.append(SettingProblem(None, f"Cannot read {where}", f"{error.strerror or error}."))
    return {}


def load_settings(
    flags: Mapping[str, str | bool] | None = None, environ: Mapping[str, str] | None = None
) -> LoadedSettings:
    """Resolve every setting. `flags` maps a setting name to the raw value given on the command line.

    The working directory is never used: a relative path is a problem, not a guess.
    """
    flags = flags or {}
    environ = os.environ if environ is None else environ
    problems: list[SettingProblem] = []
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}

    # data_dir comes first: it says where config.toml is.
    data_spec = SPEC_BY_NAME["data_dir"]
    data_dir, data_source = default_data_dir(environ), "default"
    for variable in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        if environ.get(variable):
            data_source = f"environment {variable}"
            break
    candidates = [
        (
            environ.get(data_spec.env_name),
            f"environment {data_spec.env_name}",
            f"environment variable {data_spec.env_name}",
        ),
        (flags.get("data_dir"), f"flag {data_spec.flag}", f"flag {data_spec.flag}"),
    ]
    for raw, source, where in candidates:
        if raw is None or raw == "":
            continue
        try:
            data_dir, data_source = data_spec.parse(raw, True), source
        except ValueError as error:
            problems.append(SettingProblem("data_dir", where, str(error)))
    sources["data_dir"] = data_source
    config_path = data_dir / CONFIG_FILE

    file_values = _read_config(config_path, problems)
    file_where = f"{CONFIG_FILE} at {config_path}"
    for key in file_values:
        spec = SPEC_BY_NAME.get(key)
        if spec is None:
            problems.append(SettingProblem(key, file_where, "this is not a setting. Check the spelling."))
        elif not spec.in_file:
            problems.append(
                SettingProblem(
                    key,
                    file_where,
                    "this setting cannot be set in the file, because the file is inside the directory. "
                    f"Use {spec.env_name} or {spec.flag}.",
                )
            )

    for spec in _SPECS:
        if spec.name == "data_dir":
            continue
        sources[spec.name] = "default"
        layers: list[tuple[Any, bool, str, str]] = []
        if spec.in_file and spec.name in file_values:
            layers.append((file_values[spec.name], False, CONFIG_FILE, file_where))
        if environ.get(spec.env_name):
            layers.append(
                (environ[spec.env_name], True, f"environment {spec.env_name}", f"environment variable {spec.env_name}")
            )
        if spec.name in flags:
            layers.append((flags[spec.name], True, f"flag {spec.flag}", f"flag {spec.flag}"))
        for raw, from_text, source, where in layers:
            try:
                values[spec.name] = spec.parse(raw, from_text)
                sources[spec.name] = source
            except ValueError as error:
                problems.append(SettingProblem(spec.name, where, str(error)))

    settings = Settings(data_dir=data_dir, **values)
    return LoadedSettings(settings, sources, tuple(problems), config_path)
