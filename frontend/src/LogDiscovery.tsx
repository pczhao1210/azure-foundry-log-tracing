import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  Clock3,
  MinusCircle,
  XCircle,
} from "lucide-react";
import { tokenCount } from "./metrics";
import {
  milliseconds,
  resourceName,
  type LogResult,
  type TimelineEvent,
} from "./types";

type Status =
  "found" | "candidate" | "waiting" | "missing" | "skipped" | "idle" | "error";
type LogRecord = LogResult["records"][number];

const USAGE = "AzureOpenAIRequestUsage";
const KNOWN = [
  {
    category: "RequestResponse",
    label: "请求日志",
    about: "每次调用一条：操作、结果码、服务端耗时、Token。",
  },
  {
    category: USAGE,
    label: "用量日志",
    about: "模型用量：部署、Token 数、首/末 token 耗时。",
  },
];

const statusText: { [key in Status]: string } = {
  found: "已找到",
  candidate: "候选 · 待核验",
  waiting: "等待入库",
  missing: "未找到",
  skipped: "不适用",
  idle: "未查询",
  error: "查询失败",
};

const statusNote: { [key in Status]: string } = {
  found: "",
  candidate: "",
  waiting: "各类日志入库延迟不同，通常 1–3 分钟后可查询。",
  missing: "已查询，但所选范围内没有该类日志。",
  skipped: "调用失败或被取消，通常不产生用量日志。",
  idle: "",
  error: "查询出错，详见关联说明。",
};

function StatusIcon({ status }: { status: Status }) {
  const props = { size: 16, className: `discovery-icon ${status}` };
  if (status === "found") return <CheckCircle2 {...props} />;
  if (status === "candidate") return <AlertCircle {...props} />;
  if (status === "waiting") return <Clock3 {...props} />;
  if (status === "missing" || status === "error") return <XCircle {...props} />;
  if (status === "skipped") return <MinusCircle {...props} />;
  return <CircleDashed {...props} />;
}

function categoryStatus(
  records: LogRecord[],
  logs: LogResult | null,
  optional: boolean,
): Status {
  if (records.length)
    return logs?.state === "candidate" ? "candidate" : "found";
  if (!logs || logs.state === "idle" || logs.state === "unavailable")
    return "idle";
  if (optional) return "skipped";
  if (logs.polling || logs.state === "querying") return "waiting";
  if (logs.state === "error") return "error";
  return "missing";
}

function tokens(value: unknown): string | null {
  const count = tokenCount(value);
  return count === null ? null : String(count);
}

function facts(record: LogRecord): [string, string][] {
  const fields = record.fields;
  const text = (value: unknown) =>
    typeof value === "string" || typeof value === "number"
      ? String(value)
      : null;
  const ms = (value: unknown) => {
    const n = tokenCount(value);
    return n === null ? null : milliseconds(n);
  };
  const usage = record.category === USAGE;
  const input = tokens(fields.promptTokens);
  const output = tokens(
    usage ? fields.generatedTokens : fields.completionTokens,
  );
  const rows: [string, string | null][] = [
    ["操作", text(fields.OperationName)],
    ["结果", usage ? null : text(fields.ResultSignature)],
    ["服务端耗时", usage ? null : ms(fields.DurationMs)],
    ["部署", text(fields.modelDeploymentName)],
    ["Token", input || output ? `${input ?? "—"} → ${output ?? "—"}` : null],
    ["末 token", usage ? ms(fields.timeToLastTokenMs) : null],
    ["Foundry", record.resource ? resourceName(record.resource) : null],
  ];
  return rows.filter((row): row is [string, string] => row[1] !== null);
}

function seconds(ms: number): string {
  return ms >= 10_000 ? `${Math.round(ms / 1000)} s` : milliseconds(ms);
}

function queryResult(event: TimelineEvent): string {
  if (event.status === "running") return "查询中";
  if (event.status === "error") return "失败";
  if (event.status === "cancelled") return "已停止";
  const count = event.details.record_count;
  return typeof count === "number"
    ? count
      ? `${count} 条`
      : "无结果"
    : "完成";
}

