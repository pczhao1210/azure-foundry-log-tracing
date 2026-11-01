import { useEffect, useState } from "react";
import {
  eventName,
  eventSource,
  latencyLabels,
  milliseconds,
  type TimelineEvent,
} from "./types";

export function timelineGeometry(events: TimelineEvent[], clock: number) {
  const dated = events
    .filter((event) => Number.isFinite(Date.parse(event.started_at)))
    .sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at));
  const start = Math.min(...dated.map((event) => Date.parse(event.started_at)));
  const end = Math.max(
    ...dated.map(
      (event) =>
        Date.parse(event.started_at) +
        (event.duration_ms ??
          (event.status === "running"
            ? Math.max(0, clock - Date.parse(event.started_at))
            : 0)),
    ),
  );
  return { dated, start, span: Math.max(1, end - start) };
}

export function Timeline({
  events,
  lane,
  selected,
  onSelect,
  emptyText = "执行请求后展示真实阶段；未采集的内部阶段不会被推测。",
}: {
  events: TimelineEvent[];
  lane: "call" | "logs";
  selected?: string;
  onSelect: (event: TimelineEvent) => void;
  emptyText?: string;
}) {
  const [clock, setClock] = useState(Date.now());
  const rows = events.filter((event) => event.lane === lane);
  const running = rows.some((event) => event.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setClock(Date.now()), 250);
    return () => clearInterval(timer);
  }, [running]);
  const { dated, start, span } = timelineGeometry(rows, clock);
  return (
    <section
      className="timeline-section"
      aria-label={lane === "call" ? "调用时间轴" : "日志发现时间轴"}
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">
            {lane === "call" ? "EXECUTION" : "LOG DISCOVERY"}
          </span>
          <h3>{lane === "call" ? "调用时间轴" : "日志发现时间轴"}</h3>
        </div>
        <span className="quiet">
          {dated.length ? `${dated.length} 个阶段 / 事件` : "等待运行"}
        </span>
      </div>
      {!dated.length ? (
        <div className="empty-chart">{emptyText}</div>
      ) : (
        <div className="timeline-scroll">
          <div className="timeline-axis">
            <span>阶段 / 数据来源</span>
            <div>
              {[0, 0.25, 0.5, 0.75, 1].map((n) => (
                <span key={n} style={{ left: `${n * 100}%` }}>
                  {milliseconds(span * n)}
                </span>
              ))}
            </div>
          </div>
          {dated.map((event) => {
            const offset =
              ((Date.parse(event.started_at) - start) / span) * 100;
            const duration =
              event.duration_ms ??
              (event.status === "running"
                ? Math.max(0, clock - Date.parse(event.started_at))
                : 0);
            return (
              <button
                key={event.id}
                type="button"
                className={`timeline-row ${selected === event.id ? "selected" : ""}`}
                aria-pressed={selected === event.id}
                onClick={() => onSelect(event)}
              >
                <span className="row-label">
                  <strong>{eventName(event.title)}</strong>
                  <small>{eventSource(event)}</small>
                </span>
                <span className="track">
                  <span
                    className={`bar ${event.status} ${event.source} ${event.kind}`}
                    style={{
                      left: `${Math.min(offset, 99.5)}%`,
                      width:
                        event.kind === "point"
                          ? 10
                          : `${Math.max((duration / span) * 100, 0.5)}%`,
                    }}
                    title={`${eventName(event.title)} · ${event.started_at}`}
                  />
                  <span className="duration">
                    {event.kind === "point"
                      ? "事件"
                      : event.duration_ms === null && event.status !== "running"
                        ? "耗时未知"
                        : milliseconds(duration)}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      {lane === "call" && (
        <p className="legend">
          <i className="legend-blue" />
          本地实测区间
          <i className="legend-purple" />
          Azure 诊断日志（服务时钟，可能存在时钟偏差）
          <span>用量日志阶段起点按 requestTime 近似对齐</span>
        </p>
      )}
      {lane === "logs" && (
        <p className="legend">
          <i className="legend-blue" />
          本地查询与等待
          <i className="legend-purple" />
          Azure 入库时间 ingestion_time()
          <span>不计入模型执行耗时</span>
        </p>
      )}
    </section>
  );
}

export function LatencyBreakdown({
  usage,
}: {
  usage: Record<string, unknown>;
}) {
  const checkpoint = usage.latency_checkpoint;
  if (!checkpoint || typeof checkpoint !== "object") return null;
  const rows = Object.entries(checkpoint as Record<string, unknown>).filter(
    (entry): entry is [string, number] =>
      typeof entry[1] === "number" && entry[1] >= 0,
  );
  if (!rows.length) return null;
  rows.sort((a, b) => a[1] - b[1]);
  // *_tbt_ms is a per-token interval, not elapsed time, so it is not plotted.
  const intervals = rows.filter(([key]) => key.endsWith("_tbt_ms"));
  const milestones = rows.filter(([key]) => !key.endsWith("_tbt_ms"));
  const span = Math.max(1, ...milestones.map(([, value]) => value));
  return (
    <section
      className="timeline-section latency-section"
      aria-label="服务端自报阶段"
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">SERVICE REPORTED</span>
          <h3>服务端自报阶段（响应体 latency_checkpoint）</h3>
        </div>
        <span className="quiet">{milestones.length} 个耗时值 · 相对值</span>
      </div>
      <div className="timeline-scroll">
        {milestones.map(([key, value]) => (
          <div className="timeline-row static" key={key}>
            <span className="row-label">
              <strong>{latencyLabels[key] ?? key}</strong>
              <small>{key}</small>
            </span>
            <span className="track">
              <span
                className="bar observed service"
                style={{
                  left: 0,
                  width: `${Math.max((value / span) * 100, 0.5)}%`,
                }}
              />
              <span className="duration">{milliseconds(value)}</span>
            </span>
          </div>
        ))}
      </div>
      {intervals.length > 0 && (
        <div className="meta-grid" aria-label="token 间隔">
          {intervals.map(([key, value]) => (
            <span key={key}>
              <small>{key}</small>
              {latencyLabels[key] ?? key}：{milliseconds(value)}
            </span>
          ))}
        </div>
      )}
      <p className="legend">
        Azure 未公开这些字段的定义：耗时值按大小排列，各自起点不一定相同；token
        间隔单独列出。均无绝对时间，因此不与上方时间轴对齐。
      </p>
    </section>
  );
}
