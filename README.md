# Foundry 请求关联实验台

基于 Python/FastAPI + React/TypeScript 的单用户 Web 工具。后端调用 Azure Foundry 模型，读取响应中的原生请求 ID，
再与 Log Analytics 中的 Azure 诊断日志精确关联，在同一页面展示调用时间轴、日志发现、多口径 TPS 和原始日志行。

## 主要功能

- **发起请求**：Chat Completions / Responses，流式或非流式；模型认证支持 API Key 与 Managed Identity。
- **自动关联日志**：按 `apim-request-id` 轮询查询 `AzureDiagnostics`，逐字段展示匹配证据。
- **调用时间轴**：合并 Client 本地阶段、Azure 服务端处理区间和模型首/末 token 时长。
- **TPS 指标**：同一请求按 6 种时间基准计算输出速率。
- **原始日志**：按请求 ID 读取完整诊断日志行，并高亮关键字段。
- **回查历史调用**：按 `apim-request-id` 或时间、部署、调用方等条件查询 Workspace 中的任意调用。
- **资源发现**：列出当前身份可访问的 Foundry 资源，自动填写 Endpoint、部署和日志 Workspace。

适用于 Azure 公有云的 Azure OpenAI 兼容接口 `/openai/v1/chat/completions` 与 `/openai/v1/responses`（单轮文本输入）。
所有 Azure 调用都由后端发起，浏览器不持有 Key 或 Token。

## 关联原理

Azure 响应头 `apim-request-id` 等于 `AzureDiagnostics.CorrelationId`，Chat Completions 与 Responses 的流式、非流式调用均适用。
同一请求的两类诊断日志共用这个 `CorrelationId`：

| 日志类别 | OperationName | 提供的数据 |
|---|---|---|
| `RequestResponse` | Chat：`chatcompletions_create`；Responses：`create-response` | 结果码、Token 数，以及由 `properties.requestTime`（100ns 时间单位）+ `DurationMs` 构成的服务端处理区间 |
| `AzureOpenAIRequestUsage` | Chat：`completion`；Responses：`responses` | `timeToFirstTokenMs` / `timeToLastTokenMs`、`promptTokens`、`generatedTokens`、`cachedTokens`、部署名、模型版本、`streamType` |

- `x-request-id` 和客户端发送的 `x-ms-client-request-id` 不会写入诊断日志，不能用于关联。
- 非流式 Chat Completions 的 `timeToFirstTokenMs` 与 `timeToLastTokenMs` 相同，表示完整输出耗时，界面标为“模型完整输出（TTFT = TTLT）”。
- 非流式 Chat Completions 的响应体还包含 `usage.latency_checkpoint`（`pre_inference_ms`、`engine_ttft_ms`、`service_ttft_ms` 等相对耗时），
  界面单独显示为“服务端自报阶段”。
- 两类日志的入库时间不同，通常在请求后 1–3 分钟可以查询。

`CorrelationId ↔ apim-request-id` 是内置规则，直接判定为“精确匹配”；其他字段或响应头相等时标记为“候选”，人工核验后再确认。

## 快速启动

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22.12+ 或 24。

```bash
uv sync --frozen
npm ci --prefix frontend
cp .env.example .env
# 在 .env 中配置 Azure 资源与身份，不要提交 .env
./start.sh
```

打开 <http://127.0.0.1:8000>。后端同时提供静态 UI 和 API。

### start.sh

[start.sh](start.sh) 是日常启动入口，可在任意目录执行：

```bash
./start.sh              # 重启：停掉遗留后端 → 按需构建前端 → 启动（start / --start 等价）
./start.sh stop         # 停止本项目的后端服务（--stop 等价）；用 --port 启动的需指定相同端口
./start.sh new          # 全新启动：先清空 SQLite 数据库再启动（--new 等价）
./start.sh --port 8010  # 换端口；--host 同理，默认 127.0.0.1:8000
```

