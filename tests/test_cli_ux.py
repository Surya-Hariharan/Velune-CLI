"""Tests for the REPL UX layer: fuzzy completion, stream buffer, status bar."""

from __future__ import annotations

from prompt_toolkit.document import Document

from velune.cli.autocomplete import CommandEntry, SlashCompleter, fuzzy_score
from velune.cli.rendering.markdown import MarkdownStreamBuffer, StreamStats
from velune.cli.statusbar import StatusBarState, render_status_bar


def _completions(completer: SlashCompleter, text: str) -> list[str]:
    doc = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(doc, None)]


ENTRIES = [
    CommandEntry("model", "Switch model", "Models", aliases=("m",)),
    CommandEntry("models", "List models", "Models", aliases=("ls",)),
    CommandEntry("memory", "Inspect memory", "Memory", aliases=("mem",)),
    CommandEntry("councilmodel", "Assign council models", "Council", aliases=("cm",)),
    CommandEntry("doctor", "Health checks", "System", aliases=("diag",)),
]


class TestFuzzyScore:
    def test_exact_beats_prefix(self):
        assert fuzzy_score("model", "model") > fuzzy_score("model", "models")

    def test_prefix_beats_substring(self):
        assert fuzzy_score("mod", "model") > fuzzy_score("mod", "councilmodel")

    def test_substring_beats_subsequence(self):
        assert fuzzy_score("ode", "model") > fuzzy_score("ode", "doctor")

    def test_subsequence_matches(self):
        assert fuzzy_score("cmodel", "councilmodel") > 0

    def test_non_match_is_zero(self):
        assert fuzzy_score("xyz", "model") == 0

    def test_case_insensitive(self):
        assert fuzzy_score("MODEL", "model") == 1000


class TestSlashCompleter:
    def test_prefix_completion(self):
        completer = SlashCompleter(commands=ENTRIES)
        results = _completions(completer, "/mod")
        assert results[0] == "model"
        assert "models" in results

    def test_fuzzy_subsequence_completion(self):
        completer = SlashCompleter(commands=ENTRIES)
        assert "councilmodel" in _completions(completer, "/cnclm")

    def test_alias_match_surfaces_canonical_name(self):
        completer = SlashCompleter(commands=ENTRIES)
        assert "doctor" in _completions(completer, "/diag")

    def test_recent_use_boosts_ranking(self):
        completer = SlashCompleter(commands=ENTRIES)
        # "memory" scores below "model"/"models" for prefix "m"
        before = _completions(completer, "/m")
        assert before.index("memory") > before.index("model")
        completer.record_use("memory")
        after = _completions(completer, "/m")
        assert after.index("memory") < after.index("model")

    def test_no_completions_without_slash(self):
        completer = SlashCompleter(commands=ENTRIES)
        assert _completions(completer, "hello") == []

    def test_model_argument_completion_is_fuzzy(self):
        completer = SlashCompleter(
            commands=ENTRIES,
            model_ids=["qwen2.5-coder-7b", "llama3.1-8b", "phi3-mini"],
        )
        assert _completions(completer, "/model qwen") == ["qwen2.5-coder-7b"]
        assert "qwen2.5-coder-7b" in _completions(completer, "/pull qcoder")

    def test_static_fallback_list_still_works(self):
        completer = SlashCompleter()
        assert "help" in _completions(completer, "/he")


class TestMarkdownStreamBuffer:
    def test_accumulates_content(self):
        buf = MarkdownStreamBuffer()
        buf.append("Hello ")
        buf.append("world")
        assert buf.raw_content == "Hello world"

    def test_trailing_partial_fence_trimmed(self):
        assert MarkdownStreamBuffer._stabilize("text\n`") == "text"
        assert MarkdownStreamBuffer._stabilize("text\n``") == "text"
        assert MarkdownStreamBuffer._stabilize("`") == ""

    def test_open_fence_virtually_closed(self):
        stabilized = MarkdownStreamBuffer._stabilize("```python\nprint('hi')")
        assert stabilized.endswith("```")
        # Balanced fence count after stabilization
        fences = [ln for ln in stabilized.splitlines() if ln.lstrip().startswith("```")]
        assert len(fences) % 2 == 0

    def test_closed_fence_untouched(self):
        content = "```python\nprint('hi')\n```"
        assert MarkdownStreamBuffer._stabilize(content) == content

    def test_renderable_cached_between_appends(self):
        buf = MarkdownStreamBuffer()
        buf.append("stable text")
        first = buf.get_renderable()
        assert buf.get_renderable() is first
        buf.append(" more")
        assert buf.get_renderable() is not first


class TestStreamStats:
    def test_records_throughput(self):
        stats = StreamStats()
        stats.record_chunk("abcd" * 25)  # ~25 tokens
        assert stats.approx_tokens == 25
        assert stats.time_to_first_token_ms is not None
        assert stats.tokens_per_second > 0

    def test_no_chunks_means_no_first_token(self):
        stats = StreamStats()
        assert stats.time_to_first_token_ms is None


class TestStatusBar:
    def _text(self, state: StatusBarState) -> str:
        return "".join(fragment for _, fragment in render_status_bar(state))

    def test_renders_core_fields(self):
        state = StatusBarState(
            model_id="qwen2.5-coder-7b",
            mode_label="NORMAL",
            profile_label="BALANCED",
            context_pct=42.0,
        )
        text = self._text(state)
        assert "qwen2.5-coder-7b" in text
        assert "NORMAL" in text
        assert "BALANCED" in text
        assert "ctx 42%" in text

    def test_latency_formats_ms_and_seconds(self):
        assert "first token 250ms" in self._text(StatusBarState(last_latency_ms=250.0))
        assert "first token 1.5s" in self._text(StatusBarState(last_latency_ms=1500.0))

    def test_optional_fields_omitted(self):
        text = self._text(StatusBarState())
        assert "tok/s" not in text
        assert "first token" not in text
        assert "no model" in text
