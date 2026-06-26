"""Tests for the session lifecycle systems: interrupts, session store, workspaces."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from velune._compat import uncancel_task
from velune.cli.interrupts import InterruptController
from velune.cli.sessions import SessionStore, auto_title
from velune.cli.workspaces import WorkspaceRegistry

# ─────────────────────────────────────────────────────────────────────────────
# InterruptController
# ─────────────────────────────────────────────────────────────────────────────


class TestInterruptController:
    def test_single_interrupt_does_not_open_exit(self):
        ctl = InterruptController()
        assert ctl.note_interrupt() is False

    def test_double_interrupt_within_window_exits(self):
        ctl = InterruptController()
        ctl.note_interrupt()
        assert ctl.note_interrupt() is True

    def test_interrupt_outside_window_resets(self, monkeypatch):
        ctl = InterruptController()
        clock = [100.0]
        monkeypatch.setattr("velune.cli.interrupts.time.monotonic", lambda: clock[0])
        ctl.note_interrupt()
        clock[0] += ctl.exit_window_seconds + 0.1
        assert ctl.note_interrupt() is False

    def test_exit_hint_window(self, monkeypatch):
        ctl = InterruptController()
        clock = [50.0]
        monkeypatch.setattr("velune.cli.interrupts.time.monotonic", lambda: clock[0])
        assert ctl.exit_hint_active is False
        ctl.note_interrupt()
        assert ctl.exit_hint_active is True
        clock[0] += ctl.exit_window_seconds + 0.1
        assert ctl.exit_hint_active is False

    def test_reset_exit_window(self):
        ctl = InterruptController()
        ctl.note_interrupt()
        ctl.reset_exit_window()
        assert ctl.note_interrupt() is False

    async def test_foreground_cancellation_marks_user_cancel(self):
        ctl = InterruptController()
        started = asyncio.Event()

        async def work():
            try:
                async with ctl.foreground():
                    started.set()
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                if not ctl.consume_user_cancelled():
                    raise
                task = asyncio.current_task()
                if task is not None:
                    uncancel_task(task)
                return "interrupted"
            return "finished"

        task = asyncio.create_task(work())
        await started.wait()
        # Simulate what the SIGINT handler does for a registered foreground task.
        ctl._user_cancelled = True
        task.cancel()
        assert await task == "interrupted"

    async def test_non_user_cancellation_propagates(self):
        ctl = InterruptController()
        started = asyncio.Event()

        async def work():
            try:
                async with ctl.foreground():
                    started.set()
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                if not ctl.consume_user_cancelled():
                    raise
                return "interrupted"

        task = asyncio.create_task(work())
        await started.wait()
        task.cancel()  # shutdown-style cancel: no user flag set
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_foreground_cleared_after_block(self):
        ctl = InterruptController()
        async with ctl.foreground():
            assert ctl.has_foreground is True
        assert ctl.has_foreground is False


# ─────────────────────────────────────────────────────────────────────────────
# auto_title
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoTitle:
    def test_strips_stopwords_and_keeps_intent(self):
        convo = [{"role": "user", "content": "can you help me debug the JWT refresh flow"}]
        title = auto_title(convo)
        assert "JWT" in title
        assert "debug" in title.lower()
        assert "can" not in title.lower().split()

    def test_strips_slash_command_prefix(self):
        convo = [{"role": "user", "content": "/run fix the auth bug in login.py"}]
        title = auto_title(convo)
        assert not title.startswith("/")
        assert "auth" in title.lower()

    def test_empty_conversation(self):
        assert auto_title([]) == "Untitled session"

    def test_system_turns_ignored(self):
        convo = [
            {"role": "system", "content": "You are a coding assistant."},
            {"role": "user", "content": "refactor SQLite memory layer"},
        ]
        assert "SQLite" in auto_title(convo)

    def test_long_title_truncated(self):
        convo = [{"role": "user", "content": "implement " + "verylongword" * 20}]
        assert len(auto_title(convo)) <= 48


# ─────────────────────────────────────────────────────────────────────────────
# SessionStore
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionStore:
    def _store(self, tmp_path: Path) -> SessionStore:
        return SessionStore(tmp_path / "sessions")

    def test_save_and_load_roundtrip(self, tmp_path):
        store = self._store(tmp_path)
        convo = [
            {"role": "user", "content": "design MCP integration"},
            {"role": "assistant", "content": "Here is a plan."},
        ]
        meta = store.save(convo, workspace=str(tmp_path / "proj"), model_id="llama3")
        loaded = store.load(meta.id)
        assert loaded is not None
        loaded_meta, loaded_convo = loaded
        assert loaded_convo == convo
        assert loaded_meta.model_id == "llama3"
        assert loaded_meta.project_name == "proj"
        assert "MCP" in loaded_meta.title

    def test_update_in_place_preserves_created_at(self, tmp_path):
        store = self._store(tmp_path)
        convo = [{"role": "user", "content": "first"}]
        meta = store.save(convo, workspace="w", model_id="m")
        convo.append({"role": "assistant", "content": "reply"})
        meta2 = store.save(convo, workspace="w", model_id="m", session_id=meta.id)
        assert meta2.id == meta.id
        assert meta2.created_at == meta.created_at
        assert meta2.turn_count == 2

    def test_list_filters_by_workspace(self, tmp_path):
        store = self._store(tmp_path)
        ws_a = tmp_path / "a"
        ws_b = tmp_path / "b"
        ws_a.mkdir()
        ws_b.mkdir()
        store.save([{"role": "user", "content": "alpha"}], workspace=str(ws_a), model_id="m")
        store.save([{"role": "user", "content": "beta"}], workspace=str(ws_b), model_id="m")
        only_a = store.list(workspace=str(ws_a))
        assert len(only_a) == 1
        assert only_a[0].title.lower().startswith("alpha")
        assert len(store.list()) == 2

    def test_delete(self, tmp_path):
        store = self._store(tmp_path)
        meta = store.save([{"role": "user", "content": "x"}], workspace="w", model_id="m")
        assert store.delete(meta.id) is True
        assert store.load(meta.id) is None
        assert store.delete("nope") is False

    def test_export_markdown(self, tmp_path):
        store = self._store(tmp_path)
        meta = store.save(
            [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}],
            workspace="w",
            model_id="m",
            title="My Session",
        )
        md = store.export_markdown(meta.id)
        assert md is not None
        assert "My Session" in md
        assert "### User" in md
        assert "answer" in md

    def test_legacy_flat_format_still_loads(self, tmp_path):
        root = tmp_path / "sessions"
        root.mkdir()
        legacy = {
            "id": "abc12345",
            "timestamp": "2026-01-01T10:00:00",
            "model_id": "old-model",
            "workspace": "C:/old",
            "conversation": [{"role": "user", "content": "legacy data"}],
            "turn_count": 1,
        }
        (root / "abc12345.json").write_text(json.dumps(legacy), encoding="utf-8")
        store = SessionStore(root)
        loaded = store.load("abc12345")
        assert loaded is not None
        meta, convo = loaded
        assert meta.model_id == "old-model"
        assert convo[0]["content"] == "legacy data"

    def test_corrupt_file_skipped_in_list(self, tmp_path):
        store = self._store(tmp_path)
        store.save([{"role": "user", "content": "good"}], workspace="w", model_id="m")
        (store.root / "broken.json").write_text("{not json", encoding="utf-8")
        assert len(store.list()) == 1


# ─────────────────────────────────────────────────────────────────────────────
# WorkspaceRegistry
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceRegistry:
    def _registry(self, tmp_path: Path) -> WorkspaceRegistry:
        return WorkspaceRegistry(tmp_path / "workspaces.json")

    def test_register_and_list(self, tmp_path):
        reg = self._registry(tmp_path)
        proj = tmp_path / "my-proj"
        proj.mkdir()
        info = reg.register(proj)
        assert info.name == "my-proj"
        assert reg.list()[0].name == "my-proj"

    def test_git_detection(self, tmp_path):
        reg = self._registry(tmp_path)
        proj = tmp_path / "gitproj"
        (proj / ".git").mkdir(parents=True)
        assert reg.register(proj).is_git is True

    def test_persistence_across_instances(self, tmp_path):
        path = tmp_path / "workspaces.json"
        proj = tmp_path / "persisted"
        proj.mkdir()
        WorkspaceRegistry(path).register(proj)
        assert WorkspaceRegistry(path).find_by_name("persisted") is not None

    def test_touch_updates_ordering(self, tmp_path, monkeypatch):
        reg = self._registry(tmp_path)
        a = tmp_path / "aaa"
        b = tmp_path / "bbb"
        a.mkdir()
        b.mkdir()
        reg.register(a)
        reg.register(b)
        # Force a strictly later timestamp for the touch.
        from datetime import datetime, timedelta

        import velune.cli.workspaces as ws_mod

        real_now = datetime.now()

        class _FakeDateTime:
            @staticmethod
            def now():
                return real_now + timedelta(seconds=5)

        monkeypatch.setattr(ws_mod, "datetime", _FakeDateTime)
        reg.touch(a)
        assert reg.list()[0].name == "aaa"

    def test_deleted_paths_pruned_from_list(self, tmp_path):
        reg = self._registry(tmp_path)
        gone = tmp_path / "gone"
        gone.mkdir()
        reg.register(gone)
        gone.rmdir()
        assert reg.list() == []

    def test_remove(self, tmp_path):
        reg = self._registry(tmp_path)
        proj = tmp_path / "remove-me"
        proj.mkdir()
        reg.register(proj)
        assert reg.remove("remove-me") is True
        assert reg.find_by_name("remove-me") is None

    def test_duplicate_register_is_idempotent(self, tmp_path):
        reg = self._registry(tmp_path)
        proj = tmp_path / "dup"
        proj.mkdir()
        reg.register(proj)
        reg.register(proj)
        assert len(reg.list()) == 1
