"""Site entries and the decision about one URL, without a browser."""

import pytest

from browser_tools_plugin.sites import SiteLimits, parse_entry


@pytest.mark.parametrize(
    "entry",
    [
        "example.com",
        "EXAMPLE.com",
        "*.example.com",
        "example.com:8080",
        "*.example.com:8080",
        "127.0.0.1:3000",
        "localhost:3000",
        "[::1]:3000",
        "https://example.com",
        "http://127.0.0.1:3000",
        "wss://example.com:9000",
        "bücher.example",
    ],
)
def test_valid_entries_are_accepted(entry: str):
    parse_entry(entry)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("example.com/path", "path"),
        ("https://example.com/", "path"),
        ("example.com/", "path"),
        ("example.com?x=1", "path"),
        ("example.com#top", "path"),
        ("user@example.com", "user name"),
        ("", "empty"),
        ("*", "wildcard"),
        ("*example.com", "wildcard"),
        ("a.*.example.com", "wildcard"),
        ("*.", "host"),
        ("ftp://example.com", "scheme"),
        ("file:///tmp", "scheme"),
        ("example.com:", "port"),
        ("example.com:0", "port"),
        ("example.com:99999", "port"),
        ("example.com:http", "port"),
        ("exa mple.com", "host"),
        ("*.127.0.0.1", "wildcard"),
        ("http://", "host"),
    ],
)
def test_invalid_entries_are_rejected_with_a_reason(entry: str, reason: str):
    with pytest.raises(ValueError, match=reason):
        parse_entry(entry)


def limits(allow: tuple[str, ...] = (), deny: tuple[str, ...] = (), files: bool = False) -> SiteLimits:
    return SiteLimits.from_entries(allow, deny, allow_file_urls=files)


def allowed(site_limits: SiteLimits, url: str) -> bool:
    return site_limits.check(url) is None


def test_no_lists_means_no_limit_but_file_urls_are_refused():
    none = limits()
    assert allowed(none, "https://example.com/a")
    assert allowed(none, "http://127.0.0.1:1/")
    assert allowed(none, "about:blank")
    assert allowed(none, "data:text/html,hi")
    assert allowed(none, "chrome-extension://abc/blank.html")
    assert not allowed(none, "file:///tmp/x.txt")
    assert "allow_file_urls" in (none.check("file:///tmp/x.txt") or "")
    assert allowed(limits(files=True), "file:///tmp/x.txt")


def test_a_bare_host_matches_that_host_on_any_port_and_scheme():
    site = limits(allow=("example.com",))
    for url in ("http://example.com/", "https://example.com:8443/x", "ws://example.com/", "wss://EXAMPLE.com./"):
        assert allowed(site, url), url
    for url in ("https://www.example.com/", "https://example.org/", "https://example.com.evil.test/"):
        assert not allowed(site, url), url


def test_a_wildcard_matches_subdomains_and_not_the_apex():
    site = limits(allow=("*.example.com",))
    assert allowed(site, "https://www.example.com/")
    assert allowed(site, "https://a.b.example.com/")
    assert not allowed(site, "https://example.com/")
    assert not allowed(site, "https://badexample.com/")
    assert not allowed(site, "https://example.com.evil.test/")


def test_a_port_in_an_entry_pins_the_port_and_a_default_port_counts():
    site = limits(allow=("example.com:8080", "secure.example:443"))
    assert allowed(site, "http://example.com:8080/")
    assert not allowed(site, "http://example.com:8081/")
    assert not allowed(site, "http://example.com/")
    assert allowed(site, "https://secure.example/")
    assert not allowed(site, "http://secure.example/")


def test_an_entry_with_a_scheme_is_an_origin_so_a_missing_port_is_the_default_port():
    site = limits(allow=("https://example.com", "http://dev.example:3000"))
    assert allowed(site, "https://example.com/")
    assert allowed(site, "https://example.com:443/")
    assert not allowed(site, "https://example.com:8443/")
    assert not allowed(site, "http://example.com/")
    assert allowed(site, "http://dev.example:3000/x")
    assert not allowed(site, "https://dev.example:3000/x")


def test_deny_beats_allow():
    site = limits(allow=("*.example.com",), deny=("bad.example.com", "other.example.com:81"))
    assert allowed(site, "https://good.example.com/")
    assert not allowed(site, "https://bad.example.com/")
    assert "deny list" in (site.check("https://bad.example.com/") or "")
    assert allowed(site, "http://other.example.com/")
    assert not allowed(site, "http://other.example.com:81/")
    assert not allowed(limits(allow=("example.com",), deny=("example.com",)), "https://example.com/")


def test_a_deny_list_alone_refuses_only_what_it_names():
    site = limits(deny=("bad.example",))
    assert allowed(site, "https://good.example/")
    assert not allowed(site, "https://bad.example/")


def test_under_an_allow_list_everything_else_is_refused_except_localhost_and_127_0_0_1():
    site = limits(allow=("example.com",))
    assert "allow list" in (site.check("https://other.test/") or "")
    assert allowed(site, "http://localhost:3000/")
    assert allowed(site, "http://127.0.0.1:5000/")
    assert not allowed(site, "http://127.0.0.2:5000/")
    assert not allowed(site, "http://b.localhost:5000/")
    assert not allowed(site, "http://[::1]:5000/")
    denied = limits(allow=("example.com",), deny=("localhost:3000", "127.0.0.1"))
    assert not allowed(denied, "http://localhost:3000/")
    assert allowed(denied, "http://localhost:3001/")
    assert not allowed(denied, "http://127.0.0.1:5000/")


