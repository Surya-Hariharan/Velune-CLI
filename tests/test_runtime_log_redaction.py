"""The real CLI runtime masks registered secrets in its log output.

``SecretRedactingFilter`` used to be installed only by ``core.logging.configure_logging``,
which nothing calls; the runtime's ``logging.basicConfig`` handler had no filter, so a
value registered with ``register_secret_value`` (a database password, an API token)
reached stderr in clear text. Separately, a secret inside an exception was printed in
the traceback even with the filter installed, because the formatter renders
``record.exc_info`` after filters run.

The end-to-end cases run ``build_runtime`` in a fresh interpreter: only there does the
root logger start without handlers, so ``basicConfig`` installs the same stderr handler
the CLI gets (under pytest the root logger always carries pytest's capture handlers).
The in-process cases cover handlers that already exist, repeated initialization and
unchanged formatting, with the global logging state saved and restored around each.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from velune.core.redaction import (
    REDACTION_PLACEHOLDER,
    SecretRedactingFilter,
    install_log_redaction,
    register_secret_value,
    unregister_secret_value,
)
from velune.core.runtime import build_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]

# Registered values have no recognizable key shape, so only the runtime registry can
# mask them; the pattern rules in ``redact_secrets`` would let them through.
FIRST = "hush-" + "alpha" + "-7731"
SECOND = "hush-" + "bravo" + "-4402"


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    (home / "Local").mkdir(parents=True, exist_ok=True)
    (home / "Roaming").mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        LOCALAPPDATA=str(home / "Local"),
        APPDATA=str(home / "Roaming"),
        VELUNE_DATA_HOME=str(tmp_path / "data"),
        PYTHONPATH=str(REPO_ROOT),
        PYTHONIOENCODING="utf-8",
    )
    return env


# ── the CLI's own logging path, end to end ───────────────────────────────────

_CHILD = textwrap.dedent(
    """
    import json, logging, sys
    from pathlib import Path

    first, second, workspace = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    assert not logging.getLogger().handlers, "the root logger must start empty, as in the CLI"

    from velune.core.redaction import register_secret_value, SecretRedactingFilter
    from velune.core.runtime import build_runtime

    build_runtime(workspace, defer_background=True)
    root = logging.getLogger()
    log = logging.getLogger("velune.probe")
    register_secret_value(first)
    register_secret_value(second)

    def case(name, emit):
        sys.stderr.write(f"\\n@@case {name}\\n")
        emit()
        sys.stderr.flush()

    def raising(message):
        raise RuntimeError(message)

    def in_traceback():
        try:
            raising(f"authentication failed with {first}")
        except RuntimeError:
            log.exception("connector failed")

    def exc_info_object():
        try:
            raising(f"driver rejected {second}")
        except RuntimeError as exc:
            log.error("query failed", exc_info=exc)

    def chained():
        try:
            try:
                raise KeyError(first)
            except KeyError as inner:
                raise ValueError("lookup failed") from inner
        except ValueError:
            log.warning("retrying", exc_info=True)

    def plain_traceback():
        try:
            raising("nothing secret here")
        except RuntimeError:
            log.exception("ordinary failure")

    case("embedded", lambda: log.warning(f"connecting with password {first} to db"))
    case("argument", lambda: log.warning("connecting with password %s to db", first))
    case("two", lambda: log.error("primary %s, replica %s", first, second))
    case("traceback", in_traceback)
    case("exc_info_object", exc_info_object)
    case("chained", chained)
    case("stack", lambda: log.warning("checkpoint %s", first, stack_info=True))
    case("plain", lambda: log.warning("plain message %d", 42))
    case("debug", lambda: log.debug("debug detail %s", "hidden"))
    case("plain_traceback", plain_traceback)

    print(json.dumps({
        "handlers": [
            {
                "type": type(h).__name__,
                "stream": getattr(getattr(h, "stream", None), "name", None),
                "filters": sum(isinstance(f, SecretRedactingFilter) for f in h.filters),
                "format": h.formatter._fmt if h.formatter else None,
            }
            for h in root.handlers
        ],
        "root_level": root.level,
    }))
    """
)


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory):
    """Run the probe once; return ``(stderr split by case, handler summary)``."""
    tmp = tmp_path_factory.mktemp("runtime_log_redaction")
    workspace = tmp / "ws"
    workspace.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, FIRST, SECOND, str(workspace)],
        cwd=REPO_ROOT,
        env=_isolated_env(tmp),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    cases: dict[str, str] = {}
    for chunk in proc.stderr.split("\n@@case ")[1:]:
        name, _, body = chunk.partition("\n")
        cases[name] = body
    return cases, json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "case", ["embedded", "argument", "two", "traceback", "exc_info_object", "chained", "stack"]
)
def test_the_runtime_masks_registered_secrets_in_log_output(cli_run, case):
    cases, _ = cli_run
    output = cases[case]
    assert output.strip(), f"{case}: nothing was logged"
    assert FIRST not in output and SECOND not in output, output
    assert REDACTION_PLACEHOLDER in output, output


def test_both_secrets_in_one_record_are_masked(cli_run):
    cases, _ = cli_run
    assert cases["two"].count(REDACTION_PLACEHOLDER) == 2, cases["two"]


def test_a_redacted_traceback_keeps_its_frames_and_exception_type(cli_run):
    cases, _ = cli_run
    output = cases["traceback"]
    assert "Traceback (most recent call last)" in output
    assert "in raising" in output  # the frame that raised is still shown
    assert f"RuntimeError: authentication failed with {REDACTION_PLACEHOLDER}" in output
    assert "connector failed" in output
    chained = cases["chained"]
    assert "The above exception was the direct cause" in chained and "ValueError" in chained


def test_the_cli_handler_is_unchanged_apart_from_the_filter(cli_run):
    cases, summary = cli_run
    (handler,) = summary["handlers"]
    assert handler["type"] == "StreamHandler" and handler["stream"] == "<stderr>"
    assert handler["filters"] == 1
    assert handler["format"] == logging.BASIC_FORMAT
    assert summary["root_level"] == logging.WARNING
    # Ordinary records are formatted exactly as before and the level still filters.
    assert cases["plain"].strip() == "WARNING:velune.probe:plain message 42"
    assert cases["debug"].strip() == ""
    plain = cases["plain_traceback"]
    assert plain.startswith(
        "ERROR:velune.probe:ordinary failure\nTraceback (most recent call last)"
    )
    assert "RuntimeError: nothing secret here" in plain
    assert REDACTION_PLACEHOLDER not in plain


# ── in-process: existing handlers, repeated initialization, formatting ───────


@pytest.fixture
def root_logging():
    """Save and restore the root logger, its handlers' filters and the velune logger."""
    root = logging.getLogger()
    velune = logging.getLogger("velune")
    saved_handlers = list(root.handlers)
    saved_filters = {h: list(h.filters) for h in saved_handlers}
    saved = (root.level, list(root.filters), velune.level)
    registered: list[str] = []
    try:
        yield registered
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = saved_handlers
        for handler, filters in saved_filters.items():
            handler.filters[:] = filters
        root.setLevel(saved[0])
        root.filters[:] = saved[1]
        velune.setLevel(saved[2])
        for value in registered:
            unregister_secret_value(value)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    for key, value in _isolated_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    for key in [k for k in os.environ if k.endswith("_API_KEY")]:
        monkeypatch.delenv(key)


