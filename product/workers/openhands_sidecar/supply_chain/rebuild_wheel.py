from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

SUPPLY_CHAIN_DIR = Path(__file__).resolve().parent
LOCK_PATH = SUPPLY_CHAIN_DIR / "source-lock.json"
PATCH_PATH = SUPPLY_CHAIN_DIR / "openhands-sdk-noobservability.patch"
NOTICE_PATH = SUPPLY_CHAIN_DIR / "NOTICE"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


class BuildError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise BuildError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def file_map(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(root).parts
        and path.suffix != ".pyc"
    }


def aggregate_tree_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative_path, file_hash in sorted(files.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_hash))
    return digest.hexdigest()


def read_lock() -> dict[str, Any]:
    data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    require_equal("source lock schema", data.get("schema_version"), 1)
    return data


def verify_recipe_files(lock: dict[str, Any]) -> None:
    derivative = lock["derivative"]
    require_equal("patch SHA-256", sha256_file(PATCH_PATH), derivative["patch_sha256"])
    require_equal("NOTICE SHA-256", sha256_file(NOTICE_PATH), derivative["notice_sha256"])


def download_archive(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "q-fintelligence-supply-chain-rebuilder/1"},
    )
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as out:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise BuildError("upstream archive exceeded the 100 MiB safety limit")
            out.write(chunk)


def safe_extract_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise BuildError("upstream archive is empty")
        roots: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise BuildError(f"unsafe archive member: {member.name}")
            if member.issym() or member.islnk():
                raise BuildError(f"archive links are not accepted: {member.name}")
            roots.add(pure.parts[0])
        require_equal("archive root count", len(roots), 1)
        bundle.extractall(destination, filter="data")
    root = destination / next(iter(roots))
    if not root.is_dir():
        raise BuildError("archive root was not extracted as a directory")
    return root


def verify_git_source(source: Path, lock: dict[str, Any]) -> None:
    if not (source / ".git").exists():
        return
    upstream = lock["upstream"]
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    require_equal("upstream Git commit", head, upstream["commit"])
    require_equal("upstream Git tree", tree, upstream["git_tree"])


def copy_source(source: Path, destination: Path) -> Path:
    project = source / "openhands-sdk"
    license_path = source / "LICENSE"
    if not (project / "pyproject.toml").is_file() or not license_path.is_file():
        raise BuildError("source root does not contain openhands-sdk/pyproject.toml and LICENSE")
    destination.mkdir(parents=True)
    shutil.copytree(project, destination / "openhands-sdk")
    shutil.copy2(license_path, destination / "LICENSE")
    return destination


def verify_upstream(source: Path, lock: dict[str, Any]) -> None:
    upstream = lock["upstream"]
    project = source / "openhands-sdk"
    sdk = project / "openhands" / "sdk"
    require_equal(
        "upstream pyproject SHA-256",
        sha256_file(project / "pyproject.toml"),
        upstream["sdk_project_pyproject_sha256"],
    )
    require_equal(
        "upstream LICENSE SHA-256",
        sha256_file(source / "LICENSE"),
        upstream["license_sha256"],
    )
    source_files = file_map(sdk)
    require_equal("upstream SDK file count", len(source_files), upstream["sdk_source_file_count"])
    require_equal(
        "upstream SDK tree SHA-256",
        aggregate_tree_digest(source_files),
        upstream["sdk_source_tree_sha256"],
    )
    python_files = {name: value for name, value in source_files.items() if name.endswith(".py")}
    require_equal(
        "upstream SDK Python file count",
        len(python_files),
        upstream["sdk_python_file_count"],
    )
    require_equal(
        "upstream SDK Python tree SHA-256",
        aggregate_tree_digest(python_files),
        upstream["sdk_python_tree_sha256"],
    )


