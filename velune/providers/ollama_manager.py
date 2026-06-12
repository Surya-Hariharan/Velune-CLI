"""Ollama pull / delete helpers with live progress rendering."""

from __future__ import annotations

import asyncio
import json

import httpx
from rich.console import Console
from rich.live import Live
from rich.text import Text

OLLAMA_BASE = "http://localhost:11434"

RECOMMENDED_MODELS: list[dict] = [
    {
        "model_id": "qwen2.5-coder:7b",
        "size_gb": 4.7,
        "description": "Best coding model at 7B — recommended default",
        "skill": "coding",
        "ram_needed": "8 GB",
    },
    {
        "model_id": "qwen2.5-coder:14b",
        "size_gb": 9.0,
        "description": "Better coding quality, needs 16 GB RAM",
        "skill": "coding",
        "ram_needed": "16 GB",
    },
    {
        "model_id": "phi4",
        "size_gb": 9.1,
        "description": "Microsoft Phi-4 — excellent reasoning + coding",
        "skill": "reasoning",
        "ram_needed": "16 GB",
    },
    {
        "model_id": "deepseek-r1:8b",
        "size_gb": 4.9,
        "description": "DeepSeek R1 reasoning — strong for complex tasks",
        "skill": "reasoning",
        "ram_needed": "8 GB",
    },
    {
        "model_id": "llama3.2:3b",
        "size_gb": 2.0,
        "description": "Fastest local model — good for 8 GB machines",
        "skill": "general",
        "ram_needed": "4 GB",
    },
    {
        "model_id": "mistral:7b",
        "size_gb": 4.1,
        "description": "Fast general purpose — good instruction following",
        "skill": "general",
        "ram_needed": "8 GB",
    },
    {
        "model_id": "nomic-embed-text",
        "size_gb": 0.3,
        "description": "Embedding model — required for semantic memory",
        "skill": "embedding",
        "ram_needed": "1 GB",
    },
    {
        "model_id": "codellama:13b",
        "size_gb": 7.4,
        "description": "Meta CodeLlama — strong at multi-language coding",
        "skill": "coding",
        "ram_needed": "16 GB",
    },
    {
        "model_id": "gemma2:9b",
        "size_gb": 5.4,
        "description": "Google Gemma 2 — balanced quality at 9B",
        "skill": "general",
        "ram_needed": "8 GB",
    },
    {
        "model_id": "qwen2.5:72b",
        "size_gb": 47.0,
        "description": "Flagship 72B — requires 48 GB RAM or 24 GB VRAM",
        "skill": "general",
        "ram_needed": "48 GB",
    },
]


class OllamaManager:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=None)

    async def is_running(self) -> bool:
        try:
            r = await self._client.get(f"{OLLAMA_BASE}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    async def list_local_models(self) -> list[str]:
        try:
            r = await self._client.get(f"{OLLAMA_BASE}/api/tags", timeout=5.0)
            if r.status_code == 200:
                return [m["name"] for m in r.json().get("models", [])]
            return []
        except Exception:
            return []

    async def pull_model(self, model_id: str, console: Console) -> bool:
        console.print(f"[cyan]Pulling {model_id} from Ollama registry...[/cyan]")
        console.print("[dim]This may take several minutes for large models.[/dim]\n")

        current_status: list[str] = ["Initializing..."]
        current_percent: list[int] = [0]
        completed: list[bool] = [False]
        error_msg: list[str | None] = [None]

        async def do_pull() -> None:
            try:
                async with self._client.stream(
                    "POST",
                    f"{OLLAMA_BASE}/api/pull",
                    json={"name": model_id, "stream": True},
                ) as response:
                    if response.status_code != 200:
                        error_msg[0] = f"HTTP {response.status_code}"
                        return
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            status = data.get("status", "")
                            current_status[0] = status
                            total = data.get("total", 0)
                            done = data.get("completed", 0)
                            if total > 0:
                                current_percent[0] = int((done / total) * 100)
                            if status == "success":
                                completed[0] = True
                                return
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                error_msg[0] = str(e)

        def render_progress() -> Text:
            t = Text()
            pct = current_percent[0]
            filled = int(pct / 5)
            bar = "█" * filled + "░" * (20 - filled)
            t.append(f"  [{bar}] ", style="cyan")
            t.append(f"{pct:3d}%  ", style="bold white")
            t.append(current_status[0][:60], style="dim")
            return t

        pull_task = asyncio.create_task(do_pull())

        with Live(
            render_progress(),
            console=console,
            refresh_per_second=4,
            vertical_overflow="visible",
        ) as live:
            while not pull_task.done():
                live.update(render_progress())
                await asyncio.sleep(0.25)

        await pull_task

        if error_msg[0]:
            console.print(f"[red]✗ Pull failed: {error_msg[0]}[/red]")
            return False

        if completed[0]:
            console.print(f"\n[green]✓ {model_id} downloaded successfully.[/green]")
            return True

        console.print("[yellow]Pull completed with unknown status.[/yellow]")
        return True

    async def delete_model(self, model_id: str) -> bool:
        try:
            r = await self._client.delete(
                f"{OLLAMA_BASE}/api/delete",
                json={"name": model_id},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
