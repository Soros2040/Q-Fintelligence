"""Deterministic QF policy layered on OpenHands security controls."""

from __future__ import annotations

import re
import shlex
from typing import Any

from openhands.sdk.event import ActionEvent
from openhands.sdk.security import PatternSecurityAnalyzer, SecurityRisk

FORBIDDEN_ACTION_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"(^|[\s;&|])sudo(?:\s|$)", "sudo is outside the approved run", "qf-sudo"),
    (
        r"(^|[\s;&|])(?:docker|podman|nerdctl|nsenter|mount|umount)(?:\s|$)",
        "host or container control is forbidden",
        "qf-host-control",
    ),
    (
        r"(^|[\s;&|])git\s+(?:commit|push|remote|config)(?:\s|$)",
        "repository publishing and identity changes are forbidden",
        "qf-git-control",
    ),
    (
        r"(?:curl|wget)\b[^\n|;&]*\|\s*(?:sh|bash)\b",
        "download-and-execute pipelines are forbidden",
        "qf-download-execute",
    ),
    (
        r"(?:^|[\s\"'])(?:/home|/mnt|/root|/etc|/proc|/sys|/openhands-state|/var/run/docker\.sock)(?:/|[\s\"']|$)",
        "host and runtime-state paths are outside the run workspace",
        "qf-host-path",
    ),
    (
        r"(?:^|/)\.(?:env|ssh)(?:/|[\s\"']|$)",
        "secret-bearing paths are forbidden",
        "qf-secret-path",
    ),
    (
        r"(^|[\s;&|])(?:shutdown|reboot|poweroff|mkfs|fdisk)(?:\s|$)",
        "destructive system control is forbidden",
        "qf-system-control",
    ),
    (
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
        "fork bombs are forbidden",
        "qf-fork-bomb",
    ),
)

NETWORK_ACTION_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"(^|[\s;&|])(?:curl|wget|nc|ncat|ssh|scp|sftp|rsync)(?:\s|$)",
        "workspace network access requires an explicit allowlist",
        "qf-network",
    ),
    (
        r"(^|[\s;&|])(?:npm|pnpm|yarn|pip|uv|apt|apt-get)\s+(?:add|install|sync|update)",
        "dependency installation requires explicit approval",
        "qf-dependency-install",
    ),
)

_FORBIDDEN = tuple(
    (re.compile(pattern, re.IGNORECASE), description, detection_id)
    for pattern, description, detection_id in FORBIDDEN_ACTION_PATTERNS
)
_SAFE_READ_PROGRAMS = frozenset(
    {"cat", "find", "grep", "head", "ls", "pwd", "rg", "sed", "tail", "wc"}
)
_SHELL_CONTROL_PATTERN = re.compile(r"(?:[;&|<>`]|\$\(|\r|\n)")


def _action_text(action: ActionEvent) -> str:
    if action.action is None:
        return ""
    payload = action.action.model_dump(mode="json")
    return "\n".join(
        value
        for key in ("command", "path", "old_str", "new_str", "file_text", "url", "query")
        if isinstance((value := payload.get(key)), str)
    )


def forbidden_action_reason(action: ActionEvent) -> str | None:
    text = _action_text(action)
    for pattern, description, detection_id in _FORBIDDEN:
        if pattern.search(text):
            return f"{detection_id}: {description}"
    return None


def _workspace_relative_path(value: str) -> str | None:
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/workspace/"):
        normalized = normalized.removeprefix("/workspace/")
    elif normalized == "/workspace":
        return ""
    elif normalized.startswith("/"):
        return None
    pieces = [piece for piece in normalized.split("/") if piece not in ("", ".")]
    if ".." in pieces:
        return None
    return "/".join(pieces)


def path_is_allowed(value: str, allowed_paths: tuple[str, ...]) -> bool:
    relative = _workspace_relative_path(value)
    if relative is None:
        return False
    for configured in allowed_paths:
        allowed = _workspace_relative_path(configured)
        if allowed is None:
            continue
        if allowed == "" or relative == allowed or relative.startswith(f"{allowed}/"):
            return True
    return False


def _command_is_allowed(command: str, allowed_commands: tuple[str, ...]) -> bool:
    if _SHELL_CONTROL_PATTERN.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    if tokens[0] in _SAFE_READ_PROGRAMS:
        return "-i" not in tokens and "--in-place" not in tokens
    if tokens[0] == "git":
        return len(tokens) >= 2 and tokens[1] in {"diff", "log", "show", "status"}
    return any(
        (approved_tokens := shlex.split(approved))
        and tokens[: len(approved_tokens)] == approved_tokens
        for approved in allowed_commands
    )


def action_policy_reason(
    action: ActionEvent,
    *,
    allow_network: bool,
    allowed_paths: tuple[str, ...],
    allowed_commands: tuple[str, ...],
) -> str | None:
    if reason := forbidden_action_reason(action):
        return reason
    payload = {} if action.action is None else action.action.model_dump(mode="json")
    path = payload.get("path")
    if isinstance(path, str):
        if payload.get("command") == "view":
            if _workspace_relative_path(path) is None:
                return "qf-path-scope: file read escaped the run workspace"
        elif not path_is_allowed(path, allowed_paths):
            return "qf-path-scope: file edit escaped the approved scope"
    command = payload.get("command")
    if isinstance(command, str) and not isinstance(path, str):
        if not _command_is_allowed(command, allowed_commands):
            return "qf-command-scope: command escaped the approved test scope"
    if not allow_network and any(
        re.search(pattern, _action_text(action), re.IGNORECASE)
        for pattern, _description, _detection_id in NETWORK_ACTION_PATTERNS
    ):
        return "qf-network: workspace network access is not approved"
    return None


class QfSecurityAnalyzer(PatternSecurityAnalyzer):
    """OpenHands analyzer extended by versioned QF authorization scopes."""

    allow_network: bool = False
    allowed_paths: tuple[str, ...] = ("",)
    allowed_commands: tuple[str, ...] = (
        "npm run typecheck",
        "npm test",
        "npm run build",
        "uv run pytest",
        "uv run ruff check",
    )

    def __init__(
        self,
        *,
        allow_network: bool = False,
        allowed_paths: tuple[str, ...] = ("",),
        allowed_commands: tuple[str, ...] = (
            "npm run typecheck",
            "npm test",
            "npm run build",
            "uv run pytest",
            "uv run ruff check",
        ),
        **data: Any,
    ) -> None:
        inherited = list(data.pop("high_patterns", PatternSecurityAnalyzer().high_patterns))
        super().__init__(
            high_patterns=[
                *inherited,
                *FORBIDDEN_ACTION_PATTERNS,
                *(() if allow_network else NETWORK_ACTION_PATTERNS),
            ],
            allow_network=allow_network,
            allowed_paths=allowed_paths,
            allowed_commands=allowed_commands,
            **data,
        )

    def security_risk(self, action: ActionEvent) -> SecurityRisk:
        if action_policy_reason(
            action,
            allow_network=self.allow_network,
            allowed_paths=self.allowed_paths,
            allowed_commands=self.allowed_commands,
        ):
            return SecurityRisk.HIGH
        return super().security_risk(action)
