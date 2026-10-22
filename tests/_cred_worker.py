"""Subprocess worker for ``test_credential_multiprocess.py``.

Runs in its own interpreter so the credential store is exercised by genuinely
separate OS processes. Never touches the real config dir or OS keyring: the
config directory is patched to the one given on the command line and the master
key is a fixed fake.

usage: _cred_worker.py <config_dir> <go_file> <prefix> <count> [crash]
"""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

FAKE_MASTER = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")


def main() -> int:
    config_dir, go_file, prefix, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    crash = len(sys.argv) > 5 and sys.argv[5] == "crash"

    with (
        patch("velune.providers.keystore.user_config_dir", return_value=config_dir),
        patch("velune.providers.crypto.get_or_create_master_key", return_value=FAKE_MASTER),
    ):
        from velune.providers import keystore as ks

        ks._manager._init()

        if os.environ.get("CRED_WORKER_NOLOCK"):
            # Negative control used when validating the test itself: with the
            # cross-process lock removed, the harness must lose updates.
            import contextlib

            ks.locked = lambda *_a, **_k: contextlib.nullcontext()  # type: ignore[assignment]

        if crash:
            # Die between writing the temp file and renaming it over the store.
            def _die(self, target):
                os._exit(9)

            Path.replace = _die  # type: ignore[method-assign]

        deadline = time.monotonic() + 30
        while not Path(go_file).exists():
            if time.monotonic() > deadline:
                print("never got the go signal", file=sys.stderr)
                return 3
            time.sleep(0.005)

        for i in range(count):
            ks.save_key(f"{prefix}{i}", f"sk-test-{prefix}-{i}", verified=True)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        print(f"worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)
