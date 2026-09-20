import { useEffect } from "react";

import type {
  AgentUiEvent,
  ConversationSnapshot,
  ProjectSourceSummary,
} from "@q-fintelligence/contracts";

type DetailSelection = { kind: string; title: string; data: unknown } | null;

function safeValue(value: unknown): unknown {
  if (typeof value === "string") {
    return value
      .replace(/authorization\s*:\s*bearer\s+\S+/giu, "Authorization: Bearer [REDACTED]")
      .replace(/cookie\s*:\s*[^\r\n]+/giu, "Cookie: [REDACTED]")
      .replace(
        /(["']?(?:api[_ -]?key|access[_ -]?token|connection[_ -]?key)["']?\s*[:=]\s*["']?)[^"'\s,;}]+/giu,
        "$1[REDACTED]",
      );
  }
  if (Array.isArray(value)) return value.map(safeValue);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [
      key,
      /(?:^|[_-])(api[_-]?key|token|secret|password|cookie|authorization)(?:$|[_-])/iu.test(key)
        ? "[REDACTED]"
        : safeValue(entry),
    ]));
  }
  return value;
}

function eventLabel(event: AgentUiEvent): string {
  const summary = event.workspace?.summary;
  if (typeof summary === "string" && summary.trim()) return summary;
  return event.type;
}

export function FullDetailView({
  snapshot,
  selected,
  sources,
  events,
  onClose,
}: {
  snapshot: ConversationSnapshot;
  selected: DetailSelection;
  sources: ProjectSourceSummary[];
  events: AgentUiEvent[];
  onClose: () => void;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const toolEvents = events.filter((event) => event.type.startsWith("tool."));
  const evidenceEvents = events.filter((event) => (
    event.type === "artifact.created"
    || event.type === "hardware.batch"
    || event.type === "approval.requested"
    || event.type === "approval.resolved"
  ));
  const activeDetail = selected ?? {
    kind: "conversation",
    title: snapshot.conversation.title,
    data: snapshot.conversation,
  };

  return (
    <div className="detail-overlay" role="dialog" aria-modal="true" aria-label="完整成果详情">
      <section className="detail-view">
        <header className="detail-view__header">
          <div>
            <span>Full Detail View</span>
            <h2>量融智枢完整成果详情</h2>
            <p>{snapshot.conversation.title} · {snapshot.conversation.provider}/{snapshot.conversation.modelId}</p>
          </div>
          <button type="button" aria-label="关闭完整详情" onClick={onClose}>×</button>
        </header>
        <div className="detail-view__body">
          <section className="full-detail-metrics" aria-label="运行摘要">
            <div><span>状态</span><strong>{snapshot.conversation.state}</strong></div>
            <div><span>证据事件</span><strong>{evidenceEvents.length}</strong></div>
            <div><span>来源</span><strong>{sources.length}</strong></div>
            <div><span>工件</span><strong>{snapshot.artifacts.length}</strong></div>
            <div><span>事件游标</span><strong>#{snapshot.latestSequence}</strong></div>
          </section>

          <div className="full-detail-grid">
            <section className="full-detail-card full-detail-card--wide">
              <span>Evidence · 当前审阅对象</span>
              <h3>{activeDetail.title}</h3>
              <small>{activeDetail.kind}</small>
              <pre>{JSON.stringify(safeValue(activeDetail.data), null, 2)}</pre>
            </section>

            <section className="full-detail-card">
              <span>Sources · 来源</span>
              <h3>项目事实源</h3>
              {sources.length === 0
                ? <p>尚无已登记项目来源。</p>
                : <ul>{sources.map((source) => (
                  <li key={source.sourceId}>
                    <strong>{source.fileName}</strong>
                    <small>{source.detectedMediaType} · {source.sha256.slice(0, 12)} · {source.quarantined ? "已隔离" : "可用"}</small>
                  </li>
                ))}</ul>}
            </section>

            <section className="full-detail-card">
              <span>Tools · 工具与 Agent 活动</span>
              <h3>OpenHands / QF Tool Gateway</h3>
              {toolEvents.length === 0
                ? <p>当前窗口内尚无工具事件。</p>
                : <ol>{toolEvents.slice(-24).reverse().map((event) => (
                  <li key={event.eventId}>
                    <strong>{eventLabel(event)}</strong>
                    <small>#{event.sequence} · {event.type}</small>
                  </li>
                ))}</ol>}
            </section>

            <section className="full-detail-card full-detail-card--wide">
              <span>Artifacts · 内容寻址工件</span>
              <h3>成果与复现包</h3>
              {snapshot.artifacts.length === 0
                ? <p>尚无登记工件。</p>
                : <div className="full-detail-artifacts">{snapshot.artifacts.map((artifact) => (
                  <article key={artifact.sha256}>
                    <div>
                      <strong>{artifact.producer}</strong>
                      <code>{artifact.sha256}</code>
                      <small>{artifact.mediaType} · {artifact.bytes} bytes · 父工件 {artifact.parentHashes.length}</small>
                    </div>
                    <a href={`/api/artifacts/${artifact.sha256}/download`} download>下载</a>
                  </article>
                ))}</div>}
            </section>

            <section className="full-detail-card full-detail-card--wide">
              <span>Review · 门禁与可恢复性</span>
              <h3>控制面状态所有权</h3>
              <dl>
                <div><dt>正式测试</dt><dd>SEALED</dd></div>
                <div><dt>审批</dt><dd>{snapshot.approvals.length} 项</dd></div>
                <div><dt>Runtime</dt><dd>{snapshot.session?.runtimeKind ?? "未初始化"}</dd></div>
                <div><dt>Runtime revision</dt><dd>{snapshot.session?.runtimeRevision ?? "—"}</dd></div>
                <div><dt>恢复游标</dt><dd>{snapshot.session?.recoveryCursor ?? 0}</dd></div>
                <div><dt>Workspace</dt><dd>{snapshot.workspace?.backend ?? "未初始化"}</dd></div>
                <div><dt>宿主工作区</dt><dd>{snapshot.workspace?.hostLocalWorkspace === false ? "未暴露" : "无证明"}</dd></div>
                <div><dt>快照 SHA</dt><dd>{String(snapshot.workspace?.snapshot.snapshotSha256 ?? "—")}</dd></div>
                <div><dt>Agent Server 镜像</dt><dd>{snapshot.workspace?.imageDigest ?? "—"}</dd></div>
              </dl>
            </section>
          </div>
        </div>
      </section>
    </div>
  );
}
