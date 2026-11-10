"""Outbound network policy: URL parsing, address classes, trust and resolution checks (VAIR A3)."""

from __future__ import annotations

import ipaddress

import pytest

from velune.resources.http.netpolicy import (
    Kind,
    NetworkPolicy,
    Origin,
    PolicyViolation,
    classify_ip,
    parse_origin,
    parse_url,
)

# ── Strict URL parsing ──────────────────────────────────────────────────────


def test_a_plain_url_is_parsed_into_canonical_parts():
    url = parse_url("HTTP://Example.COM:8080/a/b?x=1#frag")
    assert (url.scheme, url.host, url.port, url.path, url.query) == (
        "http",
        "example.com",
        8080,
        "/a/b",
        "x=1",
    )
    assert str(url.origin) == "http://example.com:8080"
    assert url.target == "/a/b?x=1"


def test_default_ports_and_empty_paths():
    assert parse_url("https://example.com").port == 443
    assert parse_url("http://example.com").port == 80
    assert parse_url("https://example.com").path == "/"
    assert str(parse_url("https://example.com:443/x").origin) == "https://example.com"


def test_trailing_dot_and_case_are_normalized():
    assert parse_url("https://API.Example.com./x").host == "api.example.com"


def test_internationalized_hosts_are_idna_encoded():
    assert parse_url("https://bücher.example/x").host == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "//example.com/path",
        "example.com/path",
        "http://",
        "http:///path",
        "http://user:pass@example.com/",  # embedded credentials
        "http://user@example.com/",
        "http://example.com\\@127.0.0.1/",  # backslash parser-differential trick
        "http://example.com/\x00",
        "http://exa mple.com/",
        "http://example.com/a b",
        "http://example.com:99999/",
        "http://example.com:0/",
        "http://example.com:abc/",
        "http://exa%6Dple.com/",  # percent-encoded host
        "http://.example.com/",
        "http://a..b.com/",
    ],
)
def test_malformed_or_dangerous_urls_are_rejected(raw):
    with pytest.raises(PolicyViolation):
        parse_url(raw)


@pytest.mark.parametrize(
    "host",
    [
        "127.1",  # shorthand
        "2130706433",  # decimal integer
        "0x7f000001",  # hex integer
        "0x7f.0.0.1",
        "0177.0.0.1",  # octal
        "0300.0250.0.1",
        "10.1",
        "0",
    ],
)
def test_unusual_ip_encodings_are_rejected_outright(host):
    with pytest.raises(PolicyViolation) as caught:
        parse_url(f"http://{host}/")
    assert "unusual notation" in caught.value.message


def test_real_hostnames_that_merely_look_hexadecimal_are_accepted():
    assert parse_url("http://be.ef/").host == "be.ef"
    assert parse_url("http://1e100.net/").host == "1e100.net"


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("http://[::1]:8080/", "::1"),
        ("http://[2001:DB8::1]/", "2001:db8::1"),
        ("http://[::ffff:127.0.0.1]/", "::ffff:7f00:1"),
    ],
)
def test_ipv6_literals_are_normalized(raw, host):
    parsed = parse_url(raw)
    assert parsed.host == host
    assert parsed.ip is not None


def test_ipv6_origins_round_trip_with_brackets():
    assert str(parse_origin("http://[::1]:5000")) == "http://[::1]:5000"


def test_a_url_is_rebuilt_from_its_parts_so_one_parser_decides_the_host():
    parsed = parse_url("http://EXAMPLE.com:80/x?y=1#z")
    assert str(parsed) == "http://example.com/x?y=1"


def test_overlong_urls_are_rejected():
    with pytest.raises(PolicyViolation):
        parse_url("http://example.com/" + "a" * 9000)


# ── Address classification ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "kind"),
    [
        ("127.0.0.1", Kind.LOOPBACK),
        ("127.5.6.7", Kind.LOOPBACK),
        ("::1", Kind.LOOPBACK),
        ("10.0.0.5", Kind.PRIVATE),
        ("172.16.0.1", Kind.PRIVATE),
        ("172.31.255.255", Kind.PRIVATE),
        ("192.168.1.1", Kind.PRIVATE),
        ("100.64.0.1", Kind.PRIVATE),  # shared address space
        ("fd12:3456::1", Kind.PRIVATE),  # unique local
        ("169.254.169.254", Kind.BLOCKED),  # cloud metadata
        ("169.254.170.2", Kind.BLOCKED),
        ("100.100.100.200", Kind.BLOCKED),  # Alibaba metadata, inside the shared range
        ("192.0.0.192", Kind.BLOCKED),  # Oracle metadata
        ("fd00:ec2::254", Kind.BLOCKED),  # AWS IPv6 metadata
        ("169.254.1.1", Kind.BLOCKED),  # link-local
        ("fe80::1", Kind.BLOCKED),
        ("0.0.0.0", Kind.BLOCKED),
        ("0.1.2.3", Kind.BLOCKED),
        ("::", Kind.BLOCKED),
        ("224.0.0.1", Kind.BLOCKED),  # multicast
        ("ff02::1", Kind.BLOCKED),
        ("255.255.255.255", Kind.BLOCKED),
        ("240.0.0.1", Kind.BLOCKED),
        ("8.8.8.8", Kind.PUBLIC),
        ("93.184.216.34", Kind.PUBLIC),
        ("2606:4700:4700::1111", Kind.PUBLIC),
    ],
)
def test_addresses_are_classified(address, kind):
    assert classify_ip(ipaddress.ip_address(address))[0] is kind


