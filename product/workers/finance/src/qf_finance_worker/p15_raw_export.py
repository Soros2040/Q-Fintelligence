from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


def _write_table(rows: list[dict[str, Any]], target: Path) -> None:
    frame = pd.DataFrame.from_records(rows)
    frame.to_parquet(target, index=False, compression="zstd")


def main() -> int:
    if len(sys.argv) != 3:
        raise ValueError("usage: p15_raw_export BUNDLE_JSON OUTPUT_DIR")
    bundle_path = Path(sys.argv[1]).resolve(strict=True)
    output_dir = Path(sys.argv[2]).resolve(strict=True)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    daily = bundle.get("daily")
    factors = bundle.get("adjustmentFactors")
    if not isinstance(daily, list) or not isinstance(factors, list):
        raise ValueError("raw Tushare bundle is missing daily or adjustmentFactors")
    _write_table(daily, output_dir / "tushare-six-stock-daily-raw.parquet")
    _write_table(factors, output_dir / "tushare-six-stock-adj-factor-raw.parquet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
