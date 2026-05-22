# Cognitive Architecture

This document captures the memory, context, and retrieval layers used by Velune.

## 5. Hierarchical Cognitive Memory Architecture

Memory is organized in five tiers, each with its own persistence model, retrieval characteristics, and lifecycle.

### Tier 0 - Working Memory

- Storage: Python heap via Pydantic objects
- Contents: current task state, active agent messages, live tool results, immediate conversation turns
- Capacity: bounded by the context window budget, typically 4-8K tokens of dense context
- Lifecycle: created on task start, destroyed on task completion or timeout
- Access: direct Python object references
- Purpose: avoids redundant retrieval during active task execution

### Tier 1 - Episodic Memory

- Storage: SQLite with FTS5 extension
- Contents: conversation turns, task outcomes, debugging sessions, decisions made, errors encountered
- Capacity: unbounded, then pruned by importance score and age
- Lifecycle: created on session events, consolidated after task completion
- Access: temporal query, semantic query, entity-filtered query
- Purpose: answers questions like "what happened yesterday with the auth issue?"

### Tier 2 - Semantic Memory

- Storage: ChromaDB, or Qdrant in production
- Contents: extracted facts about the codebase, learned architectural patterns, domain knowledge distilled from episodic memory
- Capacity: unbounded, deduplicated by semantic similarity
- Lifecycle: populated during consolidation from episodic events; updated when new evidence contradicts existing facts
- Access: semantic similarity search
- Purpose: answers questions like "what is the authentication architecture in this project?"

### Tier 3 - Procedural Memory

- Storage: SQLite plus vector index
- Contents: successful task execution patterns, proven debugging strategies, workflow templates extracted from completed tasks
- Capacity: bounded, keeping top-N patterns per task category scored by success rate
- Lifecycle: learned from successful task completions; decays on failures
- Access: task-similarity search
- Purpose: answers questions like "how did we successfully fix a similar database migration issue?"

### Tier 4 - Graph Memory

- Storage: Graphiti with a Neo4j-compatible graph store
- Contents: entity relationship graph for files, functions, developers, decisions, bugs, tasks, concepts, and temporal edges
- Capacity: unbounded, with importance-based pruning of low-degree nodes
- Lifecycle: built continuously from all other memory tiers via entity extraction
- Access: graph traversal, entity-centered queries, relationship path queries
- Purpose: answers questions like "which files are affected by changes to the auth module?" and "who last worked on this subsystem?"

### Consolidation Pipeline

When a task completes, working memory is snapshotted and fanned out into the persistent tiers:

1. Event extraction feeds episodic memory with conversation turns, actions, and observed results.
2. Fact extraction feeds semantic memory with deduplicated architectural facts and decisions.
3. Pattern extraction feeds procedural memory when the task succeeds, producing a workflow template.
4. Entity extraction feeds graph memory with files touched, functions modified, errors seen, and relationships created.
5. Importance scoring tags new records for pruning, archiving, or retention.

### Importance Scoring

Importance is computed as:

$$
importance(record) = base\_score \times recency\_weight(age\_days) \times access\_frequency\_weight(access\_count) \times task\_relevance\_weight(current\_task\_embedding, record\_embedding) \times specificity\_weight(record.specificity)
$$

with temporal decay modeled as:

$$
recency\_weight(t) = e^{-\lambda t}
$$

where $\lambda \approx 0.05 / day$.

Records below importance 0.1 after 30 days are archived. Records below 0.05 are deleted.

## 6. Context Management and Prioritization

The context window manager allocates token budget before each inference call.

### Priority Tiers

- P0 Identity: system prompt, role definition, task contract, 5%, never evicted
- P1 Task: current task definition, active plan steps, 10%, evicted on task change
- P2 Direct Code: files directly mentioned or being edited, 30%, evicted on relevance drop
- P3 Retrieved: RAG-retrieved code, docs, and memory, 25%, evicted by reranker score
- P4 Episodic: relevant past task summaries, 15%, evicted by recency
- P5 Graph: graph-traversal context, 10%, evicted by graph distance
- P6 Background: workspace summary and recent git activity, 5%, lowest priority

### Priority Scoring

Each context chunk receives a composite relevance score using semantic similarity, recency, graph proximity, explicit references, and access frequency.

### Compression Rules

- P0 to P2 are never summarized by the compressor.
- P2 code chunks are truncated with head and tail preserved rather than summarized.
- P3 to P6 chunks below threshold are evicted.
- Near-threshold chunks are LLM-compressed to roughly 30% of original length.
- The compressor rescans and repeats until the budget is satisfied.