- **遗留进程**：只停止“命令行含 `backend.app:create_app`、工作目录为本项目、正在监听目标端口”的进程。
  先发 SIGTERM，`STOP_TIMEOUT`（默认 10 秒）内未退出再 SIGKILL。端口被其他程序占用或无法识别占用者时直接报错退出，
  请改用 `--port` 或自行处理。
- **`stop`**：按同样规则停止目标端口上的后端后退出，不构建前端、不启动服务。没有运行中的服务时直接成功返回；
  端口被其他程序占用时只提示。不能与 `new` 同时使用。
- **`new`**：删除 `DATABASE_PATH` 指向的数据库及其 `-wal`/`-shm`/`-journal` 文件，**实验历史无法恢复**；
  数据库在启动时自动重建。清空发生在旧进程已停止、端口确认空闲之后；前端构建失败、配置读取失败或端口无法释放时，
  数据库保持原样。
- **前端**：`frontend/dist` 不存在或比源码旧时才构建（缺 `node_modules` 时先 `npm ci`）。构建失败会取消启动，
  正在运行的旧进程保持不变。
- **多实例**：只处理目标端口上的进程。并行运行第二个实例时，请同时指定不同的 `--port` 和 `DATABASE_PATH`；
  否则后启动的实例会把先启动实例中进行中的请求标记为中断。
- 脚本在前台运行，Ctrl+C 停止，也可以在另一个终端执行 `./start.sh stop`。需要后台运行时用 `systemd`、tmux 等包装。

不使用脚本时，手工等价命令是：

```bash
npm run build --prefix frontend
uv run uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000
```

### 开发模式

开发模式（后端热重载、前端热更新）使用两个终端：

```bash
uv run uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000 --reload
npm run dev --prefix frontend
```

打开 Vite 输出的地址（通常为 <http://127.0.0.1:5173>），`/api` 自动代理到后端。

## 身份与配置

参考 [.env.example](.env.example)。启动时读取 `.env`；修改服务器密钥后需要重启。
Endpoint、Deployment、身份模式和日志关联参数也可以在界面中编辑，每次执行都会保存配置快照。
API Key 只从后端环境读取，浏览器无法输入或读取。

**上次调用即默认值**：每次点击「执行请求」都会把连接配置（Foundry、Deployment、身份、Workspace 等）、Prompt、
输出上限、调用接口和流式开关保存到 SQLite `preferences` 表，覆盖上一条；下次打开页面优先使用它，
配置卡顶部显示“默认值来自上次调用”和保存时间。点击「恢复 .env 默认」删除该记录，回到 `.env` 的值。
保存的值完整覆盖 `.env` 连接配置，因此修改 `.env` 后需要先恢复默认才会生效。

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
| `LOG_ID_FIELD` | 可选；留空使用内置规则 `CorrelationId ↔ apim-request-id`，需要按其他字段关联时设置 |
| `LOG_ID_MAPPING_VERIFIED` | 默认 `false`；自定义字段人工核验后设为 `true` |
| `DATABASE_PATH` | 默认 `.data/experiments.sqlite3` |
| `ALLOWED_HOSTS` | JSON 数组，默认仅 localhost/127.0.0.1/[::1]；使用反向代理时加入实际主机名 |

### Managed Identity

- 后端需要运行在启用了 MI 的 Azure 主机上。
- 在目标 Azure OpenAI 资源上给 MI 授予 `Cognitive Services OpenAI User` 或等效权限。
- 系统分配 MI 留空 Client ID；用户分配 MI 指定 Client ID。
- MI 模式下模型请求只使用 `ManagedIdentityCredential`。
- “检查身份与日志连接”只获取 Token、不调用模型；模型 RBAC、部署和配额以实际请求结果为准。

### 查询 Log Analytics

