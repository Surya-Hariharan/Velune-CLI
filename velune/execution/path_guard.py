from pathlib import Path

def is_within_workspace(path: Path, workspace: Path) -> bool:
    """Check if path is within workspace directory."""
    try:
        resolved = Path(path).resolve()
        workspace_resolved = Path(workspace).resolve()
        return (workspace_resolved in resolved.parents or 
                resolved == workspace_resolved or
                str(resolved).startswith(str(workspace_resolved) + "/") or
                str(resolved).startswith(str(workspace_resolved) + "\\"))
    except (ValueError, OSError):
        return False

def validate_workspace_path(path: Path, workspace: Path, label: str = "path") -> None:
    """Raise ValueError if path is outside workspace."""
    if not is_within_workspace(path, workspace):
        raise ValueError(f"Security: {label} '{path}' is outside workspace '{workspace}'")
