# Foundry 请求关联实验台

Python/FastAPI + React/TypeScript 的单用户实验应用。先验证 **Client → Foundry**
调用的原生响应请求 ID 能否与现有 Log Analytics 诊断记录关联。

Client 指本应用后端；浏览器不持有 Azure Key 或 Token。首版仅支持 Azure 公有云
Azure OpenAI-compatible `/openai/v1/chat/completions`、非流式调用。
LiteLLM Gateway 路径、流式调用、内部模型调度追踪不在本版范围。

## 真实环境验证结论（2026-11-01，East US 2，gpt-5.4-nano，MI）

| 证据 | 结果 |
|---|---|
| 关联键 | `AzureDiagnostics.CorrelationId` **等于**响应头 `apim-request-id` |
| 匹配的日志类别 | `RequestResponse`（`chatcompletions_create`）与 `AzureOpenAIRequestUsage`（`completion`）共用同一个 `CorrelationId` |
| 不进入日志 | `x-request-id`、客户端发送的 `x-ms-client-request-id`（仅在响应中回显） |
| 服务端区间 | `RequestResponse.properties.requestTime`（100ns 时间单位）+ `DurationMs` |
| 模型阶段 | `AzureOpenAIRequestUsage.properties.timeToFirstTokenMs` / `timeToLastTokenMs`；非流式调用两者相同，表示完整输出耗时，UI 标为“模型完整输出（TTFT = TTLT）” |
| 用量 | `promptTokens`、`generatedTokens`、`cachedTokens`（数组形式）、部署名、模型版本、`streamType` |
| 入库延迟 | `ingestion_time()` 观测到约 50–110 秒（相对请求开始）；两个类别到达时间不同 |
| 响应体自带阶段 | `usage.latency_checkpoint`（`pre_inference_ms`、`engine_ttft_ms`、`service_ttft_ms` 等，仅相对值） |

因此无需 OpenTelemetry 或自定义日志即可做到**请求级**精确关联。`CorrelationId ↔ apim-request-id`
已作为内置规则直接判定为“精确匹配”；其他字段/响应头的相等仍只算“候选”，需要人工确认。

## 快速启动

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22.12+ 或 24。