- 日志查询使用 Entra 身份，需要在 Workspace 上授予 `Log Analytics Reader` 或等效的表查询权限；API Key 不能用于查询日志。
- 本地开发或 MI 没有 Workspace 读取权限时，可以 `az login` 后选择 `azure_cli`，UI 会标明使用开发者身份；模型调用仍使用所选模型认证方式。
- 日志来源为 `AzureDiagnostics` 表，需要先通过 Azure Diagnostic settings 把 Foundry 日志导出到该 Workspace。

### 资源发现

配置卡顶部的「Foundry 资源」可以直接输入名称，或从下拉列表选择。

- 名称右侧的「Azure CLI / MI」开关切换发现身份，它与下方「日志查询身份」是同一设置；
  页面加载和切换身份时自动发现，「刷新」按钮重新读取列表、部署和诊断设置。
  修改 MI Client ID 后，状态行会提示点击「刷新」。
- **本地缓存**：资源列表和每个 Foundry 的部署/诊断设置按发现身份缓存在 SQLite `discovery_cache` 表。
  页面加载、切换身份、选择资源时优先读取缓存（状态行注明“缓存于 …”，超过 24 小时提示刷新）；
  未命中缓存时读取 Azure；已有缓存时点击「刷新」重新读取并覆盖缓存。
  刷新失败时继续显示旧缓存并提示原因。`./start.sh new` 会一并清空缓存。
- 发现过程为只读访问：通过 Azure Resource Graph 列出该身份可访问的所有订阅中的 `OpenAI` / `AIServices` 账户，最多 200 个。
- 选择后读取账户、部署和 Diagnostic settings，自动填写 Endpoint、Resource ID、Deployment，
  并在当前 Workspace 不在日志目标中时切换到第一个接收 `RequestResponse` 的 Workspace。
- 需要资源/资源组/订阅上的 `Reader`（读取 Diagnostic settings 和部署），以及 Workspace 的读取权限
  （解析 Workspace GUID）；缺少权限时对应部分显示提示，其余字段可手动填写。
- 「日志去向」列出每个 Log Analytics 目标及是否包含请求/用量日志，并提示：
  - 没有任何 Workspace 接收 `RequestResponse`（例如只发往存储账户或 Event Hub）；
  - 当前 Workspace ID 不在该资源的诊断目标中；
  - 使用 `Dedicated`（资源专用表）模式。
- 由 Azure Policy 创建的诊断设置（例如 `setByPolicy-*`）同样会被识别；日志可能已经在策略指定的 Workspace 中。

### 调用接口与流式

请求卡片中的「调用接口」切换 Chat Completions / Responses，「输出方式」切换非流式 / 流式。
页面顶部标签显示当前组合，历史列表的每条记录也标注其调用方式。

| 接口 | 请求 | 说明 |
|---|---|---|
| Chat Completions | `POST /chat/completions`，`messages` + `max_completion_tokens` | 流式时附带 `stream_options.include_usage`，以便在流中返回用量 |
| Responses | `POST /responses`，`input` + `max_output_tokens`，固定 `store:false` | 输出上限最少 16（切换时自动提升）；`store:false` 使 Azure 不保留响应对象 |

- 流式按 SSE 逐事件读取，回答实时显示。调用时间轴用“接收流式响应”区间替代“读取/解析响应”，
  并增加“首个内容增量（Client 实测首 token）”点事件；区间详情包含 SSE 事件数、内容增量数、字节数、
  首/末增量相对时间。
- 流在完成事件前结束、收到 `error` / `response.failed` 事件或 `incomplete`（如达到输出上限且无文本）时记为失败；
  响应体与流的累计大小上限为 4 MiB。失败时界面显示错误码。

## 关联流程

1. 配置 Endpoint、Deployment、认证、Workspace ID 和 Foundry Resource ID（可通过「Foundry 资源」自动填写）。
2. 点击“检查身份与日志连接”：读取过去一天的少量日志样本，列出可用的 ID 字段。
3. 执行请求。每次执行可能产生费用；输出上限最多 4096 tokens，默认 256（Responses 最少 16）；
   请求不自动重试，最多同时运行 3 个模型请求。
