"""Split a command line into arguments exactly the way the command executor does.

Anything that judges a command before it runs (is it read-only? which files does it name?) must
see the same arguments the executor will pass to the program. Quotes inside a token are removed
(``-de"lete"`` is ``-delete``), backslashes are not escapes (Windows paths keep them) and ``#`` is
not a comment, so this is deliberately not ``shlex.split``'s default behaviour.
"""

from __future__ import annotations

import shlex


def split_command(command: str) -> list[str]:
    """The arguments of *command*; raises :class:`ValueError` for an unbalanced quote."""
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)
