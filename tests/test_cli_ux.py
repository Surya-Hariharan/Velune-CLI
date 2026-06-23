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
        # "models" ranks below "model" for prefix "mod" (longer name)
        before = _completions(completer, "/mod")
        assert before.index("models") > before.index("model")
        completer.record_use("models")
        after = _completions(completer, "/mod")
        assert after.index("models") < after.index("model")

    def test_exact_alias_match_outranks_recency(self):
        completer = SlashCompleter(commands=ENTRIES)
        completer.record_use("memory")
        # "/m" is an exact alias of /model — it must stay on top
        assert _completions(completer, "/m")[0] == "model"

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
        assert MarkdownStreamBuffer._stabilize("``") == ""

    def test_lone_triple_backtick_at_end_closes_as_open_fence(self):
        # "```" alone is parsed as an opening fence (fence_count == 1), so it
        # must be virtually closed. The result should have an even fence count.
        stabilized = MarkdownStreamBuffer._stabilize("```")
        fences = [ln for ln in stabilized.splitlines() if ln.lstrip().startswith("```")]
        assert len(fences) % 2 == 0

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

    def test_fence_split_across_small_chunks(self):
        """Provider streaming in 1-char deltas must not corrupt fence detection."""
        full = "```python\nfor i in range(10):\n    print(i)\n```"
        buf = MarkdownStreamBuffer()
        for char in full:
            buf.append(char)
        # After the full fence is consumed the buffer should be stable (even fence count)
        stabilized = MarkdownStreamBuffer._stabilize(buf.raw_content)
        fences = [ln for ln in stabilized.splitlines() if ln.lstrip().startswith("```")]
        assert len(fences) % 2 == 0
        assert buf.raw_content == full

    def test_two_code_blocks_balanced(self):
        content = "```py\nfoo()\n```\n\nSome text\n\n```js\nbar()\n```"
        stabilized = MarkdownStreamBuffer._stabilize(content)
        fences = [ln for ln in stabilized.splitlines() if ln.lstrip().startswith("```")]
        assert len(fences) == 4  # 2 open + 2 close, already balanced
        assert stabilized == content  # no mutation needed


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
        # The status bar renders latency as "⚡ 250ms" / "⚡ 1.5s".
        assert "250ms" in self._text(StatusBarState(last_latency_ms=250.0))
        assert "1.5s" in self._text(StatusBarState(last_latency_ms=1500.0))

    def test_optional_fields_omitted(self):
        text = self._text(StatusBarState())
        assert "tok/s" not in text
        assert "first token" not in text
        assert "no model" in text

    def test_token_budget_rendered_when_known(self):
        state = StatusBarState(context_pct=71.0, context_used=142_000, context_max=200_000)
        text = self._text(state)
        assert "ctx 71%" in text
        assert "142k/200k" in text

    def test_token_budget_omitted_without_max(self):
        text = self._text(StatusBarState(context_pct=10.0, context_used=500))
        assert "ctx 10%" in text
        assert "/" not in text.split("ctx 10%")[1].split("│")[0]

    def test_session_cost_only_when_nonzero(self):
        assert "$" not in self._text(StatusBarState(session_cost=0.0))
        assert "$0.42" in self._text(StatusBarState(session_cost=0.42))

    def test_provider_health_states(self):
        assert "provider ok" in self._text(StatusBarState(provider_health="ok"))
        assert "provider degraded" in self._text(StatusBarState(provider_health="degraded"))
        assert "provider down" in self._text(StatusBarState(provider_health="down"))
        assert "provider" not in self._text(StatusBarState(provider_health=None))


class TestPipelineTracker:
    def _plain(self, tracker) -> str:
        return tracker.render().plain

    def test_initial_all_waiting(self):
        from velune.cli.display.pipeline import PipelineTracker

        tracker = PipelineTracker()
        plain = self._plain(tracker)
        assert "Planner" in plain and "Synthesis" in plain
        assert "◆" not in plain  # nothing active yet
        assert "✓" not in plain  # nothing done yet

    def test_advance_completes_earlier_stages(self):
        from velune.cli.display.pipeline import PipelineTracker

        tracker = PipelineTracker()
        tracker.advance("planner")
        tracker.advance("reviewer")
        assert tracker.state_of("planner") == "done"
        assert tracker.state_of("coder") == "done"
        assert tracker.state_of("reviewer") == "active"
        assert tracker.state_of("synthesis") == "waiting"

    def test_unknown_phase_is_appended_not_dropped(self):
        from velune.cli.display.pipeline import PipelineTracker

        tracker = PipelineTracker()
        tracker.advance("debate")
        assert "Debate" in self._plain(tracker)
        assert tracker.state_of("debate") == "active"

    def test_fail_marks_current_stage(self):
        from velune.cli.display.pipeline import PipelineTracker

        tracker = PipelineTracker()
        tracker.advance("coder")
        tracker.fail()
        assert tracker.state_of("coder") == "failed"
        assert "✗" in self._plain(tracker)

    def test_complete_finishes_active_stage(self):
        from velune.cli.display.pipeline import PipelineTracker

        tracker = PipelineTracker()
        tracker.advance("synthesis")
        tracker.complete()
        assert tracker.state_of("synthesis") == "done"

    def test_failed_stage_not_overwritten_by_advance(self):
        from velune.cli.display.pipeline import PipelineTracker

        tracker = PipelineTracker()
        tracker.advance("planner")
        tracker.fail("planner")
        tracker.advance("reviewer")
        assert tracker.state_of("planner") == "failed"


class TestDesignTokens:
    def test_context_state_thresholds(self):
        from velune.cli import design

        assert design.context_state(0.0) == "ok"
        assert design.context_state(69.9) == "ok"
        assert design.context_state(70.0) == "warn"
        assert design.context_state(89.9) == "warn"
        assert design.context_state(90.0) == "danger"
        assert design.context_state(100.0) == "danger"

    def test_color_enabled_honors_no_color(self, monkeypatch):
        from velune.cli import design

        monkeypatch.setenv("NO_COLOR", "1")
        assert design.color_enabled() is False
