import { z } from "zod";

const jsonObject = z.record(z.string(), z.unknown());
export const connectionSchema = z.object({
  endpoint: z.string(),
  deployment: z.string(),
  auth_mode: z.enum(["api_key", "managed_identity"]),
  managed_identity_client_id: z.string(),
  logs_auth_mode: z.enum(["managed_identity", "azure_cli"]),
  workspace_id: z.string(),
  resource_id: z.string(),
  log_id_field: z.string(),
  mapping_verified: z.boolean(),
});
export const configSchema = connectionSchema.extend({
  api_key_configured: z.boolean(),
});
export const eventSchema = z.object({
  id: z.string(),
  title: z.string(),
  lane: z.enum(["call", "logs"]),
  source: z.enum(["client", "azure", "log_query"]),
  kind: z.enum(["interval", "point"]),
  started_at: z.string(),
  ended_at: z.string().nullable(),
  duration_ms: z.number().nullable(),
  status: z.enum(["running", "success", "error", "cancelled", "observed"]),
  details: jsonObject,
});
export const logSchema = z.object({
  state: z.enum([
    "idle",
    "querying",
    "pending",
    "matched",
    "candidate",
    "unavailable",
    "error",
    "partial",
    "cancelled",
  ]),
  message: z.string(),
  query: z.string(),
  checked_at: z.string().nullable(),
  attempts: z.number(),
  polling: z.boolean(),
  limited: z.boolean(),
  records: z.array(
    z.object({
      timestamp: z.string(),
      ingested_at: z.string().nullable(),
      category: z.string(),
      resource: z.string().optional(),
      fields: jsonObject,
      matching_fields: z.array(z.string()),
    }),
  ),
  id_fields: z.array(z.string()),
  missing: z.array(z.string()),
});
export const runSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  finished_at: z.string().nullable(),
  duration_ms: z.number().nullable(),
  status: z.enum(["queued", "running", "succeeded", "failed", "cancelled"]),
  connection: connectionSchema,
  max_completion_tokens: z.number(),
  http_status: z.number().nullable(),
  request_ids: z.record(z.string(), z.string()),
  response_meta: jsonObject,
  usage: jsonObject,
  answer: z.string().nullable(),
  error: z.string().nullable(),
  events: z.array(eventSchema),
  logs: logSchema,
});
export const historySchema = z.array(
  z.object({
    id: z.string(),
    created_at: z.string(),
    status: runSchema.shape.status,
    deployment: z.string(),
    auth_mode: connectionSchema.shape.auth_mode,
    log_state: logSchema.shape.state,
  }),
);
export const checkSchema = z.object({
  model: z.object({ ok: z.boolean(), message: z.string() }),
  logs: logSchema,
});
export const rawLogsSchema = z.object({
  state: z.enum(["ok", "empty", "partial", "error", "unavailable"]),
  message: z.string(),
  query: z.string(),
  checked_at: z.string().nullable(),
  limited: z.boolean(),
  rows: z.array(
    z.object({
      category: z.string(),
      matching_fields: z.array(z.string()),
      column_count: z.number(),
      columns: jsonObject,
    }),
  ),
});
const lookupState = z.enum(["ok", "empty", "partial", "error", "unavailable"]);
export const lookupResourcesSchema = z.object({
  state: lookupState,
  message: z.string(),
  query: z.string(),
  checked_at: z.string().nullable(),
  resources: z.array(
    z.object({
      resource_id: z.string(),
      name: z.string(),
      rows: z.number(),
      inference: z.number(),
      last_seen: z.string().nullable(),
    }),
  ),
});
export const candidatesSchema = z.object({
  state: lookupState,
  message: z.string(),
  query: z.string(),
  checked_at: z.string().nullable(),
  limited: z.boolean(),
  rows: z.array(
    z.object({
      time: z.string(),
      request_id: z.string(),
      resource: z.string(),
      resource_id: z.string(),
      operation: z.string(),
      result: z.string(),
      duration_ms: z.number().nullable(),
      deployment: z.string(),
      model: z.string(),
      model_version: z.string(),
      stream_type: z.string(),
      prompt_tokens: z.number().nullable(),
      completion_tokens: z.number().nullable(),
      object_id: z.string(),
      caller_ip: z.string(),
      local_run_id: z.string().nullable(),
    }),
  ),
});
export const traceSchema = z.object({
  request_id: z.string(),
  scope: z.string(),
  start: z.string(),
  end: z.string(),
  logs: logSchema,
  events: z.array(eventSchema),
  resources: z.array(z.string()),
  local_run_id: z.string().nullable(),
  raw_scope: z.string(),
  raw_start: z.string().nullable(),
  raw_end: z.string().nullable(),
});
export type Connection = z.infer<typeof connectionSchema>;
export type Run = z.infer<typeof runSchema>;
export type TimelineEvent = z.infer<typeof eventSchema>;
export type History = z.infer<typeof historySchema>;
export type Check = z.infer<typeof checkSchema>;
export type RawLogs = z.infer<typeof rawLogsSchema>;
export type LookupResources = z.infer<typeof lookupResourcesSchema>;
export type Candidates = z.infer<typeof candidatesSchema>;
export type Candidate = Candidates["rows"][number];
export type Trace = z.infer<typeof traceSchema>;

