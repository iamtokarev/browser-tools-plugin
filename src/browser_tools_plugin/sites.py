"""Site limits: the entries of `allow_sites` and `deny_sites`, and the decision about one URL.

This module has no browser in it. The enforcer in `limits.py` asks it about each request. The
entries are `example.com` (that host, any port), `*.example.com` (subdomains, not the apex),
`host:port`, and `scheme://host:port`. An entry never has a path. See the spec, section 4.2.
"""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# The schemes that the limits look at. Others (about:, data:, blob:, chrome-extension:) pass.
WEB_SCHEMES = ("http", "https", "ws", "wss")
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
# Under an allow list these two hosts stay reachable, unless the deny list names them.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})

_HOST = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")


@dataclass(frozen=True, slots=True)
class SiteEntry:
    host: str  # lower case, without the `*.` and without brackets
    wildcard: bool = False
    scheme: str | None = None
    port: int | None = None  # None: any port, unless there is a scheme

    def matches(self, scheme: str, host: str, port: int | None) -> bool:
        if self.wildcard:
            if not host.endswith("." + self.host):
                return False
        elif host != self.host:
            return False
        if self.scheme is not None and self.scheme != scheme:
            return False
        wanted = self.port
        if wanted is None and self.scheme is not None:
            wanted = DEFAULT_PORTS[self.scheme]  # `https://example.com` is an origin
        return wanted is None or wanted == (port if port is not None else DEFAULT_PORTS[scheme])


def _normal_host(host: str) -> str | None:
    """The host in lower case, without a trailing dot, with an IDN in punycode; None if it is not a host."""
    host = host.lower().rstrip(".")
    if not host:
        return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    return host if _HOST.fullmatch(host) else None


def parse_entry(text: str) -> SiteEntry:
    """Read one entry. Raises ValueError, with a reason that names the entry, when it is not valid."""
    entry = text.strip()

    def invalid(reason: str) -> ValueError:
        return ValueError(f"{entry!r} is not a valid site: {reason}")

    if not entry:
        raise ValueError("an entry is empty")
    scheme = None
    rest = entry
    if "://" in entry:
        scheme, _, rest = entry.partition("://")
        scheme = scheme.lower()
        if scheme not in WEB_SCHEMES:
            raise invalid("the scheme must be http, https, ws or wss")
    if any(char in rest for char in "/?#"):
        raise invalid("an entry has no path, query or fragment; use only the host, port and scheme")
    if "@" in rest:
        raise invalid("an entry has no user name")
    port_text: str | None = None
    if rest.startswith("["):
        end = rest.find("]")
        if end == -1:
            raise invalid("the host is not valid")
        host_text, after = rest[1:end], rest[end + 1 :]
        try:
            host = str(ipaddress.IPv6Address(host_text))
        except ValueError:
            raise invalid("the host is not a valid IPv6 address") from None
        if after:
            if not after.startswith(":"):
                raise invalid("the host is not valid")
            port_text = after[1:]
        wildcard = False
    else:
        if rest.count(":") > 1:
            raise invalid("an IPv6 address needs square brackets")
        host_text, colon, port_part = rest.partition(":")
        if colon:
            port_text = port_part
        wildcard = host_text.startswith("*.")
        if wildcard:
            host_text = host_text[2:]
        if "*" in host_text:
            raise invalid("a wildcard is only allowed as the first label, as in *.example.com")
        normal = _normal_host(host_text)
        if normal is None:
            raise invalid("the host is not valid")
        host = normal
        if wildcard and re.fullmatch(r"[0-9.]+", host):
            raise invalid("a wildcard cannot be used with an IP address")
    port: int | None = None
    if port_text is not None:
        if not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise invalid("the port must be a number from 1 to 65535")
        port = int(port_text)
    return SiteEntry(host, wildcard, scheme, port)


@dataclass(frozen=True, slots=True)
class SiteLimits:
    """The allow list, the deny list and the file switch, ready to decide about a URL."""

    allow: tuple[SiteEntry, ...] = ()
    deny: tuple[SiteEntry, ...] = ()
    allow_file_urls: bool = False

    @classmethod
    def from_entries(cls, allow: tuple[str, ...], deny: tuple[str, ...], *, allow_file_urls: bool) -> "SiteLimits":
        return cls(tuple(map(parse_entry, allow)), tuple(map(parse_entry, deny)), allow_file_urls)

    @property
    def active(self) -> bool:
        """Whether any request can be refused. Without a list, `file://` is refused unless it is allowed."""
        return bool(self.allow or self.deny) or not self.allow_file_urls

    def check(self, url: str) -> str | None:
        """None when the URL may load; otherwise the reason it is refused, as a short phrase."""
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme == "file":
                return None if self.allow_file_urls else "file:// pages are off, because allow_file_urls is false"
            if scheme not in WEB_SCHEMES:
                return None
            host = _normal_host(parts.hostname or "") if parts.hostname else None
            if host is None and parts.hostname and ":" in parts.hostname:
                host = str(ipaddress.IPv6Address(parts.hostname))  # `_normal_host` does not know IPv6
            port = parts.port
        except ValueError:
            return "the URL cannot be read"
        if host is None:
            return "the URL has no valid host"
        if any(entry.matches(scheme, host, port) for entry in self.deny):
            return "its site is on the deny list"
        if not self.allow:
            return None
        if host in LOOPBACK_HOSTS or any(entry.matches(scheme, host, port) for entry in self.allow):
            return None
        return "its site is not on the allow list"

    @staticmethod
    def describe_url(url: str) -> str:
        """The URL as the agent may read it: no user name, query or fragment, which can hold secrets."""
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            port = f":{parts.port}" if parts.port is not None else ""
        except ValueError:
            return "an unreadable URL"
        return f"{parts.scheme}://{host}{port}{parts.path}"
