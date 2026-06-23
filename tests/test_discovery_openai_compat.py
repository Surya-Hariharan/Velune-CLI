"""Tests for the generic OpenAI-compatible local discovery (Rule 7).

These run without a network dependency: probing is pointed at a port that is
(almost certainly) closed, so ``is_running``/``discover`` resolve to falsy/empty,
and ``_parse_model`` is exercised directly as a pure function.
"""

from __future__ import annotations

import asyncio

import velune.providers.discovery.openai_compat as oc
from velune.providers.discovery.openai_compat import OpenAICompatDiscovery


def test_is_running_false_when_no_server(monkeypatch) -> None:
    # Point discovery at a single, almost-certainly-closed port.
    monkeypatch.setattr(oc, "_CANDIDATE_PORTS", (59999,))
    assert asyncio.run(OpenAICompatDiscovery.is_running()) is False


def test_discover_empty_when_no_server(monkeypatch) -> None:
    monkeypatch.setattr(oc, "_CANDIDATE_PORTS", (59999,))
    models = asyncio.run(OpenAICompatDiscovery().discover())
    assert models == []


def test_parse_model_records_base_url() -> None:
    disc = OpenAICompatDiscovery()
    descriptor = disc._parse_model({"id": "my-local-model"}, "http://localhost:8000/v1")
    assert descriptor is not None
    assert descriptor.model_id == "my-local-model"
    assert descriptor.provider_id == "openai-compat"
    assert descriptor.is_local is True
    assert descriptor.metadata["base_url"] == "http://localhost:8000/v1"
    assert "openai-compat" in descriptor.tags


def test_parse_model_skips_entries_without_id() -> None:
    disc = OpenAICompatDiscovery()
    assert disc._parse_model({}, "http://localhost:8000/v1") is None
