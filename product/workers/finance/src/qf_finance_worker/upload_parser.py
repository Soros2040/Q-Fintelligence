from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import sys
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

import filetype
import h5py
import numpy as np
import openpyxl
import pandas as pd
import pandera.pandas as pa
import pyarrow as arrow
import pyarrow.parquet as pq
from pypdf import PdfReader

MAX_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_RATIO = 100
MAX_PREVIEW_ROWS = 12
MAX_TEXT_CHARS = 60_000

ALLOWED_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".xlsx",
    ".json",
    ".jsonl",
    ".parquet",
    ".npy",
    ".npz",
    ".h5",
    ".hdf5",
    ".txt",
    ".md",
    ".pdf",
    ".qasm",
    ".qcis",
}

INJECTION_PATTERNS = {
    "system_override": re.compile(
        r"(ignore|disregard|override|replace).{0,40}(system|developer|previous|instruction)",
        re.IGNORECASE | re.DOTALL,
    ),
    "model_override": re.compile(
        r"(switch|change|use).{0,30}(model|provider)|gpt-[0-9]|tianyan_sw",
        re.IGNORECASE | re.DOTALL,
    ),
    "capability_escalation": re.compile(
        r"(grant|enable|allow|approve|unlock|bypass).{0,40}"
        r"(shell|tool|permission|secret|key|token|hardware|test)",
        re.IGNORECASE | re.DOTALL,
    ),
    "secret_exfiltration": re.compile(
        r"(print|read|reveal|exfiltrate|send).{0,40}"
        r"(api.?key|connection.?key|token|\.env|authorization)",
        re.IGNORECASE | re.DOTALL,
    ),
    "formal_test_unseal": re.compile(
        r"(unseal|unlock|open|reveal).{0,30}(formal|official|2024).{0,20}(test|metric)",
        re.IGNORECASE | re.DOTALL,
    ),
}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _resolve_inside(root: Path, candidate: str) -> Path:
    path = Path(candidate).resolve(strict=True)
    if path != root and root not in path.parents:
        raise ValueError("upload path escaped the P05 workspace")
    return path


def _decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)[:MAX_TEXT_CHARS], encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("text encoding could not be decoded")


def _normalized_variants(text: str) -> list[tuple[str, str]]:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", normalized)
    variants = [("normalized", normalized)]
    compact = re.sub(r"\s+", "", normalized)
    for label, pattern, decoder in (
        ("base64", r"(?:[A-Za-z0-9+/]{32,}={0,2})", base64.b64decode),
        ("hex", r"(?:[0-9A-Fa-f]{40,})", bytes.fromhex),
    ):
        for match in re.findall(pattern, compact)[:20]:
            try:
                decoded = decoder(match).decode("utf-8", errors="ignore")
            except (ValueError, TypeError):
                continue
            if decoded:
                variants.append((label, unicodedata.normalize("NFKC", decoded)))
    return variants


