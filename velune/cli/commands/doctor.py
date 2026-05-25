"""Environment health diagnostics for Velune."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

console = Console()
doctor_cmd = typer.Typer(help="Environment health diagnostics")

@doctor_cmd.command(name="check")
def check(
    fix: bool = typer.Option(False, "--fix", help="Attempt to fix issues automatically"),
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """Run Velune environment health checks."""
    if fix:
        console.print("[yellow]Attempting automatic fixes...[/yellow]")

        # Fix 1: Create .velune/ directory
        velune_dir = Path.cwd() / ".velune"
        if not velune_dir.exists():
            try:
                velune_dir.mkdir(parents=True, exist_ok=True)
                console.print("[green]✓ Created .velune/ directory.[/green]")
            except Exception as e:
                console.print(f"[red]✗ Failed to create .velune/: {e}[/red]")

        # Fix 2: Create default velune.toml if missing
        config_file = Path.cwd() / "velune.toml"
        if not config_file.exists():
            try:
                import toml  # type: ignore[import-untyped]

                from velune.kernel.config import get_default_config
                default_config = get_default_config()
                with open(config_file, "w") as f:
                    toml.dump(default_config.model_dump(), f)
                console.print("[green]✓ Created default velune.toml config file.[/green]")
            except Exception as e:
                console.print(f"[red]✗ Failed to create default velune.toml: {e}[/red]")

        # Fix 3: Initialize databases
        db_file = velune_dir / "velune_cognitive_core.db"
        try:
            from velune.telemetry.cognition import CognitivePerformanceAnalytics
            CognitivePerformanceAnalytics(db_path=db_file)
            console.print("[green]✓ SQLite database successfully initialized.[/green]")
        except Exception as e:
            console.print(f"[red]✗ Failed to initialize SQLite database: {e}[/red]")

        console.print("[yellow]Re-running checks after fixes...[/yellow]\n")

    checks = [
        _check_python_version,
        _check_core_dependencies,
        _check_ollama_connectivity,
        _check_ollama_models,
        _check_lm_studio,
        _check_openai_api_key,
        _check_anthropic_api_key,
        _check_velune_dir,
        _check_sqlite,
        _check_qdrant,
        _check_config,
        _check_treesitter,
        _check_git,
        _check_gpu,
        _check_vram,
    ]

    results = []
    for check_fn in checks:
        try:
            result = check_fn()
            results.append(result)
        except Exception as e:
            results.append({"name": check_fn.__name__.replace("_check_", "").replace("_", " ").title(), "status": "error", "message": str(e)})

    if json_output:
        import json
        print(json.dumps(results, indent=2))
        return

    _render_results(results)

    failures = [r for r in results if r["status"] == "fail"]
    if failures:
        console.print(f"\n[red]✗ {len(failures)} check(s) failed.[/red]")
        console.print("[dim]Run 'velune doctor --fix' to attempt automatic fixes.[/dim]")
        raise typer.Exit(1)
    else:
        console.print("\n[green]✓ All checks passed. Velune is ready.[/green]")

def _check_python_version() -> dict:
    version = sys.version_info
    clean_version = sys.version.replace('\n', ' ')
    if version >= (3, 11):
        return {"name": "Python Version", "status": "ok", "message": f"{clean_version}"}
    return {"name": "Python Version", "status": "fail",
            "message": f"Python {version.major}.{version.minor} < 3.11. Install Python 3.11+. Details: {clean_version}"}

def _check_core_dependencies() -> dict:
    deps = ["pydantic", "typer", "rich", "httpx", "qdrant_client", "toml"]
    missing = []
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)

    if not missing:
        return {"name": "Core Dependencies", "status": "ok", "message": "All core dependencies installed."}
    return {"name": "Core Dependencies", "status": "fail", "message": f"Missing core dependencies: {', '.join(missing)}"}

def _check_ollama_connectivity() -> dict:
    import httpx
    try:
        httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        return {"name": "Ollama Connectivity", "status": "ok", "message": "Connected successfully to http://localhost:11434."}
    except Exception:
        return {"name": "Ollama Connectivity", "status": "warn", "message": "Could not connect to Ollama at http://localhost:11434."}

def _check_ollama_models() -> dict:
    import httpx
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        models = r.json().get("models", [])
        if models:
            model_names = [m.get("name") for m in models]
            return {"name": "Ollama Model Availability", "status": "ok", "message": f"{len(models)} model(s) found: {', '.join(model_names[:3])}{'...' if len(model_names) > 3 else ''}"}
        return {"name": "Ollama Model Availability", "status": "warn", "message": "No local Ollama models installed. Run 'ollama pull llama3.2'."}
    except Exception:
        return {"name": "Ollama Model Availability", "status": "warn", "message": "Unable to check model list (Ollama not connected)."}

def _check_lm_studio() -> dict:
    import httpx
    try:
        r = httpx.get("http://localhost:1234/v1/models", timeout=3.0)
        if r.status_code == 200:
            return {"name": "LM Studio Connectivity", "status": "ok", "message": "Connected successfully to http://localhost:1234."}
        return {"name": "LM Studio Connectivity", "status": "warn", "message": f"Connected to http://localhost:1234 but received status {r.status_code}."}
    except Exception:
        return {"name": "LM Studio Connectivity", "status": "warn", "message": "Not running or not accessible at http://localhost:1234."}

def _check_openai_api_key() -> dict:
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return {"name": "OpenAI API Key", "status": "ok", "message": f"Found in environment ({key[:4]}...{key[-4:] if len(key) > 8 else ''})"}
    return {"name": "OpenAI API Key", "status": "warn", "message": "Missing OPENAI_API_KEY env variable."}

def _check_anthropic_api_key() -> dict:
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return {"name": "Anthropic API Key", "status": "ok", "message": f"Found in environment ({key[:4]}...{key[-4:] if len(key) > 8 else ''})"}
    return {"name": "Anthropic API Key", "status": "warn", "message": "Missing ANTHROPIC_API_KEY env variable."}

def _check_velune_dir() -> dict:
    velune_dir = Path.cwd() / ".velune"
    try:
        velune_dir.mkdir(exist_ok=True)
        test_file = velune_dir / ".write_test"
        test_file.write_text("test")
        test_file.unlink()
        return {"name": ".velune Directory Writable", "status": "ok", "message": f"Writable directory at {velune_dir}"}
    except Exception as e:
        return {"name": ".velune Directory Writable", "status": "fail", "message": f"Cannot write to {velune_dir}: {e}"}

def _check_sqlite() -> dict:
    velune_dir = Path.cwd() / ".velune"
    db_file = velune_dir / "velune_cognitive_core.db"
    try:
        velune_dir.mkdir(exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(db_file), timeout=3.0)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()
        return {"name": "SQLite DB Initializable", "status": "ok", "message": f"Successfully initialized/opened sqlite database at {db_file}"}
    except Exception as e:
        return {"name": "SQLite DB Initializable", "status": "fail", "message": f"Failed to initialize SQLite database: {e}"}

def _check_qdrant() -> dict:
    try:
        from qdrant_client import QdrantClient
        with tempfile.TemporaryDirectory(prefix="velune-qdrant-") as temp_dir:
            qdrant_path = Path(temp_dir)
            client = QdrantClient(path=str(qdrant_path))
            client.get_collections()
            client.close()
            return {"name": "Qdrant In-Process Initializable", "status": "ok", "message": f"Qdrant local storage successfully initialized at {qdrant_path}"}
    except Exception as e:
        return {"name": "Qdrant In-Process Initializable", "status": "fail", "message": f"Failed to initialize local Qdrant client: {e}"}

def _check_config() -> dict:
    config_file = Path.cwd() / "velune.toml"
    if not config_file.exists():
        return {"name": "velune.toml Config File", "status": "warn", "message": "No velune.toml found in current workspace. Using defaults."}

    try:
        import toml  # type: ignore[import-untyped]

        from velune.kernel.config import VeluneConfig
        data = toml.load(config_file)
        VeluneConfig(**data)
        return {"name": "velune.toml Config File", "status": "ok", "message": f"Found and validated successfully at {config_file}"}
    except Exception as e:
        return {"name": "velune.toml Config File", "status": "fail", "message": f"Invalid velune.toml format or schema validation error: {e}"}

def _check_treesitter() -> dict:
    try:
        import tree_sitter_go
        import tree_sitter_python
        import tree_sitter_rust
        import tree_sitter_typescript
        from tree_sitter import Language

        langs = []
        for name, mod in [("python", tree_sitter_python), ("typescript", tree_sitter_typescript), ("go", tree_sitter_go), ("rust", tree_sitter_rust)]:
            try:
                if name == "typescript":
                    Language(mod.language_typescript())
                else:
                    Language(mod.language())
                langs.append(name)
            except Exception:
                pass
        if langs:
            return {"name": "Tree-sitter Grammars", "status": "ok", "message": f"Tree-sitter grammars loaded: {', '.join(langs)}."}
        return {"name": "Tree-sitter Grammars", "status": "warn", "message": "tree-sitter installed but no grammars loaded correctly."}
    except ImportError as e:
        return {"name": "Tree-sitter Grammars", "status": "warn", "message": f"Tree-sitter package or parser modules missing: {e}."}

def _check_git() -> dict:
    git_path = shutil.which("git")
    if git_path:
        return {"name": "Git in PATH", "status": "ok", "message": f"Found Git at {git_path}"}
    return {"name": "Git in PATH", "status": "fail", "message": "Git is not installed or not in system PATH."}

def _check_gpu() -> dict:
    from velune.providers.discovery.gpu import GPUDetector
    try:
        gpu_info = GPUDetector().detect()
        if gpu_info.get("has_gpu"):
            gpu_name = gpu_info.get("gpu_name", "Unknown Name")
            gpu_type = gpu_info.get("gpu_type", "Unknown")
            return {"name": "GPU Detection", "status": "ok", "message": f"Detected GPU: {gpu_name} ({gpu_type.upper()})"}
        return {"name": "GPU Detection", "status": "warn", "message": "No dedicated GPU detected. Models will run on CPU."}
    except Exception as e:
        return {"name": "GPU Detection", "status": "warn", "message": f"Failed to run GPU detection: {e}"}

def _check_vram() -> dict:
    from velune.providers.discovery.gpu import GPUDetector
    try:
        gpu_info = GPUDetector().detect()
        if gpu_info.get("has_gpu") and gpu_info.get("vram_total_gb") is not None:
            total = gpu_info.get("vram_total_gb", 0)
            free = gpu_info.get("vram_free_gb", 0)
            return {"name": "Available VRAM", "status": "ok", "message": f"VRAM Total: {total:.2f} GB, VRAM Free: {free:.2f} GB"}
        return {"name": "Available VRAM", "status": "warn", "message": "Unified or CPU-only memory in use."}
    except Exception as e:
        return {"name": "Available VRAM", "status": "warn", "message": f"Failed to query VRAM: {e}"}

def _render_results(results: list) -> None:
    table = Table(title="Velune Environment Check", show_header=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    status_styles = {"ok": "[green]✓ OK[/green]", "warn": "[yellow]⚠ WARN[/yellow]",
                     "fail": "[red]✗ FAIL[/red]", "error": "[red]✗ ERROR[/red]"}

    for result in results:
        table.add_row(
            result["name"],
            status_styles.get(result["status"], result["status"]),
            result.get("message", "")
        )
    console.print(table)
