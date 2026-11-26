"""The run store: a swarm run's shared, temporary memory.

Each agent has its own conversation; the only way information passes between
agents is through this store. Everything is kept in memory and mirrored to a
folder so large results stay out of prompts and a run can be reopened::

    <workspace storage>/runs/<run_id>/
        plan.json          the SwarmPlan
        <agent_id>.md      each agent's full result
        notes/<name>.md    scratch notes agents leave for each other
        events.jsonl       start / finish events per agent

Agents reach it through three scratch tools that can only touch this folder.
Old runs are removed: anything older than 7 days, and all but the newest 20.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from velune.swarm.types import AgentResult, SwarmPlan
from velune.tools.base.tool import BaseTool

logger = logging.getLogger("velune.swarm.store")

KEEP_RUNS = 20
KEEP_DAYS = 7
SUMMARY_CHARS = 1200  # ~300 tokens
_NOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def summarize(text: str, limit: int = SUMMARY_CHARS) -> str:
    """The first *limit* characters, cut at a line break when one is near."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    cut = cut if cut > limit // 2 else limit
    return text[:cut].rstrip() + "\n… (full result available with swarm_result_read)"


class RunStore:
    """Shared memory for one swarm run."""

    def __init__(self, folder: Path, plan: SwarmPlan, run_id: str) -> None:
        self.folder = folder
        self.plan = plan
        self.run_id = run_id
        self.results: dict[str, AgentResult] = {}
        self.notes: dict[str, str] = {}
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def create(cls, runs_root: Path, plan: SwarmPlan) -> RunStore:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        prune_runs(runs_root)
        return cls(runs_root / run_id, plan, run_id)

    # ── results ──────────────────────────────────────────────────────────

    def record(self, result: AgentResult) -> AgentResult:
        if not result.summary:
            result.summary = summarize(result.content or result.error)
        self.results[result.agent_id] = result
        body = result.content or f"(no answer: {result.status}: {result.error})"
        self._write(f"{result.agent_id}.md", body)
        self.event("finish", result.agent_id, status=result.status, error=result.error)
        return result

    def result(self, agent_id: str) -> AgentResult | None:
        return self.results.get(agent_id)

    def earlier_results(self, stage: int) -> list[AgentResult]:
        """Results of every agent in stages before *stage*, in plan order."""
        earlier = [a.id for a in self.plan.agents if a.stage < stage]
        return [self.results[i] for i in earlier if i in self.results]

    # ── notes ────────────────────────────────────────────────────────────

    def write_note(self, name: str, text: str) -> str:
        if not _NOTE_NAME.match(name):
            raise ValueError("a note name is letters, digits, '-' and '_' (max 64)")
        self.notes[name] = text
        self._write(f"notes/{name}.md", text)
        return f"Saved note '{name}' ({len(text)} characters)."

    def read_note(self, name: str) -> str:
        if name not in self.notes:
            have = ", ".join(sorted(self.notes)) or "none yet"
            raise LookupError(f"no note named '{name}' (notes: {have})")
        return self.notes[name]

    # ── events and files ─────────────────────────────────────────────────

    def event(self, kind: str, agent_id: str = "", **data: Any) -> None:
        line = json.dumps({"t": round(time.time(), 3), "event": kind, "agent": agent_id, **data})
        try:
            with (self.folder / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:  # the store is a convenience; never fail a run on it
            logger.debug("Could not write swarm event: %s", exc)

    def _write(self, rel: str, text: str) -> None:
        path = (self.folder / rel).resolve()
        if not path.is_relative_to(self.folder.resolve()):
            raise ValueError("path leaves the run folder")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not write %s: %s", path, exc)

    # ── tools for agents ─────────────────────────────────────────────────

    def scratch_tools(self, agent_id: str) -> ScratchTools:
        return ScratchTools(self, agent_id)


def prune_runs(runs_root: Path, *, keep: int = KEEP_RUNS, days: int = KEEP_DAYS) -> None:
    """Delete runs older than *days*, and all but the newest *keep*."""
    if not runs_root.is_dir():
        return
    runs = sorted((p for p in runs_root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - days * 86400
    for index, run in enumerate(runs):
        if run.stat().st_mtime < cutoff or index < len(runs) - keep:
            shutil.rmtree(run, ignore_errors=True)


# ── scratch tools ────────────────────────────────────────────────────────────


class _ScratchTool(BaseTool):
    """A tool that only reads or writes this run's store (no workspace access)."""

    name = ""
    description = ""
    schema: dict[str, Any] = {}

    def __init__(self, store: RunStore, agent_id: str) -> None:
        self._store = store
        self._agent_id = agent_id

    def get_name(self) -> str:
        return self.name

    def get_description(self) -> str:
        return self.description

    def get_schema(self) -> dict[str, Any]:
        return self.schema


def _string_args(*names: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {n: {"type": "string"} for n in names},
        "required": list(names),
    }


class NoteWrite(_ScratchTool):
    name = "swarm_note_write"
    description = (
        "Save a scratch note other agents in this task can read (for example a list of "
        "files found, or a decision). Overwrites a note with the same name."
    )
    schema = _string_args("name", "text")

    async def execute(self, name: str = "", text: str = "", **_: Any) -> str:
        self._store.event("note", self._agent_id, name=name)
        return self._store.write_note(name, text)


class NoteRead(_ScratchTool):
    name = "swarm_note_read"
    description = "Read a scratch note saved by an agent in this task."
    schema = _string_args("name")

    async def execute(self, name: str = "", **_: Any) -> str:
        return self._store.read_note(name)


class ResultRead(_ScratchTool):
    name = "swarm_result_read"
    description = (
        "Read the full result of an earlier agent in this task, when the summary you "
        "were given is not enough. Pass the agent's id."
    )
    schema = _string_args("agent_id")

    async def execute(self, agent_id: str = "", **_: Any) -> str:
        result = self._store.result(agent_id)
        if result is None:
            done = ", ".join(self._store.results) or "none yet"
            raise LookupError(f"no finished agent '{agent_id}' (finished: {done})")
        return result.content or f"{agent_id} produced no answer: {result.error}"


class ScratchTools:
    """``DynamicToolSource`` giving one agent the three scratch tools."""

    version = 1

    def __init__(self, store: RunStore, agent_id: str) -> None:
        self._tools = [cls(store, agent_id) for cls in (NoteWrite, NoteRead, ResultRead)]

    def tools(self) -> list[BaseTool]:
        return list(self._tools)

    def unknown_tool_hint(self, name: str) -> str | None:
        return None
