"""`/doctor`'s memory-embedding diagnostic (Part 13 of the Groq/memory audit).

Regression coverage: before this check existed, there was no single place
that told you which provider/model memory actually embeds with, so a user
seeing an unrelated Groq chat failure had no way to tell "memory embedding is
also failing" apart as its own, independently-diagnosable problem.
"""

from __future__ import annotations

from unittest.mock import patch

from velune.cli.commands.doctor import _check_memory_embedding


def test_reports_ok_when_ollama_is_reachable():
    with patch("httpx.get") as mock_get:
        mock_get.return_value.status_code = 200
        result = _check_memory_embedding()

    assert result["status"] == "ok"
    assert "ollama" in result["message"]
    assert "nomic-embed-text" in result["message"]


def test_reports_warn_when_ollama_is_unreachable():
    with patch("httpx.get", side_effect=ConnectionError("refused")):
        result = _check_memory_embedding()

    assert result["status"] == "warn"
    assert "ollama" in result["message"]
    assert "chat inference is unaffected" in result["message"].lower()


def test_never_names_the_active_chat_provider():
    """The embedding provider identity must be independent of whichever
    chat provider (Groq, OpenAI, ...) happens to be active."""
    with patch("httpx.get") as mock_get:
        mock_get.return_value.status_code = 200
        result = _check_memory_embedding()

    for chat_provider in ("groq", "openai", "anthropic"):
        assert chat_provider not in result["message"].lower()
