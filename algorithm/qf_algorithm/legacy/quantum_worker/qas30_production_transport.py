"""Production-only transport selection and local credential loading for QAS30.

The public command payload identifies the exact target and transport class.  The
connection key is read separately from one caller-selected, owner-only ``.env``
file and is returned only in memory.  This module never serializes credential
values or provider tokens.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import qas30_tianyan

PRODUCTION_TRANSPORT_KIND = "TIANYAN176_PRODUCTION_SINGLE_ATTEMPT"
TARGET = "tianyan176"
CREDENTIAL_NAME = "TIANYAN176_CONNECTION_KEY"
MAXIMUM_CREDENTIAL_FILE_BYTES = 64 * 1024


class Qas30ProductionTransportError(RuntimeError):
    """A production transport or credential boundary failed closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.resubmit_allowed = False


def validate_production_transport_spec(value: Any) -> dict[str, str]:
    """Require an explicit exact-target marker before any credential is read."""

    if (
        not isinstance(value, Mapping)
        or set(value) != {"kind", "target"}
        or value.get("kind") != PRODUCTION_TRANSPORT_KIND
        or value.get("target") != TARGET
    ):
        raise Qas30ProductionTransportError(
            "QAS30_PRODUCTION_TRANSPORT_INVALID",
            "production transport must bind the exact Tianyan176 single-attempt boundary",
        )
    return {"kind": PRODUCTION_TRANSPORT_KIND, "target": TARGET}


def _credential_error(code: str, message: str) -> Qas30ProductionTransportError:
    return Qas30ProductionTransportError(code, message)


def _read_owned_dotenv(path: Path) -> str:
    if not path.is_absolute() or path.name != ".env":
        raise _credential_error(
            "QAS30_CREDENTIAL_SOURCE_INVALID",
            "credential source must be an absolute .env path",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _credential_error(
            "QAS30_CREDENTIAL_SOURCE_UNAVAILABLE",
            "credential source is unavailable",
        ) from error
    opened: os.stat_result | None = None
    closed: os.stat_result | None = None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_size < 1
            or opened.st_size > MAXIMUM_CREDENTIAL_FILE_BYTES
        ):
            raise _credential_error(
                "QAS30_CREDENTIAL_SOURCE_UNSAFE",
                "credential source metadata is unsafe",
            )
        parts: list[bytes] = []
        remaining = MAXIMUM_CREDENTIAL_FILE_BYTES + 1
        while remaining > 0:
            part = os.read(descriptor, remaining)
            if not part:
                break
            parts.append(part)
            remaining -= len(part)
        payload = b"".join(parts)
        if len(payload) > MAXIMUM_CREDENTIAL_FILE_BYTES:
            raise _credential_error(
                "QAS30_CREDENTIAL_SOURCE_UNSAFE",
                "credential source exceeds the governed size limit",
            )
        closed = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    try:
        linked = path.lstat()
    except OSError as error:
        raise _credential_error(
            "QAS30_CREDENTIAL_SOURCE_CHANGED",
            "credential source changed while reading",
        ) from error
    if (
        opened is None
        or closed is None
        or stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or linked.st_uid != os.geteuid()
        or stat.S_IMODE(linked.st_mode) != 0o600
        or linked.st_nlink != 1
        or linked.st_size < 1
        or linked.st_size > MAXIMUM_CREDENTIAL_FILE_BYTES
        or len(payload) != opened.st_size
        or (linked.st_dev, linked.st_ino, linked.st_size, linked.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or (closed.st_dev, closed.st_ino, closed.st_size, closed.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    ):
        raise _credential_error(
            "QAS30_CREDENTIAL_SOURCE_CHANGED",
            "credential source changed while reading",
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _credential_error(
            "QAS30_CREDENTIAL_SOURCE_INVALID",
            "credential source is not UTF-8 text",
        ) from error


def _dotenv_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise _credential_error(
            "QAS30_CREDENTIAL_VALUE_INVALID",
            "the Tianyan176 credential value is invalid",
        )
    return value


def load_tianyan176_connection_key(credential_file: Path) -> str:
    """Load the exact Tianyan176 key from a metadata-validated local file."""

    value: str | None = None
    for raw_line in _read_owned_dotenv(credential_file).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        if name.strip() != CREDENTIAL_NAME:
            continue
        if not separator or value is not None:
            raise _credential_error(
                "QAS30_CREDENTIAL_ASSIGNMENT_INVALID",
                "the Tianyan176 credential assignment is invalid",
            )
        value = _dotenv_value(raw_value)
    if value is None:
        raise _credential_error(
            "QAS30_CREDENTIAL_NOT_CONFIGURED",
            "the exact Tianyan176 credential is not configured",
        )
    return value


def new_single_attempt_transport() -> qas30_tianyan.RequestsSingleAttemptTransport:
    """Return an observed-evidence transport with all automatic retries disabled."""

    return qas30_tianyan.RequestsSingleAttemptTransport()
