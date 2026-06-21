"""Sandboxed plugin hook runner — executed as a subprocess by PluginSandbox.

Reads one JSON request from stdin, executes the named hook on the plugin class,
writes one JSON response to stdout, then exits.  This process intentionally has
no access to parent environment variables (the caller strips the env).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path


def _run() -> None:
    try:
        req = json.loads(sys.stdin.readline())
        plugin_dir = Path(req["plugin_dir"])
        entry_point: str = req["entry_point"]
        class_name: str = req.get("class_name", "Plugin")
        hook_name: str = req["hook_name"]
        payload: dict = req.get("payload", {})

        entry_file = plugin_dir / entry_point
        module_name = f"_velune_plugin_{plugin_dir.name}"
        spec = importlib.util.spec_from_file_location(module_name, str(entry_file))
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load plugin from {entry_file}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        cls = getattr(module, class_name)
        instance = cls()

        hook = getattr(instance, hook_name, None)
        if hook is None:
            result = None
        elif asyncio.iscoroutinefunction(hook):
            result = asyncio.run(hook(**payload))
        else:
            result = hook(**payload)

        sys.stdout.write(json.dumps({"result": result, "error": None}) + "\n")
        sys.stdout.flush()
    except Exception as exc:
        sys.stdout.write(json.dumps({"result": None, "error": str(exc)}) + "\n")
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    _run()
