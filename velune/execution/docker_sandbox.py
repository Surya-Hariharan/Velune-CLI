"""Docker-based execution sandbox with true OS-level container isolation.

Provides the same interface as SubprocessSandbox but runs every command
inside a per-session Docker container with the workspace mounted as a volume.
This prevents any agent-executed code from affecting the host beyond the
declared workspace directory.

Lifecycle:
    sandbox = DockerSandbox.for_workspace(workspace_path)
    sandbox.start()          # pulls image if needed, creates+starts container
    result = sandbox.execute(spec)
    sandbox.pause()          # docker pause (container keeps state)
    sandbox.resume()         # docker unpause
    sandbox.stop()           # docker stop + rm

Fallback:
    If Docker is not installed or the daemon is unreachable, DockerSandbox
    raises DockerUnavailableError at start() time so callers can fall back to
    SubprocessSandbox.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from velune.core.errors.execution import SandboxError
from velune.execution.command_spec import CommandSpec
from velune.execution.sandbox import SandboxResult

logger = logging.getLogger("velune.execution.docker_sandbox")

#: Docker image used for the sandbox container.  Override via velune.toml
#: execution.docker_image or VELUNE_DOCKER_IMAGE env var.
DEFAULT_IMAGE = "python:3.12-slim"

#: Per-stream output cap — same as SubprocessSandbox.
DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MiB

#: Workspace is always mounted at this path inside the container.
CONTAINER_WORKSPACE = "/workspace"


class DockerUnavailableError(SandboxError):
    """Raised when the Docker daemon is unreachable or docker-py is not installed."""


class DockerSandbox:
    """OS-level isolated sandbox backed by a Docker container.

    One instance = one container = one session.  The container is started
    lazily on first execute() if start() has not been called explicitly.
    """

    def __init__(
        self,
        workspace_path: Path,
        image: str = DEFAULT_IMAGE,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        container_timeout: int = 30,
        bus: Any | None = None,
    ) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.image = image
        self.max_output_bytes = max_output_bytes
        self.container_timeout = container_timeout
        self.bus = bus

        # Unique session ID doubles as the container name so it's easy to
        # identify in `docker ps` output.
        self._session_id = f"velune-{secrets.token_hex(8)}"
        self._session_api_key = secrets.token_urlsafe(32)
        self._container: Any | None = None  # docker.models.containers.Container
        self._client: Any | None = None  # docker.DockerClient
        self._started = False
        self._lock = threading.Lock()

    @classmethod
    def for_workspace(cls, workspace_path: Path, **kwargs: Any) -> DockerSandbox:
        return cls(workspace_path, **kwargs)

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Create and start the sandbox container.

        Raises:
            DockerUnavailableError: If docker-py is not installed or Docker
                daemon is unreachable.
            SandboxError: If container creation fails for any other reason.
        """
        with self._lock:
            if self._started:
                return
            self._client = self._get_docker_client()
            self._pull_image_if_needed()
            self._create_container()
            self._started = True

    def pause(self) -> None:
        """Pause the container (freezes all processes, keeps memory)."""
        self._require_started()
        try:
            self._container.pause()
            logger.info("Container %s paused", self._session_id)
        except Exception as e:
            raise SandboxError(f"Failed to pause container: {e}") from e

    def resume(self) -> None:
        """Unpause a previously paused container."""
        self._require_started()
        try:
            self._container.unpause()
            logger.info("Container %s resumed", self._session_id)
        except Exception as e:
            raise SandboxError(f"Failed to resume container: {e}") from e

    def stop(self) -> None:
        """Stop and remove the container, releasing all resources."""
        if not self._started or self._container is None:
            return
        try:
            self._container.stop(timeout=10)
        except Exception as e:
            logger.warning("Container stop error (non-fatal): %s", e)
        try:
            self._container.remove(force=True)
        except Exception as e:
            logger.warning("Container remove error (non-fatal): %s", e)
        finally:
            self._container = None
            self._started = False
            logger.info("Container %s stopped and removed", self._session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_api_key(self) -> str:
        return self._session_api_key

    @property
    def status(self) -> str:
        """Return container status: running, paused, stopped, or unknown."""
        if not self._started or self._container is None:
            return "stopped"
        try:
            self._container.reload()
            return self._container.status  # "running", "paused", "exited", etc.
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def execute(self, spec: CommandSpec) -> SandboxResult:
        """Run a command inside the container and return the result.

        The command runs in CONTAINER_WORKSPACE (the mounted workspace).
        stdout/stderr are captured with the same byte-cap logic as
        SubprocessSandbox.  The container itself is NOT restarted between
        calls — state (installed packages, created files) persists for the
        lifetime of the container.
        """
        if not self._started:
            self.start()
        self._require_started()

        argv = spec.to_argv()
        cmd_str = " ".join(argv)
        logger.debug("Docker exec: %s", cmd_str)

        # Determine working directory inside the container.  If spec.cwd is
        # the workspace root or a sub-path, map it into the container.
        try:
            rel = spec.cwd.relative_to(self.workspace_path)
            container_cwd = f"{CONTAINER_WORKSPACE}/{rel}".rstrip("/.")
        except ValueError:
            container_cwd = CONTAINER_WORKSPACE

        start_time = time.perf_counter()
        try:
            exit_code, raw_output = self._container.exec_run(
                cmd=argv,
                workdir=container_cwd,
                environment=dict(spec.env_additions),
                demux=True,
                stdout=True,
                stderr=True,
            )
        except Exception as e:
            raise SandboxError(f"Docker exec failed: {e}") from e

        duration_ms = (time.perf_counter() - start_time) * 1000

        stdout_bytes, stderr_bytes = raw_output if raw_output else (b"", b"")
        stdout = self._decode_capped(stdout_bytes or b"")
        stderr = self._decode_capped(stderr_bytes or b"")

        return SandboxResult(
            exit_code=exit_code if exit_code is not None else 1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            peak_memory_mb=0.0,  # not tracked at container level
            peak_cpu_pct=0.0,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_docker_client(self) -> Any:
        try:
            import docker
        except ImportError as e:
            raise DockerUnavailableError(
                "docker-py is not installed. Install it with: pip install docker"
            ) from e
        try:
            client = docker.from_env()
            client.ping()
            return client
        except Exception as e:
            raise DockerUnavailableError(
                f"Docker daemon unreachable: {e}. Ensure Docker Desktop is running, then retry."
            ) from e

    def _pull_image_if_needed(self) -> None:
        try:
            self._client.images.get(self.image)
            logger.debug("Docker image %s already present", self.image)
        except Exception:
            logger.info("Pulling Docker image %s ...", self.image)
            try:
                self._client.images.pull(self.image)
                logger.info("Image %s pulled successfully", self.image)
            except Exception as e:
                raise SandboxError(f"Failed to pull Docker image '{self.image}': {e}") from e

    def _create_container(self) -> None:
        logger.info(
            "Creating Docker container %s (image=%s, workspace=%s)",
            self._session_id,
            self.image,
            self.workspace_path,
        )
        try:
            self._container = self._client.containers.run(
                image=self.image,
                name=self._session_id,
                command="sleep infinity",  # keep container alive between exec calls
                detach=True,
                remove=False,  # we remove manually in stop()
                volumes={
                    str(self.workspace_path): {
                        "bind": CONTAINER_WORKSPACE,
                        "mode": "rw",
                    }
                },
                working_dir=CONTAINER_WORKSPACE,
                environment={
                    "VELUNE_SESSION_KEY": self._session_api_key,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUNBUFFERED": "1",
                },
                # Resource limits — sensible defaults, configurable later
                mem_limit="1g",
                nano_cpus=int(2e9),  # 2 CPUs
                network_mode="bridge",
            )
            logger.info("Container %s started", self._session_id)
        except Exception as e:
            raise SandboxError(f"Failed to create Docker container: {e}") from e

    def _require_started(self) -> None:
        if not self._started or self._container is None:
            raise SandboxError("DockerSandbox has not been started. Call start() first.")

    def _decode_capped(self, data: bytes) -> str:
        if len(data) > self.max_output_bytes:
            data = data[: self.max_output_bytes]
            suffix = f"\n[velune: output truncated at {self.max_output_bytes} bytes]"
            return data.decode("utf-8", errors="replace") + suffix
        return data.decode("utf-8", errors="replace")
