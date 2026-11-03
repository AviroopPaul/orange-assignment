"""A TCP proxy the harness can switch off, so an outage is real and needs no Docker socket."""

import asyncio


class TcpProxy:
    def __init__(self, listen_port: int, host: str, port: int) -> None:
        self.listen_port = listen_port
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._down = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen_port)

    async def stop(self) -> None:
        self.down()
        if self._server is not None:
            self._server.close()

    def down(self) -> None:
        """New connections are closed at once, and open ones are cut."""
        self._down = True
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()

    def up(self) -> None:
        self._down = False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._down:
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(self._host, self._port)
        except OSError:
            writer.close()
            return
        self._writers |= {writer, upstream_writer}
        await asyncio.gather(
            self._pump(reader, upstream_writer),
            self._pump(upstream_reader, writer),
            return_exceptions=True,
        )

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
