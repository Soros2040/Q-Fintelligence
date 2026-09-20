"""CONNECT proxy that permits only the configured HTTPS model host."""

from __future__ import annotations

import argparse
import selectors
import socket
import threading
from contextlib import closing

MAX_HEADER_BYTES = 8_192
MAX_CONNECTIONS = 32
SOCKET_TIMEOUT_SECONDS = 360


def _read_header(client: socket.socket) -> bytes:
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = client.recv(min(4_096, MAX_HEADER_BYTES - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) >= MAX_HEADER_BYTES:
            raise ValueError("proxy request header is too large")
    return bytes(payload)


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while True:
            events = selector.select(timeout=SOCKET_TIMEOUT_SECONDS)
            if not events:
                return
            for key, _mask in events:
                source = key.fileobj
                target = key.data
                payload = source.recv(65_536)
                if not payload:
                    return
                target.sendall(payload)
    finally:
        selector.close()


def _handle(client: socket.socket, allow_host: str, semaphore: threading.Semaphore) -> None:
    try:
        client.settimeout(SOCKET_TIMEOUT_SECONDS)
        header = _read_header(client)
        request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="strict")
        method, authority, _version = request_line.split(" ", 2)
        host, separator, raw_port = authority.rpartition(":")
        if (
            method.upper() != "CONNECT"
            or not separator
            or host.casefold() != allow_host.casefold()
            or raw_port != "443"
        ):
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        with closing(
            socket.create_connection((allow_host, 443), timeout=SOCKET_TIMEOUT_SECONDS)
        ) as upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _relay(client, upstream)
    except (OSError, UnicodeError, ValueError):
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
    finally:
        client.close()
        semaphore.release()


def serve(allow_host: str) -> None:
    semaphore = threading.BoundedSemaphore(MAX_CONNECTIONS)
    with socket.create_server(("0.0.0.0", 3128), reuse_port=False) as server:
        while True:
            client, _address = server.accept()
            if not semaphore.acquire(blocking=False):
                client.sendall(b"HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n")
                client.close()
                continue
            threading.Thread(
                target=_handle,
                args=(client, allow_host, semaphore),
                daemon=True,
            ).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-host", required=True)
    arguments = parser.parse_args()
    allow_host = arguments.allow_host.strip().rstrip(".")
    if not allow_host or any(character in allow_host for character in "/:@[]"):
        raise SystemExit("invalid allowlisted host")
    serve(allow_host)


if __name__ == "__main__":
    main()
