"""Classify shell commands as read-only, mutating, high-risk or blocked.

Builds on :func:`velune.tools.safety.classify_command` (the existing BLOCK
patterns and read-only prefixes) instead of keeping a second list, and adds
the *high-risk* tier the execution modes need: commands that are legitimate
in development but destroy work or reach beyond the project, so they need an
explicit confirmation even in AUTO mode.
"""

from __future__ import annotations

import re
import shlex

from velune._compat import StrEnum
from velune.core.argv import split_command
from velune.tools.safety import (
    ApprovalMode,
    classify_command,
    has_shell_syntax,
    program_name,
    split_segments,
)


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

# ── Integration-owning programs ──────────────────────────────────────────────────────────────
#
# Docker, the databases and Git remotes each have a native integration with its own permission
# rules (a database connector runs reads only in AUTO, Docker only touches what Velune created,
# the agent cannot push). A shell command that reaches the same system must not be a
# less-guarded route to it, so the programs below are judged by what they would do to that
# system, token by token, wherever they appear in the line (after ``cd x &&``, inside
# ``env``/``cmd /c``/``powershell -Command``, behind ``git -C dir``).

_WRAPPERS = frozenset({"env", "nohup", "time", "nice", "command", "exec", "stdbuf", "xargs"})
_SHELL_RUNNERS = {
    "cmd": ("/c", "/k", "/r"),
    "powershell": ("-command", "-c"),
    "pwsh": ("-command", "-c"),
    "bash": ("-c",),
    "sh": ("-c",),
    "zsh": ("-c",),
}
_MAX_DEPTH = 4

_GIT_GLOBAL_WITH_VALUE = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)
# Subcommands that send commits, refs or objects to another repository or service.
_GIT_REMOTE_MUTATIONS = frozenset({"push", "send-pack", "svn", "lfs", "cvsexportcommit", "p4"})

_DOCKER_PROGRAMS = frozenset({"docker", "podman", "docker-compose", "podman-compose", "nerdctl"})
_DOCKER_READ_VERBS = frozenset(
    {
        "ps", "images", "inspect", "logs", "info", "version", "stats", "top", "port", "diff",
        "history", "events", "search", "ls", "list", "config", "help", "df",
    }
)  # fmt: skip
# ``docker <noun> <verb>``: the nouns whose verbs are ls / inspect / logs / ... or a change.
_DOCKER_NOUNS = frozenset(
    {"container", "image", "volume", "network", "context", "compose", "system", "builder", "buildx"}
)

_DATABASE_PROGRAMS = frozenset(
    {
        "mysql", "mariadb", "mysqlsh", "mysqladmin", "mysqldump", "mysqlpump", "mariadb-dump",
        "mariadb-admin", "mycli", "psql", "pgcli", "pg_dump", "pg_dumpall", "pg_restore",
        "createdb", "dropdb", "createuser", "dropuser", "mongosh", "mongo", "mongodump",
        "mongorestore", "mongoexport", "mongoimport", "sqlite3", "litecli", "redis-cli",
        "sqlcmd", "sqlplus", "cqlsh", "usql", "clickhouse-client", "duckdb", "prisma",
        "alembic", "flyway", "liquibase",
    }
)  # fmt: skip
_INFO_ONLY_ARGS = frozenset({"--version", "-v", "--help", "-?"})

_FORGE_PROGRAMS = frozenset({"gh", "glab"})
_FORGE_READ_VERBS = frozenset({"view", "list", "ls", "status", "diff", "checks", "show", "help"})

# Programs whose purpose is to write: reported as such (they are MUTATING either way, because
# nothing outside the read-only table is ever read-only).
_WRITE_PROGRAMS = frozenset(
    {
        "set-content", "add-content", "out-file", "tee-object", "tee", "new-item", "clear-content",
        "copy-item", "move-item", "rename-item", "remove-item", "export-csv", "export-clixml",
        "sc", "ac", "ni", "ri", "ren", "rename", "copy", "move", "cp", "mv", "rm", "del", "erase",
        "touch", "truncate", "dd", "install", "ln", "mkdir", "md", "rmdir", "rd", "sed", "perl",
    }
)  # fmt: skip

