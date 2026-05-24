"""Unit tests for quantization-aware scoring, model family adjustments, and role-specific context mapping."""

from __future__ import annotations

import pytest

from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.models.scorer import ModelScorer
from velune.models.specializations import ModelSpecializationMapper, CouncilRole, ROLE_CONTEXT_REQUIREMENTS
from velune.models.registry import ModelCapabilityRegistry


def test_qwen_coding_boost():
    scorer = ModelScorer()

    qwen_coder = ModelDescriptor(
        id="qwen-coder-7b",
        provider="ollama",
        name="Qwen 2.5 Coder 7B",
        context_window=16384,
        is_local=True,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast"
    )

    llama_generic = ModelDescriptor(
        id="llama-3.2-3b",
        provider="ollama",
        name="Llama 3.2 3B",
        context_window=16384,
        is_local=True,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast"
    )

    qwen_score = scorer.score(qwen_coder, "coding")
    llama_score = scorer.score(llama_generic, "coding")

    # Qwen should receive a family capability boost (+0.1) for coding, making its score higher than Llama
    assert qwen_score > llama_score


def test_deepseek_reasoning_boost():
    scorer = ModelScorer()

    ds_r1 = ModelDescriptor(
        id="deepseek-r1-7b",
        provider="ollama",
        name="Deepseek R1 7B",
        context_window=16384,
        is_local=True,
        capabilities=ModelCapabilityProfile(reasoning=CapabilityLevel.ADVANCED),
        speed_tier="fast"
    )

    generic = ModelDescriptor(
        id="generic-model",
        provider="ollama",
        name="Generic Model",
        context_window=16384,
        is_local=True,
        capabilities=ModelCapabilityProfile(reasoning=CapabilityLevel.ADVANCED),
        speed_tier="fast"
    )

    ds_score = scorer.score(ds_r1, "reasoning")
    generic_score = scorer.score(generic, "reasoning")

    # DeepSeek should receive a family capability boost (+0.1) for reasoning
    assert ds_score > generic_score


def test_quantization_penalties():
    scorer = ModelScorer()

    # Base descriptor with reasoning capability
    base_params = {
        "provider": "ollama",
        "name": "Model",
        "context_window": 16384,
        "is_local": True,
        "capabilities": ModelCapabilityProfile(reasoning=CapabilityLevel.ADVANCED),
        "speed_tier": "fast"
    }

    model_q2 = ModelDescriptor(id="ds-q2", quantization="Q2", **base_params)
    model_q4 = ModelDescriptor(id="ds-q4", quantization="Q4_K_M", **base_params)
    model_q8 = ModelDescriptor(id="ds-q8", quantization="Q8_0", **base_params)
    model_fp16 = ModelDescriptor(id="ds-fp16", quantization="FP16", **base_params)

    score_q2 = scorer.score(model_q2, "reasoning")
    score_q4 = scorer.score(model_q4, "reasoning")
    score_q8 = scorer.score(model_q8, "reasoning")
    score_fp16 = scorer.score(model_fp16, "reasoning")

    # Q2 should have a heavy penalty (-0.25)
    # Q4_K_M should have a minor penalty (-0.05)
    # Q8 should have no penalty
    # FP16 should have a boost (+0.05)
    assert score_q2 < score_q4
    assert score_q4 < score_q8
    assert score_q8 < score_fp16


def test_role_specific_context_mapping():
    # Setup registry with two models: one small context (8k) and one huge context (128k)
    registry = ModelCapabilityRegistry(scanner=None)

    small_ctx_model = ModelDescriptor(
        id="small-ctx-model",
        provider="ollama",
        name="Small Context Model",
        context_window=8192,
        is_local=True,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED
        ),
        speed_tier="fast"
    )

    huge_ctx_model = ModelDescriptor(
        id="huge-ctx-model",
        provider="ollama",
        name="Huge Context Model",
        context_window=128000,
        is_local=True,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED
        ),
        speed_tier="fast"
    )

    registry.register(small_ctx_model)
    registry.register(huge_ctx_model)

    mapper = ModelSpecializationMapper(registry=registry)
    assignments = mapper.map_roles()

    # Synthesizer needs 64k context, so the huge context model should be assigned over the small one
    assert assignments[CouncilRole.SYNTHESIZER].model_id == "huge-ctx-model"

    # Verify context requirements dict is present
    assert ROLE_CONTEXT_REQUIREMENTS[CouncilRole.SYNTHESIZER] == 65536
    assert ROLE_CONTEXT_REQUIREMENTS[CouncilRole.CODER] == 32768


def test_vram_routing_guardrails():
    # Setup container with mocked gpu_info
    from velune.kernel.registry import get_container
    container = get_container()
    
    # We save existing gpu_info if any
    old_gpu_info = container._singletons.get("runtime.gpu_info")
    
    # Mock free VRAM to 4.0 GB
    container.register_instance("runtime.gpu_info", {"has_gpu": True, "vram_free_gb": 4.0})
    
    try:
        registry = ModelCapabilityRegistry(scanner=None)
        
        # 8GB model (should be skipped)
        huge_vram_model = ModelDescriptor(
            id="huge-vram-model",
            provider="ollama",
            name="Huge VRAM Model",
            context_window=16384,
            is_local=True,
            vram_required_gb=8.0,
            capabilities=ModelCapabilityProfile(
                coding=CapabilityLevel.ADVANCED,
                reasoning=CapabilityLevel.ADVANCED,
                planning=CapabilityLevel.ADVANCED
            ),
            speed_tier="fast"
        )
        
        # 3.5GB model (should be selected)
        small_vram_model = ModelDescriptor(
            id="small-vram-model",
            provider="ollama",
            name="Small VRAM Model",
            context_window=16384,
            is_local=True,
            vram_required_gb=3.5,
            capabilities=ModelCapabilityProfile(
                coding=CapabilityLevel.ADVANCED,
                reasoning=CapabilityLevel.ADVANCED,
                planning=CapabilityLevel.ADVANCED
            ),
            speed_tier="fast"
        )
        
        registry.register(huge_vram_model)
        registry.register(small_vram_model)
        
        mapper = ModelSpecializationMapper(registry=registry)
        assignments = mapper.map_roles()
        
        # Huge VRAM model is skipped, small VRAM model is selected
        assert assignments[CouncilRole.PLANNER].model_id == "small-vram-model"
        
    finally:
        # Restore old gpu_info
        if old_gpu_info is not None:
            container.register_instance("runtime.gpu_info", old_gpu_info)
        elif "runtime.gpu_info" in container._singletons:
            del container._singletons["runtime.gpu_info"]


def test_gguf_vram_estimation():
    from velune.providers.discovery.gguf import GGUFDiscovery
    discovery = GGUFDiscovery()
    
    # Q4_K_M: bytes_per_param = 0.55
    # Formula: (param_count_b * bytes_per_param) + 0.5
    # For 7B params: (7.0 * 0.55) + 0.5 = 3.85 + 0.5 = 4.35
    vram_q4 = discovery._estimate_vram(7.0, "Q4_K_M")
    assert abs(vram_q4 - 4.35) < 1e-5
    
    # Q8_0: bytes_per_param = 1.0
    # For 7B params: (7.0 * 1.0) + 0.5 = 7.5
    vram_q8 = discovery._estimate_vram(7.0, "Q8_0")
    assert abs(vram_q8 - 7.5) < 1e-5
    
    # FP16: bytes_per_param = 2.0
    # For 7B params: (7.0 * 2.0) + 0.5 = 14.5
    vram_fp16 = discovery._estimate_vram(7.0, "FP16")
    assert abs(vram_fp16 - 14.5) < 1e-5
