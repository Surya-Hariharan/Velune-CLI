"""Tests for velune.analysis.type_inferrer.TypeInferrer."""

from __future__ import annotations

import pytest

from velune.analysis.type_inferrer import TypeInferrer, TypeSuggestion


@pytest.fixture()
def inferrer():
    return TypeInferrer()


class TestTypeInferrer:
    def test_already_annotated_skipped(self, inferrer):
        src = "def foo(x: int) -> str:\n    return str(x)\n"
        assert inferrer.infer_source(src) == []

    def test_bool_return_inferred(self, inferrer):
        src = "def is_valid(x):\n    return True\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "bool"

    def test_str_return_inferred(self, inferrer):
        src = "def greet():\n    return 'hello'\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "str"

    def test_int_return_inferred(self, inferrer):
        src = "def count():\n    return 42\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "int"

    def test_none_return_inferred(self, inferrer):
        src = "def noop():\n    pass\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "None"

    def test_list_return_inferred(self, inferrer):
        src = "def items():\n    return [1, 2, 3]\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "list"

    def test_dict_return_inferred(self, inferrer):
        src = "def mapping():\n    return {'key': 'value'}\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "dict"

    def test_generator_detected(self, inferrer):
        src = "def gen():\n    yield 1\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "Generator"

    def test_param_bool_default(self, inferrer):
        src = "def foo(flag=False):\n    pass\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert "flag" in sug[0].param_suggestions
        assert sug[0].param_suggestions["flag"] == "bool"

    def test_param_int_default(self, inferrer):
        src = "def foo(count=0):\n    pass\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].param_suggestions.get("count") == "int"

    def test_param_str_default(self, inferrer):
        src = "def foo(name=''):\n    pass\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].param_suggestions.get("name") == "str"

    def test_multiple_return_types_union(self, inferrer):
        src = "def maybe(x):\n    if x:\n        return 'yes'\n    return None\n"
        sug = inferrer.infer_source(src)
        assert sug
        ret = sug[0].return_suggestion
        assert ret is not None
        assert "str" in ret
        assert "None" in ret

    def test_empty_function_no_params_returns_none(self, inferrer):
        src = "def empty():\n    pass\n"
        sug = inferrer.infer_source(src)
        assert sug
        assert sug[0].return_suggestion == "None"

    def test_infer_file_roundtrip(self, inferrer, tmp_path):
        f = tmp_path / "sample.py"
        f.write_text("def add(a, b):\n    return 1\n")
        sug = inferrer.infer_file(f)
        assert sug

    def test_infer_file_missing_returns_empty(self, inferrer, tmp_path):
        assert inferrer.infer_file(tmp_path / "nofile.py") == []

    def test_render_suggestions_produces_diff(self, inferrer):
        src = "def foo():\n    return True\n"
        sug = inferrer.infer_source(src)
        diff = inferrer._render_suggestions(src, sug)
        assert "---" in diff or "no changes" in diff

    def test_apply_suggestions_patches_source(self, inferrer):
        src = "def foo():\n    return True\n"
        sug = inferrer.infer_source(src)
        patched = inferrer.apply_suggestions(src, sug)
        assert "-> bool" in patched

    def test_syntax_error_returns_empty(self, inferrer):
        assert inferrer.infer_source("def bad(") == []

    def test_suggestions_sorted_by_line(self, inferrer):
        src = (
            "def a():\n    return 1\n\n"
            "def b():\n    return 'x'\n"
        )
        sug = inferrer.infer_source(src)
        lines = [s.line for s in sug]
        assert lines == sorted(lines)
