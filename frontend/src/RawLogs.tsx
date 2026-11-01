import { useEffect, useRef, useState } from "react";
import { Database, RefreshCw } from "lucide-react";
import { api, rawLogsSchema, type RawLogs, type Run } from "./types";

type Row = RawLogs["rows"][number];
export type FieldKind =
  "match" | "id" | "time" | "tokens" | "result" | "caller";

export const kindLabels: Record<FieldKind, string> = {
  match: "关联 ID（已匹配）",
  id: "请求 ID",
  time: "时间与耗时",
  tokens: "Token 用量",
  result: "操作、结果与模型",
  caller: "调用方",
};

const keyFields: Record<string, FieldKind> = {
  correlationid: "id",
  requestid: "id",
  requestid_s: "id",
  request_id: "id",
  apimrequestid: "id",
  timegenerated: "time",
  ingested: "time",
  durationms: "time",
  requesttime: "time",
  timetofirsttokenms: "time",
  timetolasttokenms: "time",
  prompttokens: "tokens",
  completiontokens: "tokens",
  generatedtokens: "tokens",
  cachedtokens: "tokens",
  totaltokens: "tokens",
  category: "result",
  operationname: "result",
  resultsignature: "result",
  streamtype: "result",
  modeldeploymentname: "result",
  modelname: "result",
  modelversion: "result",
  calleripaddress: "caller",
  objectid: "caller",
  callerobjectid: "caller",
};

export function fieldKind(
  path: string,
  matched: Map<string, string>,
): FieldKind | null {
  if (matched.has(path)) return "match";
  return keyFields[path.split(".").at(-1)!.toLowerCase()] ?? null;
}

// Matching paths use the query alias "properties.x"; raw rows keep the column name.
export function matchedPaths(row: Row): Map<string, string> {
  return new Map(
    row.matching_fields.map((pair) => {
      const [path, header] = pair.split(" = ");
      return [path.replace(/^properties\./, "properties_s."), header ?? ""];
    }),
  );
}

function isContainer(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    (!Array.isArray(value) ||
      value.some((item) => typeof item === "object" && item !== null))
  );
}

function hasKeyField(
  value: unknown,
  path: string,
  matched: Map<string, string>,
): boolean {
  if (fieldKind(path, matched)) return true;
  return (
    isContainer(value) &&
    Object.entries(value).some(([key, item]) =>
      hasKeyField(item, `${path}.${key}`, matched),
    )
  );
}

function note(path: string, value: unknown, matched: Map<string, string>) {
  if (matched.has(path)) return `= ${matched.get(path)} ✓`;
  if (path === "Ingested") return "查询附加列：ingestion_time()";
  if (path === "properties_s.requestTime" && typeof value === "string")
    return "超出 JS 安全整数，以字符串保留精度";
  return "";
}

function Entry({
  name,
  value,
  path,
  depth,
  last,
  matched,
  keysOnly,
}: {
  name?: string;
  value: unknown;
  path: string;
  depth: number;
  last: boolean;
  matched: Map<string, string>;
  keysOnly: boolean;
}) {
  const kind = name === undefined ? null : fieldKind(path, matched);
  const className = `raw-line${kind ? ` hl hl-${kind}` : ""}`;
  const indent = { paddingLeft: `${depth * 14 + 6}px` };
  const label = name !== undefined && (
    <>
      <span className="raw-key">{JSON.stringify(name)}</span>
      {": "}
    </>
  );
  if (isContainer(value)) {
    const array = Array.isArray(value);
    const entries = Object.entries(value).filter(
      ([key, item]) =>
        !keysOnly || array || hasKeyField(item, `${path}.${key}`, matched),
    );
    return (
      <>
        <div className={className} style={indent}>
          {label}
          {array ? "[" : "{"}
        </div>
        {entries.map(([key, item], i) => (
          <Entry
            key={key}
            name={array ? undefined : key}
            value={item}
            path={`${path}.${key}`}
            depth={depth + 1}
            last={i === entries.length - 1}
            matched={matched}
            keysOnly={keysOnly}
          />
        ))}
        <div className="raw-line" style={indent}>
          {array ? "]" : "}"}
          {last ? "" : ","}
        </div>
      </>
    );
  }
  const remark = note(path, value, matched);
  return (
    <div
      className={className}
      style={indent}
      title={kind ? kindLabels[kind] : undefined}
    >
      {label}
      <span className={`raw-value ${typeof value}`}>
        {JSON.stringify(value)}
      </span>
      {last ? "" : ","}
      {remark && <span className="raw-note">← {remark}</span>}
    </div>
  );
}

