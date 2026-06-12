# Architecture

## Overview

Velune is organized into distinct layers with clear separation of concerns:

```
┌─────────────────────────────────────────────────┐
│ velune/cli/                 (UI Layer)           │
│ Commands, REPL, output formatting               │
├─────────────────────────────────────────────────┤
│ velune/cognition/           (Application Logic)  │
│ Agents, orchestration, decision-making          │
├─────────────────────────────────────────────────┤
│ velune/retrieval/           (Retrieval Layer)    │
│ Search, ranking, document handling              │
├─────────────────────────────────────────────────┤
│ velune/memory/              (Infrastructure)    │
│ Storage tiers, lifecycle management             │
│                                                 │
│ velune/providers/           (Infrastructure)    │
│ LLM providers, routing, health monitoring       │
│                                                 │
│ velune/kernel/              (Infrastructure)    │
│ Config, registry, lifecycle coordination        │
├─────────────────────────────────────────────────┤
│ velune/telemetry/           (Observability)     │
│ Logging, tracing, usage analytics               │
└─────────────────────────────────────────────────┘
```

## Layer Boundaries

### CLI Layer (`velune/cli/`)
- Commands (REPL, init, setup, doctor)
- Output formatting and display
- User input handling
- Terminal UI components

**Cannot import from:** Application logic or below

### Application Logic (`velune/cognition/`)
- Council agents (Planner, Coder, Reviewer, etc.)
- Orchestration and coordination
- Decision-making algorithms
- Task execution strategies

**Can import from:** Memory, Providers, Kernel, Telemetry
**Cannot import from:** CLI

### Retrieval Layer (`velune/retrieval/`)
- Full-text search (BM25)
- Semantic search (embeddings)
- Document ranking
- Context assembly

**Can import from:** Memory, Providers, Kernel, Telemetry
**Cannot import from:** CLI, Cognition

### Memory Layer (`velune/memory/`)
- Working memory (session state)
- Episodic memory (history)
- Semantic memory (embeddings)
- Graph memory (relationships)
- Lineage memory (decisions)

**Can import from:** Kernel, Telemetry, Core types
**Cannot import from:** CLI, Cognition, Retrieval

### Providers Layer (`velune/providers/`)
- Cloud provider adapters (OpenAI, Anthropic, Groq, etc.)
- Local provider adapters (Ollama, LM Studio, llama.cpp)
- Health monitoring and routing
- Rate limiting and caching

**Can import from:** Kernel, Telemetry, Core types
**Cannot import from:** CLI, Cognition, Retrieval, Memory

### Kernel (`velune/kernel/`)
- Configuration management
- Service container (dependency injection)
- Lifecycle coordination (startup/shutdown)
- Bootstrap process
- Module registration

**Can import from:** Core types, Telemetry
**Cannot import from:** Any higher layer

### Core Types (`velune/core/types/`)
- Provider types (ProviderHealth, CapabilityManifest)
- Inference types (InferenceRequest, InferenceResponse)
- Model types (ModelDescriptor, CapabilityLevel)

**No imports from Velune modules** - only standard library and 3rd party

### Telemetry (`velune/telemetry/`)
- Structured logging
- Span tracing
- Usage tracking
- Metrics collection

**Can import from:** Core types, Kernel (for context access)
**Cannot import from:** CLI, Cognition, Memory, Providers

## Data Flow

### Initialization
```
velune init
   ↓
velune/kernel/bootstrap.py (RuntimeBootstrapper)
   ↓
Register all modules (providers, memory, config)
   ↓
velune/kernel/lifecycle.py (LifecycleCoordinator)
   ↓
Start background services (health monitor, background tasks)
```

### Inference Request
```
User input (CLI)
   ↓
velune/cli/repl.py
   ↓
velune/cognition/ (Council agents)
   ↓
velune/retrieval/ (Fetch context)
   ↓
velune/memory/ (Store history)
   ↓
velune/providers/router.py (Select model)
   ↓
velune/providers/adapters/* (Execute)
   ↓
Response → Memory → CLI output
```

### Health Monitoring
```
velune/providers/health_monitor.py (Background)
   ↓
Every 30 seconds:
- Poll each provider's health_check()
- Track latency (rolling 5-call average)
- Update CapabilityManifest
   ↓
velune/providers/router.py (Health-aware routing)
   ↓
Prefer healthy providers
```

## Key Components

### Service Container
Centralized dependency injection via `velune/kernel/registry.py`:

```python
from velune.kernel.registry import get_container

container = get_container()
router = container.get("runtime.provider_router")
monitor = container.get("runtime.provider_health_monitor")
```

### Provider Registry
All LLM providers registered in `velune/providers/registry.py`:

- Lazy initialization (factories, not singletons)
- Dynamic provider discovery
- Key management via OS keyring

### Memory System
Five-tier memory architecture in `velune/memory/`:

