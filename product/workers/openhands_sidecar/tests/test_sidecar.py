from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest import mock

from jsonschema import FormatChecker
from jsonschema import validators as jsonschema_validators
from openhands.sdk.event import ActionEvent, AgentErrorEvent, MessageEvent
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.tool import Action, Observation
from openhands.sdk.tool.client_tool import ClientToolObservation

import main as sidecar
from qf_openhands.sandbox import AGENT_SERVER_PORTS


class CapturingWriter:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.failure_code: str | None = None
        self.failure_handler: Any = None
        self.on_send: Any = None

    def set_failure_handler(self, handler: Any) -> None:
        self.failure_handler = handler

    def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if self.on_send is not None:
            self.on_send(message)

    def send_callback(self, message: dict[str, Any]) -> bool:
        self.send(message)
        return True

    def fail_closed(self, code: str) -> None:
        self.failure_code = code
        if self.failure_handler is not None:
            self.failure_handler(code)


class FakeRuntime:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.active_tool_names = frozenset({"qf_test"})
        self.runtime_session_id = "11111111-1111-4111-8111-111111111111"
        validator_class = jsonschema_validators.validator_for(schema)
        validator_class.check_schema(schema)
        self.validator = validator_class(schema, format_checker=FormatChecker())

    def validate_tool_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None:
        if tool_name != "qf_test":
            raise RuntimeError("unexpected test tool")
        self.validator.validate(arguments)


def make_action_and_event(
    schema: dict[str, Any],
    arguments: dict[str, Any],
    tool_call_id: str = "call_exactly_once",
    raw_arguments: dict[str, Any] | None = None,
) -> tuple[Action, ActionEvent]:
    action_type = Action.from_mcp_schema("QfTestCrashAction", schema)
    action = action_type.model_construct(**arguments)
    event = ActionEvent(
        thought=[],
        action=action,
        tool_name="qf_test",
        tool_call_id=tool_call_id,
        tool_call={
            "id": tool_call_id,
            "name": "qf_test",
            "arguments": json.dumps(raw_arguments if raw_arguments is not None else arguments),
            "origin": "completion",
        },
        llm_response_id="test-response",
    )
    return action, event


