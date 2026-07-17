"""@mention file resolution for LLM context injection.

@filepath  — resolves to file content (existing behaviour).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Matches @path/to/file.py or @filename.py
# Negative lookbehind prevents email addresses (user@host.com) from matching.
_MENTION_RE = re.compile(r"(?<![a-zA-Z0-9])@([\w./\\-]+\.\w+)")

MAX_MENTION_CHARS = 8000
MAX_MENTIONS = 5

# Extensions treated as binary — skip these during fuzzy match
_BINARY_EXTS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".svg",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".whl",
        ".pyc",
        ".pyd",
        ".so",
        ".dll",
        ".exe",
        ".bin",
        ".db",
        ".sqlite",
    }
)


@dataclass
class MentionedFile:
    raw_token: str  # the original @... token
    resolved_path: Path  # absolute path to the file
    content: str  # file content (may be truncated)
    truncated: bool  # True if content was capped at MAX_MENTION_CHARS


def parse_mentions(
    text: str,
    workspace: Path,
) -> tuple[str, list[MentionedFile], list[str]]:
    """Parse @filepath tokens from user input.

    Resolution strategy:
    1. Exact relative path from workspace root.
    2. Basename fuzzy match: scan workspace for a file whose name matches.
       The shallowest match (fewest path segments) wins.

    Returns:
        (cleaned_text, resolved_mentions, unresolved_tokens)
        cleaned_text has @tokens removed where resolved.
        Unresolved tokens are left in cleaned_text for the caller to warn about.
    """
    tokens = _MENTION_RE.findall(text)
    if not tokens:
        return text, [], []

    mentioned: list[MentionedFile] = []
    unresolved: list[str] = []
    seen_paths: set[Path] = set()
    cleaned = text

    for token in tokens[:MAX_MENTIONS]:
        resolved = _resolve_path(token, workspace)
        if resolved is None:
            unresolved.append(token)
            continue
        if resolved in seen_paths:
            cleaned = cleaned.replace(f"@{token}", "", 1)
            continue
        seen_paths.add(resolved)

        try:
            raw_content = resolved.read_text(errors="replace")
        except OSError:
            unresolved.append(token)
            continue

        truncated = len(raw_content) > MAX_MENTION_CHARS
        content = raw_content[:MAX_MENTION_CHARS]
        if truncated:
            content += "\n... [file truncated]"

        mentioned.append(
            MentionedFile(
                raw_token=f"@{token}",
                resolved_path=resolved,
                content=content,
                truncated=truncated,
            )
        )
        cleaned = cleaned.replace(f"@{token}", "", 1)

    return cleaned.strip(), mentioned, unresolved


def _resolve_path(token: str, workspace: Path) -> Path | None:
    """Resolve an @mention token to an absolute path inside the workspace."""
    # 1. Exact relative path from workspace root
    candidate = (workspace / token).resolve()
    try:
        candidate.relative_to(workspace.resolve())
        if candidate.is_file() and candidate.suffix not in _BINARY_EXTS:
            return candidate
    except ValueError:
        pass

    # 2. Basename fuzzy match — find the shallowest file with matching name
    target_name = Path(token).name
    if not target_name:
        return None

    best: Path | None = None
    best_depth = 999
    try:
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in _BINARY_EXTS:
                continue
            if path.name == target_name:
                try:
                    rel = path.relative_to(workspace)
                    depth = len(rel.parts)
                    if depth < best_depth:
                        best_depth = depth
                        best = path
                except ValueError:
                    pass
    except OSError:
        pass

    return best


def build_mention_context(mentioned: list[MentionedFile]) -> str:
    """Format mentioned files as a context block for LLM injection."""
    if not mentioned:
        return ""
    blocks: list[str] = []
    for m in mentioned:
        rel = m.resolved_path.name
        blocks.append(
            f"[MENTIONED FILE: {rel}]\n```\n{m.content}\n```\n[END MENTIONED FILE: {rel}]"
        )
    return "\n\n".join(blocks)
