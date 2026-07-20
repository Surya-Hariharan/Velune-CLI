import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console

from velune.daemon.client import DaemonClient
from velune.daemon.transport import DAEMON_PID_FILE

daemon_cmd = typer.Typer(help="Start, stop, or check the background service.")
console = Console()


@daemon_cmd.command("start")
def daemon_start(workspace: Path = typer.Option(Path.cwd(), help="Workspace root")):
    """Start Velune daemon in background."""
    if DaemonClient.is_running():
        console.print("[yellow]Daemon is already running.[/yellow]")
        return

    workspace_abs = workspace.resolve()

    # Detached background process spawn. start_new_session (setsid) is POSIX
    # only; Windows needs its own creation flags to fully detach from the
    # launching console (no shared console, own process group).
    if sys.platform == "win32":
        subprocess.Popen(
            [sys.executable, "-m", "velune.daemon.server", str(workspace_abs)],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            [sys.executable, "-m", "velune.daemon.server", str(workspace_abs)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # Wait for daemon to become active
    for _ in range(30):
        time.sleep(0.1)
        if DaemonClient.is_running():
            console.print("[green]Daemon started.[/green]")
            return

    console.print("[red]Failed to start daemon.[/red]")


@daemon_cmd.command("stop")
def daemon_stop():
    """Stop background Velune daemon process."""
    if not DaemonClient.is_running():
        console.print("[yellow]Daemon is not running.[/yellow]")
        return

    if DAEMON_PID_FILE.exists():
        pid = int(DAEMON_PID_FILE.read_text())
        try:
            os.kill(pid, signal.SIGTERM)
            console.print("[green]Daemon stopped.[/green]")
        except Exception as e:
            console.print(f"[red]Failed to stop daemon PID {pid}: {e}[/red]")
        finally:
            try:
                DAEMON_PID_FILE.unlink()
            except Exception:
                pass
    else:
        console.print("[yellow]Daemon running but PID file missing.[/yellow]")


@daemon_cmd.command("status")
def daemon_status():
    """Display daemon running status and PID."""
    if DaemonClient.is_running():
        try:
            from velune.kernel.entrypoint import run_async

            result = run_async(DaemonClient.send_command("ping"))
            console.print(f"[green]Daemon running (PID: {result['pid']})[/green]")
        except Exception as e:
            console.print(f"[red]Daemon running but communication failed: {e}[/red]")
    else:
        console.print("[yellow]Daemon not running[/yellow]")
