"""Tool-calling wire conversion for Cohere and Google Gemini adapters.

Mistral and DeepSeek reuse the shared OpenAI-format helpers exercised by
test_streaming_tools.py; Cohere and Gemini need their own conversions since
neither speaks the OpenAI chat wire format.
"""

from __future__ import annotations

from velune.providers.adapters.cohere import (
    _parse_cohere_tool_calls,
    _to_cohere_messages,
    _to_cohere_tools,
)
from velune.providers.adapters.deepseek import DeepSeekProvider
from velune.providers.adapters.google import (
    _build_contents,
    _parse_gemini_tool_calls,
    _to_gemini_tool_config,
    _to_gemini_tools,
)
from velune.providers.adapters.google import GoogleProvider
from velune.providers.adapters.mistral import MistralProvider

_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "limit": {"type": "integer", "description": "Max lines"},
                },
                "required": ["path"],
            },
        },
    }
]


def test_streaming_marker_set_on_openai_compatible_adapters():
    assert MistralProvider.SUPPORTS_STREAMING_TOOL_CALLS is True
    assert DeepSeekProvider.SUPPORTS_STREAMING_TOOL_CALLS is True


# ── Cohere ───────────────────────────────────────────────────────────────────


def test_cohere_tool_conversion_maps_json_schema_types():
    converted = _to_cohere_tools(_OPENAI_TOOLS)
    assert converted == [
        {
            "name": "read_file",
            "description": "Read a file",
            "parameter_definitions": {
                "path": {"description": "File path", "type": "str", "required": True},
                "limit": {"description": "Max lines", "type": "int", "required": False},
            },
        }
    ]
    assert _to_cohere_tools(None) is None


def test_cohere_parse_tool_calls_synthesizes_ids():
    calls = _parse_cohere_tool_calls([{"name": "read_file", "parameters": {"path": "a.py"}}])
    assert calls is not None
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.py"}
    assert calls[0].id.startswith("call_")
    assert _parse_cohere_tool_calls(None) is None
    assert _parse_cohere_tool_calls([]) is None


def test_cohere_messages_round_trip_tool_turn():
    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "read a.py"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
    ]
    preamble, history, message, tool_results = _to_cohere_messages(messages)

    assert preamble == "be helpful"
    assert history == [
        {"role": "USER", "message": "read a.py"},
        {
            "role": "CHATBOT",
            "tool_calls": [{"name": "read_file", "parameters": {"path": "a.py"}}],
        },
    ]
    # Trailing tool result surfaces as top-level tool_results, not history —
    # that's how Cohere expects to be asked to continue after a tool call.
    assert message == ""
    assert tool_results == [
        {
            "call": {"name": "read_file", "parameters": {"path": "a.py"}},
            "outputs": [{"result": "file contents"}],
        }
    ]


def test_cohere_messages_historical_tool_turn_flushes_to_history():
    messages = [
        {"role": "user", "content": "read a.py"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
        {"role": "assistant", "content": "here it is"},
        {"role": "user", "content": "thanks, now read b.py"},
    ]
    preamble, history, message, tool_results = _to_cohere_messages(messages)

    assert tool_results is None
    assert message == "thanks, now read b.py"
    assert {"role": "TOOL", "tool_results": [{"call": {"name": "read_file", "parameters": {}}, "outputs": [{"result": "contents"}]}]} in history
    assert history[-1] == {"role": "CHATBOT", "message": "here it is"}


# ── Google Gemini ────────────────────────────────────────────────────────────


def test_gemini_streaming_marker_set():
    assert GoogleProvider.SUPPORTS_STREAMING_TOOL_CALLS is True


def test_gemini_tool_conversion_wraps_function_declarations():
    converted = _to_gemini_tools(_OPENAI_TOOLS)
    assert converted == [
        {
            "functionDeclarations": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": _OPENAI_TOOLS[0]["function"]["parameters"],
                }
            ]
        }
    ]
    assert _to_gemini_tools(None) is None


def test_gemini_tool_config_modes():
    assert _to_gemini_tool_config(None) is None
    assert _to_gemini_tool_config("auto") is None
    assert _to_gemini_tool_config("none") == {"functionCallingConfig": {"mode": "NONE"}}
    assert _to_gemini_tool_config("required") == {"functionCallingConfig": {"mode": "ANY"}}
    assert _to_gemini_tool_config({"type": "function", "function": {"name": "read_file"}}) == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["read_file"]}
    }


def test_gemini_parse_tool_calls_from_parts():
    parts = [{"text": "checking"}, {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}]
    calls = _parse_gemini_tool_calls(parts)
    assert calls is not None
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.py"}
    assert _parse_gemini_tool_calls([{"text": "no calls here"}]) is None


def test_gemini_build_contents_round_trip_tool_turn():
    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "read a.py"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
    ]
    contents, system_text = _build_contents(messages)

    assert system_text == "be helpful"
    assert contents[0] == {"role": "user", "parts": [{"text": "read a.py"}]}
    assert contents[1] == {
        "role": "model",
        "parts": [{"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}],
    }
    assert contents[2] == {
        "role": "user",
        "parts": [
            {"functionResponse": {"name": "read_file", "response": {"content": "file contents"}}}
        ],
    }