4. 查看 Azure 返回的 `apim-request-id`、`x-request-id` 等响应头，以及区域、实际服务模型、剩余配额等响应元数据。
   应用会为每次调用发送一个随机的 `x-ms-client-request-id`。
5. 收到 Azure 请求 ID 后自动查询日志，范围限定为目标 Resource ID、调用开始前 5 分钟到后 15 分钟的事件时间窗口。
   若 Endpoint 子域名与 Resource ID 的账户名不一致（如调用 japaneast、却查询 eastus2），配置表单会立即提示；
   查不到记录时的提示中也会说明。
6. 默认使用内置规则，同时检查常见候选字段，以“日志字段 = 响应头”的形式记录证据。
   其他相等组合标记为“候选”，核验字段语义后指定字段并勾选“我已核验”。
7. 点击“重新查日志”，把当前字段映射应用于所选记录；只重新查询日志，并保留原调用的 Endpoint、身份和日志目标。
8. 精确匹配通常返回 `RequestResponse` 与 `AzureOpenAIRequestUsage` 两条记录，逐条显示匹配证据和入库时间。

自动查询首次立即执行，随后等待 20、30、45、60、90、120 秒（约 6 分钟）。
成功调用会一直轮询到两个类别都出现；失败调用只等待 `RequestResponse`。
到期仍缺失时会说明缺失的类别，可以手动继续查询。初次启用诊断设置后，数据可能需要更久才可查询。
一直查不到时，先确认该 Foundry 的诊断设置确实发送到配置的 Workspace：
`az monitor diagnostic-settings list --resource <Foundry Resource ID>`。
订阅策略自动创建的设置可能发往另一个 Workspace 或存储账户。
同一请求最多显示 100 条记录，截断和 Azure 部分结果会明确标注。实际 KQL（含资源和事件时间范围）可在 UI 中查看。

## TPS 指标

TPS = 输出 token ÷ 时间。顶部指标卡显示端到端 TPS；调用时间轴卡片内的“TPS · 同一请求的不同时间基准”并列 6 种口径。
输出 token 取 `completion_tokens`（Chat）或 `output_tokens`（Responses）：

| 口径 | 来源 | 公式 |
| --- | --- | --- |
| 端到端 | Client 实测 | `输出 tokens ÷ Client 总耗时` |
| HTTP 往返 | Client 实测 | `输出 tokens ÷（等待响应头 + 读取响应体或接收流）` |
| 流式解码 | Client 实测（仅流式） | `(输出 − reasoning − 1) ÷ (末个内容增量 − 首个内容增量)` |
| Azure 服务端 | `RequestResponse` | `properties.completionTokens ÷ DurationMs` |
| 模型输出 | `AzureOpenAIRequestUsage` | `Σ generatedTokens ÷ timeToLastTokenMs` |
| 引擎解码 | 响应体 `latency_checkpoint` | `(n − 1) ÷ (engine_ttlt_ms − engine_ttft_ms)`，缺失时退回 `1000 ÷ engine_tbt_ms` |

- 分母越接近模型生成阶段，TPS 越高；各口径反映不同阶段，不可互相替代。
- 输出 tokens 含 reasoning tokens；非流式 Chat Completions 的 `timeToLastTokenMs` 包含首 token 前的时间。
  流式解码口径减去 reasoning tokens，因为它们在首个可见增量之前生成。
