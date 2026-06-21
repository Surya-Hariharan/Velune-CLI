"""Command safety classification for Velune's ApprovalMode system.

Ported from Gemini CLI's commandSafety.ts — adapted for Python and Windows/POSIX.

Three tiers:
  SAFE   — known read-only commands; never require a prompt.
  ASK    — unknown commands; ask the user before executing.
  BLOCK  — matches a dangerous pattern; rejected outright.

Usage::

    from velune.tools.safety import ApprovalMode, classify_command

    verdict = classify_command("rm -rf /")
    if verdict.mode == ApprovalMode.BLOCK:
        raise PermissionError(verdict.reason)
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class ApprovalMode(enum.Enum):
    """Tool/command approval tier."""

    SAFE = "safe"  # Always allowed — no user prompt needed
    ASK = "ask"  # Prompt user before executing
    BLOCK = "block"  # Permanently rejected


@dataclass(slots=True, frozen=True)
class SafetyVerdict:
    mode: ApprovalMode
    reason: str


# ---------------------------------------------------------------------------
# Dangerous patterns — any match forces BLOCK
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # Privilege escalation
        r"\bsudo\b",
        r"\bsu\s+[-\w]",
        r"\bpkexec\b",
        r"\bdoas\b",
        # ptrace / process injection
        r"\bptrace\b",
        r"\bgdb\b.*attach",
        r"\bstrace\b.*-p\s*\d",
        # Filesystem destruction
        r"\brm\s+.*-[rRfF]*[rR][fF]",  # rm -rf (any ordering)
        r"\brm\s+.*-[rRfF]*[fF][rR]",
        r"rmdir\s+/[sS]",  # rmdir /s on Windows
        r"\bshred\b",
        r"\bwipe\b",
        r"\bsrm\b",
        # Disk operations
        r"\bdd\s+if=",
        r"\bmkfs\b",
        r"\bfdisk\b",
        r"\bparted\b",
        r"\bdiskpart\b",
        r"\bformat\s+[a-zA-Z]:",  # Windows format C:
        # Fork bomb
        r":\(\)\s*\{",
        # Remote code execution via pipe
        r"\bcurl\b[^|]*\|\s*(?:ba)?sh\b",
        r"\bwget\b[^|]*\|\s*(?:ba)?sh\b",
        r"\bfetch\b[^|]*\|\s*(?:ba)?sh\b",
        r"\bbase64\b[^|]*\|\s*(?:ba)?sh\b",
        # Encoded PowerShell payloads
        r"(?i)powershell\b.*-[eE]nc(?:odedCommand)?\b",
        r"(?i)powershell\b.*-[nN]o(?:P|Profile).*-[eE]xec",
        # Windows privilege/user management
        r"\bnet\s+(?:user|localgroup|accounts)\b",
        r"\bnetplwiz\b",
        # Windows registry writes
        r"\breg\s+(?:add|delete|import|export|copy|restore)\b",
        r"\bregedit\b",
        # Windows services
        r"\bsc\s+(?:create|delete|config|failure)\b",
        r"\bschtasks\s+/create\b",
        # Network manipulation
        r"\bnetsh\s+(?:firewall|advfirewall|wlan)\b",
        r"\biptables\s+-[AI]",
        r"\bpf(?:ctl)?\b.*block",
        # Credential theft
        r"\bsecretsdump\b",
        r"\bmimikatz\b",
        r"\bpwdump\b",
        r"\bhashdump\b",
    ]
]


# ---------------------------------------------------------------------------
# Safe command prefixes — exact match or starts-with (lowercased)
# ---------------------------------------------------------------------------

_SAFE_PREFIXES: tuple[str, ...] = (
    # Directory listing
    "ls",
    "dir",
    "ls -",
    "dir /",
    # Output
    "echo",
    "cat",
    "head",
    "tail",
    "more",
    "less",
    "type",
    # Path / info
    "pwd",
    "cd",
    "which",
    "where",
    "whoami",
    "hostname",
    "uname",
    "date",
    "uptime",
    "id",
    # System info (read-only)
    "df",
    "du ",
    "free",
    "top -",
    "htop",
    "ps ",
    "ps aux",
    "ps -",
    "env",
    "set",
    "printenv",
    # Git (read-only operations only)
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git show",
    "git remote",
    "git fetch",
    "git stash list",
    "git tag",
    "git rev-parse",
    "git shortlog",
    # Python tooling
    "python --version",
    "python3 --version",
    "py --version",
    "pip list",
    "pip show",
    "pip check",
    "uv pip list",
    "uv pip show",
    # Node
    "node --version",
    "npm --version",
    "yarn --version",
    "pnpm --version",
    "npx --version",
    # Search (read-only)
    "grep",
    "rg ",
    "find ",
    "fd ",
    # File stats
    "wc ",
    "stat ",
    "file ",
    # Windows safe equivalents
    "type ",
    "ver",
    "systeminfo",
    "ipconfig /all",
    "ipconfig /displaydns",
)


def _normalize(command: str) -> str:
    """Strip `.exe` from the leading word so Windows paths match cross-platform prefixes.

    e.g. ``python.exe --version`` → ``python --version``
    """
    idx = command.find(" ")
    first = command[:idx] if idx >= 0 else command
    rest = command[idx:] if idx >= 0 else ""
    if first.lower().endswith(".exe"):
        first = first[:-4]
    return first + rest


def classify_command(command: str) -> SafetyVerdict:
    """Classify *command* and return a SafetyVerdict.

    Priority order:
      1. Dangerous pattern match → BLOCK
      2. Known-safe prefix       → SAFE
      3. Everything else         → ASK
    """
    stripped = command.strip()
    # Keep the original for pattern matching; normalize only for prefix matching.
    normalized = _normalize(stripped)
    lower = normalized.lower()

    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(stripped):
            return SafetyVerdict(
                mode=ApprovalMode.BLOCK,
                reason=f"Matches dangerous pattern: {pattern.pattern!r}",
            )

    for prefix in _SAFE_PREFIXES:
        if lower == prefix.rstrip() or lower.startswith(prefix):
            return SafetyVerdict(mode=ApprovalMode.SAFE, reason="Known read-only command")

    return SafetyVerdict(
        mode=ApprovalMode.ASK,
        reason="Unknown command — approval required",
    )


def is_safe(command: str) -> bool:
    return classify_command(command).mode == ApprovalMode.SAFE


def is_blocked(command: str) -> bool:
    return classify_command(command).mode == ApprovalMode.BLOCK
