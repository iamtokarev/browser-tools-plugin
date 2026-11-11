"""Built-in default settings.

Loading settings from `config.toml`, environment variables and command-line flags
arrives with a later ticket; for now the server runs on these defaults.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "browser-tools"


def default_data_dir() -> Path:
    """`PLUGIN_DATA`, then `CLAUDE_PLUGIN_DATA`, then the platform user-data directory."""
    for variable in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(variable)
        if value:
            return Path(value)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_NAME


@dataclass(frozen=True, slots=True)
class Settings:
    headed: bool = False
    profile: str = "default"
    chrome_path: str | None = None
    data_dir: Path = field(default_factory=default_data_dir)
    capture_timeout: float = 30.0
    quiet_wait: bool = True

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profiles" / self.profile

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"