def _scan_injection(sources: list[tuple[str, str]]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    for source, text in sources:
        for encoding, variant in _normalized_variants(text):
            for rule, pattern in INJECTION_PATTERNS.items():
                match = pattern.search(variant)
                if match:
                    findings.append(
                        {
                            "source": source,
                            "encoding": encoding,
                            "rule": rule,
                            "evidence_sha256": hashlib.sha256(
                                match.group(0).encode("utf-8")
                            ).hexdigest(),
                        }
                    )
    rules = sorted({finding["rule"] for finding in findings})
    risk = "HIGH" if rules else "LOW"
    return {
        "route": "QUARANTINE" if risk == "HIGH" else "UNTRUSTED_DATA",
        "risk_level": risk,
        "quarantined": risk == "HIGH",
        "rules": rules,
        "findings": findings[:100],
        "capabilities_granted": [],
        "system_prompt_mutated": False,
        "model_mutated": False,
        "formal_test_unsealed": False,
    }


def _archive_gate(path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(path):
        return {"archive": False}
    with zipfile.ZipFile(path) as archive:
        expanded = sum(item.file_size for item in archive.infolist())
        compressed = max(1, sum(item.compress_size for item in archive.infolist()))
        ratio = expanded / compressed
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES or ratio > MAX_ARCHIVE_RATIO:
            raise ValueError("compressed upload exceeded expansion safety gate")
        return {
            "archive": True,
            "members": len(archive.infolist()),
            "expanded_bytes": expanded,
            "compression_ratio": ratio,
        }


def _dataframe_quality(frame: pd.DataFrame) -> dict[str, Any]:
    schema = pa.DataFrameSchema(
        checks=[pa.Check(lambda value: len(value.columns) > 0, error="no columns")],
        strict=False,
    )
    validated = schema.validate(frame, lazy=True)
    missing = {str(column): int(validated[column].isna().sum()) for column in validated.columns}
    comparable = validated.map(
        lambda value: json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if isinstance(value, (dict, list, tuple, set))
        else value
    )
    return {
        "third_party_validator": "pandera==0.32.1",
        "rows": int(len(validated)),
        "columns": [str(column) for column in validated.columns],
        "missing": missing,
        "duplicate_rows": int(comparable.duplicated().sum()),
        "dtypes": {str(column): str(validated[column].dtype) for column in validated.columns},
        "preview": _json_safe(validated.head(MAX_PREVIEW_ROWS).to_dict(orient="records")),
    }


def _parse_tabular(path: Path, extension: str) -> tuple[str, dict[str, Any], list[tuple[str, str]]]:
    if extension in {".csv", ".tsv"}:
        frame = pd.read_csv(path, sep="\t" if extension == ".tsv" else ",", nrows=100_000)
        parser = "pandas.read_csv+pandera"
        hidden: list[str] = []
    else:
        workbook = openpyxl.load_workbook(path, read_only=False, data_only=True)
        hidden = [sheet.title for sheet in workbook.worksheets if sheet.sheet_state != "visible"]
        rows: list[list[Any]] = []
        sources: list[tuple[str, str]] = []
        for sheet in workbook.worksheets:
            values = list(sheet.iter_rows(values_only=True, max_row=1000, max_col=200))
            sources.append(
                (
                    f"xlsx-sheet:{sheet.title}:{sheet.sheet_state}",
                    json.dumps(_json_safe(values), ensure_ascii=False),
                )
            )
            if sheet.sheet_state == "visible" and values and not rows:
                rows = [list(row) for row in values]
        if rows:
            headers = [
                str(value) if value is not None else f"column_{index}"
                for index, value in enumerate(rows[0])
            ]
            frame = pd.DataFrame(rows[1:], columns=headers)
        else:
            frame = pd.DataFrame({"empty": []})
        result = _dataframe_quality(frame)
        result["hidden_sheets"] = hidden
        result["sheet_count"] = len(workbook.worksheets)
        return "openpyxl+pandera", result, sources
    quality = _dataframe_quality(frame)
    sources = [
        (
            "tabular-cells",
            json.dumps(_json_safe(frame.head(500).to_dict(orient="records")), ensure_ascii=False),
        )
    ]
    quality["hidden_sheets"] = hidden
    return parser, quality, sources


def _parse_structured(
    path: Path, extension: str
) -> tuple[str, dict[str, Any], list[tuple[str, str]]]:
    if extension == ".parquet":
        arrow.set_cpu_count(1)
        arrow.set_io_thread_count(1)
        table = pq.read_table(path, use_threads=False)
        frame = table.slice(0, min(table.num_rows, 100_000)).to_pandas(use_threads=False)
        payload = _dataframe_quality(frame)
        payload["parquet_schema"] = str(table.schema)
        return (
            "pyarrow.parquet+pandera",
            payload,
            [("parquet-values", json.dumps(_json_safe(payload["preview"]), ensure_ascii=False))],
        )
    if extension == ".jsonl":
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        data: Any = records
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.json_normalize(data if isinstance(data, list) else [data])
    return (
        "json+pandas+pandera",
        _dataframe_quality(frame),
        [("json-values", json.dumps(_json_safe(data), ensure_ascii=False)[:MAX_TEXT_CHARS])],
    )


def _parse_array(path: Path, extension: str) -> tuple[str, dict[str, Any], list[tuple[str, str]]]:
    if extension == ".npy":
        array = np.load(path, allow_pickle=False)
        result = {
            "arrays": [{"name": "array", "shape": list(array.shape), "dtype": str(array.dtype)}],
            "preview": _json_safe(array.reshape(-1)[:100].tolist()),
        }
        return (
            "numpy.load(allow_pickle=False)",
            result,
            [("npy-preview", json.dumps(result["preview"]))],
        )
    if extension == ".npz":
        with np.load(path, allow_pickle=False) as archive:
            arrays = [
                {
                    "name": name,
                    "shape": list(archive[name].shape),
                    "dtype": str(archive[name].dtype),
                }
                for name in archive.files
            ]
            preview = {
                name: _json_safe(archive[name].reshape(-1)[:50].tolist()) for name in archive.files
            }
        return (
            "numpy.load(allow_pickle=False)",
            {"arrays": arrays, "preview": preview},
            [("npz-preview", json.dumps(preview))],
        )
    datasets: list[dict[str, Any]] = []
    previews: dict[str, Any] = {}
    with h5py.File(path, "r") as handle:

        def visit(name: str, value: Any) -> None:
            if isinstance(value, h5py.Dataset):
                datasets.append(
                    {"name": name, "shape": list(value.shape), "dtype": str(value.dtype)}
                )
                if value.size <= 10_000:
                    previews[name] = _json_safe(np.asarray(value).reshape(-1)[:50].tolist())

        handle.visititems(visit)
    return (
        "h5py",
        {"datasets": datasets, "preview": previews},
        [("hdf5-preview", json.dumps(previews, ensure_ascii=False))],
    )


def _parse_document(
    path: Path, extension: str
) -> tuple[str, dict[str, Any], list[tuple[str, str]]]:
    if extension == ".pdf":
        reader = PdfReader(path)
        metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:50])[:MAX_TEXT_CHARS]
        return (
            "pypdf",
            {
                "pages": len(reader.pages),
                "metadata_keys": sorted(metadata),
                "text_preview": text[:4000],
            },
            [("pdf-text", text), ("pdf-metadata", json.dumps(metadata, ensure_ascii=False))],
        )
    text, encoding = _decode_text(path.read_bytes())
    parser = {
        ".qasm": "openqasm-text-safe-parser",
        ".qcis": "qcis-text-safe-parser",
        ".md": "markdown-text-safe-parser",
    }.get(extension, "bounded-text-parser")
    return (
        parser,
        {"encoding": encoding, "characters": len(text), "text_preview": text[:4000]},
        [("document-text", text)],
    )