export function LogDiscovery({
  logs,
  events,
  usageExpected = true,
  selected,
  onSelect,
}: {
  logs: LogResult | null;
  events: TimelineEvent[];
  usageExpected?: boolean;
  selected?: string;
  onSelect: (event: TimelineEvent) => void;
}) {
  const records = logs?.records ?? [];
  const extra = [...new Set(records.map((record) => record.category))]
    .filter((category) => !KNOWN.some((item) => item.category === category))
    .map((category) => ({
      category,
      label: category || "其他诊断日志",
      about: "",
    }));
  const items = [...KNOWN, ...extra].map((item) => {
    const matched = records.filter(
      (record) => record.category === item.category,
    );
    const optional = item.category === USAGE && !usageExpected;
    return {
      ...item,
      records: matched,
      optional,
      status: categoryStatus(matched, logs, optional),
    };
  });
  const needed = items.filter((item) => !item.optional);
  const found = needed.filter((item) => item.status === "found").length;
  const candidates = items.filter((item) => item.status === "candidate").length;
  const ingestion = events.filter(
    (event) => event.lane === "logs" && event.source === "azure",
  );
  const queries = events.filter(
    (event) =>
      event.source === "log_query" &&
      event.title === "Read-only Log Analytics query",
  );
  const waiting = events.find(
    (event) =>
      event.source === "log_query" &&
      event.title === "Waiting before next log query" &&
      event.status === "running",
  );

  return (
    <section className="timeline-section discovery" aria-label="日志发现">
      <div className="section-heading">
        <div>
          <span className="eyebrow">LOG DISCOVERY</span>
          <h3>日志发现</h3>
        </div>
        <span className="quiet">
          {found}/{needed.length} 类日志已找到
          {candidates ? ` · ${candidates} 类待核验` : ""}
          {logs?.attempts ? ` · 查询 ${logs.attempts} 次` : ""}
        </span>
      </div>
      <div className="discovery-grid">
        {items.map((item) => {
          const record = item.records[0];
          const event =
            record &&
            ingestion.find(
              (candidate) =>
                candidate.details.category === item.category &&
                candidate.details.TimeGenerated === record.timestamp,
            );
          const since = event?.details.since_run_start_ms;
          const delay =
            record?.ingested_at && Number.isFinite(Date.parse(record.timestamp))
              ? Date.parse(record.ingested_at) - Date.parse(record.timestamp)
              : null;
          const body = (
            <>
              <span className="discovery-head">
                <StatusIcon status={item.status} />
                <span>
                  <strong>{item.label}</strong>
                  <small>{item.category || "未知类别"}</small>
                </span>
                <span className={`discovery-status ${item.status}`}>
                  {statusText[item.status]}
                  {item.records.length > 1
                    ? ` · ${item.records.length} 条`
                    : ""}
                </span>
              </span>
              {record ? (
                <>
                  {record.matching_fields.length > 0 && (
                    <span className="discovery-evidence">
                      <code>
                        {record.matching_fields[0]}
                        {item.status === "found" ? " ✓" : ""}
                      </code>
                    </span>
                  )}
                  <span className="discovery-facts">
                    {facts(record).map(([label, value]) => (
                      <span key={label}>
                        <small>{label}</small>
                        {value}
                      </span>
                    ))}
                  </span>
                  {(typeof since === "number" || delay !== null) && (
                    <span className="discovery-note">
                      {typeof since === "number"
                        ? `请求开始后约 ${seconds(since)} 可查询`
                        : `入库耗时约 ${seconds(delay ?? 0)}（TimeGenerated → ingestion_time）`}
                    </span>
                  )}
                </>
              ) : (
                <span className="discovery-note">
                  {statusNote[item.status] || item.about}
                </span>
              )}
            </>
          );
          return event ? (
            <button
              key={item.category}
              type="button"
              className={`discovery-item ${item.status} ${selected === event.id ? "selected" : ""}`}
              aria-pressed={selected === event.id}
              onClick={() => onSelect(event)}
            >
              {body}
            </button>
          ) : (
            <div
              key={item.category}
              className={`discovery-item ${item.status}`}
            >
              {body}
            </div>
          );
        })}
      </div>
      {(queries.length > 0 || waiting) && (
        <div className="discovery-steps" aria-label="查询过程">
          <span>查询过程</span>
          {queries.map((event, i) => (
            <button
              key={event.id}
              type="button"
              className={`${event.status} ${
                typeof event.details.record_count === "number" &&
                event.details.record_count > 0
                  ? "hit"
                  : ""
              }`}
              aria-pressed={selected === event.id}
              onClick={() => onSelect(event)}
            >
              第 {i + 1} 次 · {queryResult(event)}
            </button>
          ))}
          {waiting && (
            <button
              type="button"
              className="waiting"
              aria-pressed={selected === waiting.id}
              onClick={() => onSelect(waiting)}
            >
              等待下一次查询…
            </button>
          )}
        </div>
      )}
      <p className="legend">
        按日志类别显示是否找到；只关联共享同一请求 ID
        的记录。入库耗时仅供参考，不计入模型执行耗时。
      </p>
    </section>
  );
}
