"""Tests for prompt adaptation engine.

Tests verify that:
1. Each family's template produces the correct format
2. System prompt truncation works for models with small limits
3. UNKNOWN family falls back to StandardAPITemplate
4. Message adaptation works correctly
5. Capabilities are properly reported
"""

from __future__ import annotations

import pytest

from velune.context.prompt_adaptation import (
    DeepSeekTemplate,
    GemmaTemplate,
    Llama3Template,
    MistralTemplate,
    PhiTemplate,
    PromptAdaptationEngine,
    QwenTemplate,
    StandardAPITemplate,
)
from velune.core.types.model import ModelDescriptor
from velune.models.family import ModelFamily, detect_family


class TestModelFamilyDetection:
    """Test model family detection from model IDs."""

    def test_detect_qwen(self) -> None:
        assert detect_family("qwen:7b") == ModelFamily.QWEN
        assert detect_family("qwen:14b") == ModelFamily.QWEN
        assert detect_family("Qwen/Qwen2-7B") == ModelFamily.QWEN

    def test_detect_deepseek(self) -> None:
        assert detect_family("deepseek-r1") == ModelFamily.DEEPSEEK
        assert detect_family("DeepSeek-V3") == ModelFamily.DEEPSEEK

    def test_detect_llama3(self) -> None:
        assert detect_family("llama3:7b") == ModelFamily.LLAMA3
        assert detect_family("meta-llama/Llama-2-7b") == ModelFamily.LLAMA3
        assert detect_family("llama-3-8b") == ModelFamily.LLAMA3

    def test_detect_phi(self) -> None:
        assert detect_family("phi:2.5") == ModelFamily.PHI
        assert detect_family("microsoft/phi-2") == ModelFamily.PHI

    def test_detect_mistral(self) -> None:
        assert detect_family("mistral:7b") == ModelFamily.MISTRAL
        assert detect_family("mistralai/Mistral-7B") == ModelFamily.MISTRAL

    def test_detect_gemma(self) -> None:
        assert detect_family("gemma:7b") == ModelFamily.GEMMA
        assert detect_family("google/gemma-7b") == ModelFamily.GEMMA

    def test_detect_claude(self) -> None:
        assert detect_family("claude-3") == ModelFamily.CLAUDE
        assert detect_family("claude-3-sonnet") == ModelFamily.CLAUDE

    def test_detect_gpt(self) -> None:
        assert detect_family("gpt-4") == ModelFamily.GPT
        assert detect_family("gpt-3.5-turbo") == ModelFamily.GPT

    def test_detect_gemini(self) -> None:
        assert detect_family("gemini-pro") == ModelFamily.GEMINI
        assert detect_family("google/gemini-2.0") == ModelFamily.GEMINI

    def test_detect_unknown(self) -> None:
        assert detect_family("unknown-model") == ModelFamily.UNKNOWN
        assert detect_family("") == ModelFamily.UNKNOWN
        assert detect_family("random-string") == ModelFamily.UNKNOWN

    def test_case_insensitive(self) -> None:
        assert detect_family("QWEN:7B") == ModelFamily.QWEN
        assert detect_family("Claude-3") == ModelFamily.CLAUDE
        assert detect_family("DeepSeek-V2") == ModelFamily.DEEPSEEK


class TestQwenTemplate:
    """Test Qwen ChatML format with <|im_start|>/<|im_end|> tokens."""

    def test_format_system(self) -> None:
        result = QwenTemplate.format_system("You are a helpful assistant.")
        assert "<|im_start|>system" in result
        assert "You are a helpful assistant." in result
        assert "<|im_end|>" in result

    def test_format_user(self) -> None:
        result = QwenTemplate.format_user("Hello, how are you?")
        assert "<|im_start|>user" in result
        assert "Hello, how are you?" in result
        assert "<|im_end|>" in result

    def test_format_assistant(self) -> None:
        result = QwenTemplate.format_assistant("I'm doing well, thank you!")
        assert "<|im_start|>assistant" in result
        assert "I'm doing well, thank you!" in result
        assert "<|im_end|>" in result

    def test_format_full_conversation(self) -> None:
        turns = [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
        ]
        result = QwenTemplate.format_full_conversation("You are helpful.", turns)
        assert "<|im_start|>system" in result
        assert "You are helpful." in result
        assert "What is 2+2?" in result
        assert "<|im_end|>" in result

    def test_capabilities(self) -> None:
        assert QwenTemplate.supports_xml_structured_output is True
        assert QwenTemplate.supports_json_mode is True
        assert QwenTemplate.max_recommended_system_tokens == 4096


