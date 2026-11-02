# 端到端追踪：Client → 网关（LiteLLM / New API）→ Foundry

日期：2026-11-01  
状态：建议与结论。本文没有修改项目代码，也没有在 LiteLLM / New API 实例上做运行时验证。

依据：

- 本项目直连 Foundry 的实测结论，见 [README](./README.md)。
- LiteLLM 源码 `60fc121`（main），并抽查 `v1.81.0-stable`、`v1.83.7-stable`、`v1.83.10-stable`、`v1.83.14-stable`、`v1.100.5`、`v1.103.10` 等发布 tag。
- New API（`QuantumNous/new-api`）源码 `70e6af6`。

## 结论速览

1. **关联键只有一个：Azure 响应头 `apim-request-id`，它等于 `AzureDiagnostics.CorrelationId`。** 直连时已在 Chat / Responses、流式 / 非流式四种组合下验证。网关只是多了一跳，追踪的本质仍然是：`Client 调用 ↔ 网关请求 ID ↔ 每次上游 attempt 的 apim-request-id ↔ Azure 日志`。
2. **LiteLLM 可以把 Azure ID 返回给 Client，但它自己的数据库不存这个 ID。**
   - Client 可从响应头 `llm_provider-apim-request-id` 直接拿到 Azure ID，网关无需改动即可完成 L1（最终 attempt 的关联）。
   - SpendLogs / Admin UI、OTel v2 都不保存它。
   - 要追踪每一次重试（L2），需要加一个自定义 callback，且要求 LiteLLM ≥ `v1.83.10-stable`。
3. **New API 目前完全不记录 `apim-request-id`。**
   - 日志里的 `upstream_request_id` 是上游 New API 的 `X-Oneapi-Request-Id`，不是 Azure ID。
   - 非流式响应可能把 Azure 头原样带给 Client，流式大概率带不到。
   - 要完整追踪，需要给 New API 提 PR：在出站响应处采集，写入日志的 `other.admin_info`，不改表结构。
4. **路线：** 先做 L1（单一目标、关闭重试，四种组合打通），再做 L2（打开重试 / fallback，逐个 attempt 记录）。OpenTelemetry 是增强项，不是前置条件。

---

## 0. 端到端追踪原理

### 0.1 关联链

```text
Client run（run_id）
   │  请求头：x-litellm-call-id（LiteLLM 允许 Client 指定）
   │  响应头：x-litellm-call-id / X-Oneapi-Request-Id（网关请求 ID）
   ▼
网关请求（gateway_request_id）
   │  1 : N   ← 重试、fallback、换渠道，每次都是一个新的上游 attempt
   ▼
上游 attempt #0..N
   │  Azure 响应头 apim-request-id（每个 attempt 一个，彼此不同）
   ▼
AzureDiagnostics.CorrelationId
   ├─ Category = RequestResponse          （请求、状态、耗时）
   └─ Category = AzureOpenAIRequestUsage  （Token 用量）
```

### 0.2 三条基本事实

1. Azure 日志唯一收录的调用方可见请求 ID 是 `apim-request-id`，其他 ID 都不进日志（见 0.4）。
2. 网关不改变这一点，只带来两个新问题：
   - Azure ID 可能被网关“吞掉”：没有回传给 Client，也没有记进网关日志。
   - 一次网关请求对应多次上游调用（1:N）。
3. 只按 ID 精确相等来关联。拿不到 ID 时，明确标记“关联中断”，不靠时间、Token 数或 Prompt 去猜。

### 0.3 两个采集点

| 采集点 | 拿到什么 | 覆盖范围 | 代价 |
| --- | --- | --- | --- |
| Client 读取最终响应头 | 最终返回给 Client 的那次 attempt 的 Azure ID | L1 | LiteLLM 零改动；New API 仅非流式可能可行 |
| 网关逐 attempt 记录 | 每次上游调用的 Azure ID、状态、目标 | L2 | LiteLLM：callback；New API：PR 或私有补丁 |

