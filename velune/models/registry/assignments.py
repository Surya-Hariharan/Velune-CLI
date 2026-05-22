"""Role-to-model assignment store."""

from typing import Dict, Optional
from velune.core.types import AgentRole, ModelDescriptor
from velune.models.registry.store import ModelCapabilityStore


class AssignmentStore:
    """Store for agent role to model assignments."""

    def __init__(self, capability_store: ModelCapabilityStore):
        self.capability_store = capability_store
        self._assignments: Dict[AgentRole, str] = {}
        self._set_default_assignments()

    def _set_default_assignments(self) -> None:
        """Set default role-to-model assignments."""
        # These will be updated when models are loaded
        pass

    async def initialize(self) -> None:
        """Initialize assignments based on available models."""
        await self.capability_store.initialize()
        
        # Assign best models for each role
        from velune.core.types import ModelCapability
        
        # Planner needs reasoning and planning
        planner_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.PLANNING
        )
        if planner_model:
            self._assignments[AgentRole.PLANNER] = planner_model.id
        
        # Coder needs code generation
        coder_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.CODE_GENERATION
        )
        if coder_model:
            self._assignments[AgentRole.CODER] = coder_model.id
        
        # Reasoner needs reasoning
        reasoner_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.REASONING
        )
        if reasoner_model:
            self._assignments[AgentRole.REASONER] = reasoner_model.id
        
        # Reviewer needs code analysis
        reviewer_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.CODE_ANALYSIS
        )
        if reviewer_model:
            self._assignments[AgentRole.REVIEWER] = reviewer_model.id
        
        # Debugger needs debugging
        debugger_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.DEBUGGING
        )
        if debugger_model:
            self._assignments[AgentRole.DEBUGGER] = debugger_model.id
        
        # Summarizer needs summarization
        summarizer_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.SUMMARIZATION
        )
        if summarizer_model:
            self._assignments[AgentRole.SUMMARIZER] = summarizer_model.id
        
        # Retriever needs retrieval
        retriever_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.RETRIEVAL
        )
        if retriever_model:
            self._assignments[AgentRole.RETRIEVER] = retriever_model.id
        
        # Supervisor needs high-level reasoning and planning
        supervisor_model = self.capability_store.find_best_model_for_capability(
            ModelCapability.PLANNING
        )
        if supervisor_model:
            self._assignments[AgentRole.SUPERVISOR] = supervisor_model.id

    def assign(self, role: AgentRole, model_id: str) -> None:
        """Assign a model to a role."""
        self._assignments[role] = model_id

    def get_assignment(self, role: AgentRole) -> Optional[str]:
        """Get the model ID assigned to a role."""
        return self._assignments.get(role)

    def get_model(self, role: AgentRole) -> Optional[ModelDescriptor]:
        """Get the model descriptor assigned to a role."""
        model_id = self.get_assignment(role)
        if model_id:
            return self.capability_store.get_model(model_id)
        return None

    def list_assignments(self) -> Dict[AgentRole, str]:
        """List all role-to-model assignments."""
        return self._assignments.copy()
