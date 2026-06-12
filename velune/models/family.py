"""Model family detection and classification.

Identifies model families by ID pattern to enable prompt format adaptation.
Different families respond better to different prompt structures:
- Qwen: ChatML format with <|im_start|>/<|im_end|>
- DeepSeek: Specialized thinking prompt structure
- Llama3: [INST] and <<SYS>> markers
- Phi: Shorter, more direct prompts
- Mistral: [INST] markers with different system placement
- Gemma: Direct format similar to Llama
- Cloud APIs: Standard message list format
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger("velune.models.family")


class ModelFamily(StrEnum):
    """Enumeration of model families with distinct prompt formats."""

    QWEN = "qwen"
    DEEPSEEK = "deepseek"
    LLAMA3 = "llama3"
    PHI = "phi"
    MISTRAL = "mistral"
    GEMMA = "gemma"
    CLAUDE = "claude"
    GPT = "gpt"
    GEMINI = "gemini"
    UNKNOWN = "unknown"


def detect_family(model_id: str) -> ModelFamily:
    """Detect model family from model ID string (case-insensitive).

    Parameters
    ----------
    model_id:
        The model identifier, e.g., "qwen:7b", "Claude-3", "deepseek-r1"

    Returns
    -------
    ModelFamily:
        The detected family, or UNKNOWN if no match.
    """
    if not model_id:
        return ModelFamily.UNKNOWN

    model_id_lower = model_id.lower()

    # Check for exact or prefix matches (order matters for specificity)
    if "qwen" in model_id_lower:
        return ModelFamily.QWEN
    if "deepseek" in model_id_lower or "deepseek-r1" in model_id_lower:
        return ModelFamily.DEEPSEEK
    if "llama3" in model_id_lower or "llama-3" in model_id_lower:
        return ModelFamily.LLAMA3
    if "phi" in model_id_lower:
        return ModelFamily.PHI
    if "mistral" in model_id_lower:
        return ModelFamily.MISTRAL
    if "gemma" in model_id_lower:
        return ModelFamily.GEMMA
    if "claude" in model_id_lower:
        return ModelFamily.CLAUDE
    if "gpt" in model_id_lower or "gpt-4" in model_id_lower or "gpt-3.5" in model_id_lower:
        return ModelFamily.GPT
    if "gemini" in model_id_lower:
        return ModelFamily.GEMINI

    logger.debug("Unknown model family for model_id: %s", model_id)
    return ModelFamily.UNKNOWN
