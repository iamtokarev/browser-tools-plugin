"""The `browser-tools` command: serve over stdio (the default), or `doctor`."""

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from .facade import find_chrome
from .settings import (
    BOOLEAN_SETTINGS,
    SETTING_NAMES,
    LoadedSettings,
    Settings,
    flag_name,
    load_settings,
)

COMMANDS = ("serve", "doctor")

_HELP = {
    "headed": "Show the browser window.",
    "profile": "Name of the dedicated profile.",
    "throwaway_profile": "Always use a throwaway profile.",
    "data_dir": "Directory for profiles, downloads and artifacts. It also holds config.toml.",
    "chrome_path": "Absolute path of the Chrome to launch.",
    "groups": "Tool groups that are on, separated by commas: core, debugging.",
    "allow_sites": "Sites the browser may open, separated by commas. Empty means no limit.",
    "deny_sites": "Sites the browser may not open, separated by commas.",
    "allow_file_urls": "Allow file:// pages.",
    "download_dir": "Directory for downloads.",
    "artifact_dir": "Directory where page code may save screenshots.",
    "result_ceiling": "Most characters in one snapshot, run, console or network result.",
    "run_timeout": "Seconds before a run is stopped.",
    "capture_timeout": "Seconds before a snapshot or screenshot is stopped.",
    "quiet_wait": "End timed waits early when the page is quiet.",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="browser-tools",
        description=(
            "Browser tools for agents, over MCP. With no command, serve over stdio. "
            "Settings come from config.toml in the data directory, then BROWSER_TOOLS_* "
            "environment variables, then these flags."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        default="serve",
        help="serve: run the MCP server (default). doctor: show the settings and the Chrome found.",
    )
    for name in SETTING_NAMES:
        flag = flag_name(name)
        spellings = (flag, "--" + name) if flag != "--" + name else (flag,)
        if name in BOOLEAN_SETTINGS:
            parser.add_argument(
                *spellings,
                dest=name,
                action=argparse.BooleanOptionalAction,
                default=argparse.SUPPRESS,
                help=_HELP[name],
            )
        else:
            parser.add_argument(*spellings, dest=name, default=argparse.SUPPRESS, metavar="VALUE", help=_HELP[name])
    return parser


def parse_command(argv: Sequence[str]) -> tuple[str, dict[str, str | bool]]:
    """The command and the settings given as flags. Values stay raw, so that a bad one is
    reported as a setting problem and does not stop the server."""
    values = vars(_build_parser().parse_args(list(argv)))
    command = values.pop("command")
    return command, values


def _show(setting: str, settings: Settings) -> str:
    value = getattr(settings, setting)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, tuple):
        return ", ".join(value) or "(none)"
    if value is None:
        return "auto-detect"
    return str(value)


def run_doctor(
    loaded: LoadedSettings,
    *,
    chrome_finder: Callable[[str | None], str] = find_chrome,
    out: TextIO | None = None,
) -> int:
    """Print the resolved settings with their sources, and the Chrome a launch would use.

    Reads and creates nothing, and starts no browser. Returns 0 when all is well, else 1.
    """
    out = out or sys.stdout
    settings = loaded.settings
    rows = [(name, _show(name, settings), loaded.sources[name]) for name in SETTING_NAMES]
    name_width = max(len(name) for name, _, _ in rows)
    value_width = max(len(value) for _, value, _ in rows)

    def say(text: str = "") -> None:
        print(text, file=out)

    say("browser-tools doctor")
    say()
    say(f"Data directory: {settings.data_dir}")
    state = "found" if loaded.config_path.is_file() else "not found"
    say(f"Config file: {loaded.config_path} ({state})")
    say()
    say("Settings (name, value, source):")
    for name, value, source in rows:
        say(f"  {name.ljust(name_width)}  {value.ljust(value_width)}  {source}")
    say()
    healthy = True
    try:
        chrome = chrome_finder(settings.chrome_path)
    except RuntimeError as error:
        healthy = False
        say(f"Chrome: not found. {error}")
    else:
        origin = "from the chrome_path setting" if settings.chrome_path else "found automatically"
        say(f"Chrome: {chrome} ({origin})")
    if loaded.problems:
        healthy = False
        say()
        say("Problems:")
        for problem in loaded.problems:
            say(f"  - {problem.describe()}")
        say("The server starts with these problems, but every tool call returns an error until they are corrected.")
    say()
    say("OK." if healthy else "Not ready. Correct the items above.")
    return 0 if healthy else 1


def main(argv: Sequence[str] | None = None) -> None:
    """Read the settings once, then serve or run `doctor`."""
    command, flags = parse_command(sys.argv[1:] if argv is None else argv)
    loaded = load_settings(flags)
    if command == "doctor":
        sys.exit(run_doctor(loaded))
    from .server import create_server

    for problem in loaded.problems:
        print(f"browser-tools: {problem.describe()}", file=sys.stderr, flush=True)
    create_server(loaded.settings, problems=loaded.problems).run()
