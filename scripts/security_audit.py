"""Security audit script for Velune CLI.

Checks six security properties of the codebase. Exits 0 if all pass.
Run from the repository root: python scripts/security_audit.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
VELUNE = ROOT / "velune"

PASS = "[PASS]"
FAIL = "[FAIL]"

_failures: list[str] = []


def _fail(msg: str) -> None:
    _failures.append(msg)
    print(f"{FAIL} {msg}")


def _pass(msg: str) -> None:
    print(f"{PASS} {msg}")


# ---------------------------------------------------------------------------
# CHECK 1 — No shell=True in subprocess calls
# ---------------------------------------------------------------------------
def check_no_shell_true() -> None:
    pattern = re.compile(r"\bshell\s*=\s*True")
    comment = re.compile(r"^\s*#")
    hits: list[str] = []
    for py in VELUNE.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if comment.match(line):
                continue
            # Strip inline comment before checking (handles: shell=False  # NEVER shell=True)
            code_part = line.split("#")[0]
            if pattern.search(code_part):
                hits.append(f"{py.relative_to(ROOT)}:{i}")
    if hits:
        _fail(f"shell=True found in non-comment lines: {hits}")
    else:
        _pass("No shell=True in subprocess calls.")


# ---------------------------------------------------------------------------
# CHECK 2 — No bare os.getenv for provider API keys
# ---------------------------------------------------------------------------
_KEY_ENVVARS = re.compile(
    r'os\.getenv\(["\']('
    r"ANTHROPIC_API_KEY|OPENAI_API_KEY|XAI_API_KEY|GOOGLE_API_KEY"
    r"|GROQ_API_KEY|OPENROUTER_API_KEY|HF_TOKEN|HUGGINGFACE_API_KEY"
    r")[\"\']\)"
)

_ALLOWED_GETENV_PATHS = {
    "velune/providers/keystore.py",  # keystore itself maps env vars
}


def check_no_bare_getenv_for_keys() -> None:
    hits: list[str] = []
    for py in VELUNE.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if rel in _ALLOWED_GETENV_PATHS:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*#", line):
                continue
            if _KEY_ENVVARS.search(line):
                hits.append(f"{rel}:{i}")
    if hits:
        _fail(f"Bare os.getenv for API key env vars (use keystore.get_key instead): {hits}")
    else:
        _pass("No bare os.getenv for provider API keys.")


# ---------------------------------------------------------------------------
# CHECK 3 — SSRF validator blocks private IP ranges
# ---------------------------------------------------------------------------
def check_ssrf_validator() -> None:
    validator = VELUNE / "tools" / "web" / "validator.py"
    if not validator.exists():
        _fail(f"SSRF validator missing: {validator.relative_to(ROOT)}")
        return
    src = validator.read_text(encoding="utf-8")
    required = [
        "169.254.",   # link-local / IMDS
        "private",    # private IP check
        "loopback",   # loopback check
    ]
    missing = [r for r in required if r not in src]
    if missing:
        _fail(f"SSRF validator missing checks for: {missing}")
    else:
        _pass("SSRF validator covers private/loopback/link-local/IMDS.")


# ---------------------------------------------------------------------------
# CHECK 4 — DEFAULT_VELUNEIGNORE covers common secret patterns
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    ".env",
    "*.pem",
    "*.key",
    "id_rsa",
    "*.crt",
    ".netrc",
    ".aws",
    "credentials.json",
]


def check_veluneignore_covers_secrets() -> None:
    scanner = VELUNE / "repository" / "scanner.py"
    if not scanner.exists():
        _fail(f"scanner.py not found: {scanner.relative_to(ROOT)}")
        return
    src = scanner.read_text(encoding="utf-8")
    missing = [p for p in _SECRET_PATTERNS if p not in src]
    if missing:
        _fail(f"DEFAULT_VELUNEIGNORE missing patterns: {missing}")
    else:
        _pass("DEFAULT_VELUNEIGNORE covers all required secret patterns.")


# ---------------------------------------------------------------------------
# CHECK 5 — No hardcoded credentials in source
# ---------------------------------------------------------------------------
_HARDCODED = re.compile(
    r'["\']('
    r"sk-[A-Za-z0-9]{20,}"        # OpenAI key
    r"|gsk_[A-Za-z0-9]{20,}"      # Groq key
    r"|sk-ant-[A-Za-z0-9\-]{20,}" # Anthropic key
    r"|AIza[A-Za-z0-9\-_]{20,}"   # Google key
    r"|xai-[A-Za-z0-9]{20,}"      # xAI key
    r")[\"']"
)

_ALLOWED_HARDCODED_PATHS: set[str] = {
    "scripts/security_audit.py",  # this file — pattern strings are not real keys
    "tests/test_security.py",
}


def check_no_hardcoded_credentials() -> None:
    hits: list[str] = []
    for py in ROOT.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if rel in _ALLOWED_HARDCODED_PATHS:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*#", line):
                continue
            if _HARDCODED.search(line):
                hits.append(f"{rel}:{i}")
    if hits:
        _fail(f"Possible hardcoded credentials found: {hits}")
    else:
        _pass("No hardcoded credentials detected.")


# ---------------------------------------------------------------------------
# CHECK 6 — MCP server has rate limiting
# ---------------------------------------------------------------------------
def check_mcp_rate_limiter() -> None:
    server = VELUNE / "mcp" / "server.py"
    if not server.exists():
        _fail(f"MCP server not found: {server.relative_to(ROOT)}")
        return
    src = server.read_text(encoding="utf-8")
    checks = {
        "RateLimiter": "RateLimiter class",
        "DEFAULT_HOST": "DEFAULT_HOST constant",
        "MAX_REQUEST_BYTES": "MAX_REQUEST_BYTES constant",
    }
    missing = [label for symbol, label in checks.items() if symbol not in src]
    if missing:
        _fail(f"MCP server missing security hardening: {missing}")
    else:
        _pass("MCP server has RateLimiter, DEFAULT_HOST, and MAX_REQUEST_BYTES.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 60)
    print("Velune Security Audit")
    print("=" * 60)

    check_no_shell_true()
    check_no_bare_getenv_for_keys()
    check_ssrf_validator()
    check_veluneignore_covers_secrets()
    check_no_hardcoded_credentials()
    check_mcp_rate_limiter()

    print("=" * 60)
    if _failures:
        print(f"FAILED: {len(_failures)} issue(s) found.")
        sys.exit(1)
    else:
        print(f"PASSED: All 6 checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