当 `x-litellm-attempted-retries`、`x-litellm-attempted-fallbacks` 大于 0，或 New API 的 `use_channel` 链长度大于 1 时，Client 拿到的 ID 只对应最终那次 attempt。界面必须标为“部分”，不能显示为完整。

### 0.4 看起来像 ID、但不能用来关联的字段

| 字段 | 原因 |
| --- | --- |
| `x-request-id`、`x-ms-client-request-id` | 不进入 Azure 日志（直连已验证） |
| `chatcmpl-…` / `resp_…` | 响应体 ID，不进入 Azure 日志；LiteLLM SpendLogs 的 `request_id` 就是它 |
| LiteLLM `additional_headers["request-id"]` | 在 Azure 上取的是 `x-request-id` |
| LiteLLM `error_information.error_provider_request_id` | 存在 `x-request-id` 时优先取它；直到 `v1.103.10` 才识别 `apim-request-id`（`v1.100.5` 尚不识别） |
| New API `upstream_request_id` | 上游 New API 的 `X-Oneapi-Request-Id`，用于网关级联 |

### 0.5 加入网关后语义的变化

- **耗时：** Client 端耗时 = 网关开销 + 所有 attempt 的耗时 + 网络。各段计时边界不同、可能重叠，不能用“Client − 网关 − Azure”相减得出网络耗时。
- **身份：** Azure 日志里的调用方变成网关的 Key 或 MI，不再是原 Client。
- **资源：** 不同 attempt 可能打到不同的 Foundry 资源，要按实际资源和 Workspace 分别查日志。
- **延迟：** 日志入库约 50–110 秒，与直连相同，沿用现有轮询策略。
- **流式：** 响应头到达时就能拿到 Azure ID；但成功与否要以流结束（或 SSE error）为准。
- **缓存：** 缓存命中没有新的 Azure 调用，不能把旧 ID 当作本次调用的 ID。

### 0.6 完整性分级

| 层级 | 回答的问题 | 必需数据 |
| --- | --- | --- |
| L1 请求级 | 这次 Client 调用对应哪条 Azure 日志 | 网关请求 ID + 最终 attempt 的 `apim-request-id` |
| L2 逐次 attempt | 有没有重试、换渠道；每次结果如何 | 网关逐 attempt 的结构化记录 |
| L3 分布式性能 | 时间花在鉴权、路由还是上游 | Client / 网关的 span，以及统一的 trace context |

---

## 1. LiteLLM 全流程追踪如何实施

### 1.1 LiteLLM 在哪些地方带有 `apim-request-id`

| 位置 | 是否包含 Azure ID |
| --- | --- |
| 返回给 Client 的响应头 | **有**，名为 `llm_provider-apim-request-id` |
| DB `LiteLLM_SpendLogs` / Admin UI Logs | **没有**。没有对应列，`metadata` 只保存 `SpendLogsMetadata` 白名单字段 |
| callback 的 `StandardLoggingPayload.hidden_params.additional_headers` | **`v1.83.10-stable` 起有**，保留全部上游头；`v1.83.7-stable` 及更早只保留 `x-ratelimit-*` |
| OTel v1（`callbacks: ["otel"]`） | 间接有：放在 span 属性 `hidden_params` 的 JSON 里，版本要求同上 |
| OTel v2（`LITELLM_OTEL_V2=true`） | **没有**：header 和 `hidden_params` 被当作敏感键过滤掉 |

### 1.2 版本前提

- callback 方案要求 **≥ `v1.83.10-stable`**。部署版本可从响应头 `x-litellm-version` 读取。
- Client 指定 `x-litellm-call-id`、`x-litellm-spend-logs-metadata`：抽查 `v1.81.0-stable` 起已支持。
- 只做 L1（读响应头）对版本无特殊要求，但仍需实测确认该头能透传。

### 1.3 网关配置（`config.yaml`）

以下为结构示例。合并进现有配置时，不要覆盖现网路由。

