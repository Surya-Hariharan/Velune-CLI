"""Real-terminal (PTY) audit harness for the interactive REPL.

Unlike the in-process prompt_toolkit tests elsewhere in ``tests/``, these tests
spawn the actual ``velune`` program inside a pseudo-terminal, feed it real
keystrokes, and read back the emulated screen. See ``README.md``.
"""