class ProtocolWriterTests(unittest.TestCase):
    def test_private_agent_server_ports_do_not_overlap_qf_or_xh_services(self) -> None:
        self.assertEqual(len(AGENT_SERVER_PORTS), len(set(AGENT_SERVER_PORTS)))
        self.assertTrue(set(AGENT_SERVER_PORTS).isdisjoint(range(27_871, 27_876)))
        self.assertTrue(set(AGENT_SERVER_PORTS).isdisjoint(range(43_187, 43_193)))

    def test_confirmation_and_pause_statuses_require_explicit_resolution(self) -> None:
        self.assertTrue(
            sidecar._execution_status_requires_resolution(
                sidecar.ConversationExecutionStatus.PAUSED
            )
        )
        self.assertTrue(
            sidecar._execution_status_requires_resolution(
                sidecar.ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
            )
        )
        self.assertFalse(
            sidecar._execution_status_requires_resolution(
                sidecar.ConversationExecutionStatus.FINISHED
            )
        )

    def test_callback_queue_overflow_never_raises_and_fails_closed(self) -> None:
        writer = sidecar.ProtocolWriter(
            capacity=1,
            output=io.StringIO(),
            start_thread=False,
        )
        self.assertTrue(writer.send_callback({"type": "event", "value": 1}))
        self.assertFalse(writer.send_callback({"type": "event", "value": 2}))
        self.assertEqual(writer.failure_code, "IPC_OUTPUT_BACKPRESSURE")
        writer.close()

    def test_concrete_tool_observation_round_trips_through_sdk_union(self) -> None:
        observation = ClientToolObservation.from_text("ok")
        restored = Observation.model_validate(observation.model_dump(mode="json"))
        self.assertIsInstance(restored, ClientToolObservation)

    def test_sdk_token_callback_never_raises_when_output_queue_overflows(self) -> None:
        writer = sidecar.ProtocolWriter(
            capacity=1,
            output=io.StringIO(),
            start_thread=False,
        )
        bridge = sidecar.Bridge(writer)
        runtime = sidecar.SidecarRuntime(bridge, writer)
        chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="token"))])
        runtime._on_token(chunk)
        runtime._on_token(chunk)
        self.assertEqual(writer.failure_code, "IPC_OUTPUT_BACKPRESSURE")
        writer.close()

    def test_agent_message_event_emits_stable_persisted_stream_event(self) -> None:
        writer = CapturingWriter()
        bridge = sidecar.Bridge(writer)  # type: ignore[arg-type]
        runtime = sidecar.SidecarRuntime(bridge, writer)  # type: ignore[arg-type]
        event = MessageEvent(
            id="assistant-event-1",
            source="agent",
            llm_message=Message(
                role="assistant",
                content=[TextContent(text="real persisted response")],
            ),
        )
        runtime._on_event(event)
        self.assertEqual(
            writer.messages,
            [
                {
                    "type": "event",
                    "requestId": "",
                    "eventId": "assistant-event-1",
                    "eventType": "assistant.delta",
                    "payload": {
                        "text": "real persisted response",
                        "backendEventId": "assistant-event-1",
                        "streamMode": "persisted-message",
                    },
                }
            ],
        )

    def test_action_event_reports_deterministic_qf_risk_not_llm_claim(self) -> None:
        writer = CapturingWriter()
        bridge = sidecar.Bridge(writer)  # type: ignore[arg-type]
        runtime = sidecar.SidecarRuntime(bridge, writer)  # type: ignore[arg-type]
        runtime.security_analyzer = sidecar.QfSecurityAnalyzer()
        _, event = make_action_and_event(
            {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            {"command": "echo unsafe > marker.txt"},
            tool_call_id="call_scope_rejected",
        )
        runtime._on_event(event)
        self.assertEqual(writer.messages[0]["payload"]["securityRisk"], "HIGH")


class ToolRecoveryTests(unittest.TestCase):
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["allowed"]},
            "nested": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 2}},
                "required": ["count"],
                "additionalProperties": False,
            },
        },
        "required": ["mode", "nested"],
        "additionalProperties": False,
    }

    def bridge(
        self,
        root: Path,
        writer: CapturingWriter,
        event: ActionEvent,
    ) -> sidecar.Bridge:
        bridge = sidecar.Bridge(writer)  # type: ignore[arg-type]
        bridge.configure_tool_state(root / "tool-journal")
        bridge.runtime = FakeRuntime(self.schema)  # type: ignore[assignment]
        self.assertEqual(bridge.rebuild_action_index([event]), 1)
        return bridge

    def test_original_json_schema_blocks_call_before_host_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, event = make_action_and_event(
                self.schema,
                {"mode": "allowed", "nested": {"count": 2}},
                "call_schema_rejected",
                raw_arguments={
                    "mode": "forbidden",
                    "nested": {"count": "2", "extra": True},
                },
            )
            writer = CapturingWriter()
            bridge = self.bridge(Path(directory), writer, event)
            with self.assertRaisesRegex(RuntimeError, "versioned JSON Schema"):
                bridge.execute_mcp_tool(
                    "qf_test",
                    {"mode": "forbidden", "nested": {"count": "2", "extra": True}},
                )
            self.assertFalse(any(item.get("type") == "tool.call" for item in writer.messages))

    def test_rebuild_excludes_actions_with_persisted_terminal_event(self) -> None:
        _, event = make_action_and_event(
            self.schema,
            {"mode": "allowed", "nested": {"count": 2}},
            "call_terminal",
        )
        terminal = AgentErrorEvent(
            tool_name="qf_test",
            tool_call_id="call_terminal",
            error="persisted terminal",
        )
        writer = CapturingWriter()
        bridge = sidecar.Bridge(writer)  # type: ignore[arg-type]
        self.assertEqual(bridge.rebuild_action_index([event, terminal]), 0)

    def test_recovered_terminal_candidate_does_not_shadow_identical_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = {"mode": "allowed", "nested": {"count": 2}}
            _, stale = make_action_and_event(self.schema, arguments, "call_stale")
            _, retry = make_action_and_event(self.schema, arguments, "call_retry")
            writer = CapturingWriter()
            bridge = self.bridge(root, writer, stale)
            bridge.register_action(retry)
            request_sha256 = sidecar._sha256({"toolName": "qf_test", "arguments": arguments})
            bridge._write_journal({
                "status": "COMPLETED",
                "toolCallId": "call_stale",
                "toolName": "qf_test",
                "requestSha256": request_sha256,
                "message": {"ok": False, "result": None, "error": {"message": "old failure"}},
            })

            def resolve(message: dict[str, Any]) -> None:
                if message.get("type") == "tool.call":
                    bridge._resolve_tool({
                        "type": "tool.result",
                        "requestId": message["requestId"],
                        "ok": True,
                        "result": {"accepted": True},
                    })

            writer.on_send = resolve
            result = bridge.execute_mcp_tool("qf_test", arguments)
            self.assertEqual(result, {"accepted": True})
            calls = [item for item in writer.messages if item.get("type") == "tool.call"]
            self.assertEqual([item["toolCallId"] for item in calls], ["call_retry"])

    def test_crash_before_result_reuses_stable_id_for_host_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = {"mode": "allowed", "nested": {"count": 2}}
            _, event = make_action_and_event(
                self.schema,
                arguments,
            )
            first_writer = CapturingWriter()
            first_bridge = self.bridge(root, first_writer, event)
            effects: list[str] = []
            host_cache: dict[str, dict[str, str]] = {}

            def crash_before_result(message: dict[str, Any]) -> None:
                if message.get("type") == "tool.call":
                    tool_call_id = str(message["toolCallId"])
                    if tool_call_id not in host_cache:
                        effects.append(tool_call_id)
                        host_cache[tool_call_id] = {"effect": "committed-once"}
                    raise RuntimeError("simulated process crash before result")

            first_writer.on_send = crash_before_result
            with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
                first_bridge.execute_mcp_tool("qf_test", arguments)

            second_writer = CapturingWriter()
            second_bridge = self.bridge(root, second_writer, event)

            def deduplicating_host(message: dict[str, Any]) -> None:
                if message.get("type") != "tool.call":
                    return
                tool_call_id = str(message["toolCallId"])
                if tool_call_id not in host_cache:
                    effects.append(tool_call_id)
                    host_cache[tool_call_id] = {"effect": "committed-once"}
                second_bridge._resolve_tool(
                    {
                        "requestId": message["requestId"],
                        "ok": True,
                        "result": host_cache[tool_call_id],
                    }
                )

            second_writer.on_send = deduplicating_host
            result = second_bridge.execute_mcp_tool("qf_test", arguments)
            calls = [
                item
                for item in first_writer.messages + second_writer.messages
                if item.get("type") == "tool.call"
            ]
            self.assertEqual({item["toolCallId"] for item in calls}, {"call_exactly_once"})
            self.assertEqual(effects, ["call_exactly_once"])
            self.assertEqual(result, {"effect": "committed-once"})

    def test_crash_after_result_recovers_journal_without_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = {"mode": "allowed", "nested": {"count": 2}}
            _, event = make_action_and_event(
                self.schema,
                arguments,
            )
            effects: list[str] = []
            first_writer = CapturingWriter()
            first_bridge = self.bridge(root, first_writer, event)

            def host_then_crash(message: dict[str, Any]) -> None:
                if message.get("type") != "tool.call":
                    return
                effects.append(str(message["toolCallId"]))
                first_bridge._resolve_tool(
                    {
                        "requestId": message["requestId"],
                        "ok": True,
                        "result": {"effect": "committed-once"},
                    }
                )
                raise RuntimeError("simulated process crash after result persistence")

            first_writer.on_send = host_then_crash
            with self.assertRaisesRegex(RuntimeError, "after result persistence"):
                first_bridge.execute_mcp_tool("qf_test", arguments)

            second_writer = CapturingWriter()
            second_bridge = self.bridge(root, second_writer, event)
            result = second_bridge.execute_mcp_tool("qf_test", arguments)
            self.assertEqual(effects, ["call_exactly_once"])
            self.assertFalse(
                any(item.get("type") == "tool.call" for item in second_writer.messages)
            )
            self.assertEqual(result, {"effect": "committed-once"})