```yaml
model_list:
  - model_name: foundry-trace
    litellm_params:
      model: azure/<部署名>
      api_base: os.environ/FOUNDRY_ENDPOINT        # Azure 资源根地址
      api_version: os.environ/AZURE_API_VERSION   # 按部署实测填写
      api_key: os.environ/FOUNDRY_API_KEY         # 用 MI 时删除此行

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY

litellm_settings:
  callbacks: custom_callbacks.proxy_handler_instance   # 1.5 节的逐次 attempt 记录
  turn_off_message_logging: true                       # 不记录 Prompt / Completion
  cache: false
  # enable_azure_ad_token_refresh: true                # 走 MI 时开启

router_settings:
  num_retries: 0      # 阶段 P1 基线；阶段 P2 改为 1–2，验证逐次 attempt
  fallbacks: []
```

要点：

- 取得 Azure ID **不需要** `forward_client_headers_to_llm_api`。该设置控制的是请求头方向（Client→Azure），而 `apim-request-id` 走响应头方向（Azure→Client）。
- 网关前如有 Nginx 或 Ingress，要保留 `llm_provider-*` 和 `x-litellm-*` 响应头，且 SSE 不能被缓冲。
- 使用 MI 时，Azure 日志里的身份是网关 MI。要验证实际使用的主体，排除凭据链回退到其他身份。
- Responses API 经 `azure/` provider 时，实际出站路径和 api-version 可能与直连 `/openai/v1` 不同，需要重新核对 Azure 日志匹配规则。

### 1.4 Client 侧（L1）

请求时由 Client 生成 ID，并让网关沿用它，从而省掉一层映射：

```bash
RUN_ID=$(uuidgen)
curl -sS -D headers.txt -o body.json "$LITELLM_BASE/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -H "x-litellm-call-id: $RUN_ID" \
  -H "x-litellm-spend-logs-metadata: {\"run_id\":\"$RUN_ID\"}" \
  -d '{"model":"foundry-trace","messages":[{"role":"user","content":"ping"}]}'

grep -iE '^(x-litellm-(call-id|attempted-retries|attempted-fallbacks|model-id|model-api-base|version|response-duration-ms|overhead-duration-ms)|llm_provider-apim-request-id):' headers.txt
```

响应头白名单（只保存这些，不保存任意头）：

| 响应头 | 用途 |
| --- | --- |
| `x-litellm-call-id` | 网关请求 ID；Client 指定时等于 `run_id` |
| `llm_provider-apim-request-id` | **最终 attempt 的 Azure ID**，直接用现有逻辑查 `CorrelationId` |
| `x-litellm-attempted-retries` / `x-litellm-attempted-fallbacks` | 大于 0 时 L1 只覆盖部分 attempt，需要到 callback 记录里取全部 |
| `x-litellm-model-id` / `x-litellm-model-api-base` | 实际命中的部署和资源，用于确定查哪个 Workspace |
| `x-litellm-response-duration-ms` / `x-litellm-overhead-duration-ms` | 网关自报耗时；流式下发头时流尚未结束，不能当作总耗时 |
| `x-litellm-version` | 判断 1.2 节的版本前提 |

`x-litellm-spend-logs-metadata` 会让 SpendLogs 的 `metadata.spend_logs_metadata` 带上 `run_id`，方便在 LiteLLM UI 中反查成本和 Token。SpendLogs 的 `litellm_call_id` 列也有索引，可直接用 call id 查询。

### 1.5 网关侧 callback（L2，逐 attempt 记录）

把下面的文件与 `config.yaml` 放在同一目录（容器部署时一起挂载）。它只输出白名单元数据，不输出消息内容。

