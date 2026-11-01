import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ExternalLink,
  Fingerprint,
  ListFilter,
  RefreshCw,
  Search,
} from "lucide-react";
import { Timeline } from "./Timeline";
import { LogDiscovery } from "./LogDiscovery";
import { ThroughputBreakdown } from "./Throughput";
import { RawLogPanel } from "./RawLogs";
import { formatTps, tokenCount, tpsBases, type TpsInput } from "./metrics";
import {
  api,
  candidatesSchema,
  eventName,
  lookupResourcesSchema,
  milliseconds,
  rawLogsSchema,
  resourceName,
  traceSchema,
  type Candidate,
  type Candidates,
  type Connection,
  type LookupResources,
  type Trace,
} from "./types";
import { Badge, Json } from "./ui";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const CANDIDATE_DAYS = 7;

type Outcome = "all" | "success" | "failure";
type Filters = {
  start: string;
  end: string;
  scope: string;
  deployment: string;
  object_id: string;
  outcome: Outcome;
  inference_only: boolean;
};

// <input type="datetime-local"> values are wall-clock strings without an offset.
export function localInput(ms: number): string {
  const offset = new Date(ms).getTimezoneOffset() * 60_000;
  return new Date(ms - offset).toISOString().slice(0, 16);
}

export function isoFromLocal(value: string): string | null {
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

function shortId(value: string): string {
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function candidateTps(row: Candidate): number | null {
  return row.completion_tokens !== null &&
    row.duration_ms !== null &&
    row.duration_ms > 0
    ? (row.completion_tokens * 1000) / row.duration_ms
    : null;
}

function ScopeSelect({
  value,
  onChange,
  resources,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  resources: LookupResources | null;
  label: string;
}) {
  const known = resources?.resources ?? [];
  return (
    <label>
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">整个 Workspace（所有 Foundry）</option>
        {known.map((item) => (
          <option key={item.resource_id} value={item.resource_id}>
            {item.name || resourceName(item.resource_id)} · {item.inference}{" "}
            次模型调用
          </option>
        ))}
        {value && !known.some((item) => item.resource_id === value) && (
          <option value={value}>{resourceName(value)}</option>
        )}
      </select>
    </label>
  );
}

export function Lookup({
  connection,
  onOpenRun,
}: {
  connection: Connection | null;
  onOpenRun: (id: string) => void;
}) {
  const [tab, setTab] = useState<"id" | "search">("id");
  const [resources, setResources] = useState<LookupResources | null>(null);
  const [resourceRefresh, setResourceRefresh] = useState(0);
  const [requestId, setRequestId] = useState("");
  const [idScope, setIdScope] = useState("");
  const [days, setDays] = useState(30);
  const [filters, setFilters] = useState<Filters>(() => ({
    start: localInput(Date.now() - HOUR),
    end: localInput(Date.now()),
    scope: "",
    deployment: "",
    object_id: "",
    outcome: "all",
    inference_only: true,
  }));
  const [candidates, setCandidates] = useState<Candidates | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const current = useRef(connection);
  useEffect(() => {
    current.current = connection;
  });
  const workspace = connection?.workspace_id ?? "";
  const identity = connection?.logs_auth_mode ?? "";

  useEffect(() => {
    if (!workspace || !current.current) return;
    const controller = new AbortController();
    api(
      "/lookup/resources",
      lookupResourcesSchema,
      { connection: current.current },
      controller.signal,
    )
      .then(setResources)
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setError(`读取 Foundry 列表失败：${String(err)}`);
      });
    return () => controller.abort();
  }, [workspace, identity, resourceRefresh]);

  async function work(name: string, task: () => Promise<void>) {
    setBusy(name);
    setError("");
    try {
      await task();
    } catch (err: unknown) {
      setError(String(err));
    } finally {
      setBusy("");
    }
  }

  function lookupTrace(
    id: string,
    scope: string,
    start: number,
    end: number,
  ): Promise<void> {
    return work("trace", async () => {
      setTrace(
        await api("/lookup/trace", traceSchema, {
          connection,
          request_id: id.trim(),
          scope,
          start: new Date(start).toISOString(),
          end: new Date(end).toISOString(),
        }),
      );
    });
  }

  function search() {
    const start = isoFromLocal(filters.start);
    const end = isoFromLocal(filters.end);
    if (!start || !end || start >= end) {
      setError("请填写有效的开始和结束时间，且开始早于结束。");
      return;
    }
    if (Date.parse(end) - Date.parse(start) > CANDIDATE_DAYS * DAY) {
      setError(`候选搜索的时间范围最长 ${CANDIDATE_DAYS} 天。`);
      return;
    }
    void work("search", async () => {
      setCandidates(
        await api("/lookup/candidates", candidatesSchema, {
          connection,
          scope: filters.scope,
          start,
          end,
          deployment: filters.deployment.trim(),
          object_id: filters.object_id.trim(),
          outcome: filters.outcome,
          inference_only: filters.inference_only,
        }),
      );
    });
  }

  function preset(hours: number) {
    const end = Date.now();
    setFilters((value) => ({
      ...value,
      start: localInput(end - hours * HOUR),
      end: localInput(end),
    }));
  }

  function openCandidate(row: Candidate) {
    const at = Date.parse(row.time);
    setRequestId(row.request_id);
    void lookupTrace(
      row.request_id,
      row.resource_id,
      at - HOUR,
      Math.min(at + HOUR, Date.now()),
    );
  }

  const unavailable = !connection?.workspace_id;

  return (
    <div className="lookup">
      <section className="card lookup-card" aria-label="回查历史调用">
        <div className="section-heading">
          <h2>
            <Search size={18} />
            回查历史调用
          </h2>
          <span className="quiet">只读查询 · 不调用模型 · 结果不保存</span>
        </div>
        <div className="tabs lookup-tabs" role="tablist" aria-label="回查方式">
          <button
            role="tab"
            aria-selected={tab === "id"}
            onClick={() => setTab("id")}
          >
            按 apim-request-id
          </button>
          <button
            role="tab"
            aria-selected={tab === "search"}
            onClick={() => setTab("search")}
          >
            按条件找候选
          </button>
        </div>
        {unavailable && (
          <p className="hint">需要在左侧配置 Log Analytics Workspace ID。</p>
        )}
        {tab === "id" ? (
          <form
            className="lookup-form"
            onSubmit={(e) => {
              e.preventDefault();
              const end = Date.now();
              void lookupTrace(requestId, idScope, end - days * DAY, end);
            }}
          >
            <label className="wide">
              apim-request-id
              <input
                value={requestId}
                onChange={(e) => setRequestId(e.target.value)}
                placeholder="例如 3f6c2a1e-9b7d-4e58-a0c4-1d2e3f4a5b6c"
                maxLength={200}
                spellCheck={false}
              />
            </label>
            <ScopeSelect
              label="范围"
              value={idScope}
              onChange={setIdScope}
              resources={resources}
            />
            <label>
              时间
              <select
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
              >
                <option value={1}>最近 24 小时</option>
                <option value={7}>最近 7 天</option>
                <option value={30}>最近 30 天（保留期上限）</option>
              </select>
            </label>
            <div className="lookup-actions">
              <button
                className="button primary"
                type="submit"
                disabled={unavailable || !requestId.trim() || !!busy}
              >
                <Fingerprint size={15} />
                {busy === "trace" ? "回查中…" : "回查"}
              </button>
            </div>
            <p className="hint wide">
              只有 Azure 响应头 apim-request-id 会写入日志（CorrelationId）。经
              LiteLLM 时请填响应头 llm_provider-apim-request-id；x-request-id、
              chatcmpl-… 和客户端请求 ID 都查不到。
            </p>
          </form>
        ) : (
          <form
            className="lookup-form"
            onSubmit={(e) => {
              e.preventDefault();
              search();
            }}
          >
            <label>
              开始（本地时间）
              <input
                type="datetime-local"
                value={filters.start}
                onChange={(e) =>
                  setFilters({ ...filters, start: e.target.value })
                }
              />
            </label>
            <label>
              结束（本地时间）
              <input
                type="datetime-local"
                value={filters.end}
                onChange={(e) =>
                  setFilters({ ...filters, end: e.target.value })
                }
              />
            </label>
            <div className="presets" aria-label="快捷时间">
              {[1, 24, 24 * 7].map((hours) => (
                <button
                  key={hours}
                  type="button"
                  className="button secondary small"
                  onClick={() => preset(hours)}
                >
                  {hours === 1 ? "1 小时" : hours === 24 ? "24 小时" : "7 天"}
                </button>
              ))}
            </div>
            <ScopeSelect
              label="范围"
              value={filters.scope}
              onChange={(scope) => setFilters({ ...filters, scope })}
              resources={resources}
            />
            <label>
              部署名
              <input
                value={filters.deployment}
                onChange={(e) =>
                  setFilters({ ...filters, deployment: e.target.value })
                }
                placeholder="可选，如 gpt-5.4-nano"
                maxLength={64}
              />
            </label>
            <label>
              调用方 objectId
              <input
                value={filters.object_id}
                onChange={(e) =>
                  setFilters({ ...filters, object_id: e.target.value })
                }
                placeholder="可选，Entra 身份 GUID"
                spellCheck={false}
              />
            </label>
            <label>
              结果
              <select
                value={filters.outcome}
                onChange={(e) =>
                  setFilters({
                    ...filters,
                    outcome: e.target.value as Outcome,
                  })
                }
              >
                <option value="all">全部</option>
                <option value="success">成功（2xx）</option>
                <option value="failure">失败（非 2xx）</option>
              </select>
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={filters.inference_only}
                onChange={(e) =>
                  setFilters({ ...filters, inference_only: e.target.checked })
                }
              />
              仅模型调用（带 modelDeploymentName）
            </label>
            <div className="lookup-actions">
              <button
                className="button primary"
                type="submit"
                disabled={unavailable || !!busy}
              >
                <ListFilter size={15} />
                {busy === "search" ? "搜索中…" : "搜索候选"}
              </button>
            </div>
            <p className="hint wide">
              最长 {CANDIDATE_DAYS} 天、最多 50 条。日志中没有可区分 API Key
              的字段：Key 调用通常没有
              objectId，只能按时间、部署、结果缩小范围。
            </p>
          </form>
        )}
        <div className="scope-note">
          <span>
            {resources
              ? resources.message
              : unavailable
                ? ""
                : "正在读取向该 Workspace 写日志的 Foundry…"}
          </span>
          <button
            type="button"
            className="icon-button"
            aria-label="刷新 Foundry 列表"
            disabled={unavailable}
            onClick={() => setResourceRefresh((n) => n + 1)}
          >
            <RefreshCw size={14} />
          </button>
        </div>
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
      </section>

      {tab === "search" && candidates && (
        <CandidateTable
          data={candidates}
          busy={!!busy}
          active={trace?.request_id}
          onTrace={openCandidate}
        />
      )}

      {trace ? (
        <TraceView
          trace={trace}
          connection={connection}
          onOpenRun={onOpenRun}
        />
      ) : (
        <div className="card empty-evidence lookup-empty">
          <Fingerprint size={30} />
          <div>
            <strong>用 apim-request-id 回放任意一次调用</strong>
            <p>
              包括其他客户端或经 LiteLLM
              发起的调用；只要日志仍在保留期内，不需要知道调用时间。
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

function CandidateTable({
  data,
  busy,
  active,
  onTrace,
}: {
  data: Candidates;
  busy: boolean;
  active?: string;
  onTrace: (row: Candidate) => void;
}) {
  return (
    <section className="card candidates-card" aria-label="候选调用">
      <div className="section-heading">
        <h2>
          <ListFilter size={18} />
          候选调用
        </h2>
        <span className="quiet">
          {data.rows.length} 条{data.limited ? " · 已截断" : ""}
        </span>
      </div>
      <div
        className={`correlation-message ${data.state === "ok" ? "matched" : ""}`}
      >
        <Activity size={16} />
        <span>{data.message}</span>
      </div>
      {data.rows.length > 0 && (
        <div className="table-scroll">
          <table className="candidates">
            <thead>
              <tr>
                <th>时间</th>
                <th>Foundry</th>
                <th>部署 / 模型</th>
                <th>结果</th>
                <th>服务端耗时</th>
                <th>输入 / 输出 token</th>
                <th>Azure TPS</th>
                <th>objectId</th>
                <th>IP</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row, i) => (
                <tr
                  key={`${row.request_id}-${i}`}
                  className={
                    active && row.request_id === active ? "current" : ""
                  }
                >
                  <td>
                    {new Date(row.time).toLocaleString()}
                    {row.local_run_id && <span className="tag">本应用</span>}
                  </td>
                  <td title={row.resource_id}>{row.resource || "—"}</td>
                  <td>
                    {row.deployment || "—"}
                    {row.model && row.model !== row.deployment && (
                      <small>{row.model}</small>
                    )}
                  </td>
                  <td>
                    <span
                      className={`badge ${row.result.startsWith("2") ? "matched" : "failed"}`}
                    >
                      {row.result || "—"}
                    </span>
                  </td>
                  <td>
                    {row.duration_ms === null
                      ? "—"
                      : milliseconds(row.duration_ms)}
                  </td>
                  <td>
                    {row.prompt_tokens ?? "—"} / {row.completion_tokens ?? "—"}
                  </td>
                  <td>{formatTps(candidateTps(row))}</td>
                  <td title={row.object_id}>
                    <code>{row.object_id ? shortId(row.object_id) : "—"}</code>
                  </td>
                  <td>{row.caller_ip || "—"}</td>
                  <td>
                    <button
                      type="button"
                      className="button secondary small"
                      disabled={busy || !row.request_id}
                      onClick={() => onTrace(row)}
                      aria-label={`回查 ${row.request_id}`}
                    >
                      回查
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.query && (
        <details className="raw-query">
          <summary>候选查询 KQL</summary>
          <pre className="json">{data.query}</pre>
        </details>
      )}
    </section>
  );
}

function TraceView({
  trace,
  connection,
  onOpenRun,
}: {
  trace: Trace;
  connection: Connection | null;
  onOpenRun: (id: string) => void;
}) {
  const [selected, setSelected] = useState("");
  const request = trace.logs.records.find(
    (record) => record.category === "RequestResponse",
  );
  const input: TpsInput = {
    usage: {},
    duration_ms: null,
    events: trace.events,
    logs: trace.logs,
    external: true,
  };
  const azureTps =
    tpsBases(input).find((item) => item.key === "azure_request")?.value ?? null;
  const duration = tokenCount(request?.fields.DurationMs);
  const prompt = tokenCount(request?.fields.promptTokens);
  const completion = tokenCount(request?.fields.completionTokens);
  const event = trace.events.find((item) => item.id === selected);
  const scope = trace.scope ? resourceName(trace.scope) : "整个 Workspace";

  return (
    <>
      <section className="card evidence-card trace-card" aria-label="回查结果">
        <div className="section-heading">
          <h2>
            <Fingerprint size={18} />
            回查结果
          </h2>
          <div className="actions">
            <span className={`tag ${trace.local_run_id ? "local" : ""}`}>
              {trace.local_run_id ? "本应用发起" : "外部调用"}
            </span>
            {trace.local_run_id && (
              <button
                className="button secondary small"
                onClick={() => onOpenRun(trace.local_run_id!)}
              >
                <ExternalLink size={14} />
                打开本地实验
              </button>
            )}
          </div>
        </div>
        <div className="id-strip">
          <span>apim-request-id</span>
          <code>{trace.request_id}</code>
        </div>
        <div className="id-strip">
          <span>Foundry</span>
          <code>
            {trace.resources.length
              ? trace.resources.map(resourceName).join("、")
              : "—"}
          </code>
        </div>
        <div className="id-strip">
          <span>查询范围</span>
          <code>
            {scope} · {new Date(trace.start).toLocaleString()} →{" "}
            {new Date(trace.end).toLocaleString()}
          </code>
        </div>
        <div className={`correlation-message ${trace.logs.state}`}>
          <Activity size={16} />
          <span>{trace.logs.message}</span>
        </div>
      </section>

      <div className="metrics">
        <div className="metric">
          <span>结果</span>
          <strong>{String(request?.fields.ResultSignature ?? "—")}</strong>
          <small>
            {String(request?.fields.OperationName ?? "RequestResponse")}
          </small>
        </div>
        <div className="metric">
          <span>服务端耗时</span>
          <strong>{duration === null ? "—" : milliseconds(duration)}</strong>
          <small>RequestResponse.DurationMs</small>
        </div>
        <div className="metric">
          <span>输入 / 输出 token</span>
          <strong>
            {prompt ?? "—"} / {completion ?? "—"}
          </strong>
          <small>来自 Azure 请求日志</small>
        </div>
        <div className="metric">
          <span>输出 TPS · Azure</span>
          <strong>
            {azureTps === null ? (
              "—"
            ) : (
              <>
                {azureTps.toFixed(1)}
                <em> tok/s</em>
              </>
            )}
          </strong>
          <small>completionTokens ÷ DurationMs</small>
        </div>
        <div className="metric">
          <span>日志状态</span>
          <strong>
            <Badge state={trace.logs.state} />
          </strong>
          <small>{trace.logs.records.length} 条记录</small>
        </div>
      </div>

      <div className="card timeline-card">
        <Timeline
          events={trace.events}
          selected={selected}
          onSelect={(item) => setSelected(item.id)}
          emptyText="没有可绘制的 Azure 服务端阶段。"
        />
        <p className="hint lookup-note">
          回查没有客户端阶段：时间轴只包含 Azure
          日志中的服务端区间和用量日志阶段。
        </p>
        <ThroughputBreakdown run={input} />
        <LogDiscovery
          logs={trace.logs}
          events={trace.events}
          usageExpected={
            !request ||
            String(request.fields.ResultSignature ?? "").startsWith("2")
          }
          selected={selected}
          onSelect={(item) => setSelected(item.id)}
        />
      </div>

      <section className="card details-card" aria-label="回查详情">
        <div className="detail-content">
          {event ? (
            <>
              <h3>{eventName(event.title)}</h3>
              <Badge state={event.status} />
              <Json value={event} />
            </>
          ) : (
            <p className="quiet">点击时间轴，查看该阶段的日志字段与来源。</p>
          )}
          <details className="raw-query">
            <summary>Azure 日志记录（{trace.logs.records.length}）</summary>
            {trace.logs.records.map((record, i) => (
              <details key={i} className="log-record">
                <summary>
                  {record.category || "Diagnostic"} · {record.timestamp}
                </summary>
                <Json value={record.fields} />
              </details>
            ))}
          </details>
          <details className="raw-query">
            <summary>回查 KQL</summary>
            <pre className="json">{trace.logs.query}</pre>
          </details>
        </div>
      </section>

      <RawLogPanel
        id={trace.raw_start ? trace.request_id : undefined}
        ready={!!trace.raw_start}
        checkedAt={trace.logs.checked_at}
        load={(signal) =>
          api(
            "/lookup/raw-logs",
            rawLogsSchema,
            {
              connection,
              request_id: trace.request_id,
              scope: trace.raw_scope,
              start: trace.raw_start ?? trace.start,
              end: trace.raw_end ?? trace.end,
            },
            signal,
          )
        }
        idleText="回查到日志记录后显示原始行；未找到的 ID 不读取原始日志。"
        hint="按 apim-request-id 只读查询 AzureDiagnostics 完整行（时间窗口收窄到记录前后 5 分钟）；仅在页面显示，不写入本地数据库。"
      />
    </>
  );
}