- 流式解码测量的是 Client 收到内容增量的间隔。Azure 默认内容过滤会缓冲输出并
  [分块返回](https://learn.microsoft.com/azure/ai-foundry/openai/concepts/content-streaming#comparison-of-content-filtering-modes)，
  因此该值反映到达速率，可能远高于模型解码速度；部署改用 Asynchronous Filter 后更接近逐 token 速率。
- Azure 口径使用已匹配请求 ID 的日志记录；日志未入库或字段缺失时显示“—”和原因。

## 原始 Log Analytics 日志

页面底部按本次调用的 Azure 请求 ID 查询 `AzureDiagnostics` 完整行，
并高亮关联 ID（标注“= apim-request-id ✓”）、时间与耗时、Token、操作/结果/模型、调用方等关键字段；
可切换“仅显示关键字段”。

- 关联到日志记录后自动读取，也可手动刷新。
- 最多显示 20 行；省略空列，并显示“有值列数/总列数”；`properties_s` 解析为 JSON。
- 超出 JS 安全整数范围的值（如 `requestTime`）以字符串保留精度；`Ingested` 列为 `ingestion_time()`。
- 原始行只在当前页面显示，不保存到本地数据库，也不包含在 JSON 导出中。

## 回查历史调用

页面顶部切换到“回查历史调用”，直接查询 Log Analytics，结果只在页面中显示。

**按 apim-request-id 回查**（默认整个 Workspace、最近 30 天，即诊断日志默认保留期）：

- 输入 Azure 响应头 `apim-request-id`（即日志中的 `CorrelationId`）。经 LiteLLM 转发时，
  使用 LiteLLM 透传的响应头 `llm_provider-apim-request-id`。LiteLLM 重试时每次上游调用的 ID 不同，
  可查到的是对应的那一次上游调用。
- 结果包含来源 Foundry、精确匹配证据、结果码、服务端耗时、Token、Azure 时间轴、日志发现清单、TPS 和原始日志行
  （时间窗口收窄到记录前后 5 分钟）。回查结果只有 Azure 侧数据，可计算 2 种 Azure TPS 口径。
- 若 ID 属于本应用发起的请求，标记“本应用发起”，可一键打开本地记录。
- 未找到时列出常见原因（不是 apim-request-id、超出时间窗口或保留期、尚未入库、
  该 Foundry 未配置诊断设置或日志发往其他 Workspace）。

**按条件找候选**（适用于没有保存 ID 的情况）：

- 时间范围最长 7 天，最多显示 50 条（超出时标注“已截断”），可按 Foundry、部署名、调用方 `objectId`、
  成功/失败和“仅模型调用”过滤；每行可直接回查完整时间轴。
- API Key 调用通常没有 `objectId`，可按时间、部署和结果缩小范围；Entra/MI 调用可按 `objectId` 过滤。

同一时间最多执行 2 个回查查询，超出时返回 429；单次查询超时为 45 秒。
接口：`POST /api/lookup/resources | candidates | trace | raw-logs`。

### 多个 Foundry 共用一个 Workspace

- 每个 Foundry 资源都要单独配置 Diagnostic settings，把 `RequestResponse` 与
  `AzureOpenAIRequestUsage` 发送到同一 Workspace。
- 回查默认查询整个 Workspace（`ResourceProvider = MICROSOFT.COGNITIVESERVICES`），按 `_ResourceId`
  标出每条记录来自哪个 Foundry；范围下拉列出最近 30 天写过日志的资源（最多 50 个），可限定单个资源。
- 需要 Workspace 级或 `AzureDiagnostics` 表级读取权限。
- 每次回查针对配置中的一个 Workspace；“发起请求”的自动关联查询配置中的 Foundry Resource ID。

## 时间轴与日志发现

- 调用时间轴按开始时间排序，显示本地采集的认证、HTTP 等待、响应头、响应读取、解析阶段，
  以及来自日志的 Azure 服务端处理区间（`requestTime + DurationMs`）和首/末 token 时长。
- 首/末 token 时长按 `requestTime` 近似对齐，事件详情中有标注；用量记录的 `TimeGenerated` 是汇总时间，不绘制到时间轴。
- 日志发现按类别（`RequestResponse`、`AzureOpenAIRequestUsage`）显示状态：已找到 / 候选待核验 /
  等待入库 / 未找到 / 不适用（失败或取消的调用通常没有用量日志）。卡片显示匹配字段、关键元数据和来源 Foundry，
  并附注入库延迟（请求开始后约 X s 可查询）；查询过程以“第 n 次 · k 条”标签列出。
- 响应体的 `latency_checkpoint` 单独显示为相对阶段图；`*_tbt_ms`（token 间隔）只列数值。
- 本地区间耗时用单调时钟测量；Azure 日志使用服务端时钟，两者之间可能存在时钟偏差。
- 取消只停止本地等待和读取，Azure 侧可能仍会完成执行并计费。

## 数据与隐私

- SQLite 保存配置快照、请求 ID、数值用量、阶段和脱敏关联证据；实验记录不保存 Prompt、回答、Key 或 Token。
  回答仅保留在服务进程内存中，重启后清除。
- `preferences` 表保存**最近一次提交的 Prompt**（作为下次默认值），每次提交覆盖、只保留一条；
  「恢复 .env 默认」或 `./start.sh new` 会删除它。请勿在 Prompt 中放入不应落盘的内容。
- `discovery_cache` 表保存资源发现结果（订阅/资源名、Endpoint、部署、诊断设置目标），不含密钥或 Token。
- Responses 调用固定发送 `store:false`，Azure 不保留响应对象；诊断日志照常写入，可正常关联。
- 关联记录与导出只提取允许的元数据字段，不包含 Prompt、Completion 或任意请求头。
- 底部原始日志面板显示 Azure 中存储的完整非空列，包括调用方 `objectId`（如 MI principal）、
  Azure 已遮蔽末段的 `CallerIPAddress`、租户/订阅/资源 ID。`RequestResponse` 与 `AzureOpenAIRequestUsage`
  行不含 Prompt/Completion；Workspace 中的其他内容会按原样显示。
- 回查候选列表会显示 Workspace 中其他调用方的 `objectId` 和 Azure 已遮蔽的 IP，请在有权查看这些日志的环境中使用。
- JSON 导出不包含回答；请求 ID、资源路径等元数据应作为内部数据保护。

## 部署与安全

本工具面向可信的本机/单用户环境，不包含登录与多租户权限隔离。
**不要直接暴露到公网**。需要远程使用时，通过 SSH/VS Code 端口转发，或带身份验证的私有反向代理，
并使用 HTTPS、严格限制访问者和允许的 Host。
容器部署时为 `.data` 挂载持久卷；服务重启时，未完成的请求会被标记为中断。

## 开发与测试

```bash
uv run pytest -q
uv run ruff check backend tests
uv run ruff format --check backend tests
npm test --prefix frontend
npm run build --prefix frontend
npm run test:e2e --prefix frontend
uvx --from shellcheck-py shellcheck start.sh
```

首次运行浏览器测试时若提示缺少 Chromium，在 `frontend` 目录执行 `npx playwright install chromium`。
E2E 测试使用独立的 5174 端口服务，不影响正在运行的实例和数据库。

主要代码：[Python API](backend/app.py)、[执行引擎](backend/engine.py)、
[日志关联](backend/azure.py)、[资源发现](backend/discovery.py)、[React UI](frontend/src/App.tsx)、
[时间轴](frontend/src/Timeline.tsx)、[日志发现](frontend/src/LogDiscovery.tsx)、[历史回查](frontend/src/Lookup.tsx)。

## 参考

- [Managed Identity](https://learn.microsoft.com/azure/foundry-classic/openai/how-to/managed-identity)
- [诊断日志](https://learn.microsoft.com/azure/foundry/how-to/diagnostic-logging)
- [Azure Monitor Query Python SDK](https://learn.microsoft.com/python/api/overview/azure/monitor-query-readme)

中文字体随应用本地打包；Noto Sans SC 使用 SIL Open Font License，生产构建附带 `OFL-NotoSansSC.txt`。