```python
# custom_callbacks.py
import json
import sys
from collections.abc import Mapping

from litellm.integrations.custom_logger import CustomLogger

APIM_HEADER = "apim-request-id"


def _lower_keys(headers):
    if not isinstance(headers, Mapping):
        return {}
    return {str(k).lower(): v for k, v in headers.items()}


def _find_apim_id(payload, kwargs):
    # 成功：上游头带 llm_provider- 前缀；失败：可能是原始头名
    extra = _lower_keys((payload.get("hidden_params") or {}).get("additional_headers"))
    for key in ("llm_provider-" + APIM_HEADER, APIM_HEADER):
        if extra.get(key):
            return str(extra[key]), "hidden_params." + key
    exc = kwargs.get("exception")
    for headers in (
        getattr(exc, "litellm_response_headers", None),
        getattr(getattr(exc, "response", None), "headers", None),
    ):
        value = _lower_keys(headers).get(APIM_HEADER)
        if value:
            return str(value), "exception." + APIM_HEADER
    return None, None


class E2ETraceLogger(CustomLogger):
    def _emit(self, kwargs):
        try:
            payload = kwargs.get("standard_logging_object") or {}
            apim_id, apim_source = _find_apim_id(payload, kwargs)
            error = payload.get("error_information") or {}
            record = {
                "type": "e2e_attempt",
                "trace_id": payload.get("trace_id"),            # 同一逻辑请求的重试 / fallback 共享
                "litellm_call_id": payload.get("litellm_call_id"),
                "status": payload.get("status"),
                "call_type": payload.get("call_type"),
                "stream": payload.get("stream"),
                "model_group": payload.get("model_group"),
                "model_id": payload.get("model_id"),
                "api_base": payload.get("api_base"),
                "start_time": payload.get("startTime"),
                "end_time": payload.get("endTime"),
                "completion_start_time": payload.get("completionStartTime"),
                "azure_apim_request_id": apim_id,                # 没有就是 null，不伪造
                "apim_id_source": apim_source,
                "error_class": error.get("error_class"),
                "error_code": error.get("error_code"),
            }
            sys.stdout.write(json.dumps(record, default=str) + "\n")
            sys.stdout.flush()
        except Exception:
            pass  # 追踪失败不能影响业务请求

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._emit(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._emit(kwargs)


proxy_handler_instance = E2ETraceLogger()
```

- 输出目标：示例写到 stdout（JSON Lines），由容器日志采集，例如 Container Apps 采集到 Log Analytics。也可以改成写入受控文件或数据库表，但必须保持白名单字段。
- 在 P2 阶段必须实测：
  - 失败的 attempt 是否各自触发 failure 事件。最新 main 有 `dispatch_attempt_failure_handlers`，但旧版本行为未验证。
  - 同一逻辑请求的多个 attempt 是否共享 `trace_id`、是否共享 `litellm_call_id`。
  - 流式成功事件中是否带有上游头。
  - 没有 Azure 响应头的失败（超时、连接错误）是否输出 `null`。

### 1.6 关联查询

1. Client run 的 `run_id` 就是 `x-litellm-call-id`。
2. 在 callback 记录里按 `litellm_call_id` 找到对应的 `trace_id`，再按 `trace_id` 取出全部 attempt 及其 `azure_apim_request_id`。
3. 用这些 ID 查询 Azure。项目现有的 `log_query` / `BUILTIN_MAPPING` 可直接复用，见 [azure.py](./backend/azure.py)。手工查询示例：

```kusto
AzureDiagnostics
| where TimeGenerated between (datetime(2026-11-01T00:00:00Z) .. datetime(2026-11-01T01:00:00Z))
| where CorrelationId in ("<apim-id-1>", "<apim-id-2>")
| project TimeGenerated, Category, OperationName, CorrelationId,
          DurationMs = column_ifexists("DurationMs", real(null)),
          ResultSignature = column_ifexists("ResultSignature", ""), _ResourceId
| order by TimeGenerated asc
```

4. 成本和 Token：在 SpendLogs 中按 `litellm_call_id` 或 `metadata.spend_logs_metadata.run_id` 查询。

### 1.7 可选：OpenTelemetry（L3）