1. **Working Memory** - Session state (fast, volatile)
2. **Episodic Memory** - Conversation history (SQLite, persistent)
3. **Semantic Memory** - Embeddings (Qdrant, searchable)
4. **Graph Memory** - Relationships (Neo4j-like SQLite, queryable)
5. **Lineage Memory** - Decision history (SQLite, auditable)

### Health Monitoring
Real-time provider status in `velune/providers/health_monitor.py`:

- Continuous polling (30-second interval)
- Manifest tracking (health, latency, rate limits)
- Regression detection (3+ consecutive failures)
- Router integration (health-aware selection)

## Extension Points

### Adding a Provider
1. Create adapter in `velune/providers/adapters/your_provider.py`
2. Implement `ModelProvider` protocol:
   - `provider_id` property
   - `list_models()` → list[ModelDescriptor]
   - `infer()` → InferenceResponse
   - `stream()` → AsyncIterator[StreamChunk]
   - `health_check()` → ProviderHealth
3. Register in `velune/providers/registry.py`
4. Add to provider table in README.md

### Adding a Memory Tier
1. Create tier in `velune/memory/tiers/your_tier.py`
2. Implement lifecycle methods
3. Register in `velune/memory/module.py`
4. Update `MemoryLifecycleManager`

### Adding a CLI Command
1. Create command in `velune/cli/commands/your_command.py`
2. Register in `velune/cli/app.py`
3. Add to command table in README.md

### Adding an Agent
1. Create agent in `velune/cognition/agents/your_agent.py`
2. Implement agent protocol
3. Register in `velune/cognition/council_orchestrator.py`

## Dependency Graph

```
Core Types (no dependencies)
   ↑
   ├─ Kernel (config, registry, lifecycle)
   ├─ Telemetry (logging, tracing)
   │
   ├─ Providers (adapters, routing, health)
   │   ├─ Kernel
   │   └─ Telemetry
   │
   ├─ Memory (storage, tiers)
   │   ├─ Kernel
   │   └─ Telemetry
   │
   ├─ Retrieval (search, ranking)
   │   ├─ Memory
   │   ├─ Providers
   │   ├─ Kernel
   │   └─ Telemetry
   │
   ├─ Cognition (agents, orchestration)
   │   ├─ Retrieval
   │   ├─ Memory
   │   ├─ Providers
   │   ├─ Kernel
   │   └─ Telemetry
   │
   └─ CLI (commands, REPL, UI)
       ├─ Cognition
       ├─ Retrieval
       ├─ Memory
       ├─ Providers
       ├─ Kernel
       └─ Telemetry
```

## Code Organization

```
velune/
├── __init__.py              # Version, exports
├── main.py                  # Entry point (CLI app)
├── core/
│   └── types/               # Type definitions (no code dependencies)
│       ├── model.py
│       ├── provider.py
│       ├── inference.py
│       └── ...
├── kernel/                  # Infrastructure layer
│   ├── bootstrap.py         # Module initialization
│   ├── registry.py          # Service container
│   ├── lifecycle.py         # Startup/shutdown coordination
│   └── config.py            # Configuration management
├── providers/               # Provider adapters + routing
│   ├── registry.py
│   ├── router.py
│   ├── health_monitor.py
│   ├── adapters/
│   │   ├── openai.py
│   │   ├── anthropic.py
│   │   └── ...
│   └── discovery/
│       ├── openai.py
│       └── ...
├── memory/                  # Storage + memory tiers
│   ├── tiers/
│   │   ├── working.py
│   │   ├── episodic.py
│   │   └── ...
│   └── storage/
│       ├── sqlite_pool.py
│       └── lancedb_store.py
├── retrieval/               # Search + ranking
│   ├── hybrid.py
│   └── ranking.py
├── cognition/               # Agents + orchestration
│   ├── agents/
│   │   ├── planner.py
│   │   ├── coder.py
│   │   └── ...
│   └── council_orchestrator.py
├── cli/                     # Commands + REPL
│   ├── app.py
│   ├── repl.py
│   ├── commands/
│   │   ├── init.py
│   │   ├── setup.py
│   │   └── ...
│   └── model_selector.py
└── telemetry/               # Logging + observability
    ├── logging.py
    ├── spans.py
    ├── usage_tracker.py
    └── doctor.py
```

## Design Principles

1. **Clear Layering** - Dependencies flow downward only
2. **Composition over Inheritance** - Prefer protocols and delegation
3. **Async-First** - All I/O is async, single `asyncio.run()` entry point
4. **Type-Safe** - Comprehensive type hints, pyright in standard mode
5. **Observable** - Structured logging, spans, and metrics throughout
6. **Testable** - Isolated layers, mocks for external services
7. **Documented** - Code explains intent, not just what it does

## Further Reading

- [CI/CD Pipeline](../CI_CD_SETUP.md) - Testing and deployment
- [CONTRIBUTING.md](../CONTRIBUTING.md) - Developer workflow
- [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) - Community guidelines
