import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  Database,
  Download,
  Fingerprint,
  FlaskConical,
  History as HistoryIcon,
  KeyRound,
  Play,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Square,
  Workflow,
} from "lucide-react";
import { LatencyBreakdown, Timeline } from "./Timeline";
import { LogDiscovery } from "./LogDiscovery";
import { ThroughputBreakdown } from "./Throughput";
import { RunRawLogs } from "./RawLogs";
import { Lookup } from "./Lookup";
import {
  DeploymentField,
  FoundryPicker,
  LogTargets,
  useFoundryDiscovery,
} from "./Discovery";
import { tpsBases } from "./metrics";
import { Badge, Json } from "./ui";
import {
  accountMismatch,
  api,
  checkSchema,
  configSchema,
  connectionSchema,
  eventName,
  historySchema,
  milliseconds,
  runSchema,
  type Check,
  type Connection,
  type History,
  type Run,
} from "./types";

export default function App() {
  const [config, setConfig] = useState<Connection | null>(null);
  const [keyReady, setKeyReady] = useState(false);
  const [prompt, setPrompt] = useState(
    "请用一句话解释分布式系统中的请求关联。",
  );
  const [tokens, setTokens] = useState(256);
  const [run, setRun] = useState<Run | null>(null);
  const [history, setHistory] = useState<History>([]);
  const [check, setCheck] = useState<Check | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState("");
  const [tab, setTab] = useState<"event" | "logs" | "query">("event");
  const [mode, setMode] = useState<"run" | "lookup">("run");
  const [lookupOpened, setLookupOpened] = useState(false);
  const selection = useRef("");

  const refreshHistory = useCallback(
    async () => setHistory(await api("/runs", historySchema)),
    [],
  );

  useEffect(() => {
    let current = true;
    Promise.all([api("/config", configSchema), api("/runs", historySchema)])
      .then(([value, items]) => {
        if (current) {
          setConfig(connectionSchema.parse(value));
          setKeyReady(value.api_key_configured);
          setHistory(items);
        }
      })
      .catch((err: unknown) => {
        if (current) setError(String(err));
      });
    return () => {
      current = false;
    };
  }, []);

  const active =
    run?.status === "queued" ||
    run?.status === "running" ||
    !!run?.logs.polling;
  const runId = run?.id;
  useEffect(() => {
    if (!runId || !active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const updated = await api(
          `/runs/${runId}`,
          runSchema,
          undefined,
          controller.signal,
        );
        if (selection.current !== runId) return;
        setRun(updated);
        await refreshHistory();
        timer = setTimeout(poll, 900);
      } catch (err: unknown) {
        if (!controller.signal.aborted)
          setError(`刷新失败：${String(err)}。可从历史记录重新打开。`);
      }
    }
    timer = setTimeout(poll, 400);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [runId, active, refreshHistory]);

  async function action(name: string, work: () => Promise<void>) {
    setBusy(name);
    setError("");
    try {
      await work();
    } catch (err: unknown) {
      setError(String(err));
    } finally {
      setBusy("");
    }
  }

  function update<K extends keyof Connection>(key: K, value: Connection[K]) {
    setConfig((current) => (current ? { ...current, [key]: value } : current));
    setCheck(null);
  }

  const discovery = useFoundryDiscovery(config, (change) => {
    setConfig((current) => (current ? change(current) : current));
    setCheck(null);
  });

  function selectRun(value: Run) {
    selection.current = value.id;
    setRun(value);
    setSelected("");
    setTab("event");
  }

  function openRun(id: string) {
    return action("load", async () => {
      const value = await api(`/runs/${id}`, runSchema);
      selectRun(value);
      setConfig(value.connection);
      setCheck(null);
      setMode("run");
    });
  }

  function switchMode(value: "run" | "lookup") {
    setMode(value);
    if (value === "lookup") setLookupOpened(true);
  }

  function exportRun() {
    if (!run) return;
    const { answer: _answer, ...metadata } = run;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(metadata, null, 2)], {
        type: "application/json",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `foundry-${run.id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  const event = run?.events.find((item) => item.id === selected);
  // ARM reports the real endpoint, so a custom subdomain is not a mismatch.
  const mismatch =
    config && discovery.detail?.endpoint !== config.endpoint
      ? accountMismatch(config.endpoint, config.resource_id)
      : null;
  const callDuration = run?.duration_ms ?? null;
  const totalTokens = run?.usage.total_tokens;
  const endToEndTps = run
    ? (tpsBases(run).find((item) => item.key === "client_total")?.value ?? null)
    : null;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark">
          <Workflow size={23} />
        </div>
        <div className="brand">
          <strong>
            Foundry<span> / Request Lab</span>
          </strong>
          <small>请求关联实验台</small>
        </div>
        <span className="top-label">
          <span className="status-dot" />
          原生日志 · 只读关联
        </span>
        <span className="version">LOCAL WORKBENCH / 01</span>
      </header>

      <main>
        <div className="page-heading">
          <div>
            <span className="eyebrow">OBSERVE. CORRELATE. VERIFY.</span>
            <h1>让每一次请求，都有据可循。</h1>
            <p>
              调用 Foundry，读取真实诊断日志，用请求 ID 验证链路，而不是猜测。
            </p>
          </div>
          <div className="mode-pill">
            <FlaskConical size={17} />
            直连实验 · 非流式
          </div>
        </div>

        <div className="route-picker">
          <div className="route active-route">
            <span className="radio-dot" />
            Client <ArrowRight size={15} />
            <strong>Azure Foundry</strong>
            <span className="route-tag">当前路径</span>
          </div>
          <div className="route disabled-route" aria-disabled="true">
            Client <ArrowRight size={15} />
            LiteLLM Gateway <ArrowRight size={15} />
            Foundry <span className="route-tag">后续支持</span>
          </div>
        </div>
        {error && (
          <div role="alert" className="alert error">
            <strong>操作未完成</strong>
            {error}
          </div>
        )}

        <div className="workspace">
          <aside>
            <section className="card config-card">
              <div className="section-heading">
                <h2>
                  <Settings2 size={18} />
                  连接配置
                </h2>
                <span className="quiet">服务端调用</span>
              </div>
              {!config ? (
                <p className="quiet">读取配置中…</p>
              ) : (
                <>
                  <FoundryPicker
                    discovery={discovery}
                    config={config}
                    onIdentity={(mode) => update("logs_auth_mode", mode)}
                  />
                  <label>
                    Foundry Endpoint
                    <input
                      value={config.endpoint}
                      onChange={(e) => update("endpoint", e.target.value)}
                      placeholder="https://resource.openai.azure.com"
                    />
                  </label>
                  <DeploymentField
                    detail={discovery.detail}
                    value={config.deployment}
                    onChange={(value) => update("deployment", value)}
                  />
                  <div className="field-label">模型认证</div>
                  <div className="segmented">
                    <button
                      className={config.auth_mode === "api_key" ? "chosen" : ""}
                      onClick={() => update("auth_mode", "api_key")}
                    >
                      <KeyRound size={14} />
                      API Key
                    </button>
                    <button
                      className={
                        config.auth_mode === "managed_identity" ? "chosen" : ""
                      }
                      onClick={() => update("auth_mode", "managed_identity")}
                    >
                      <ShieldCheck size={14} />
                      MI
                    </button>
                  </div>
                  <div
                    className={`credential-note ${config.auth_mode === "api_key" && !keyReady ? "warn" : ""}`}
                  >
                    {config.auth_mode === "api_key"
                      ? keyReady
                        ? "API Key 已在服务端配置，不会返回浏览器。"
                        : "服务端未配置 FOUNDRY_API_KEY。"
                      : "仅使用 Managed Identity；失败不会回退到其他凭据。"}
                  </div>
                  <label>
                    用户分配 MI Client ID <span className="quiet">可选</span>
                    <input
                      value={config.managed_identity_client_id}
                      onChange={(e) =>
                        update("managed_identity_client_id", e.target.value)
                      }
                      placeholder="留空使用系统分配身份"
                    />
                  </label>
                  <div className="divider" />
                  <h3 className="subheading">
                    <Database size={16} />
                    Log Analytics
                  </h3>
                  <LogTargets
                    detail={discovery.detail}
                    workspaceId={config.workspace_id}
                    onUse={(value) => update("workspace_id", value)}
                  />
                  <label>
                    Workspace ID
                    <input
                      value={config.workspace_id}
                      onChange={(e) => update("workspace_id", e.target.value)}
                      placeholder="Workspace GUID"
                    />
                  </label>
                  <label>
                    Foundry Resource ID
                    <textarea
                      rows={2}
                      value={config.resource_id}
                      onChange={(e) => update("resource_id", e.target.value)}
                      placeholder="/subscriptions/…/providers/Microsoft.CognitiveServices/accounts/…"
                    />
                  </label>
                  {mismatch && (
                    <div className="credential-note warn" role="alert">
                      Endpoint 账户 <code>{mismatch[0]}</code> 与 Resource ID
                      账户 <code>{mismatch[1]}</code> 不一致。日志查询按
                      Resource ID
                      过滤，若两者不是同一资源，将查不到这次调用的日志。
                    </div>
                  )}
                  <label>
                    日志查询身份
                    <select
                      value={config.logs_auth_mode}
                      onChange={(e) =>
                        update(
                          "logs_auth_mode",
                          e.target.value === "azure_cli"
                            ? "azure_cli"
                            : "managed_identity",
                        )
                      }
                    >
                      <option value="managed_identity">
                        Managed Identity（Azure 环境）
                      </option>
                      <option value="azure_cli">
                        Azure CLI（开发者身份，非 MI）
                      </option>
                    </select>
                  </label>
                  <p className="hint">
                    日志查询使用独立的 Entra 权限，不能使用模型 API Key；
                    同时用于上方 Foundry 资源发现。 内置规则：日志 CorrelationId
                    等于响应头 apim-request-id。
                  </p>
                  <label>
                    日志请求 ID 字段
                    <input
                      value={config.log_id_field}
                      onChange={(e) => {
                        update("log_id_field", e.target.value);
                        update("mapping_verified", false);
                      }}
                      placeholder="留空：内置 CorrelationId ↔ apim-request-id"
                      list="id-fields"
                    />
                  </label>
                  <datalist id="id-fields">
                    {check?.logs.id_fields.map((name) => (
                      <option key={name} value={name} />
                    ))}
                  </datalist>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={config.mapping_verified}
                      disabled={!config.log_id_field}
                      onChange={(e) =>
                        update("mapping_verified", e.target.checked)
                      }
                    />
                    我已核验该字段与 Azure 响应请求 ID 的对应关系
                  </label>
                  <button
                    className="button secondary full"
                    disabled={!!busy}
                    onClick={() =>
                      action("check", async () =>
                        setCheck(await api("/check", checkSchema, config)),
                      )
                    }
                  >
                    <ShieldCheck size={16} />
                    {busy === "check" ? "检查中…" : "检查身份与日志连接"}
                  </button>
                  <p className="hint">
                    不发起模型调用；MI Token 可用不等于模型 RBAC 已验证。
                  </p>
                  {check && (
                    <div className="check-results">
                      <div>
                        <Badge state={check.model.ok ? "success" : "error"} />
                        {check.model.message}
                      </div>
                      <div>
                        <Badge state={check.logs.state} />
                        {check.logs.message}
                      </div>
                      <details>
                        <summary>查看字段样本与实际 KQL</summary>
                        <Json value={check.logs} />
                      </details>
                    </div>
                  )}
                </>
              )}
            </section>
            <section className="card history-card">
              <div className="section-heading">
                <h2>
                  <HistoryIcon size={17} />
                  最近实验
                </h2>
                <button
                  className="icon-button"
                  aria-label="刷新历史"
                  onClick={() => action("history", refreshHistory)}
                >
                  <RefreshCw size={15} />
                </button>
              </div>
              {!history.length ? (
                <p className="quiet">暂无实验。先从一次小请求开始。</p>
              ) : (
                <div className="history-list">
                  {history.map((item) => (
                    <button
                      className={`history-item ${item.id === run?.id ? "current" : ""}`}
                      key={item.id}
                      disabled={!!busy}
                      onClick={() => void openRun(item.id)}
                    >
                      <span>
                        <strong>{item.deployment}</strong>
                        <small>
                          {new Date(item.created_at).toLocaleString()}
                        </small>
                      </span>
                      <Badge state={item.status} />
                    </button>
                  ))}
                </div>
              )}
            </section>
          </aside>

          <div className="content-column">
            <div className="mode-switch" role="tablist" aria-label="工作模式">
              <button
                role="tab"
                aria-selected={mode === "run"}
                onClick={() => switchMode("run")}
              >
                <Play size={14} />
                发起请求
              </button>
              <button
                role="tab"
                aria-selected={mode === "lookup"}
                onClick={() => switchMode("lookup")}
              >
                <Search size={14} />
                回查历史调用
              </button>
            </div>
            {lookupOpened && (
              <div hidden={mode !== "lookup"}>
                <Lookup connection={config} onOpenRun={openRun} />
              </div>
            )}
            <div hidden={mode !== "run"}>
              <section className="card request-card">
                <div className="section-heading">
                  <h2>
                    <Play size={18} />
                    发起一次请求
                  </h2>
                  <span className="quiet">
                    每次执行可能产生 Azure 费用 · 无自动重试
                  </span>
                </div>
                <label className="sr-only" htmlFor="prompt">
                  Prompt
                </label>
                <textarea
                  id="prompt"
                  className="prompt"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  maxLength={32000}
                  rows={3}
                />
                <div className="request-actions">
                  <label className="token-input">
                    Max completion tokens
                    <input
                      type="number"
                      min={1}
                      max={4096}
                      value={tokens}
                      onChange={(e) => setTokens(Number(e.target.value))}
                    />
                  </label>
                  <div className="actions">
                    {active && (
                      <button
                        className="button secondary"
                        disabled={!!busy}
                        onClick={() =>
                          action("cancel", async () => {
                            if (run)
                              setRun(
                                await api(
                                  `/runs/${run.id}/cancel`,
                                  runSchema,
                                  {},
                                ),
                              );
                          })
                        }
                      >
                        <Square size={14} />
                        {run?.status === "running" ? "取消调用" : "停止查询"}
                      </button>
                    )}
                    <button
                      className="button primary"
                      disabled={!config || !!busy || active || !prompt.trim()}
                      onClick={() =>
                        action("run", async () => {
                          const value = await api("/runs", runSchema, {
                            connection: config,
                            prompt,
                            max_completion_tokens: tokens,
                          });
                          selectRun(value);
                          await refreshHistory();
                        })
                      }
                    >
                      <Play size={15} />
                      {busy === "run" ? "提交中…" : "执行请求"}
                    </button>
                  </div>
                </div>
              </section>

              <div className="metrics">
                <div className="metric">
                  <span>模型调用</span>
                  <strong>
                    {run ? <Badge state={run.status} /> : "待执行"}
                  </strong>
                  <small>HTTP {run?.http_status ?? "—"}</small>
                </div>
                <div className="metric">
                  <span>Client 总耗时</span>
                  <strong>
                    {callDuration === null ? "—" : milliseconds(callDuration)}
                  </strong>
                  <small>包括身份准备，不代表模型内部耗时</small>
                </div>
                <div className="metric">
                  <span>Total tokens</span>
                  <strong>
                    {typeof totalTokens === "number" ? totalTokens : "—"}
                  </strong>
                  <small>来自模型响应；缺失不记为 0</small>
                </div>
                <div className="metric">
                  <span>输出 TPS · 端到端</span>
                  <strong>
                    {endToEndTps === null ? (
                      "—"
                    ) : (
                      <>
                        {endToEndTps.toFixed(1)}
                        <em> tok/s</em>
                      </>
                    )}
                  </strong>
                  <small>completion ÷ Client 总耗时；其他口径见时间轴</small>
                </div>
                <div className="metric">
                  <span>日志关联</span>
                  <strong>
                    {run ? <Badge state={run.logs.state} /> : "待验证"}
                  </strong>
                  <small>
                    {run?.logs.records.length ?? 0} 条记录 ·{" "}
                    {run?.logs.attempts ?? 0} 次查询
                  </small>
                </div>
              </div>

              <section className="card evidence-card">
                <div className="section-heading">
                  <h2>
                    <Fingerprint size={18} />
                    关联证据
                  </h2>
                  <div className="actions">
                    <button
                      className="button secondary small"
                      disabled={!run || active || !!busy}
                      onClick={() =>
                        action("query", async () => {
                          if (run)
                            setRun(
                              await api(`/runs/${run.id}/query`, runSchema, {
                                log_id_field:
                                  config?.log_id_field ??
                                  run.connection.log_id_field,
                                mapping_verified:
                                  config?.mapping_verified ??
                                  run.connection.mapping_verified,
                              }),
                            );
                        })
                      }
                    >
                      <RefreshCw size={14} />
                      重新查日志
                    </button>
                    <button
                      className="icon-button"
                      aria-label="导出脱敏实验记录"
                      disabled={!run}
                      onClick={exportRun}
                    >
                      <Download size={17} />
                    </button>
                  </div>
                </div>
                {run ? (
                  <>
                    <div className="id-strip">
                      <span>本地 run_id</span>
                      <code>{run.id}</code>
                    </div>
                    {Object.entries(run.request_ids).map(([key, value]) => (
                      <div className="id-strip" key={key}>
                        <span>{key}</span>
                        <code>{value}</code>
                      </div>
                    ))}
                    {!Object.keys(run.request_ids).some(
                      (key) => key !== "x-ms-client-request-id",
                    ) && (
                      <p className="hint">
                        尚未获得 Azure 请求 ID。不能仅凭时间相近认定请求对应。
                      </p>
                    )}
                    {Object.keys(run.response_meta).length > 0 && (
                      <div className="meta-grid" aria-label="响应元数据">
                        {Object.entries(run.response_meta).map(
                          ([key, value]) => (
                            <span key={key}>
                              <small>{key}</small>
                              {String(value)}
                            </span>
                          ),
                        )}
                      </div>
                    )}
                    <div className={`correlation-message ${run.logs.state}`}>
                      <Activity size={16} />
                      <span>
                        {run.logs.message || "模型请求结束后自动查询现有日志。"}
                      </span>
                    </div>
                    {run.error && (
                      <div className="alert error" role="alert">
                        {run.error}
                      </div>
                    )}
                    <details className="answer" open={!!run.answer}>
                      <summary>模型回答与响应 usage</summary>
                      <p className="answer-text">
                        {run.answer ??
                          "回答正文只在当前服务进程内保留；重启后不会恢复。"}
                      </p>
                      <Json value={run.usage} />
                    </details>
                  </>
                ) : (
                  <div className="empty-evidence">
                    <Fingerprint size={30} />
                    <div>
                      <strong>先收集 ID，再验证对应关系</strong>
                      <p>
                        这里将展示实际 Azure 请求 ID、查询结果和字段级匹配证据。
                      </p>
                    </div>
                  </div>
                )}
              </section>

              <div className="card timeline-card">
                <Timeline
                  events={run?.events ?? []}
                  selected={selected}
                  onSelect={(item) => {
                    setSelected(item.id);
                    setTab("event");
                  }}
                />
                <LatencyBreakdown usage={run?.usage ?? {}} />
                <ThroughputBreakdown run={run} />
                <LogDiscovery
                  logs={run?.logs ?? null}
                  events={run?.events ?? []}
                  usageExpected={
                    !run || !["failed", "cancelled"].includes(run.status)
                  }
                  selected={selected}
                  onSelect={(item) => {
                    setSelected(item.id);
                    setTab("event");
                  }}
                />
              </div>

              <section className="card details-card">
                <div className="tabs" role="tablist" aria-label="实验详情">
                  {(["event", "logs", "query"] as const).map((key) => (
                    <button
                      key={key}
                      role="tab"
                      aria-selected={tab === key}
                      onClick={() => setTab(key)}
                    >
                      {key === "event"
                        ? "阶段与事件"
                        : key === "logs"
                          ? "Azure 日志记录"
                          : "实际 KQL"}
                    </button>
                  ))}
                </div>
                <div className="detail-content">
                  {tab === "event" && (
                    <div className="event-layout">
                      <div className="event-list">
                        {run?.events.length ? (
                          run.events.map((item) => (
                            <button
                              key={item.id}
                              className={selected === item.id ? "selected" : ""}
                              onClick={() => setSelected(item.id)}
                            >
                              <CheckCircle2 size={14} />
                              <span>
                                {eventName(item.title)}
                                <small>
                                  {new Date(
                                    item.started_at,
                                  ).toLocaleTimeString()}
                                </small>
                              </span>
                              <ChevronRight size={13} />
                            </button>
                          ))
                        ) : (
                          <p className="quiet">尚无事件。</p>
                        )}
                      </div>
                      <div>
                        {event ? (
                          <>
                            <h3>{eventName(event.title)}</h3>
                            <Badge state={event.status} />
                            <Json value={event} />
                          </>
                        ) : (
                          <p className="quiet">
                            点击时间轴或事件，查看真实时点、来源和数据。
                          </p>
                        )}
                      </div>
                    </div>
                  )}
                  {tab === "logs" && (
                    <>
                      <p className="hint">
                        此处为关联用的精简元数据字段；完整原始行见页面底部（按需读取、不保存）。
                      </p>
                      {run?.logs.records.length ? (
                        run.logs.records.map((record, i) => (
                          <details
                            key={i}
                            className="log-record"
                            open={i === 0}
                          >
                            <summary>
                              {record.category || "Diagnostic"} ·{" "}
                              {record.timestamp}
                              <span className="quiet">
                                证据：
                                {record.matching_fields.join(", ") || "无"}
                                {record.ingested_at &&
                                  ` · 入库 ${record.ingested_at}`}
                              </span>
                            </summary>
                            <Json value={record.fields} />
                          </details>
                        ))
                      ) : (
                        <p className="quiet">
                          暂无匹配记录。查询延迟不等于请求未到达 Azure。
                        </p>
                      )}
                    </>
                  )}
                  {tab === "query" && (
                    <>
                      <p className="hint">
                        只读查询限定资源与时间窗口；重新查询不会再次调用模型。
                      </p>
                      <pre className="json">
                        {run?.logs.query || "执行日志查询后显示实际 KQL。"}
                      </pre>
                    </>
                  )}
                </div>
              </section>
              <RunRawLogs run={run} />
            </div>
            <footer>
              不接入 OpenTelemetry · 不写入 Azure 自定义日志 ·
              内部不可见阶段不作推测
            </footer>
          </div>
        </div>
      </main>
    </div>
  );
}
