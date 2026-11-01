import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import App from "./App";
import { LatencyBreakdown, Timeline, timelineGeometry } from "./Timeline";
import { ThroughputBreakdown } from "./Throughput";
import { RunRawLogs } from "./RawLogs";
import { formatTps, tokenCount, tpsBases } from "./metrics";
import {
  api,
  connectionSchema,
  runSchema,
  traceSchema,
  type TimelineEvent,
} from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const connection = connectionSchema.parse({
  endpoint: "https://example.openai.azure.com",
  deployment: "test-deployment",
  auth_mode: "api_key",
  managed_identity_client_id: "",
  logs_auth_mode: "azure_cli",
  workspace_id: "00000000-0000-0000-0000-000000000002",
  resource_id:
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/example",
  log_id_field: "",
  mapping_verified: false,
});
const event: TimelineEvent = {
  id: "event-1",
  title: "Authentication",
  lane: "call",
  source: "client",
  kind: "interval",
  started_at: "2026-10-31T12:00:00Z",
  ended_at: "2026-10-31T12:00:00.050Z",
  duration_ms: 50,
  status: "success",
  details: {},
};
const run = runSchema.parse({
  id: "test-run-1",
  created_at: "2026-10-31T12:00:00Z",
  finished_at: "2026-10-31T12:00:01Z",
  duration_ms: 1000,
  status: "succeeded",
  connection,
  max_completion_tokens: 256,
  http_status: 200,
  request_ids: {
    "x-ms-client-request-id": "client-123",
    "apim-request-id": "azure-123",
  },
  response_meta: { "x-ms-region": "East US 2" },
  usage: { total_tokens: 8 },
  answer: "This is a test fixture, not an Azure response.",
  error: null,
  events: [event],
  logs: {
    state: "candidate",
    message: "Unverified field mapping",
    query: "AzureDiagnostics",
    checked_at: null,
    attempts: 1,
    polling: false,
    limited: false,
    id_fields: ["requestId_s"],
    missing: [],
    records: [
      {
        timestamp: event.started_at,
        ingested_at: null,
        category: "RequestResponse",
        fields: { requestId_s: "azure-123" },
        matching_fields: ["requestId_s = apim-request-id"],
      },
    ],
  },
});

const rawLogs = {
  state: "ok",
  message: "读取到 1 行原始记录（已省略空列）。",
  query: "AzureDiagnostics\n| take 21",
  checked_at: "2026-10-31T12:02:00Z",
  limited: false,
  rows: [
    {
      category: "RequestResponse",
      matching_fields: ["CorrelationId = apim-request-id"],
      column_count: 130,
      columns: {
        TimeGenerated: "2026-10-31T12:00:00Z",
        Category: "RequestResponse",
        CorrelationId: "azure-123",
        SourceSystem: "Azure",
        properties_s: {
          requestTime: "17935073291553453",
          completionTokens: 49,
          apiName: "AIServices",
        },
        Ingested: "2026-10-31T12:01:30Z",
      },
    },
  ],
};

// Mirrors the verified live run: 49 completion tokens over several time bases.
const measured = runSchema.parse({
  ...run,
  duration_ms: 1968.89,
  usage: {
    completion_tokens: 49,
    completion_tokens_details: { reasoning_tokens: 0 },
    latency_checkpoint: { engine_ttft_ms: 17, engine_ttlt_ms: 222 },
  },
  events: [
    {
      ...event,
      id: "http",
      title: "HTTP request / waiting for headers",
      duration_ms: 1600.21,
    },
    {
      ...event,
      id: "body",
      title: "Reading non-streaming response body",
      duration_ms: 0.84,
    },
  ],
  logs: {
    ...run.logs,
    state: "matched",
    records: [
      {
        timestamp: event.started_at,
        ingested_at: null,
        category: "RequestResponse",
        fields: { completionTokens: 49, DurationMs: 782 },
        matching_fields: ["CorrelationId = apim-request-id"],
      },
      {
        timestamp: event.started_at,
        ingested_at: null,
        category: "AzureOpenAIRequestUsage",
        fields: { generatedTokens: [49], timeToLastTokenMs: 751.339 },
        matching_fields: ["CorrelationId = apim-request-id"],
      },
    ],
  },
});