def apply_derivative_patch(source: Path, lock: dict[str, Any]) -> dict[str, Any]:
    project = source / "openhands-sdk"
    subprocess.run(
        [
            "patch",
            "--batch",
            "--forward",
            "--fuzz=0",
            "-p1",
            "--input",
            str(PATCH_PATH),
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.copy2(source / "LICENSE", project / "LICENSE")
    shutil.copy2(NOTICE_PATH, project / "NOTICE")

    derivative = lock["derivative"]
    pyproject_path = project / "pyproject.toml"
    require_equal(
        "patched pyproject SHA-256",
        sha256_file(pyproject_path),
        derivative["patched_pyproject_sha256"],
    )
    with pyproject_path.open("rb") as handle:
        pyproject = tomllib.load(handle)
    project_metadata = pyproject["project"]
    require_equal("derivative name", project_metadata["name"], derivative["distribution"])
    require_equal("derivative version", project_metadata["version"], derivative["version"])
    require_equal("derivative license", project_metadata["license"], "MIT")
    require_equal(
        "derivative license files",
        project_metadata["license-files"],
        ["LICENSE", "NOTICE"],
    )
    if any(
        requirement.lower().startswith("lmnr")
        for requirement in project_metadata["dependencies"]
    ):
        raise BuildError("patched project still contains an lmnr dependency")
    return pyproject


def build_metadata(pyproject: dict[str, Any]) -> bytes:
    project = pyproject["project"]
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
        f"Summary: {project['description']}",
        f"License-Expression: {project['license']}",
    ]
    lines.extend(f"Project-URL: {name}, {url}" for name, url in project["urls"].items())
    lines.append(f"Requires-Python: {project['requires-python']}")
    lines.extend(f"License-File: {name}" for name in project["license-files"])
    lines.extend(f"Requires-Dist: {requirement}" for requirement in project["dependencies"])
    for extra, requirements in project["optional-dependencies"].items():
        lines.append(f"Provides-Extra: {extra}")
        lines.extend(
            f'Requires-Dist: {requirement}; extra == "{extra}"'
            for requirement in requirements
        )
    lines.append("Dynamic: license-file")
    return ("\n".join(lines) + "\n").encode("utf-8")


def runtime_entries(source: Path, lock: dict[str, Any]) -> list[tuple[str, bytes]]:
    sdk = source / "openhands-sdk" / "openhands" / "sdk"
    entries: list[tuple[str, bytes]] = []
    runtime_hashes: dict[str, str] = {}
    for directory, directory_names, file_names in os.walk(sdk):
        directory_names.sort()
        file_names.sort()
        directory_path = Path(directory)
        for file_name in file_names:
            path = directory_path / file_name
            relative = path.relative_to(sdk).as_posix()
            if not (relative.endswith((".py", ".j2")) or relative == "py.typed"):
                continue
            data = path.read_bytes()
            entries.append((f"openhands/sdk/{relative}", data))
            runtime_hashes[relative] = sha256_bytes(data)
    derivative = lock["derivative"]
    require_equal("wheel runtime file count", len(entries), derivative["runtime_file_count"])
    require_equal(
        "wheel runtime tree SHA-256",
        aggregate_tree_digest(runtime_hashes),
        derivative["runtime_tree_sha256"],
    )
    return entries


def record_bytes(entries: list[tuple[str, bytes]], record_name: str) -> bytes:
    lines: list[str] = []
    for name, data in entries:
        encoded = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .decode("ascii")
            .rstrip("=")
        )
        lines.append(f"{name},sha256={encoded},{len(data)}\n")
    lines.append(f"{record_name},,\n")
    return "".join(lines).encode("utf-8")


