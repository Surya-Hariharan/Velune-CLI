"""Strict, pure parsing of a model reply into a draft, and the one-shot repair request.

Parsing is deliberately not lenient: the reply must be one JSON object (a single surrounding code
fence is tolerated, trailing prose is not). Errors are reduced to ``location: message`` strings so
nothing of the model's text is echoed into traces or repair requests beyond field paths.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TypeVar

from pydantic import ValidationError

from velune.council.ports import SeatMessage
from velune.council.serialization import Contract

D = TypeVar("D", bound=Contract)
R = TypeVar("R")

MAX_ERRORS = 8
MAX_ERROR_LENGTH = 160
MAX_PREVIOUS_REPLY = 8000

_FENCE = re.compile(r"^```[A-Za-z]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```$", re.DOTALL)


class DraftParseError(Exception):
    """The reply was not a valid draft. ``errors`` are short ``location: message`` strings."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _clip(text: str) -> str:
    return text if len(text) <= MAX_ERROR_LENGTH else text[: MAX_ERROR_LENGTH - 1] + "…"


def strip_fence(text: str) -> str:
    """Remove one surrounding code fence, if the whole reply is a single fenced block."""
    body = text.strip()
    match = _FENCE.match(body)
    return match.group("body").strip() if match else body


def _summarize(exc: ValidationError) -> tuple[str, ...]:
    out: list[str] = []
    for item in exc.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(part) for part in item["loc"]) or "reply"
        out.append(_clip(f"{where}: {item['msg']}"))
    return tuple(out[:MAX_ERRORS])


def parse_draft(text: str, model: type[D]) -> D:
    """Validate ``text`` as exactly one ``model`` object, or raise ``DraftParseError``."""
    body = strip_fence(text)
    if not (body.startswith("{") and body.endswith("}")):
        raise DraftParseError(("reply: expected exactly one JSON object and nothing else",))
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise DraftParseError(_summarize(exc) or ("reply: invalid",)) from None


def parse_and_convert(text: str, model: type[D], convert: Callable[[D], R]) -> R:
    """Parse a draft and convert it to its contract; either step failing is a ``DraftParseError``."""
    draft = parse_draft(text, model)
    try:
        return convert(draft)
    except ValidationError as exc:
        raise DraftParseError(_summarize(exc) or ("reply: invalid",)) from None
    except ValueError as exc:
        raise DraftParseError((_clip(str(exc)),)) from None


def repair_messages(
    original: tuple[SeatMessage, ...], previous_reply: str, errors: tuple[str, ...]
) -> tuple[SeatMessage, ...]:
    """The single repair request: the original messages, the seat's own reply, and the errors.

    Nothing else is added, so a repair can never carry another seat's output.
    """
    note = (
        "Your previous reply was rejected: "
        + "; ".join(errors)
        + ". Reply again with exactly one JSON object that matches the schema, and nothing else."
    )
    return (
        *original,
        SeatMessage(role="assistant", content=previous_reply[:MAX_PREVIOUS_REPLY]),
        SeatMessage(role="user", content=note),
    )