- LiteLLM 侧开启：`LITELLM_OTEL_V2=true`、`OTEL_EXPORTER=otlp_http`、`OTEL_ENDPOINT=<内部 Collector>`、`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content`。
- Client 必须自己创建 span 并注入 `traceparent`，LiteLLM 会沿用入站的 trace context。
- v2 的 span 不包含 Azure ID，所以要把 `azure_apim_request_id` 写在 Client span 属性或 callback 记录中。Azure 日志与 trace 仍通过这个 ID 显式关联。
- `forward_traceparent_to_llm_provider` 是面向自托管 LLM 的选项，不会让 Foundry 内部产生 span。

### 1.8 实施步骤与验收

| 阶段 | 内容 | 通过标准 |
| --- | --- | --- |
| P0 | 确认 LiteLLM 版本、部署方式，以及 callback 文件能否挂载 | `x-litellm-version` ≥ `v1.83.10-stable`；否则只做 L1 或先升级 |
| P1 | 单目标、`num_retries: 0`、关闭缓存；Chat / Responses × 流式 / 非流式 | 每次调用的三处值相等：响应头 `llm_provider-apim-request-id`、callback 中的 `azure_apim_request_id`、Azure `CorrelationId`；两类日志都能查到 |
| P2 | `num_retries: 1–2`，人为制造可重试错误（例如对第一个部署设置极低 TPM 以触发 429） | 每个失败 attempt 各有一条记录；有 Azure 响应头的 attempt 都能在 Azure 日志中查到；最终 attempt 的 ID 与 Client 响应头一致 |
| P3 | 可选 OTel | Client 与网关 span 构成父子关系；不采集消息内容 |

---

## 2. 给 New API 提 PR：如何写提示词让 AI 完成

### 2.1 现状（源码 `70e6af6`）

| 事实 | 位置 |
| --- | --- |
| 每个请求生成 `X-Oneapi-Request-Id`，写入响应头和 `logs.request_id`（有索引） | `middleware/request-id.go`、`model/log.go` |
| 出站时**只**读取上游的 `X-Oneapi-Request-Id`，写入 `UpstreamRequestIdKey`，最终落到 `logs.upstream_request_id`；不读 `apim-request-id` | `relay/channel/api_request.go` 的 `doRequest`（约 507 行） |
| 重试循环：每次 attempt 调用 `service.AppendUsedChannel`；失败 attempt 走 `ProcessChannelError`，开启错误日志时写 `RecordErrorLog` | `controller/relay.go`、`service/relay_error.go` |
| 消费日志（`GenerateTextOtherInfo`）和错误日志**都调用** `AppendRelayLogAdminInfo`，后者已写入 `use_channel` 等管理员字段 | `service/log_info_generate.go` |
| `LogOther.SetAdmin` 写入 `other.admin_info`，用户侧 API 会过滤掉，只有 admin / root 可见 | `model/log_other.go` |
| SSE 路径只复制少数指定头；非流式会复制大部分上游头 | `relay/helper/stream_scanner.go`、`service/http.go` |

### 2.2 建议的 PR 设计（交给 AI 的方向，需维护者认可）

- 在 `doRequest` 拿到响应后（无论状态码），按**固定白名单**（至少包含 `apim-request-id`）读取 provider 原生请求 ID。
- 每个 attempt 追加一条记录到请求级上下文，做法参考 `use_channel`。字段为 `retry_index`、`channel_id`、`status_code`、`header`、`value`；对长度、字符集、条目数都设上限。
- 在 `AppendRelayLogAdminInfo` 中写入 `other.admin_info.upstream_provider_request_ids`（字段名由维护者确定）。这样消费日志能拿到全部 attempt，错误日志能拿到当前及之前的 attempt。
- **不改表结构**，因此不需要做三库迁移验证。按 Azure ID 建索引搜索可作为后续 Issue（届时需要 SQLite / MySQL / PostgreSQL 真实验证）。
- **非目标：**
  - 不改变 `upstream_request_id` 的语义。
  - 不改返回给 Client 的响应头。“流式回传 `apim-request-id`”另开 Issue。
  - 不碰计费，不记录消息内容，不加依赖。

