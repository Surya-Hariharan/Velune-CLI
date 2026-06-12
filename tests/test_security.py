"""Security-property tests for Velune CLI."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
VELUNE = ROOT / "velune"

# ---------------------------------------------------------------------------
# Test 1 — No shell=True in subprocess calls
# ---------------------------------------------------------------------------


def test_no_shell_true_in_source():
    """No production code may pass shell=True to a subprocess call."""
    comment = re.compile(r"^\s*#")
    pattern = re.compile(r"\bshell\s*=\s*True")
    hits: list[str] = []
    for py in VELUNE.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if comment.match(line):
                continue
            # Strip inline comment before checking (e.g. shell=False  # NEVER shell=True)
            code_part = line.split("#")[0]
            if pattern.search(code_part):
                hits.append(f"{py.relative_to(ROOT)}:{i}")
    assert hits == [], f"shell=True found in non-comment lines: {hits}"


# ---------------------------------------------------------------------------
# Test 2 — Provider code uses keystore, not bare os.getenv
# ---------------------------------------------------------------------------

_KEY_ENVVARS = re.compile(
    r'os\.getenv\(["\']('
    r"ANTHROPIC_API_KEY|OPENAI_API_KEY|XAI_API_KEY|GOOGLE_API_KEY"
    r"|GROQ_API_KEY|OPENROUTER_API_KEY|HF_TOKEN|HUGGINGFACE_API_KEY"
    r")[\"\']\)"
)
_ALLOWED = {"velune/providers/keystore.py"}


def test_no_bare_getenv_for_api_keys():
    """Provider adapters and discovery modules must use get_key(), not os.getenv()."""
    hits: list[str] = []
    for py in VELUNE.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*#", line):
                continue
            if _KEY_ENVVARS.search(line):
                hits.append(f"{rel}:{i}")
    assert hits == [], f"Bare os.getenv for API keys found — use keystore.get_key: {hits}"


# ---------------------------------------------------------------------------
# Test 3 — SSRF validator blocks private IP ranges
# ---------------------------------------------------------------------------


def test_ssrf_blocks_private_ips():
    from velune.tools.web.validator import validate_url

    private_urls = [
        "https://192.168.1.1/admin",
        "https://10.0.0.1/data",
        "https://172.16.0.1/secret",
        "https://127.0.0.1/anything",
        "https://169.254.169.254/latest/meta-data/",
    ]
    for url in private_urls:
        is_valid, error = validate_url(url)
        assert not is_valid, f"Expected {url!r} to be blocked but validate_url returned valid"
        assert error, f"Expected an error message for blocked URL {url!r}"


# ---------------------------------------------------------------------------
# Test 4 — DEFAULT_VELUNEIGNORE covers critical secret file patterns
# ---------------------------------------------------------------------------

_REQUIRED_PATTERNS = [
    ".env",
    "*.pem",
    "*.key",
    "id_rsa",
    "*.crt",
    ".netrc",
    ".aws",
    "credentials.json",
]


def test_veluneignore_covers_secret_patterns():
    from velune.repository.scanner import DEFAULT_VELUNEIGNORE

    missing = [p for p in _REQUIRED_PATTERNS if p not in DEFAULT_VELUNEIGNORE]
    assert missing == [], f"DEFAULT_VELUNEIGNORE missing patterns: {missing}"


# ---------------------------------------------------------------------------
# Test 5 — MCP RateLimiter throttles excess requests
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_and_throttles():
    from velune.mcp.server import RateLimiter

    limiter = RateLimiter(calls_per_minute=60)

    # Bucket starts full — first call is always allowed without sleeping.
    assert limiter.is_allowed("client-a"), "Expected first call to be allowed"

    # Drain the bucket completely with rapid successive calls.
    for _ in range(200):
        limiter.is_allowed("client-a")

    # With an empty bucket and no refill time, the next call should be denied.
    assert not limiter.is_allowed("client-a"), "Expected rate limit to kick in on empty bucket"


def test_rate_limiter_separate_clients():
    from velune.mcp.server import RateLimiter

    limiter = RateLimiter(calls_per_minute=60)

    assert limiter.is_allowed("client-x"), "First call on client-x should be allowed"
    # Drain client-x
    for _ in range(200):
        limiter.is_allowed("client-x")

    # client-y gets its own independent full bucket — should be allowed immediately.
    assert limiter.is_allowed("client-y"), "Separate client buckets should be independent"