class ManifestAndIsolationTests(unittest.TestCase):
    def runtime(self) -> sidecar.SidecarRuntime:
        writer = CapturingWriter()
        bridge = sidecar.Bridge(writer)  # type: ignore[arg-type]
        runtime = sidecar.SidecarRuntime(bridge, writer)  # type: ignore[arg-type]
        runtime.runtime_session_id = "22222222-2222-4222-8222-222222222222"
        return runtime

    def test_manifest_is_hashed_and_recovery_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.runtime()
            manifest = {"modelId": "deepseek-v4-pro", "promptSha256": "a" * 64}
            with mock.patch.object(sidecar, "STATE_ROOT", root):
                digest = runtime._persist_or_validate_manifest(manifest)
                self.assertEqual(digest, sidecar._sha256(manifest))
                self.assertEqual(runtime._persist_or_validate_manifest(manifest), digest)
                with self.assertRaisesRegex(RuntimeError, "manifest mismatch"):
                    runtime._persist_or_validate_manifest(
                        {"modelId": "other-model", "promptSha256": "a" * 64}
                    )

    def test_existing_persistence_without_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.runtime()
            event_dir = (
                root / "persistence" / runtime.runtime_session_id.replace("-", "") / "events"
            )
            event_dir.mkdir(parents=True)
            (event_dir / "event.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(sidecar, "STATE_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "no runtime manifest"):
                    runtime._persist_or_validate_manifest({"modelId": "deepseek-v4-pro"})

    def test_runtime_manifest_covers_config_without_persisting_api_key(self) -> None:
        runtime = self.runtime()
        runtime.provider = "deepseek"
        runtime.model_id = "deepseek-v4-pro"
        runtime.all_tool_names = frozenset({"qf_test"})
        message = {
            "baseUrl": "https://example.invalid/v1",
            "apiKey": "must-not-persist",
            "systemPromptHash": "a" * 64,
            "currentDateTime": "2026-07-29T00:00:00Z",
            "maxOutputTokens": 512,
            "maxIterations": 8,
        }
        tool_specs = [
            {
                "name": "qf_test",
                "description": "test",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        ]
        manifest = runtime._manifest(message, tool_specs)
        self.assertEqual(manifest["sdkVersion"], sidecar.EXPECTED_SDK_VERSION)
        self.assertEqual(manifest["modelId"], "deepseek-v4-pro")
        self.assertEqual(manifest["systemPromptSha256"], "a" * 64)
        self.assertEqual(manifest["toolSpecs"], tool_specs)
        self.assertNotIn("must-not-persist", sidecar._canonical(manifest))

    def test_wrapper_declares_resource_and_process_isolation_gates(self) -> None:
        wrapper = Path(__file__).parents[1] / "run-isolated.sh"
        text = wrapper.read_text(encoding="utf-8")
        for assertion in (
            "ulimit -c 0",
            "ulimit -n 256",
            "ulimit -u 1024",
            "ulimit -v 2097152",
            "flock -n 9",
            "--cap-drop ALL",
            "--tmpfs /mnt",
            "--bind /run/docker.sock /run/docker.sock",
            "QF_OPENHANDS_AGENT_SERVER_IMAGE",
        ):
            self.assertIn(assertion, text)


if __name__ == "__main__":
    unittest.main()
