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
import shlex
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


# ---------------------------------------------------------------------------
# Read-only means the *arguments* are read-only too, not just the first words
# ---------------------------------------------------------------------------
#
# A SAFE command is never shown to the user in MANUAL mode and is allowed even in PLAN mode, so
# a prefix such as "find " or "git log" is only a safe start of a command line, not a
# guarantee: ``find . -delete`` deletes, ``git log --output=f`` overwrites f, and
# ``git fetch --upload-pack=CMD`` runs CMD. These tables name the options that turn an
# otherwise read-only program into one that writes, deletes or runs something; a command
# that uses one is classified ASK (it still runs, after the user approves it).

_FIND_ACTIONS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
_FD_EXEC = frozenset({"-x", "--exec", "-X", "--exec-batch"})
# Options of git that write a file or run a program. Git accepts any unambiguous abbreviation
# of a long option, so a shortened spelling (``--outp=f``) is treated like the full one.
_GIT_DANGEROUS_OPTIONS = ("--output", "--upload-pack", "--receive-pack", "--exec", "--exec-path")
# The only forms of ``git branch`` / ``tag`` / ``remote`` that merely list.
_GIT_BRANCH_LISTING = frozenset(
    {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "--show-current"}
)
_GIT_REMOTE_LISTING = ([], ["-v"], ["--verbose"])


def _is_dangerous_git_option(arg: str) -> bool:
    if not arg.startswith("--"):
        return False
    name = arg.split("=", 1)[0]
    return len(name) > 2 and any(opt.startswith(name) for opt in _GIT_DANGEROUS_OPTIONS)


def _git_is_read_only(args: list[str]) -> bool:
    if any(_is_dangerous_git_option(a) for a in args):
        return False
    sub, rest = (args[0], args[1:]) if args else ("", [])
    if sub == "fetch":  # reaches the network, writes refs and objects, can run --upload-pack
        return False
    if sub == "branch":
        return all(a in _GIT_BRANCH_LISTING for a in rest)
    if sub == "tag":
        return not rest or (
            rest[0] in ("-l", "--list") and not any(a.startswith("-") for a in rest[1:])
        )
    if sub == "remote":
        return rest in _GIT_REMOTE_LISTING or (len(rest) == 2 and rest[0] == "get-url")
    if sub == "stash":
        return rest[:1] == ["list"]
    return True


def _arguments_are_read_only(command: str) -> bool:
    """False when *command* uses an option that makes an otherwise read-only program write."""
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return False
    if not tokens:
        return True
    program = tokens[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if program.endswith(".exe"):
        program = program[:-4]
    args = [t.strip("\"'").lower() for t in tokens[1:]]
    if program == "find":
        return not any(a in _FIND_ACTIONS for a in args)
    if program == "fd":
        return not any(a in _FD_EXEC or a.startswith("--exec") for a in args)
    if program == "rg":  # --pre and --hostname-bin run a program on every file
        return not any(a.startswith(("--pre", "--hostname-bin")) for a in args)
    if program == "git":
        return _git_is_read_only(args)
    return True


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
      2. Known-safe prefix, and no argument that writes or runs a program → SAFE
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
            if not _arguments_are_read_only(normalized):
                return SafetyVerdict(
                    mode=ApprovalMode.ASK,
                    reason="Uses an option that writes, deletes or runs a program — approval required",
                )
            return SafetyVerdict(mode=ApprovalMode.SAFE, reason="Known read-only command")

    return SafetyVerdict(
        mode=ApprovalMode.ASK,
        reason="Unknown command — approval required",
    )


def is_safe(command: str) -> bool:
    return classify_command(command).mode == ApprovalMode.SAFE


def is_blocked(command: str) -> bool:
    return classify_command(command).mode == ApprovalMode.BLOCK
