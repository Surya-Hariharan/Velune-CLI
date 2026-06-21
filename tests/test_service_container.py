"""Kernel ServiceContainer registration and resolution semantics."""

from __future__ import annotations

import pytest

from velune.kernel.registry import ServiceContainer, get_container


class TestServiceContainer:
    def test_singleton_factory_called_once(self) -> None:
        container = ServiceContainer()
        calls = []
        container.register("svc", lambda: calls.append(1) or object(), singleton=True)
        first = container.get("svc")
        second = container.get("svc")
        assert first is second
        assert len(calls) == 1

    def test_non_singleton_factory_called_each_time(self) -> None:
        container = ServiceContainer()
        container.register("svc", lambda: object(), singleton=False)
        assert container.get("svc") is not container.get("svc")

    def test_register_instance(self) -> None:
        container = ServiceContainer()
        sentinel = object()
        container.register_instance("svc", sentinel)
        assert container.get("svc") is sentinel

    def test_instance_takes_priority_over_factory(self) -> None:
        container = ServiceContainer()
        container.register("svc", lambda: "from-factory")
        sentinel = object()
        container.register_instance("svc", sentinel)
        assert container.get("svc") is sentinel

    def test_hot_swap_replaces_resolved_singleton(self) -> None:
        container = ServiceContainer()
        container.register("svc", lambda: "original")
        assert container.get("svc") == "original"
        container.hot_swap("svc", "replacement")
        assert container.get("svc") == "replacement"

    def test_missing_service_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="not registered"):
            ServiceContainer().get("nope")

    def test_has(self) -> None:
        container = ServiceContainer()
        assert not container.has("svc")
        container.register("svc", lambda: 1)
        assert container.has("svc")

    def test_clear_purges_everything(self) -> None:
        container = ServiceContainer()
        container.register("a", lambda: 1)
        container.register_instance("b", 2)
        container.clear()
        assert not container.has("a")
        assert not container.has("b")

    def test_global_container_is_stable(self) -> None:
        assert get_container() is get_container()