_RANK = {
    CommandClass.READ_ONLY: 0,
    CommandClass.MUTATING: 1,
    CommandClass.HIGH_RISK: 2,
    CommandClass.BLOCKED: 3,
}

_Finding = tuple[CommandClass, str]


def _git_command(tokens: list[str]) -> tuple[str, list[str], list[str]]:
    """``(subcommand, its arguments, global options)`` of a ``git`` argv, skipping ``-C dir`` etc."""
    index, global_opts = 1, []
    while index < len(tokens):
        token = tokens[index]
        if token in _GIT_GLOBAL_WITH_VALUE:
            global_opts.extend(tokens[index : index + 2])
            index += 2
        elif token.startswith("-"):
            global_opts.append(token)
            index += 1
        else:
            return token.lower(), tokens[index + 1 :], global_opts
    return "", [], global_opts


def _analyse_git(tokens: list[str]) -> _Finding | None:
    sub, rest, global_opts = _git_command(tokens)
    if any(o.lower().startswith("alias.") for o in global_opts):
        return CommandClass.HIGH_RISK, "defines a git alias for this command"
    if sub in _GIT_REMOTE_MUTATIONS:
        return CommandClass.HIGH_RISK, "publishes to a remote repository"
    if sub == "remote" and rest[:1] and rest[0].lower() in ("add", "set-url", "remove", "rm"):
        return CommandClass.HIGH_RISK, "changes where this repository publishes"
    if sub == "submodule" and rest[:1] == ["foreach"]:
        return CommandClass.HIGH_RISK, "runs a command in every submodule"
    # The history-rewriting rules are written against ``git <sub> <args>`` text.
    canonical = " ".join(["git", sub, *rest])
    for pattern, why in _HIGH_RISK_PATTERNS:
        if pattern.search(canonical):
            return CommandClass.HIGH_RISK, why
    return None


def _analyse_docker(tokens: list[str]) -> _Finding | None:
    args = [t.lower() for t in tokens[1:]]
    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        if all(a in _INFO_ONLY_ARGS for a in args):
            return None
        return CommandClass.HIGH_RISK, "changes Docker state"
    first = positional[0]
    if first in _DOCKER_NOUNS and len(positional) > 1:
        # ``docker compose up`` / ``docker volume rm``: judge the verb after the noun.
        if positional[1] in _DOCKER_READ_VERBS:
            return None
        return CommandClass.HIGH_RISK, f"docker {first} {positional[1]} changes Docker state"
    if first in _DOCKER_READ_VERBS:
        return None
    return CommandClass.HIGH_RISK, f"docker {first} changes Docker state or runs a container"


def _analyse_database(program: str, tokens: list[str]) -> _Finding | None:
    args = [t.lower() for t in tokens[1:]]
    if args and all(a in _INFO_ONLY_ARGS for a in args):
        return None
    return (
        CommandClass.HIGH_RISK,
        f"{program} talks to a database directly, bypassing the database integration's "
        "read-only and approval rules",
    )


def _analyse_forge(program: str, tokens: list[str]) -> _Finding | None:
    args = [t.lower() for t in tokens[1:] if not t.startswith("-")]
    if not args and all(t.lower() in _INFO_ONLY_ARGS for t in tokens[1:]):
        return None
    if args[:1] != ["api"] and any(a in _FORGE_READ_VERBS for a in args[1:2]):
        return None
    return CommandClass.HIGH_RISK, f"{program} changes a hosted repository"


def _inner_command(tokens: list[str]) -> str | None:
    """The command *line* a wrapper (``env``, ``cmd /c``, ``bash -c``, ``timeout``) hands on to."""
    program = program_name(tokens[0]).replace("\\", "/").rsplit("/", 1)[-1]
    rest = tokens[1:]
    if program in _SHELL_RUNNERS:
        flags = _SHELL_RUNNERS[program]
        for index, token in enumerate(rest):
            if token.lower() in flags and index + 1 < len(rest):
                return " ".join(rest[index + 1 :])
        return None
    if program == "timeout":
        rest = [t for t in rest if not t.startswith("-")][1:]
    elif program in _WRAPPERS:
        while rest and (rest[0].startswith("-") or "=" in rest[0]):
            rest = rest[1:]
    else:
        return None
    return " ".join(shlex.quote(t) if not t.isalnum() else t for t in rest) if rest else None