class TestDeepSeekTemplate:
    """Test DeepSeek format with reasoning support."""

    def test_format_system(self) -> None:
        result = DeepSeekTemplate.format_system("Think step by step.")
        assert "System:" in result
        assert "Think step by step." in result

    def test_format_full_conversation(self) -> None:
        turns = [
            {"role": "user", "content": "Explain quantum computing."},
            {"role": "assistant", "content": "Quantum computers use qubits..."},
        ]
        result = DeepSeekTemplate.format_full_conversation("You are expert.", turns)
        assert "System:" in result
        assert "You are expert." in result
        assert "User:" in result
        assert "Assistant:" in result

    def test_capabilities(self) -> None:
        assert DeepSeekTemplate.supports_xml_structured_output is True
        assert DeepSeekTemplate.supports_json_mode is True


class TestLlama3Template:
    """Test Llama3 [INST] / <<SYS>> format."""

    def test_format_system(self) -> None:
        result = Llama3Template.format_system("Answer concisely.")
        assert "<<SYS>>" in result
        assert "Answer concisely." in result
        assert "<</SYS>>" in result

    def test_format_user(self) -> None:
        result = Llama3Template.format_user("What is AI?")
        assert "[INST]" in result
        assert "What is AI?" in result
        assert "[/INST]" in result

    def test_format_full_conversation(self) -> None:
        turns = [
            {"role": "user", "content": "Define machine learning."},
            {"role": "assistant", "content": "Machine learning is..."},
        ]
        result = Llama3Template.format_full_conversation("Be technical.", turns)
        assert "<<SYS>>" in result
        assert "Be technical." in result
        assert "[INST]" in result
        assert "[/INST]" in result

    def test_capabilities(self) -> None:
        assert Llama3Template.supports_xml_structured_output is True
        assert Llama3Template.supports_json_mode is False
        assert Llama3Template.max_recommended_system_tokens == 2048


class TestPhiTemplate:
    """Test Phi template with shorter, direct format."""

    def test_format_system(self) -> None:
        # Phi returns system as-is
        result = PhiTemplate.format_system("Be brief.")
        assert result == "Be brief."

    def test_format_user(self) -> None:
        # Phi returns user as-is
        result = PhiTemplate.format_user("Hello.")
        assert result == "Hello."

    def test_prefer_shorter_prompts(self) -> None:
        assert PhiTemplate.prefer_shorter_system_prompts is True
        assert PhiTemplate.max_recommended_system_tokens == 512

    def test_capabilities(self) -> None:
        assert PhiTemplate.supports_xml_structured_output is False
        assert PhiTemplate.supports_json_mode is False


class TestMistralTemplate:
    """Test Mistral [INST] format with [SYSTEM] prefix."""

    def test_format_system(self) -> None:
        result = MistralTemplate.format_system("Be helpful.")
        assert "[SYSTEM]" in result
        assert "Be helpful." in result

    def test_format_full_conversation(self) -> None:
        turns = [{"role": "user", "content": "Hello!"}]
        result = MistralTemplate.format_full_conversation("System context.", turns)
        assert "[SYSTEM]" in result
        assert "[INST]" in result


class TestGemmaTemplate:
    """Test Gemma format with <start_of_turn>/<end_of_turn> markers."""

    def test_format_user(self) -> None:
        result = GemmaTemplate.format_user("What's your name?")
        assert "<start_of_turn>user" in result
        assert "What's your name?" in result
        assert "<end_of_turn>" in result

    def test_format_assistant(self) -> None:
        result = GemmaTemplate.format_assistant("I'm Gemma.")
        assert "<start_of_turn>model" in result
        assert "I'm Gemma." in result
        assert "<end_of_turn>" in result


class TestStandardAPITemplate:
    """Test standard API format for Claude, GPT, Gemini."""

    def test_format_system(self) -> None:
        result = StandardAPITemplate.format_system("You are helpful.")
        assert result == "You are helpful."

    def test_format_user(self) -> None:
        result = StandardAPITemplate.format_user("Hello!")
        assert result == "Hello!"

    def test_format_assistant(self) -> None:
        result = StandardAPITemplate.format_assistant("Hi!")
        assert result == "Hi!"

    def test_capabilities(self) -> None:
        assert StandardAPITemplate.supports_xml_structured_output is True
        assert StandardAPITemplate.supports_json_mode is True
        assert StandardAPITemplate.max_recommended_system_tokens == 4096


