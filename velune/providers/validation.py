"""Provider validation engine — real API calls, human-friendly diagnostics.

Every ``validate_provider()`` call makes a live round-trip to the provider's
API so that invalid, expired, revoked, or rate-limited keys are caught before
being persisted to the OS keyring.  No regex, no prefix heuristics — the only
source of truth is the provider's own response.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum


class ValidationStatus(str, Enum):
    OK = "ok"
    INVALID_KEY = "invalid_key"
    EXPIRED_KEY = "expired_key"
    REVOKED_KEY = "revoked_key"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    MALFORMED_KEY = "malformed_key"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class ValidationResult:
    provider_id: str
    status: ValidationStatus
    message: str
    models: list[str] = field(default_factory=list)
    account_info: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == ValidationStatus.OK

    def human_message(self) -> str:
        """Return a single, user-facing diagnostic line."""
        if self.ok:
            n = len(self.models)
            return f"✓ {self.provider_id} — authenticated ({n} model{'s' if n != 1 else ''} available)"
        icons = {
            ValidationStatus.INVALID_KEY: "✗",
            ValidationStatus.EXPIRED_KEY: "✗",
            ValidationStatus.REVOKED_KEY: "✗",
            ValidationStatus.RATE_LIMITED: "⚠",
            ValidationStatus.NETWORK_ERROR: "✗",
            ValidationStatus.MALFORMED_KEY: "✗",
            ValidationStatus.PERMISSION_DENIED: "✗",
            ValidationStatus.UNKNOWN_ERROR: "✗",
        }
        return f"{icons.get(self.status, '✗')} {self.message}"


# ---------------------------------------------------------------------------
# Per-provider validators
# ---------------------------------------------------------------------------

async def _validate_openai(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="openai",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            body = resp.json()
            err = body.get("error", {}).get("message", "")
            if "expired" in err.lower():
                return ValidationResult("openai", ValidationStatus.EXPIRED_KEY,
                                        "OpenAI API key has expired.")
            return ValidationResult("openai", ValidationStatus.INVALID_KEY,
                                    "Invalid OpenAI API key.")
        if resp.status_code == 429:
            return ValidationResult("openai", ValidationStatus.RATE_LIMITED,
                                    "OpenAI API key is rate-limited. Try again in a moment.")
        return ValidationResult("openai", ValidationStatus.UNKNOWN_ERROR,
                                f"OpenAI returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("openai", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching OpenAI: {e}")


async def _validate_anthropic(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Anthropic has no cheap /models endpoint that lists all models without billing;
            # send a minimal 1-token completion which is the canonical auth test.
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
            )
        if resp.status_code in (200, 400):
            # 400 can mean valid key but bad params — auth passed either way
            return ValidationResult(
                provider_id="anthropic",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"],
                account_info={"organization": "verified"},
            )
        if resp.status_code == 401:
            return ValidationResult("anthropic", ValidationStatus.INVALID_KEY,
                                    "Invalid Anthropic API key.")
        if resp.status_code == 403:
            return ValidationResult("anthropic", ValidationStatus.PERMISSION_DENIED,
                                    "Anthropic API key lacks permission for this operation.")
        if resp.status_code == 429:
            return ValidationResult("anthropic", ValidationStatus.RATE_LIMITED,
                                    "Anthropic API key is rate-limited.")
        return ValidationResult("anthropic", ValidationStatus.UNKNOWN_ERROR,
                                f"Anthropic returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("anthropic", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching Anthropic: {e}")


async def _validate_google(api_key: str) -> ValidationResult:
    try:
        import httpx
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("name", "").split("/")[-1] for m in data.get("models", [])]
            return ValidationResult(
                provider_id="google",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 400:
            body = resp.json()
            status_val = body.get("error", {}).get("status", "")
            if status_val in ("INVALID_ARGUMENT",):
                return ValidationResult("google", ValidationStatus.INVALID_KEY,
                                        "Invalid Google API key.")
        if resp.status_code == 403:
            body = resp.json()
            msg = body.get("error", {}).get("message", "")
            if "api key" in msg.lower():
                return ValidationResult("google", ValidationStatus.INVALID_KEY,
                                        "Invalid or disabled Google API key.")
            return ValidationResult("google", ValidationStatus.PERMISSION_DENIED,
                                    "Google API key lacks required permissions.")
        if resp.status_code == 429:
            return ValidationResult("google", ValidationStatus.RATE_LIMITED,
                                    "Google API key is rate-limited.")
        return ValidationResult("google", ValidationStatus.UNKNOWN_ERROR,
                                f"Google returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("google", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching Google: {e}")


async def _validate_groq(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="groq",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("groq", ValidationStatus.INVALID_KEY,
                                    "Invalid Groq API key.")
        if resp.status_code == 429:
            return ValidationResult("groq", ValidationStatus.RATE_LIMITED,
                                    "Groq API key is rate-limited.")
        return ValidationResult("groq", ValidationStatus.UNKNOWN_ERROR,
                                f"Groq returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("groq", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching Groq: {e}")


async def _validate_openrouter(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="openrouter",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models[:30],  # cap at 30 for display
                account_info={"total_models": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("openrouter", ValidationStatus.INVALID_KEY,
                                    "Invalid OpenRouter API key.")
        if resp.status_code == 429:
            return ValidationResult("openrouter", ValidationStatus.RATE_LIMITED,
                                    "OpenRouter API key is rate-limited.")
        return ValidationResult("openrouter", ValidationStatus.UNKNOWN_ERROR,
                                f"OpenRouter returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("openrouter", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching OpenRouter: {e}")


async def _validate_together(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.together.xyz/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("id", "") for m in (data if isinstance(data, list) else data.get("data", []))]
            return ValidationResult(
                provider_id="together",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models[:20],
                account_info={"total_models": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("together", ValidationStatus.INVALID_KEY,
                                    "Invalid Together.AI API key.")
        if resp.status_code == 429:
            return ValidationResult("together", ValidationStatus.RATE_LIMITED,
                                    "Together.AI API key is rate-limited.")
        return ValidationResult("together", ValidationStatus.UNKNOWN_ERROR,
                                f"Together.AI returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("together", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching Together.AI: {e}")


async def _validate_fireworks(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.fireworks.ai/inference/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="fireworks",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models[:20],
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("fireworks", ValidationStatus.INVALID_KEY,
                                    "Invalid Fireworks.AI API key.")
        if resp.status_code == 429:
            return ValidationResult("fireworks", ValidationStatus.RATE_LIMITED,
                                    "Fireworks.AI API key is rate-limited.")
        return ValidationResult("fireworks", ValidationStatus.UNKNOWN_ERROR,
                                f"Fireworks.AI returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("fireworks", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching Fireworks.AI: {e}")


async def _validate_deepseek(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.deepseek.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="deepseek",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("deepseek", ValidationStatus.INVALID_KEY,
                                    "Invalid DeepSeek API key.")
        if resp.status_code == 429:
            return ValidationResult("deepseek", ValidationStatus.RATE_LIMITED,
                                    "DeepSeek API key is rate-limited.")
        return ValidationResult("deepseek", ValidationStatus.UNKNOWN_ERROR,
                                f"DeepSeek returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("deepseek", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching DeepSeek: {e}")


async def _validate_mistral(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.mistral.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="mistral",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("mistral", ValidationStatus.INVALID_KEY,
                                    "Invalid Mistral API key.")
        if resp.status_code == 429:
            return ValidationResult("mistral", ValidationStatus.RATE_LIMITED,
                                    "Mistral API key is rate-limited.")
        return ValidationResult("mistral", ValidationStatus.UNKNOWN_ERROR,
                                f"Mistral returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("mistral", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching Mistral: {e}")


async def _validate_cohere(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.cohere.com/v1/models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            return ValidationResult(
                provider_id="cohere",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("cohere", ValidationStatus.INVALID_KEY,
                                    "Invalid Cohere API key.")
        if resp.status_code == 429:
            return ValidationResult("cohere", ValidationStatus.RATE_LIMITED,
                                    "Cohere API key is rate-limited.")
        return ValidationResult("cohere", ValidationStatus.UNKNOWN_ERROR,
                                f"Cohere returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("cohere", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching Cohere: {e}")


async def _validate_nvidia(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://integrate.api.nvidia.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="nvidia",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models[:20],
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("nvidia", ValidationStatus.INVALID_KEY,
                                    "Invalid NVIDIA API key.")
        if resp.status_code == 429:
            return ValidationResult("nvidia", ValidationStatus.RATE_LIMITED,
                                    "NVIDIA API key is rate-limited.")
        return ValidationResult("nvidia", ValidationStatus.UNKNOWN_ERROR,
                                f"NVIDIA NIM returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("nvidia", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching NVIDIA NIM: {e}")


async def _validate_xai(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.x.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="xai",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=models,
                account_info={"model_count": len(models)},
            )
        if resp.status_code == 401:
            return ValidationResult("xai", ValidationStatus.INVALID_KEY,
                                    "Invalid xAI API key.")
        if resp.status_code == 429:
            return ValidationResult("xai", ValidationStatus.RATE_LIMITED,
                                    "xAI API key is rate-limited.")
        return ValidationResult("xai", ValidationStatus.UNKNOWN_ERROR,
                                f"xAI returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("xai", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching xAI: {e}")


async def _validate_huggingface(api_key: str) -> ValidationResult:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://huggingface.co/api/whoami-v2",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            username = data.get("name", "unknown")
            return ValidationResult(
                provider_id="huggingface",
                status=ValidationStatus.OK,
                message="Authenticated successfully",
                models=[],
                account_info={"username": username, "type": data.get("type", "")},
            )
        if resp.status_code == 401:
            return ValidationResult("huggingface", ValidationStatus.INVALID_KEY,
                                    "Invalid HuggingFace token.")
        return ValidationResult("huggingface", ValidationStatus.UNKNOWN_ERROR,
                                f"HuggingFace returned HTTP {resp.status_code}.")
    except Exception as e:
        return ValidationResult("huggingface", ValidationStatus.NETWORK_ERROR,
                                f"Network error reaching HuggingFace: {e}")


async def _validate_ollama(_api_key: str = "") -> ValidationResult:
    """Ollama is local and keyless — just check reachability."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://localhost:11434/api/tags")
        if resp.status_code == 200:
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return ValidationResult(
                provider_id="ollama",
                status=ValidationStatus.OK,
                message="Ollama server reachable",
                models=models,
                account_info={"local": True, "model_count": len(models)},
            )
        return ValidationResult("ollama", ValidationStatus.UNKNOWN_ERROR,
                                f"Ollama server returned HTTP {resp.status_code}.")
    except Exception:
        return ValidationResult("ollama", ValidationStatus.NETWORK_ERROR,
                                "Ollama server is not running. Start it with `ollama serve`.")


