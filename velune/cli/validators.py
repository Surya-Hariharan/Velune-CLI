"""Real-time input validation for the Velune REPL.

InlineSyntaxValidator hooks into prompt_toolkit's validate_while_typing
to highlight Python syntax errors as the user types — but only when the
input actually looks like Python code, not a natural-language prompt.
"""

from __future__ import annotations

from prompt_toolkit.document import Document
from prompt_toolkit.validation import ValidationError, Validator

# Lines that could be the start of a Python statement.
_PYTHON_STARTERS: frozenset[str] = frozenset(
    {
        "def ",
        "async def ",
        "class ",
        "import ",
        "from ",
        "for ",
        "async for ",
        "while ",
        "if ",
        "try:",
        "try :",
        "with ",
        "async with ",
        "return ",
        "yield ",
        "raise ",
        "assert ",
        "lambda ",
        "@",  # decorator
    }
)


class InlineSyntaxValidator(Validator):
    """Validates Python-looking REPL input on each keystroke.

    Plain natural-language prompts are not validated — only input whose
    first non-empty line begins with a Python statement keyword.  This
    avoids false positives on everyday chat messages.
    """

    def validate(self, document: Document) -> None:
        text = document.text
        if not text.strip():
            return
        if not self._looks_like_python(text):
            return
        try:
            compile(text, "<repl-input>", "exec")
        except SyntaxError as exc:
            raise ValidationError(
                message=f"SyntaxError: {exc.msg}",
                cursor_position=self._error_offset(document, exc),
            ) from exc
        except ValueError:
            # compile() raises ValueError for null bytes — ignore
            pass

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _looks_like_python(self, text: str) -> bool:
        first = text.lstrip().split("\n")[0].lstrip()
        return any(first.startswith(s) for s in _PYTHON_STARTERS)

    def _error_offset(self, document: Document, exc: SyntaxError) -> int:
        """Convert SyntaxError (lineno, offset) to an absolute buffer offset."""
        lines = document.text.split("\n")
        lineno = max((exc.lineno or 1) - 1, 0)
        col = max((exc.offset or 1) - 1, 0)
        # Sum lengths of preceding lines (+1 for each newline character)
        prefix = sum(len(line) + 1 for line in lines[:lineno])
        return min(prefix + col, len(document.text))