def test_an_allow_list_with_a_file_url_follows_allow_file_urls_alone():
    assert not allowed(limits(allow=("example.com",)), "file:///tmp/x")
    assert allowed(limits(allow=("example.com",), files=True), "file:///tmp/x")
    assert allowed(limits(deny=("example.com",), files=True), "file:///tmp/x")


def test_other_schemes_and_a_url_that_cannot_be_read_are_handled():
    site = limits(allow=("example.com",))
    for url in ("about:blank", "data:text/plain,x", "blob:http://other.test/uuid", "chrome://version/"):
        assert allowed(site, url), url
    assert not allowed(site, "http://[bad/")
    assert not allowed(site, "http:///nohost")


def test_a_user_name_in_the_url_does_not_change_the_host():
    site = limits(allow=("example.com",))
    assert not allowed(site, "https://example.com@evil.test/")
    assert allowed(site, "https://evil@example.com/")


def test_an_idn_entry_matches_the_punycode_host():
    site = limits(allow=("bücher.example",))
    assert allowed(site, "https://xn--bcher-kva.example/")


def test_the_limits_are_active_with_a_list_or_without_allow_file_urls():
    assert limits().active
    assert not limits(files=True).active
    assert limits(deny=("a.example",), files=True).active
    assert limits(allow=("a.example",), files=True).active


def test_the_refused_url_shown_to_the_agent_has_no_query_or_fragment():
    assert SiteLimits.describe_url("https://u:p@example.com:8443/a/b?token=FAKESECRET#frag") == (
        "https://example.com:8443/a/b"
    )
    assert SiteLimits.describe_url("file:///tmp/x.txt?x=1") == "file:///tmp/x.txt"


async def test_a_tab_where_the_enforcer_cannot_start_is_closed_not_left_open():
    from typing import Any

    from browser_tools_plugin.cdp import CdpError
    from browser_tools_plugin.limits import SiteEnforcer

    sent: list[str] = []

    class Stub:
        async def send(self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None) -> Any:
            sent.append(method)
            if method == "Fetch.enable":
                raise CdpError("no")

    enforcer = SiteEnforcer(Stub(), limits(allow=("example.com",)), lambda cause: None)  # ty: ignore[invalid-argument-type]
    (command,) = enforcer.on_attach("s1", {"type": "page"}, "t1")
    await command
    assert sent == ["Fetch.enable", "Target.closeTarget"]


class _Stub:
    def __init__(self, fail: str | None = None) -> None:
        self.sent: list[str] = []
        self._fail = fail

    async def send(self, method: str, params: object = None, *, session: str | None = None) -> object:
        from browser_tools_plugin.cdp import CdpError

        self.sent.append(method)
        if method == self._fail:
            raise CdpError("no")
        return {}


async def test_an_ordinary_worker_where_the_enforcer_cannot_start_is_closed_and_never_resumed():
    from browser_tools_plugin.limits import SiteEnforcer

    stub = _Stub(fail="Fetch.enable")
    lost: list[str] = []
    enforcer = SiteEnforcer(stub, limits(allow=("example.com",)), on_unsafe=lost.append)  # ty: ignore[invalid-argument-type]
    info = {"type": "service_worker", "url": "https://example.com/sw.js"}
    (command,) = enforcer.on_attach("s1", info, "t1")
    try:
        await command
    except Exception as error:  # the listener reports the failure; `Targets` then calls on_attach_failed
        await enforcer.on_attach_failed("s1", info, "t1", error)
    assert stub.sent == ["Fetch.enable", "Target.closeTarget"]
    assert lost == []


async def test_the_extension_service_worker_where_the_enforcer_cannot_start_makes_the_browser_lost():
    from browser_tools_plugin.limits import SiteEnforcer

    stub = _Stub(fail="Fetch.enable")
    lost: list[str] = []
    enforcer = SiteEnforcer(stub, limits(allow=("example.com",)), on_unsafe=lost.append)  # ty: ignore[invalid-argument-type]
    info = {"type": "service_worker", "url": "chrome-extension://abc/service-worker.js"}
    (command,) = enforcer.on_attach("s1", info, "t1")
    try:
        await command
    except Exception as error:
        await enforcer.on_attach_failed("s1", info, "t1", error)
    assert stub.sent == ["Fetch.enable"]  # not closed, not resumed
    assert len(lost) == 1 and "extension" in lost[0]


async def test_targets_never_resumes_a_worker_that_a_listener_could_not_prepare():
    from browser_tools_plugin.limits import SiteEnforcer
    from browser_tools_plugin.targets import Targets

    stub = _Stub(fail="Fetch.enable")
    enforcer = SiteEnforcer(stub, limits(allow=("example.com",)), on_unsafe=lambda cause: None)  # ty: ignore[invalid-argument-type]
    targets = Targets(stub)  # ty: ignore[invalid-argument-type]
    targets.add_listener(enforcer)
    for kind in ("service_worker", "shared_worker"):
        stub.sent.clear()
        info = {"type": kind, "targetId": "w1", "url": "https://example.com/w.js"}
        await targets._setup("s1", info, None, waiting=True)
        assert "Runtime.runIfWaitingForDebugger" not in stub.sent, kind
    stub2 = _Stub()
    targets = Targets(stub2)  # ty: ignore[invalid-argument-type]
    targets.add_listener(SiteEnforcer(stub2, limits(allow=("example.com",)), on_unsafe=lambda cause: None))  # ty: ignore[invalid-argument-type]
    await targets._setup(
        "s1", {"type": "shared_worker", "targetId": "w2", "url": "https://example.com/w.js"}, None, waiting=True
    )
    assert stub2.sent == ["Fetch.enable", "Runtime.runIfWaitingForDebugger"]  # resumed after Fetch.enable