const resourceId = connection.resource_id.toLowerCase();
const lookupResources = {
  state: "ok",
  message: "1 个 Foundry 资源向该 Workspace 写入诊断日志。",
  query: "AzureDiagnostics | summarize",
  checked_at: "2026-11-01T05:00:00Z",
  resources: [
    {
      resource_id: resourceId,
      name: "EXAMPLE",
      rows: 120,
      inference: 9,
      last_seen: "2026-11-01T04:30:00Z",
    },
  ],
};
const candidateRow = {
  time: "2026-10-01T04:10:00Z",
  request_id: "azure-123",
  resource: "EXAMPLE",
  resource_id: resourceId,
  operation: "ChatCompletions_Create",
  result: "200",
  duration_ms: 782,
  deployment: "test-deployment",
  model: "gpt-test",
  model_version: "2026-03-17",
  stream_type: "Non-Streaming",
  prompt_tokens: 20,
  completion_tokens: 49,
  object_id: "",
  caller_ip: "10.1.2.**",
  local_run_id: run.id,
};
const candidates = {
  state: "ok",
  message: "找到 2 条候选调用。",
  query: "AzureDiagnostics | take 51",
  checked_at: "2026-11-01T05:00:00Z",
  limited: false,
  rows: [
    candidateRow,
    {
      ...candidateRow,
      time: "2026-10-01T04:20:00Z",
      request_id: "ext-456",
      result: "429",
      object_id: "11111111-2222-3333-4444-555555555555",
      local_run_id: null,
    },
  ],
};
const trace = traceSchema.parse({
  request_id: "azure-123",
  scope: "",
  start: "2026-10-02T05:00:00Z",
  end: "2026-11-01T05:00:00Z",
  logs: {
    ...measured.logs,
    message: "按 CorrelationId 精确匹配到 2 条记录。",
    records: measured.logs.records.map((record) => ({
      ...record,
      resource: resourceId,
      fields:
        record.category === "RequestResponse"
          ? {
              ...record.fields,
              ResultSignature: "200",
              OperationName: "ChatCompletions_Create",
              promptTokens: 20,
            }
          : record.fields,
    })),
  },
  events: [
    {
      ...event,
      id: "azure-service",
      title: "Azure service request",
      source: "azure",
      duration_ms: 782,
    },
  ],
  resources: [resourceId],
  local_run_id: null,
  raw_scope: resourceId,
  raw_start: "2026-11-01T04:05:00Z",
  raw_end: "2026-11-01T04:15:00Z",
});

function mockApi() {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const body: unknown =
        typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ url: input, body });
      let payload: unknown;
      if (input === "/api/config")
        payload = { ...connection, api_key_configured: true };
      else if (input === "/api/runs" && !init?.method) payload = [];
      else if (input === "/api/runs" && init?.method === "POST") payload = run;
      else if (input === `/api/runs/${run.id}` && !init?.method) payload = run;
      else if (input === `/api/runs/${run.id}/query`) payload = run;
      else if (input === `/api/runs/${run.id}/raw-logs`) payload = rawLogs;
      else if (input === "/api/lookup/resources") payload = lookupResources;
      else if (input === "/api/lookup/candidates") payload = candidates;
      else if (input === "/api/lookup/raw-logs") payload = rawLogs;
      else if (input === "/api/lookup/trace") {
        const id = (body as { request_id: string }).request_id;
        payload =
          id === "missing-1"
            ? {
                ...trace,
                request_id: id,
                logs: {
                  ...trace.logs,
                  state: "pending",
                  message: "在所选范围和时间窗口内没有找到该 ID。",
                  records: [],
                },
                events: [],
                resources: [],
                raw_scope: "",
                raw_start: null,
                raw_end: null,
              }
            : {
                ...trace,
                request_id: id,
                local_run_id: id === "azure-123" ? run.id : null,
              };
      } else throw new Error(`Unmocked URL ${input}`);
      return new Response(JSON.stringify(payload), { status: 200 });
    }),
  );
  return calls;
}