```bash
uv sync --frozen
npm ci --prefix frontend
cp .env.example .env
# 在 .env 中配置 Azure 资源与身份，不要提交 .env
npm run build --prefix frontend
uv run uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。先构建前端再启动后端；后端同时提供静态 UI 和 API。

开发模式使用两个终端：

```bash
uv run uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000 --reload
npm run dev --prefix frontend
```

打开 Vite 输出的地址（通常为 <http://127.0.0.1:5173>），`/api` 自动代理到后端。

## 身份与配置

参考 [.env.example](.env.example)。启动时读取 `.env`；修改服务器密钥后需要重启。
Endpoint、Deployment、身份模式和日志关联参数也可以在界面中编辑，每次执行保存配置快照。
Key 只从后端环境读取，不提供浏览器输入/读取密钥的接口。

| 配置 | 说明 |
|---|---|
| `FOUNDRY_ENDPOINT` | Azure 资源根 URL，或以 `/openai/v1` 结尾的 URL，不是 Foundry 项目/Agent URL |
| `FOUNDRY_DEPLOYMENT` | 实际模型部署名称，不一定等于模型家族名称 |
| `LLM_AUTH_MODE` | `api_key` 或 `managed_identity` |
| `FOUNDRY_API_KEY` | 仅 API Key 模式需要 |
| `AZURE_CLIENT_ID` | 可选；用户分配 MI 的 Client ID，不是 Object ID |
| `LOGS_AUTH_MODE` | `managed_identity` 或本地开发用 `azure_cli` |
| `LOG_ANALYTICS_WORKSPACE_ID` | Workspace 的 GUID，不是 ARM Resource ID |
| `FOUNDRY_RESOURCE_ID` | 发出模型诊断日志的 Cognitive Services Account ARM Resource ID |
| `LOG_ID_FIELD` | 可选；留空使用内置规则 `CorrelationId ↔ apim-request-id`。仅在测试其他字段时设置 |
| `LOG_ID_MAPPING_VERIFIED` | 默认 `false`；自定义字段只有人工核验后才设 `true` |
| `DATABASE_PATH` | 默认 `.data/experiments.sqlite3` |
| `ALLOWED_HOSTS` | JSON 数组，默认仅 localhost/127.0.0.1/[::1]；反向代理需显式加入实际主机名 |

### Managed Identity

- 后端必须运行在启用了 MI 的 Azure 主机上。
- 在目标 Azure OpenAI 资源上给 MI 授予 `Cognitive Services OpenAI User` 或等效权限。
- 系统分配 MI 留空 Client ID；用户分配 MI 指定 Client ID。
- LLM 请求只使用 `ManagedIdentityCredential`，失败不会回退到 CLI 或 API Key。
- 连接检查只获取 Token，不调用模型；Token 获取成功不代表模型 RBAC、部署和配额已验证。

### 查询 Log Analytics

- API Key 无法查询日志，日志查询需要独立的 Entra 身份权限。
- 给 MI/开发者身份授予 Workspace 上的 `Log Analytics Reader` 或等效的最小表查询权限。
- 本地无 MI，或 MI 只有模型权限、没有 Workspace 读取权限时，可以使用 `az login` 并选择
  `azure_cli`，UI 明确显示为开发者身份。模型调用仍然只使用 MI。
- 本版读取 `AzureDiagnostics`。日志必须已经通过 Azure Diagnostic settings 导出到该 Workspace；
  不自动修改诊断设置、不创建 Azure 资源、不写入 Azure 日志表。
- Private Link、网络限制、其他云环境和其他日志表需要单独适配。

### 资源发现

配置卡顶部的「Foundry 资源」可以直接输入名称，或从下拉列表选择。

- 名称右侧的「Azure CLI / MI」开关切换发现身份，它与下方「日志查询身份」是同一设置；
  页面加载和切换身份时自动发现，「刷新」按钮重新读取列表、部署和诊断设置。
  修改 MI Client ID 后不会自动刷新，状态行会提示点击「刷新」。
- 只读，不修改任何 Azure 资源。
- 通过 Azure Resource Graph 列出该身份可访问的所有订阅中 `OpenAI` / `AIServices` 账户，最多 200 个。
- 选择后读取账户、部署和 Diagnostic settings，自动填写 Endpoint、Resource ID、Deployment，
  并在当前 Workspace 不在日志目标中时切换到第一个接收 `RequestResponse` 的 Workspace。
- 需要资源/资源组/订阅上的 `Reader`（读取 Diagnostic settings 和部署），以及 Workspace 的读取权限
  （解析 Workspace GUID）；缺权限时对应部分显示提示，其余字段仍可手动填写。
- 「日志去向」列出每个 Log Analytics 目标及是否包含请求/用量日志，并提示：
  - 没有任何 Workspace 接收 `RequestResponse`（例如只发往存储账户或 Event Hub）；
  - 当前 Workspace ID 不在该资源的诊断目标中；
  - `Dedicated`（资源专用表）模式，本版只查询 `AzureDiagnostics`。
- 由 Azure Policy 创建的诊断设置（例如 `setByPolicy-*`）同样会被识别；日志可能已经在策略指定的
  Workspace 中，不一定需要手工再加一份。

## 如何验证关联

1. 配置 Endpoint、Deployment、认证、Workspace ID 和 Foundry Resource ID。
2. 点击“检查身份与日志连接”：读取过去一天的有限日志样本，查看 ID 候选字段。
3. 执行一个小请求。每次执行可能产生费用；上限 4096 completion tokens，默认 256；
   没有 SDK 隐式重试、没有自动重放，最多同时运行 3 个模型请求。
4. 查看实际返回的 `apim-request-id`、`x-request-id` 等原生响应头，以及区域、实际服务模型、
   剩余配额等响应元数据。应用为每次调用发送一个随机的 `x-ms-client-request-id`；
   它只会被回显，不会写入诊断日志，因此只有 Azure 返回请求 ID 后才查询日志。
5. 日志查询限定目标 Resource ID 和调用开始前 5 分钟到后 15 分钟的事件时间窗口。
   日志延迟入库时继续查询相同窗口，不将查询时间当作请求时间。
   若 Endpoint 子域名与 Resource ID 的账户名不同（如调用 japaneast、却查询 eastus2），
   配置表单会立即告警，查不到记录时提示中也会说明；因为自定义子域名可能与账户名不同，
   这只是告警，不阻止调用。
6. 未配置 ID 字段时，检查已列出的常见候选字段，并记录“日志字段 = 响应头”形式的证据。
   `CorrelationId = apim-request-id` 属于内置的已验证规则，标记为“精确匹配”；
   其他相等组合标记为“候选”，先核验字段语义，再指定字段并勾选“我已核验”。
7. 点击“重新查日志”，把当前字段映射应用于已选实验；不会重新调用模型。
   此操作只更新关联字段与确认状态，保留原调用的 Endpoint、身份和日志目标。
8. 精确匹配通常返回 `RequestResponse` 与 `AzureOpenAIRequestUsage` 两条记录，逐条显示匹配证据和入库时间。
   只关联共享已验证 ID 的记录，不按 Prompt、Token 数或时间相近拼凑。

自动查询首次立即执行，随后等待 20、30、45、60、90、120 秒（约 6 分钟）。
成功调用会一直轮询到两个类别都出现；失败调用只等待 `RequestResponse`。
到期仍缺失时会说明缺失的类别，可以手动继续。初次启用诊断设置后，数据可能较久才可查询。
一直查不到时，先确认该 Foundry 的诊断设置确实发送到配置的 Workspace：
`az monitor diagnostic-settings list --resource <Foundry Resource ID>`。
订阅策略自动创建的设置可能发往另一个 Workspace 或存储账户。
同一请求最多显示 100 条记录；截断和 Azure 部分结果会明确标注。
实际 KQL（含资源和事件时间范围）可在 UI 查看。

## TPS 指标

TPS = 输出 token ÷ 时间。顶部指标卡显示端到端 TPS；调用时间轴卡片内的
“TPS · 同一请求的不同时间基准”并列 5 种口径（真实 MI 调用示例：49 completion tokens）：

| 口径 | 来源 | 公式 | 示例 |
| --- | --- | --- | --- |
| 端到端 | Client 实测 | `completion_tokens ÷ Client 总耗时` | 24.9 tok/s |
| HTTP 往返 | Client 实测 | `completion_tokens ÷（等待响应头 + 读取响应体）` | 30.6 tok/s |
| Azure 服务端 | `RequestResponse` | `properties.completionTokens ÷ DurationMs` | 62.7 tok/s |
| 模型输出 | `AzureOpenAIRequestUsage` | `Σ generatedTokens ÷ timeToLastTokenMs` | 65.2 tok/s |
| 引擎解码 | 响应体 `latency_checkpoint` | `(n − 1) ÷ (engine_ttlt_ms − engine_ttft_ms)`，缺失时退回 `1000 ÷ engine_tbt_ms` | 234.1 tok/s |

- 分母越接近模型生成阶段，TPS 越高；各口径不可互相替代，也不能相减推算网络耗时。
- `completion_tokens` 含 reasoning tokens；非流式调用的 `timeToLastTokenMs` 包含首 token 前的时间。
- Azure 口径只使用已匹配请求 ID 的日志记录；日志未入库或字段缺失时显示“—”和原因，不记为 0。

## 原始 Log Analytics 日志

页面底部按本次调用的 Azure 请求 ID 只读查询 `AzureDiagnostics` 完整行（不做字段投影），
并高亮关联 ID（标注“= apim-request-id ✓”）、时间与耗时、Token、操作/结果/模型、调用方等关键字段；
可切换“仅显示关键字段”。

- 关联到日志记录后自动读取，也可手动刷新；每个实验同一时间只执行一个原始查询。
- 最多显示 20 行；省略空列，并显示“有值列数/总列数”；`properties_s` 解析为 JSON。
- 超出 JS 安全整数范围的值（如 `requestTime`）以字符串保留精度；`Ingested` 是查询附加的 `ingestion_time()`。
- 原始行仅返回给当前页面：不写入 SQLite，不进入 JSON 导出。查询失败时不显示 Azure 错误详情。

## 回查历史调用

页面顶部切换到“回查历史调用”，只读查询 Log Analytics，不调用模型，结果不写入本地历史、SQLite 或导出。

**按 apim-request-id 回查**（默认整个 Workspace、最近 30 天，即诊断日志默认保留期）：

- 只有 Azure 响应头 `apim-request-id` 会写入日志（`CorrelationId`）。经 LiteLLM 转发时，
  使用 LiteLLM 透传的响应头 `llm_provider-apim-request-id`；`x-request-id`、`chatcmpl-…`、
  客户端请求 ID 都查不到。LiteLLM 重试时每次上游调用的 ID 不同，只能查到对应那一次。
- 结果包含来源 Foundry、精确匹配证据、结果码、服务端耗时、Token、Azure 时间轴、日志发现清单、TPS 和原始日志行
  （时间窗口收窄到记录前后 5 分钟）。回查结果没有客户端阶段，因此只能计算 2 种 Azure TPS 口径，
  其余口径显示原因。
- 若 ID 属于本应用发起的实验，标记“本应用发起”，可一键打开本地实验。
- 未找到时会列出常见原因（不是 apim-request-id、超出时间窗口或保留期、尚未入库、
  该 Foundry 未配置诊断设置或日志发往其他 Workspace）。

**按条件找候选**（适用于没有保存 ID 的情况）：

- 时间范围最长 7 天，最多显示 50 条（超出标注“已截断”），可按 Foundry、部署名、调用方 `objectId`、
  成功/失败和“仅模型调用”过滤；每行可直接回查完整时间轴。
- 日志中**没有可区分 API Key 的字段**：Key 调用通常没有 `objectId`，只能按时间、部署、结果缩小范围；
  Entra/MI 调用可按 `objectId` 过滤。

同一时间最多执行 2 个回查查询，超出立即返回 429；单次查询 45 秒超时。
接口：`POST /api/lookup/resources | candidates | trace | raw-logs`。

### 多个 Foundry 共用一个 Workspace

- 每个 Foundry 资源都要单独配置 Diagnostic settings，把 `RequestResponse` 与
  `AzureOpenAIRequestUsage` 发送到同一 Workspace。
- 回查默认查询整个 Workspace（`ResourceProvider = MICROSOFT.COGNITIVESERVICES`），按 `_ResourceId`
  标出每条记录来自哪个 Foundry；范围下拉列出最近 30 天写过日志的资源（最多 50 个），可限定单个资源。
- 需要 Workspace 级或 `AzureDiagnostics` 表级读取权限；只对单个资源有资源上下文权限时，无法按 Workspace 查询。
- 跨多个 Workspace 的查询本版不支持。“发起请求”路径的自动关联仍只查询配置中的 Foundry Resource ID。

## 可见性与隐私边界

- 调用轴按开始时间排序，显示本地实际采集的认证、HTTP 等待、响应头、响应读取、解析阶段，
  以及来自日志的 Azure 服务端处理区间（`requestTime + DurationMs`）和首/末 token 时长。
- 首/末 token 时长没有绝对起点，按 `requestTime` 近似对齐并在事件详情中注明；
  用量记录的 `TimeGenerated` 是汇总时间（比请求晚约 1 分钟），不会绘制到调用轴。
- 日志发现按类别（`RequestResponse`、`AzureOpenAIRequestUsage`）列出是否找到：已找到 / 候选待核验 /
  等待入库 / 未找到 / 不适用（失败或取消的调用通常没有用量日志）。卡片显示匹配字段、关键元数据和来源 Foundry；
  入库延迟只作为附注（请求开始后约 X s 可查询），查询过程以“第 n 次 · k 条”标签列出，不再绘制时间轴。
- 响应体的 `latency_checkpoint` 单独显示为相对阶段图，不与绝对时间轴混合；`*_tbt_ms`（token 间隔）不画条形，只列数值。Azure 未公开这些字段的定义，各值的起点不一定相同。
- 跨机器时钟可能不一致；本地区间耗时用单调时钟测量，不通过相减估算网络耗时。
- 取消只停止本地等待/读取，不能保证 Azure 停止执行或不计费。
- SQLite 保存配置快照、请求 ID、数值用量、阶段和脱敏关联证据；不保存 Prompt、回答、
  Key 或 Token。回答仅在服务进程内存中保留，重启后不恢复。
- Azure 日志记录与导出只提取允许的元数据字段；不显示 Prompt、Completion、任意请求头等内容。
- 底部原始日志面板显示 Azure 中存储的完整非空列，包括调用方 `objectId`（如 MI principal）、
  Azure 已遮蔽末段的 `CallerIPAddress`、租户/订阅/资源 ID。已验证的 `RequestResponse` 与
  `AzureOpenAIRequestUsage` 行不含 Prompt/Completion；但若 Workspace 中的行包含其他内容，也会原样显示。
- 回查候选列表会显示 Workspace 中**其他调用方**的 `objectId` 和 Azure 已遮蔽的 IP；
  只应在有权查看这些日志的环境中使用。回查结果仅在页面显示，不落盘。
- JSON 导出不包含回答；请求 ID、资源路径等实验元数据仍应作为内部数据保护。
- 此应用不配置任何 OpenTelemetry SDK/exporter，不发送自定义遥测。
  Azure SDK 可能间接依赖 OpenTelemetry API 包，但本应用没有注册遥测导出。

本工具面向可信本机/单用户，不带登录与多租户权限隔离。
**不要直接暴露到公网**。需要远程使用时，通过 SSH/VS Code 端口转发，或带身份验证的私有反向代理。
使用 HTTPS、严格限制访问者和允许的 Host；不应把 Azure 密钥能力开放给任意用户。
在容器部署时为 `.data` 挂载持久卷；重启未完成请求会标记为中断，不自动重试。

## 验证

```bash
uv run pytest -q
uv run ruff check backend tests
uv run ruff format --check backend tests
npm test --prefix frontend
npm run build --prefix frontend
npm run test:e2e --prefix frontend
```

首次运行浏览器测试若提示缺少 Chromium，在 `frontend` 目录执行
`npx playwright install chromium` 后重试。E2E 会启动并自动关闭独立的 5174 端口测试服务，
所有 Azure/API 响应均由浏览器测试隔离模拟，不修改真实实验数据库。

测试使用明确隔离的模拟 HTTP/Azure 响应，覆盖认证分支、取消、429 无重试、持久化脱敏、
查询延迟/部分失败、候选/确认关联、历史映射重查、调用时间轴与日志发现清单，以及基于真实日志结构的
`requestTime` 解码、数组 token 字段和“等待两个日志类别”轮询；回查覆盖查询注入与时间窗口校验、
本地实验标记、并发 429、错误脱敏、结果不落盘，以及多 Foundry 共用 Workspace（仅模拟数据）。
**Mock 测试不证明真实 Azure 关联成功**；真实验收需要配置自己的资源、身份与诊断字段。

主要代码：[Python API](backend/app.py)、[执行引擎](backend/engine.py)、
[日志关联](backend/azure.py)、[React UI](frontend/src/App.tsx)、[时间轴](frontend/src/Timeline.tsx)、
[日志发现](frontend/src/LogDiscovery.tsx)、[历史回查](frontend/src/Lookup.tsx)。

官方参考：[Managed Identity](https://learn.microsoft.com/azure/foundry-classic/openai/how-to/managed-identity)、
[诊断日志](https://learn.microsoft.com/azure/foundry/how-to/diagnostic-logging)、
[Azure Monitor Query Python SDK](https://learn.microsoft.com/python/api/overview/azure/monitor-query-readme)。

中文字体随应用本地打包，不请求第三方字体 CDN；Noto Sans SC 使用 SIL Open Font License，
生产构建附带 `OFL-NotoSansSC.txt`。
