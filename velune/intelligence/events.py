"""Typed event constants and factory helpers for repository intelligence events.

All events ride the existing CognitiveBus (velune/events.py) and follow its
Event schema.  This module defines the canonical type strings so subscribers
can use them without magic strings, and provides lightweight factory functions
so the engine does not construct raw dicts by hand.

Subscribers can use wildcards::

    await bus.subscribe("repository.*", handler)      # all repo events
    await bus.subscribe("repository.files_changed", handler)  # specific
"""

from __future__ import annotations

import time
from typing import Any

from velune._compat import StrEnum
from velune.events import Event

_SOURCE = "repository.intelligence_engine"


class RepositoryEventType(StrEnum):
    """Canonical event type strings for all repository intelligence events."""

    # Fired when the incremental indexer detects at least one changed file.
    FILES_CHANGED = "repository.files_changed"

    # Fired after IndexState is successfully updated on disk.
    INDEX_UPDATED = "repository.index_updated"

    # Fired after the KnowledgeGraph is surgically patched for a delta.
    KNOWLEDGE_GRAPH_PATCHED = "repository.knowledge_graph_patched"

    # Fired when the git branch, HEAD SHA, or uncommitted-file count changes.
    GIT_STATE_CHANGED = "repository.git_state_changed"

    # Fired after a quick_summary/profile refresh completes.
    PROFILE_REFRESHED = "repository.profile_refreshed"

    # Fired after the pipeline cache (import graph, API map, architecture/tech
    # summary) has been incrementally refreshed for a delta, off the
    # interactive prompt path.
    PIPELINE_REFRESHED = "repository.pipeline_refreshed"

    # Lifecycle events.
    ENGINE_STARTED = "repository.engine_started"
    ENGINE_STOPPED = "repository.engine_stopped"


# ---------------------------------------------------------------------------
# Factory helpers — keep payloads small and well-typed
# ---------------------------------------------------------------------------


def make_files_changed(
    added: list[str],
    updated: list[str],
    removed: list[str],
) -> Event:
    return Event(
        event_type=RepositoryEventType.FILES_CHANGED,
        source=_SOURCE,
        data={
            "added": added,
            "updated": updated,
            "removed": removed,
            "total": len(added) + len(updated) + len(removed),
        },
    )


def make_index_updated(commit_sha: str | None) -> Event:
    return Event(
        event_type=RepositoryEventType.INDEX_UPDATED,
        source=_SOURCE,
        data={"commit_sha": commit_sha, "indexed_at": time.time()},
    )


def make_knowledge_graph_patched(
    nodes_added: int,
    nodes_removed: int,
    edges_added: int,
) -> Event:
    return Event(
        event_type=RepositoryEventType.KNOWLEDGE_GRAPH_PATCHED,
        source=_SOURCE,
        data={
            "nodes_added": nodes_added,
            "nodes_removed": nodes_removed,
            "edges_added": edges_added,
        },
    )


def make_git_state_changed(
    branch: str | None,
    commit_sha: str | None,
    uncommitted_files: int,
    changed: list[str],
) -> Event:
    return Event(
        event_type=RepositoryEventType.GIT_STATE_CHANGED,
        source=_SOURCE,
        data={
            "branch": branch,
            "commit_sha": commit_sha,
            "uncommitted_files": uncommitted_files,
            "changed": changed,
        },
    )


def make_profile_refreshed(profile: dict[str, Any]) -> Event:
    return Event(
        event_type=RepositoryEventType.PROFILE_REFRESHED,
        source=_SOURCE,
        data=profile,
    )


def make_pipeline_refreshed(
    files_recomputed: int,
    edge_count: int,
    route_count: int,
) -> Event:
    return Event(
        event_type=RepositoryEventType.PIPELINE_REFRESHED,
        source=_SOURCE,
        data={
            "files_recomputed": files_recomputed,
            "edge_count": edge_count,
            "route_count": route_count,
            "refreshed_at": time.time(),
        },
    )


def make_engine_started(workspace: str) -> Event:
    return Event(
        event_type=RepositoryEventType.ENGINE_STARTED,
        source=_SOURCE,
        data={"workspace": workspace},
    )


def make_engine_stopped(workspace: str) -> Event:
    return Event(
        event_type=RepositoryEventType.ENGINE_STOPPED,
        source=_SOURCE,
        data={"workspace": workspace},
    )
