"""Velune - Cognitive AI CLI for autonomous software engineering."""

__version__ = "0.1.0"

from velune.core import *
from velune.providers import *
from velune.models import *
from velune.memory import *
from velune.context import *
from velune.retrieval import *
from velune.repository import *
from velune.workspace import *
from velune.events import *
from velune.tools import *

__all__ = [
    # Core
    "AgentRole",
    "AgentMessage",
    "AgentMessageType",
    "AgentResult",
    "ContextPriority",
    "ContextChunk",
    "ContextWindow",
    "InferenceRequest",
    "StreamChunk",
    "InferenceResponse",
    "MemoryType",
    "MemoryRecord",
    "MemoryQuery",
    "CapabilityLevel",
    "ModelCapability",
    "ModelDescriptor",
    "ProviderConfig",
    "ProviderCapabilities",
    "FileNode",
    "SymbolNode",
    "DependencyEdge",
    "TaskStatus",
    "Task",
    "TaskStep",
    "TaskPlan",
    "TaskResult",
    "WorkspaceState",
    "WorkspaceEvent",
    "CognitionModel",
    "VeluneConfig",
    "ConfigLoader",
    "ServiceContainer",
    # Providers
    "ModelProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "ProviderRegistry",
    # Models
    "ModelCapabilityStore",
    "AssignmentStore",
    "RoutingEngine",
    # Memory
    "WorkingMemoryManager",
    "EpisodicMemoryStore",
    "SemanticMemoryStore",
    "ProceduralMemoryStore",
    "GraphMemoryStore",
    "MemoryLifecycleManager",
    # Context
    "ContextWindowManager",
    "PriorityEngine",
    "CompressionEngine",
    "ContextReconstructor",
    # Retrieval
    "VectorStore",
    "ChromaVectorStore",
    "BM25Index",
    "GraphRetriever",
    "HybridRetrievalPipeline",
    # Repository
    "FilesystemScanner",
    "ASTParser",
    "RepositoryIndexer",
    "RepositoryCognitiveModel",
    # Workspace
    "WorkspaceStateMachine",
    "LiveCognitionModel",
    "GitAwareness",
    # Events
    "EventBus",
    "Event",
    "EventLog",
    # Tools
    "BaseTool",
    "ToolRegistry",
    "ReadFile",
    "WriteFile",
    "ExecuteCommand",
]
