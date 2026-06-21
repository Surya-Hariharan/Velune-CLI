"""Tests for velune.cli.validators.InlineSyntaxValidator."""

from __future__ import annotations

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.validation import ValidationError

from velune.cli.validators import InlineSyntaxValidator


@pytest.fixture()
def validator():
    return InlineSyntaxValidator()


class TestInlineSyntaxValidator:
    def test_empty_input_passes(self, validator):
        validator.validate(Document(""))

    def test_plain_text_not_validated(self, validator):
        # Natural language should never raise
        validator.validate(Document("explain how auth works"))
        validator.validate(Document("what is the difference between jwt and session"))

    def test_valid_python_function_passes(self, validator):
        src = "def foo(x):\n    return x + 1"
        validator.validate(Document(src))

    def test_valid_import_passes(self, validator):
        validator.validate(Document("import os"))

    def test_valid_class_passes(self, validator):
        src = "class Foo:\n    pass"
        validator.validate(Document(src))

    def test_syntax_error_raises_validation_error(self, validator):
        with pytest.raises(ValidationError) as exc_info:
            validator.validate(Document("def foo(x\n    return x"))
        assert "SyntaxError" in exc_info.value.message

    def test_missing_colon_caught(self, validator):
        with pytest.raises(ValidationError):
            validator.validate(Document("def foo()"))

    def test_multiline_valid_function_passes(self, validator):
        src = "def greet(name: str) -> str:\n    return f'Hello {name}'"
        validator.validate(Document(src))

    def test_for_loop_validated(self, validator):
        src = "for x in range(10"  # missing closing paren — syntax error
        with pytest.raises(ValidationError):
            validator.validate(Document(src))

    def test_error_offset_is_non_negative(self, validator):
        src = "def bad("
        try:
            validator.validate(Document(src))
        except ValidationError as exc:
            assert exc.cursor_position >= 0

    def test_async_def_validated(self, validator):
        src = "async def handler("  # incomplete
        with pytest.raises(ValidationError):
            validator.validate(Document(src))

    def test_decorator_line_validated(self, validator):
        src = "@property\ndef foo(self"  # missing closing paren
        with pytest.raises(ValidationError):
            validator.validate(Document(src))