function RawRow({ row, keysOnly }: { row: Row; keysOnly: boolean }) {
  const matched = matchedPaths(row);
  const columns = Object.entries(row.columns).filter(
    ([key, value]) => !keysOnly || hasKeyField(value, key, matched),
  );
  const operation = row.columns.OperationName;
  const time = row.columns.TimeGenerated;
  return (
    <article className="raw-row">
      <header>
        <strong>{row.category || "AzureDiagnostics"}</strong>
        {typeof operation === "string" && <code>{operation}</code>}
        {typeof time === "string" && <span>{time}</span>}
        <span className={`raw-badge ${matched.size ? "matched" : ""}`}>
          {row.matching_fields.join("、") || "无 ID 匹配字段"}
        </span>
        <small>
          {Object.keys(row.columns).length}/{row.column_count} 列有值
        </small>
      </header>
      <div className="raw-json">
        <div className="raw-line">{"{"}</div>
        {columns.map(([key, value], i) => (
          <Entry
            key={key}
            name={key}
            value={value}
            path={key}
            depth={1}
            last={i === columns.length - 1}
            matched={matched}
            keysOnly={keysOnly}
          />
        ))}
        <div className="raw-line">{"}"}</div>
      </div>
    </article>
  );
}

export function RawLogPanel({
  id,
  ready,
  checkedAt,
  load,
  idleText = "执行请求后显示本次调用的原始日志行。",
  hint = "按本次调用的请求 ID 只读查询 AzureDiagnostics 完整行并省略空列；仅在页面显示，不写入本地数据库，也不进入导出。",
}: {
  id?: string;
  ready: boolean;
  checkedAt?: string | null;
  load: (signal: AbortSignal) => Promise<RawLogs>;
  idleText?: string;
  hint?: string;
}) {
  const [data, setData] = useState<RawLogs | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [keysOnly, setKeysOnly] = useState(false);
  const [manual, setManual] = useState({ id: "", count: 0 });
  // Callers pass inline closures; only id/ready/checkedAt/refresh should refetch.
  const loader = useRef(load);
  useEffect(() => {
    loader.current = load;
  });
  const refresh = manual.id === id ? manual.count : 0;

  useEffect(() => {
    setData(null);
    setError("");
  }, [id]);

  useEffect(() => {
    if (!id || (!ready && !refresh)) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    loader
      .current(controller.signal)
      .then(setData)
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(String(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      setLoading(false);
    };
  }, [id, ready, checkedAt, refresh]);

  return (
    <section className="card raw-card" aria-label="原始 Log Analytics 日志">
      <div className="section-heading">
        <h2>
          <Database size={18} />
          原始 Log Analytics 日志
        </h2>
        <div className="actions">
          <label className="raw-toggle">
            <input
              type="checkbox"
              checked={keysOnly}
              onChange={(e) => setKeysOnly(e.target.checked)}
            />
            仅显示关键字段
          </label>
          <button
            className="button secondary small"
            disabled={!id || loading}
            onClick={() => setManual({ id: id ?? "", count: refresh + 1 })}
          >
            <RefreshCw size={14} />
            {loading ? "读取中…" : "刷新原始日志"}
          </button>
        </div>
      </div>
      <p className="hint">{hint}</p>
      <div className="raw-legend" aria-label="高亮图例">
        {(Object.keys(kindLabels) as FieldKind[]).map((kind) => (
          <span key={kind} className={`hl hl-${kind}`}>
            {kindLabels[kind]}
          </span>
        ))}
      </div>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {!id ? (
        <p className="quiet">{idleText}</p>
      ) : !data ? (
        <p className="quiet">
          {loading
            ? "正在读取原始日志…"
            : "关联到日志记录后自动读取；也可手动刷新。"}
        </p>
      ) : (
        <>
          <div
            className={`correlation-message ${data.state === "ok" ? "matched" : ""}`}
          >
            <Database size={16} />
            <span>
              {data.message}
              {data.checked_at &&
                ` · ${new Date(data.checked_at).toLocaleTimeString()}`}
            </span>
          </div>
          {data.rows.map((row, i) => (
            <RawRow key={i} row={row} keysOnly={keysOnly} />
          ))}
          {data.query && (
            <details className="raw-query">
              <summary>原始行查询 KQL</summary>
              <pre className="json">{data.query}</pre>
            </details>
          )}
        </>
      )}
    </section>
  );
}

export function RunRawLogs({ run }: { run: Run | null }) {
  return (
    <RawLogPanel
      id={run?.id}
      ready={!!run && run.logs.records.length > 0}
      checkedAt={run?.logs.checked_at}
      load={(signal) =>
        api(`/runs/${run?.id}/raw-logs`, rawLogsSchema, {}, signal)
      }
    />
  );
}
