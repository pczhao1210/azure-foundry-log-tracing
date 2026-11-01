import { formatTps, sourceLabels, tpsBases, type TpsInput } from "./metrics";
import { milliseconds } from "./types";

export function ThroughputBreakdown({ run }: { run: TpsInput | null }) {
  if (!run) return null;
  const bases = tpsBases(run);
  const max = Math.max(1, ...bases.map((item) => item.value ?? 0));
  const available = bases.filter((item) => item.value !== null).length;
  const details = run.usage.completion_tokens_details;
  const reasoning =
    typeof details === "object" && details !== null
      ? (details as Record<string, unknown>).reasoning_tokens
      : undefined;
  return (
    <section
      className="timeline-section throughput-section"
      aria-label="TPS 对比"
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">THROUGHPUT</span>
          <h3>TPS · 同一请求的不同时间基准</h3>
        </div>
        <span className="quiet">
          {available}/{bases.length} 个口径可计算 · 输出 token ÷ 时间
        </span>
      </div>
      <div className="timeline-scroll">
        {bases.map((item) => (
          <div
            className="timeline-row static"
            key={item.key}
            title={item.formula}
          >
            <span className="row-label">
              <strong>{item.label}</strong>
              <small>
                {item.value === null || item.ms === null
                  ? item.missing
                  : `${sourceLabels[item.source]} · ${item.tokens} tok ÷ ${milliseconds(item.ms)}`}
              </small>
            </span>
            <span className="track">
              {item.value !== null && (
                <span
                  className={`bar observed ${item.source}`}
                  style={{
                    left: 0,
                    width: `${Math.max((item.value / max) * 100, 0.5)}%`,
                  }}
                />
              )}
              <span className="duration">{formatTps(item.value)}</span>
            </span>
          </div>
        ))}
      </div>
      <details className="tps-formulas">
        <summary>计算口径</summary>
        <ul>
          {bases.map((item) => (
            <li key={item.key}>
              <strong>{item.label}</strong>
              <span>{sourceLabels[item.source]}</span>
              <code>{item.formula}</code>
            </li>
          ))}
        </ul>
      </details>
      <p className="legend">
        {run.external
          ? "回查调用只有 Azure 日志口径；completionTokens 含 reasoning tokens。"
          : `completion_tokens 含 reasoning tokens（本次 ${typeof reasoning === "number" ? reasoning : "—"}）。`}
        分母越接近模型生成阶段，TPS 越高；各口径不可互相替代。
      </p>
    </section>
  );
}