describe("request lab", () => {
  it("renders configuration and clearly disables the gateway route", async () => {
    mockApi();
    render(<App />);
    await screen.findByText("API Key 已在服务端配置，不会返回浏览器。");
    expect(screen.getByText("后续支持")).toBeTruthy();
    expect(screen.queryByText("精确匹配")).toBeNull();
    expect(screen.getByLabelText("Foundry Endpoint")).toHaveProperty(
      "value",
      connection.endpoint,
    );
  });

  it("shows native IDs, candidates and event details without pretending they are verified", async () => {
    const calls = mockApi();
    render(<App />);
    await screen.findByText("API Key 已在服务端配置，不会返回浏览器。");
    fireEvent.click(screen.getByRole("button", { name: "执行请求" }));
    await screen.findByText("azure-123");
    expect(screen.getByText("候选 · 待核验")).toBeTruthy();
    expect(screen.getByText("East US 2")).toBeTruthy();
    expect(screen.queryByText("精确匹配")).toBeNull();
    expect(screen.getByText(run.answer!)).toBeTruthy();
    const stage = screen
      .getAllByRole("button")
      .find(
        (button) =>
          button.textContent?.includes("身份认证 / 准备凭据") &&
          button.hasAttribute("aria-pressed"),
      );
    expect(stage).toBeTruthy();
    fireEvent.click(stage!);
    expect(screen.getByText(/"duration_ms": 50/)).toBeTruthy();
    expect(
      calls.filter((call) => call.url === "/api/runs" && call.body),
    ).toHaveLength(1);
  });

  it("updates mapping on a log-only requery without another model call", async () => {
    const calls = mockApi();
    render(<App />);
    await screen.findByText("API Key 已在服务端配置，不会返回浏览器。");
    fireEvent.click(screen.getByRole("button", { name: "执行请求" }));
    await screen.findByText("azure-123");
    fireEvent.change(screen.getByLabelText("日志请求 ID 字段"), {
      target: { value: "CorrelationId" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /我已核验该字段/ }));
    fireEvent.click(screen.getByRole("button", { name: "重新查日志" }));
    await waitFor(() =>
      expect(calls.find((call) => call.url.endsWith("/query"))?.body).toEqual({
        log_id_field: "CorrelationId",
        mapping_verified: true,
      }),
    );
    expect(
      calls.filter((call) => call.url === "/api/runs" && call.body),
    ).toHaveLength(1);
  });

  it("surfaces invalid API responses and HTTP failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "Permission denied" }), {
            status: 403,
          }),
      ),
    );
    await expect(api("/runs", runSchema)).rejects.toThrow("Permission denied");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ id: "incomplete" }))),
    );
    await expect(api("/runs", runSchema)).rejects.toThrow();
  });
});

