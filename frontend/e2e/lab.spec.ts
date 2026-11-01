import { expect, test, type Page } from "@playwright/test";

// These browser fixtures never call Azure or write experiments to the real backend.
async function fixtureApi(page: Page) {
  const connection = {
    endpoint: "https://example.openai.azure.com/openai/v1",
    deployment: "browser-test-fixture",
    auth_mode: "api_key",
    managed_identity_client_id: "",
    logs_auth_mode: "azure_cli",
    workspace_id: "00000000-0000-0000-0000-000000000002",
    resource_id:
      "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/example",
    log_id_field: "",
    mapping_verified: false,
  };
  const created = new Date().toISOString();
  const after = (milliseconds: number) =>
    new Date(Date.parse(created) + milliseconds).toISOString();
  const run = {
    id: "browser-fixture",
    created_at: created,
    finished_at: after(1500),
    duration_ms: 1500,
    status: "succeeded",
    connection,
    max_completion_tokens: 256,
    http_status: 200,
    request_ids: {
      "x-ms-client-request-id": "client-browser-fixture",
      "apim-request-id": "azure-browser-fixture",
    },
    response_meta: {
      "x-ms-region": "East US 2",
      "x-ms-served-model": "fixture-model",
      "response.id": "chatcmpl-fixture",
    },
    usage: {
      total_tokens: 12,
      completion_tokens: 6,
      latency_checkpoint: {
        pre_inference_ms: 68,
        service_ttft_ms: 288,
        service_ttlt_ms: 310,
      },
    },
    answer: "仅用于浏览器测试的模拟回答，不是真实 Azure 响应。",
    error: null,
    events: [
      {
        id: "auth",
        title: "Authentication",
        lane: "call",
        source: "client",
        kind: "interval",
        started_at: created,
        ended_at: after(50),
        duration_ms: 50,
        status: "success",
        details: {},
      },
      {
        id: "http",
        title: "HTTP request / waiting for headers",
        lane: "call",
        source: "client",
        kind: "interval",
        started_at: after(50),
        ended_at: after(1400),
        duration_ms: 1350,
        status: "success",
        details: {},
      },
      {
        id: "header",
        title: "Response headers received",
        lane: "call",
        source: "client",
        kind: "point",
        started_at: after(1400),
        ended_at: null,
        duration_ms: null,
        status: "observed",
        details: { "apim-request-id": "azure-browser-fixture" },
      },
      {
        id: "azure",
        title: "Azure RequestResponse: service processing",
        lane: "call",
        source: "azure",
        kind: "interval",
        started_at: after(300),
        ended_at: after(1200),
        duration_ms: 900,
        status: "observed",
        details: { CorrelationId: "azure-browser-fixture" },
      },
      {
        id: "ttft",
        title: "Azure usage log: time to first token",
        lane: "call",
        source: "azure",
        kind: "interval",
        started_at: after(300),
        ended_at: after(1150),
        duration_ms: 850,
        status: "observed",
        details: {},
      },
      {
        id: "ingest",
        title: "Azure ingestion: RequestResponse",
        lane: "logs",
        source: "azure",
        kind: "point",
        started_at: after(9000),
        ended_at: null,
        duration_ms: null,
        status: "observed",
        details: {
          category: "RequestResponse",
          TimeGenerated: after(1300),
          ingestion_time: after(9000),
          ingestion_delay_ms: 7700,
          since_run_start_ms: 9000,
        },
      },
      {
        id: "wait",
        title: "Waiting before next log query",
        lane: "logs",
        source: "log_query",
        kind: "interval",
        started_at: after(1500),
        ended_at: after(16500),
        duration_ms: 15000,
        status: "success",
        details: {},
      },
      {
        id: "query",
        title: "Read-only Log Analytics query",
        lane: "logs",
        source: "log_query",
        kind: "interval",
        started_at: after(16500),
        ended_at: after(17000),
        duration_ms: 500,
        status: "success",
        details: { query: "AzureDiagnostics", record_count: 1 },
      },
    ],
    logs: {
      state: "candidate",
      message: "Browser fixture: mapping has not been verified.",
      query:
        "AzureDiagnostics\n| where CorrelationId == 'azure-browser-fixture'",
      checked_at: after(17000),
      attempts: 2,
      polling: false,
      limited: false,
      id_fields: ["requestId_s"],
      missing: [],
      records: [
        {
          timestamp: after(1300),
          ingested_at: after(9000),
          category: "RequestResponse",
          fields: {
            requestId_s: "azure-browser-fixture",
            DurationMs: 900,
            completionTokens: 6,
          },
          matching_fields: ["requestId_s = apim-request-id"],
        },
      ],
    },
  };
  let modelCalls = 0;
  let rawCalls = 0;
  const rawLogs = {
    state: "ok",
    message: "Browser fixture: 1 raw row.",
    query: "AzureDiagnostics\n| take 21",
    checked_at: after(17000),
    limited: false,
    rows: [
      {
        category: "RequestResponse",
        matching_fields: ["requestId_s = apim-request-id"],
        column_count: 130,
        columns: {
          TimeGenerated: after(1300),
          Category: "RequestResponse",
          OperationName: "chatcompletions_create",
          requestId_s: "azure-browser-fixture",
          ResourceId: `/SUBSCRIPTIONS/00000000-0000-0000-0000-000000000001/RESOURCEGROUPS/TEST/PROVIDERS/MICROSOFT.COGNITIVESERVICES/ACCOUNTS/${"VERY-LONG-ACCOUNT-NAME-".repeat(4)}`,
          properties_s: {
            requestTime: "17935073291553453",
            completionTokens: 6,
            userAgent: "fixture-agent/".repeat(20),
          },
          Ingested: after(9000),
        },
      },
    ],
  };
  const eastus = connection.resource_id.toLowerCase();
  const westus = eastus.replace("/example", "/westus-b");
  const lookupResources = {
    state: "ok",
    message: "Browser fixture: 2 Foundry resources share this workspace.",
    query: "AzureDiagnostics | summarize",
    checked_at: after(17000),
    resources: [
      {
        resource_id: eastus,
        name: "EASTUS2-A",
        rows: 300,
        inference: 7,
        last_seen: after(1300),
      },
      {
        resource_id: westus,
        name: "WESTUS-B",
        rows: 120,
        inference: 3,
        last_seen: after(1300),
      },
    ],
  };
  const candidateRow = {
    time: after(1300),
    request_id: "azure-browser-fixture",
    resource: "EASTUS2-A",
    resource_id: eastus,
    operation: "ChatCompletions_Create",
    result: "200",
    duration_ms: 900,
    deployment: connection.deployment,
    model: "fixture-model",
    model_version: "2026-03-17",
    stream_type: "Non-Streaming",
    prompt_tokens: 6,
    completion_tokens: 6,
    object_id: "",
    caller_ip: "10.1.2.**",
    local_run_id: run.id,
  };
  const candidates = {
    state: "ok",
    message: "Browser fixture: 2 candidates from 2 Foundry resources.",
    query: "AzureDiagnostics | take 51",
    checked_at: after(17000),
    limited: false,
    rows: [
      candidateRow,
      {
        ...candidateRow,
        request_id: "ext-westus",
        resource: "WESTUS-B",
        resource_id: westus,
        object_id: "11111111-2222-3333-4444-555555555555",
        caller_ip: "10.9.8.**",
        local_run_id: null,
      },
    ],
  };
  const lookupBodies: { path: string; body: unknown }[] = [];
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() as Record<
      string,
      unknown
    > | null;
    let data: unknown;
    if (path === "/api/config")
      data = {
        ...connection,
        api_key_configured: true,
        source: "env",
        saved_at: null,
        prompt: "",
        max_completion_tokens: 256,
        api: "chat_completions",
        stream: false,
      };
    else if (path === "/api/lookup/resources") data = lookupResources;
    else if (path === "/api/lookup/candidates") {
      lookupBodies.push({ path, body });
      data = candidates;
    } else if (path === "/api/lookup/trace") {
      lookupBodies.push({ path, body });
      const id = String(body?.request_id);
      const resource = id === "ext-westus" ? westus : eastus;
      data = {
        request_id: id,
        scope: body?.scope ?? "",
        start: body?.start,
        end: body?.end,
        logs: {
          ...run.logs,
          state: "matched",
          message: `Browser fixture: CorrelationId == ${id}.`,
          records: run.logs.records.map((record) => ({
            ...record,
            resource,
            fields: {
              ...record.fields,
              ResultSignature: "200",
              promptTokens: 6,
            },
            matching_fields: ["CorrelationId = apim-request-id"],
          })),
        },
        events: run.events.filter((event) => event.source !== "client"),
        resources: [resource],
        local_run_id: id === run.request_ids["apim-request-id"] ? run.id : null,
        raw_scope: resource,
        raw_start: after(-300000),
        raw_end: after(300000),
      };
    } else if (path === "/api/lookup/raw-logs") {
      lookupBodies.push({ path, body });
      data = rawLogs;
    } else if (path === "/api/runs" && route.request().method() === "GET")
      data = modelCalls
        ? [
            {
              id: run.id,
              created_at: created,
              status: run.status,
              deployment: connection.deployment,
              auth_mode: connection.auth_mode,
              log_state: run.logs.state,
            },
          ]
        : [];
    else if (path === "/api/runs" && route.request().method() === "POST") {
      modelCalls += 1;
      data = run;
    } else if (path.endsWith("/query")) {
      run.logs.state = "matched";
      run.logs.attempts += 1;
      run.logs.message = "Browser fixture: explicit field mapping confirmed.";
      data = run;
    } else if (path === `/api/runs/${run.id}/raw-logs`) {
      rawCalls += 1;
      data = rawLogs;
    } else if (path === `/api/runs/${run.id}`) data = run;
    else if (path === "/api/discovery/foundries")
      data = {
        state: "ok",
        message: "找到 1 个可访问的 Foundry / Azure OpenAI 资源。",
        checked_at: created,
        limited: false,
        items: [
          {
            resource_id: connection.resource_id,
            name: "example",
            location: "eastus2",
            kind: "OpenAI",
            resource_group: "test",
            subscription_id: "00000000-0000-0000-0000-000000000001",
            subscription: "Fixture subscription",
            endpoint: connection.endpoint,
          },
        ],
      };
    else if (path === "/api/discovery/foundry")
      data = {
        state: "ok",
        message: "",
        checked_at: created,
        resource_id: connection.resource_id,
        name: "example",
        location: "eastus2",
        kind: "OpenAI",
        endpoint: connection.endpoint,
        deployments: [
          {
            name: connection.deployment,
            model: "gpt-fixture",
            version: "1",
            format: "OpenAI",
            sku: "GlobalStandard",
            capacity: 10,
            state: "Succeeded",
          },
        ],
        deployments_error: "",
        deployments_limited: false,
        workspaces: [
          {
            workspace_id: connection.workspace_id,
            name: "fixture-logs",
            resource_id:
              "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.OperationalInsights/workspaces/fixture-logs",
            location: "eastus2",
            settings: ["fixture"],
            request_logs: true,
            usage_logs: true,
            dedicated: false,
            error: "",
          },
        ],
        other_destinations: [],
        diagnostics_error: "",
      };
    else throw new Error(`Unexpected test request: ${path}`);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });
  return {
    modelCalls: () => modelCalls,
    rawCalls: () => rawCalls,
    lookupBodies,
    westus,
  };
}

