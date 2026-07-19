"""Single source of truth for provider display metadata.

Before this module, the same 15 providers were hand-maintained independently
in three places (``cli/commands/setup.py::PROVIDER_METADATA``,
``cli/onboarding.py::_ONBOARDING_CLOUD`` — which only listed 7 of 15 — and
``cli/provider_ui.py``'s ``CLOUD_PROVIDERS``/``LOCAL_PROVIDERS``), each with a
different shape and each requiring a manual edit in three files whenever a
provider was added, renamed, or re-priced. This module is the one place that
changes now.

Ordering is always alphabetical by ``display_name`` — never by price or
recommendation. Requirement: "Do not prioritize providers based on pricing or
recommendation. Recommendations should be displayed separately." — see
``RECOMMENDED_FREE_START``, an advisory tuple that call sites may render as a
callout line but must never use to reorder the list.
"""

from __future__ import annotations

from dataclasses import dataclass

from velune.providers.keystore import PROVIDER_ENV_VARS


@dataclass(frozen=True, slots=True)
class ProviderMeta:
    """Display metadata for one provider, independent of live configuration
    state (``has_key()``/``is_ollama_live()`` stay in ``keystore.py`` since
    that's runtime state, not a static catalog fact)."""

    id: str
    display_name: str
    description: str
    requires_key: bool
    free_tier: bool
    key_label: str
    get_key_url: str
    env_var: str | None = None


