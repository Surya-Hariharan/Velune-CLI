# Velune CLI

Cognitive AI CLI for autonomous software engineering.

## Overview

Velune is a sophisticated AI-powered CLI designed for autonomous software engineering tasks. It features a multi-layered cognitive architecture with memory systems, hybrid retrieval, repository cognition, and event-driven processing.

## Architecture

### Completed Components

- **Core System**: Types, configuration, error handling, dependency injection
- **Provider Abstraction**: Unified interface for OpenAI, Anthropic, Ollama, and local models
- **Model Registry**: Capability-based model discovery, routing, and assignment
- **Memory Architecture**: hierarchical working, episodic, semantic, procedural, and graph tiers with consolidation and pruning rules
- **Context Management**: token budgeting, prioritization, compression, and reconstruction
- **Hybrid Retrieval**: vector search, BM25 lexical search, graph traversal, RRF fusion, and reranking
- **Repository Cognition**: Tree-sitter AST parsing, semantic chunking, dependency graphs
- **Tool System**: Filesystem, Git, terminal, code search, web fetch tools
- **Workspace Cognition**: State machine, live cognitive model, git/terminal/environment awareness
- **Event System**: Async event bus, typed events, handlers for memory/cognition/indexing
- **CLI Interface**: Typer-based commands with Rich terminal display

See [docs/cognitive-architecture.md](docs/cognitive-architecture.md) for the detailed memory, context, and retrieval architecture.

### Remaining Components

- Agent system with protocol-based communication
- Orchestration engine with LangGraph integration
- Execution pipeline with sandboxing and rollback
- Intent reconstruction pipeline

## Installation

```bash
pip install -e .
```

## Usage

### Initialize Workspace

```bash
velune workspace init
```

### Run Tasks

```bash
velune run "Fix the bug in the authentication module"
```

### Model Management

```bash
velune models scan
velune models list
velune models assign planner gpt-4
```

### Memory Inspection

```bash
velune memory inspect --type episodic
velune memory clear working
```

### Workspace Status

```bash
velune workspace status
```

## Configuration

Edit `velune.toml` in your workspace root to configure providers, memory settings, context limits, and execution safety.

## Project Structure

```
velune/
├── cli/                    # Typer-based CLI
├── core/                   # Foundational primitives
├── providers/              # Model provider abstraction
├── models/                 # Model registry and routing
├── memory/                 # Cognitive memory systems
├── context/                # Context management
├── retrieval/              # Hybrid retrieval
├── repository/             # Repository cognition
├── workspace/              # Workspace cognition
├── events/                 # Event-driven system
└── tools/                  # Tool system
```

## License

MIT