test("request, call timeline, log discovery and log-only requery", async ({
  page,
}, testInfo) => {
  const fixture = await fixtureApi(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  const picker = page.getByRole("combobox", { name: /Foundry 资源/ });
  await expect(picker).toHaveValue("example");
  await expect(page.getByLabel(/^Deployment/)).toHaveValue(
    "browser-test-fixture",
  );
  await expect(page.getByRole("group", { name: "诊断设置目标" })).toContainText(
    "fixture-logs",
  );
  await page.getByRole("button", { name: "执行请求" }).click();
  await expect(
    page.getByText("azure-browser-fixture", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("候选 · 待核验", { exact: true })).toHaveCount(2);
  const call = page.getByRole("region", { name: "调用时间轴", exact: true });
  const logs = page.getByRole("region", { name: "日志发现", exact: true });
  await expect(call.getByText("HTTP 请求 · 等待响应头")).toBeVisible();
  await expect(
    call.getByText("Azure 服务端处理（RequestResponse）"),
  ).toBeVisible();
  await expect(logs).toContainText("0/2 类日志已找到 · 1 类待核验 · 查询 2 次");
  const requestLog = logs.getByRole("button", { name: /请求日志/ });
  await expect(requestLog).toContainText("候选 · 待核验");
  await expect(requestLog).toContainText("requestId_s = apim-request-id");
  await expect(requestLog).not.toContainText("✓");
  await expect(requestLog).toContainText("900.0 ms");
  await expect(requestLog).toContainText("请求开始后约 9.00 s 可查询");
  await expect(
    logs.locator(".discovery-item").filter({ hasText: "用量日志" }),
  ).toContainText("未找到");
  await expect(logs.getByLabel("查询过程")).toContainText("第 1 次 · 1 条");
  await expect(logs.locator(".lane-track, .track")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "服务端自报阶段" }),
  ).toContainText("服务末 token");
  await expect(page.getByText("fixture-model")).toBeVisible();
  await expect(call.getByText("等待下一次日志查询")).toHaveCount(0);
  const tps = page.getByRole("region", { name: "TPS 对比" });
  await expect(tps).toContainText("4.0 tok/s");
  await expect(tps).toContainText("Azure 日志 · 6 tok ÷ 900.0 ms");
  await expect(tps).toContainText("响应未包含可用的 latency_checkpoint");
  await expect(
    page.locator(".metric").filter({ hasText: "TPS" }),
  ).toContainText("4.0");
  const raw = page.getByRole("region", { name: "原始 Log Analytics 日志" });
  await expect(raw.locator(".raw-line.hl-match")).toContainText(
    "= apim-request-id ✓",
  );
  await expect(raw).toContainText("7/130 列有值");
  await expect(raw).toContainText('"userAgent"');
  await raw.getByRole("checkbox", { name: "仅显示关键字段" }).check();
  await expect(raw).not.toContainText('"userAgent"');
  await expect(raw).toContainText('"completionTokens"');
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: testInfo.outputPath("desktop.png"),
    fullPage: true,
  });
  await requestLog.click();
  await expect(page.locator(".detail-content")).toContainText(
    "since_run_start_ms",
  );
  await call.getByRole("button", { name: /收到响应头/ }).click();
  await expect(page.locator(".detail-content")).toContainText(
    "apim-request-id",
  );
  await page.getByRole("tab", { name: "实际 KQL" }).click();
  await expect(page.locator(".detail-content")).toContainText(
    "AzureDiagnostics",
  );
  await page.getByLabel("日志请求 ID 字段").fill("requestId_s");
  await page.getByRole("checkbox", { name: /我已核验该字段/ }).check();
  await page.getByRole("button", { name: "重新查日志" }).click();
  await expect(page.getByText("精确匹配", { exact: true })).toBeVisible();
  expect(fixture.modelCalls()).toBe(1);
  expect(fixture.rawCalls()).toBeGreaterThanOrEqual(1);
  await page.reload();
  await page.getByRole("button", { name: /browser-test-fixture/ }).click();
  await expect(
    page.getByText("azure-browser-fixture", { exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("phone layout has no page-wide horizontal overflow", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixtureApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: "执行请求" }).click();
  await expect(
    page.getByText("azure-browser-fixture", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".raw-row")).toHaveCount(1);
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
  await expect(
    page.getByRole("region", { name: "日志发现", exact: true }),
  ).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: testInfo.outputPath("phone.png"),
    fullPage: true,
  });
});

