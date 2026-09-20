import { useRef } from "react";

import type { ProjectSourceSummary } from "@q-fintelligence/contracts";

function formatBytes(bytes: number): string {
  if (bytes < 1_024) return `${bytes} B`;
  if (bytes < 1_048_576) return `${(bytes / 1_024).toFixed(1)} KB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

function sourceKind(source: ProjectSourceSummary): "pdf" | "data" | "document" {
  if (source.extension === ".pdf") return "pdf";
  if ([".csv", ".tsv", ".xlsx", ".json", ".jsonl", ".parquet", ".npy", ".npz", ".h5", ".hdf5"].includes(source.extension)) {
    return "data";
  }
  return "document";
}

export function ProjectSourcesPanel({
  projectName,
  sources,
  busy,
  status,
  onFiles,
  onArchive,
  onInspect,
}: {
  projectName: string;
  sources: ProjectSourceSummary[];
  busy: boolean;
  status: string;
  onFiles: (files: FileList | null) => void;
  onArchive: (source: ProjectSourceSummary) => void;
  onInspect: (source: ProjectSourceSummary) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  return <section className="project-sources" aria-labelledby="project-sources-title">
    <header className="project-sources__header">
      <div>
        <span>项目来源</span>
        <h1 id="project-sources-title">{projectName}</h1>
        <p>这些文件由同一项目的全部对话共享；内容始终按不可信数据解析，隔离项不会进入 Agent 上下文。</p>
      </div>
      <button className="source-add-button" type="button" disabled={busy} onClick={() => inputRef.current?.click()}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        {busy ? "安全解析中…" : "添加来源"}
      </button>
      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        aria-label="添加项目来源"
        accept=".csv,.tsv,.xlsx,.json,.jsonl,.parquet,.npy,.npz,.h5,.hdf5,.txt,.md,.pdf,.qasm,.qcis"
        onChange={(event) => {
          onFiles(event.target.files);
          event.currentTarget.value = "";
        }}
      />
    </header>
    <div className="project-sources__toolbar">
      <strong>来源</strong>
      <span>{sources.length} 个文件</span>
      <small aria-live="polite">{status}</small>
    </div>
    {sources.length === 0
      ? <div className="project-sources__empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M4 5h6l2 2h8v12H4z" /></svg>
        <strong>还没有项目来源</strong>
        <p>从这里添加，或在对话输入框右下角选择附件；发送后会自动进入本项目来源。</p>
      </div>
      : <div className="project-source-list">
        {sources.map((source) => <article key={source.sourceId} className={source.quarantined ? "quarantined" : ""}>
          <span className={`source-file-icon ${sourceKind(source)}`} aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><path d="M6 3h8l4 4v14H6zM14 3v5h5" /></svg>
          </span>
          <div className="source-file-copy">
            <strong>{source.fileName}</strong>
            <span>{source.extension.replace(".", "").toLocaleUpperCase() || "FILE"} · {formatBytes(source.byteSize)} · {formatDate(source.createdAt)}</span>
            <small>{source.quarantined ? "已隔离，不进入 Agent 上下文" : `已安全解析 · ${source.parser}`}</small>
          </div>
          <div className="source-file-actions">
            <button type="button" onClick={() => onInspect(source)}>详情</button>
            {source.artifactSha256 && <a href={`/api/artifacts/${source.artifactSha256}/download`} download>下载</a>}
            <button className="danger-text" type="button" onClick={() => onArchive(source)}>移除</button>
          </div>
        </article>)}
      </div>}
  </section>;
}