async def _validate_lmstudio(_api_key: str = "") -> ValidationResult:
    """LM Studio is local and keyless — just check reachability."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://localhost:1234/v1/models")
        if resp.status_code == 200:
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return ValidationResult(
                provider_id="lmstudio",
                status=ValidationStatus.OK,
                message="LM Studio server reachable",
                models=models,
                account_info={"local": True, "model_count": len(models)},
            )
        return ValidationResult("lmstudio", ValidationStatus.UNKNOWN_ERROR,
                                f"LM Studio returned HTTP {resp.status_code}.")
    except Exception:
        return ValidationResult("lmstudio", ValidationStatus.NETWORK_ERROR,
                                "LM Studio server is not running. Open LM Studio and start the server.")


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_VALIDATORS: dict[str, object] = {
    "openai": _validate_openai,
    "anthropic": _validate_anthropic,
    "google": _validate_google,
    "groq": _validate_groq,
    "openrouter": _validate_openrouter,
    "together": _validate_together,
    "fireworks": _validate_fireworks,
    "deepseek": _validate_deepseek,
    "mistral": _validate_mistral,
    "cohere": _validate_cohere,
    "nvidia": _validate_nvidia,
    "xai": _validate_xai,
    "huggingface": _validate_huggingface,
    "ollama": _validate_ollama,
    "lmstudio": _validate_lmstudio,
}


async def validate_provider(provider_id: str, api_key: str = "") -> ValidationResult:
    """Validate *provider_id* credentials with a live API round-trip.

    Returns a :class:`ValidationResult` whose ``ok`` property is True only when
    authentication succeeded.  Human-readable diagnostics are always available
    via :meth:`ValidationResult.human_message`.
    """
    validator = _VALIDATORS.get(provider_id)
    if validator is None:
        return ValidationResult(
            provider_id=provider_id,
            status=ValidationStatus.UNKNOWN_ERROR,
            message=f"No validator implemented for provider '{provider_id}'.",
        )
    try:
        return await asyncio.wait_for(validator(api_key), timeout=15.0)
    except TimeoutError:
        return ValidationResult(
            provider_id=provider_id,
            status=ValidationStatus.NETWORK_ERROR,
            message=f"Validation timed out for {provider_id}. Check your network connection.",
        )


def validate_provider_sync(provider_id: str, api_key: str = "") -> ValidationResult:
    """Synchronous wrapper around :func:`validate_provider` for non-async contexts."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run, validate_provider(provider_id, api_key)
                )
                return future.result(timeout=20.0)
        return loop.run_until_complete(validate_provider(provider_id, api_key))
    except Exception as e:
        return ValidationResult(
            provider_id=provider_id,
            status=ValidationStatus.UNKNOWN_ERROR,
            message=f"Validation error: {e}",
        )


def supported_providers() -> list[str]:
    """Return provider IDs that have a validator registered."""
    return sorted(_VALIDATORS.keys())
