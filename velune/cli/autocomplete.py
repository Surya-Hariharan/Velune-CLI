"""Slash command completion for the Velune REPL.

Completions are fuzzy (prefix > substring > subsequence), grouped by category,
and boosted by recent use, so the menu surfaces what the user actually reaches
for. The REPL passes its live command registry in, so the completer can never
drift from the commands that actually exist.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document


@dataclass(frozen=True)
class CommandEntry:
    name: str
    description: str
    category: str = "General"
    aliases: tuple[str, ...] = ()


# Display order for categories in /help and the completion menu. Categories
# not listed here are appended afterwards, alphabetically.
CATEGORY_ORDER: list[str] = [
    "Session",
    "Workspace",
    "Models",
    "Providers",
    "Council",
    "Modes",
    "Memory",
    "Code",
    "Git",
    "Extend",
    "System",
]

# NOTE: Category assignments now live on each ``SlashCommand`` (set in
# velune.cli.slash_dispatcher) so /help and this completer share one source of
# truth and can never drift. The live REPL always passes ``commands=`` built
# from that registry; the static ``SLASH_COMMANDS`` fallback below defaults
# unlisted commands to "General".

# Static fallback used when no live registry is supplied (kept in sync with
# velune.cli.slash_dispatcher.build_slash_registry). Prefer passing
# `commands=` from the live registry so this can never drift.
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("help", "Show all available commands"),
    ("exit", "Exit the Velune session"),
    ("clear", "Clear the terminal screen (conversation preserved)"),
    ("new", "Start a new conversation session (project memory persists)"),
    ("project", "Open, close, or inspect project workspaces"),
    ("index", "Index the workspace so Velune understands its code"),
    ("doctor", "Run environment health checks"),
    ("config", "Show current system configuration settings"),
    ("stats", "Show session statistics: tokens, cost, turns, uptime"),
    ("history", "Show REPL command execution history"),
    ("hooks", "List active lifecycle hooks and their config"),
    ("model", "Discover, connect, switch, or inspect models"),
    ("models", "List all available models"),
    ("pull", "Download an Ollama model interactively"),
    ("delete", "Delete a locally installed Ollama model"),
    ("councilmodel", "Assign specific models to council agent roles"),
    ("run", "Execute a task through the Reasoning Council"),
    ("council", "Force full council tier on a task"),
    ("jobs", "List background jobs or cancel one"),
    ("dashboard", "Live progress dashboard: jobs, alerts, provider health"),
    ("session", "Pick, resume, save, or export sessions"),
    ("memory", "Inspect memory tiers and stats"),
    ("context", "Show context window usage"),
    ("graph", "Render a hierarchical tree of knowledge graph entities"),
    ("mode", "Show or switch the session mode: fast | max | normal"),
    ("optimus", "Speed mode — instant tier, smallest model"),
    ("godly", "Max power — full council, largest model"),
    ("normal", "Return to balanced normal mode"),
    ("diff", "Show uncommitted file changes from the last council run"),
    ("undo", "Revert the last Velune-generated git commit"),
    ("hunk", "Toggle hunk-by-hunk review mode for edits"),
    ("approve", "Set tool/command approval mode: safe | ask | block"),
    ("bench", "View or run empirical model capability benchmarks"),
    ("providers", "Add, manage, test, and discover models from cloud AI providers"),
    ("mcp", "Inspect MCP servers, tools, and resources"),
    ("plugin", "List, enable, disable, or reload declarative plugins"),
    ("push", "Push current branch to remote origin"),
    ("pr", "Create a pull request / merge request"),
    ("issue", "Fetch a GitHub/GitLab issue as conversation context"),
    ("sandbox", "Show current sandbox type and status"),
    ("lint", "Lint a Python file"),
    ("refactor", "Detect code smells in a Python file"),
    ("typify", "Suggest type hints for a Python file"),
]

# Commands whose first argument is a model id.
_MODEL_ARG_COMMANDS = frozenset({"model", "pull", "delete"})


def fuzzy_score(query: str, candidate: str) -> int:
    """Score how well *query* matches *candidate*. Higher is better, 0 = no match.

    Tiers: exact (1000) > prefix (500) > substring (250) > subsequence (1..100).
    Within the subsequence tier, denser matches score higher.
    """
    query = query.lower()
    candidate_l = candidate.lower()
    if not query:
        return 1
    if query == candidate_l:
        return 1000
    if candidate_l.startswith(query):
        return 500 - len(candidate_l)
    if query in candidate_l:
        return 250 - candidate_l.index(query)

    # Subsequence: every query char must appear in order.
    pos = -1
    first = -1
    for ch in query:
        pos = candidate_l.find(ch, pos + 1)
        if pos == -1:
            return 0
        if first == -1:
            first = pos
    span = pos - first + 1
    density = len(query) / span  # 1.0 = contiguous
    return max(1, int(100 * density) - first)


@dataclass
class _ScoredEntry:
    score: int
    entry: CommandEntry
    matched_alias: str | None = None
    recency_rank: int = -1  # 0 = most recent

    sort_key: tuple = field(init=False)

    def __post_init__(self) -> None:
        recency_boost = max(0, 5 - self.recency_rank) * 50 if self.recency_rank >= 0 else 0
        self.sort_key = (-(self.score + recency_boost), self.entry.name)


class SlashCompleter(Completer):
    def __init__(
        self,
        extra_commands: list[tuple[str, str]] | None = None,
        model_ids: list[str] | None = None,
        commands: list[CommandEntry] | None = None,
        max_results: int = 12,
        symbol_names: list[str] | None = None,
    ) -> None:
        if commands is not None:
            self._entries = list(commands)
        else:
            pairs = SLASH_COMMANDS + (extra_commands or [])
            self._entries = [CommandEntry(name=name, description=desc) for name, desc in pairs]
        self._model_ids: list[str] = model_ids or []
        self._symbol_names: list[str] = symbol_names or []
        self._max_results = max_results
        self._recent: deque[str] = deque(maxlen=8)

    def record_use(self, command_name: str) -> None:
        """Note that a command was executed, boosting it in future completions."""
        if command_name in self._recent:
            self._recent.remove(command_name)
        self._recent.appendleft(command_name)

    def set_model_ids(self, model_ids: list[str]) -> None:
        self._model_ids = list(model_ids)

    def set_symbol_names(self, names: list[str]) -> None:
        """Replace the in-memory symbol name cache."""
        self._symbol_names = list(names)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        # @@symbol completion (check before /-prefix so @@ is caught first)
        at_at = text.rfind("@@")
        if at_at != -1:
            partial = text[at_at + 2 :]
            if " " not in partial:
                yield from self._complete_symbol_mentions(partial)
                return

        if not text.startswith("/"):
            return

        body = text[1:]
        head, sep, rest = body.partition(" ")

        # "/<cmd> <partial>" → model id completion for model-taking commands
        if sep and head.lower() in _MODEL_ARG_COMMANDS:
            yield from self._complete_model_ids(rest)
            return
        if sep:
            return

        yield from self._complete_commands(head)

    def _complete_symbol_mentions(self, partial: str):
        """Complete @@<partial> against the in-memory symbol name cache."""
        scored = [(fuzzy_score(partial, name), name) for name in self._symbol_names]
        scored = [(s, name) for s, name in scored if s > 0]
        scored.sort(key=lambda t: (-t[0], t[1]))
        for _, name in scored[: self._max_results]:
            yield Completion(
                text=name,
                start_position=-len(partial),
                display=f"@@{name}",
                display_meta="symbol",
            )

    def _complete_model_ids(self, partial: str):
        scored = [(fuzzy_score(partial, mid), mid) for mid in self._model_ids]
        scored = [(s, mid) for s, mid in scored if s > 0]
        scored.sort(key=lambda t: (-t[0], t[1]))
        for _, mid in scored[: self._max_results]:
            yield Completion(
                text=mid,
                start_position=-len(partial),
                display=mid,
            )

    def _complete_commands(self, word: str):
        scored: list[_ScoredEntry] = []
        for entry in self._entries:
            best = fuzzy_score(word, entry.name)
            matched_alias = None
            for alias in entry.aliases:
                alias_score = fuzzy_score(word, alias)
                if alias_score > best:
                    best = alias_score
                    matched_alias = alias
            if best <= 0:
                continue
            recency_rank = -1
            if entry.name in self._recent:
                recency_rank = list(self._recent).index(entry.name)
            scored.append(_ScoredEntry(best, entry, matched_alias, recency_rank))

        scored.sort(key=lambda s: s.sort_key)
        for item in scored[: self._max_results]:
            entry = item.entry
            display = f"/{entry.name}"
            if item.matched_alias:
                display = f"/{entry.name} (/{item.matched_alias})"
            meta = f"{entry.category} · {entry.description}"
            yield Completion(
                text=entry.name,
                start_position=-len(word),
                display=display,
                display_meta=meta,
            )
