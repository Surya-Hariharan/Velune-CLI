import ipaddress
import re
from urllib.parse import urlparse
from typing import Optional

ALLOWED_SCHEMES = {"https", "http"}
BLOCKED_LOCALHOST_PATTERNS = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|::1|0\.0\.0\.0)$",
    re.IGNORECASE
)

def validate_url(url: str, allow_http: bool = False) -> tuple[bool, Optional[str]]:
    """
    Validate URL for SSRF safety.
    Returns (is_valid, error_message).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"
    
    # Scheme check
    if not parsed.scheme or parsed.scheme not in ALLOWED_SCHEMES:
        return False, f"URL scheme '{parsed.scheme}' not allowed. Use https://"
    
    if parsed.scheme == "http" and not allow_http:
        return False, "HTTP not allowed. Use HTTPS."
    
    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"
    
    # Block localhost
    if BLOCKED_LOCALHOST_PATTERNS.match(hostname):
        return False, f"Requests to localhost/loopback are not allowed: {hostname}"
    
    # Block RFC-1918 private ranges and link-local
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, f"Requests to private/reserved IP ranges not allowed: {hostname}"
    except ValueError:
        pass  # Not an IP address, is a hostname - OK
    
    # Block common internal hostnames
    blocked_hosts = {"metadata.google.internal", "169.254.169.254"}
    if hostname.lower() in blocked_hosts:
        return False, f"Requests to cloud metadata endpoints not allowed: {hostname}"
    
    return True, None