### 2.3 New API 仓库对 AI PR 的硬性规则

| 规则 | 对提示词的影响 |
| --- | --- |
| `CLAUDE.md` 要求先完整读取 `AGENTS.md` | 提示词的第一步就写明这一点 |
| 新功能必须先用 `.agents/github/ISSUE.md` 开 Issue；大改动要先与维护者达成一致 | 分两阶段：先 Issue，维护者认可后再写代码 |
| PR 正文用 `.agents/github/PR.md`；User request 一节**逐字引用**你的提示词 | **提示词里不能出现密钥、内部域名、客户名**，语言就用你希望出现在 PR 中的语言 |
| 非核心开发者要声明 AI 生成或辅助 | 要求 AI 在 PR 正文中声明 |
| Proof of Work 必须是真实执行的命令和观察到的结果，只写 `go test` 通过不算 | 验证步骤中必须包含真实 Azure 渠道调用 |
| 测试优先扩展已有文件；最多新增 1 个；使用 testify `require` / `assert`；表驱动 | 点名已有测试文件 |
| 涉及数据库必须做真实三库验证 | 明确“不改表”，必须改表时先停下来问 |
| JSON 用 `common.Marshal` 等；`gofmt`；遵循现代 Go 写法；单一聚焦改动 | 让 AI 遵守 `AGENTS.md`，不必在提示词里逐条重复 |
| 若触及 billing 定义，需完整读取 `.agents/rules/billing.md` | 让 AI 自行判断并说明理由 |

### 2.4 写提示词的要点

1. **固定上下文：** 仓库、fork、基线 commit、关联 Issue 编号。
2. **事实在前，期望在后：** 先写已核实的现象和代码证据，再写期望行为，最后写约束、非目标和验收标准。
3. **分阶段且必须停下：** 阶段 A 只调研并起草 Issue；阶段 B 实现、验证并起草 PR 正文。每个阶段结束都停下等人审核，**不让 AI 自动 push 或开 PR**。
4. **给落点，但允许依据证据调整：** 写明文件和函数，同时要求 AI 改动时说明理由。
5. **显式禁止项：** 不改表、不改计费、不改响应头、不全头透传、不记录内容、不加依赖。
6. **不确定就问：** 白名单范围、字段命名、可见性等问题不让 AI 自行决定。
7. **可核对的验收：** 四种组合加一个重试场景，最后与 Azure `CorrelationId` 逐一比对。

### 2.5 提示词模板 A：调研并起草 Issue

```text
你在 QuantumNous/new-api 仓库工作（fork：<fork-url>，基线 commit：<sha>）。

第一步：按 CLAUDE.md 的要求，用读文件工具完整读取仓库根目录的 AGENTS.md，并在整个任务中遵守。

背景（已核实）：
- 我们通过 new-api 的 Azure 渠道调用 Azure OpenAI。Azure 每次响应都带 apim-request-id 响应头，它等于 Azure 诊断日志 AzureDiagnostics 的 CorrelationId，是把一次调用对应到 Azure 侧日志的唯一可用键。
- 直连 Azure 时可以从响应头取得该值；经过 new-api 后，消费日志和错误日志中都没有它。
- 代码：relay/channel/api_request.go 的 doRequest 只读取上游的 X-Oneapi-Request-Id 写入 upstream_request_id（用于 new-api 级联），没有读取 apim-request-id；发生重试时，也无法区分每次 attempt 对应哪个 Azure 请求。

任务：只做调研并起草 Issue，不写代码，不提交 Issue。
1. 查阅 https://docs.newapi.ai/、https://deepwiki.com/QuantumNous/new-api、README 和代码，判断当前版本是否已能做到。如果已有配置或功能可以实现，直接告诉我并停止。
2. 搜索已有的 Issues 和 PRs，确认不重复。
3. 用 .agents/github/ISSUE.md 起草 Issue 正文（Kind = Feature），写入 issue-draft.md。只使用我下面提供的事实；缺少的不要编造，列出来问我。
4. 输出草稿后停止，等我确认。

我提供的事实：
- new-api 版本 / 镜像 tag：<填写，不能是 latest>
- 部署方式：<docker / 二进制>；数据库：<sqlite / mysql / postgres>
- 渠道类型：Azure OpenAI；接口：/v1/chat/completions 与 /v1/responses，流式与非流式
- 实际行为：logs 表的 request_id、upstream_request_id、other 中都没有 apim-request-id
- 影响：无法把 new-api 请求对应到 Azure 诊断日志，排查延迟和失败只能按时间人工猜测
- 频率：每次请求
- 证据：同一请求直连 Azure 时响应头含 apim-request-id；经 new-api 后日志中没有（附脱敏的 SQL 输出 / 截图）
```

