"""Versioned MCP boundary from OpenHands to the QF Control Plane Tool Host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.tools import Tool
from pydantic import PrivateAttr

MCP_GATEWAY_VERSION = "qf.openhands-mcp.v1"


class _BoundaryAuditMiddleware:
    """Persist only non-secret MCP routing metadata for failed requests."""

    def __init__(self, app: Any, state_root: Path) -> None:
        self.app = app
        self.path = state_root / "mcp-boundary-audit.json"
        self._lock = threading.Lock()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.decode("latin-1").lower() in {"host", "origin"}
        }

        async def audited_send(message: dict[str, Any]) -> None:
            status = int(message.get("status", 0))
            if message.get("type") == "http.response.start" and status >= 400:
                record = {
                    "schemaVersion": "qf.openhands-mcp-boundary-audit.v1",
                    "status": status,
                    "host": headers.get("host"),
                    "origin": headers.get("origin"),
                }
                temporary = self.path.with_suffix(".tmp")
                with self._lock:
                    temporary.write_text(
                        json.dumps(record, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, self.path)
            await send(message)

        await self.app(scope, receive, audited_send)


class QfGatewayTool(Tool):
    """FastMCP tool retaining the exact QF-owned JSON Schema."""

    _handler: Callable[[str, dict[str, Any]], Any] = PrivateAttr()

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[[str, dict[str, Any]], Any],
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            parameters=parameters,
            output_schema=None,
            timeout=920.0,
        )
        self._handler = handler

    async def run(self, arguments: dict[str, Any]):
        result = await asyncio.to_thread(self._handler, self.name, arguments)
        return self.convert_result(result)


class McpToolGateway:
    """Serve one capability-scoped MCP endpoint over a protected Unix socket."""

    def __init__(
        self,
        *,
        state_root: Path,
        tool_specs: list[dict[str, Any]],
        handler: Callable[[str, dict[str, Any]], Any],
    ) -> None:
        self.state_root = state_root.resolve()
        local_root = next(
            (candidate for candidate in self.state_root.parents if candidate.name == ".local"),
            self.state_root.parent,
        )
        socket_root = local_root / "ohs"
        socket_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        socket_name = hashlib.sha256(str(self.state_root).encode()).hexdigest()[:20]
        self.socket_path = socket_root / f"{socket_name}.sock"
        run_slug = hashlib.sha256(str(self.state_root).encode()).hexdigest()[:16]
        self.allowed_host = f"qf-oh-tools-{run_slug}:8001"
        self.capability_path = self._load_or_create_capability()
        tools = [
            QfGatewayTool(
                name=str(spec["name"]),
                description=str(spec["description"]),
                parameters=dict(spec["parameters"]),
                handler=handler,
            )
            for spec in tool_specs
        ]
        self._tool_names = sorted(tool.name for tool in tools)
        self.server = FastMCP(
            name="q-fintelligence Tool Gateway",
            instructions=(
                "Use only the QF tools authorized for this conversation. "
                "The QF Control Plane enforces approvals, Query ID idempotency, "
                "artifact lineage, scientific gates, and hardware policy."
            ),
            version=MCP_GATEWAY_VERSION,
            tools=tools,
            mask_error_details=True,
            strict_input_validation=True,
        )
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None

    def _load_or_create_capability(self) -> str:
        path = self.state_root / "mcp-capability.json"
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            token = value.get("token")
            if (
                value.get("schemaVersion") == MCP_GATEWAY_VERSION
                and isinstance(token, str)
                and len(token) >= 43
            ):
                return f"/mcp/{token}"
            raise RuntimeError("QF MCP capability record is invalid")
        token = secrets.token_urlsafe(36)
        path.write_text(
            json.dumps(
                {"schemaVersion": MCP_GATEWAY_VERSION, "token": token},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return f"/mcp/{token}"

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("QF MCP Tool Gateway is already running")
        self.socket_path.unlink(missing_ok=True)
        app = self.server.http_app(
            path=self.capability_path,
            json_response=True,
            stateless_http=False,
            transport="streamable-http",
            allowed_hosts=[self.allowed_host],
            # The MCP client may set an Origin for the Agent Server process
            # rather than the tool-proxy host. The gateway is reachable only
            # through its per-run internal Docker network, Unix socket, and
            # unguessable capability path; exact Host validation remains on.
            allowed_origins=["*"],
        )
        app = _BoundaryAuditMiddleware(app, self.state_root)
        config = uvicorn.Config(
            app,
            uds=str(self.socket_path),
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._server = server
        self._thread = threading.Thread(
            target=server.run,
            name="qf-openhands-mcp-gateway",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                os.chmod(self.socket_path, 0o600)
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                        probe.settimeout(0.2)
                        probe.connect(str(self.socket_path))
                    return
                except OSError:
                    pass
            time.sleep(0.05)
        raise TimeoutError("QF MCP Tool Gateway did not bind its Unix socket")

    def close(self) -> None:
        server = self._server
        thread = self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        self._server = None
        self._thread = None
        self.socket_path.unlink(missing_ok=True)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tool_names)