test("historical lookup across Foundry resources without model calls", async ({
  page,
}, testInfo) => {
  const fixture = await fixtureApi(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByRole("tab", { name: "回查历史调用" }).click();
  await expect(page.getByRole("button", { name: "执行请求" })).toBeHidden();
  const scope = page.getByLabel("范围");
  await expect(scope.locator("option")).toHaveText([
    "整个 Workspace（所有 Foundry）",
    "EASTUS2-A · 7 次模型调用",
    "WESTUS-B · 3 次模型调用",
  ]);

  await page.getByRole("tab", { name: "按条件找候选" }).click();
  await page.getByRole("button", { name: "24 小时" }).click();
  await page.getByRole("button", { name: "搜索候选" }).click();
  const table = page.getByRole("table");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect(table).toContainText("WESTUS-B");
  await expect(table).toContainText("本应用");
  await expect(table).toContainText("6.7 tok/s");
  await page.getByRole("button", { name: "回查 ext-westus" }).click();
  const result = page.getByRole("region", { name: "回查结果" });
  await expect(result).toContainText("外部调用");
  await expect(result).toContainText("westus-b");
  await expect(page.getByRole("region", { name: "TPS 对比" })).toContainText(
    "回查仅含 Azure 日志，无客户端数据",
  );
  await expect(
    page.getByRole("region", { name: "调用时间轴", exact: true }),
  ).toContainText("Azure 服务端处理（RequestResponse）");
  const discovery = page.getByRole("region", { name: "日志发现", exact: true });
  await expect(discovery).toContainText("1/2 类日志已找到");
  await expect(
    discovery.getByRole("button", { name: /请求日志/ }),
  ).toContainText("CorrelationId = apim-request-id ✓");
  const raw = page.getByRole("region", { name: "原始 Log Analytics 日志" });
  await expect(raw.locator(".raw-line.hl-match")).toContainText(
    "= apim-request-id ✓",
  );
  const trace = fixture.lookupBodies.find(
    (item) => item.path === "/api/lookup/trace",
  );
  expect(trace?.body).toMatchObject({
    request_id: "ext-westus",
    scope: fixture.westus,
  });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: testInfo.outputPath("lookup-desktop.png"),
    fullPage: true,
  });

  await page.getByRole("tab", { name: "按 apim-request-id" }).click();
  await page.getByLabel("apim-request-id").fill("azure-browser-fixture");
  await page.getByRole("button", { name: "回查", exact: true }).click();
  await expect(result).toContainText("本应用发起");
  await page.getByRole("button", { name: "打开本地实验" }).click();
  await expect(page.getByRole("button", { name: "执行请求" })).toBeVisible();
  await expect(result).toBeHidden();
  await expect(
    page
      .getByText("azure-browser-fixture", { exact: true })
      .filter({ visible: true }),
  ).toHaveCount(1);
  expect(fixture.modelCalls()).toBe(0);
  expect(errors).toEqual([]);
});

test("phone lookup layout has no page-wide horizontal overflow", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixtureApi(page);
  await page.goto("/");
  await page.getByRole("tab", { name: "回查历史调用" }).click();
  await page.getByRole("tab", { name: "按条件找候选" }).click();
  await page.getByRole("button", { name: "搜索候选" }).click();
  await page.getByRole("button", { name: "回查 ext-westus" }).click();
  await expect(page.getByRole("region", { name: "回查结果" })).toBeVisible();
  await expect(page.locator(".raw-row")).toHaveCount(1);
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: testInfo.outputPath("phone-lookup.png"),
    fullPage: true,
  });
});
