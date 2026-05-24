# ---
# title: "Velune CLI"
# description: "Terminal-first cognitive operating layer for repository-aware engineering workflows."
# ---

# Velune CLI

Velune CLI is a terminal-first cognitive operating layer for repository-aware
engineering workflows.

## What It Does Now

- `velune models scan` discovers local Ollama, LM Studio, GGUF, and Hugging Face model surfaces.
- `velune models list` shows the discovered registry with derived specialization and capability data.
- `velune ask` provides the orchestration boundary for future intent routing.
- `velune memory stats` reports the memory lifecycle configuration.
- `velune workspace init` and `velune workspace status` manage the local workspace container.

## Current Architecture

- Model discovery and registry live in `velune/models/discovery`.
- Repository cognition lives in `velune/repository/cognition`.
- Retrieval lives in `velune/retrieval` with local-first vector and lexical stores.
- Graph memory and lifecycle policy live in `velune/memory/graph` and `velune/memory/lifecycle`.
- The runtime container wires all subsystems through `velune/core/runtime.py`.

See [docs/intelligence-foundation.md](docs/intelligence-foundation.md) and
[docs/cognitive-architecture.md](docs/cognitive-architecture.md) for the subsystem
design.

## Installation

```bash
pip install -e .[dev]
```

## Usage

```bash
velune --help
velune models scan
velune models list
velune ask "Analyze this repository"
velune memory stats
velune workspace status
```

## Configuration

Edit `velune.toml` in the workspace root to configure provider endpoints, telemetry, retrieval limits, and safety defaults. For provider keys, copy [.env.example](.env.example).

## Project Structure

```
velune/
├── cli/             # Typer CLI and command routing
├── core/            # Runtime, configuration, logging, and shared contracts
├── models/          # Model discovery, registry, and classification
├── repository/      # Repository cognition and graph building
├── retrieval/       # Hybrid retrieval and local-first indexes
├── memory/          # Graph memory and lifecycle policy
├── orchestration/   # Future LangGraph-ready execution contracts
└── providers/       # Provider abstraction layer
```

## Development

```bash
ruff check .
black --check .
python -m compileall velune
```

## License

MIT

---
License: MIT
Copyright © 2026 Velune Contributors
