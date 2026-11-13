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
    """The fixture files, and a few dynamic routes under /api/ for the network tests.

    Every secret in them is an obviously fake value.
    """

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _api(self, body: bytes = b"") -> bool:
        path = self.path.split("?", 1)[0]
        json_type = "application/json"
        if path == "/api/items":
            self._send(200, b'{"items":[1,2,3]}', json_type)
        elif path == "/api/missing":
            self._send(404, b'{"error":"no such item"}', json_type)
        elif path == "/api/echo":
            self._send(200, b'{"echo":' + (body or b"null") + b"}", json_type)
        elif path == "/api/secret":
            text = b'{"token":"sk-FAKEKEY123456789","note":"Bearer FAKEBEARER123456789"}'
            self._send(200, text, json_type, {"Set-Cookie": "session=FAKESESSIONVALUE; Path=/"})
        elif path == "/api/medium":
            self._send(200, b"m" * 20000, "text/plain")
        elif path == "/api/big":
            self._send(200, b"b" * 300000, "text/plain")
        elif path == "/api/bin":
            self._send(200, bytes(range(256)) * 8, "application/octet-stream")
        elif path == "/api/style.css":
            self._send(200, b"body { margin: 0; }", "text/css")
        elif path == "/api/redirect":
            self._send(302, b"", "text/plain", {"Location": "/api/items"})
        else:
            return False
        return True

    def do_GET(self) -> None:
        if not self._api():
            super().do_GET()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not self._api(body):
            self.send_error(404)


class _FixtureServer(http.server.ThreadingHTTPServer):
    request_queue_size = 128  # the default of 5 refuses a burst of parallel requests


@pytest.fixture(scope="session")
def fixture_url() -> Iterator[str]:
    """Serve tests/fixtures on 127.0.0.1 and yield the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    server = _FixtureServer(("127.0.0.1", 0), handler)
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