describe("timeline evidence", () => {
  it("keeps the long log-discovery interval out of the call lane", () => {
    const onSelect = vi.fn();
    render(
      <Timeline
        events={[
          event,
          {
            ...event,
            id: "logs",
            lane: "logs",
            title: "Waiting before next log query",
            duration_ms: 60000,
          },
        ]}
        lane="call"
        onSelect={onSelect}
      />,
    );
    expect(screen.queryByText("等待下一次日志查询")).toBeNull();
    expect(screen.getAllByText("50.0 ms").length).toBeGreaterThan(0);
    expect(screen.getByText(/requestTime 近似对齐/)).toBeTruthy();
  });

  it("orders lanes chronologically and labels Azure service intervals", () => {
    render(
      <Timeline
        events={[
          {
            ...event,
            id: "parse",
            title: "Parsing response",
            started_at: "2026-10-31T12:00:01Z",
          },
          {
            ...event,
            id: "azure",
            source: "azure",
            title: "Azure RequestResponse: service processing",
            started_at: "2026-10-31T12:00:00.020Z",
            duration_ms: 567,
            status: "observed",
          },
          event,
        ]}
        lane="call"
        onSelect={() => {}}
      />,
    );
    const labels = screen
      .getAllByRole("button")
      .map((button) => button.querySelector("strong")?.textContent);
    expect(labels).toEqual([
      "身份认证 / 准备凭据",
      "Azure 服务端处理（RequestResponse）",
      "解析回答与用量",
    ]);
    expect(screen.getByText("Azure 日志 · 服务时钟区间")).toBeTruthy();
  });

  it("renders service-reported latency milestones without absolute alignment", () => {
    render(
      <LatencyBreakdown
        usage={{
          latency_checkpoint: {
            service_ttlt_ms: 310,
            pre_inference_ms: 68,
            service_tbt_ms: 4,
          },
        }}
      />,
    );
    expect(screen.getByText("推理前处理")).toBeTruthy();
    expect(screen.getByText("310.0 ms")).toBeTruthy();
    expect(screen.getByText("2 个耗时值 · 相对值")).toBeTruthy();
    expect(screen.getByText("服务 token 间隔：4.0 ms")).toBeTruthy();
    expect(document.querySelectorAll(".latency-section .bar")).toHaveLength(2);
    expect(screen.getByText(/不与上方时间轴对齐/)).toBeTruthy();
  });

  it("hides the latency chart when the response has no checkpoint", () => {
    const { container } = render(
      <LatencyBreakdown usage={{ total_tokens: 8 }} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("does not infer missing durations from wall-clock timestamps", () => {
    render(
      <Timeline
        events={[
          {
            ...event,
            duration_ms: null,
            status: "error",
          },
        ]}
        lane="call"
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("耗时未知")).toBeTruthy();
  });

  it("uses measured duration and treats Azure records as points", () => {
    const geometry = timelineGeometry(
      [
        event,
        {
          ...event,
          id: "azure",
          source: "azure",
          kind: "point",
          duration_ms: null,
          started_at: "2026-10-31T12:00:00.100Z",
          status: "observed",
        },
      ],
      Date.parse("2026-10-31T12:00:10Z"),
    );
    expect(geometry.span).toBe(100);
  });
});

describe("throughput", () => {
  it("computes every TPS basis from the verified live numbers", () => {
    const values = Object.fromEntries(
      tpsBases(measured).map((item) => [item.key, formatTps(item.value)]),
    );
    expect(values).toEqual({
      client_total: "24.9 tok/s",
      client_http: "30.6 tok/s",
      azure_request: "62.7 tok/s",
      azure_usage: "65.2 tok/s",
      decode: "234.1 tok/s",
    });
    expect(tokenCount([20, 29])).toBe(49);
    expect(tokenCount(["49"])).toBe(49);
    expect(tokenCount([1, "x"])).toBeNull();
  });

  it("explains missing bases instead of showing zero", () => {
    const pending = runSchema.parse({
      ...measured,
      usage: { completion_tokens: 49 },
      logs: { ...measured.logs, polling: true, records: [] },
    });
    const bases = Object.fromEntries(
      tpsBases(pending).map((item) => [item.key, item]),
    );
    expect(formatTps(bases.azure_request.value)).toBe("—");
    expect(bases.azure_request.missing).toBe("等待 RequestResponse 日志入库");
    expect(bases.decode.missing).toBe("响应未包含可用的 latency_checkpoint");
    const unmatched = runSchema.parse({
      ...measured,
      logs: {
        ...measured.logs,
        records: measured.logs.records.map((record) => ({
          ...record,
          matching_fields: [],
        })),
      },
    });
    expect(
      tpsBases(unmatched).find((item) => item.key === "azure_usage")?.missing,
    ).toBe("未关联到 AzureOpenAIRequestUsage 日志");
  });

  it("renders the comparison with sources and formulas", () => {
    render(<ThroughputBreakdown run={measured} />);
    const section = screen.getByRole("region", { name: "TPS 对比" });
    expect(section.textContent).toContain("5/5 个口径可计算");
    expect(section.textContent).toContain("Azure 日志 · 49 tok ÷ 782.0 ms");
    expect(section.textContent).toContain("234.1 tok/s");
    expect(section.textContent).toContain("reasoning tokens（本次 0）");
    expect(section.querySelectorAll(".bar.observed")).toHaveLength(5);
  });
});

describe("raw log panel", () => {
  it("highlights the matched ID and filters to key fields", async () => {
    const calls = mockApi();
    render(<RunRawLogs run={run} />);
    const matched = await screen.findByText(/= apim-request-id ✓/);
    expect(matched.closest(".raw-line")?.className).toContain("hl-match");
    expect(screen.getByText(/以字符串保留精度/)).toBeTruthy();
    expect(screen.getByText(/ingestion_time\(\)/)).toBeTruthy();
    expect(screen.getByText("6/130 列有值")).toBeTruthy();
    expect(screen.getByText('"SourceSystem"')).toBeTruthy();
    expect(screen.getByText('"apiName"')).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "仅显示关键字段" }));
    expect(screen.queryByText('"SourceSystem"')).toBeNull();
    expect(screen.queryByText('"apiName"')).toBeNull();
    expect(screen.getByText('"completionTokens"')).toBeTruthy();
    expect(calls.filter((call) => call.url.endsWith("/raw-logs"))).toHaveLength(
      1,
    );
  });

  it("waits for correlated records before querying raw rows", () => {
    const calls = mockApi();
    const empty = runSchema.parse({
      ...run,
      logs: { ...run.logs, records: [] },
    });
    render(<RunRawLogs run={empty} />);
    expect(
      screen.getByText("关联到日志记录后自动读取；也可手动刷新。"),
    ).toBeTruthy();
    expect(calls).toHaveLength(0);
  });
});

