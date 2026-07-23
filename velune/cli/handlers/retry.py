"""/retry — regenerate the last assistant response, optionally on a different model."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_POPPABLE_ROLES = {"assistant", "tool", "system"}


def _pop_last_turn(repl: VeluneREPL) -> str | None:
    """Pop the most recent (user, [tool/system]*, assistant) group and return
    the user's text — or None if there's no prior user turn to retry.

    The pair isn't always the last two entries: a ``tool`` activity entry (and
    occasionally a mention/hook ``system`` entry) can sit between the user's
    prompt and the assistant's reply when the native tool loop ran tools
    (see ``_handle_prompt``'s ``self._conversation.append({"role": "tool", ...})``
    calls). Walk backward past those before looking for the user entry.
    """
    conversation = repl._conversation
    popped: list[dict] = []
    saw_assistant = False
    while conversation and conversation[-1].get("role") in _POPPABLE_ROLES:
        entry = conversation.pop()
        if entry.get("role") == "assistant":
            saw_assistant = True
        popped.append(entry)

    # Require an actual assistant reply among what we popped — a dangling
    # user prompt with no response yet (mid-flight, or an odd interrupt
    # state) isn't a completed exchange to redo; there's nothing to
    # "regenerate."
    if not saw_assistant or not conversation or conversation[-1].get("role") != "user":
        # Nothing retriable — put back whatever we removed so /retry is a
        # true no-op rather than silently discarding trailing context.
        while popped:
            conversation.append(popped.pop())
        return None

    return conversation.pop().get("content", "")


async def cmd_retry(repl: VeluneREPL, args: str) -> None:
    """/retry [model] — regenerate the last response, optionally on another model."""
    model_override = None
    provider_override = None

    model_name = args.strip()
    if model_name:
        model_registry = repl._require("runtime.model_registry", "model registry")
        if model_registry is None:
            return
        provider_registry = repl._require("runtime.provider_registry", "provider registry")
        if provider_registry is None:
            return

        model_override = model_registry.get(model_name)
        if model_override is None:
            from velune.cli.rendering.error_panel import render_error
            from velune.core.errors.catalog import ModelNotFoundError

            repl.console.print(render_error(ModelNotFoundError(f"'{model_name}'")))
            return

        provider_override = provider_registry.get(model_override.provider_id)
        if provider_override is None:
            repl.console.print(f"[red]No active provider for '{model_override.provider_id}'.[/red]")
            return

    user_text = _pop_last_turn(repl)
    if user_text is None:
        repl.console.print("[dim]Nothing to retry — no previous turn in this session.[/dim]")
        return

    if model_override is not None:
        repl.console.print(f"[dim]Retrying on {model_override.model_id}…[/dim]")
    else:
        repl.console.print("[dim]Retrying…[/dim]")

    await repl._handle_prompt(
        user_text, model_override=model_override, provider_override=provider_override
    )