@pytest.mark.parametrize(
    ("address", "kind"),
    [
        ("::ffff:127.0.0.1", Kind.LOOPBACK),  # IPv4-mapped loopback
        ("::ffff:10.0.0.1", Kind.PRIVATE),
        ("::ffff:169.254.169.254", Kind.BLOCKED),  # mapped metadata
        ("64:ff9b::7f00:1", Kind.LOOPBACK),  # NAT64 of 127.0.0.1
        ("64:ff9b::a9fe:a9fe", Kind.BLOCKED),  # NAT64 of the metadata address
        ("2002:7f00:1::1", Kind.LOOPBACK),  # 6to4 embedding 127.0.0.1
        ("2002:a9fe:a9fe::1", Kind.BLOCKED),  # 6to4 embedding the metadata address
        ("::ffff:8.8.8.8", Kind.PUBLIC),
        ("::7f00:1", Kind.LOOPBACK),  # IPv4-compatible
    ],
)
def test_ipv6_transition_addresses_use_the_ipv4_address_they_embed(address, kind):
    assert classify_ip(ipaddress.ip_address(address))[0] is kind


# ── Classification of a URL (no DNS) ────────────────────────────────────────


def test_localhost_and_loopback_literals_are_loopback():
    policy = NetworkPolicy()
    for text in (
        "http://localhost:5000/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://api.localhost/",
    ):
        assert policy.classify(parse_url(text)).kind is Kind.LOOPBACK


def test_ordinary_names_are_public_until_resolution_says_otherwise():
    destination = NetworkPolicy().classify(parse_url("https://api.example.com/"))
    assert destination.kind is Kind.PUBLIC and not destination.blocked and not destination.trusted