### 2.6 提示词模板 B：实现、验证并起草 PR（Issue 获认可后使用）

```text
你在 QuantumNous/new-api 的 fork（<fork-url>）分支 feat/upstream-provider-request-id 上工作，基线 commit <sha>。关联 Issue：#<n>，维护者意见：<引用要点>。

第一步：用读文件工具完整读取 AGENTS.md 并遵守。判断本任务是否触及 AGENTS.md 中 billing 的定义：若触及，完整读取 .agents/rules/billing.md；若不触及，在交付说明中写明理由。

目标：在日志中记录每次上游 attempt 的 provider 原生请求 ID（首要是 Azure 的 apim-request-id），让管理员能从 new-api 日志定位到 Azure 诊断日志。

期望行为：
1. 每次 HTTP 上游请求拿到响应头后（不论状态码），按固定白名单读取请求 ID 头。白名单至少包含 apim-request-id；如果要加入其他头，需给出 provider 文档依据，并先问我。
2. 每个 attempt 追加一条记录：retry_index、channel_id、上游 HTTP 状态码、头名、值。值要有长度上限，只接受可打印 ASCII；条目总数有上限。
3. 记录写入 other.admin_info（仅 admin / root 可见）。成功的消费日志包含本请求的全部 attempt；开启错误日志时，错误日志至少包含当前失败的 attempt。优先复用 service.AppendRelayLogAdminInfo，因为消费日志和错误日志都会调用它。
4. 上游没有返回响应（超时、连接错误）时不写空值，也不伪造。

建议落点（以代码事实为准；需要调整时说明理由）：
- relay/channel/api_request.go 的 doRequest（现有读取 X-Oneapi-Request-Id 的位置附近）
- service/log_info_generate.go 的 AppendRelayLogAdminInfo
- 请求级累积方式参考 service.AppendUsedChannel / use_channel

约束与非目标：
- 不改数据库表结构、索引和迁移。如果你认为必须改，先停下来说明原因并问我。
- 不改变 upstream_request_id 和 X-Oneapi-Request-Id 的现有语义。
- 不改变返回给客户端的响应头（流式回传另开 Issue）。
- 不改计费，不记录请求或响应内容，不引入新依赖，不改前端；如需前端展示，先问我。
- 保持单一、聚焦的改动。

测试：
- 优先扩展已有测试文件（例如 relay/channel/api_request_test.go、service/relay_error_test.go、model/log_other_test.go），最多新增一个测试文件；使用 testify 的 require / assert；采用表驱动。
- 覆盖以下情况：
  - 有和没有 apim-request-id 头
  - 非 2xx 响应
  - 两次 attempt（第一次失败、第二次成功）后，消费日志包含两条记录
  - 用户可见的日志 API 不返回该字段
  - 超长值或非法值被丢弃

验证（必须真实执行，记录命令和脱敏后的输出）：
1. 运行相关包的 go test；gofmt -l 无输出；go build ./... 通过。
2. 用 SQLite 在本地启动 new-api，配置一个 Azure 渠道。凭据由我通过环境变量提供，不得写入任何文件或输出。
   - Chat 非流式、Chat 流式、Responses 非流式、Responses 流式各调用一次。
   - 从 logs 表或管理端日志 API 读取 other.admin_info，确认其中的 apim-request-id 与非流式响应头中的值一致。
   - 重试场景：同一模型配置两个渠道，让第一个渠道产生可重试错误（以 use_channel 中出现两个渠道为准），检查错误日志和消费日志里的 attempt 记录。
3. 列出全部采集到的 apim-request-id，我会用 KQL 在 Azure 侧核对 CorrelationId。

交付：
- 代码改动和测试。
- 用 .agents/github/PR.md 起草 PR 正文，写入 pr-draft.md：
  - 声明代码由 AI 辅助生成；
  - User request 一节逐字引用本提示词；
  - Verification 只写实际执行过的命令和结果；
  - 未验证项写入 Not verified，例如“未改动数据库，因此未做 MySQL / PostgreSQL 验证”。
- 不要 push，不要开 PR。完成后停下，等我审核。
- 任何不确定之处（白名单范围、字段命名、可见性）先问我，不要猜。
```