## 7. Hybrid Retrieval Architecture

Retrieval is organized as a multi-path pipeline:

1. Query analysis extracts entities, temporal expressions, intent, and query expansion terms.
2. Parallel retrieval runs three paths at once:
   - BM25 lexical search over code, docs, and memory
   - Dense vector search against embedded chunks
   - Graph traversal from focal entities with depth 2-3
3. Reciprocal Rank Fusion combines the results with $k = 60$.
4. A cross-encoder reranker scores the top candidates and keeps the best matches.
5. The context assembler applies priority tiers and compression to fit the final window.

### AST-Aware Chunking

Before retrieval, the repository is chunked to preserve semantic coherence:

- Functions and methods stay intact as one chunk.
- Classes are split into a header chunk plus separate method chunks.
- Import blocks are stored as complete import sections.
- Configuration files are chunked by logical sections such as TOML tables or YAML keys.
- Documentation is chunked at paragraph level.
- Long functions over 300 lines may be split at logical block boundaries with overlap preserved.

Each chunk carries metadata for file path, line range, language, symbol name, parent symbol, chunk type, and AST node type.

## 9. Intent Reconstruction System

The intent reconstruction system turns vague, context-dependent prompts into precise task specifications.

### Reconstruction Pipeline

1. Temporal parsing resolves phrases like "yesterday" into a concrete date range.
2. Entity extraction maps user terms like "auth" to related concepts such as authentication, authorization, login, JWT, and session.
3. Multi-source evidence gathering runs in parallel across episodic memory, git history, file system changes, terminal history, and semantic memory.
4. Evidence fusion cross-references all signals and builds an evidence graph.
5. Hypothesis generation produces candidate fixes with confidence scores.
6. Confidence gating decides whether to proceed or ask for clarification.
7. Task specification emits a structured TaskSpec with primary files, context files, relevant commits, prior session references, a description, and confidence.

### Example Outcome

For a prompt like "fix the auth issue from yesterday", the system should resolve a likely bug fix such as incomplete JWT token refresh logic, while keeping alternate hypotheses available when confidence is lower.

## 10. Multi-Agent Orchestration Engine

The orchestration engine is a dynamic runtime that builds task-specific execution graphs at runtime rather than relying on rigid, predeclared flows.

### Communication Model

All inter-agent communication uses typed AgentMessage objects routed through the orchestration engine. Agents do not call each other directly.

### Orchestration Flow

1. The intent reconstructor emits a TaskSpec.
2. The supervisor initializes the execution pipeline.
3. A retriever agent gathers relevant repository, memory, and git context.
4. A planner agent decomposes the task into ordered and parallelizable steps.
5. A reasoner agent validates the plan for correctness and risk.
6. A coder agent executes the patching steps.
7. An execution validator runs syntax, lint, and test checks.
8. A reviewer agent audits quality, regression risk, and security concerns.
9. The supervisor synthesizes the result.
10. The memory consolidator records the outcome across episodic, semantic, procedural, and graph memory.

### Supervisor Responsibilities

- Detect stalls such as agent timeouts and circular reasoning.
- Re-prompt stuck agents with more context or escalate to stronger models.
- Replan when outputs differ from expectations.
- Escalate to the user when confidence falls below threshold or destructive actions are proposed.
- Track token budget across agents and request context compression when needed.

## 11. Autonomous Planning System

The planner decomposes high-level goals into layered research, design, implementation, and validation work.

### Hierarchical Decomposition

1. Level 1 splits the goal into research, design, implement, and validate phases.
2. Level 2 expands each phase into concrete tool calls, code edits, and checks.
3. Each execution result feeds back into the planner for continuation, replanning, retry, rollback, or escalation.

### Feedback Loops

- Success with expected results continues the plan.
- Success with unexpected results pauses execution and replans the remaining steps.
- Recoverable failures are retried with a modified approach.
- Unrecoverable failures roll back to the last snapshot and escalate.
- Missing information spawns a retriever agent and then resumes.

## 12. Execution and Validation Pipeline

Before any file write or command execution, the system captures a pre-execution snapshot.

### Snapshot Policy

- File modifications trigger a snapshot of the current workspace state.
- Command execution captures environment and process state.
- The snapshot is used for rollback, debugging, and post-task consolidation.

### Safety Levels