describe("historical lookup", () => {
  const DAY = 86_400_000;
  type Body = Record<string, unknown> & { start: string; end: string };
  const lastBody = (calls: { url: string; body: unknown }[], url: string) =>
    calls.filter((call) => call.url === url).at(-1)?.body as Body | undefined;

  async function openLookup() {
    const calls = mockApi();
    render(<App />);
    await screen.findByText("API Key 已在服务端配置，不会返回浏览器。");
    expect(calls.some((call) => call.url.startsWith("/api/lookup"))).toBe(
      false,
    );
    fireEvent.click(screen.getByRole("tab", { name: "回查历史调用" }));
    await screen.findByRole("option", { name: "EXAMPLE · 9 次模型调用" });
    return calls;
  }

  it("marks client-only TPS bases as unavailable for lookups", () => {
    const bases = Object.fromEntries(
      tpsBases({
        usage: {},
        duration_ms: null,
        events: trace.events,
        logs: trace.logs,
        external: true,
      }).map((item) => [item.key, item]),
    );
    expect(formatTps(bases.azure_request.value)).toBe("62.7 tok/s");
    expect(formatTps(bases.azure_usage.value)).toBe("65.2 tok/s");
    for (const key of ["client_total", "client_http", "decode"])
      expect(bases[key].missing).toBe("回查仅含 Azure 日志，无客户端数据");
  });

  it("traces an apim-request-id and opens the matching local run", async () => {
    const calls = await openLookup();
    expect(screen.queryByRole("button", { name: "执行请求" })).toBeNull();
    fireEvent.change(screen.getByLabelText("apim-request-id"), {
      target: { value: "  azure-123 " },
    });
    fireEvent.click(screen.getByRole("button", { name: "回查" }));
    await screen.findByRole("region", { name: "回查结果" });
    const body = lastBody(calls, "/api/lookup/trace")!;
    expect(body).toMatchObject({ request_id: "azure-123", scope: "" });
    expect(Date.parse(body.end) - Date.parse(body.start)).toBe(30 * DAY);
    expect(screen.getByText("本应用发起")).toBeTruthy();
    const tps = screen.getByRole("region", { name: "TPS 对比" });
    expect(tps.textContent).toContain("62.7 tok/s");
    expect(tps.textContent).toContain("回查仅含 Azure 日志，无客户端数据");
    await screen.findByText(/= apim-request-id ✓/);
    expect(lastBody(calls, "/api/lookup/raw-logs")).toEqual({
      connection,
      request_id: "azure-123",
      scope: resourceId,
      start: trace.raw_start,
      end: trace.raw_end,
    });

    fireEvent.click(screen.getByRole("button", { name: "打开本地实验" }));
    await screen.findByRole("button", { name: "执行请求" });
    expect(calls.some((call) => call.url === `/api/runs/${run.id}`)).toBe(true);
    expect(screen.queryByRole("region", { name: "回查结果" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "回查历史调用" }));
    expect(screen.getByRole("region", { name: "回查结果" })).toBeTruthy();
    expect(
      calls.filter((call) => call.url === "/api/lookup/resources"),
    ).toHaveLength(1);
  });

  it("explains an unknown ID without reading raw rows", async () => {
    const calls = await openLookup();
    fireEvent.change(screen.getByLabelText("apim-request-id"), {
      target: { value: "missing-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "回查" }));
    await screen.findByText("在所选范围和时间窗口内没有找到该 ID。");
    expect(screen.getByText("暂未查到")).toBeTruthy();
    expect(screen.getByText("外部调用")).toBeTruthy();
    expect(
      screen.getByText(
        "回查到日志记录后显示原始行；未找到的 ID 不读取原始日志。",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /刷新原始日志/ })).toHaveProperty(
      "disabled",
      true,
    );
    expect(calls.some((call) => call.url === "/api/lookup/raw-logs")).toBe(
      false,
    );
  });

  it("searches candidates and traces a row within its own Foundry", async () => {
    const calls = await openLookup();
    fireEvent.click(screen.getByRole("tab", { name: "按条件找候选" }));
    fireEvent.change(screen.getByLabelText("开始（本地时间）"), {
      target: { value: "2026-10-01T00:00" },
    });
    fireEvent.change(screen.getByLabelText("结束（本地时间）"), {
      target: { value: "2026-10-09T00:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜索候选" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "候选搜索的时间范围最长 7 天。",
    );
    expect(calls.some((call) => call.url === "/api/lookup/candidates")).toBe(
      false,
    );

    fireEvent.click(screen.getByRole("button", { name: "24 小时" }));
    fireEvent.change(screen.getByLabelText("部署名"), {
      target: { value: " test-deployment " },
    });
    fireEvent.change(screen.getByLabelText("结果"), {
      target: { value: "failure" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜索候选" }));
    const table = await screen.findByRole("table");
    const body = lastBody(calls, "/api/lookup/candidates")!;
    expect(body).toMatchObject({
      scope: "",
      deployment: "test-deployment",
      object_id: "",
      outcome: "failure",
      inference_only: true,
    });
    expect(Date.parse(body.end) - Date.parse(body.start)).toBe(DAY);
    expect(table.textContent).toContain("本应用");
    expect(table.textContent).toContain("10.1.2.**");
    expect(table.textContent).toContain("11111111…5555");
    expect(table.textContent).toContain("62.7 tok/s");

    fireEvent.click(screen.getByRole("button", { name: "回查 ext-456" }));
    await screen.findByText("外部调用");
    expect(lastBody(calls, "/api/lookup/trace")).toMatchObject({
      request_id: "ext-456",
      scope: resourceId,
      start: "2026-10-01T03:20:00.000Z",
      end: "2026-10-01T05:20:00.000Z",
    });
    expect(screen.queryByRole("button", { name: /打开本地实验/ })).toBeNull();
  });
});