def parse_upload(request: dict[str, Any]) -> dict[str, Any]:
    root = Path(str(request["workspace_root"])).resolve(strict=True)
    path = _resolve_inside(root, str(request["path"]))
    file_name = str(request["file_name"])
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("upload extension is not registered")
    byte_size = path.stat().st_size
    if byte_size > MAX_BYTES:
        raise ValueError("upload exceeded the 16 MiB size gate")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != request.get("sha256"):
        raise ValueError("upload content hash mismatch")
    archive = _archive_gate(path)
    guess = filetype.guess(data[:8192])
    detected = (
        guess.mime if guess else mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    )

    if extension in {".csv", ".tsv", ".xlsx"}:
        parser, content, sources = _parse_tabular(path, extension)
    elif extension in {".json", ".jsonl", ".parquet"}:
        parser, content, sources = _parse_structured(path, extension)
    elif extension in {".npy", ".npz", ".h5", ".hdf5"}:
        parser, content, sources = _parse_array(path, extension)
    else:
        parser, content, sources = _parse_document(path, extension)
    security = _scan_injection([("file-name", file_name), *sources])
    return {
        "schema_version": "qf.p05.upload-parse.v1",
        "status": "QUARANTINED" if security["quarantined"] else "PARSED",
        "file_name": file_name,
        "extension": extension,
        "sha256": digest,
        "byte_size": byte_size,
        "declared_media_type": str(
            request.get("declared_media_type") or "application/octet-stream"
        ),
        "detected_media_type": detected,
        "parser": parser,
        "archive_gate": archive,
        "content": content,
        "security": security,
        "untrusted_data_only": True,
    }


def main() -> int:
    request = json.load(sys.stdin)
    if request.get("action") != "parse":
        raise ValueError("upload parser action is not registered")
    json.dump(_json_safe(parse_upload(request)), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - bounded worker envelope.
        json.dump(
            {
                "schema_version": "qf.p05.upload-error.v1",
                "status": "FAILED",
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            },
            sys.stdout,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        raise SystemExit(1) from None
