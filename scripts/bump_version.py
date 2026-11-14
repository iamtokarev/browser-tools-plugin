"""Release step: move the plugin version in every file that holds it, in one command.

    uv run python scripts/bump_version.py 0.2.0

It sets the new version in `pyproject.toml`, both `plugin.json` files, the git tag in
`mcp.json`, the tag in the README install commands, and the package entry in `uv.lock`.
It stops before it writes anything if one of the files has no version to change.
It does not run git. Commit the change, then tag it `vX.Y.Z` and push the tag.
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"\d+\.\d+\.\d+")
PACKAGE = "browser-tools-plugin"

# Each file: a pattern with three groups (before, version, after). Only the middle group changes.
_JSON_VERSION = r'(?m)^(  "version": ")([^"]+)(",?)$'
_TAG = r"(git\+https://github\.com/[\w.-]+/" + PACKAGE + r"@v)(\d+\.\d+\.\d+)()"
PATTERNS: dict[str, str] = {
    "pyproject.toml": r'(?m)^(version = ")([^"]+)(")$',
    "plugin.json": _JSON_VERSION,
    ".claude-plugin/plugin.json": _JSON_VERSION,
    "mcp.json": _TAG,
    "README.md": _TAG,
    "uv.lock": r'(name = "' + PACKAGE + r'"\nversion = ")([^"]+)(")',
}


def current_version(root: Path = ROOT) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def bump(root: Path, new_version: str) -> list[str]:
    """Set `new_version` everywhere. Return the files changed. Raise ValueError and write nothing on a problem."""
    if not VERSION.fullmatch(new_version):
        raise ValueError(f"The version must be of the form X.Y.Z, for example 0.2.0. Got: {new_version!r}.")
    if new_version == current_version(root):
        raise ValueError(f"The version is already {new_version}.")
    updated: dict[Path, str] = {}
    for relative, pattern in PATTERNS.items():
        path = root / relative
        text = path.read_text()
        changed, count = re.subn(pattern, lambda match: f"{match[1]}{new_version}{match[3]}", text)
        if count == 0:
            raise ValueError(f"{relative} has no version to change. Nothing was written.")
        updated[path] = changed
    for path, text in updated.items():
        path.write_text(text)
    return list(PATTERNS)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        files = bump(ROOT, argv[0])
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Version set to {argv[0]} in: {', '.join(files)}")
    print(f"Next: review the diff, commit it, then tag it v{argv[0]} and push the tag.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