async def _build(workspace: Path):
    workspace.mkdir(exist_ok=True)
    runtime = build_runtime(workspace, defer_background=True)
    lifecycle = runtime.container.get_optional("runtime.lifecycle")
    if lifecycle is not None:
        await lifecycle.shutdown()
    return runtime


def _redactors(handler: logging.Handler) -> int:
    return sum(isinstance(f, SecretRedactingFilter) for f in handler.filters)


async def test_handlers_that_already_exist_get_the_filter(root_logging, isolated_home, tmp_path):
    buffer = io.StringIO()
    existing = logging.StreamHandler(buffer)
    existing.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    logging.getLogger().addHandler(existing)

    await _build(tmp_path / "ws")  # basicConfig adds nothing: the root already has handlers

    assert all(_redactors(h) == 1 for h in logging.getLogger().handlers)
    register_secret_value(FIRST)
    root_logging.append(FIRST)
    logging.getLogger("velune.probe").error("token %s", FIRST)
    assert buffer.getvalue() == f"velune.probe | token {REDACTION_PLACEHOLDER}\n"


async def test_repeated_initialization_does_not_stack_filters(
    root_logging, isolated_home, tmp_path
):
    logging.getLogger().addHandler(logging.StreamHandler(io.StringIO()))
    await _build(tmp_path / "ws1")
    await _build(tmp_path / "ws2")
    install_log_redaction()
    assert all(_redactors(h) == 1 for h in logging.getLogger().handlers)


def test_installation_leaves_levels_formatters_and_other_filters_alone():
    logger = logging.Logger("velune.test.isolated")
    handler = logging.StreamHandler(io.StringIO())
    formatter = logging.Formatter("%(message)s")
    other = logging.Filter("velune")
    handler.setLevel(logging.ERROR)
    handler.setFormatter(formatter)
    handler.addFilter(other)
    logger.addHandler(handler)

    install_log_redaction(logger)
    install_log_redaction(logger)

    assert handler.level == logging.ERROR and handler.formatter is formatter
    assert handler.filters[0] is other and _redactors(handler) == 1


def test_a_traceback_without_secrets_keeps_its_exception_for_custom_formatters():
    """Nothing is pre-rendered when there is nothing to mask: a formatter that overrides
    ``formatException`` (or a rich traceback handler) still receives ``exc_info``."""
    try:
        raise RuntimeError("ordinary")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, sys.exc_info())
    SecretRedactingFilter().filter(record)
    assert record.exc_info is not None and record.exc_text is None


def test_a_traceback_already_rendered_by_another_handler_is_redacted(root_logging):
    register_secret_value(SECOND)
    root_logging.append(SECOND)
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    record.exc_text = f"Traceback ...\nRuntimeError: {SECOND}"
    SecretRedactingFilter().filter(record)
    assert SECOND not in record.exc_text and REDACTION_PLACEHOLDER in record.exc_text
