import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";

import { TushareAdapter, type TushareDatasetBundle } from "../apps/control-plane/src/campaign/tushare-adapter.js";

type RawRow = TushareDatasetBundle["daily"][number];

const projectRoot = path.resolve(import.meta.dirname, "..");
const cacheRoot = path.join(projectRoot, ".local", "market-cache", "tushare");
const token = process.env.TUSHARE_TOKEN ?? "";
if (!token.trim()) throw new Error("TUSHARE_TOKEN is missing");

function compactTimestamp(value: Date): string {
  return value.toISOString().replace(/[-:]/gu, "").replace(/\.\d{3}Z$/u, "Z");
}

function csvCell(value: string | number | null): string {
  if (value === null) return "";
  const text = String(value);
  return /[",\r\n]/u.test(text) ? `"${text.replaceAll("\"", "\"\"")}"` : text;
}

function encodeCsv(rows: RawRow[]): string {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].sort();
  return [
    columns.join(","),
    ...rows.map((row) => columns.map((column) => csvCell(row[column] ?? null)).join(",")),
  ].join("\n").concat("\n");
}

async function sha256File(filePath: string): Promise<string> {
  return createHash("sha256").update(await readFile(filePath)).digest("hex");
}

async function convertParquet(bundlePath: string, outputDir: string): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(
      path.join(projectRoot, ".venv", "bin", "python"),
      ["-m", "qf_finance_worker.p15_raw_export", bundlePath, outputDir],
      {
        cwd: projectRoot,
        env: {
          PATH: process.env.PATH ?? "",
          PYTHONPATH: path.join(projectRoot, "workers", "finance", "src"),
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => { stderr = `${stderr}${chunk}`.slice(-4_000); });
    child.stdout.resume();
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`Parquet export exited ${String(code)}: ${stderr}`));
    });
  });
}

const acquiredAt = new Date();
const outputDir = path.join(projectRoot, ".local", "p15-input", compactTimestamp(acquiredAt));
await mkdir(outputDir, { recursive: true });

const adapter = new TushareAdapter(token, cacheRoot);
const bundle = await adapter.fetchHistoricalBundle((message) => {
  process.stderr.write(`[p15:tushare] ${message}\n`);
});
const bundlePath = path.join(outputDir, "tushare-six-stock-raw-bundle.json");
const dailyCsvPath = path.join(outputDir, "tushare-six-stock-daily-raw.csv");
const adjustmentCsvPath = path.join(outputDir, "tushare-six-stock-adj-factor-raw.csv");
await writeFile(bundlePath, `${JSON.stringify(bundle)}\n`, { encoding: "utf8", mode: 0o600 });
await writeFile(dailyCsvPath, encodeCsv(bundle.daily), { encoding: "utf8", mode: 0o600 });
await writeFile(adjustmentCsvPath, encodeCsv(bundle.adjustmentFactors), { encoding: "utf8", mode: 0o600 });
await convertParquet(bundlePath, outputDir);

const files = [
  "tushare-six-stock-raw-bundle.json",
  "tushare-six-stock-daily-raw.csv",
  "tushare-six-stock-adj-factor-raw.csv",
  "tushare-six-stock-daily-raw.parquet",
  "tushare-six-stock-adj-factor-raw.parquet",
];
const manifest = {
  schemaVersion: "qf.p15.dataset-manifest.v1",
  datasetId: `p15-tushare-six-stock-${compactTimestamp(acquiredAt)}`,
  source: "Tushare Pro API",
  acquisition: {
    acquiredAt: acquiredAt.toISOString(),
    asOfDate: bundle.asOfDate,
    downloadedRange: bundle.downloadedRange,
    historicalSnapshot: bundle.historicalSnapshot,
    requestEvidence: bundle.requests,
  },
  selection: {
    policy: "historical HS300 membership and six-industry 2019 median-amount maximum",
    stocks: bundle.selected,
  },
  processing: {
    status: "RAW_UNCLEANED",
    dailyRows: bundle.daily.length,
    adjustmentFactorRows: bundle.adjustmentFactors.length,
    transformations: [
      "API field arrays decoded to row objects",
      "same raw rows serialized to JSON, CSV, and Parquet without cleaning, winsorization, imputation, or return construction",
    ],
  },
  formalTest: {
    status: "SEALED",
    firstPermittedDate: "2024-01-01",
    rowsIncluded: 0,
  },
  files: await Promise.all(files.map(async (fileName) => {
    const filePath = path.join(outputDir, fileName);
    return {
      fileName,
      bytes: (await readFile(filePath)).byteLength,
      sha256: await sha256File(filePath),
    };
  })),
};
const manifestPath = path.join(outputDir, "DatasetManifest.json");
await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });

process.stdout.write(`${JSON.stringify({
  status: "COMPLETED",
  outputDir,
  manifestPath,
  files: [...files, "DatasetManifest.json"],
  selectedStocks: bundle.selected.map((item) => item.tsCode),
  dailyRows: bundle.daily.length,
  adjustmentFactorRows: bundle.adjustmentFactors.length,
  formalTestSealed: true,
})}\n`);
