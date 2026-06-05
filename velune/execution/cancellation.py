"""Clean cancellation primitives for inference streams and background tasks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        await self._event.wait()


class InferenceGuard:
    """
    Wraps any async inference call or stream with clean cancellation.
    On KeyboardInterrupt: cancels cleanly, returns partial content,
    never raises to the caller.
    """

    def __init__(self, console) -> None:
        self.console = console
        self._current_token: CancellationToken | None = None

    def abort(self) -> None:
        if self._current_token:
            self._current_token.cancel()

    @asynccontextmanager
    async def guard(self):
        token = CancellationToken()
        self._current_token = token
        try:
            yield token
        except asyncio.CancelledError:
            self.console.print("\n[dim]↩ Generation cancelled.[/dim]")
        except KeyboardInterrupt:
            token.cancel()
            self.console.print("\n[dim]↩ Generation stopped.[/dim]")
        finally:
            self._current_token = None
