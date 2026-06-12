from __future__ import annotations

from velune.cli import theme


def test_cli_theme_constants():
    assert hasattr(theme, "ACCENT")
    assert hasattr(theme, "SUCCESS")
    assert hasattr(theme, "WARNING")
    assert hasattr(theme, "ERROR")
    assert hasattr(theme, "DIM")
    assert hasattr(theme, "CODE_BG")

    assert theme.ACCENT == "cyan"
    assert theme.SUCCESS == "green"
    assert theme.WARNING == "yellow"
    assert theme.ERROR == "red"
    assert theme.DIM == "dim"
    assert theme.CODE_BG == "#2e2e2e"
