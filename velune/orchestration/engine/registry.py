"""An explicit set of agent specs, keyed by role.

The registry starts empty and only grows through :meth:`AgentRegistry.register`. There is no
default agent, no replacement and no global instance; whoever builds the engine decides which agents
exist.
"""

from __future__ import annotations

from collections.abc import Iterable

from velune.core.types.agent import AgentRole
from velune.orchestration.engine.errors import DuplicateAgentError, UnknownAgentError
from velune.orchestration.engine.spec import AgentSpec


class AgentRegistry:
    def __init__(self, specs: Iterable[AgentSpec] = ()) -> None:
        self._specs: dict[AgentRole, AgentSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: AgentSpec) -> None:
        if spec.role in self._specs:
            raise DuplicateAgentError(spec.role.value)
        self._specs[spec.role] = spec

    def get(self, role: AgentRole | str) -> AgentSpec:
        try:
            key = AgentRole(role)
        except ValueError:
            raise UnknownAgentError(str(role), self.roles()) from None
        try:
            return self._specs[key]
        except KeyError:
            raise UnknownAgentError(key.value, self.roles()) from None

    def roles(self) -> tuple[str, ...]:
        return tuple(sorted(role.value for role in self._specs))

    def specs(self) -> tuple[AgentSpec, ...]:
        """A snapshot in registration order; mutating it cannot change the registry."""
        return tuple(self._specs.values())

    def __contains__(self, role: object) -> bool:
        if isinstance(role, str):
            try:
                role = AgentRole(role)
            except ValueError:
                return False
        return role in self._specs

    def __len__(self) -> int:
        return len(self._specs)
