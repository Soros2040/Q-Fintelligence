import { createHash } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

interface TushareResponse {
  code: number;
  msg: string;
  request_id?: string;
  data?: { fields?: string[]; items?: unknown[][] };
}

interface RequestEvidence {
  apiName: string;
  paramsHash: string;
  responseHash: string;
  requestId: string | null;
  rowCount: number;
  cached: boolean;
}

type RecordRow = Record<string, string | number | null>;

export interface TushareDatasetBundle {
  schemaVersion: "qf.tushare-bundle.v1";
  source: "tushare";
  asOfDate: "2026-07-20";
  formalTestSealed: true;
  downloadedRange: { start: "2019-01-01"; end: "2023-12-31" };
  historicalSnapshot: {
    hs300IndexCode: string;
    componentTradeDate: string;
    classificationEffectiveDate: "2019-12-31";
  };
  selected: Array<{
    industry: string;
    l1Code: string;
    tsCode: string;
    medianAmount2019: number;
    validTradingDays2019: number;
    membershipInDate: string;
    membershipOutDate: string | null;
  }>;
  daily: RecordRow[];
  adjustmentFactors: RecordRow[];
  requests: RequestEvidence[];
}

const TARGETS = [
  { industry: "银行", l1Code: "801780.SI" },
  { industry: "食品饮料", l1Code: "801120.SI" },
  { industry: "医药生物", l1Code: "801150.SI" },
  { industry: "电子", l1Code: "801080.SI" },
  { industry: "公用事业", l1Code: "801160.SI" },
  { industry: "交通运输", l1Code: "801170.SI" },
] as const;

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function rowsFromResponse(response: TushareResponse): RecordRow[] {
  const fields = response.data?.fields ?? [];
  const items = response.data?.items ?? [];
  return items.map((item) => Object.fromEntries(fields.map((field, index) => {
    const value = item[index];
    return [field, typeof value === "string" || typeof value === "number" || value === null ? value : String(value)];
  })));
}

function fieldText(row: RecordRow, field: string): string {
  const value = row[field];
  if (typeof value !== "string") throw new Error(`Tushare field ${field} was not text`);
  return value;
}

function median(values: number[]): number {
  const sorted = [...values].sort((left, right) => left - right);
  if (sorted.length === 0) throw new Error("cannot compute a median from an empty series");
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[middle - 1]! + sorted[middle]!) / 2 : sorted[middle]!;
}

export class TushareAdapter {
  private readonly requests: RequestEvidence[] = [];

  constructor(
    private readonly token: string,
    private readonly cacheRoot: string,
    private readonly endpoint = "https://api.tushare.pro",
  ) {
    if (!token.trim()) throw new Error("TUSHARE_TOKEN is not configured");
  }

