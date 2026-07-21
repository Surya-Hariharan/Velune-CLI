"""Alt+Up/Down must not fire while an InlineFlow (``/connect``, ``/providers``)
or the command palette owns the prompt box.

Unlike the command palette, the model switcher's key bindings
(``escape,up`` / ``escape,down``) carry no filter — they are unconditionally
eager. Without an explicit suppression predicate, cycling models mid-``/connect``
pops a second float on top of the flow's panel (both are drawn at z_index=20
in overlapping screen regions) and silently swaps ``repl.active_model`` out
from under the flow via the debounced commit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from velune.cli.model_switcher import ModelSwitcher


@dataclass
class _Model:
    model_id: str
    provider_id: str
    is_local: bool = True
    display_name: str = ""


class _Registry:
    def __init__(self, models: list[_Model]) -> None:
        self._models = models

    def list_all(self) -> list[_Model]:
        return self._models


class _ProviderRegistry:
    def check_provider_available(self, provider_id: str) -> bool:
        return True


class _Container:
    def __init__(self, models: list[_Model]) -> None:
        self._registry = _Registry(models)
        self._provider_registry = _ProviderRegistry()

    def get(self, name: str):
        if name == "runtime.model_registry":
            return self._registry
        if name == "runtime.provider_registry":
            return self._provider_registry
        raise KeyError(name)


class _Repl:
    def __init__(self, models: list[_Model]) -> None:
        self.container = _Container(models)
        self.active_model = models[0]


def _models() -> list[_Model]:
    return [
        _Model("llama3.2", "ollama"),
        _Model("gpt-4o", "openai"),
    ]


def test_move_is_a_no_op_while_suppressed():
    async def _body():
        repl = _Repl(_models())
        original = repl.active_model
        switcher = ModelSwitcher(repl, suppressed=lambda: True)

        switcher.move(1)

        assert repl.active_model is original, "suppressed move() must not touch the active model"
        assert switcher.is_visible() is False

    asyncio.run(_body())


def test_move_works_normally_when_not_suppressed():
    async def _body():
        repl = _Repl(_models())
        switcher = ModelSwitcher(repl, suppressed=lambda: False)

        switcher.move(1)

        assert switcher.is_visible() is True
        assert repl.active_model is not None

    asyncio.run(_body())


def test_is_visible_hides_an_already_open_switcher_once_suppressed():
    """The race where a flow opens mid-cycle, before the debounced commit fires."""

    async def _body():
        repl = _Repl(_models())
        flag = {"suppressed": False}
        switcher = ModelSwitcher(repl, suppressed=lambda: flag["suppressed"])

        switcher.move(1)
        assert switcher.is_visible() is True

        flag["suppressed"] = True
        assert switcher.is_visible() is False, (
            "an open switcher must yield once something else owns the prompt box"
        )

    asyncio.run(_body())


def test_commit_reverts_instead_of_activating_when_suppressed_mid_cycle():
    async def _body():
        repl = _Repl(_models())
        original = repl.active_model
        flag = {"suppressed": False}
        switcher = ModelSwitcher(repl, suppressed=lambda: flag["suppressed"])

        switcher.move(1)
        assert repl.active_model is not original  # live preview applied

        # A flow opened between the keypress and the debounce firing.
        flag["suppressed"] = True
        switcher._commit()

        assert repl.active_model is original, (
            "commit must revert the preview rather than activate a model behind a flow"
        )
        assert switcher.is_visible() is False

    asyncio.run(_body())


def test_default_suppressed_predicate_never_blocks():
    """No predicate supplied (e.g. a bare unit test constructing ModelSwitcher)
    must behave exactly as before this fix."""

    async def _body():
        repl = _Repl(_models())
        switcher = ModelSwitcher(repl)

        switcher.move(1)

        assert switcher.is_visible() is True

    asyncio.run(_body())
