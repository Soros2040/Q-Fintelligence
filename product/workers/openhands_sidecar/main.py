from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import queue
import resource
import sys
import threading
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from jsonschema import FormatChecker
from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validators as jsonschema_validators
from openhands.sdk.agent import Agent
from openhands.sdk.context import AgentContext
from openhands.sdk.conversation import Conversation
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import (
    ActionEvent,
    AgentErrorEvent,
    InterruptEvent,
    MessageEvent,
    ObservationEvent,
)
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.event.llm_convertible.observation import ObservationBaseEvent
from openhands.sdk.llm import LLM, TextContent
from openhands.sdk.mcp import MCPServer
from openhands.sdk.security import ConfirmRisky, SecurityRisk
from openhands.sdk.tool import Tool
from openhands.sdk.workspace import RemoteWorkspace
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool
from pydantic import SecretStr

# Python isolated mode deliberately omits the script directory from sys.path.
# Add only the immutable sidecar code root so QF's versioned extensions remain
# importable without exposing the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qf_openhands.mcp_gateway import MCP_GATEWAY_VERSION, McpToolGateway
from qf_openhands.sandbox import (
    DockerServerHandle,
    HardenedDockerAgentServer,
    create_repository_snapshot,
)
from qf_openhands.security import QfSecurityAnalyzer

PROTOCOL_VERSION = "qf.agent-runtime.v2"
SIDECAR_VERSION = "qf-openhands-sidecar.v3"
STATE_ROOT = Path(os.environ.get("QF_OPENHANDS_STATE_ROOT", "/run/qf-state"))
WORKSPACE_ROOT = Path(os.environ.get("QF_OPENHANDS_WORKSPACE_ROOT", "/tmp/workspace"))
PROJECT_SNAPSHOT_SOURCE = Path(
    os.environ.get("QF_OPENHANDS_PROJECT_SOURCE", "/opt/qf-project")
)
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_TOOL_WAIT_SECONDS = 920
PROTOCOL_QUEUE_CAPACITY = 1024
QUEUED_MESSAGE_CAPACITY = 64
MANIFEST_SCHEMA_VERSION = "qf.openhands-runtime-manifest.v1"
TOOL_JOURNAL_SCHEMA_VERSION = "qf.openhands-tool-journal.v1"
EXPECTED_SDK_VERSION = "1.39.0+qf.noobservability.1"
SIDECAR_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class WorkspaceActionScopeRequiredError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _execution_status_requires_resolution(
    status: ConversationExecutionStatus,
) -> bool:
    return status in {
        ConversationExecutionStatus.PAUSED,
        ConversationExecutionStatus.WAITING_FOR_CONFIRMATION,
    }


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (_canonical(value) + "\n").encode("utf-8")
    if len(encoded) > MAX_LINE_BYTES:
        raise RuntimeError("sidecar state record exceeds the protocol limit")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json_record(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"cannot read sidecar state record {path.name}") from error
    if len(raw) > MAX_LINE_BYTES:
        raise RuntimeError(f"sidecar state record {path.name} exceeds the size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"sidecar state record {path.name} is invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"sidecar state record {path.name} is not an object")
    return value


def _assert_local_json_references(value: Any) -> None:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if reference is not None and (
            not isinstance(reference, str) or not reference.startswith("#")
        ):
            raise RuntimeError("QF tool schemas may only use document-local $ref values")
        for entry in value.values():
            _assert_local_json_references(entry)
    elif isinstance(value, list):
        for entry in value:
            _assert_local_json_references(entry)


def _safe_json(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        return value if isinstance(value, int) or value == value else str(value)
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, depth + 1) for item in value[:200]]
    if isinstance(value, dict):
        return {str(key): _safe_json(entry, depth + 1) for key, entry in list(value.items())[:200]}
    if hasattr(value, "model_dump"):
        return _safe_json(value.model_dump(mode="json", exclude_none=True), depth + 1)
    return str(value)


class ProtocolWriter:
    _WAKE = object()
    _STOP = object()

    def __init__(
        self,
        *,
        capacity: int = PROTOCOL_QUEUE_CAPACITY,
        output: io.TextIOBase | None = None,
        start_thread: bool = True,
    ) -> None:
        if capacity < 1:
            raise ValueError("protocol writer capacity must be positive")
        self._output = output or sys.stdout
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=capacity)
        self._failure_lock = threading.Lock()
        self._failure: str | None = None
        self._failure_handler: Any = None
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(
                target=self._write_loop,
                name="qf-openhands-ipc-writer",
                daemon=True,
            )
            self._thread.start()

    @property
    def failure_code(self) -> str | None:
        with self._failure_lock:
            return self._failure

    def set_failure_handler(self, handler: Any) -> None:
        self._failure_handler = handler

    def _encode(self, message: dict[str, Any]) -> str:
        envelope = {"protocol": PROTOCOL_VERSION, **_safe_json(message)}
        encoded = _canonical(envelope)
        if len(encoded.encode("utf-8")) > MAX_LINE_BYTES:
            raise RuntimeError("sidecar output frame exceeds the protocol limit")
        return encoded

    def _record_failure(self, code: str) -> None:
        handler = None
        with self._failure_lock:
            if self._failure is not None:
                return
            self._failure = code
            handler = self._failure_handler
        try:
            self._queue.put_nowait(self._WAKE)
        except queue.Full:
            pass
        if handler is not None:
            try:
                handler(code)
            except Exception:
                pass

    def fail_closed(self, code: str) -> None:
        self._record_failure(code)

    def send(self, message: dict[str, Any]) -> None:
        if self.failure_code is not None:
            raise RuntimeError(f"sidecar protocol writer failed: {self.failure_code}")
        try:
            encoded = self._encode(message)
            self._queue.put(encoded, timeout=5)
        except queue.Full as error:
            self._record_failure("IPC_OUTPUT_BACKPRESSURE")
            raise RuntimeError("sidecar protocol writer queue is full") from error
        except Exception:
            self._record_failure("IPC_OUTPUT_ENCODING_FAILED")
            raise

    def send_callback(self, message: dict[str, Any]) -> bool:
        """Best-effort enqueue for SDK callbacks; this method never raises."""
        try:
            if self.failure_code is not None:
                return False
            self._queue.put_nowait(self._encode(message))
            return True
        except queue.Full:
            self._record_failure("IPC_OUTPUT_BACKPRESSURE")
        except Exception:
            self._record_failure("IPC_OUTPUT_ENCODING_FAILED")
        return False

    def _write_fatal(self, code: str) -> None:
        encoded = self._encode({"type": "fatal", "code": code})
        self._output.write(encoded + "\n")
        self._output.flush()

    def _write_loop(self) -> None:
        try:
            while True:
                item = self._queue.get()
                try:
                    failure = self.failure_code
                    if failure is not None:
                        self._write_fatal(failure)
                        return
                    if item is self._STOP:
                        return
                    if item is self._WAKE:
                        continue
                    self._output.write(str(item) + "\n")
                    self._output.flush()
                finally:
                    self._queue.task_done()
        except Exception:
            self._record_failure("IPC_OUTPUT_WRITE_FAILED")
        finally:
            self._closed.set()

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            self._closed.set()
            return
        if self.failure_code is None:
            try:
                self._queue.put(self._STOP, timeout=5)
            except queue.Full:
                self._record_failure("IPC_OUTPUT_BACKPRESSURE")
        thread.join(timeout=5)
        if thread.is_alive():
            self._record_failure("IPC_OUTPUT_SHUTDOWN_TIMEOUT")