def _classify_simple(command: str, depth: int) -> _Finding | None:
    """Verdict for one simple command (no chaining), or ``None`` when nothing here is notable."""
    try:
        tokens = split_command(command)
    except ValueError:
        return None
    if not tokens:
        return None
    program = program_name(tokens[0]).replace("\\", "/").rsplit("/", 1)[-1]
    if program.endswith((".cmd", ".bat", ".ps1")):
        program = program.rsplit(".", 1)[0]

    inner = _inner_command(tokens) if depth < _MAX_DEPTH else None
    if inner:
        return _classify_line(inner, depth + 1)
    if program == "git":
        return _analyse_git(tokens)
    if program in _DOCKER_PROGRAMS:
        return _analyse_docker(tokens)
    if program in _DATABASE_PROGRAMS:
        return _analyse_database(program, tokens)
    if program in _FORGE_PROGRAMS:
        return _analyse_forge(program, tokens)
    if program in _WRITE_PROGRAMS:
        return CommandClass.MUTATING, "writes or removes files"
    return None


def _classify_line(command: str, depth: int) -> _Finding | None:
    """The most severe verdict over every simple command in *command* (chained or not)."""
    worst: _Finding | None = None
    for segment in split_segments(command):
        found = _classify_simple(segment, depth)
        if found is not None and (worst is None or _RANK[found[0]] > _RANK[worst[0]]):
            worst = found
    return worst


def classify(command: str) -> tuple[CommandClass, str]:
    """Return the class of *command* and a short human reason."""
    verdict = classify_command(command)
    if verdict.mode is ApprovalMode.BLOCK:
        return CommandClass.BLOCKED, verdict.reason
    for pattern, why in _HIGH_RISK_PATTERNS:
        if pattern.search(command):
            return CommandClass.HIGH_RISK, why
    found = _classify_line(command, 0)
    if found is not None and found[0] is CommandClass.HIGH_RISK:
        return found
    if verdict.mode is ApprovalMode.SAFE:
        return CommandClass.READ_ONLY, "read-only command"
    if found is not None:
        return found
    if has_shell_syntax(command):
        return CommandClass.MUTATING, "shell chaining, piping or redirection may change files"
    return CommandClass.MUTATING, "may change files or state"


# ── What a command writes to ─────────────────────────────────────────────────────────────────
#
# Whether a command *changes* a credentials file is a different question from whether it is
# "read-only": ``cat .env`` only reads it, ``echo x > .env`` and ``cp .env.example .env`` change
# it, ``pytest --env-file .env`` merely names it. The functions below return only the operands
# a command writes, replaces, moves or deletes, so the caller can ask about secret files without
# flagging every command that mentions one.