### 2.7 PR 合并前的过渡方案

- **非流式：** 先实测 Client 能否直接从 New API 响应头读到 `apim-request-id`（非流式会复制上游头）。能读到，就可以先做 L1。
- **流式，或需要 L2：** 只能在自有 fork 上打同样的补丁，或在 New API 与 Azure 之间加一层受控的出站代理。代理会增加 TLS 和凭据边界，不作为首选。

---

## 3. 推荐路线与待确认项

推荐顺序：

1. **LiteLLM L1：** Client 发送 `x-litellm-call-id`，读取 `llm_provider-apim-request-id`，复用现有 Azure 回查逻辑。网关零改动。
2. **LiteLLM L2：** 确认版本 ≥ `v1.83.10-stable` 后挂载 1.5 节的 callback，按 1.8 节的 P1 → P2 逐步验收。
3. **New API：** 先实测非流式响应头能否透传（过渡方案）；同时用模板 A 起草 Issue，维护者认可后用模板 B 实现。
4. **本项目：** 网关数据到位后，再按 attempt 数据模型改造连接配置、存储和时间轴（三泳道）。不先改代码。

待确认：

- LiteLLM 的部署版本与部署方式（能否挂载 callback 文件；callback 输出到哪里）。
- New API 的部署版本，是否为 fork，是否允许打私有补丁。
- 网关到 Azure 的认证方式：Key 还是 MI。是否要求 MI-only，New API 的 Azure 渠道是否支持 MI。
- 可能路由到的全部 Foundry 资源，以及它们的 Diagnostic settings 和 Workspace 映射。

## 4. 参考

- LiteLLM：[Logging / Custom Callback](https://docs.litellm.ai/docs/proxy/logging)、[Response Headers](https://docs.litellm.ai/docs/proxy/response_headers)。源码：`litellm/litellm_core_utils/litellm_logging.py`（`get_additional_headers`）、`litellm/types/utils.py`（`StandardLoggingPayload`）、`litellm/proxy/_types.py`（`SpendLogsMetadata`）、`schema.prisma`（`LiteLLM_SpendLogs`）、`litellm/proxy/common_request_processing.py`（`resolve_litellm_call_id`）、`litellm/integrations/otel/model/payloads.py`（敏感键过滤）。
- New API：`AGENTS.md`、`CLAUDE.md`、`.agents/github/ISSUE.md`、`.agents/github/PR.md`、`relay/channel/api_request.go`、`controller/relay.go`、`service/relay_error.go`、`service/log_info_generate.go`、`model/log.go`、`model/log_other.go`。
- Azure：[Monitor Azure OpenAI](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/monitor-openai)；本项目直连验证结论见 [README](./README.md)。
