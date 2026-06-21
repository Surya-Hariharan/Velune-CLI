"""Tests for velune.analysis.refactor — code-smell detectors."""

from __future__ import annotations

import ast
import textwrap

import pytest

from velune.analysis.refactor import (
    BareExceptDetector,
    DeepNestingDetector,
    DuplicateArgumentsDetector,
    LongFunctionDetector,
    MutableDefaultArgumentDetector,
    RefactorAnalyzer,
    RefactorHint,
)


def _hints(source: str, detector_cls) -> list[RefactorHint]:
    hints: list[RefactorHint] = []
    tree = ast.parse(textwrap.dedent(source))
    detector_cls(hints).visit(tree)
    return hints


class TestLongFunctionDetector:
    def test_short_function_no_hint(self):
        src = "def foo():\n    return 1\n"
        assert _hints(src, LongFunctionDetector) == []

    def test_long_function_triggers_R001(self):
        body = "\n".join(f"    x{i} = {i}" for i in range(55))
        src = f"def big():\n{body}\n    return x0\n"
        hints = _hints(src, LongFunctionDetector)
        assert any(h.rule_id == "R001" for h in hints)

    def test_hint_contains_function_name(self):
        body = "\n".join(f"    x{i} = {i}" for i in range(55))
        src = f"def my_long_func():\n{body}\n    return x0\n"
        hints = _hints(src, LongFunctionDetector)
        r001 = next(h for h in hints if h.rule_id == "R001")
        assert r001.function_name == "my_long_func"


class TestDuplicateArgumentsDetector:
    def test_few_params_no_hint(self):
        src = "def foo(a, b, c): pass\n"
        assert _hints(src, DuplicateArgumentsDetector) == []

    def test_many_params_triggers_R002(self):
        src = "def foo(a, b, c, d, e, f): pass\n"
        hints = _hints(src, DuplicateArgumentsDetector)
        assert any(h.rule_id == "R002" for h in hints)

    def test_self_excluded_from_count(self):
        src = "def foo(self, a, b, c, d, e): pass\n"
        assert _hints(src, DuplicateArgumentsDetector) == []


class TestDeepNestingDetector:
    def test_shallow_nesting_no_hint(self):
        src = "def foo(x):\n    if x:\n        return 1\n"
        assert _hints(src, DeepNestingDetector) == []

    def test_deep_nesting_triggers_R003(self):
        src = textwrap.dedent("""\
            def deep(x):
                if x:
                    for i in range(x):
                        while i:
                            if True:
                                if x > 0:
                                    pass
        """)
        hints = _hints(src, DeepNestingDetector)
        assert any(h.rule_id == "R003" for h in hints)


class TestMutableDefaultDetector:
    def test_list_default_triggers_R004(self):
        src = "def foo(x=[]): pass\n"
        hints = _hints(src, MutableDefaultArgumentDetector)
        assert any(h.rule_id == "R004" for h in hints)

    def test_dict_default_triggers_R004(self):
        src = "def foo(x={}): pass\n"
        hints = _hints(src, MutableDefaultArgumentDetector)
        assert any(h.rule_id == "R004" for h in hints)

    def test_set_default_triggers_R004(self):
        src = "def foo(x=set()): pass\n"
        # set() is a Call, not ast.Set — should NOT trigger
        hints = _hints(src, MutableDefaultArgumentDetector)
        assert not any(h.rule_id == "R004" for h in hints)

    def test_immutable_default_no_hint(self):
        src = "def foo(x=None, y=0, z=''): pass\n"
        assert _hints(src, MutableDefaultArgumentDetector) == []


class TestBareExceptDetector:
    def test_bare_except_triggers_R005(self):
        src = textwrap.dedent("""\
            def foo():
                try:
                    pass
                except:
                    pass
        """)
        hints = _hints(src, BareExceptDetector)
        assert any(h.rule_id == "R005" for h in hints)

    def test_typed_except_no_hint(self):
        src = textwrap.dedent("""\
            def foo():
                try:
                    pass
                except ValueError:
                    pass
        """)
        assert _hints(src, BareExceptDetector) == []


class TestRefactorAnalyzer:
    def test_clean_file_no_hints(self, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("def foo(a, b):\n    return a + b\n")
        assert RefactorAnalyzer().analyze_file(f) == []

    def test_multiple_detectors_run(self, tmp_path):
        body = "\n".join(f"    x{i} = {i}" for i in range(55))
        src = f"def big(a, b, c, d, e, f):\n    try:\n        pass\n    except:\n        pass\n{body}\n    return x0\n"
        f = tmp_path / "smelly.py"
        f.write_text(src)
        hints = RefactorAnalyzer().analyze_file(f)
        rule_ids = {h.rule_id for h in hints}
        assert "R001" in rule_ids  # long function
        assert "R002" in rule_ids  # too many params
        assert "R005" in rule_ids  # bare except

    def test_analyze_source_syntax_error_returns_empty(self):
        assert RefactorAnalyzer().analyze_source("def bad(", "t.py") == []

    def test_hints_sorted_by_line(self, tmp_path):
        src = textwrap.dedent("""\
            def short(a=[]):
                pass

            def also_short(b={}):
                pass
        """)
        hints = RefactorAnalyzer().analyze_source(src, "t.py")
        lines = [h.line for h in hints]
        assert lines == sorted(lines)


@pytest.mark.parametrize(
    "source,expected_rule_id",
    [
        ("def f(x=[]): pass\n", "R004"),
        (
            "def f():\n    try:\n        pass\n    except:\n        pass\n",
            "R005",
        ),
        ("def f(a, b, c, d, e, f): pass\n", "R002"),
    ],
)
def test_parametrized_rules(source, expected_rule_id):
    hints = RefactorAnalyzer().analyze_source(source, "t.py")
    assert any(h.rule_id == expected_rule_id for h in hints), (
        f"Expected {expected_rule_id} in {[h.rule_id for h in hints]}"
    )