def zip_info(name: str, timestamp: tuple[int, ...], mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, timestamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.external_attr = mode << 16
    return info


def build_wheel(
    source: Path,
    output: Path,
    pyproject: dict[str, Any],
    lock: dict[str, Any],
) -> None:
    derivative = lock["derivative"]
    canonical = lock["canonical_wheel"]
    metadata = build_metadata(pyproject)
    require_equal("METADATA SHA-256", sha256_bytes(metadata), derivative["metadata_sha256"])

    wheel_metadata = (
        b"Wheel-Version: 1.0\n"
        b"Generator: setuptools (83.0.0)\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n\n"
    )
    top_level = b"openhands\n"
    require_equal(
        "WHEEL metadata SHA-256",
        sha256_bytes(wheel_metadata),
        derivative["wheel_metadata_sha256"],
    )
    require_equal("top_level SHA-256", sha256_bytes(top_level), derivative["top_level_sha256"])

    dist_info = "openhands_sdk-1.39.0+qf.noobservability.1.dist-info"
    entries = runtime_entries(source, lock)
    entries.extend(
        [
            (f"{dist_info}/licenses/LICENSE", (source / "LICENSE").read_bytes()),
            (f"{dist_info}/licenses/NOTICE", NOTICE_PATH.read_bytes()),
            (f"{dist_info}/METADATA", metadata),
            (f"{dist_info}/WHEEL", wheel_metadata),
            (f"{dist_info}/top_level.txt", top_level),
        ]
    )
    record_name = f"{dist_info}/RECORD"
    record = record_bytes(entries, record_name)

    runtime_timestamp = tuple(canonical["runtime_zip_timestamp"])
    metadata_timestamp = tuple(canonical["metadata_zip_timestamp"])
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            runtime_count = derivative["runtime_file_count"]
            for index, (name, data) in enumerate(entries):
                timestamp = runtime_timestamp if index < runtime_count else metadata_timestamp
                archive.writestr(zip_info(name, timestamp, 0o100644), data)
            archive.writestr(zip_info(record_name, metadata_timestamp, 0o100664), record)
        require_equal(
            "rebuilt wheel SHA-256",
            sha256_file(temporary),
            derivative["wheel_sha256"],
        )
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild the pinned QF no-observability OpenHands SDK wheel."
    )
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument(
        "--source-dir",
        type=Path,
        help="Existing official software-agent-sdk checkout at the pinned commit.",
    )
    inputs.add_argument(
        "--archive",
        type=Path,
        help="Existing codeload archive; its SHA-256 is always verified.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            SUPPLY_CHAIN_DIR
            / "dist"
            / "openhands_sdk-1.39.0+qf.noobservability.1-py3-none-any.whl"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output after the rebuilt SHA-256 passes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock = read_lock()
    require_equal("Python version", sys.version.split()[0], lock["toolchain"]["python"])
    require_equal("zlib version", zlib.ZLIB_RUNTIME_VERSION, lock["toolchain"]["zlib"])
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise BuildError(f"output already exists (use --force): {output}")

    verify_recipe_files(lock)
    upstream = lock["upstream"]
    with tempfile.TemporaryDirectory(prefix="qf-openhands-rebuild-") as temporary_name:
        temporary = Path(temporary_name)
        if args.source_dir is not None:
            supplied_source = args.source_dir.resolve()
            verify_git_source(supplied_source, lock)
        else:
            archive = (
                args.archive.resolve()
                if args.archive is not None
                else temporary / "source.tar.gz"
            )
            if args.archive is None:
                download_archive(upstream["archive_url"], archive)
            require_equal(
                "upstream archive SHA-256",
                sha256_file(archive),
                upstream["archive_sha256"],
            )
            supplied_source = safe_extract_archive(archive, temporary / "archive")
        source = copy_source(supplied_source, temporary / "source")
        verify_upstream(source, lock)
        pyproject = apply_derivative_patch(source, lock)
        build_wheel(source, output, pyproject, lock)

    print(
        json.dumps(
            {
                "result": "PASS",
                "output": str(output),
                "version": lock["derivative"]["version"],
                "sha256": sha256_file(output),
                "upstream_commit": upstream["commit"],
                "upstream_git_tree": upstream["git_tree"],
                "observability_dependency_removed": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"rebuild failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
