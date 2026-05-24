# ---
# title: "Cognitive Architecture"
# description: "Cognition, retrieval, and orchestration boundaries for Velune."
# ---

# Cognitive Architecture

This document captures the current cognition, retrieval, and orchestration
boundaries used by Velune.

## Cognitive Layers

- Working memory keeps the current task and live execution state in process.
- Episodic memory records task outcomes, decisions, and conversational history.
- Semantic memory stores distilled facts and architectural knowledge.
- Procedural memory stores successful workflows and reusable playbooks.
- Graph memory stores entities and relationships across files, tasks, and conversations.

## Context Management

Velune assembles context by ranking task evidence, retrieved evidence, and workspace evidence against a token budget. Lower-value content is compressed or evicted before it reaches orchestration.

## Retrieval Pipeline

1. Normalize the query into search-ready terms.
2. Retrieve candidates through lexical, vector, and graph paths.
3. Fuse the candidates into a single ranking with provenance.
4. Pass the top results into context assembly and compression.

## Repository Cognition

Repository cognition indexes files, symbols, imports, and dependency edges so the runtime can reason about code structure instead of treating the repo as plain text.

## Orchestration Boundary

Velune keeps orchestration behind explicit contracts so LangGraph or a comparable runtime can be attached later without rewriting the CLI or intelligence stack.

## Execution Policy

- Read-only operations remain immediate.
- Mutating operations remain snapshot-aware.
- Validation remains staged: syntax, type, lint, and then targeted runtime checks.

# Current Foundation Status

- Model discovery and registry are live.
- Repository cognition is live.
- Local-first retrieval and graph memory are live.
- Lifecycle management is live.
- Full autonomous orchestration is intentionally still a future boundary.

---
License: MIT
Copyright © 2026 Velune Contributors