import asyncio
import json
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

DAEMON_SOCKET_PATH = Path.home() / ".velune" / "daemon.sock"
DAEMON_PID_FILE = Path.home() / ".velune" / "daemon.pid"
DAEMON_PIPE_ADDRESS = r"\\.\pipe\velune_daemon"


def get_ipc_address() -> str:
    if sys.platform == "win32":
        return DAEMON_PIPE_ADDRESS
    return str(DAEMON_SOCKET_PATH)


class IpcServer:
    def __init__(self, address: str, handle_callback: Callable[[dict[str, Any]], Any]):
        self.address = address
        self.handle_callback = handle_callback
        self._server = None
        self._win_server = None

    async def start(self):
        # Ensure directories exist
        if sys.platform != "win32":
            socket_path = Path(self.address)
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            if socket_path.exists():
                try:
                    socket_path.unlink()
                except Exception:
                    pass

            self._server = await asyncio.start_unix_server(
                self._handle_unix_client, path=self.address
            )
        else:
            self._win_server = _WindowsNamedPipeServer(self.address, self.handle_callback)
            self._win_server.start()

    async def serve_forever(self):
        if sys.platform != "win32":
            await self._server.serve_forever()
        else:
            await self._win_server.serve_forever()

    def close(self):
        if self._server:
            self._server.close()
        if self._win_server:
            self._win_server.close()

    async def _handle_unix_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.read(65536)
            if not data:
                return
            request = json.loads(data.decode("utf-8"))
            response = await self.handle_callback(request)
            writer.write(json.dumps(response).encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


class _WindowsNamedPipeServer:
    def __init__(self, address: str, handle_callback: Callable[[dict[str, Any]], Any]):
        self.address = address
        self.handle_callback = handle_callback
        self.listener = None
        self.is_running = False

    def start(self):
        from multiprocessing.connection import Listener

        self.listener = Listener(self.address, "AF_PIPE")
        self.is_running = True

    def close(self):
        self.is_running = False
        # Wake up the blocked accept() thread with a dummy connection on Windows
        try:
            from multiprocessing.connection import Client

            conn = Client(self.address, "AF_PIPE")
            conn.close()
        except Exception:
            pass
        if self.listener:
            try:
                self.listener.close()
            except Exception:
                pass

    async def serve_forever(self):
        loop = asyncio.get_running_loop()
        while self.is_running:
            try:
                conn = await loop.run_in_executor(None, self.listener.accept)
                from velune.core.task_registry import track

                track(asyncio.create_task(self._handle_conn(conn), name="daemon_conn_handler"))
            except Exception:
                if not self.is_running:
                    break
                await asyncio.sleep(0.1)

    async def _handle_conn(self, conn):
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, conn.recv)
            request = json.loads(data)
            response = await self.handle_callback(request)
            await loop.run_in_executor(None, conn.send, json.dumps(response))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class IpcClient:
    @staticmethod
    def is_running(address: str) -> bool:
        if sys.platform != "win32":
            socket_path = Path(address)
            if not socket_path.exists():
                return False
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(address)
                sock.close()
                return True
            except Exception:
                return False
        else:
            from multiprocessing.connection import Client

            try:
                conn = Client(address, "AF_PIPE")
                conn.close()
                return True
            except Exception:
                return False

    @staticmethod
    async def send_command(address: str, command: str, **kwargs) -> dict:
        request = {"command": command, **kwargs}
        if sys.platform != "win32":
            reader, writer = await asyncio.open_unix_connection(address)
            try:
                writer.write(json.dumps(request).encode("utf-8"))
                await writer.drain()
                data = await reader.read(65536)
                return json.loads(data.decode("utf-8"))
            finally:
                writer.close()
        else:
            from multiprocessing.connection import Client

            loop = asyncio.get_running_loop()

            def _send():
                conn = Client(address, "AF_PIPE")
                try:
                    conn.send(json.dumps(request))
                    res = conn.recv()
                    return json.loads(res)
                finally:
                    conn.close()

            return await loop.run_in_executor(None, _send)
