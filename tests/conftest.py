import functools
import http.server
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from browser_tools_plugin.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
]


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
        if found := shutil.which(candidate):
            return found
    return None


needs_chrome = pytest.mark.skipif(find_chrome() is None, reason="Chrome is not installed")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="session")
def fixture_url() -> Iterator[str]:
    """Serve tests/fixtures on 127.0.0.1 and yield the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads the real settings sources: not the user's data directory, not `BROWSER_TOOLS_*`."""
    for name in list(os.environ):
        if name.startswith("BROWSER_TOOLS_"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Headless, with every directory under tmp_path: never the user's own profile."""
    return Settings(data_dir=tmp_path / "data", chrome_path=find_chrome())
