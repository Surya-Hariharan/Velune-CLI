"""Event routing rules."""

from collections.abc import Callable

from velune.events.bus.engine import Event


class EventRouter:
    """Routes events to appropriate handlers based on rules."""

    def __init__(self):
        self._routing_rules: dict[str, Callable[[Event], bool]] = {}
        self._handlers: dict[str, list[Callable]] = {}

    def add_rule(self, rule_name: str, condition: Callable[[Event], bool]) -> None:
        """Add a routing rule."""
        self._routing_rules[rule_name] = condition

    def add_handler(self, rule_name: str, handler: Callable[[Event], None]) -> None:
        """Add a handler for a rule."""
        if rule_name not in self._handlers:
            self._handlers[rule_name] = []
        self._handlers[rule_name].append(handler)

    def route(self, event: Event) -> list[Callable]:
        """Route event to matching handlers."""
        matching_handlers = []

        for rule_name, condition in self._routing_rules.items():
            if condition(event):
                if rule_name in self._handlers:
                    matching_handlers.extend(self._handlers[rule_name])

        return matching_handlers