  private async query(apiName: string, params: Record<string, string | number>, fields: string): Promise<RecordRow[]> {
    const canonical = JSON.stringify({ apiName, params: Object.fromEntries(Object.entries(params).sort()), fields });
    const paramsHash = sha256(canonical);
    const cachePath = path.join(this.cacheRoot, `${paramsHash}.json`);
    let raw: string;
    let cached = false;
    try {
      raw = await readFile(cachePath, "utf8");
      cached = true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      const response = await fetch(this.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_name: apiName, token: this.token, params, fields }),
        signal: AbortSignal.timeout(60_000),
      });
      if (!response.ok) throw new Error(`Tushare ${apiName} returned HTTP ${response.status}`);
      raw = await response.text();
      const parsed = JSON.parse(raw) as TushareResponse;
      if (parsed.code !== 0) throw new Error(`Tushare ${apiName} business code ${parsed.code}: ${parsed.msg.slice(0, 300)}`);
      await mkdir(this.cacheRoot, { recursive: true });
      const temporary = `${cachePath}.${process.pid}.tmp`;
      await writeFile(temporary, `${raw.trim()}\n`, { encoding: "utf8", mode: 0o600 });
      await rename(temporary, cachePath);
    }
    const parsed = JSON.parse(raw) as TushareResponse;
    if (parsed.code !== 0) throw new Error(`cached Tushare ${apiName} business code ${parsed.code}`);
    const result = rowsFromResponse(parsed);
    this.requests.push({
      apiName,
      paramsHash,
      responseHash: sha256(raw),
      requestId: typeof parsed.request_id === "string" ? parsed.request_id : null,
      rowCount: result.length,
      cached,
    });
    return result;
  }

  async preflight(): Promise<{ ok: true; evidence: RequestEvidence }> {
    const rows = await this.query("trade_cal", {
      exchange: "SSE",
      start_date: "20200102",
      end_date: "20200102",
    }, "exchange,cal_date,is_open");
    if (rows.length !== 1) throw new Error(`Tushare trade_cal preflight returned ${rows.length} rows`);
    return { ok: true, evidence: this.requests.at(-1)! };
  }

  private async historicalHs300(): Promise<{ indexCode: string; tradeDate: string; codes: Set<string> }> {
    for (const indexCode of ["000300.SH", "399300.SZ"]) {
      const data = await this.query("index_weight", {
        index_code: indexCode,
        start_date: "20200101",
        end_date: "20200131",
      }, "index_code,con_code,trade_date,weight");
      if (data.length === 0) continue;
      const tradeDate = data.map((row) => fieldText(row, "trade_date")).sort().at(0)!;
      const codes = new Set(data.filter((row) => fieldText(row, "trade_date") === tradeDate).map((row) => fieldText(row, "con_code")));
      if (codes.size < 250) throw new Error(`historical HS300 snapshot only contained ${codes.size} constituents`);
      return { indexCode, tradeDate, codes };
    }
    throw new Error("Tushare returned no January 2020 HS300 component snapshot");
  }

  private async historicalIndustryMembers(l1Code: string): Promise<Map<string, { inDate: string; outDate: string | null }>> {
    const combined: RecordRow[] = [];
    for (const isNew of ["Y", "N"]) {
      combined.push(...await this.query("index_member_all", { l1_code: l1Code, is_new: isNew },
        "l1_code,l1_name,ts_code,name,in_date,out_date,is_new"));
    }
    const active = new Map<string, { inDate: string; outDate: string | null }>();
    for (const row of combined) {
      const inDate = fieldText(row, "in_date");
      const rawOutDate = row.out_date;
      const outDate = typeof rawOutDate === "string" && rawOutDate.length > 0 ? rawOutDate : null;
      if (inDate > "20191231" || (outDate !== null && outDate < "20191231")) continue;
      const tsCode = fieldText(row, "ts_code");
      const previous = active.get(tsCode);
      if (!previous || inDate > previous.inDate) active.set(tsCode, { inDate, outDate });
    }
    if (active.size === 0) throw new Error(`Tushare could not prove historical membership for ${l1Code}`);
    return active;
  }

  async fetchHistoricalBundle(onProgress?: (message: string) => void): Promise<TushareDatasetBundle> {
    await this.preflight();
    const snapshot = await this.historicalHs300();
    const selected: TushareDatasetBundle["selected"] = [];
    for (const target of TARGETS) {
      const membership = await this.historicalIndustryMembers(target.l1Code);
      const candidates = [...membership.keys()].filter((tsCode) => snapshot.codes.has(tsCode)).sort();
      if (candidates.length === 0) throw new Error(`no historical HS300 candidates were proven for ${target.industry}`);
      let best: TushareDatasetBundle["selected"][number] | null = null;
      for (const tsCode of candidates) {
        onProgress?.(`Tushare 2019 liquidity ${target.industry} ${tsCode}`);
        const daily = await this.query("daily", {
          ts_code: tsCode,
          start_date: "20190101",
          end_date: "20191231",
        }, "ts_code,trade_date,close,vol,amount");
        const amounts = daily.flatMap((row) => typeof row.amount === "number" && Number.isFinite(row.amount) ? [row.amount] : []);
        const complete = daily.filter((row) => typeof row.close === "number" && typeof row.amount === "number").length;
        if (daily.length < 200 || complete / daily.length < 0.98 || amounts.length < 200) continue;
        const membershipEvidence = membership.get(tsCode)!;
        const candidate = {
          industry: target.industry,
          l1Code: target.l1Code,
          tsCode,
          medianAmount2019: median(amounts),
          validTradingDays2019: daily.length,
          membershipInDate: membershipEvidence.inDate,
          membershipOutDate: membershipEvidence.outDate,
        };
        if (!best || candidate.medianAmount2019 > best.medianAmount2019
          || (candidate.medianAmount2019 === best.medianAmount2019 && candidate.tsCode < best.tsCode)) best = candidate;
      }
      if (!best) throw new Error(`no ${target.industry} candidate passed the 2019 liquidity and completeness gate`);
      selected.push(best);
    }
    if (new Set(selected.map((item) => item.tsCode)).size !== 6) throw new Error("historical selection did not produce six unique stocks");

    const daily: RecordRow[] = [];
    const adjustmentFactors: RecordRow[] = [];
    for (const item of selected) {
      onProgress?.(`Tushare train-validation data ${item.tsCode}`);
      daily.push(...await this.query("daily", {
        ts_code: item.tsCode,
        start_date: "20190101",
        end_date: "20231231",
      }, "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"));
      adjustmentFactors.push(...await this.query("adj_factor", {
        ts_code: item.tsCode,
        start_date: "20190101",
        end_date: "20231231",
      }, "ts_code,trade_date,adj_factor"));
    }
    if (daily.some((row) => fieldText(row, "trade_date") >= "20240101")) {
      throw new Error("formal test rows crossed the sealed boundary");
    }
    return {
      schemaVersion: "qf.tushare-bundle.v1",
      source: "tushare",
      asOfDate: "2026-07-20",
      formalTestSealed: true,
      downloadedRange: { start: "2019-01-01", end: "2023-12-31" },
      historicalSnapshot: {
        hs300IndexCode: snapshot.indexCode,
        componentTradeDate: snapshot.tradeDate,
        classificationEffectiveDate: "2019-12-31",
      },
      selected,
      daily,
      adjustmentFactors,
      requests: [...this.requests],
    };
  }
}
