from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SIDECAR_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SIDECAR_DIR.parents[1]


class SupplyChainTests(unittest.TestCase):
    def make_fresh_checkout(self, destination: Path) -> Path:
        repository = destination / "q-fintelligence"
        sidecar = repository / "workers" / "openhands_sidecar"
        (sidecar / "vendor").mkdir(parents=True)
        shutil.copy2(REPOSITORY_ROOT / ".gitattributes", repository / ".gitattributes")
        for name in ("bootstrap-frozen.sh", "pyproject.toml", "uv.lock"):
            shutil.copy2(SIDECAR_DIR / name, sidecar / name)
        wheel = "openhands_sdk-1.39.0+qf.noobservability.1-py3-none-any.whl"
        shutil.copy2(SIDECAR_DIR / "vendor" / wheel, sidecar / "vendor" / wheel)
        shutil.copytree(SIDECAR_DIR / "supply_chain", sidecar / "supply_chain")
        self.assertFalse((sidecar / ".venv").exists())
        return sidecar

    def test_fresh_checkout_inputs_pass_without_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sidecar = self.make_fresh_checkout(Path(directory))
            result = subprocess.run(
                ["bash", str(sidecar / "bootstrap-frozen.sh"), "--verify-only"],
                cwd=sidecar,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"fresh_checkout_inputs": true', result.stdout)
            self.assertIn("fresh-checkout inputs: PASS", result.stdout)
            self.assertFalse((sidecar / ".venv").exists())

    def test_fresh_checkout_rejects_corrupted_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sidecar = self.make_fresh_checkout(Path(directory))
            lock = json.loads(
                (sidecar / "supply_chain" / "source-lock.json").read_text(
                    encoding="utf-8"
                )
            )
            wheel = sidecar / "vendor" / lock["derivative"]["wheel_filename"]
            with wheel.open("r+b") as handle:
                handle.seek(-1, 2)
                final_byte = handle.read(1)
                handle.seek(-1, 2)
                handle.write(bytes([final_byte[0] ^ 0x01]))
            result = subprocess.run(
                [
                    "/usr/bin/python3",
                    str(sidecar / "supply_chain" / "verify_supply_chain.py"),
                    "--sidecar-dir",
                    str(sidecar),
                ],
                cwd=sidecar,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("vendored wheel SHA-256 mismatch", result.stderr)

    @unittest.skipUnless(
        os.environ.get("QF_RUN_FRESH_CHECKOUT_INSTALL") == "1",
        "set QF_RUN_FRESH_CHECKOUT_INSTALL=1 for the cached/offline integration test",
    )
    def test_fresh_checkout_performs_offline_frozen_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sidecar = self.make_fresh_checkout(Path(directory))
            result = subprocess.run(
                ["bash", str(sidecar / "bootstrap-frozen.sh"), "--offline"],
                cwd=sidecar,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("frozen bootstrap: PASS", result.stdout)
            python = sidecar / ".venv" / "bin" / "python"
            self.assertTrue(python.is_symlink())
            version = subprocess.check_output(
                [
                    str(python),
                    "-I",
                    "-c",
                    "from importlib.metadata import version; print(version('openhands-sdk'))",
                ],
                text=True,
            ).strip()
            self.assertEqual(version, "1.39.0+qf.noobservability.1")

    def test_bootstrap_is_frozen_to_approved_python_and_uv(self) -> None:
        script = (SIDECAR_DIR / "bootstrap-frozen.sh").read_text(encoding="utf-8")
        for assertion in (
            'EXPECTED_UV_VERSION="uv 0.11.29 (x86_64-unknown-linux-gnu)"',
            'PYTHON_REQUEST="3.12.13"',
            "--managed-python",
            "--frozen",
            "--no-dev",
            '"${UV_BIN}" sync',
        ):
            self.assertIn(assertion, script)

    def test_rebuild_lock_pins_upstream_and_approved_output(self) -> None:
        supply_chain = SIDECAR_DIR / "supply_chain"
        lock = json.loads((supply_chain / "source-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(
            lock["upstream"]["commit"],
            "54dfbc551408d10de54eb8ac5612bae6d3f99d16",
        )
        self.assertEqual(
            lock["derivative"]["wheel_sha256"],
            "9818384aa3393524ed05574fba041009a91f6e2ce35db77d86cd1a600a6e70e3",
        )
        patch = (supply_chain / "openhands-sdk-noobservability.patch").read_text(
            encoding="utf-8"
        )
        self.assertIn('version = "1.39.0+qf.noobservability.1"', patch)
        self.assertIn('-    "lmnr>=0.7.56,<0.8.0",', patch)
        self.assertNotIn("openhands/sdk/", patch)


if __name__ == "__main__":
    unittest.main()
