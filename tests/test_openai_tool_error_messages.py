"""The turn after a failed tool call must not be rejected by OpenAI-style providers.

Regression (reproduced live against Groq): the tool loop marks failed tool
results with ``"is_error": True`` and the OpenAI-compatible adapters sent it
as-is. Groq answered ``HTTP 400 'messages.2': property 'is_error' is
unsupported``, so every turn after a denied/failed tool (e.g. a sandbox
rejecting ``mkdir``) died with "rejected the request as malformed". The same
request without the key succeeds. Also, the provider's explanation was
discarded during streaming; it is now part of the error.
"""

from __future__ import annotations

import httpx
import pytest

from velune.core.errors.provider import InvalidRequestError
from velune.core.types.inference import InferenceRequest
from velune.providers.adapters._http_errors import raise_typed_http_error
from velune.providers.adapters._messages import openai_messages
from velune.providers.adapters.groq import GroqProvider

TOOL_ERROR_TURN = [
    {"role": "user", "content": "make a folder"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "execute_command", "arguments": "{}"},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "Error: denied", "is_error": True},
]


def test_internal_is_error_key_is_stripped_without_mutating_input():
    sent = openai_messages(TOOL_ERROR_TURN)
    assert all("is_error" not in m for m in sent)
    assert sent[2]["content"] == "Error: denied"  # the error is still conveyed
    assert TOOL_ERROR_TURN[2]["is_error"] is True  # Anthropic still needs it


def test_groq_payload_never_contains_is_error():
    provider = GroqProvider(api_key="test-key")
    request = InferenceRequest(model_id="m", messages=list(TOOL_ERROR_TURN))
    payload = provider._chat_payload(request)
    assert all("is_error" not in m for m in payload["messages"])


@pytest.mark.parametrize(
    "module, cls",
    [
        ("velune.providers.adapters.openai_compat", None),
        ("velune.providers.adapters.deepseek", None),
        ("velune.providers.adapters.mistral", None),
        ("velune.providers.adapters.nvidia", None),
        ("velune.providers.adapters.lmstudio", None),
        ("velune.providers.adapters.ollama", None),
    ],
)
def test_every_openai_compatible_adapter_uses_the_sanitizer(module, cls):
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module))
    assert '"messages": request.messages' not in source
    assert "openai_messages(request.messages)" in source


def _status_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("bad", request=request, response=response)


def test_400_includes_the_providers_own_reason():
    exc = _status_error(
        400,
        '{"error":{"message":"\'messages.2\' : property \'is_error\' is unsupported",'
        '"type":"invalid_request_error"}}',
    )
    with pytest.raises(InvalidRequestError) as info:
        raise_typed_http_error("groq", exc, "stream")
    assert "property 'is_error' is unsupported" in str(info.value)


def test_unread_streaming_body_does_not_crash_the_error_path():
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    response = httpx.Response(400, request=request, stream=httpx.ByteStream(b"{}"))
    exc = httpx.HTTPStatusError("bad", request=request, response=response)
    with pytest.raises(InvalidRequestError) as info:
        raise_typed_http_error("groq", exc, "stream")
    assert "HTTP 400" in str(info.value)
