import { Fragment, type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import type {
  AgentUiEvent,
  ApprovalRequest,
  ArtifactSummary,
  CampaignSummary,
  ConversationSnapshot,
  P05TaskDetail,
} from "@q-fintelligence/contracts";

import {
  currentStage,
  elapsedLabel,
  latestHeartbeat,
  resultKind,
  runPresentation,
  runSurfaceLabel,
  taskProgress,
  visibleWorkspaceEvents,
  workspaceEvent,
} from "./workspace-model.js";

type DetailTab = "EVIDENCE" | "SOURCES" | "TOOLS" | "ARTIFACTS" | "REVIEW";

interface CentralWorkspaceProps {
  snapshot: ConversationSnapshot;
  events: AgentUiEvent[];
  streamingText: string;
  connection: string;
  campaign: CampaignSummary | null;
  p05: P05TaskDetail | null;
  intake: ReactNode;
  onSelectDetail: (kind: string, title: string, data: unknown, tab?: DetailTab) => void;
  onPreviewArtifact: (artifact: ArtifactSummary) => void;
  onDecision: (approval: ApprovalRequest, decision: "APPROVE" | "REJECT") => void;
}

function inlineMarkdown(text: string): ReactNode[] {
  const tokens = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)]+\))/gu);
  return tokens.map((token, index) => {
    if (token.startsWith("`") && token.endsWith("`")) return <code key={index}>{token.slice(1, -1)}</code>;
    if (token.startsWith("**") && token.endsWith("**")) return <strong key={index}>{token.slice(2, -2)}</strong>;
    const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/u);
    if (link) return <a key={index} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    return <Fragment key={index}>{token}</Fragment>;
  });
}

