"""Environment health diagnostics for Velune."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from velune.providers.keystore import get_key

console = Console()
doctor_cmd = typer.Typer(help="Check that providers, models, and paths are healthy.")


@doctor_cmd.callback(invoke_without_command=True)
def doctor_main(
    ctx: typer.Context,
    perf: bool = typer.Option(False, "--perf", help="Check startup performance"),
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """Callback for doctor command group to enable startup performance diagnostics."""
    if ctx.invoked_subcommand is None:
        if perf:
            import time

            from velune.core.startup_profiler import _PROCESS_START

            startup_time_ms = (time.perf_counter() - _PROCESS_START) * 1000.0

            if json_output:
                import json

                print(
                    json.dumps(
                        {
                            "startup_time_ms": round(startup_time_ms, 2),
                            "status": "ok" if startup_time_ms < 3000 else "fail",
                        }
                    )
                )
            else:
                status = "OK" if startup_time_ms < 3000 else "FAIL"
                console.print(f"Startup performance: {startup_time_ms:.2f}ms [{status}]")
            raise typer.Exit()
        else:
            console.print(ctx.get_help())
            raise typer.Exit()


@doctor_cmd.command(name="providers")
def show_providers() -> None:
    """Show provider health and capability status."""
    from velune.telemetry import print_provider_health_report

    print_provider_health_report(console)


@doctor_cmd.command(name="network")
def check_network() -> None:
    """Proactively ping all providers and display network health dashboard."""
    from rich.table import Table

    from velune.core.types.provider import ProviderHealth
    from velune.providers.manager import ProviderManager
    from velune.providers.registry import ProviderRegistry

    async def _run() -> None:
        registry = ProviderRegistry()
        manager = ProviderManager(registry)
        console.print("[cyan]Pinging all registered providers...[/cyan]")
        health_map = await manager.check_all_health()

        table = Table(
            title="Provider Network Health Dashboard", show_header=True, header_style="bold magenta"
        )
        table.add_column("Provider", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Models Discovered")
        table.add_column("Streaming")

        for pid, status in health_map.items():
            provider = registry.get(pid)
            color = (
                "green"
                if status == ProviderHealth.HEALTHY
                else "red"
                if status in (ProviderHealth.OFFLINE, ProviderHealth.UNAUTHORIZED)
                else "yellow"
            )

            models_str = "0"
            streaming = "Unknown"

            if status == ProviderHealth.HEALTHY and provider:
                try:
                    models = await provider.list_models()
                    models_str = str(len(models))
                except Exception:
                    models_str = "Error"

                caps = provider.get_capabilities()
                streaming = "✓" if caps.supports_streaming else "✗"

            table.add_row(
                pid,
                f"[{color}]{status.value.upper()}[/{color}]",
                models_str,
                streaming,
            )

        console.print(table)

    from velune.kernel.entrypoint import run_async

    run_async(_run())


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
                console.print("[green]Created .velune/ directory.[/green]")
            except Exception as e:
                console.print(f"[red]Failed to create .velune/: {e}[/red]")

        # Fix 2: Create default velune.toml if missing
        config_file = Path.cwd() / "velune.toml"
        if not config_file.exists():
            try:
                import toml  # type: ignore[import-untyped]

                from velune.kernel.config import get_default_config

                default_config = get_default_config()
                with open(config_file, "w", encoding="utf-8") as f:
                    toml.dump(default_config.model_dump(), f)
                console.print("[green]Created default velune.toml config file.[/green]")
            except Exception as e:
                console.print(f"[red]Failed to create default velune.toml: {e}[/red]")

        # Fix 3: Initialize databases
        db_file = velune_dir / "velune_cognitive_core.db"
        try:
            from velune.telemetry.cognition import CognitivePerformanceAnalytics

            CognitivePerformanceAnalytics(db_path=db_file)
            console.print("[green]SQLite database successfully initialized.[/green]")
        except Exception as e:
            console.print(f"[red]Failed to initialize SQLite database: {e}[/red]")

        console.print("[yellow]Re-running checks after fixes...[/yellow]\n")

    checks = [
        _check_python_version,
        _check_console_launcher,
        _check_scripts_on_path,
        _check_pip,
        _check_core_dependencies,
        _check_internet_connectivity,
        _check_ollama_connectivity,
        _check_ollama_models,
        _check_lm_studio,
        _check_openai_api_key,
        _check_anthropic_api_key,
        _check_groq,
        _check_google,
        _check_velune_dir,
        _check_sqlite,
        _check_qdrant,
        _check_config,
        _check_treesitter,
        _check_git,
        _check_runtime_safety,
        _check_gpu,
        _check_vram,
        _check_model_benchmarks,
        _check_session_cost,
        _check_memory_health,
        _check_council_roles,
    ]

    results = []
    for check_fn in checks:
        try:
            result = check_fn()
            results.append(result)
        except Exception as e:
            results.append(
                {
                    "name": check_fn.__name__.replace("_check_", "").replace("_", " ").title(),
                    "status": "error",
                    "message": str(e),
                }
            )

    if json_output:
        import json

        print(json.dumps(results, indent=2))
        return

    _render_results(results)

    failures = [r for r in results if r["status"] == "fail"]
    if failures:
        console.print(f"\n[red]{len(failures)} check(s) failed.[/red]")
        console.print("[dim]Run 'velune doctor --fix' to attempt automatic fixes.[/dim]")
        raise typer.Exit(1)
    else:
        console.print("\n[green]All checks passed. Velune is ready.[/green]")


def _check_python_version() -> dict:
    version = sys.version_info
    clean_version = sys.version.replace("\n", " ")
    if version >= (3, 10):
        return {"name": "Python Version", "status": "ok", "message": f"{clean_version}"}
    return {
        "name": "Python Version",
        "status": "fail",
        "message": f"Python {version.major}.{version.minor} < 3.10. Install Python 3.10+. Details: {clean_version}",
    }


def _check_console_launcher() -> dict:
    """Verify the generated ``velune`` launcher resolves and points at *this*
    interpreter.

    On Windows the ``velune.exe`` launcher embeds the absolute path of the
    interpreter present at install time. If that interpreter is later upgraded,
    moved, or uninstalled, the launcher fails with "cannot locate pythonXY.dll
    (126)". We can't run the broken launcher safely, but we *can* detect that
    the command on PATH belongs to a different interpreter than the one running
    ``doctor`` — the strongest in-process signal that a reinstall is needed.
    """
    launcher = shutil.which("velune")
    if not launcher:
        return {
            "name": "Console Launcher",
            "status": "warn",
            "message": (
                "'velune' is not on PATH. Use 'python -m velune' or add the "
                "Python Scripts directory to PATH. (Reinstall with "
                "'python -m pip install --force-reinstall velune-cli'.)"
            ),
        }

    scripts_dir = Path(sys.executable).parent
    # The launcher for the running interpreter lives next to python.exe (in
    # Scripts/ or bin/). If the resolved launcher is elsewhere, PATH is pointing
    # at a different (possibly stale) install.
    launcher_parent = Path(launcher).resolve().parent
    expected_dirs = {
        scripts_dir.resolve(),
        (scripts_dir / "Scripts").resolve(),
        (scripts_dir.parent / "Scripts").resolve(),
        (scripts_dir.parent / "bin").resolve(),
    }
    if launcher_parent not in expected_dirs:
        return {
            "name": "Console Launcher",
            "status": "warn",
            "message": (
                f"'velune' on PATH ({launcher}) is not the launcher for the "
                f"running interpreter ({sys.executable}). If it errors, run "
                f"'python -m velune' or reinstall."
            ),
        }
    return {
        "name": "Console Launcher",
        "status": "ok",
        "message": f"Resolves to {launcher}",
    }


def _check_scripts_on_path() -> dict:
    """Check that the interpreter's Scripts/bin directory is on PATH so that
    console entry points (``velune``) are runnable as bare commands."""
    import os

    base = Path(sys.executable).parent
    scripts_dir = base / "Scripts" if sys.platform == "win32" else base
    if not scripts_dir.exists():
        # Fall back to the base dir (venvs put scripts beside python on *nix).
        scripts_dir = base
    path_entries = {Path(p).resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p}
    if scripts_dir.resolve() in path_entries:
        return {
            "name": "Scripts on PATH",
            "status": "ok",
            "message": f"{scripts_dir} is on PATH",
        }
    return {
        "name": "Scripts on PATH",
        "status": "warn",
        "message": (
            f"{scripts_dir} is not on PATH — bare 'velune' may not be found. "
            f"Add it to PATH, or always use 'python -m velune'."
        ),
    }


def _check_pip() -> dict:
    """Confirm pip is importable for the running interpreter (needed for
    self-repair / reinstall guidance to be actionable)."""
    try:
        import importlib.metadata as _md

        pip_version = _md.version("pip")
        return {
            "name": "pip Available",
            "status": "ok",
            "message": f"pip {pip_version} (use 'python -m pip')",
        }
    except Exception:
        return {
            "name": "pip Available",
            "status": "warn",
            "message": (
                "pip not found for this interpreter. Run "
                "'python -m ensurepip --upgrade' to restore it."
            ),
        }


def _check_core_dependencies() -> dict:
    deps = ["pydantic", "typer", "rich", "httpx", "qdrant_client", "toml"]
    missing = []
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)

    if not missing:
        return {
            "name": "Core Dependencies",
            "status": "ok",
            "message": "All core dependencies installed.",
        }
    return {
        "name": "Core Dependencies",
        "status": "fail",
        "message": f"Missing core dependencies: {', '.join(missing)}",
    }


def _check_ollama_connectivity() -> dict:
    import httpx

    try:
        httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        return {
            "name": "Ollama Connectivity",
            "status": "ok",
            "message": "Connected successfully to http://localhost:11434.",
        }
    except Exception:
        return {
            "name": "Ollama Connectivity",
            "status": "warn",
            "message": "Could not connect to Ollama at http://localhost:11434.",
        }


def _check_ollama_models() -> dict:
    import httpx

    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        models = r.json().get("models", [])
        if models:
            model_names = [m.get("name") for m in models]
            return {
                "name": "Ollama Model Availability",
                "status": "ok",
                "message": f"{len(models)} model(s) found: {', '.join(model_names[:3])}{'...' if len(model_names) > 3 else ''}",
            }
        return {
            "name": "Ollama Model Availability",
            "status": "warn",
            "message": "No local Ollama models installed. Run 'ollama pull llama3.2'.",
        }
    except Exception:
        return {
            "name": "Ollama Model Availability",
            "status": "warn",
            "message": "Unable to check model list (Ollama not connected).",
        }


def _check_lm_studio() -> dict:
    import httpx

    try:
        r = httpx.get("http://localhost:1234/v1/models", timeout=3.0)
        if r.status_code == 200:
            return {
                "name": "LM Studio Connectivity",
                "status": "ok",
                "message": "Connected successfully to http://localhost:1234.",
            }
        return {
            "name": "LM Studio Connectivity",
            "status": "warn",
            "message": f"Connected to http://localhost:1234 but received status {r.status_code}.",
        }
    except Exception:
        return {
            "name": "LM Studio Connectivity",
            "status": "warn",
            "message": "Not running or not accessible at http://localhost:1234.",
        }


def _check_openai_api_key() -> dict:
    key = get_key("openai")
    if key:
        return {
            "name": "OpenAI API Key",
            "status": "ok",
            "message": f"Configured ({key[:4]}...{key[-4:] if len(key) > 8 else ''})",
        }
    return {
        "name": "OpenAI API Key",
        "status": "warn",
        "message": "Not configured. Run 'velune setup' to add your key.",
    }


def _check_anthropic_api_key() -> dict:
    key = get_key("anthropic")
    if key:
        return {
            "name": "Anthropic API Key",
            "status": "ok",
            "message": f"Configured ({key[:4]}...{key[-4:] if len(key) > 8 else ''})",
        }
    return {
        "name": "Anthropic API Key",
        "status": "warn",
        "message": "Not configured. Run 'velune setup' to add your key.",
    }


def _check_velune_dir() -> dict:
    velune_dir = Path.cwd() / ".velune"
    try:
        velune_dir.mkdir(exist_ok=True)
        test_file = velune_dir / ".write_test"
        test_file.write_text("test")
        test_file.unlink()
        return {
            "name": ".velune Directory Writable",
            "status": "ok",
            "message": f"Writable directory at {velune_dir}",
        }
    except Exception as e:
        return {
            "name": ".velune Directory Writable",
            "status": "fail",
            "message": f"Cannot write to {velune_dir}: {e}",
        }


def _check_sqlite() -> dict:
    velune_dir = Path.cwd() / ".velune"
    db_file = velune_dir / "velune_cognitive_core.db"
    try:
        velune_dir.mkdir(exist_ok=True)
        import sqlite3

        # closing() so an exception between connect and close cannot leak the
        # handle — the previous bare close() was skipped on any failure path.
        from contextlib import closing

        with closing(sqlite3.connect(str(db_file), timeout=3.0)) as conn:
            conn.execute("SELECT 1")
        return {
            "name": "SQLite DB Initializable",
            "status": "ok",
            "message": f"Successfully initialized/opened sqlite database at {db_file}",
        }
    except Exception as e:
        return {
            "name": "SQLite DB Initializable",
            "status": "fail",
            "message": f"Failed to initialize SQLite database: {e}",
        }


def _check_qdrant() -> dict:
    try:
        from qdrant_client import QdrantClient

        with tempfile.TemporaryDirectory(prefix="velune-qdrant-") as temp_dir:
            qdrant_path = Path(temp_dir)
            client = QdrantClient(path=str(qdrant_path))
            client.get_collections()
            client.close()
            return {
                "name": "Qdrant In-Process Initializable",
                "status": "ok",
                "message": f"Qdrant local storage successfully initialized at {qdrant_path}",
            }
    except Exception as e:
        return {
            "name": "Qdrant In-Process Initializable",
            "status": "fail",
            "message": f"Failed to initialize local Qdrant client: {e}",
        }


def _check_config() -> dict:
    config_file = Path.cwd() / "velune.toml"
    if not config_file.exists():
        return {
            "name": "velune.toml Config File",
            "status": "warn",
            "message": "No velune.toml found in current workspace. Using defaults.",
        }

    try:
        import toml  # type: ignore[import-untyped]

        from velune.kernel.config import VeluneConfig

        data = toml.load(config_file)
        VeluneConfig(**data)
        return {
            "name": "velune.toml Config File",
            "status": "ok",
            "message": f"Found and validated successfully at {config_file}",
        }
    except Exception as e:
        return {
            "name": "velune.toml Config File",
            "status": "fail",
            "message": f"Invalid velune.toml format or schema validation error: {e}",
        }


def _check_treesitter() -> dict:
    try:
        import tree_sitter_go
        import tree_sitter_python
        import tree_sitter_rust
        import tree_sitter_typescript
        from tree_sitter import Language

        langs = []
        for name, mod in [
            ("python", tree_sitter_python),
            ("typescript", tree_sitter_typescript),
            ("go", tree_sitter_go),
            ("rust", tree_sitter_rust),
        ]:
            try:
                if name == "typescript":
                    Language(mod.language_typescript())  # type: ignore
                else:
                    Language(mod.language())  # type: ignore
                langs.append(name)
            except Exception:
                pass
        if langs:
            return {
                "name": "Tree-sitter Grammars",
                "status": "ok",
                "message": f"Tree-sitter grammars loaded: {', '.join(langs)}.",
            }
        return {
            "name": "Tree-sitter Grammars",
            "status": "warn",
            "message": "tree-sitter installed but no grammars loaded correctly.",
        }
    except ImportError as e:
        return {
            "name": "Tree-sitter Grammars",
            "status": "warn",
            "message": f"Tree-sitter package or parser modules missing: {e}.",
        }


def _check_git() -> dict:
    git_path = shutil.which("git")
    if git_path:
        return {"name": "Git in PATH", "status": "ok", "message": f"Found Git at {git_path}"}
    return {
        "name": "Git in PATH",
        "status": "fail",
        "message": "Git is not installed or not in system PATH.",
    }


def _check_runtime_safety() -> dict:
    """Surface PATH-hijack exposure for the executables Velune is allowed to run.

    Mirrors the real execution guard: each allowlisted tool is resolved via the
    same ``shutil.which`` lookup the sandbox uses, then checked against the
    actual ``_is_trusted_path`` predicate. A tool that resolves to a location
    outside the trusted system/venv roots is either an attacker-planted shadow
    earlier in PATH or a legitimate non-standard install — both are worth
    surfacing because the sandbox will refuse to execute it.
    """
    from pathlib import Path

    from velune.execution.command_spec import (
        ALLOWED_EXECUTABLES,
        _is_trusted_path,
    )

    untrusted: list[str] = []
    checked = 0
    for exe in sorted(ALLOWED_EXECUTABLES):
        resolved = shutil.which(exe)
        if resolved is None:
            continue  # not installed — not a safety concern, just absent
        checked += 1
        try:
            resolved_path = Path(resolved).resolve()
        except OSError:
            untrusted.append(f"{exe} (unresolvable: {resolved})")
            continue
        if not _is_trusted_path(resolved_path):
            untrusted.append(f"{exe} -> {resolved_path}")

    if untrusted:
        return {
            "name": "Runtime Path Safety",
            "status": "warn",
            "message": (
                "Allowlisted tools resolve to untrusted locations and will be "
                f"refused by the sandbox (possible PATH hijack): {'; '.join(untrusted)}"
            ),
        }
    if checked == 0:
        return {
            "name": "Runtime Path Safety",
            "status": "warn",
            "message": "No allowlisted executables found in PATH to validate.",
        }
    return {
        "name": "Runtime Path Safety",
        "status": "ok",
        "message": f"All {checked} resolvable allowlisted tools live in trusted paths.",
    }


def _check_gpu() -> dict:
    from velune.providers.discovery.gpu import GPUDetector

    try:
        gpu_info = GPUDetector().detect()
        if gpu_info.get("has_gpu"):
            gpu_name = gpu_info.get("gpu_name", "Unknown Name")
            gpu_type = gpu_info.get("gpu_type", "Unknown")
            return {
                "name": "GPU Detection",
                "status": "ok",
                "message": f"Detected GPU: {gpu_name} ({gpu_type.upper()})",
            }
        return {
            "name": "GPU Detection",
            "status": "warn",
            "message": "No dedicated GPU detected. Models will run on CPU.",
        }
    except Exception as e:
        return {
            "name": "GPU Detection",
            "status": "warn",
            "message": f"Failed to run GPU detection: {e}",
        }


def _check_vram() -> dict:
    from velune.providers.discovery.gpu import GPUDetector

    try:
        # Free VRAM is volatile — bypass the startup disk cache so doctor
        # reports a live reading rather than a stale snapshot.
        gpu_info = GPUDetector().detect(use_cache=False)
        if gpu_info.get("has_gpu") and gpu_info.get("vram_total_gb") is not None:
            total = gpu_info.get("vram_total_gb", 0)
            free = gpu_info.get("vram_free_gb", 0)
            return {
                "name": "Available VRAM",
                "status": "ok",
                "message": f"VRAM Total: {total:.2f} GB, VRAM Free: {free:.2f} GB",
            }
        return {
            "name": "Available VRAM",
            "status": "warn",
            "message": "Unified or CPU-only memory in use.",
        }
    except Exception as e:
        return {"name": "Available VRAM", "status": "warn", "message": f"Failed to query VRAM: {e}"}


def _check_groq() -> dict:
    from velune.providers.keystore import get_key, has_key

    if not has_key("groq"):
        return {
            "name": "Groq",
            "status": "warn",
            "message": "Not configured — free tier available at console.groq.com/keys",
        }
    try:
        import httpx

        key = get_key("groq")
        r = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5,
        )
        if r.status_code == 200:
            models = r.json().get("data", [])
            return {
                "name": "Groq",
                "status": "ok",
                "message": f"Connected — {len(models)} models available",
            }
        return {
            "name": "Groq",
            "status": "fail",
            "message": f"Auth failed (HTTP {r.status_code}) — check your key",
        }
    except Exception as e:
        return {
            "name": "Groq",
            "status": "fail",
            "message": f"Cannot reach api.groq.com — {e}",
        }


def _check_google() -> dict:
    from velune.providers.keystore import get_key, has_key

    if not has_key("google"):
        return {
            "name": "Google Gemini",
            "status": "warn",
            "message": "Not configured — free quota at aistudio.google.com",
        }
    try:
        import httpx

        key = get_key("google")
        r = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key or ""},
            timeout=5,
        )
        if r.status_code == 200:
            models = r.json().get("models", [])
            return {
                "name": "Google Gemini",
                "status": "ok",
                "message": f"Connected — {len(models)} models available",
            }
        return {
            "name": "Google Gemini",
            "status": "fail",
            "message": f"Auth failed (HTTP {r.status_code})",
        }
    except Exception as e:
        return {
            "name": "Google Gemini",
            "status": "fail",
            "message": f"Cannot reach googleapis.com — {e}",
        }


def _check_model_benchmarks() -> dict:
    profile_path = Path.cwd() / ".velune" / "model_profiles.json"
    if profile_path.exists():
        try:
            import json

            data = json.loads(profile_path.read_text(encoding="utf-8"))
            if data:
                return {
                    "name": "Empirical Model Benchmarks",
                    "status": "ok",
                    "message": f"Cached capability profiles found for {len(data)} model(s).",
                }
        except Exception:
            pass
    return {
        "name": "Empirical Model Benchmarks",
        "status": "warn",
        "message": "No empirical model capability benchmarks cached. Run: velune models scan --probe",
    }


def _check_internet_connectivity() -> dict:
    from velune.providers.health import get_checker

    checker = get_checker()
    if checker.is_online:
        return {
            "name": "Internet Connectivity",
            "status": "ok",
            "message": "Online — cloud providers reachable",
        }
    return {
        "name": "Internet Connectivity",
        "status": "warn",
        "message": "Offline — router will fall back to local models only",
    }


def _check_session_cost() -> dict:
    from velune.telemetry.token_tracker import current_session

    total_tokens = current_session.total_tokens
    total_cost = current_session.total_cost
    if total_tokens == 0:
        return {
            "name": "Session Cost Tracking",
            "status": "ok",
            "message": "No inference calls recorded in this session",
        }
    cost_str = f"~${total_cost:.4f}" if total_cost > 0 else "free (local models only)"
    return {
        "name": "Session Cost Tracking",
        "status": "ok",
        "message": f"{total_tokens:,} tokens used · {cost_str}",
    }


def _check_memory_health() -> dict:
    """Report the same MemoryHealth every other memory-health surface reports.

    Previously reimplemented its own file-size-only view (raw disk bytes,
    no session/index counts) instead of going through
    ``MemoryLifecycleManager.health()`` like ``velune memory stats`` and the
    REPL's ``/memory`` — the three could show different numbers for the same
    workspace. ``doctor`` is a deliberately fast/standalone diagnostic with
    no shared runtime container (``bootstrap="light"``), so this builds a
    throwaway manager via :func:`read_memory_health` rather than reaching
    into a container that was never bootstrapped, but computes the exact
    same metrics the other two surfaces do.
    """
    from velune.kernel.entrypoint import run_async
    from velune.memory.lifecycle import read_memory_health

    try:
        health = run_async(read_memory_health(Path.cwd()))
    except Exception as exc:
        return {
            "name": "Memory Subsystem",
            "status": "warn",
            "message": f"Could not read memory health: {exc}",
        }

    message = (
        f"Episodic: {health.episodic_sessions} session(s) · "
        f"Semantic: {health.semantic_indexed_count} indexed · "
        f"LanceDB: {health.lancedb_size_mb:.1f} MB"
    )

    return {
        "name": "Memory Subsystem",
        "status": "ok",
        "message": message,
    }


def _check_council_roles() -> dict:
    """Verify that each council role has at least one model candidate assigned."""
    try:
        from velune.models.registry import ModelCapabilityRegistry
        from velune.models.specializations import CouncilRole, ModelSpecializationMapper

        mapper = ModelSpecializationMapper(ModelCapabilityRegistry())
        try:
            role_map = mapper.map_roles()
        except Exception as exc:
            return {
                "name": "Council Role Assignments",
                "status": "warn",
                "message": f"Role mapping unavailable (no providers discovered): {exc}",
            }

        roles_covered = [r.value for r in CouncilRole if r in role_map]
        roles_missing = [r.value for r in CouncilRole if r not in role_map]

        if roles_missing:
            return {
                "name": "Council Role Assignments",
                "status": "warn",
                "message": (
                    f"{len(roles_covered)} role(s) mapped, "
                    f"{len(roles_missing)} missing: {', '.join(roles_missing)}"
                ),
            }

        role_summary = "  ".join(
            f"{r.value}→{role_map[r].model_id.split('/')[-1]}" for r in CouncilRole if r in role_map
        )
        return {
            "name": "Council Role Assignments",
            "status": "ok",
            "message": role_summary,
        }

    except ImportError:
        return {
            "name": "Council Role Assignments",
            "status": "warn",
            "message": "ModelSpecializationMapper not importable — council subsystem unavailable",
        }


def _render_results(results: list) -> None:
    from rich.panel import Panel
    from rich.text import Text

    from velune.cli import design

    categories_map = {
        "Internet Connectivity": "Providers",
        "Ollama Connectivity": "Providers",
        "Ollama Model Availability": "Providers",
        "LM Studio Connectivity": "Providers",
        ".velune Directory Writable": "Storage",
        "SQLite DB Initializable": "Storage",
        "Qdrant In-Process Initializable": "Storage",
        "velune.toml Config File": "Storage",
        "Memory Subsystem": "Storage",
        "Council Role Assignments": "Council",
        "OpenAI API Key": "Security",
        "Anthropic API Key": "Security",
        "Groq": "Security",
        "Google Gemini": "Security",
        "Runtime Path Safety": "Security",
        "Python Version": "Performance",
        "Console Launcher": "Performance",
        "Scripts on PATH": "Performance",
        "pip Available": "Performance",
        "Core Dependencies": "Performance",
        "Git in PATH": "Performance",
        "Tree-sitter Grammars": "Performance",
        "GPU Detection": "Performance",
        "Available VRAM": "Performance",
        "Empirical Model Benchmarks": "Performance",
        "Session Cost Tracking": "Performance",
    }

    categories = ["Providers", "Storage", "Security", "Performance", "Council"]
    grouped: dict[str, list] = {cat: [] for cat in categories}
    for r in results:
        cat = categories_map.get(r["name"], "Performance")
        grouped[cat].append(r)

    # --- Summary header ---
    total = len(results)
    fails = sum(1 for r in results if r["status"] in ("fail", "error"))
    warns = sum(1 for r in results if r["status"] == "warn")

    if fails:
        summary_color = design.DANGER
        summary_icon = "FAIL"
        summary_tail = f"  [{design.DANGER}]{fails} failed[/{design.DANGER}]"
        if warns:
            summary_tail += (
                f"  [{design.WARN}]{warns} warning{'s' if warns > 1 else ''}[/{design.WARN}]"
            )
    elif warns:
        summary_color = design.WARN
        summary_icon = "WARN"
        summary_tail = f"  [{design.WARN}]{warns} warning{'s' if warns > 1 else ''}[/{design.WARN}]"
    else:
        summary_color = design.OK
        summary_icon = "OK"
        summary_tail = f"  [{design.OK}]all clear[/{design.OK}]"

    console.print()
    console.print(
        Text.assemble(
            (f" {summary_icon} ", f"bold {summary_color}"),
            ("Velune Environment — ", "bold white"),
            (f"{total} checks", "white"),
        ).__add__(Text.from_markup(summary_tail))
    )
    console.print()

    # --- Per-category panels ---
    status_icons = {
        "ok": (f"[{design.OK}]ok[/{design.OK}]", design.OK),
        "warn": (f"[{design.WARN}]warn[/{design.WARN}]", design.WARN),
        "fail": (f"[{design.DANGER}]fail[/{design.DANGER}]", design.DANGER),
        "error": (f"[{design.DANGER}]fail[/{design.DANGER}]", design.DANGER),
    }

    for cat in categories:
        cat_results = grouped[cat]
        if not cat_results:
            continue

        cat_fails = any(r["status"] in ("fail", "error") for r in cat_results)
        cat_warns = any(r["status"] == "warn" for r in cat_results)
        border_color = design.DANGER if cat_fails else design.WARN if cat_warns else design.FAINT

        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column("icon", width=3, no_wrap=True)
        table.add_column("name", style=f"bold {design.MUTED}", no_wrap=True)
        table.add_column("details", style=design.MUTED)

        for r in cat_results:
            icon_markup, _ = status_icons.get(r["status"], ("?", design.MUTED))
            table.add_row(icon_markup, r["name"], r.get("message", ""))

        console.print(
            Panel(
                table,
                title=f"[bold]{cat}[/bold]",
                border_style=border_color,
                padding=(0, 1),
            )
        )
