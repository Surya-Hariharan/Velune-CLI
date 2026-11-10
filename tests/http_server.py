"""A tiny in-process HTTP/1.1 server for the HTTP integration tests (VAIR A3).

Real sockets on 127.0.0.1, so the tests exercise the actual pinned transport,
redirects, timeouts and size limits rather than a mock. Offline and deterministic.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

Reply = tuple[int, dict[str, str], bytes]
Handler = Callable[["Seen"], "Reply | Awaitable[Reply]"]

_REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


@dataclass
class Seen:
    method: str
    target: str
    headers: dict[str, str]  # lowercase names
    body: bytes
    peer: tuple[str, int] = ("", 0)

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]

    @property
    def query(self) -> str:
        return self.target.split("?", 1)[1] if "?" in self.target else ""


@dataclass
class TestServer:
    handler: Handler
    ssl_context: ssl.SSLContext | None = None
    requests: list[Seen] = field(default_factory=list)
    port: int = 0
    _server: asyncio.AbstractServer | None = None
    connections: int = 0

    async def start(self) -> TestServer:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def base(self) -> str:
        scheme = "https" if self.ssl_context else "http"
        return f"{scheme}://127.0.0.1:{self.port}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                method, target, _version = lines[0].split(" ", 2)
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        name, _, value = line.partition(":")
                        headers[name.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0") or 0)
                body = await reader.readexactly(length) if length else b""
                seen = Seen(
                    method, target, headers, body, writer.get_extra_info("peername") or ("", 0)
                )
                self.requests.append(seen)
                result: Any = self.handler(seen)
                if asyncio.iscoroutine(result):
                    result = await result
                status, reply_headers, payload = result
                lines_out = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}"]
                names = {k.lower() for k in reply_headers}
                for key, value in reply_headers.items():
                    lines_out.append(f"{key}: {value}")
                if "content-length" not in names and "transfer-encoding" not in names:
                    lines_out.append(f"Content-Length: {len(payload)}")
                lines_out.append("Connection: close")
                writer.write(("\r\n".join(lines_out) + "\r\n\r\n").encode("latin-1"))
                if method != "HEAD":
                    writer.write(payload)
                await writer.drain()
                break
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def start_server(handler: Handler, ssl_context: ssl.SSLContext | None = None) -> TestServer:
    return await TestServer(handler, ssl_context).start()


def json_reply(payload: str, status: int = 200, **headers: str) -> Reply:
    return status, {"Content-Type": "application/json", **headers}, payload.encode()


def text_reply(text: str, status: int = 200, **headers: str) -> Reply:
    return status, {"Content-Type": "text/plain", **headers}, text.encode()


def make_tls_pair(hostname: str, tmp_path) -> tuple[ssl.SSLContext, ssl.SSLContext, list[str]]:
    """A throw-away CA-less self-signed certificate for *hostname*.

    Returns ``(server_context, client_context, sni_seen)``: the client context trusts
    exactly this certificate, and ``sni_seen`` collects the server names clients ask for.
    """
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(hostname), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    sni_seen: list[str] = []
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_file, key_file)
    server.sni_callback = lambda sock, name, ctx: sni_seen.append(name or "") or None
    client = ssl.create_default_context(cafile=str(cert_file))
    return server, client, sni_seen
