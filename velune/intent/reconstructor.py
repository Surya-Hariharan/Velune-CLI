"""Intent Reconstructor.

Coordinates multi-source signal inputs, temporal windows, and workspace states
to reconstruct ambiguous queries into structured goals using LLMs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from velune.providers.base import ModelProvider
from velune.intent.parser import IntentSignalParser
from velune.intent.temporal import TemporalResolver
from velune.intent.hypothesis import HypothesisGenerator, IntentHypothesis

logger = logging.getLogger("velune.intent.reconstructor")


class IntentReconstructor:
    """Combines workspace context, history, and linguistic parsed signals to rebuild user intent."""

    def __init__(
        self,
        parser: Optional[IntentSignalParser] = None,
        temporal_resolver: Optional[TemporalResolver] = None,
        hypothesis_gen: Optional[HypothesisGenerator] = None,
    ) -> None:
        self.parser = parser or IntentSignalParser()
        self.temporal_resolver = temporal_resolver or TemporalResolver()
        self.hypothesis_gen = hypothesis_gen or HypothesisGenerator()

    async def reconstruct(
        self,
        raw_query: str,
        provider: ModelProvider,
        model_id: str,
        git_status_output: Optional[str] = None,
    ) -> IntentHypothesis:
        """
        Reconstruct a rich, explicit goal hypothesis from ambiguous inputs using LLM arbitration.
        """
        # 1. Parse linguistic signals and temporal offsets
        signals = self.parser.parse(raw_query)
        temporal_offset = self.temporal_resolver.resolve(raw_query)
        
        # 2. Heuristically generate candidates
        candidates = self.hypothesis_gen.generate_candidates(signals, temporal_offset)
        
        # 3. Formulate LLM prompt to select/synthesize the ultimate goal
        prompt = (
            "You are a Cognitive Intent Reconstructor. Resolve this ambiguous user query into a single structured, "
            "clear execution plan.\n\n"
            f"User Query: {raw_query}\n"
            f"Linguistic Signals:\n{json.dumps(signals, indent=2)}\n"
            f"Git Status Details:\n{git_status_output or 'Clean'}\n\n"
            "Format your output strictly as a JSON object with these keys:\n"
            "- 'goal_description': Detailed user objective (e.g. 'Fix division-by-zero bug in math_ops.py').\n"
            "- 'confidence': Float (0.0 to 1.0) indicating certainty.\n"
            "- 'primary_category': One of 'coding', 'debugging', 'planning', 'general'.\n"
            "- 'target_files': List of exact workspace relative paths referenced or implied.\n"
            "- 'action_plan': List of 3-5 sequential execution steps required to achieve this goal.\n\n"
            "Respond ONLY with valid JSON."
        )

        try:
            logger.info("Executing LLM-driven intent reconstruction using model %s...", model_id)
            response = await provider.complete(prompt=prompt, model=model_id)
            
            content = response.text.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
                
            data = json.loads(content)
            
            reconstructed = IntentHypothesis(
                goal_description=data.get("goal_description", raw_query),
                confidence=float(data.get("confidence", 0.8)),
                primary_category=data.get("primary_category", "general"),
                target_files=data.get("target_files", signals.get("target_files", [])),
                action_plan=data.get("action_plan", ["Decompose goal", "sandbox execution", "validate outcome"]),
            )
            
            logger.info("Intent successfully reconstructed: %s (confidence: %.2f)", reconstructed.goal_description, reconstructed.confidence)
            return reconstructed
        except Exception as e:
            logger.error("Failed to reconstruct intent via LLM: %s. Falling back to heuristic candidate.", e)
            return candidates[0]