_PROVIDERS: tuple[ProviderMeta, ...] = (
    ProviderMeta(
        id="anthropic",
        display_name="Anthropic",
        description="Creator of Claude — leading AI safety research and models.",
        requires_key=True,
        free_tier=False,
        key_label="Anthropic API key",
        get_key_url="https://console.anthropic.com",
        env_var=PROVIDER_ENV_VARS.get("anthropic"),
    ),
    ProviderMeta(
        id="cohere",
        display_name="Cohere",
        description="Enterprise NLP — Command R+, embeddings, and reranking.",
        requires_key=True,
        free_tier=False,
        key_label="Cohere API key",
        get_key_url="https://dashboard.cohere.com/api-keys",
        env_var=PROVIDER_ENV_VARS.get("cohere"),
    ),
    ProviderMeta(
        id="deepseek",
        display_name="DeepSeek",
        description="DeepSeek-V3, R1 and Coder — powerful reasoning and coding models.",
        requires_key=True,
        free_tier=False,
        key_label="DeepSeek API key",
        get_key_url="https://platform.deepseek.com/api_keys",
        env_var=PROVIDER_ENV_VARS.get("deepseek"),
    ),
    ProviderMeta(
        id="fireworks",
        display_name="Fireworks.AI",
        description="Production open model inference — fast, cheap, and reliable.",
        requires_key=True,
        free_tier=False,
        key_label="Fireworks.AI API key",
        get_key_url="https://fireworks.ai/account/api-keys",
        env_var=PROVIDER_ENV_VARS.get("fireworks"),
    ),
    ProviderMeta(
        id="google",
        display_name="Gemini",
        description="Gemini Pro, Flash, Ultra — Google's multimodal AI family.",
        requires_key=True,
        free_tier=True,
        key_label="Google API key",
        get_key_url="https://aistudio.google.com/app/apikey",
        env_var=PROVIDER_ENV_VARS.get("google"),
    ),
    ProviderMeta(
        id="groq",
        display_name="Groq",
        description="Ultra-fast LPU inference — open models at blazing speed.",
        requires_key=True,
        free_tier=True,
        key_label="Groq API key",
        get_key_url="https://console.groq.com/keys",
        env_var=PROVIDER_ENV_VARS.get("groq"),
    ),
    ProviderMeta(
        id="huggingface",
        display_name="HuggingFace",
        description="70,000+ models via HF Inference API and Inference Endpoints.",
        requires_key=True,
        free_tier=True,
        key_label="HuggingFace token",
        get_key_url="https://huggingface.co/settings/tokens",
        env_var=PROVIDER_ENV_VARS.get("huggingface"),
    ),
    ProviderMeta(
        id="lmstudio",
        display_name="LM Studio",
        description="GUI for local model management and inference.",
        requires_key=False,
        free_tier=True,
        key_label="",
        get_key_url="https://lmstudio.ai",
        env_var=None,
    ),
    ProviderMeta(
        id="meta",
        display_name="Meta",
        description="Meta's official Llama API — Llama 4 and 3.3 models from the source.",
        requires_key=True,
        free_tier=True,
        key_label="Llama API key",
        get_key_url="https://llama.developer.meta.com",
        env_var=PROVIDER_ENV_VARS.get("meta"),
    ),
    ProviderMeta(
        id="mistral",
        display_name="Mistral AI",
        description="European AI — Mistral Large, Codestral, and Mistral Nemo.",
        requires_key=True,
        free_tier=False,
        key_label="Mistral API key",
        get_key_url="https://console.mistral.ai/api-keys",
        env_var=PROVIDER_ENV_VARS.get("mistral"),
    ),
    ProviderMeta(
        id="nvidia",
        display_name="NVIDIA NIM",
        description="Optimized inference on NVIDIA hardware — enterprise AI at scale.",
        requires_key=True,
        free_tier=False,
        key_label="NVIDIA API key",
        get_key_url="https://build.nvidia.com/",
        env_var=PROVIDER_ENV_VARS.get("nvidia"),
    ),
    ProviderMeta(
        id="ollama",
        display_name="Ollama",
        description="Run models locally — zero cloud dependency.",
        requires_key=False,
        free_tier=True,
        key_label="",
        get_key_url="https://ollama.com",
        env_var=None,
    ),
    ProviderMeta(
        id="openai",
        display_name="OpenAI",
        description="GPT-4, o1, and more — the most widely integrated AI platform.",
        requires_key=True,
        free_tier=False,
        key_label="OpenAI API key",
        get_key_url="https://platform.openai.com/api-keys",
        env_var=PROVIDER_ENV_VARS.get("openai"),
    ),
    ProviderMeta(
        id="openrouter",
        display_name="OpenRouter",
        description="Unified access to 100+ models from all major providers.",
        requires_key=True,
        free_tier=False,
        key_label="OpenRouter API key",
        get_key_url="https://openrouter.ai/keys",
        env_var=PROVIDER_ENV_VARS.get("openrouter"),
    ),
    ProviderMeta(
        id="together",
        display_name="Together.AI",
        description="Open model ecosystem — fine-tuning, inference, and embedding.",
        requires_key=True,
        free_tier=False,
        key_label="Together.AI API key",
        get_key_url="https://api.together.ai/settings/api-keys",
        env_var=PROVIDER_ENV_VARS.get("together"),
    ),
    ProviderMeta(
        id="xai",
        display_name="xAI",
        description="xAI's Grok models — built for real-time reasoning.",
        requires_key=True,
        free_tier=False,
        key_label="xAI API key",
        get_key_url="https://console.x.ai",
        env_var=PROVIDER_ENV_VARS.get("xai"),
    ),
    ProviderMeta(
        id="zai",
        display_name="Z.ai",
        description="Z.ai (Zhipu) — the GLM model family, including GLM-4.6.",
        requires_key=True,
        free_tier=True,
        key_label="Z.ai API key",
        get_key_url="https://z.ai/manage-apikey/apikey-list",
        env_var=PROVIDER_ENV_VARS.get("zai"),
    ),
)

_BY_ID: dict[str, ProviderMeta] = {p.id: p for p in _PROVIDERS}

# Advisory only — never used for sorting. Rendered as a separate "recommended
# free start" callout beneath the alphabetical checklist.
RECOMMENDED_FREE_START: tuple[str, ...] = ("ollama", "groq", "google")


def get(provider_id: str) -> ProviderMeta | None:
    return _BY_ID.get(provider_id)


def list_providers_alphabetical() -> list[ProviderMeta]:
    """All known providers, sorted by ``display_name`` — never by price."""
    return sorted(_PROVIDERS, key=lambda p: p.display_name.lower())


def list_cloud_providers_alphabetical() -> list[ProviderMeta]:
    """Providers that require an API key, alphabetically."""
    return [p for p in list_providers_alphabetical() if p.requires_key]


def list_local_providers_alphabetical() -> list[ProviderMeta]:
    """Providers that run locally and need no key, alphabetically."""
    return [p for p in list_providers_alphabetical() if not p.requires_key]