function Markdown({ content }: { content: string }) {
  const lines = content.split(/\r?\n/u);
  const nodes: ReactNode[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    if (line.startsWith("```")) {
      const language = line.slice(3).trim();
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !(lines[index] ?? "").startsWith("```")) {
        body.push(lines[index] ?? "");
        index += 1;
      }
      nodes.push(<pre className="rich-code" key={`code-${index}`} data-language={language || "text"}><code>{body.join("\n")}</code></pre>);
      index += 1;
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/u);
    if (heading) {
      const level = heading[1]?.length ?? 2;
      const Heading = `h${Math.min(4, level + 1)}` as "h2" | "h3" | "h4";
      nodes.push(<Heading key={`heading-${index}`}>{inlineMarkdown(heading[2] ?? "")}</Heading>);
      index += 1;
      continue;
    }
    if (/^[-*]\s+/u.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/u.test(lines[index] ?? "")) {
        items.push((lines[index] ?? "").replace(/^[-*]\s+/u, ""));
        index += 1;
      }
      nodes.push(<ul key={`list-${index}`}>{items.map((item) => <li key={item}>{inlineMarkdown(item)}</li>)}</ul>);
      continue;
    }
    if (line.includes("|") && /^\s*\|?.+\|.+\|?\s*$/u.test(line) && /^\s*\|?\s*:?-+/u.test(lines[index + 1] ?? "")) {
      const header = line.split("|").map((cell) => cell.trim()).filter(Boolean);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && (lines[index] ?? "").includes("|")) {
        rows.push((lines[index] ?? "").split("|").map((cell) => cell.trim()).filter(Boolean));
        index += 1;
      }
      nodes.push(<div className="rich-table-scroll" key={`table-${index}`}><table><thead><tr>{header.map((cell) => <th key={cell}>{inlineMarkdown(cell)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{inlineMarkdown(cell)}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }
    if (line.startsWith(">")) {
      nodes.push(<blockquote key={`quote-${index}`}>{inlineMarkdown(line.replace(/^>\s?/u, ""))}</blockquote>);
      index += 1;
      continue;
    }
    if (line.trim() !== "") nodes.push(<p key={`p-${index}`}>{inlineMarkdown(line)}</p>);
    index += 1;
  }
  return <div className="rich-markdown">{nodes}</div>;
}

function JsonPreview({ value }: { value: unknown }) {
  return <pre className="rich-code"><code>{JSON.stringify(value, null, 2)}</code></pre>;
}

function DataTable({ payload }: { payload: AgentUiEvent["payload"] }) {
  const stdout = typeof payload.stdout === "object" && payload.stdout !== null && !Array.isArray(payload.stdout)
    ? payload.stdout
    : {};
  const preview = Array.isArray(stdout.preview) ? stdout.preview : [];
  const columns = Array.isArray(stdout.columns) ? stdout.columns.map(String) : [];
  const chart = Array.isArray(stdout.chart) ? stdout.chart : [];
  return <div className="result-surface">
    <dl className="result-metrics"><div><dt>行</dt><dd>{String(stdout.rows ?? "—")}</dd></div><div><dt>字段</dt><dd>{columns.length}</dd></div><div><dt>重复行</dt><dd>{String(stdout.duplicate_rows ?? "—")}</dd></div><div><dt>质量门</dt><dd>{String(stdout.quality_gate ?? "—")}</dd></div></dl>
    {preview.length > 0 && columns.length > 0 ? <div className="rich-table-scroll"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{preview.slice(0, 8).map((row, rowIndex) => <tr key={rowIndex}>{columns.map((column) => <td key={column}>{String(typeof row === "object" && row !== null && !Array.isArray(row) ? (row as Record<string, unknown>)[column] ?? "" : "")}</td>)}</tr>)}</tbody></table></div> : null}
    {chart.length > 0 ? <div className="mini-chart" aria-label="字段缺失值图表">{chart.map((item, chartIndex) => {
      const row = typeof item === "object" && item !== null && !Array.isArray(item) ? item as Record<string, unknown> : {};
      const count = Number(row.missing ?? 0);
      return <div key={`${String(row.field)}-${chartIndex}`}><span>{String(row.field)}</span><i style={{ "--bar": `${Math.min(100, count * 12)}%` } as CSSProperties} /><b>{count}</b></div>;
    })}</div> : null}
  </div>;
}

function QuantumCircuit({ payload }: { payload: AgentUiEvent["payload"] }) {
  const circuit = typeof payload.circuit === "object" && payload.circuit !== null && !Array.isArray(payload.circuit)
    ? payload.circuit
    : {};
  const manifest = typeof circuit.manifest === "object" && circuit.manifest !== null && !Array.isArray(circuit.manifest)
    ? circuit.manifest as Record<string, unknown>
    : {};
  return <div className="result-surface quantum-surface">
    <dl className="result-metrics"><div><dt>qubits</dt><dd>{String(manifest.qubits ?? "—")}</dd></div><div><dt>门数</dt><dd>{String(manifest.instruction_count ?? "—")}</dd></div><div><dt>深度</dt><dd>{String(manifest.depth ?? "—")}</dd></div><div><dt>shots</dt><dd>{String(manifest.shots ?? 100)}</dd></div></dl>
    <div className="circuit-wire" aria-label="QAOA 线路摘要"><span>|ψ₀⟩</span><i>Cost(γ)</i><i>XY(β)</i><i>Measure</i></div>
    {typeof circuit.qcis === "string" ? <details><summary>QCIS · {String(manifest.qcis_sha256 ?? "hash pending").slice(0, 16)}</summary><pre className="rich-code"><code>{circuit.qcis}</code></pre></details> : null}
    {typeof circuit.diff === "string" ? <details><summary>线路策略 diff</summary><pre className="rich-code diff"><code>{circuit.diff}</code></pre></details> : null}
    {Array.isArray(circuit.tests) ? <ul className="test-results">{circuit.tests.map((test, index) => <li key={index}>{typeof test === "object" && test !== null && !Array.isArray(test) ? `${String((test as Record<string, unknown>).status)} · ${String((test as Record<string, unknown>).name)}` : String(test)}</li>)}</ul> : null}
  </div>;
}

function EventResult({ event }: { event: AgentUiEvent }) {
  const kind = resultKind(event);
  if (kind === "data_table") return <DataTable payload={event.payload} />;
  if (kind === "quantum_circuit") return <QuantumCircuit payload={event.payload} />;
  if (kind === "hardware") return <div className="result-surface hardware-surface"><dl><div><dt>后端</dt><dd>{String(event.payload.backend ?? "tianyan176")}</dd></div><div><dt>状态</dt><dd>{String(event.payload.status ?? "—")}</dd></div><div><dt>Query ID</dt><dd><code>{String(event.payload.queryId ?? "尚未取得")}</code></dd></div><div><dt>shots</dt><dd>{String(event.payload.shots ?? "—")}</dd></div></dl></div>;
  if (kind === "warning" || kind === "error") return <div className={`result-alert ${kind}`}><strong>{kind === "error" ? "错误" : "警告"}</strong><p>{workspaceEvent(event).summary}</p>{typeof event.payload.recovery === "string" ? <p>恢复：{event.payload.recovery}</p> : null}</div>;
  return null;
}

function ActivityEvent({
  event,
  current,
  open,
  onToggle,
  onSelectDetail,
}: {
  event: AgentUiEvent;
  current: boolean;
  open: boolean;
  onToggle: (open: boolean) => void;
  onSelectDetail: CentralWorkspaceProps["onSelectDetail"];
}) {
  const view = workspaceEvent(event);
  const kind = resultKind(event);
  return <details className={`workspace-event workspace-event--${kind}${current ? " current" : ""}`} open={open} onToggle={(toggle) => onToggle(toggle.currentTarget.open)} data-event-id={event.eventId}>
    <summary><span className="event-mark" aria-hidden="true" /><span><strong>{view.summary}</strong><small>#{view.sequence ?? "live"} · {view.agentId ?? view.kind} · {new Date(view.timestamp).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small></span><span className="event-kind">{view.kind}</span></summary>
    <div className="workspace-event__body"><EventResult event={event} /><p className="event-ref">来源 {view.detailRefs.eventId}{view.detailRefs.runId ? ` · Run ${view.detailRefs.runId}` : ""}</p><button type="button" onClick={() => onSelectDetail("Event", view.sourceEventId, event)}>在右栏查看完整 Event</button></div>
  </details>;
}

function stageLabel(event: AgentUiEvent): string {
  const view = workspaceEvent(event);
  if (view.stage) return view.stage;
  if (["tool_started", "tool_progress", "tool_completed", "tool_failed"].includes(view.kind)) return "科学工具与计算";
  if (["blocked", "waiting", "heartbeat", "retrying", "recovered"].includes(view.kind)) return "等待、审批与恢复";
  if (["artifact_created", "result_ready", "completed"].includes(view.kind)) return "成果与收口";
  return "任务与 Agent 活动";
}

export function CentralWorkspace(props: CentralWorkspaceProps) {
  const { snapshot, events, streamingText, connection, intake, onSelectDetail, onPreviewArtifact, onDecision } = props;
  const visibleEvents = useMemo(() => visibleWorkspaceEvents(events), [events]);
  const heartbeat = useMemo(() => latestHeartbeat(events), [events]);
  const latest = visibleEvents.at(-1) ?? heartbeat;
  const [now, setNow] = useState(() => Date.now());
  const scrollRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const previousSignature = useRef("");
  const [unseen, setUnseen] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const run = runPresentation(snapshot, events);
  const surfaceLabel = runSurfaceLabel(events);
  const activeRun = run.ticking;
  const [runOpen, setRunOpen] = useState(activeRun);
  const storageKey = `qf.ui.workspace.${snapshot.conversation.conversationId}`;

  useEffect(() => {
    if (!activeRun) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [activeRun]);

  useEffect(() => {
    const saved = sessionStorage.getItem(storageKey);
    let parsed: { top?: number; following?: boolean; expanded?: string[]; runOpen?: boolean } = {};
    try {
      parsed = saved ? JSON.parse(saved) as typeof parsed : {};
    } catch {
      sessionStorage.removeItem(storageKey);
    }
    followingRef.current = parsed.following ?? true;
    setExpanded(new Set(parsed.expanded ?? []));
    setRunOpen(parsed.runOpen ?? activeRun);
    window.requestAnimationFrame(() => {
      const element = scrollRef.current;
      if (!element) return;
      element.scrollTop = followingRef.current ? element.scrollHeight : parsed.top ?? 0;
    });
    return () => {
      const element = scrollRef.current;
      sessionStorage.setItem(storageKey, JSON.stringify({ top: element?.scrollTop ?? 0, following: followingRef.current, expanded: [...expanded], runOpen }));
    };
  }, [storageKey]);

  useEffect(() => {
    if (activeRun) setRunOpen(true);
  }, [activeRun, snapshot.conversation.conversationId]);

  useEffect(() => {
    const signature = `${events.at(-1)?.eventId ?? "none"}:${streamingText.length}:${snapshot.messages.length}:${snapshot.artifacts.length}`;
    if (previousSignature.current === "") {
      previousSignature.current = signature;
      return;
    }
    if (signature === previousSignature.current) return;
    previousSignature.current = signature;
    const element = scrollRef.current;
    if (!element) return;
    if (followingRef.current) window.requestAnimationFrame(() => element.scrollTo({ top: element.scrollHeight, behavior: "smooth" }));
    else setUnseen((count) => count + 1);
  }, [events, snapshot.artifacts.length, snapshot.messages.length, streamingText.length]);

  const groups = useMemo(() => {
    const result = new Map<string, AgentUiEvent[]>();
    for (const event of visibleEvents) {
      const label = stageLabel(event);
      result.set(label, [...(result.get(label) ?? []), event]);
    }
    return [...result.entries()];
  }, [visibleEvents]);
  const agentTeam = useMemo(() => {
    const agents = new Map<string, { label: string; state: "done" | "running" | "waiting"; summary: string }>();
    for (const event of visibleEvents) {
      const view = workspaceEvent(event);
      if (!view.agentId) continue;
      const terminal = ["completed", "result_ready", "artifact_created", "tool_completed"].includes(view.kind);
      const waiting = ["blocked", "waiting"].includes(view.kind);
      agents.set(view.agentId, {
        label: view.agentId,
        state: waiting ? "waiting" : terminal ? "done" : "running",
        summary: view.summary,
      });
    }
    return [...agents.values()].slice(0, 5);
  }, [visibleEvents]);

  const currentEventId = visibleEvents.findLast((event) => {
    const kind = workspaceEvent(event).kind;
    return !["completed", "result_ready", "artifact_created"].includes(kind);
  })?.eventId ?? visibleEvents.at(-1)?.eventId ?? null;
  const progress = taskProgress(snapshot);
  const stage = currentStage(snapshot, events);
  const toolEventCount = visibleEvents.filter((event) => workspaceEvent(event).kind.startsWith("tool_")).length;
  const featuredEvents = visibleEvents.filter((event) => ["warning", "error", "hardware", "data_table", "quantum_circuit"].includes(resultKind(event)));
  const lastUserIndex = snapshot.messages.findLastIndex((message) => message.role === "USER");

  const setEventOpen = (eventId: string, open: boolean) => setExpanded((current) => {
    const next = new Set(current);
    if (open) next.add(eventId); else next.delete(eventId);
    sessionStorage.setItem(storageKey, JSON.stringify({ top: scrollRef.current?.scrollTop ?? 0, following: followingRef.current, expanded: [...next], runOpen }));
    return next;
  });

  const contextDisclosure = <details className="context-disclosure">
    <summary>
      <span className="disclosure-icon" aria-hidden="true">+</span>
      <span>任务上下文</span>
      <small>{snapshot.artifacts.length > 0 ? `${snapshot.artifacts.length} 个已登记工件` : "可上传验证数据"}</small>
      <span className="disclosure-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div className="context-disclosure__body">{intake}</div>
  </details>;

  const workDisclosure = <details className={`work-disclosure ${run.hasStarted ? activeRun ? "running" : "terminal" : "idle"}`} open={runOpen} onToggle={(event) => {
    const open = event.currentTarget.open;
    setRunOpen(open);
    sessionStorage.setItem(storageKey, JSON.stringify({ top: scrollRef.current?.scrollTop ?? 0, following: followingRef.current, expanded: [...expanded], runOpen: open }));
  }}>
    <summary>
      <span className={`work-state-dot ${snapshot.conversation.state.toLocaleLowerCase()}`} aria-hidden="true" />
      <span className="work-summary-title">{run.label}{run.startedAt ? ` ${elapsedLabel(run.startedAt, now)}` : ""}</span>
      <small>{visibleEvents.length} 条记录 · {toolEventCount} 次工具活动 · {snapshot.artifacts.length} 个产出</small>
      <span className="disclosure-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div className="work-disclosure__body">
      <header className="run-overview">
        <div>
          <span>{run.hasStarted ? surfaceLabel : "READY"} · {latest?.runId ?? snapshot.conversation.taskId}</span>
          <h2>{snapshot.conversation.title}</h2>
          <p>{stage} · {snapshot.conversation.state} · {progress}%</p>
        </div>
        <dl>
          <div><dt>模型</dt><dd>{snapshot.conversation.mode === "OPENHANDS" ? `${snapshot.conversation.provider}/${snapshot.conversation.modelId}` : snapshot.conversation.mode === "PI" ? "Pi（历史只读）" : "Mock fixture"}</dd></div>
          <div><dt>连接</dt><dd>{connection} · cursor #{snapshot.latestSequence}</dd></div>
        </dl>
      </header>

      <section className="execution-plan-card" aria-label="执行计划">
        <header>
          <div><span>Execution Plan · V1</span><h2>执行计划</h2></div>
          <strong>{snapshot.approvals.some((approval) => approval.status === "PENDING") ? "等待授权" : run.hasStarted ? "计划执行中" : "计划就绪"}</strong>
        </header>
        <dl>
          <div><dt>目标</dt><dd>{snapshot.conversation.title}</dd></div>
          <div><dt>运行时</dt><dd>{snapshot.conversation.mode === "OPENHANDS" ? "OpenHands Conversation + Remote Workspace" : `${snapshot.conversation.mode} 历史/测试模式`}</dd></div>
          <div><dt>模型与工具</dt><dd>{snapshot.conversation.provider}/{snapshot.conversation.modelId} · QF MCP allowlist</dd></div>
          <div><dt>治理边界</dt><dd>审批、证据、工件、Query ID 与科学终态由 QF Control Plane 持有</dd></div>
        </dl>
        {snapshot.steps.length > 0 && <ol>{snapshot.steps.map((step) => <li key={step.stepId}><span>{step.name}</span><strong>{step.status}</strong></li>)}</ol>}
      </section>

      {snapshot.approvals.filter((approval) => approval.status === "PENDING").map((approval) => <section className="authorization-card" key={`work-${approval.approvalId}`} aria-label="待授权操作">
        <div><span>Authorization</span><strong>{approval.action}</strong><small>{approval.rationale}</small></div>
        <div className="button-row"><button className="primary" onClick={() => onDecision(approval, "APPROVE")}>批准</button><button onClick={() => onDecision(approval, "REJECT")}>拒绝</button></div>
      </section>)}

      <div className="current-work" aria-label="当前工作" aria-live="polite">
        <span className={`work-pulse ${snapshot.conversation.state.toLocaleLowerCase()}`} aria-hidden="true" />
        <div><span>当前工作 · {latest ? workspaceEvent(latest).agentId ?? "Agent" : "等待任务"}</span><strong>{latest ? workspaceEvent(latest).summary : "等待真实事件"}</strong><small>{heartbeat ? `等待对象 ${String(heartbeat.payload.waitingFor ?? "当前外部调用")} · 最后真实活动 ${String(heartbeat.payload.lastRealEventAt ?? "—")} · 下次更新 ${String(heartbeat.payload.nextPollAt ?? heartbeat.payload.nextQueryAt ?? "—")}` : "状态由 SQLite / SSE 真实事件驱动"}</small></div>
        {currentEventId ? <button type="button" onClick={() => scrollRef.current?.querySelector(`[data-event-id="${currentEventId}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" })}>定位当前</button> : null}
      </div>

      {agentTeam.length > 0 ? <div className="agent-team" aria-label="Agent 团队状态">{agentTeam.map((agent) => <div className={`agent-member ${agent.state}`} key={agent.label}><span>{agent.label.slice(0, 1).toLocaleUpperCase()}</span><div><strong>{agent.label}</strong><small>{agent.summary}</small><i /></div></div>)}</div> : null}

      <section className="stage-timeline" aria-labelledby="stage-timeline-title">
        <header className="section-title"><div><span>运行记录</span><h2 id="stage-timeline-title">工具与阶段详情</h2></div><div className="timeline-actions"><button type="button" onClick={() => setExpanded(new Set())}>全部收起</button><button type="button" onClick={() => setExpanded(new Set(currentEventId ? [currentEventId] : []))}>仅当前</button></div></header>
        {groups.length === 0 ? <p className="empty-compact">等待第一条持久事件。</p> : groups.map(([label, group]) => <section className="timeline-group" key={label}><h3>{label}<span>{group.length}</span></h3>{group.map((event) => <ActivityEvent key={event.eventId} event={event} current={event.eventId === currentEventId} open={expanded.has(event.eventId)} onToggle={(open) => setEventOpen(event.eventId, open)} onSelectDetail={onSelectDetail} />)}</section>)}
      </section>
    </div>
  </details>;

  const evidenceDisclosure = <details className="evidence-disclosure">
    <summary>
      <span className="disclosure-icon evidence" aria-hidden="true">✓</span>
      <span>成果与证据</span>
      <small>{snapshot.artifacts.length} 个产出 · {snapshot.approvals.length} 项审批 · {featuredEvents.length} 条关键结果</small>
      <span className="disclosure-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div className="evidence-disclosure__body">
      <div className="artifact-grid">{snapshot.artifacts.length === 0 ? <p className="empty-compact">科学工具登记工件后在此出现。</p> : snapshot.artifacts.map((artifact) => <article key={artifact.sha256}><span>{artifact.mediaType.includes("data-quality") ? "真实数据检查" : artifact.mediaType.includes("quantum-circuit") ? "本地量子线路" : artifact.mediaType.includes("tianyan") ? "真机事实" : "内容寻址工件"}</span><strong>{artifact.producer}</strong><code>{artifact.sha256}</code><small>{artifact.bytes.toLocaleString()} bytes · {artifact.mediaType}</small><div><button type="button" onClick={() => onPreviewArtifact(artifact)}>在右栏打开</button><a href={`/api/artifacts/${artifact.sha256}/download`} download>下载</a></div></article>)}</div>
      {snapshot.approvals.map((approval) => <article className="approval-card" key={approval.approvalId}><div><span className="approval-icon">!</span><div><strong>需要人工审批 · {approval.action}</strong><p>{approval.rationale}</p><small>{approval.status} · {approval.approvalId}</small></div></div>{approval.status === "PENDING" ? <div className="button-row"><button className="primary" onClick={() => onDecision(approval, "APPROVE")}>批准</button><button onClick={() => onDecision(approval, "REJECT")}>拒绝</button></div> : null}</article>)}
      {featuredEvents.map((event) => <article className="featured-result" key={`result-${event.eventId}`}><header><strong>{workspaceEvent(event).summary}</strong><button type="button" onClick={() => onSelectDetail("Event", event.eventId, event)}>原始详情</button></header><EventResult event={event} /></article>)}
    </div>
  </details>;

  return <div className="workspace-scroll" ref={scrollRef} onScroll={(event) => {
    const element = event.currentTarget;
    const atLatest = element.scrollHeight - element.scrollTop - element.clientHeight < 96;
    followingRef.current = atLatest;
    if (atLatest) setUnseen(0);
    sessionStorage.setItem(storageKey, JSON.stringify({ top: element.scrollTop, following: atLatest, expanded: [...expanded], runOpen }));
  }}>
    <section className="conversation-layer" aria-labelledby="conversation-layer-title">
      <header className="section-title"><div><span>Conversation</span><h2 id="conversation-layer-title">任务对话</h2></div><small>{snapshot.messages.length} 条持久消息</small></header>
      {snapshot.messages.length === 0 ? <><p className="empty-compact">新对话已就绪；上传验证数据后描述可检验任务。</p>{contextDisclosure}{workDisclosure}</> : snapshot.messages.map((message, index) => <Fragment key={message.messageId}><article className={`message ${message.role.toLocaleLowerCase()}`} onClick={() => onSelectDetail("Message", message.messageId, message)}><div className="message-body"><div className="message-meta"><strong>{message.role === "USER" ? "我" : snapshot.conversation.mode === "OPENHANDS" ? "OpenHands Orchestrator" : snapshot.conversation.mode === "PI" ? "Pi（历史）" : "Mock Orchestrator"}</strong><time>{new Date(message.createdAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time></div><Markdown content={message.content} /></div></article>{index === lastUserIndex ? <div className="run-anchor">{contextDisclosure}{workDisclosure}</div> : null}</Fragment>)}
      {streamingText ? <article className="message assistant streaming"><div className="message-body"><div className="message-meta"><strong>真实模型增量输出</strong><span className="typing">LIVE</span></div><Markdown content={streamingText} /></div></article> : null}
      {evidenceDisclosure}
    </section>
    <div className="transcript-spacer" />
    {unseen > 0 ? <button className="return-latest" type="button" onClick={() => {
      followingRef.current = true;
      setUnseen(0);
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
    }}>{unseen} 个新更新 · 回到最新</button> : null}
  </div>;
}

export { Markdown, JsonPreview };
