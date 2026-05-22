"""Memory Consolidation Engine.

Distills highly detailed, transient episodic logs into high-level semantic
facts and relationship graphs, executing dynamic tier consolidation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from velune.providers.base import ModelProvider
from velune.memory.tiers.working import WorkingMemoryTier
from velune.memory.tiers.episodic import EpisodicMemoryTier
from velune.memory.tiers.semantic import SemanticMemoryTier
from velune.memory.tiers.graph import GraphMemoryTier
from velune.memory.tiers.archive import LongTermArchiveTier

logger = logging.getLogger("velune.memory.consolidator")


class MemoryConsolidator:
    """Consolidation pipeline moving data through the hierarchical memory tiers."""

    def __init__(
        self,
        working_tier: WorkingMemoryTier,
        episodic_tier: EpisodicMemoryTier,
        semantic_tier: SemanticMemoryTier,
        graph_tier: GraphMemoryTier,
        archive_tier: LongTermArchiveTier,
    ) -> None:
        self.working = working_tier
        self.episodic = episodic_tier
        self.semantic = semantic_tier
        self.graph = graph_tier
        self.archive = archive_tier

    async def ingest_working_to_episodic(self, session_id: str) -> None:
        """Flushes transient working memory turns and steps into SQLite episodic storage."""
        turns = self.working.get_turns()
        for turn in turns:
            self.episodic.add_turn(
                session_id=session_id,
                role=turn.role,
                content=turn.content,
                metadata=turn.metadata,
            )

        logs = self.working.get_execution_logs()
        for log in logs:
            self.episodic.add_execution_step(
                session_id=session_id,
                step_name=log["step"],
                status="completed",
                payload=log["payload"],
            )

        # Clear working memory after flush
        self.working.clear()
        logger.info("Successfully ingested working memory turns to Episodic SQLite tier.")

    async def consolidate_episodic_to_semantic_and_graph(
        self,
        session_id: str,
        provider: ModelProvider,
        model_id: str,
        embedding_provider: Optional[Any] = None,
    ) -> None:
        """
        Uses an LLM (Synthesizer) to distill raw SQLite conversation history into semantic facts
        and relationship graphs, upserting into Qdrant & Graph database.
        """
        turns = self.episodic.get_turns(session_id)
        steps = self.episodic.get_execution_steps(session_id)
        
        if not turns:
            logger.info("No episodic turns found to consolidate for session %s", session_id)
            return

        # 1. Compile prompt to extract key factual assertions
        history_text = "\n".join([f"{t.role.upper()}: {t.content}" for t in turns])
        
        prompt = (
            "You are a cognitive memory consolidator. Analyze the following conversation history and execution steps "
            "and extract a structured list of permanent semantic facts and entity relationships.\n\n"
            "History:\n"
            f"{history_text}\n\n"
            "Format your output strictly as a JSON object with two fields:\n"
            "1. 'facts': A list of strings representing high-level factual assertions (e.g. 'User wants Python 3.10', 'Modified server.py to add retry mechanism').\n"
            "2. 'relations': A list of objects with 'source', 'target', 'relation_type' keys (e.g. 'server.py', 'retry_mechanism', 'implements').\n"
            "Ensure the output is valid JSON."
        )

        try:
            logger.info("Distilling episodic memory via model %s...", model_id)
            response = await provider.complete(prompt=prompt, model=model_id)
            
            # Simple JSON extraction
            content = response.text.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            data = json.loads(content)
            facts: List[str] = data.get("facts", [])
            relations: List[Dict[str, str]] = data.get("relations", [])
            
            logger.info("Extracted %d facts and %d relations.", len(facts), len(relations))

            # 2. Add to Graph Tier
            for rel in relations:
                src, tgt, r_type = rel.get("source"), rel.get("target"), rel.get("relation_type")
                if src and tgt and r_type:
                    self.graph.add_node(src, "entity")
                    self.graph.add_node(tgt, "entity")
                    self.graph.add_edge(src, tgt, r_type)

            # 3. Embed and Add to Semantic Qdrant Tier
            if facts and embedding_provider:
                self.semantic.create_collection("cognitive_facts")
                # Generate embeddings in batch or sequentially
                vectors = []
                payloads = []
                ids = []
                for i, fact in enumerate(facts):
                    emb = await embedding_provider.embed(fact)
                    vectors.append(emb)
                    payloads.append({"fact": fact, "session_id": session_id})
                    ids.append(f"{session_id}_fact_{i}")
                
                self.semantic.upsert_points("cognitive_facts", ids, vectors, payloads)

            # 4. Long-Term Gzip Archival
            turns_dict = [t.model_dump() for t in turns]
            steps_dict = [s.model_dump() for s in steps]
            facts_dict = [{"fact": f} for f in facts]
            
            self.archive.archive_session(
                session_id=session_id,
                turns=turns_dict,
                steps=steps_dict,
                facts=facts_dict,
            )

            # 5. Clean up SQLite database to prevent bloating
            self.episodic.delete_session(session_id)
            logger.info("Successfully consolidated episodic memory to semantic and graph tiers.")

        except Exception as e:
            logger.error("Consolidation pipeline failed for session %s: %s", session_id, e)
