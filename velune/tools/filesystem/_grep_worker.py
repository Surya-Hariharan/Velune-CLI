"""Isolated regex scan worker for ``grep_files``.

Run as ``python -I _grep_worker.py``: one JSON request on stdin, one JSON object
per line on stdout (``{"m": ...}`` per matching line, then a final ``{"done": ...}``).

Why a separate process: CPython's ``re`` engine holds the GIL for the whole of a
single ``search`` call and cannot be interrupted, so a catastrophic-backtracking
pattern (``^(a+)+$``) freezes every thread -- including the UI's event loop --
until it finishes. A process can be killed on a deadline; a thread cannot.

Standard library only: it must start fast and never import the application.
"""

from __future__ import annotations

import json
import re
import sys

_BINARY_SNIFF_BYTES = 8192


def main() -> int:
    req = json.loads(sys.stdin.read())
    try:
        rx = re.compile(req["pattern"], re.IGNORECASE)
    except re.error as exc:  # the parent validates first; belt and braces
        sys.stdout.write(json.dumps({"error": f"invalid regular expression: {exc}"}) + "\n")
        return 2

    max_matches: int = req["max_matches"]
    max_file_bytes: int = req["max_file_bytes"]
    max_line_chars: int = req["max_line_chars"]
    text_chars: int = req["text_chars"]
    max_output_chars: int = req["max_output_chars"]

    emitted = 0
    output_chars = 0
    scanned = skipped_large = skipped_binary = skipped_unreadable = 0
    stop_reason: str | None = None
    out = sys.stdout

    for path in req["files"]:
        try:
            with open(path, "rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
                if b"\x00" in head:
                    skipped_binary += 1
                    continue
                if len(head) == _BINARY_SNIFF_BYTES:
                    rest = fh.read(max_file_bytes - _BINARY_SNIFF_BYTES + 1)
                    if len(head) + len(rest) > max_file_bytes:
                        skipped_large += 1
                        continue
                    data = head + rest
                else:
                    data = head
        except OSError:
            skipped_unreadable += 1
            continue

        scanned += 1
        text = data.decode("utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > max_line_chars:
                line = line[:max_line_chars]
            m = rx.search(line)
            if m is None:
                continue
            entry = {
                "file": path,
                "line": lineno,
                "match": m.group()[:text_chars],
                "text": line.strip()[:text_chars],
            }
            output_chars += len(path) + len(entry["text"]) + len(entry["match"]) + 32
            out.write(json.dumps({"m": entry}) + "\n")
            out.flush()
            emitted += 1
            if emitted >= max_matches:
                stop_reason = "max_matches"
                break
            if output_chars >= max_output_chars:
                stop_reason = "output_limit"
                break
        if stop_reason:
            break

    out.write(
        json.dumps(
            {
                "done": {
                    "stop_reason": stop_reason,
                    "files_scanned": scanned,
                    "skipped_large": skipped_large,
                    "skipped_binary": skipped_binary,
                    "skipped_unreadable": skipped_unreadable,
                }
            }
        )
        + "\n"
    )
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
