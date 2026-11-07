"""Shared foundation for the future ``update`` / ``repair`` / ``uninstall`` commands.

Internal and deliberately small: ownership detection, the result/exit-code
model, Windows self-mutation safety, and the journal data model. There is no
command here and nothing is registered in the CLI; later phases build on it.

Design rule for those phases: uv's exit code is never proof of the resulting
installation state (it exited 1 after upgrading, and 0 while reporting a
damaged environment). Re-detect ownership and re-check the version afterwards.
"""
