"""Install lifecycle behind ``velune update`` / ``repair`` / ``uninstall`` / ``purge-data``.

Ownership detection, the result/exit-code model, Windows self-mutation safety,
the journal, and the operations themselves. The CLI commands live in
``velune/cli/commands/{update,repair,uninstall,purge_data}.py``.

Design rule: uv's exit code is never proof of the resulting
installation state (it exited 1 after upgrading, and 0 while reporting a
damaged environment). Re-detect ownership and re-check the version afterwards.
"""