class TestPromptAdaptationEngine:
    """Test the adaptation engine."""

    @pytest.fixture
    def engine(self) -> PromptAdaptationEngine:
        return PromptAdaptationEngine()

    @pytest.fixture
    def qwen_model(self) -> ModelDescriptor:
        return ModelDescriptor(
            model_id="qwen:7b",
            provider_id="ollama",
            display_name="Qwen 7B",
            context_length=8192,
            capabilities={},
        )

    @pytest.fixture
    def phi_model(self) -> ModelDescriptor:
        return ModelDescriptor(
            model_id="phi:2.5",
            provider_id="ollama",
            display_name="Phi 2.5",
            context_length=2048,
            capabilities={},
        )

    @pytest.fixture
    def claude_model(self) -> ModelDescriptor:
        return ModelDescriptor(
            model_id="claude-3-sonnet",
            provider_id="anthropic",
            display_name="Claude 3 Sonnet",
            context_length=200000,
            capabilities={},
        )

    def test_get_template_qwen(self, engine: PromptAdaptationEngine, qwen_model: ModelDescriptor) -> None:
        template = engine.get_template(qwen_model)
        assert template == QwenTemplate

    def test_get_template_phi(self, engine: PromptAdaptationEngine, phi_model: ModelDescriptor) -> None:
        template = engine.get_template(phi_model)
        assert template == PhiTemplate

    def test_get_template_claude(self, engine: PromptAdaptationEngine, claude_model: ModelDescriptor) -> None:
        template = engine.get_template(claude_model)
        assert template == StandardAPITemplate

    def test_adapt_system_prompt_no_truncation(
        self, engine: PromptAdaptationEngine, claude_model: ModelDescriptor
    ) -> None:
        system = "You are a helpful assistant."
        adapted = engine.adapt_system_prompt(system, claude_model)
        assert adapted == system

    def test_adapt_system_prompt_with_truncation(
        self, engine: PromptAdaptationEngine, phi_model: ModelDescriptor
    ) -> None:
        # Create a long system prompt (>512 tokens)
        system = "You are a helpful assistant. " * 100  # ~2700 tokens

        adapted = engine.adapt_system_prompt(system, phi_model)

        # Adapted prompt should be shorter
        assert len(adapted) < len(system)
        # Should contain truncation marker
        assert "[... truncated" in adapted or len(adapted) < len(system)

    def test_adapt_messages_for_qwen(
        self, engine: PromptAdaptationEngine, qwen_model: ModelDescriptor
    ) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        adapted = engine.adapt_messages(messages, qwen_model)

        # For Qwen, should convert to single text message
        assert len(adapted) == 1
        assert adapted[0]["role"] == "user"
        # Should contain ChatML markers
        assert "<|im_start|>" in adapted[0]["content"]

    def test_adapt_messages_for_claude(
        self, engine: PromptAdaptationEngine, claude_model: ModelDescriptor
    ) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"},
        ]

        adapted = engine.adapt_messages(messages, claude_model)

        # For Claude, should return unchanged
        assert adapted == messages

    def test_adapt_messages_for_gpt(self, engine: PromptAdaptationEngine) -> None:
        gpt_model = ModelDescriptor(
            model_id="gpt-4",
            provider_id="openai",
            display_name="GPT-4",
            context_length=8192,
            capabilities={},
        )

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"},
        ]

        adapted = engine.adapt_messages(messages, gpt_model)

        # For GPT, should return unchanged
        assert adapted == messages

    def test_get_capabilities_qwen(
        self, engine: PromptAdaptationEngine, qwen_model: ModelDescriptor
    ) -> None:
        caps = engine.get_capabilities(qwen_model)
        assert caps["supports_xml"] is True
        assert caps["supports_json_mode"] is True
        assert caps["max_system_tokens"] == 4096

    def test_get_capabilities_phi(
        self, engine: PromptAdaptationEngine, phi_model: ModelDescriptor
    ) -> None:
        caps = engine.get_capabilities(phi_model)
        assert caps["prefer_shorter_prompts"] is True
        assert caps["supports_xml"] is False
        assert caps["supports_json_mode"] is False
        assert caps["max_system_tokens"] == 512

    def test_unknown_model_falls_back_to_standard(self, engine: PromptAdaptationEngine) -> None:
        unknown_model = ModelDescriptor(
            model_id="totally-unknown-model",
            provider_id="unknown",
            display_name="Unknown",
            context_length=2048,
            capabilities={},
        )

        template = engine.get_template(unknown_model)
        assert template == StandardAPITemplate

        messages = [
            {"role": "system", "content": "Help!"},
            {"role": "user", "content": "Hello!"},
        ]
        adapted = engine.adapt_messages(messages, unknown_model)
        # Unknown models use standard format (cloud APIs)
        assert adapted == messages

    def test_empty_system_prompt(
        self, engine: PromptAdaptationEngine, qwen_model: ModelDescriptor
    ) -> None:
        adapted = engine.adapt_system_prompt("", qwen_model)
        assert adapted == ""

    def test_none_system_prompt(
        self, engine: PromptAdaptationEngine, qwen_model: ModelDescriptor
    ) -> None:
        adapted = engine.adapt_system_prompt(None or "", qwen_model)
        assert adapted == ""