@pytest.mark.parametrize(
    "text",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.170.2/",
        "http://100.100.100.200/",
        "http://[fd00:ec2::254]/",
        "http://metadata.google.internal/",
        "http://metadata.goog/",
        "http://0.0.0.0/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
def test_metadata_and_link_local_destinations_are_blocked_even_if_trusted(text):
    parsed = parse_url(text)
    policy = NetworkPolicy.from_origins([text], allow_private_network=True)
    assert policy.classify(parsed).blocked


def test_private_destinations_need_trust_or_an_explicit_opt_in():
    parsed = parse_url("http://192.168.1.20:3000/")
    assert NetworkPolicy().classify(parsed).blocked
    assert not NetworkPolicy.from_origins(["http://192.168.1.20:3000"]).classify(parsed).blocked
    assert not NetworkPolicy(allow_private_network=True).classify(parsed).blocked


def test_trust_is_an_exact_origin_match_not_a_prefix():
    policy = NetworkPolicy.from_origins(["http://192.168.1.20:3000"])
    assert policy.classify(parse_url("http://192.168.1.20:3001/")).blocked  # other port
    assert policy.classify(parse_url("https://192.168.1.20:3000/")).blocked  # other scheme
    assert policy.classify(parse_url("http://192.168.1.21:3000/")).blocked  # other host


def test_loopback_can_be_disabled_wholesale():
    policy = NetworkPolicy(allow_loopback=False)
    assert policy.classify(parse_url("http://localhost:5000/")).blocked
    trusted = NetworkPolicy.from_origins(["http://localhost:5000"], allow_loopback=False)
    assert not trusted.classify(parse_url("http://localhost:5000/")).blocked


@pytest.mark.parametrize("port", [2375, 2376, 5432, 6379, 3306, 27017, 9200, 10250])
def test_sensitive_local_service_ports_are_flagged_unless_trusted(port):
    parsed = parse_url(f"http://127.0.0.1:{port}/")
    assert NetworkPolicy().classify(parsed).sensitive
    trusted = NetworkPolicy.from_origins([f"http://127.0.0.1:{port}"])
    assert not trusted.classify(parsed).sensitive


def test_plain_http_to_a_public_host_is_noted():
    destination = NetworkPolicy().classify(parse_url("http://api.example.com/"))
    assert any("not encrypted" in n for n in destination.notes)
    assert not NetworkPolicy().classify(parse_url("https://api.example.com/")).notes


def test_unusable_trusted_origin_entries_grant_nothing():
    policy = NetworkPolicy.from_origins(["not a url", "ftp://x", "http://user:p@host/"])
    assert policy.trusted_origins == frozenset()


# ── Checking the addresses a name really resolved to ────────────────────────


def test_a_public_name_pointing_inward_is_refused_the_rebinding_shape():
    policy = NetworkPolicy()
    with pytest.raises(PolicyViolation) as caught:
        policy.check_resolved("api.evil.example", 443, ["127.0.0.1"])
    assert "resolves to a loopback address" in caught.value.message


def test_a_public_name_pointing_at_a_private_address_is_refused():
    with pytest.raises(PolicyViolation):
        NetworkPolicy().check_resolved("api.evil.example", 443, ["10.0.0.5"])


def test_metadata_addresses_are_refused_for_any_name_even_a_trusted_one():
    policy = NetworkPolicy.from_origins(["https://api.example.com"], allow_private_network=True)
    with pytest.raises(PolicyViolation):
        policy.check_resolved("api.example.com", 443, ["169.254.169.254"])


def test_one_bad_answer_in_a_multi_record_response_rejects_the_lot():
    with pytest.raises(PolicyViolation):
        NetworkPolicy().check_resolved("api.example.com", 443, ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(PolicyViolation):
        NetworkPolicy().check_resolved("api.example.com", 443, ["93.184.216.34", "169.254.169.254"])


def test_a_trusted_origin_may_resolve_to_its_own_loopback_or_private_address():
    policy = NetworkPolicy.from_origins(["http://api.dev.test:5000", "http://intranet.test:80"])
    assert policy.check_resolved("api.dev.test", 5000, ["127.0.0.1"]) == ["127.0.0.1"]
    assert policy.check_resolved("intranet.test", 80, ["10.1.2.3"]) == ["10.1.2.3"]


def test_trust_is_per_port_when_checking_resolution():
    policy = NetworkPolicy.from_origins(["http://api.dev.test:5000"])
    with pytest.raises(PolicyViolation):
        policy.check_resolved("api.dev.test", 6000, ["127.0.0.1"])


def test_localhost_names_may_reach_loopback_without_being_listed():
    policy = NetworkPolicy()
    assert policy.check_resolved("localhost", 5000, ["127.0.0.1", "::1"]) == ["127.0.0.1", "::1"]
    assert policy.check_resolved("127.0.0.1", 5000, ["127.0.0.1"]) == ["127.0.0.1"]


def test_public_answers_pass_and_an_empty_answer_is_a_dns_failure():
    assert NetworkPolicy().check_resolved("example.com", 443, ["93.184.216.34"]) == [
        "93.184.216.34"
    ]
    with pytest.raises(PolicyViolation) as caught:
        NetworkPolicy().check_resolved("example.com", 443, [])
    assert caught.value.code == "dns_failed"


async def test_resolve_uses_the_injected_resolver_and_validates_the_answer():
    calls = []

    async def resolver(host, port):
        calls.append((host, port))
        return ["93.184.216.34"]

    assert await NetworkPolicy().resolve("example.com", 443, resolver) == ["93.184.216.34"]
    assert calls == [("example.com", 443)]


async def test_resolve_does_not_resolve_ip_literals():
    async def resolver(host, port):  # pragma: no cover - must not be called
        raise AssertionError("an IP literal must not be resolved")

    assert await NetworkPolicy().resolve("127.0.0.1", 80, resolver) == ["127.0.0.1"]


async def test_resolution_failures_and_timeouts_are_dns_failures():
    async def failing(host, port):
        raise OSError("no such host")

    async def slow(host, port):
        import asyncio

        await asyncio.sleep(10)

    with pytest.raises(PolicyViolation) as failed:
        await NetworkPolicy().resolve("nope.example", 80, failing)
    assert failed.value.code == "dns_failed"
    with pytest.raises(PolicyViolation) as timed_out:
        await NetworkPolicy(resolve_timeout=0.05).resolve("slow.example", 80, slow)
    assert timed_out.value.code == "dns_failed"


async def test_blocked_hostnames_are_refused_before_any_lookup():
    async def resolver(host, port):  # pragma: no cover
        raise AssertionError("must not resolve a blocked host")

    with pytest.raises(PolicyViolation):
        await NetworkPolicy().resolve("metadata.google.internal", 80, resolver)


def test_origin_string_forms():
    assert str(Origin("https", "example.com", 443)) == "https://example.com"
    assert str(Origin("http", "example.com", 8080)) == "http://example.com:8080"
