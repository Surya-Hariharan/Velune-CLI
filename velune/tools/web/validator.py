import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_HOSTS = frozenset({
    "169.254.169.254",   # AWS IMDS v1
    "169.254.170.2",     # AWS ECS metadata
    "metadata.google.internal",
    "metadata.goog",
    "169.254.0.0",       # link-local broadcast
})

BLOCKED_PREFIXES = (
    "169.254.",          # link-local range (catch-all)
)

def _is_private_ip(address: str) -> tuple[bool, str]:
    """Returns (is_blocked, reason). Resolves hostnames."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        # It's a hostname — resolve it
        try:
            socket.setdefaulttimeout(3)
            resolved = socket.getaddrinfo(address, None, proto=socket.IPPROTO_TCP)
            for family, _, _, _, sockaddr in resolved:
                ip_str = sockaddr[0]
                blocked, reason = _is_private_ip(ip_str)
                if blocked:
                    return True, f"hostname {address!r} resolves to blocked IP {ip_str}: {reason}"
        except socket.gaierror:
            return False, ""  # Can't resolve — let the request fail naturally
        return False, ""

    if ip.is_loopback:
        return True, "loopback address"
    if ip.is_private:
        return True, "private network range"
    if ip.is_link_local:
        return True, "link-local address (fe80::/10 or 169.254.x.x)"
    if ip.is_reserved:
        return True, "reserved address"
    if ip.is_multicast:
        return True, "multicast address"
    # Explicitly check for 0.0.0.0/8
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("0.0.0.0/8"):
        return True, "unspecified address range"
    return False, ""

def validate_url(url: str, allow_http: bool = False) -> tuple[bool, str | None]:
    """
    Validate URL for SSRF safety.
    Returns (is_valid, error_message).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Unparseable URL"

    # Reject credentials in URL
    if parsed.username or parsed.password:
        return False, "URLs with embedded credentials are not allowed"

    if parsed.scheme not in ("https", "http"):
        return False, f"Scheme '{parsed.scheme}' not allowed"
    if parsed.scheme == "http" and not allow_http:
        return False, "HTTP not allowed — use HTTPS"

    hostname = parsed.hostname
    if not hostname:
        return False, "No hostname"
    hostname = hostname.lower().strip(".")

    if hostname in BLOCKED_HOSTS:
        return False, f"Host '{hostname}' is explicitly blocked"
    for prefix in BLOCKED_PREFIXES:
        if hostname.startswith(prefix):
            return False, f"Host '{hostname}' matches blocked prefix"

    # Reject numeric escape forms (0x7f000001, 0177.0.0.1, decimal integer, etc.)
    # We use socket.inet_aton which handles hex, octal, decimal, integer, and multi-dot notations
    # exactly the way the OS/socket libraries parse numeric IPv4 addresses.
    try:
        packed = socket.inet_aton(hostname)
        ip_str = socket.inet_ntoa(packed)
        blocked, reason = _is_private_ip(ip_str)
        if blocked:
            return False, f"Numeric IP {hostname} resolves to blocked: {reason}"
    except OSError:
        pass

    blocked, reason = _is_private_ip(hostname)
    if blocked:
        return False, reason

    return True, None
