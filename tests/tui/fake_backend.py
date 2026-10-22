"""A local stand-in for every HTTP backend the REPL might talk to.

* Ollama routes (``/api/tags``, ``/api/show``, ``/api/chat``) are served for the
  local adapter via ``OLLAMA_HOST``.
* OpenAI-shaped routes (``.../models``, ``.../chat/completions``) are served for
  every cloud provider: ``_launcher.py`` reroutes all non-loopback httpx traffic
  here and adds ``X-Velune-Orig-Host`` so tests can prove nothing left the box.
* A bearer token equal to ``FAKE_KEY`` is "valid"; anything else gets a 401.

The reply text is chosen from the last user message so tests can request a long,
multiline, or slow response.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_KEY = "sk-velune-test-0000000000000000FAKE"
#: A key containing this marker gets HTTP 503 from every cloud route — a
#: provider outage, which must save the key unverified rather than reject it.
OUTAGE_MARKER = "OUTAGE"
MODEL = "fake-model:latest"

LONG_REPLY = " ".join(f"word{i}" for i in range(400))
MULTILINE_REPLY = (
    "Here is a list:\n\n- alpha\n- beta\n- gamma\n\n```python\nprint('hello')\n```\n\nDone."
)


def reply_for(prompt: str) -> str:
    if "LONGREPLY" in prompt:
        return LONG_REPLY
    if "MULTILINE" in prompt:
        return MULTILINE_REPLY
    return "FAKE-REPLY-OK"


def _last_user(msgs: list[dict]) -> str:
    for m in reversed(msgs or []):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c)
    return ""


class _Handler(BaseHTTPRequestHandler):
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

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        key = self.headers.get("x-api-key") or self.headers.get("x-goog-api-key") or ""
        return auth == f"Bearer {FAKE_KEY}" or key == FAKE_KEY

    def _record(self, method: str) -> None:
        self.server.requests.append(  # type: ignore[attr-defined]
            (method, self.path, self.headers.get("X-Velune-Orig-Host", ""))
        )

    def do_HEAD(self) -> None:
        # Real Ollama answers HEAD / with 200 ("Ollama is running"); discovery
        # uses it as its liveness check.
        self._record("HEAD")
        self.send_response(200 if self.path.split("?")[0] == "/" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._record("GET")
        p = self.path.split("?")[0]
        if p.startswith("/api/tags"):
            self._json({"models": [{"name": MODEL, "model": MODEL, "size": 1, "details": {}}]})
        elif p.startswith("/api/version"):
            self._json({"version": "0.0.0-fake"})
        elif p.endswith("/models"):
            if OUTAGE_MARKER in self.headers.get("Authorization", ""):
                self._json({"error": {"message": "upstream unavailable"}}, 503)
            elif not self._authorized():
                self._json(
                    {"error": {"message": "Invalid API key", "type": "invalid_request_error"}}, 401
                )
            else:
                self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._json({}, 404)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        self._record("POST")
        p = self.path.split("?")[0]
        if p.startswith("/api/show"):
            self._json({"parameters": "num_ctx 4096", "model_info": {}, "details": {}})
        elif p.startswith("/api/chat"):
            self._stream(_last_user(payload.get("messages")), payload.get("stream", True), "ollama")
        elif p.endswith("/chat/completions"):
            if not self._authorized():
                self._json(
                    {"error": {"message": "Invalid API key", "type": "invalid_request_error"}}, 401
                )
            else:
                self._stream(
                    _last_user(payload.get("messages")), payload.get("stream", False), "openai"
                )
        else:
            self._json({}, 404)

    def _stream(self, prompt: str, stream: bool, dialect: str) -> None:
        text = reply_for(prompt)
        if not stream:
            if dialect == "ollama":
                self._json(
                    {
                        "model": MODEL,
                        "message": {"role": "assistant", "content": text},
                        "done": True,
                    }
                )
            else:
                self._json(
                    {
                        "id": "x",
                        "object": "chat.completion",
                        "model": MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": text},
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                    }
                )
            return
        self.send_response(200)
        self.send_header(
            "Content-Type", "application/x-ndjson" if dialect == "ollama" else "text/event-stream"
        )
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(raw: bytes) -> None:
            self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
            self.wfile.flush()

        delay = self.server.chunk_delay  # type: ignore[attr-defined]
        words = text.split(" ")
        for i, w in enumerate(words):
            piece = w if i == len(words) - 1 else w + " "
            if dialect == "ollama":
                emit(
                    (
                        json.dumps(
                            {
                                "model": MODEL,
                                "message": {"role": "assistant", "content": piece},
                                "done": False,
                            }
                        )
                        + "\n"
                    ).encode()
                )
            else:
                obj = {
                    "id": "x",
                    "object": "chat.completion.chunk",
                    "model": MODEL,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
                emit(f"data: {json.dumps(obj)}\n\n".encode())
            if delay:
                time.sleep(delay)
        if dialect == "ollama":
            emit(
                (
                    json.dumps(
                        {
                            "model": MODEL,
                            "message": {"role": "assistant", "content": ""},
                            "done": True,
                            "prompt_eval_count": 3,
                            "eval_count": len(words),
                        }
                    )
                    + "\n"
                ).encode()
            )
        else:
            end = {
                "id": "x",
                "object": "chat.completion.chunk",
                "model": MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": len(words),
                    "total_tokens": 3 + len(words),
                },
            }
            emit(f"data: {json.dumps(end)}\n\n".encode())
            emit(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class FakeBackend:
    def __init__(self) -> None:
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.requests = []  # type: ignore[attr-defined]
        self._srv.chunk_delay = 0.0  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self._srv.server_address[1]}"

    @property
    def requests(self) -> list[tuple[str, str, str]]:
        """``(method, path, original_host_if_rerouted)`` for every request served."""
        return self._srv.requests  # type: ignore[attr-defined]

    @property
    def chunk_delay(self) -> float:
        return self._srv.chunk_delay  # type: ignore[attr-defined]

    @chunk_delay.setter
    def chunk_delay(self, v: float) -> None:
        self._srv.chunk_delay = v  # type: ignore[attr-defined]

    def start(self) -> FakeBackend:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
