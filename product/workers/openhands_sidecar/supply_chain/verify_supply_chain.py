from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

SUPPLY_CHAIN_DIR = Path(__file__).resolve().parent
DEFAULT_SIDECAR_DIR = SUPPLY_CHAIN_DIR.parent
BANNED_DISTRIBUTIONS = {
    "claude-agent-sdk",
    "lmnr",
    "lmnr-claude-code-proxy",
    "opentelemetry-exporter-otlp",
    "opentelemetry-instrumentation",
    "opentelemetry-sdk",
}
OFFICIAL_OPENHANDS_PACKAGES = {
    "openhands-agent-server": "1.39.0",
    "openhands-tools": "1.39.0",
    "openhands-workspace": "1.39.0",
}
BANNED_DISTRIBUTION_PREFIXES = (
    "opentelemetry-exporter-",
    "opentelemetry-instrumentation-",
)


class VerificationError(RuntimeError):
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
        raise VerificationError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def aggregate_tree_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative_path, file_hash in sorted(files.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_hash))
    return digest.hexdigest()


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def verify_gitattributes(repository_root: Path) -> None:
    attributes_path = repository_root / ".gitattributes"
    lines = {
        line.strip()
        for line in attributes_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    expected = "workers/openhands_sidecar/vendor/*.whl binary"
    if expected not in lines:
        raise VerificationError(f"missing wheel binary attribute: {expected}")


def verify_project(sidecar: Path, lock: dict[str, Any]) -> None:
    project = load_toml(sidecar / "pyproject.toml")
    derivative = lock["derivative"]
    expected_requirement = f"openhands-sdk=={derivative['version']}"
    dependencies = project["project"]["dependencies"]
    if expected_requirement not in dependencies:
        raise VerificationError(f"missing exact dependency: {expected_requirement}")
    for distribution, version in OFFICIAL_OPENHANDS_PACKAGES.items():
        expected = f"{distribution}=={version}"
        if expected not in dependencies:
            raise VerificationError(f"missing exact dependency: {expected}")
    source = project["tool"]["uv"]["sources"]["openhands-sdk"]
    require_equal(
        "pyproject wheel source",
        source,
        {"path": f"vendor/{derivative['wheel_filename']}"},
    )


def verify_lock(sidecar: Path, lock: dict[str, Any]) -> int:
    uv_lock = load_toml(sidecar / "uv.lock")
    packages = uv_lock["package"]
    derivative = lock["derivative"]
    package = next(
        (item for item in packages if item["name"] == derivative["distribution"]),
        None,
    )
    if package is None:
        raise VerificationError("uv.lock does not contain openhands-sdk")
    require_equal("uv.lock derivative version", package["version"], derivative["version"])
    require_equal(
        "uv.lock derivative source",
        package["source"],
        {"path": f"vendor/{derivative['wheel_filename']}"},
    )
    require_equal(
        "uv.lock derivative wheel",
        package["wheels"],
        [
            {
                "filename": derivative["wheel_filename"],
                "hash": f"sha256:{derivative['wheel_sha256']}",
            }
        ],
    )

    root_package = next(
        (item for item in packages if item["name"] == "qf-openhands-sidecar"),
        None,
    )
    if root_package is None:
        raise VerificationError("uv.lock does not contain qf-openhands-sidecar")
    root_requirement = next(
        (
            item
            for item in root_package["metadata"]["requires-dist"]
            if item["name"] == derivative["distribution"]
        ),
        None,
    )
    require_equal(
        "root lock derivative path",
        root_requirement,
        {
            "name": derivative["distribution"],
            "path": f"vendor/{derivative['wheel_filename']}",
        },
    )

    package_names = {item["name"] for item in packages}
    for distribution, version in OFFICIAL_OPENHANDS_PACKAGES.items():
        official = next(
            (item for item in packages if item["name"] == distribution),
            None,
        )
        if official is None:
            raise VerificationError(f"uv.lock does not contain {distribution}")
        require_equal(f"{distribution} version", official["version"], version)
        require_equal(
            f"{distribution} source",
            official["source"],
            {"registry": "https://pypi.org/simple"},
        )
    banned = sorted(
        name
        for name in package_names
        if name in BANNED_DISTRIBUTIONS
        or name.startswith(BANNED_DISTRIBUTION_PREFIXES)
    )
    require_equal("banned locked distributions", banned, [])
    return len(packages)


def expected_record_hash(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return "sha256=" + encoded.decode("ascii").rstrip("=")


def verify_wheel(sidecar: Path, lock: dict[str, Any]) -> dict[str, int]:
    derivative = lock["derivative"]
    wheel_path = sidecar / "vendor" / derivative["wheel_filename"]
    require_equal(
        "vendored wheel SHA-256",
        sha256_file(wheel_path),
        derivative["wheel_sha256"],
    )
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()
        require_equal("wheel member uniqueness", len(names), len(set(names)))
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
            raise VerificationError("wheel contains an unsafe path")
        files = {name: archive.read(name) for name in names if not name.endswith("/")}

    metadata_name = next(name for name in files if name.endswith(".dist-info/METADATA"))
    wheel_metadata_name = next(name for name in files if name.endswith(".dist-info/WHEEL"))
    top_level_name = next(name for name in files if name.endswith(".dist-info/top_level.txt"))
    record_name = next(name for name in files if name.endswith(".dist-info/RECORD"))
    notice_name = next(
        name for name in files if name.endswith(".dist-info/licenses/NOTICE")
    )
    license_name = next(
        name for name in files if name.endswith(".dist-info/licenses/LICENSE")
    )

    require_equal(
        "wheel METADATA SHA-256",
        sha256_bytes(files[metadata_name]),
        derivative["metadata_sha256"],
    )
    require_equal(
        "wheel WHEEL SHA-256",
        sha256_bytes(files[wheel_metadata_name]),
        derivative["wheel_metadata_sha256"],
    )
    require_equal(
        "wheel top_level SHA-256",
        sha256_bytes(files[top_level_name]),
        derivative["top_level_sha256"],
    )
    require_equal(
        "wheel NOTICE SHA-256",
        sha256_bytes(files[notice_name]),
        derivative["notice_sha256"],
    )
    require_equal(
        "wheel LICENSE SHA-256",
        sha256_bytes(files[license_name]),
        lock["upstream"]["license_sha256"],
    )

    metadata = BytesParser().parsebytes(files[metadata_name])
    require_equal("wheel name", metadata["Name"], derivative["distribution"])
    require_equal("wheel version", metadata["Version"], derivative["version"])
    require_equal("wheel Python requirement", metadata["Requires-Python"], ">=3.12")
    requirements = metadata.get_all("Requires-Dist", [])
    if any(requirement.lower().startswith("lmnr") for requirement in requirements):
        raise VerificationError("wheel METADATA contains an lmnr requirement")

    record_rows = list(
        csv.reader(io.StringIO(files[record_name].decode("utf-8")))
    )
    require_equal("wheel RECORD row count", len(record_rows), len(files))
    for name, encoded_hash, encoded_size in record_rows:
        if name == record_name:
            require_equal("RECORD self hash", encoded_hash, "")
            require_equal("RECORD self size", encoded_size, "")
            continue
        if name not in files:
            raise VerificationError(f"RECORD references a missing member: {name}")
        require_equal(f"RECORD hash for {name}", encoded_hash, expected_record_hash(files[name]))
        require_equal(f"RECORD size for {name}", encoded_size, str(len(files[name])))

    runtime = {
        name.removeprefix("openhands/sdk/"): sha256_bytes(data)
        for name, data in files.items()
        if name.startswith("openhands/sdk/")
    }
    require_equal("wheel runtime file count", len(runtime), derivative["runtime_file_count"])
    require_equal(
        "wheel runtime tree SHA-256",
        aggregate_tree_digest(runtime),
        derivative["runtime_tree_sha256"],
    )
    return {"wheel_files": len(files), "runtime_files": len(runtime)}


def verify_recipe(sidecar: Path, lock: dict[str, Any]) -> None:
    supply_chain = sidecar / "supply_chain"
    required = [
        "NOTICE",
        "README.md",
        "openhands-sdk-noobservability.patch",
        "rebuild-wheel.sh",
        "rebuild_wheel.py",
        "source-lock.json",
        "verify_supply_chain.py",
    ]
    missing = [name for name in required if not (supply_chain / name).is_file()]
    require_equal("rebuild recipe missing files", missing, [])
    require_equal(
        "recipe patch SHA-256",
        sha256_file(supply_chain / "openhands-sdk-noobservability.patch"),
        lock["derivative"]["patch_sha256"],
    )
    require_equal(
        "recipe NOTICE SHA-256",
        sha256_file(supply_chain / "NOTICE"),
        lock["derivative"]["notice_sha256"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify fresh-checkout OpenHands sidecar supply-chain inputs."
    )
    parser.add_argument("--sidecar-dir", type=Path, default=DEFAULT_SIDECAR_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sidecar = args.sidecar_dir.resolve()
    repository_root = sidecar.parents[1]
    lock = json.loads(
        (sidecar / "supply_chain" / "source-lock.json").read_text(encoding="utf-8")
    )
    require_equal("source lock schema", lock.get("schema_version"), 1)
    verify_gitattributes(repository_root)
    verify_project(sidecar, lock)
    package_count = verify_lock(sidecar, lock)
    wheel_counts = verify_wheel(sidecar, lock)
    verify_recipe(sidecar, lock)
    print(
        json.dumps(
            {
                "result": "PASS",
                "fresh_checkout_inputs": True,
                "python": lock["toolchain"]["python"],
                "uv": lock["toolchain"]["uv"],
                "version": lock["derivative"]["version"],
                "wheel_sha256": lock["derivative"]["wheel_sha256"],
                "locked_packages": package_count,
                **wheel_counts,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, VerificationError, ValueError, zipfile.BadZipFile) as error:
        print(f"supply-chain verification failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