class ToolReply:
    def __init__(self, request_sha256: str) -> None:
        self.request_sha256 = request_sha256
        self.event = threading.Event()
        self.message: dict[str, Any] | None = None


class Bridge:
    def __init__(self, writer: ProtocolWriter) -> None:
        self.writer = writer
        self.loop: asyncio.AbstractEventLoop | None = None
        self.commands: asyncio.Queue[dict[str, Any]] | None = None
        self.runtime: SidecarRuntime | None = None
        self._tool_replies: dict[str, ToolReply] = {}
        self._tool_lock = threading.Lock()
        self._journal_lock = threading.Lock()
        self._action_condition = threading.Condition()
        self._actions: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._known_action_ids: set[str] = set()
        self._queued_messages: queue.Queue[tuple[str, str]] = queue.Queue(
            maxsize=QUEUED_MESSAGE_CAPACITY
        )
        self._tool_state_root: Path | None = None
        self.writer.set_failure_handler(self._on_writer_failure)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.commands = asyncio.Queue()

    def configure_tool_state(self, state_root: Path) -> None:
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._tool_state_root = state_root

    def _on_writer_failure(self, code: str) -> None:
        loop = self.loop
        if loop is None:
            return

        def fail_runtime() -> None:
            if self.runtime is not None:
                self.runtime.interrupt()
            if self.commands is not None:
                self.commands.put_nowait(
                    {"protocol": PROTOCOL_VERSION, "type": "writer.failed", "code": code}
                )

        try:
            loop.call_soon_threadsafe(fail_runtime)
        except Exception:
            pass

    def start_reader(self) -> threading.Thread:
        thread = threading.Thread(
            target=self._read_loop, name="qf-openhands-ipc-reader", daemon=True
        )
        thread.start()
        return thread

    def _read_loop(self) -> None:
        try:
            for raw_line in sys.stdin.buffer:
                if len(raw_line) > MAX_LINE_BYTES:
                    self.writer.send({"type": "fatal", "code": "IPC_FRAME_TOO_LARGE"})
                    break
                try:
                    message = json.loads(raw_line)
                    if not isinstance(message, dict) or message.get("protocol") != PROTOCOL_VERSION:
                        raise ValueError("protocol mismatch")
                except Exception:
                    self.writer.send({"type": "fatal", "code": "IPC_INVALID_JSON"})
                    break
                message_type = message.get("type")
                if message_type == "tool.result":
                    self._resolve_tool(message)
                    continue
                if message_type == "steer":
                    request_id = str(message.get("requestId", ""))
                    content = str(message.get("content", ""))
                    if not request_id or not content.strip():
                        self.writer.send(
                            {
                                "type": "response",
                                "requestId": request_id,
                                "ok": False,
                                "error": {
                                    "code": "OPENHANDS_STEER_INPUT_INVALID",
                                    "message": "steer content is empty",
                                },
                            }
                        )
                        continue
                    try:
                        self._queued_messages.put_nowait(("steer", content))
                    except queue.Full:
                        self.writer.send(
                            {
                                "type": "response",
                                "requestId": request_id,
                                "ok": False,
                                "error": {
                                    "code": "OPENHANDS_MESSAGE_QUEUE_FULL",
                                    "message": "after-interrupt message queue is full",
                                },
                            }
                        )
                        continue
                    if self.loop is not None and self.runtime is not None:
                        position = self._queued_messages.qsize()

                        def apply_steer_interrupt(
                            response_request_id: str = request_id,
                            response_position: int = position,
                        ) -> None:
                            try:
                                assert self.runtime is not None
                                self.runtime.interrupt()
                                self.writer.send(
                                    {
                                        "type": "response",
                                        "requestId": response_request_id,
                                        "ok": True,
                                        "result": {
                                            "semantics": "INTERRUPT_THEN_FOLLOW_UP",
                                            "position": response_position,
                                        },
                                    }
                                )
                            except Exception:
                                self.writer.send(
                                    {
                                        "type": "response",
                                        "requestId": response_request_id,
                                        "ok": False,
                                        "error": {
                                            "code": "OPENHANDS_INTERRUPT_FAILED",
                                            "message": "OpenHands interrupt was not acknowledged",
                                        },
                                    }
                                )

                        self.loop.call_soon_threadsafe(apply_steer_interrupt)
                    else:
                        self.writer.send(
                            {
                                "type": "response",
                                "requestId": request_id,
                                "ok": False,
                                "error": {
                                    "code": "OPENHANDS_NOT_INITIALIZED",
                                    "message": "OpenHands runtime is not initialized",
                                },
                            }
                        )
                    continue
                if message_type == "follow_up":
                    request_id = str(message.get("requestId", ""))
                    content = str(message.get("content", ""))
                    if (
                        not request_id
                        or not content.strip()
                        or len(content.encode("utf-8")) > 262_144
                    ):
                        self.writer.send(
                            {
                                "type": "response",
                                "requestId": request_id,
                                "ok": False,
                                "error": {
                                    "code": "OPENHANDS_QUEUE_INPUT_INVALID",
                                    "message": "queued message is empty or too large",
                                },
                            }
                        )
                        continue
                    try:
                        self._queued_messages.put_nowait((str(message_type), content))
                    except queue.Full:
                        self.writer.send(
                            {
                                "type": "response",
                                "requestId": request_id,
                                "ok": False,
                                "error": {
                                    "code": "OPENHANDS_MESSAGE_QUEUE_FULL",
                                    "message": "after-turn message queue is full",
                                },
                            }
                        )
                        continue
                    self.writer.send(
                        {
                            "type": "response",
                            "requestId": request_id,
                            "ok": True,
                            "result": {
                                "semantics": "QUEUED_AFTER_CURRENT_TURN",
                                "position": self._queued_messages.qsize(),
                            },
                        }
                    )
                    continue
                if message_type == "abort":
                    request_id = str(message.get("requestId", ""))
                    if self.loop is not None and self.runtime is not None:
                        def apply_abort_interrupt(
                            response_request_id: str = request_id,
                        ) -> None:
                            try:
                                assert self.runtime is not None
                                self.runtime.interrupt()
                                self.writer.send(
                                    {
                                        "type": "response",
                                        "requestId": response_request_id,
                                        "ok": True,
                                    }
                                )
                            except Exception:
                                self.writer.send(
                                    {
                                        "type": "response",
                                        "requestId": response_request_id,
                                        "ok": False,
                                        "error": {
                                            "code": "OPENHANDS_INTERRUPT_FAILED",
                                            "message": "OpenHands interrupt was not acknowledged",
                                        },
                                    }
                                )

                        self.loop.call_soon_threadsafe(apply_abort_interrupt)
                    else:
                        self.writer.send(
                            {
                                "type": "response",
                                "requestId": request_id,
                                "ok": False,
                                "error": {
                                    "code": "OPENHANDS_NOT_INITIALIZED",
                                    "message": "OpenHands runtime is not initialized",
                                },
                            }
                        )
                    continue
                if self.loop is None or self.commands is None:
                    self.writer.send({"type": "fatal", "code": "IPC_LOOP_NOT_READY"})
                    break
                self.loop.call_soon_threadsafe(self.commands.put_nowait, message)
        finally:
            if self.loop is not None and self.commands is not None:
                self.loop.call_soon_threadsafe(
                    self.commands.put_nowait,
                    {"protocol": PROTOCOL_VERSION, "type": "stdin.closed"},
                )

    def register_action(self, event: ActionEvent) -> dict[str, Any]:
        raw_arguments: Any = event.tool_call.arguments
        if isinstance(raw_arguments, str):
            raw_arguments = json.loads(raw_arguments)
        if not isinstance(raw_arguments, dict):
            raise RuntimeError("OpenHands tool call arguments are not a JSON object")
        raw_arguments = json.loads(_canonical(raw_arguments))
        for internal in ("kind", "summary", "security_risk"):
            raw_arguments.pop(internal, None)
        with self._action_condition:
            if event.tool_call_id in self._known_action_ids:
                return raw_arguments
            self._known_action_ids.add(event.tool_call_id)
            self._actions.setdefault(event.tool_name, []).append(
                (event.tool_call_id, raw_arguments)
            )
            self._action_condition.notify_all()
        return raw_arguments

    def rebuild_action_index(self, events: Sequence[Any]) -> int:
        terminal_ids = {
            event.tool_call_id for event in events if isinstance(event, ObservationBaseEvent)
        }
        restored = 0
        for event in events:
            if isinstance(event, ActionEvent) and event.tool_call_id not in terminal_ids:
                before = len(self._known_action_ids)
                self.register_action(event)
                if len(self._known_action_ids) > before:
                    restored += 1
        return restored

    def _take_tool_call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        with self._action_condition:
            for _ in range(40):
                candidates = self._actions.setdefault(tool_name, [])
                canonical = _canonical(arguments)
                terminal_match: int | None = None
                for index, (tool_call_id, candidate) in enumerate(candidates):
                    # A recovered Conversation can replay an ActionEvent whose
                    # durable host result is already terminal. Never let that
                    # stale candidate shadow a newer retry with identical
                    # arguments; terminal journal entries are idempotent data,
                    # not pending MCP work.
                    existing = None
                    if self._tool_state_root is not None:
                        existing = self._load_journal(tool_call_id)
                    if _canonical(candidate) == canonical:
                        if existing is not None and existing.get("status") == "COMPLETED":
                            # Keep a terminal candidate as a fallback for a
                            # recovery with no newer retry, but prefer any
                            # non-terminal identical candidate below it.
                            terminal_match = index
                            continue
                        del candidates[index]
                        return tool_call_id
                if terminal_match is not None:
                    tool_call_id, _ = candidates.pop(terminal_match)
                    return tool_call_id
                self._action_condition.wait(timeout=0.05)
        raise RuntimeError("OpenHands MCP action was not correlated to tool execution")

    def _journal_path(self, tool_call_id: str) -> Path:
        if self._tool_state_root is None:
            raise RuntimeError("QF tool journal is not configured")
        return self._tool_state_root / f"{hashlib.sha256(tool_call_id.encode()).hexdigest()}.json"

    def _load_journal(self, tool_call_id: str) -> dict[str, Any] | None:
        path = self._journal_path(tool_call_id)
        if not path.exists():
            return None
        record = _read_json_record(path)
        entry = record.get("entry")
        if (
            record.get("schemaVersion") != TOOL_JOURNAL_SCHEMA_VERSION
            or not isinstance(entry, dict)
            or record.get("entrySha256") != _sha256(entry)
            or entry.get("toolCallId") != tool_call_id
        ):
            raise RuntimeError("QF tool journal integrity check failed")
        return entry

    def _write_journal(self, entry: dict[str, Any]) -> None:
        tool_call_id = str(entry["toolCallId"])
        _atomic_write_json(
            self._journal_path(tool_call_id),
            {
                "schemaVersion": TOOL_JOURNAL_SCHEMA_VERSION,
                "entry": entry,
                "entrySha256": _sha256(entry),
            },
        )

    def _prepare_tool_call(
        self,
        tool_call_id: str,
        tool_name: str,
        request_sha256: str,
    ) -> dict[str, Any] | None:
        with self._journal_lock:
            existing = self._load_journal(tool_call_id)
            if existing is None:
                self._write_journal(
                    {
                        "status": "PENDING",
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "requestSha256": request_sha256,
                    }
                )
                return None
            if (
                existing.get("toolName") != tool_name
                or existing.get("requestSha256") != request_sha256
            ):
                raise RuntimeError("QF tool journal request identity mismatch")
            status = existing.get("status")
            if status == "PENDING":
                return None
            if status == "COMPLETED" and isinstance(existing.get("message"), dict):
                return dict(existing["message"])
            raise RuntimeError("QF tool journal status is invalid")

    def _persist_tool_result(
        self,
        tool_call_id: str,
        request_sha256: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        persisted_message = {
            "ok": message.get("ok") is True,
            "result": _safe_json(message.get("result")),
            "error": _safe_json(message.get("error")),
        }
        with self._journal_lock:
            existing = self._load_journal(tool_call_id)
            if (
                existing is None
                or existing.get("status") not in {"PENDING", "COMPLETED"}
                or existing.get("requestSha256") != request_sha256
            ):
                raise RuntimeError("QF tool result does not match a pending request")
            if existing.get("status") == "COMPLETED":
                if existing.get("message") != persisted_message:
                    raise RuntimeError("QF tool result changed for an existing tool-call id")
                return persisted_message
            self._write_journal(
                {
                    "status": "COMPLETED",
                    "toolCallId": tool_call_id,
                    "toolName": existing.get("toolName"),
                    "requestSha256": request_sha256,
                    "message": persisted_message,
                }
            )
        return persisted_message

    def execute_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a QF MCP call through the durable Node Tool Host journal."""

        runtime = self.runtime
        if runtime is None or tool_name not in runtime.active_tool_names:
            raise RuntimeError(f"QF tool {tool_name} is not enabled for this prompt")
        try:
            runtime.validate_tool_arguments(tool_name, arguments)
        except jsonschema_exceptions.ValidationError as error:
            schema_path = "/".join(str(item) for item in error.absolute_schema_path)
            raise RuntimeError(
                "QF tool arguments failed the versioned JSON Schema"
                + (f" at /{schema_path}" if schema_path else "")
            ) from error
        tool_call_id = self._take_tool_call(tool_name, arguments)
        request_sha256 = _sha256({"toolName": tool_name, "arguments": arguments})
        cached = self._prepare_tool_call(tool_call_id, tool_name, request_sha256)
        if cached is not None:
            if cached.get("ok") is not True:
                raise RuntimeError(f"QF tool {tool_name} failed")
            return cached.get("result")
        reply = ToolReply(request_sha256)
        with self._tool_lock:
            self._tool_replies[tool_call_id] = reply
        try:
            self.writer.send(
                {
                    "type": "tool.call",
                    "requestId": tool_call_id,
                    "runtimeSessionId": runtime.runtime_session_id,
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "requestSha256": request_sha256,
                    "transport": MCP_GATEWAY_VERSION,
                }
            )
            for _ in range(MAX_TOOL_WAIT_SECONDS * 10):
                if reply.event.wait(timeout=0.1):
                    break
                if self.writer.failure_code is not None:
                    raise RuntimeError("QF IPC failed while an MCP tool was pending")
            else:
                raise TimeoutError(f"QF tool {tool_name} timed out")
            assert reply.message is not None
            if reply.message.get("ok") is not True:
                raise RuntimeError(f"QF tool {tool_name} failed")
            return reply.message.get("result")
        finally:
            with self._tool_lock:
                self._tool_replies.pop(tool_call_id, None)

    def _resolve_tool(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("requestId", ""))
        with self._tool_lock:
            reply = self._tool_replies.get(request_id)
        if reply is None:
            try:
                existing = self._load_journal(request_id)
                if existing is None or existing.get("status") != "COMPLETED":
                    raise RuntimeError("unsolicited QF tool result")
                expected = existing.get("message")
                actual = {
                    "ok": message.get("ok") is True,
                    "result": _safe_json(message.get("result")),
                    "error": _safe_json(message.get("error")),
                }
                if expected != actual:
                    raise RuntimeError("conflicting duplicate QF tool result")
                return
            except Exception:
                self.writer.fail_closed("IPC_UNEXPECTED_TOOL_RESULT")
                return
        try:
            persisted = self._persist_tool_result(
                request_id,
                reply.request_sha256,
                message,
            )
        except Exception:
            self.writer.fail_closed("IPC_TOOL_RESULT_PERSISTENCE_FAILED")
            return
        reply.message = persisted
        reply.event.set()

    def drain_queued_messages(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        while True:
            try:
                items.append(self._queued_messages.get_nowait())
            except queue.Empty:
                return items

    @property
    def queued_message_count(self) -> int:
        return self._queued_messages.qsize()
class SidecarRuntime:
    def __init__(self, bridge: Bridge, writer: ProtocolWriter) -> None:
        self.bridge = bridge
        self.writer = writer
        self.conversation: Any | None = None
        self.mcp_gateway: McpToolGateway | None = None
        self.sandbox: HardenedDockerAgentServer | None = None
        self.server_handle: DockerServerHandle | None = None
        self.security_analyzer: QfSecurityAnalyzer | None = None
        self.workspace_snapshot: dict[str, Any] | None = None
        self.runtime_session_id = ""
        self.provider = ""
        self.model_id = ""
        self.config_hash = ""
        self.runtime_revision = ""
        self.recovery_cursor = 0
        self.recovery_mode = "CREATE_OR_REUSE"
        self.provider_prompt_requests_this_process = 0
        self.usage_id = ""
        self.all_tool_names: frozenset[str] = frozenset()
        self.active_tool_names: frozenset[str] = frozenset()
        self._tool_validators: dict[str, Any] = {}
        self.manifest_hash = ""
        self.restored_pending_tool_calls = 0
        self.workspace_secret_environment_names: list[str] = []
        self._token_sequence = 0
        self._run_request_id = ""

    def _manifest(
        self, message: dict[str, Any], tool_specs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "sidecarVersion": SIDECAR_VERSION,
            "sidecarSourceSha256": SIDECAR_SOURCE_SHA256,
            "sdkVersion": importlib.metadata.version("openhands-sdk"),
            "runtimeSessionId": self.runtime_session_id,
            "provider": self.provider,
            "modelId": self.model_id,
            "baseUrl": str(message["baseUrl"]).rstrip("/"),
            "systemPromptSha256": str(message["systemPromptHash"]),
            "toolSpecs": tool_specs,
            "toolNames": sorted(self.all_tool_names),
            "agentContext": {
                "currentDateTime": str(message["currentDateTime"]),
                "defaultTools": [TerminalTool.name, FileEditorTool.name],
                "skills": [],
                "mcp": [{"name": "qf-control-plane", "version": MCP_GATEWAY_VERSION}],
                "plugins": [],
            },
            "llm": {
                "transportModel": f"openai/{self.model_id}",
                "stream": True,
                "numRetries": 0,
                "timeoutSeconds": 300,
                "maxOutputTokens": int(message.get("maxOutputTokens", 8192)),
                "nativeToolCalling": True,
                "logCompletions": False,
                "fallbackStrategy": None,
                "reasoningEffort": "none",
            },
            "maxIterations": int(message.get("maxIterations", 100)),
            "credentialMode": "stdin-secret-unpersisted",
            "workspace": {
                "backend": "hardened-docker-agent-server",
                "image": os.environ.get(
                    "QF_OPENHANDS_AGENT_SERVER_IMAGE",
                    "qfintelligence/openhands-agent-server:1.39.0-qf.1",
                ),
                "snapshot": "git-visible-working-tree",
                "hostLocalWorkspace": False,
            },
            "steerSemantics": "INTERRUPT_THEN_FOLLOW_UP",
            "followUpSemantics": "QUEUED_AFTER_CURRENT_TURN",
        }

    def _persist_or_validate_manifest(self, manifest: dict[str, Any]) -> str:
        manifest_hash = _sha256(manifest)
        path = STATE_ROOT / "runtime-manifest.json"
        persistence = STATE_ROOT / "persistence" / self.runtime_session_id.replace("-", "")
        tool_journal = STATE_ROOT / "tool-journal"
        if path.exists():
            record = _read_json_record(path)
            stored_manifest = record.get("manifest")
            if (
                record.get("schemaVersion") != MANIFEST_SCHEMA_VERSION
                or not isinstance(stored_manifest, dict)
                or record.get("manifestSha256") != _sha256(stored_manifest)
                or record.get("manifestSha256") != manifest_hash
                or stored_manifest != manifest
            ):
                raise RuntimeError("OpenHands runtime manifest mismatch during recovery")
            return manifest_hash
        if (persistence.exists() and any(persistence.iterdir())) or (
            tool_journal.exists() and any(tool_journal.iterdir())
        ):
            raise RuntimeError("OpenHands persisted state has no runtime manifest")
        _atomic_write_json(
            path,
            {
                "schemaVersion": MANIFEST_SCHEMA_VERSION,
                "manifest": manifest,
                "manifestSha256": manifest_hash,
            },
        )
        return manifest_hash

    def validate_tool_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None:
        validator = self._tool_validators.get(tool_name)
        if validator is None:
            raise RuntimeError("QF tool validator is missing")
        validator.validate(arguments)

    def _event(
        self,
        event_type: str,
        event_id: str,
        payload: dict[str, Any],
        *,
        callback: bool = False,
    ) -> None:
        message = {
            "type": "event",
            "requestId": self._run_request_id,
            "eventId": event_id,
            "eventType": event_type,
            "payload": payload,
        }
        if callback:
            self.writer.send_callback(message)
        else:
            self.writer.send(message)

    def _on_event(self, event: Any) -> None:
        try:
            event_id = str(getattr(event, "id", "unknown"))
            if isinstance(event, ActionEvent):
                security_risk = (
                    self.security_analyzer.security_risk(event)
                    if self.security_analyzer is not None
                    else event.security_risk
                )
                action = (
                    self.bridge.register_action(event)
                    if event.tool_name in self.all_tool_names
                    else (
                        {}
                        if event.action is None
                        else event.action.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )
                    )
                )
                self._event(
                    "tool.started",
                    event_id,
                    {
                        "toolCallId": event.tool_call_id,
                        "toolName": event.tool_name,
                        "arguments": action,
                        "securityRisk": security_risk.value,
                        "backendEventId": event_id,
                    },
                    callback=True,
                )
            elif isinstance(event, ObservationEvent):
                text = "".join(
                    item.text for item in event.observation.content if isinstance(item, TextContent)
                )
                self._event(
                    "tool.completed",
                    event_id,
                    {
                        "toolCallId": event.tool_call_id,
                        "toolName": event.tool_name,
                        "result": text,
                        "isError": event.observation.is_error,
                        "backendEventId": event_id,
                    },
                    callback=True,
                )
            elif isinstance(event, MessageEvent) and event.source == "agent":
                text = "".join(
                    item.text
                    for item in event.llm_message.content
                    if isinstance(item, TextContent)
                )
                if text:
                    self._event(
                        "assistant.delta",
                        event_id,
                        {
                            "text": text,
                            "backendEventId": event_id,
                            "streamMode": "persisted-message",
                        },
                        callback=True,
                    )
            elif isinstance(event, AgentErrorEvent):
                self._event(
                    "tool.completed",
                    event_id,
                    {
                        "toolCallId": event.tool_call_id,
                        "toolName": event.tool_name,
                        "result": event.error,
                        "isError": True,
                        "backendEventId": event_id,
                    },
                    callback=True,
                )
            elif isinstance(event, InterruptEvent):
                self._event(
                    "agent.aborted",
                    event_id,
                    {"backendEventId": event_id, "summary": "OpenHands conversation interrupted"},
                    callback=True,
                )
            elif isinstance(event, ConversationErrorEvent):
                self._event(
                    "run.failed",
                    event_id,
                    {
                        "backendEventId": event_id,
                        "code": event.code,
                        "message": event.detail,
                        "retryable": True,
                    },
                    callback=True,
                )
        except Exception:
            self.writer.fail_closed("SDK_EVENT_CALLBACK_FAILED")

    def _on_token(self, chunk: Any) -> None:
        try:
            choices = chunk.choices
            text = choices[0].delta.content or "" if choices else ""
            if not text:
                return
            self._token_sequence += 1
            self._event(
                "assistant.delta",
                f"token:{self._run_request_id}:{self._token_sequence}",
                {"text": text},
                callback=True,
            )
        except Exception:
            self.writer.fail_closed("SDK_TOKEN_CALLBACK_FAILED")

    def initialize(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.conversation is not None:
            raise RuntimeError("sidecar is already initialized")
        sdk_version = importlib.metadata.version("openhands-sdk")
        if sdk_version != EXPECTED_SDK_VERSION:
            raise RuntimeError("unexpected OpenHands SDK version")
        self.runtime_session_id = str(message["runtimeSessionId"])
        UUID(self.runtime_session_id)
        self.provider = str(message["provider"])
        self.model_id = str(message["modelId"])
        self.config_hash = str(message["configHash"])
        self.runtime_revision = str(message["runtimeRevision"])
        self.recovery_cursor = int(message["recoveryCursor"])
        self.recovery_mode = str(message.get("recoveryMode", "CREATE_OR_REUSE"))
        if (
            len(self.config_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.config_hash)
            or not self.runtime_revision
            or self.recovery_cursor < 0
            or self.recovery_mode not in {"CREATE_OR_REUSE", "RECOVER_EXACT"}
        ):
            raise RuntimeError("runtime recovery identity is invalid")
        if self.provider not in {"openai", "deepseek"}:
            raise RuntimeError("unsupported QF provider")
        base_url = str(message["baseUrl"]).rstrip("/")
        parsed_base_url = urlsplit(base_url)
        api_key = SecretStr(str(message["apiKey"]))
        if (
            parsed_base_url.scheme != "https"
            or not parsed_base_url.hostname
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.query
            or parsed_base_url.fragment
            or not api_key.get_secret_value()
        ):
            raise RuntimeError("provider configuration is invalid")
        system_prompt = str(message["systemPrompt"])
        if (
            not system_prompt.strip()
            or len(system_prompt.encode("utf-8")) > 1_048_576
            or hashlib.sha256(system_prompt.encode()).hexdigest() != message["systemPromptHash"]
        ):
            raise RuntimeError("system prompt hash mismatch")
        raw_tool_specs = message.get("toolSpecs")
        asserted_tool_names = message.get("toolNames")
        if (
            not isinstance(raw_tool_specs, list)
            or not raw_tool_specs
            or not isinstance(asserted_tool_names, list)
        ):
            raise RuntimeError("QF tool specs are missing")
        try:
            tool_specs = json.loads(_canonical(raw_tool_specs))
        except (TypeError, ValueError) as error:
            raise RuntimeError("QF tool specs are not JSON serializable") from error
        if len(_canonical(tool_specs).encode("utf-8")) > MAX_LINE_BYTES:
            raise RuntimeError("QF tool specs exceed the protocol limit")
        tool_names: list[str] = []
        validators: dict[str, Any] = {}
        for spec in tool_specs:
            if not isinstance(spec, dict):
                raise RuntimeError("QF tool spec is not an object")
            name = str(spec.get("name", ""))
            description = spec.get("description")
            parameters = spec.get("parameters")
            if (
                not name
                or len(name) > 128
                or any(not (character.isalnum() or character in {"_", "-"}) for character in name)
                or not isinstance(description, str)
                or not description.strip()
                or len(description.encode("utf-8")) > 16_384
                or not isinstance(parameters, dict)
                or parameters.get("type") != "object"
            ):
                raise RuntimeError("QF tool spec shape is invalid")
            properties = parameters.get("properties", {})
            if isinstance(properties, dict) and {
                "kind",
                "summary",
                "security_risk",
            }.intersection(properties):
                raise RuntimeError("QF tool schema uses an OpenHands-reserved property")
            _assert_local_json_references(parameters)
            validator_class = jsonschema_validators.validator_for(parameters)
            try:
                validator_class.check_schema(parameters)
            except jsonschema_exceptions.SchemaError as error:
                raise RuntimeError("QF tool JSON Schema is invalid") from error
            validators[name] = validator_class(
                parameters,
                format_checker=FormatChecker(),
            )
            tool_names.append(name)
        if (
            len(tool_names) != len(set(tool_names))
            or set(tool_names) != {str(name) for name in asserted_tool_names}
            or len(asserted_tool_names) != len(tool_names)
        ):
            raise RuntimeError("QF tool allowlist assertion failed during initialization")
        self.all_tool_names = frozenset(tool_names)
        self._tool_validators = validators
        manifest = self._manifest(message, tool_specs)
        self.manifest_hash = self._persist_or_validate_manifest(manifest)
        asserted_manifest_hash = message.get("manifestHash")
        if asserted_manifest_hash is not None and asserted_manifest_hash != self.manifest_hash:
            raise RuntimeError("QF asserted runtime manifest hash mismatch")
        self.bridge.configure_tool_state(STATE_ROOT / "tool-journal")
        self.bridge.runtime = self
        self.mcp_gateway = McpToolGateway(
            state_root=STATE_ROOT,
            tool_specs=tool_specs,
            handler=self.bridge.execute_mcp_tool,
        )
        self.mcp_gateway.start()
        self.workspace_snapshot = create_repository_snapshot(
            PROJECT_SNAPSHOT_SOURCE,
            WORKSPACE_ROOT,
        )
        self.sandbox = HardenedDockerAgentServer(
            STATE_ROOT,
            WORKSPACE_ROOT,
            self.mcp_gateway.socket_path,
        )
        self.server_handle, server_reused = self.sandbox.start_or_recover(base_url)
        self.usage_id = f"qf:{self.runtime_session_id}"
        llm = LLM(
            model=f"openai/{self.model_id}",
            model_canonical_name=self.model_id,
            base_url=base_url,
            api_key=api_key,
            usage_id=self.usage_id,
            stream=True,
            num_retries=0,
            timeout=300,
            max_output_tokens=int(message.get("maxOutputTokens", 8192)),
            native_tool_calling=True,
            log_completions=False,
            fallback_strategy=None,
            reasoning_effort="none",
        )
        agent = Agent(
            llm=llm,
            tools=[
                Tool(
                    name=TerminalTool.name,
                    params={
                        "terminal_type": "subprocess",
                        "no_change_timeout_seconds": 30,
                        "env": {
                            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                            "QF_RUN_WORKSPACE": "/workspace",
                        },
                    },
                ),
                Tool(name=FileEditorTool.name),
            ],
            include_default_tools=[],
            mcp_config={
                "qf-control-plane": MCPServer(
                    url=f"{self.server_handle.mcp_url}{self.mcp_gateway.capability_path}",
                    transport="streamable-http",
                    headers={"Host": SecretStr("localhost:8001")},
                    description=(
                        "QF finance, quantum, approval, evidence, artifact, "
                        "Query ID, and hardware Tool Gateway"
                    ),
                    timeout=920,
                    sse_read_timeout=920,
                )
            },
            agent_context=AgentContext(
                skills=[],
                load_user_skills=False,
                load_public_skills=False,
                load_project_skills=False,
                registered_marketplaces=[],
                current_datetime=str(message["currentDateTime"]),
            ),
            system_prompt=system_prompt,
            tool_concurrency_limit=1,
        )
        remote_workspace = RemoteWorkspace(
            working_dir="/workspace",
            host=self.server_handle.host,
            api_key=self.server_handle.api_key,
            read_timeout=920,
        )
        secret_probe = remote_workspace.execute_command(
            "python -c 'import os; "
            'print(\",\".join(sorted(name for name in os.environ '
            'if name in {\"SESSION_API_KEY\",\"OH_SESSION_API_KEYS_0\",'
            '\"DEEPSEEK_API_KEY\",\"OPENAI_API_KEY\"})))\'',
            timeout=20,
        )
        if (
            secret_probe.exit_code != 0
            or secret_probe.timeout_occurred
            or secret_probe.stderr.strip()
        ):
            raise RuntimeError("Remote Workspace secret-isolation probe failed")
        self.workspace_secret_environment_names = [
            name for name in secret_probe.stdout.strip().split(",") if name
        ]
        if self.workspace_secret_environment_names:
            raise RuntimeError("Remote Workspace inherited a model-control secret")
        self.conversation = Conversation(
            agent=agent,
            workspace=remote_workspace,
            plugins=[],
            conversation_id=UUID(self.runtime_session_id),
            callbacks=[self._on_event],
            token_callbacks=[self._on_token],
            visualizer=None,
            stuck_detection=False,
            delete_on_close=False,
            secrets={},
            max_iteration_per_run=int(message.get("maxIterations", 100)),
        )
        self.security_analyzer = QfSecurityAnalyzer(
            allow_network=False,
            allowed_paths=("",),
            allowed_commands=(
                "npm run typecheck",
                "npm test",
                "npm run build",
                "uv run pytest",
                "uv run ruff check",
            ),
        )
        self.conversation.set_security_analyzer(self.security_analyzer)
        self.conversation.set_confirmation_policy(
            ConfirmRisky(threshold=SecurityRisk.MEDIUM, confirm_unknown=True)
        )
        self.restored_pending_tool_calls = self.bridge.rebuild_action_index(
            list(self.conversation.state.events)
        )
        if (
            self.recovery_mode == "RECOVER_EXACT"
            and self.conversation.state.execution_status
            == ConversationExecutionStatus.RUNNING
        ):
            self.conversation.pause()
        return {
            "runtimeSessionId": self.runtime_session_id,
            "runtimeRevision": self.runtime_revision,
            "configHash": self.config_hash,
            "recoveryCursor": self.recovery_cursor,
            "executionStatus": self.conversation.state.execution_status.value,
            "sdkEventCount": len(self.conversation.state.events),
            "providerPromptRequestsThisProcess": self.provider_prompt_requests_this_process,
            "sdkVersion": sdk_version,
            "sidecarVersion": SIDECAR_VERSION,
            "sidecarSourceSha256": SIDECAR_SOURCE_SHA256,
            "manifestHash": self.manifest_hash,
            "toolNames": sorted(self.all_tool_names),
            "defaultTools": [TerminalTool.name, FileEditorTool.name],
            "skills": [],
            "mcp": [
                {
                    "name": "qf-control-plane",
                    "version": MCP_GATEWAY_VERSION,
                    "toolCount": len(self.all_tool_names),
                }
            ],
            "workspace": {
                "backend": "hardened-docker-agent-server",
                "hostLocalWorkspace": False,
                "workspaceId": self.runtime_session_id,
                "snapshot": self.workspace_snapshot,
                "imageReference": self.server_handle.image_reference,
                "imageDigest": self.server_handle.image_digest,
                "containerId": self.server_handle.container_id,
                "serverReused": server_reused,
                "secretEnvironmentNames": self.workspace_secret_environment_names,
            },
            "steerSemantics": "INTERRUPT_THEN_FOLLOW_UP",
            "followUpSemantics": "QUEUED_AFTER_CURRENT_TURN",
            "restoredPendingToolCalls": self.restored_pending_tool_calls,
            "persistenceRelativePath": "agent-server",
        }

    def interrupt(self) -> None:
        if self.conversation is not None:
            self.conversation.interrupt()

    def isolation_snapshot(self) -> dict[str, Any]:
        def limit(kind: int) -> dict[str, int]:
            soft, hard = resource.getrlimit(kind)
            return {"soft": int(soft), "hard": int(hard)}

        sensitive_environment = [
            name
            for name in os.environ
            if "API_KEY" in name.upper()
            or name.upper().endswith(("_TOKEN", "_SECRET", "_PASSWORD"))
        ]
        return {
            "hostname": os.uname().nodename,
            "cwd": str(Path.cwd()),
            "rlimits": {
                "core": limit(resource.RLIMIT_CORE),
                "nofile": limit(resource.RLIMIT_NOFILE),
                "nproc": limit(resource.RLIMIT_NPROC),
                "addressSpace": limit(resource.RLIMIT_AS),
            },
            "windowsProjectHidden": not Path("/mnt/d/q-fintelligence").exists(),
            "wslProjectHidden": not Path(os.environ.get("QF_PROJECT_ROOT", ".")).exists(),
            "workspaceAvailable": WORKSPACE_ROOT.is_dir(),
            "workspaceBackend": "hardened-docker-agent-server",
            "hostLocalWorkspace": False,
            "agentServerContainerId": (
                None if self.server_handle is None else self.server_handle.container_id
            ),
            "agentServerImageDigest": (
                None if self.server_handle is None else self.server_handle.image_digest
            ),
            "providerSecretEnvironmentNames": sensitive_environment,
        }

    def _usage_snapshot(self) -> dict[str, int | None]:
        assert self.conversation is not None
        metrics = self.conversation.state.stats.usage_to_metrics.get(self.usage_id)
        usage = None if metrics is None else metrics.get_snapshot().accumulated_token_usage
        if usage is None:
            return {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "reasoning": None,
                "totalTokens": 0,
            }
        return {
            "input": usage.prompt_tokens,
            "output": usage.completion_tokens,
            "cacheRead": usage.cache_read_tokens,
            "cacheWrite": usage.cache_write_tokens,
            "reasoning": usage.reasoning_tokens or None,
            "totalTokens": usage.prompt_tokens + usage.completion_tokens,
        }

    def _assistant_message(self, starting_event_count: int) -> tuple[str, str | None]:
        assert self.conversation is not None
        messages = [
            event
            for event in list(self.conversation.state.events)[starting_event_count:]
            if isinstance(event, MessageEvent) and event.source == "agent"
        ]
        if not messages:
            return "", None
        last = messages[-1]
        text = "".join(
            item.text for item in last.llm_message.content if isinstance(item, TextContent)
        )
        return text, str(last.id)

    async def prompt(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.conversation is None:
            raise RuntimeError("sidecar is not initialized")
        if self.writer.failure_code is not None:
            raise RuntimeError("sidecar protocol output is fail-closed")
        requested_tools = message.get("toolNames")
        if not isinstance(requested_tools, list):
            raise RuntimeError("prompt tool list is invalid")
        selected = frozenset(str(name) for name in requested_tools)
        if not selected.issubset(self.all_tool_names):
            raise RuntimeError("prompt requested a tool outside the QF allowlist")
        self.active_tool_names = selected
        self._run_request_id = str(message["requestId"])
        content = str(message["content"])
        if (
            not self._run_request_id
            or not content.strip()
            or len(content.encode("utf-8")) > MAX_LINE_BYTES
        ):
            raise RuntimeError("prompt request is empty or too large")
        self._token_sequence = 0
        self.provider_prompt_requests_this_process += 1
        start_count = len(self.conversation.state.events)
        usage_before = self._usage_snapshot()
        self._event(
            "agent.started",
            f"run:{self._run_request_id}:agent.started",
            {"runtimeSessionId": self.runtime_session_id, "toolNames": sorted(selected)},
        )
        self._event(
            "turn.started",
            f"run:{self._run_request_id}:turn.started",
            {"runtimeSessionId": self.runtime_session_id},
        )
        self.conversation.send_message(content, sender="qf-runtime")
        # RemoteConversation inherits the SDK base arun(), whose implementation
        # invokes the synchronous polling run() directly. Run it in a worker
        # thread so the sidecar event loop remains able to deliver abort/steer
        # requests to the Agent Server while a turn is active.
        await asyncio.to_thread(self.conversation.run)
        while True:
            queued = self.bridge.drain_queued_messages()
            if not queued:
                break
            for queue_type, content in queued:
                self._event(
                    "queue.changed",
                    f"run:{self._run_request_id}:queue:{_sha256([queue_type, content])[:16]}",
                    {
                        "mode": queue_type,
                        "semantics": "QUEUED_AFTER_CURRENT_TURN",
                        "queued": self.bridge.queued_message_count,
                    },
                )
                self.conversation.send_message(content, sender=f"qf-{queue_type}")
            await asyncio.to_thread(self.conversation.run)
        status = self.conversation.state.execution_status
        if self.writer.failure_code is not None:
            raise RuntimeError("sidecar protocol output failed closed")
        if _execution_status_requires_resolution(status):
            turn_events = list(self.conversation.state.events)[start_count:]
            if any(isinstance(event, InterruptEvent) for event in turn_events):
                raise asyncio.CancelledError
            pending_action = next(
                (
                    event
                    for event in reversed(turn_events)
                    if isinstance(event, ActionEvent)
                    and (
                        self.security_analyzer is None
                        or self.security_analyzer.security_risk(event)
                        in {
                            SecurityRisk.MEDIUM,
                            SecurityRisk.HIGH,
                            SecurityRisk.UNKNOWN,
                        }
                    )
                ),
                None,
            )
            if pending_action is not None:
                self.conversation.reject_pending_actions(
                    "QF requires a revised execution scope; this action was not authorized"
                )
                self._event(
                    "security.quarantined",
                    f"{pending_action.id}:scope",
                    {
                        "backendEventId": str(pending_action.id),
                        "toolCallId": pending_action.tool_call_id,
                        "toolName": pending_action.tool_name,
                        "securityRisk": (
                            SecurityRisk.HIGH.value
                            if self.security_analyzer is None
                            else self.security_analyzer.security_risk(
                                pending_action
                            ).value
                        ),
                        "summary": (
                            "OpenHands action exceeded the approved Remote Workspace scope"
                        ),
                        "scopeChangeRequired": True,
                    },
                    callback=True,
                )
                raise WorkspaceActionScopeRequiredError(
                    "OpenHands action exceeded the approved Remote Workspace scope"
                )
            raise asyncio.CancelledError
        if status != ConversationExecutionStatus.FINISHED:
            raise RuntimeError(f"OpenHands conversation ended with status {status.value}")
        assistant_content, message_id = self._assistant_message(start_count)
        if not assistant_content.strip():
            raise RuntimeError("OpenHands provider completed without assistant text")
        usage_after = self._usage_snapshot()
        usage = {
            key: (
                None
                if usage_after[key] is None
                else int(usage_after[key] or 0) - int(usage_before[key] or 0)
            )
            for key in usage_after
        }
        self._event(
            "turn.completed",
            f"run:{self._run_request_id}:turn.completed",
            {"runtimeSessionId": self.runtime_session_id},
        )
        self._event(
            "agent.completed",
            f"run:{self._run_request_id}:agent.completed",
            {"runtimeSessionId": self.runtime_session_id},
        )
        return {
            "assistantContent": assistant_content,
            "provider": self.provider,
            "modelId": self.model_id,
            "backendMessageId": message_id,
            "usage": usage,
            "runtimeSessionId": self.runtime_session_id,
        }

    def close(self) -> None:
        cleanup_errors = 0
        if self.conversation is not None:
            try:
                self.conversation.close()
            except Exception:
                cleanup_errors += 1
            finally:
                self.conversation = None
        if self.sandbox is not None:
            try:
                self.sandbox.stop()
            except Exception:
                cleanup_errors += 1
            finally:
                self.sandbox = None
                self.server_handle = None
        if self.mcp_gateway is not None:
            try:
                self.mcp_gateway.close()
            except Exception:
                cleanup_errors += 1
            finally:
                self.mcp_gateway = None
        if cleanup_errors:
            sys.stderr.write(f"OpenHands cleanup failures: {cleanup_errors}\n")
            sys.stderr.flush()


async def _serve() -> int:
    os.umask(0o077)
    writer = ProtocolWriter()
    bridge = Bridge(writer)
    runtime = SidecarRuntime(bridge, writer)
    loop = asyncio.get_running_loop()
    bridge.bind_loop(loop)
    bridge.start_reader()
    writer.send(
        {
            "type": "ready",
            "sidecarVersion": SIDECAR_VERSION,
            "sidecarSourceSha256": SIDECAR_SOURCE_SHA256,
            "sdkVersion": importlib.metadata.version("openhands-sdk"),
            "pid": os.getpid(),
        }
    )
    assert bridge.commands is not None
    try:
        while True:
            message = await bridge.commands.get()
            message_type = message.get("type")
            request_id = str(message.get("requestId", ""))
            if message_type == "stdin.closed":
                return 0
            if message_type == "writer.failed":
                return 74
            if message_type == "shutdown":
                writer.send({"type": "response", "requestId": request_id, "ok": True})
                return 0
            try:
                if message_type == "init":
                    result = runtime.initialize(message)
                elif message_type == "prompt":
                    result = await runtime.prompt(message)
                elif message_type == "health":
                    result = {
                        "runtimeSessionId": runtime.runtime_session_id,
                        "runtimeRevision": runtime.runtime_revision,
                        "configHash": runtime.config_hash,
                        "recoveryCursor": runtime.recovery_cursor,
                        "executionStatus": (
                            runtime.conversation.state.execution_status.value
                            if runtime.conversation is not None
                            else "UNINITIALIZED"
                        ),
                        "sdkEventCount": (
                            len(runtime.conversation.state.events)
                            if runtime.conversation is not None
                            else 0
                        ),
                        "providerPromptRequestsThisProcess": (
                            runtime.provider_prompt_requests_this_process
                        ),
                        "initialized": runtime.conversation is not None,
                        "toolNames": sorted(runtime.all_tool_names),
                        "manifestHash": runtime.manifest_hash,
                        "steerSemantics": "INTERRUPT_THEN_FOLLOW_UP",
                        "followUpSemantics": "QUEUED_AFTER_CURRENT_TURN",
                        "isolation": runtime.isolation_snapshot(),
                    }
                else:
                    raise RuntimeError("unsupported sidecar command")
                writer.send(
                    {"type": "response", "requestId": request_id, "ok": True, "result": result}
                )
            except asyncio.CancelledError:
                writer.send(
                    {
                        "type": "response",
                        "requestId": request_id,
                        "ok": False,
                        "error": {"code": "OPENHANDS_ABORTED", "message": "run aborted"},
                    }
                )
            except WorkspaceActionScopeRequiredError as error:
                writer.send(
                    {
                        "type": "response",
                        "requestId": request_id,
                        "ok": False,
                        "error": {
                            "code": "OPENHANDS_ACTION_SCOPE_REQUIRED",
                            "message": str(error),
                        },
                    }
                )
            except Exception as error:
                if message_type == "init":
                    runtime.close()
                writer.send(
                    {
                        "type": "response",
                        "requestId": request_id,
                        "ok": False,
                        "error": {
                            "code": "OPENHANDS_SIDECAR_FAILED",
                            "message": str(error)[:2000],
                            "traceSha256": hashlib.sha256(
                                traceback.format_exc().encode("utf-8")
                            ).hexdigest(),
                        },
                    }
                )
    finally:
        runtime.close()
        writer.close()


def main() -> int:
    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
