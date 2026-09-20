"""TCP-to-Unix proxy for the sidecar-owned QF MCP Tool Gateway."""

from __future__ import annotations

import argparse
import asyncio
import contextlib


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def _serve(socket_path: str, listen_port: int) -> None:
    async def handle(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            target_reader, target_writer = await asyncio.open_unix_connection(socket_path)
        except OSError:
            client_writer.close()
            await client_writer.wait_closed()
            return
        await asyncio.gather(
            _pump(client_reader, target_writer),
            _pump(target_reader, client_writer),
            return_exceptions=True,
        )

    server = await asyncio.start_server(handle, host="0.0.0.0", port=listen_port)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--listen-port", required=True, type=int)
    arguments = parser.parse_args()
    asyncio.run(_serve(arguments.socket_path, arguments.listen_port))


if __name__ == "__main__":
    main()
