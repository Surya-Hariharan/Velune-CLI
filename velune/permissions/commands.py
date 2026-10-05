"""Classify shell commands as read-only, mutating, high-risk or blocked.

Builds on :func:`velune.tools.safety.classify_command` (the existing BLOCK
patterns and read-only prefixes) instead of keeping a second list, and adds
the *high-risk* tier the execution modes need: commands that are legitimate
in development but destroy work or reach beyond the project, so they need an
explicit confirmation even in AUTO mode.
"""

from __future__ import annotations

import re

from velune._compat import StrEnum
from velune.tools.safety import ApprovalMode, classify_command


class CommandClass(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    HIGH_RISK = "high_risk"
    BLOCKED = "blocked"


_HIGH_RISK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), why)
    for p, why in [
        (r"\bgit\s+reset\b.*--hard", "discards uncommitted changes"),
        (r"\bgit\s+clean\b.*-\w*[fdx]", "deletes untracked files"),
        (
            r"\bgit\s+push\b.*(--force\b|--force-with-lease|\s-f\b|\s\+\S)",
            "rewrites remote history",
        ),
        (r"\bgit\s+(checkout|restore)\b.*(\s--\s+\.|\s\.$|--source)", "discards local edits"),
        (r"\bgit\s+branch\b.*\s-D\b", "deletes a branch"),
        (r"\bgit\s+(rebase|filter-branch|filter-repo)\b", "rewrites history"),
        (r"\bgit\s+stash\s+(drop|clear)\b", "deletes stashed work"),
        (r"\bRemove-Item\b.*-Recurse", "recursive deletion"),
        (r"\b(del|erase)\b.*\s/[sS]\b", "recursive deletion"),
        (r"\brm\s+.*-\w*r", "recursive deletion"),
        (
            r"\b(setx|Set-ItemProperty|New-ItemProperty|reg\s+(add|delete))\b",
            "changes system settings",
        ),
        (r"\$env:\w+\s*=|\[Environment\]::SetEnvironmentVariable", "changes environment variables"),
        (
            r"\b(winget|choco|scoop|apt|apt-get|yum|dnf|brew|pacman)\s+install\b",
            "installs system software",
        ),
        (r"\b(npm|pnpm|yarn)\s+(install|add|i)\b.*(\s-g\b|--global)", "installs globally"),
        (r"\bpip3?\s+install\b.*--user\b", "installs into the user site"),
        (
            r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b",
            "publishes a package",
        ),
        (r"\bdocker\s+(system|volume|image)\s+prune\b", "deletes Docker data"),
        (r"\b(netsh|New-NetFirewallRule|ufw|iptables)\b", "changes network/firewall settings"),
    ]
]


def classify(command: str) -> tuple[CommandClass, str]:
    """Return the class of *command* and a short human reason."""
    verdict = classify_command(command)
    if verdict.mode is ApprovalMode.BLOCK:
        return CommandClass.BLOCKED, verdict.reason
    for pattern, why in _HIGH_RISK_PATTERNS:
        if pattern.search(command):
            return CommandClass.HIGH_RISK, why
    if verdict.mode is ApprovalMode.SAFE:
        return CommandClass.READ_ONLY, "read-only command"
    return CommandClass.MUTATING, "may change files or state"