- SAFE: read-only operations execute immediately.
- LOW_RISK: writing new files executes with snapshot.
- MEDIUM_RISK: modifying existing files executes with snapshot and diff preview.
- HIGH_RISK: deleting files or modifying core config requires explicit confirmation.
- CRITICAL: database migrations and infrastructure changes require explicit confirmation and a dry-run first.

## Post-Execution Validation Chain

After a file is modified, the system runs a staged validation chain before it treats the change as complete:

1. Syntax check runs first. A parse failure triggers immediate rollback.
2. Type check runs next when type tooling is configured, such as mypy or pyright.
3. Lint check runs next when lint tooling is configured, such as ruff or eslint.
4. Test run targets the affected tests, such as pytest or jest.
5. Integration validation is optional and may include a docker-compose smoke test.
6. If all checks pass, the system commits the change to git with a task-specific message.
7. If any check fails, the coder agent receives the failure output and retries.
8. If retries are exhausted, the system rolls back to the pre-execution snapshot.

## 13. Event-Driven Cognition System

The event-driven cognition system converts workspace, process, agent, and session activity into incremental cognitive updates.

### Event Categories

- Workspace Events: FileCreated, FileModified, FileDeleted, DirectoryRenamed, GitCommitCreated, GitBranchChanged, GitMergeCompleted, GitConflictDetected
- Process Events: CommandExecuted, CommandFailed, TestSuiteRan, TestFailed, BuildStarted, BuildCompleted, BuildFailed
- Agent Events: TaskStarted, TaskCompleted, TaskFailed, AgentStarted, AgentStalled, PlanUpdated, MemoryConsolidated
- Session Events: SessionStarted, SessionResumed, UserPromptReceived, ClarificationRequested

### Event Processing Pipeline

When a file is modified, the event handler should:

1. Trigger incremental repository re-indexing for the changed file.
2. Invalidate cached context chunks for that file.
3. Update the workspace cognition model with the new file activity timestamp.
4. Notify the supervisor if an active task monitors the file.

### Workspace State Machine

The workspace state machine transitions through:

- IDLE -> INITIALIZING -> ACTIVE_TASK -> AWAITING_VALIDATION -> IDLE
- ACTIVE_TASK can branch into AWAITING_USER, REPLANNING, or ROLLING_BACK as needed.
- Validation success emits TaskCompleted.
- Validation failure routes to REPLANNING.
- Rollback completion emits TaskFailed.

## 14. Plugin Architecture

Velune uses a four-tier plugin model with different trust and isolation boundaries.

### Plugin Tiers

- Tier 1 Provider Plugins extend the provider layer with new model sources and must implement the ModelProvider protocol.
- Tier 2 Tool Plugins add new tools for agents and must implement the BaseTool protocol.
- Tier 3 Agent Plugins add new agent types and must implement the BaseAgent protocol with the typed message protocol.
- Tier 4 Pipeline Plugins register new orchestration pipelines and must return a valid LangGraph CompiledGraph.

### Plugin Manifest

```toml
[plugin]
name = "velune-browseruse"
version = "0.1.0"
tier = "tool"
author = "..."
description = "Adds browser automation tools"

[plugin.capabilities]
tools = ["BrowserNavigate", "BrowserExtract", "BrowserScreenshot"]

[plugin.requirements]
velune_min_version = "0.3.0"
python_packages = ["browser-use>=0.1.0"]

[plugin.permissions]
network_access = true
filesystem_access = false
subprocess_access = false
```

### Plugin Discovery

Plugin discovery scans installed packages, local plugin directories, and explicit declarations in velune.toml. Each plugin is validated for version compatibility, permissions, dependencies, and capability declarations before registration.

## 15. Telemetry and Observability

Velune traces meaningful operations with OpenTelemetry and structured logs.

### What Gets Traced

- Provider inference calls with model, tokens in, tokens out, and latency
- Retrieval queries with query type, candidates considered, and reranker scores
- Agent execution with agent role, model used, message count, and outcome
- Memory operations with operation type, tier, and records affected
- Tool executions with tool name, target, outcome, and duration
- Context assembly with chunks considered, chunks selected, total tokens, and compression ratio

### Structured Logging

Log entries are emitted as structured JSON with correlation IDs that chain across the orchestration pipeline.