export const labels: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  succeeded: "调用成功",
  failed: "调用失败",
  cancelled: "已停止",
  idle: "尚未查询",
  querying: "查询中",
  pending: "暂未查到",
  matched: "精确匹配",
  candidate: "候选 · 待核验",
  unavailable: "无法关联",
  error: "错误",
  partial: "部分结果",
  success: "完成",
  observed: "观测点",
};

export async function api<T>(
  path: string,
  schema: z.ZodType<T>,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...(body !== undefined
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
    signal,
  });
  const payload: unknown = await response.json();
  if (!response.ok) {
    const error = z.object({ detail: z.unknown() }).safeParse(payload);
    throw new Error(
      error.success
        ? `HTTP ${response.status}: ${JSON.stringify(error.data.detail)}`
        : `HTTP ${response.status}`,
    );
  }
  return schema.parse(payload);
}

export function milliseconds(value: number): string {
  return value >= 1000
    ? `${(value / 1000).toFixed(2)} s`
    : `${value.toFixed(1)} ms`;
}

export function eventName(title: string): string {
  return (
    (
      {
        Authentication: "身份认证 / 准备凭据",
        "HTTP request / waiting for headers": "HTTP 请求 · 等待响应头",
        "Response headers received": "收到响应头",
        "Reading non-streaming response body": "读取非流式响应体",
        "Parsing response": "解析回答与用量",
        "Waiting before next log query": "等待下一次日志查询",
        "Read-only Log Analytics query": "查询 Log Analytics",
        "Azure RequestResponse: service processing":
          "Azure 服务端处理（RequestResponse）",
        "Azure: RequestResponse": "Azure 请求日志时间点",
        "Azure usage log: time to first token": "模型首 token（用量日志）",
        "Azure usage log: time to last token": "模型末 token（用量日志）",
        "Azure usage log: full output (TTFT = TTLT)":
          "模型完整输出（用量日志，TTFT = TTLT）",
        "Azure ingestion: RequestResponse": "入库 · RequestResponse",
        "Azure ingestion: AzureOpenAIRequestUsage":
          "入库 · AzureOpenAIRequestUsage",
      } as Record<string, string>
    )[title] ?? title
  );
}

export function eventSource(event: TimelineEvent): string {
  if (event.source === "client") return "Client · 本地实测";
  if (event.source === "log_query") return "日志查询 · 本地实测";
  if (event.lane === "logs") return "Azure · ingestion_time()";
  return event.kind === "interval"
    ? "Azure 日志 · 服务时钟区间"
    : "Azure 日志 · 服务时钟时间点";
}

export const latencyLabels: Record<string, string> = {
  pre_inference_ms: "推理前处理",
  engine_ttft_ms: "引擎首 token",
  engine_ttlt_ms: "引擎末 token",
  user_visible_ttft_ms: "用户可见首 token",
  service_ttft_ms: "服务首 token",
  service_ttlt_ms: "服务末 token",
  engine_tbt_ms: "引擎 token 间隔",
  service_tbt_ms: "服务 token 间隔",
};
