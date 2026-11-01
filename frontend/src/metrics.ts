import type { Run } from "./types";

export type TpsBasis = {
  key:
    | "client_total"
    | "client_http"
    | "client_stream"
    | "azure_request"
    | "azure_usage"
    | "decode";
  label: string;
  source: "client" | "azure" | "service";
  tokens: number | null;
  ms: number | null;
  value: number | null;
  formula: string;
  missing: string;
};

export const sourceLabels: Record<TpsBasis["source"], string> = {
  client: "Client 实测",
  azure: "Azure 日志",
  service: "服务端自报",
};

function finite(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

// Usage logs report token counts as arrays, e.g. generatedTokens: [49].
export function tokenCount(value: unknown): number | null {
  if (!Array.isArray(value)) return finite(value);
  const items = value.map(finite);
  if (!items.length || items.some((item) => item === null)) return null;
  return items.reduce<number>((sum, item) => sum + (item ?? 0), 0);
}

export function tokensPerSecond(
  tokens: number | null,
  ms: number | null,
): number | null {
  return tokens !== null && ms !== null && tokens >= 0 && ms > 0
    ? (tokens * 1000) / ms
    : null;
}

export function formatTps(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(1)} tok/s`;
}

function basis(
  fields: Omit<TpsBasis, "value" | "missing">,
  missing: string,
): TpsBasis {
  const value = tokensPerSecond(fields.tokens, fields.ms);
  return { ...fields, value, missing: value === null ? missing : "" };
}

function asObject(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

// Chat Completions reports completion_tokens; the Responses API reports output_tokens.
export function outputTokens(usage: Record<string, unknown>): number | null {
  return tokenCount(usage.completion_tokens ?? usage.output_tokens);
}

export function reasoningTokens(usage: Record<string, unknown>): number | null {
  const details = asObject(
    usage.completion_tokens_details ?? usage.output_tokens_details,
  );
  return tokenCount(details.reasoning_tokens);
}

// Lookups of past calls only have Azure log evidence, so client bases are unavailable.
export type TpsInput = Pick<
  Run,
  "usage" | "duration_ms" | "events" | "logs"
> & {
  external?: boolean;
};

export function tpsBases(run: TpsInput): TpsBasis[] {
  const completion = outputTokens(run.usage);
  const external = "回查仅含 Azure 日志，无客户端数据";
  const noTokens = run.external
    ? external
    : "响应未包含 completion_tokens / output_tokens";
  const clientEvent = (title: string) =>
    run.events.find(
      (event) =>
        event.lane === "call" &&
        event.source === "client" &&
        event.title === title,
    );
  const waiting =
    clientEvent("HTTP request / waiting for headers")?.duration_ms ?? null;
  const stream = clientEvent("Receiving stream");
  const body =
    clientEvent("Reading non-streaming response body")?.duration_ms ??
    stream?.duration_ms ??
    null;
  // Reasoning tokens are generated before the first visible delta.
  const visible =
    completion === null ? null : completion - (reasoningTokens(run.usage) ?? 0);
  const firstDelta = finite(stream?.details.first_delta_after_ms);
  const lastDelta = finite(stream?.details.last_delta_after_ms);
  const streamSpan =
    visible !== null &&
    visible >= 2 &&
    firstDelta !== null &&
    lastDelta !== null &&
    lastDelta > firstDelta;
  const record = (category: string) =>
    run.logs.records.find(
      (item) => item.category === category && item.matching_fields.length > 0,
    );
  const request = record("RequestResponse");
  const usageLog = record("AzureOpenAIRequestUsage");
  const pending = (category: string) =>
    run.logs.polling || run.logs.state === "querying"
      ? `等待 ${category} 日志入库`
      : `未关联到 ${category} 日志`;
  const checkpoint = asObject(run.usage.latency_checkpoint);
  const engineFirst = finite(checkpoint.engine_ttft_ms);
  const engineLast = finite(checkpoint.engine_ttlt_ms);
  const engineGap = finite(checkpoint.engine_tbt_ms);
  const decodeSpan =
    completion !== null &&
    completion >= 2 &&
    engineFirst !== null &&
    engineLast !== null &&
    engineLast > engineFirst;

  return [
    basis(
      {
        key: "client_total",
        label: "端到端（Client 总耗时）",
        source: "client",
        tokens: completion,
        ms: run.duration_ms,
        formula: "completion_tokens ÷ Client 总耗时（含身份准备与网络）",
      },
      run.external ? external : completion === null ? noTokens : "调用尚未结束",
    ),
    basis(
      {
        key: "client_http",
        label: "HTTP 往返（发送 → 读完响应）",
        source: "client",
        tokens: completion,
        ms: waiting === null ? null : waiting + (body ?? 0),
        formula: "completion_tokens ÷（等待响应头 + 读取响应体或接收流）",
      },
      run.external
        ? external
        : completion === null
          ? noTokens
          : "缺少 HTTP 阶段耗时",
    ),
    basis(
      {
        key: "client_stream",
        label: "流式解码（首个 → 末个内容增量）",
        source: "client",
        tokens: streamSpan ? visible - 1 : null,
        ms: streamSpan ? lastDelta - firstDelta : null,
        formula:
          "(completion_tokens − reasoning_tokens − 1) ÷ (末个增量 − 首个增量)；默认内容过滤缓冲后分块返回，可能高于模型实际解码速度",
      },
      run.external
        ? external
        : !stream
          ? "仅流式调用可测"
          : completion === null
            ? noTokens
            : "内容增量不足以计算解码速率",
    ),
    basis(
      {
        key: "azure_request",
        label: "Azure 服务端（RequestResponse）",
        source: "azure",
        tokens: tokenCount(request?.fields.completionTokens),
        ms: finite(request?.fields.DurationMs),
        formula: "properties.completionTokens ÷ DurationMs",
      },
      request
        ? "日志缺少 completionTokens 或 DurationMs"
        : pending("RequestResponse"),
    ),
    basis(
      {
        key: "azure_usage",
        label: "模型输出（用量日志）",
        source: "azure",
        tokens: tokenCount(usageLog?.fields.generatedTokens),
        ms: finite(usageLog?.fields.timeToLastTokenMs),
        formula: "Σ generatedTokens ÷ timeToLastTokenMs（含首 token 前时间）",
      },
      usageLog
        ? "日志缺少 generatedTokens 或 timeToLastTokenMs"
        : pending("AzureOpenAIRequestUsage"),
    ),
    basis(
      decodeSpan
        ? {
            key: "decode",
            label: "引擎解码（latency_checkpoint）",
            source: "service",
            tokens: completion - 1,
            ms: engineLast - engineFirst,
            formula:
              "(completion_tokens − 1) ÷ (engine_ttlt_ms − engine_ttft_ms)",
          }
        : {
            key: "decode",
            label: "引擎解码（latency_checkpoint）",
            source: "service",
            tokens: engineGap === null ? null : 1,
            ms: engineGap,
            formula: "1000 ÷ engine_tbt_ms（整数毫秒，精度有限）",
          },
      run.external ? external : "响应未包含可用的 latency_checkpoint",
    ),
  ];
}
