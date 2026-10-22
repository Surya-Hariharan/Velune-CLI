"""A small, real HTTP server that behaves like the parts of Ollama Velune uses.

Runs on 127.0.0.1 with an OS-assigned port; point ``OLLAMA_HOST`` at
``server.host``. ``stop()`` closes the socket so tests can simulate the daemon
going away, and ``start()`` brings it back on the *same* port.

Routes: ``/api/version``, ``/api/tags``, ``/api/show``, ``/api/chat``,
``/api/generate``, ``HEAD/POST /api/blobs/<digest>``, ``/api/create``.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, models: list[str] | None = None) -> None:
        self.models: list[str] = list(models or [])
        self.blobs: dict[str, int] = {}
        self.requests: list[tuple[str, str]] = []
        self.create_status = 200
        self.create_error = ""
        self.generate_status = 200
        self._port = self._free_port()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self._port}"

    @property
    def url(self) -> str:
        return f"http://{self.host}"

    def start(self) -> FakeOllama:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a) -> None:
                pass

            def _json(self, obj, status: int = 200) -> None:
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> bytes:
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    data = b""
                    while True:
                        size = int(self.rfile.readline().strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            return data
                        data += self.rfile.read(size)
                        self.rfile.readline()
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_HEAD(self) -> None:
                fake.requests.append(("HEAD", self.path))
                digest = self.path.rsplit("/", 1)[-1]
                self.send_response(200 if digest in fake.blobs else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path))
                if self.path.startswith("/api/version"):
                    self._json({"version": "0.0.0-fake"})
                elif self.path.startswith("/api/tags"):
                    self._json(
                        {"models": [{"name": m, "model": m, "details": {}} for m in fake.models]}
                    )
                else:
                    self._json({}, 404)

            def do_POST(self) -> None:
                fake.requests.append(("POST", self.path))
                raw = self._body()
                if self.path.startswith("/api/blobs/"):
                    digest = self.path.rsplit("/", 1)[-1]
                    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
                    if actual != digest:
                        self._json({"error": "digest mismatch"}, 400)
                        return
                    fake.blobs[digest] = len(raw)
                    self.send_response(201)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.loads(raw or b"{}")
                if self.path.startswith("/api/create"):
                    if fake.create_status != 200:
                        self._json(
                            {"error": fake.create_error or "create failed"}, fake.create_status
                        )
                        return
                    missing = [d for d in payload.get("files", {}).values() if d not in fake.blobs]
                    if missing:
                        self._json({"error": f"missing blobs {missing}"}, 400)
                        return
                    fake.models.append(payload["model"])
                    self._json({"status": "success"})
                elif self.path.startswith("/api/show"):
                    self._json({"capabilities": ["completion"], "details": {"family": "llama"}})
                elif self.path.startswith("/api/generate"):
                    self._json({"response": "OK", "done": True}, fake.generate_status)
                elif self.path.startswith("/api/chat"):
                    self._json(
                        {
                            "message": {"role": "assistant", "content": "FAKE-OLLAMA-OK"},
                            "done": True,
                        }
                    )
                else:
                    self._json({}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
