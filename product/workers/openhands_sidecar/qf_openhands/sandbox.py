"""Per-run repository snapshots and a hardened OpenHands Agent Server."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

# These are private loopback ingress ports for per-conversation Agent Servers,
# not QF's public 27871-27875 service surface. Keep the pool disjoint from both
# QF and the read-only XH reference service.
AGENT_SERVER_PORTS = tuple(range(27_900, 27_964))
IMAGE_REFERENCE = "qfintelligence/openhands-agent-server:1.39.0-qf.1"
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_SECRET_PATTERN = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/-]{8,})"
)


def redact_text(value: str) -> str:
    redacted = _SECRET_PATTERN.sub("[REDACTED]", value)
    for name in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "TUSHARE_TOKEN",
        "TIANYAN_CONNECTION_KEY",
        "SESSION_API_KEY",
    ):
        secret = os.environ.get(name)
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 180.0,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "DOCKER_CONFIG": "/tmp/qf-docker-config",
        },
    )
    if check and completed.returncode != 0:
        message = redact_text(
            completed.stderr.decode("utf-8", errors="replace")[-4_000:]
        )
        raise RuntimeError(f"command failed ({completed.returncode}): {message}")
    return completed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def create_repository_snapshot(
    source_root: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    """Copy only Git-visible files into a new secret-free run snapshot."""

    source_root = source_root.resolve()
    workspace_root = workspace_root.resolve()
    if workspace_root.exists() and any(workspace_root.iterdir()):
        manifest_path = workspace_root / ".qf-snapshot.json"
        if not manifest_path.is_file():
            raise RuntimeError("existing workspace has no QF snapshot manifest")
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    listed = _run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=source_root,
    ).stdout
    relative_paths = [
        item.decode("utf-8", errors="strict")
        for item in listed.split(b"\0")
        if item
    ]
    if len(relative_paths) > MAX_SNAPSHOT_FILES:
        raise RuntimeError("repository snapshot exceeds the file-count limit")
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for relative in sorted(relative_paths):
        parts = Path(relative).parts
        if (
            not parts
            or ".." in parts
            or any(part in {".env", ".ssh", ".runtime", ".local"} for part in parts)
        ):
            continue
        source = source_root / relative
        if source.is_symlink():
            raise RuntimeError(f"repository snapshot rejects symlink: {relative}")
        if not source.is_file():
            continue
        size = source.stat().st_size
        total_bytes += size
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise RuntimeError("repository snapshot exceeds the byte limit")
        target = workspace_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append(
            {"path": relative, "bytes": size, "sha256": _sha256_file(target)}
        )
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    head = _run(["git", "rev-parse", "HEAD"], cwd=source_root).stdout.decode().strip()
    _run(["git", "init", "--quiet"], cwd=workspace_root)
    _run(["git", "config", "user.name", "QF OpenHands Sandbox"], cwd=workspace_root)
    _run(
        ["git", "config", "user.email", "sandbox@example.invalid"],
        cwd=workspace_root,
    )
    _run(["git", "config", "core.hooksPath", "/dev/null"], cwd=workspace_root)
    _run(["git", "config", "credential.helper", ""], cwd=workspace_root)
    _run(["git", "add", "--all"], cwd=workspace_root)
    _run(
        ["git", "commit", "--quiet", "--no-gpg-sign", "-m", "QF run snapshot"],
        cwd=workspace_root,
    )
    manifest = {
        "schemaVersion": "qf.openhands-workspace-snapshot.v1",
        "sourceHead": head,
        "snapshotSha256": snapshot_hash,
        "fileCount": len(files),
        "totalBytes": total_bytes,
    }
    manifest_path = workspace_root / ".qf-snapshot.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    return manifest


@dataclass(frozen=True)
class DockerServerHandle:
    container_id: str
    ingress_container_id: str
    egress_container_id: str
    tool_proxy_container_id: str
    internal_network: str
    ingress_network: str
    egress_network: str
    host: str
    api_key: str
    image_reference: str
    image_digest: str
    mcp_url: str
    workspace_path: str
    state_path: str
    port: int


class HardenedDockerAgentServer:
    """Own the lifecycle of one fail-closed, per-run Agent Server sandbox."""

    def __init__(
        self,
        state_root: Path,
        workspace_root: Path,
        tool_socket_path: Path,
    ) -> None:
        self.state_root = state_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.server_state = self.state_root / "agent-server"
        self.tool_socket_path = tool_socket_path.resolve()
        self.handle_path = self.state_root / "agent-server-handle.json"
        self.credential_path = self.state_root / "agent-server-credential.json"
        self.server_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.image_reference = os.environ.get(
            "QF_OPENHANDS_AGENT_SERVER_IMAGE",
            IMAGE_REFERENCE,
        ).strip()
        if not self.image_reference or ":latest" in self.image_reference:
            raise ValueError("OpenHands Agent Server image must be exactly versioned")

    def _load_or_create_session_token(self) -> str:
        if self.credential_path.is_file():
            record = json.loads(self.credential_path.read_text(encoding="utf-8"))
            token = record.get("token")
            if (
                record.get("schemaVersion")
                != "qf.openhands-agent-server-credential.v1"
                or not isinstance(token, str)
                or len(token) < 43
            ):
                raise RuntimeError("Agent Server credential record is invalid")
            return token
        token = secrets.token_urlsafe(36)
        self.credential_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "qf.openhands-agent-server-credential.v1",
                    "token": token,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(self.credential_path, 0o600)
        return token

    @staticmethod
    def doctor() -> tuple[bool, list[str]]:
        docker = shutil.which("docker")
        if docker is None:
            return False, ["docker CLI missing"]
        server = _run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False)
        if server.returncode != 0:
            return False, ["docker daemon unavailable"]
        image = os.environ.get(
            "QF_OPENHANDS_AGENT_SERVER_IMAGE",
            IMAGE_REFERENCE,
        ).strip()
        inspected = _run(["docker", "image", "inspect", image], check=False)
        return (
            inspected.returncode == 0,
            [
                f"docker server {server.stdout.decode().strip()}",
                (
                    "versioned Agent Server image present"
                    if inspected.returncode == 0
                    else "versioned Agent Server image missing"
                ),
            ],
        )

    def start_or_recover(self, model_base_url: str) -> tuple[DockerServerHandle, bool]:
        if self.handle_path.is_file():
            raw = json.loads(self.handle_path.read_text(encoding="utf-8"))
            handle = DockerServerHandle(**raw)
            if (
                Path(handle.workspace_path).resolve() == self.workspace_root
                and Path(handle.state_path).resolve() == self.server_state
                and self._is_healthy(handle)
            ):
                return handle, True
            self._cleanup(handle)
        return self._start(model_base_url), False

    def _start(self, model_base_url: str) -> DockerServerHandle:
        available, details = self.doctor()
        if not available:
            raise RuntimeError("; ".join(details))
        parsed = urlparse(model_base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("model base URL must be HTTPS")
        port = 0
        token = self._load_or_create_session_token()
        slug = hashlib.sha256(str(self.state_root).encode()).hexdigest()[:16]
        uid, gid = os.getuid(), os.getgid()
        if uid == 0:
            raise RuntimeError("Agent Server launcher must run as a non-root user")
        runtime_user = f"{uid}:{gid}"
        internal = f"qf-oh-net-{slug}"
        ingress_network = f"qf-oh-ingress-{slug}"
        egress_network = f"qf-oh-egress-{slug}"
        agent_name = f"qf-oh-agent-{slug}"
        ingress_name = f"qf-oh-gateway-{slug}"
        egress_name = f"qf-oh-proxy-{slug}"
        tool_name = f"qf-oh-tools-{slug}"
        for network, internal_only in (
            (internal, True),
            (ingress_network, False),
            (egress_network, False),
        ):
            arguments = ["docker", "network", "create", "--driver", "bridge"]
            if internal_only:
                arguments.append("--internal")
            arguments.append(network)
            _run(arguments)
        container_id = ingress_id = egress_id = tool_proxy_id = ""
        try:
            tool_proxy_id = _run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    tool_name,
                    "--label",
                    f"qf.openhands.run={slug}",
                    "--user",
                    runtime_user,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--pids-limit",
                    "32",
                    "--memory",
                    "128m",
                    "--cpus",
                    "0.25",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,size=16m",
                    "--network",
                    internal,
                    "--mount",
                    f"type=bind,src={self.tool_socket_path.parent},dst=/qf-tool",
                    self.image_reference,
                    "python",
                    "/opt/qf/unix_ingress_proxy.py",
                    "--socket-path",
                    f"/qf-tool/{self.tool_socket_path.name}",
                    "--listen-port",
                    "8001",
                ]
            ).stdout.decode().strip()
            egress_id = _run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    egress_name,
                    "--label",
                    f"qf.openhands.run={slug}",
                    "--user",
                    runtime_user,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--pids-limit",
                    "32",
                    "--memory",
                    "128m",
                    "--cpus",
                    "0.25",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,size=16m",
                    "--network",
                    internal,
                    self.image_reference,
                    "python",
                    "/opt/qf/egress_proxy.py",
                    "--allow-host",
                    parsed.hostname,
                ]
            ).stdout.decode().strip()
            _run(["docker", "network", "connect", egress_network, egress_id])
            container_id = _run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    agent_name,
                    "--label",
                    f"qf.openhands.run={slug}",
                    "--user",
                    runtime_user,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--pids-limit",
                    os.environ.get("QF_OPENHANDS_SANDBOX_PIDS", "256"),
                    "--memory",
                    os.environ.get("QF_OPENHANDS_SANDBOX_MEMORY", "4g"),
                    "--cpus",
                    os.environ.get("QF_OPENHANDS_SANDBOX_CPUS", "2"),
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,size=512m",
                    "--tmpfs",
                    f"/opt/qf/deps/node_modules/.vite:rw,nosuid,nodev,noexec,uid={uid},gid={gid},mode=0700,size=256m",
                    "--network",
                    internal,
                    "--mount",
                    f"type=bind,src={self.workspace_root},dst=/workspace",
                    "--mount",
                    f"type=bind,src={self.server_state},dst=/openhands-state",
                    "--env",
                    f"SESSION_API_KEY={token}",
                    "--env",
                    "OH_CONVERSATIONS_PATH=/openhands-state/conversations",
                    "--env",
                    "OH_WORKSPACE_PATH=/workspace",
                    "--env",
                    "OH_BASH_EVENTS_DIR=/openhands-state/bash-events",
                    "--env",
                    "OH_TELEMETRY_EXPORTER=none",
                    "--env",
                    "OH_ENABLE_VSCODE=false",
                    "--env",
                    "OPENHANDS_SUPPRESS_BANNER=1",
                    "--env",
                    "LITELLM_LOCAL_MODEL_COST_MAP=True",
                    "--env",
                    "HOME=/tmp/home",
                    "--env",
                    "XDG_CACHE_HOME=/tmp/cache",
                    "--env",
                    f"HTTP_PROXY=http://{egress_name}:3128",
                    "--env",
                    f"HTTPS_PROXY=http://{egress_name}:3128",
                    "--env",
                    (
                        "NO_PROXY=127.0.0.1,localhost,"
                        f"{egress_name},{ingress_name},{tool_name}"
                    ),
                    self.image_reference,
                    "bash",
                    "-c",
                    "mkdir -p /tmp/home /tmp/cache && "
                    "test ! -e /workspace/.env && test ! -e /workspace/.ssh && "
                    "test ! -e /var/run/docker.sock && "
                    "if [ ! -e /workspace/node_modules ]; then "
                    "ln -s /opt/qf/deps/node_modules /workspace/node_modules; fi && "
                    "exec /opt/qf/python/bin/python "
                    "/opt/qf/agent_server_entrypoint.py "
                    "--host 0.0.0.0 --port 8000",
                ],
                timeout=240,
            ).stdout.decode().strip()
            with self._port_allocation_lock():
                port = self._available_port()
                ingress_id = _run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        ingress_name,
                        "--label",
                        f"qf.openhands.run={slug}",
                        "--user",
                        runtime_user,
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges:true",
                        "--pids-limit",
                        "32",
                        "--memory",
                        "128m",
                        "--cpus",
                        "0.25",
                        "--read-only",
                        "--tmpfs",
                        "/tmp:rw,nosuid,nodev,noexec,size=16m",
                        "--network",
                        internal,
                        "--publish",
                        f"127.0.0.1:{port}:8000",
                        self.image_reference,
                        "python",
                        "/opt/qf/ingress_proxy.py",
                        "--target-host",
                        agent_name,
                        "--target-port",
                        "8000",
                        "--listen-port",
                        "8000",
                    ]
                ).stdout.decode().strip()
            _run(["docker", "network", "connect", ingress_network, ingress_id])
            image_digest = _run(
                ["docker", "image", "inspect", self.image_reference, "--format", "{{.Id}}"]
            ).stdout.decode().strip()
            handle = DockerServerHandle(
                container_id=container_id,
                ingress_container_id=ingress_id,
                egress_container_id=egress_id,
                tool_proxy_container_id=tool_proxy_id,
                internal_network=internal,
                ingress_network=ingress_network,
                egress_network=egress_network,
                host=f"http://127.0.0.1:{port}",
                api_key=token,
                image_reference=self.image_reference,
                image_digest=image_digest,
                mcp_url=f"http://{tool_name}:8001",
                workspace_path=str(self.workspace_root),
                state_path=str(self.server_state),
                port=port,
            )
            self._wait_healthy(handle)
            self.handle_path.write_text(
                json.dumps(asdict(handle), ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(self.handle_path, 0o600)
            return handle
        except Exception as error:
            temporary = DockerServerHandle(
                container_id=container_id,
                ingress_container_id=ingress_id,
                egress_container_id=egress_id,
                tool_proxy_container_id=tool_proxy_id,
                internal_network=internal,
                ingress_network=ingress_network,
                egress_network=egress_network,
                host=f"http://127.0.0.1:{port}",
                api_key=token,
                image_reference=self.image_reference,
                image_digest="",
                mcp_url=f"http://{tool_name}:8001",
                workspace_path=str(self.workspace_root),
                state_path=str(self.server_state),
                port=port,
            )
            diagnostics = self._diagnostics(temporary)
            self._cleanup(temporary)
            raise RuntimeError(f"{error}; {diagnostics}") from error

    def stop(self) -> None:
        if not self.handle_path.is_file():
            return
        handle = DockerServerHandle(
            **json.loads(self.handle_path.read_text(encoding="utf-8"))
        )
        self._cleanup(handle)
        self.handle_path.unlink(missing_ok=True)

    @staticmethod
    @contextmanager
    def _port_allocation_lock() -> Iterator[None]:
        project_source = os.environ.get("QF_OPENHANDS_PROJECT_SOURCE", "").strip()
        if not project_source:
            raise RuntimeError("QF OpenHands project source is required for port allocation")
        shared_root = Path(project_source).resolve() / ".local" / "ohs"
        shared_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = shared_root / "agent-server-port.lock"
        with lock_path.open("a+b") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _available_port() -> int:
        for port in AGENT_SERVER_PORTS:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
        raise RuntimeError("QF Agent Server private loopback port pool 27900-27963 is occupied")

    @staticmethod
    def _health_request(handle: DockerServerHandle) -> bool:
        request = Request(
            f"{handle.host}/alive",
            headers={"X-Session-API-Key": handle.api_key},
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
                return 200 <= int(response.status) < 300
        except (OSError, URLError):
            return False

    def _wait_healthy(self, handle: DockerServerHandle) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._health_request(handle):
                return
            time.sleep(0.25)
        raise TimeoutError("OpenHands Agent Server health check timed out")

    @classmethod
    def _is_healthy(cls, handle: DockerServerHandle) -> bool:
        running = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", handle.container_id],
            check=False,
        )
        return (
            running.returncode == 0
            and running.stdout.strip() == b"true"
            and cls._health_request(handle)
        )

    @staticmethod
    def _diagnostics(handle: DockerServerHandle) -> str:
        values: list[str] = []
        for label, identifier in (
            ("agent", handle.container_id),
            ("ingress", handle.ingress_container_id),
            ("egress", handle.egress_container_id),
        ):
            if not identifier:
                continue
            output = _run(
                ["docker", "logs", "--tail", "80", identifier],
                check=False,
                timeout=10,
            )
            rendered = redact_text(
                (output.stdout + output.stderr).decode("utf-8", errors="replace")
            ).strip()
            if rendered:
                values.append(f"{label}_logs={rendered[-2_000:]}")
        return "; ".join(values)

    @staticmethod
    def _cleanup(handle: DockerServerHandle) -> None:
        if handle.container_id:
            # Give Agent Server lifespan handlers time to flush events and
            # release the durable conversation lease. A later forced removal
            # remains the bounded fail-safe.
            _run(
                ["docker", "stop", "--time", "30", handle.container_id],
                check=False,
                timeout=40,
            )
        for identifier in (
            handle.container_id,
            handle.ingress_container_id,
            handle.egress_container_id,
            handle.tool_proxy_container_id,
        ):
            if identifier:
                _run(["docker", "rm", "--force", identifier], check=False)
        for network, pattern in (
            (handle.internal_network, r"qf-oh-net-[a-f0-9]{16}"),
            (handle.ingress_network, r"qf-oh-ingress-[a-f0-9]{16}"),
            (handle.egress_network, r"qf-oh-egress-[a-f0-9]{16}"),
        ):
            if re.fullmatch(pattern, network):
                _run(["docker", "network", "rm", network], check=False)
