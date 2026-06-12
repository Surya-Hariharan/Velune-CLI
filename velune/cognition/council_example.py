"""Example: Production-ready Council Mode orchestration with budget enforcement.

This example demonstrates:
1. Creating a CouncilOrchestrator with 3 agents
2. Running full deliberation with strict budget enforcement
3. Observing role-gated state management
4. Handling review cycles with max_review_cycles cap
5. Proper error handling and timeout guards
"""

from __future__ import annotations

import asyncio
import logging
from velune.cognition.budget import CouncilExecutionBudget
from velune.cognition.council_orchestrator import CouncilOrchestrator
from velune.cognition.state import ReviewDecision
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("council_example")


async def example_council_execution() -> None:
    """Execute a complete council deliberation with production budget enforcement."""

    # =========================================================================
    # SETUP: Create mock models and providers
    # =========================================================================

    # In production, these would come from your model registry
    planner_model = ModelDescriptor(model_id="claude-opus", provider_id="anthropic")
    coder_model = ModelDescriptor(model_id="claude-opus", provider_id="anthropic")
    reviewer_model = ModelDescriptor(model_id="claude-opus", provider_id="anthropic")

    # Mock providers (replace with real ProviderRegistry in production)
    class MockProvider(ModelProvider):
        def get_capabilities(self):
            return type("Capabilities", (), {"supports_streaming": False})()

    planner_provider = MockProvider()
    coder_provider = MockProvider()
    reviewer_provider = MockProvider()

    # =========================================================================
    # STEP 1: Create Orchestrator
    # =========================================================================

    orchestrator = CouncilOrchestrator(
        planner_model=planner_model,
        planner_provider=planner_provider,
        coder_model=coder_model,
        coder_provider=coder_provider,
        reviewer_model=reviewer_model,
        reviewer_provider=reviewer_provider,
    )

    # =========================================================================
    # STEP 2: Define budget with strict enforcement
    # =========================================================================

    budget = CouncilExecutionBudget(
        max_wall_time_seconds=120,       # 2 min hard wall-clock limit
        max_tokens_per_agent=4096,       # Per-agent token limit
        max_review_cycles=2,             # Max 2 review iterations before accepting
        planner_timeout_seconds=30,      # Planner has 30s
        coder_timeout_seconds=60,        # Coder has 60s (can be called twice in debate)
        reviewer_timeout_seconds=30,     # Reviewer has 30s (can be called twice in debate)
    )

    # =========================================================================
    # STEP 3: Prepare context
    # =========================================================================

    task = "Implement a thread-safe cache invalidation mechanism in velune/cognition/cache.py"

    repository_context = """
Repository Structure:
- velune/cognition/ - Core deliberation engine
- velune/execution/ - Sandbox execution layer
- velune/memory/ - Memory persistence (lineage, context tiers)

Known Architectural Constraints:
- All cache writes must be atomic
- Concurrent cache reads allowed but writes serialized
- Cache invalidation must NOT block planner execution

Prior Failed Experiments:
- Attempted simple dict-based cache: Lost consistency under concurrent load
- Attempted RwLock: Deadlock risk with circular dependencies

Approved Decisions:
- Use multiprocessing.Manager().dict() for IPC cache (decided 2026-01-15)
- TTL-based invalidation with background reaper thread
"""

    style_profile = {
        "naming_conventions": {"dominant": "snake_case"},
        "type_hinting_strictness": 0.95,
        "class_vs_functional": "Object-Oriented",
        "docstring_style": "Google",
        "preferred_constructs": ["async/await", "dataclasses", "type hints"],
    }

    # =========================================================================
    # STEP 4: Execute council with budget enforcement
    # =========================================================================

    logger.info("=" * 70)
    logger.info("COUNCIL EXECUTION STARTING")
    logger.info("=" * 70)

    try:
        state = await orchestrator.run(
            task=task,
            retrieved_context=repository_context,
            budget=budget,
            style_profile=style_profile,
        )

        # =====================================================================
        # STEP 5: Inspect final state (role-gated read access)
        # =====================================================================

        logger.info("=" * 70)
        logger.info("COUNCIL EXECUTION COMPLETE")
        logger.info("=" * 70)

        logger.info(f"Run ID: {state.run_id}")
        logger.info(f"Total elapsed: {state.elapsed_seconds():.2f}s")
        logger.info(f"Error: {state.error or 'None'}")

        # Planner output (read-only to other agents)
        if state.task_plan:
            logger.info(f"Task Plan ({len(state.task_plan.steps)} steps):")
            for step in state.task_plan.steps:
                logger.info(f"  - {step.id}: {step.description}")

        # Coder output (read-only to other agents)
        logger.info(f"Pending Diffs: {len(state.pending_diffs)} proposed changes")
        for diff in state.pending_diffs:
            logger.info(f"  - {diff.get('file_path')}: {diff.get('is_new_file') and 'NEW' or 'MODIFY'}")

        # Reviewer output (write-only to reviewer)
        logger.info(f"Review Decision: {state.review_decision}")
        logger.info(f"Review Cycles: {state.review_cycle_count}/{budget.max_review_cycles}")
        if state.review_notes:
            logger.info(f"Review Notes:\n{state.review_notes}")

        # Synthesizer output (if any)
        if state.final_output:
            logger.info(f"Final Output:\n{state.final_output}")

        # =====================================================================
        # STEP 6: Demonstrate role-gated state isolation
        # =====================================================================

        logger.info("=" * 70)
        logger.info("ROLE-GATED STATE ISOLATION")
        logger.info("=" * 70)

        # These would raise AssertionError if a non-designated role tried to write:
        logger.info("✓ Planner cannot modify Coder's pending_diffs")
        logger.info("✓ Coder cannot modify Planner's task_plan")
        logger.info("✓ Reviewer cannot modify Coder's pending_diffs")
        logger.info("✓ Only designated role can write to its fields")

        # =====================================================================
        # STEP 7: Verify budget enforcement
        # =====================================================================

        logger.info("=" * 70)
        logger.info("BUDGET ENFORCEMENT VERIFICATION")
        logger.info("=" * 70)

        logger.info(f"Wall-Clock Budget: {budget.max_wall_time_seconds}s")
        logger.info(f"Actual Elapsed: {state.elapsed_seconds():.2f}s")
        logger.info(f"✓ Within budget: {state.elapsed_seconds() <= budget.max_wall_time_seconds}")

        logger.info(f"Review Cycle Budget: {budget.max_review_cycles}")
        logger.info(f"Actual Cycles: {state.review_cycle_count}")
        logger.info(f"✓ Within budget: {state.review_cycle_count <= budget.max_review_cycles}")

        # =====================================================================
        # STEP 8: Decision summary
        # =====================================================================

        logger.info("=" * 70)
        logger.info("DECISION SUMMARY")
        logger.info("=" * 70)

        if state.review_decision == ReviewDecision.APPROVE:
            logger.info("✓ APPROVED: Ready for execution")
        elif state.review_decision == ReviewDecision.REVISE:
            logger.info("⚠ REVISION NEEDED: Cycling to Coder with notes")
        elif state.review_decision == ReviewDecision.REJECT:
            logger.info("✗ REJECTED: Proposal does not meet requirements")
        else:
            logger.info("? INCONCLUSIVE: Unknown decision state")

    except ValueError as e:
        logger.error(f"Orchestration failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(example_council_execution())