```json
{
   "ts": "2025-01-15T10:23:45.123Z",
   "level": "INFO",
   "session_id": "sess-abc123",
   "task_id": "task-xyz789",
   "agent": "coder",
   "event": "patch_applied",
   "file": "src/auth/tokens.py",
   "lines_changed": 47,
   "tokens_used": 3421,
   "model": "deepseek-coder-v2:16b"
}
```

## 16. Implementation Phases

Velune is planned in five phased deliverables.

### Phase 1 - Foundation (weeks 1-4)

Deliverable: velune run "describe this codebase" works.

- Core type system for all contracts
- Configuration loading and validation
- Provider abstraction for Ollama and OpenAI
- Model discovery for Ollama and GGUF
- Basic CLI skeleton with Typer and Rich
- Event bus skeleton with asyncio pub/sub
- ChromaDB vector store adapter
- Naive text chunker
- Single-agent prompt-to-response pipeline
- File system tools: ReadFile, WriteFile, GrepFiles
- Basic episodic memory for conversation turns

### Phase 2 - Repository Cognition (weeks 5-8)

Deliverable: velune run "explain the auth system" retrieves relevant code accurately.

- Tree-sitter integration for Python, TypeScript, Go, and Rust
- AST-aware semantic chunker
- Full repository indexing pipeline
- Dependency graph builder
- Incremental re-indexing via filesystem watcher
- BM25 lexical index
- Hybrid retrieval with RRF fusion
- Context window manager with priority tiers
- Semantic file summarization
- Git awareness tools

### Phase 3 - Multi-Agent Orchestration (weeks 9-14)

Deliverable: velune run "fix the failing auth tests" uses multiple agents.

- LangGraph orchestration engine with dynamic graph building
- Agent protocol with typed messages
- Planner, Coder, Reasoner, and Reviewer agents
- Supervisor agent with intervention logic
- Pre-execution snapshot and rollback
- Execution validation chain
- Cross-encoder reranker
- Full model role assignment and routing

### Phase 4 - Cognitive Memory (weeks 15-20)

Deliverable: velune run "fix the auth issue from yesterday" reconstructs intent.

- Intent reconstruction pipeline with all 7 stages
- Semantic memory with fact extraction and deduplication
- Procedural memory with pattern learning
- Graphiti graph memory integration
- Memory consolidation pipeline
- Memory importance scoring and pruning
- Context compression system
- Workspace state machine

### Phase 5 - Full Autonomy and Plugin System (weeks 21-26)

Deliverable: velune run "implement OAuth2 login" completes multi-file feature implementation with minimal supervision.

- Autonomous planning with feedback loops
- Full provider abstraction for LM Studio, HuggingFace, and Anthropic
- Plugin architecture across all four tiers
- Provider plugin discovery
- Telemetry and observability with OpenTelemetry
- Benchmark suite for retrieval quality and orchestration accuracy
- Full documentation

## 17. Architectural Tradeoffs

Velune makes several explicit tradeoffs to balance complexity, performance, and local-first ergonomics.

### LangGraph vs. Custom Orchestration

Use LangGraph. The stateful execution model, checkpointing, and streaming support fit the orchestration requirements better than a custom loop.

### ChromaDB vs. Qdrant

ChromaDB is the pragmatic default for single-workspace use. Qdrant is the production alternative for multi-tenant or higher-throughput deployments.

### SQLite vs. PostgreSQL for Episodic Memory

SQLite with FTS5 is sufficient for local use. PostgreSQL is the migration path for multi-user or server-hosted deployments.

### Local-First vs. Cloud-First

Velune is local-first by design. Cloud providers are optional fallbacks when local models cannot satisfy the capability gap and the user has consented.

### Mem0 vs. Custom Memory System

Mem0 can be used for extraction primitives, but the storage, lifecycle, tiering, and retrieval logic should remain custom.

## 18. Future Evolution Roadmap

### 0.x - Current Architecture

Single workspace, single user, one terminal session, one Velune instance.

### 1.0 - Persistent Background Agent

Velune runs as a background daemon, continuously indexing the workspace and pre-building context.

### 1.5 - Multi-Workspace

Multiple simultaneous project workspaces with separate memory and context per workspace.

### 2.0 - Distributed Agent Network

Multiple Velune instances collaborate over a local network or VPN, with specialist nodes for inference and test execution.

### 2.5 - Shared Team Cognition

Team members share a semantic memory layer with privacy filters and access controls.

### 3.0 - Autonomous Engineering Agent

Velune operates as an autonomous engineering contributor that creates PRs, responds to CI feedback, files issues, and tracks tasks across sessions.