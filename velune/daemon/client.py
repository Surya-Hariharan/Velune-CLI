from velune.daemon.transport import IpcClient, get_ipc_address


class DaemonClient:
    """Thin client for communicating with Velune daemon."""

    @staticmethod
    def is_running() -> bool:
        return IpcClient.is_running(get_ipc_address())

    @staticmethod
    async def send_command(command: str, **kwargs) -> dict:
        return await IpcClient.send_command(get_ipc_address(), command, **kwargs)
