"""Confine the paths a plugin declares to the plugin's own directory.

A manifest names the folders and files it uses (``commands``, ``skills``, ``hooks``,
``mcpServers``) and a plugin ships command and skill files. All of it is controlled by whoever
wrote the plugin, and a repository can ship symlinks, so none of it may reach outside the
plugin: a ``commands/x.md`` that is a link to ``~/.ssh/id_rsa`` would be read and sent to the
model provider as a prompt.

Two checks, both needed:

* :func:`check_declared` looks at the *text* of a declared path and rejects anything that is
  not a plain relative path. It parses the text with both the Windows and the POSIX rules, so
  the verdict is the same on every platform (``C:\\x``, ``\\\\host\\share``, ``/x`` and ``..\\x``
  are all rejected on Linux too, not just on Windows).
* :func:`is_within` looks at where the path *really* is (``resolve()`` follows symlinks) and
  compares path components, never string prefixes, so ``/plugin-evil`` is not "inside"
  ``/plugin``.

A check made when a plugin is loaded can be outdated by the time a file is read, so callers
check again at the point of use. That narrows the window but cannot close it; the limit is
documented in SECURITY.md.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


class PluginPathError(ValueError):
    """A path a plugin declared (or a file it ships) is not confined to the plugin."""


def check_declared(declared: object) -> str:
    """Return *declared* if it is a plain relative path, else raise :class:`PluginPathError`."""
    if not isinstance(declared, str) or not declared.strip():
        raise PluginPathError(f"path {declared!r} is empty or not a string")
    if "\x00" in declared:
        raise PluginPathError("path contains a NUL character")
    for flavour in (PureWindowsPath, PurePosixPath):
        pure = flavour(declared)
        if pure.is_absolute() or pure.drive or pure.root:
            raise PluginPathError(f"path {declared!r} is absolute or drive-qualified")
        if ".." in pure.parts:
            raise PluginPathError(f"path {declared!r} climbs out of the plugin with '..'")
    return declared


def is_within(path: Path, root: Path) -> bool:
    """True when *path* is *root* or lies under it, once both are fully resolved."""
    resolved_root = root.resolve()
    resolved = path.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def resolve_within(root: Path, declared: object) -> Path:
    """The real location of the relative path *declared* under *root*, or raise.

    The target does not have to exist (a missing optional folder is normal), but wherever it
    would be, it must be inside *root* after symlinks are resolved.
    """
    relative = check_declared(declared)
    target = (root.resolve() / relative).resolve()
    if not is_within(target, root):
        raise PluginPathError(f"path {relative!r} resolves outside the plugin")
    return target