# Every operand of these is written, truncated, created or deleted.
_OPERAND_WRITERS = frozenset(
    {
        "rm", "del", "erase", "remove-item", "ri", "rd", "rmdir", "clear-content", "clc",
        "truncate", "touch", "tee", "tee-object", "set-content", "sc", "add-content", "ac",
        "out-file", "new-item", "ni", "set-item", "export-csv", "export-clixml", "shred",
    }
)  # fmt: skip
# The destination (last operand, or ``-t DIR`` / ``-Destination``) is written; sources are read.
_DESTINATION_WRITERS = frozenset(
    {"cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "install", "ln", "rsync"}
)
# Source and destination both change (the source is removed or renamed).
_MOVERS = frozenset({"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni"})
# ``sed -i`` / ``perl -i`` edit their file operands in place; without -i they only print.
_IN_PLACE_EDITORS = frozenset({"sed", "perl"})
# git subcommands that overwrite or remove working-tree files named as operands.
_GIT_FILE_WRITERS = frozenset({"restore", "checkout", "rm", "mv", "clean"})
_DESTINATION_OPTIONS = frozenset({"-t", "--target-directory", "-destination", "-dest"})
_REDIRECT_END = " \t;|&<>\n\r"


def _redirect_targets(command: str) -> list[str]:
    """Words that follow an unquoted ``>``, ``>>``, ``>|``, ``n>`` or ``&>`` (glued or spaced).

    ``2>&1`` and ``>&2`` duplicate a descriptor and name no file.
    """
    targets: list[str] = []
    quote = ""
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch != ">":
            i += 1
            continue
        while i < n and command[i] == ">":
            i += 1
        if command[i : i + 1] == "&" and (
            command[i + 1 : i + 2].isdigit() or command[i + 1 : i + 2] == "-"
        ):
            i += 2
            continue
        if i < n and command[i] == "|":
            i += 1
        while i < n and command[i] in " \t":
            i += 1
        word: list[str] = []
        word_quote = ""
        while i < n:
            c = command[i]
            if word_quote:
                if c == word_quote:
                    word_quote = ""
                else:
                    word.append(c)
            elif c in "'\"":
                word_quote = c
            elif c in _REDIRECT_END:
                break
            else:
                word.append(c)
            i += 1
        if word:
            targets.append("".join(word))
    return targets


def _operand_values(tokens: list[str]) -> list[str]:
    """Operands of a command: option words are dropped, ``--opt=value`` keeps its value."""
    values: list[str] = []
    for token in tokens:
        if token.startswith("-"):
            if "=" in token:
                values.append(token.split("=", 1)[1])
            continue
        if "=" in token and token.split("=", 1)[0].lower() in ("of", "if"):
            if token.lower().startswith("of="):
                values.append(token.split("=", 1)[1])
            continue
        values.append(token)
    return values


def _simple_write_targets(command: str, depth: int) -> list[str]:
    try:
        tokens = split_command(command)
    except ValueError:
        return []
    if not tokens:
        return []
    program = program_name(tokens[0]).replace("\\", "/").rsplit("/", 1)[-1]
    if program.endswith((".cmd", ".bat", ".ps1")):
        program = program.rsplit(".", 1)[0]
    rest = tokens[1:]

    inner = _inner_command(tokens) if depth < _MAX_DEPTH else None
    if inner:
        return command_write_targets(inner, depth + 1)
    if program in _OPERAND_WRITERS:
        return _operand_values(rest)
    if program in _MOVERS:
        return _operand_values(rest)
    if program == "dd":  # only ``of=`` is written; ``if=`` is read
        return [t.split("=", 1)[1] for t in rest if t.lower().startswith("of=")]
    if program in _DESTINATION_WRITERS:
        # ``-t DIR`` / ``--target-directory=DIR`` / ``-Destination DIR`` name the destination
        # explicitly; every other operand is then a source. Otherwise it is the last operand.
        named: list[str] = []
        for index, token in enumerate(rest):
            option, equals, value = token.partition("=")
            if option.lower() in _DESTINATION_OPTIONS:
                if equals:
                    named.append(value)
                elif index + 1 < len(rest):
                    named.append(rest[index + 1])
        return named or _operand_values(rest)[-1:]
    if program in _IN_PLACE_EDITORS:
        in_place = any(
            t.lower().startswith("--in-place")
            or (t.startswith("-") and not t.startswith("--") and "i" in t[1:].lower())
            for t in rest
        )
        return _operand_values(rest) if in_place else []
    if program == "git":
        sub, args, _ = _git_command(tokens)
        return _operand_values(args) if sub in _GIT_FILE_WRITERS else []
    return []


def command_write_targets(command: str, depth: int = 0) -> list[str]:
    """The path operands *command* writes, replaces, moves or deletes (best effort).

    Covers redirection (``>``, ``>>``, ``2>``, ``&>``, ``>|``), the write commands of POSIX and
    PowerShell (``tee``, ``Set-Content``, ``Out-File``, ``Remove-Item`` ...), copy and move
    destinations, in-place editors and git commands that overwrite working-tree files, in every
    chained segment and inside ``env`` / ``cmd /c`` / ``powershell -Command`` / ``bash -c``
    wrappers. It names what a command line *says* it writes; what a program decides to write on its
    own (``python script.py``) is not visible here. Commands that only read a file name it without
    being listed.
    """
    targets = _redirect_targets(command)
    for segment in split_segments(command):
        targets.extend(_simple_write_targets(segment, depth))
    return targets
