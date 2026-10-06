"""run_async must not print asyncio tracebacks for prompt_toolkit's exit-time executor race."""

from __future__ import annotations

import asyncio
import logging

from velune.kernel.entrypoint import run_async

RACE = RuntimeError("Executor shutdown has been called")


def test_executor_shutdown_race_is_muted(caplog):
    async def main() -> str:
        asyncio.get_running_loop().call_exception_handler({"message": "cb", "exception": RACE})
        return "ok"

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        assert run_async(main()) == "ok"

    assert not [r for r in caplog.records if r.name == "asyncio"]


def test_other_loop_errors_still_reach_asyncio(caplog):
    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.call_exception_handler({"message": "boom", "exception": ValueError("real bug")})

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        run_async(main())

    assert any("boom" in r.getMessage() for r in caplog.records if r.name == "asyncio")
