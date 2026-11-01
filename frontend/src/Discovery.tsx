import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { CheckCircle2, ChevronDown, RefreshCw } from "lucide-react";
import {
  api,
  foundriesSchema,
  foundryDetailSchema,
  resourceName,
  type Connection,
  type Deployment,
  type Foundries,
  type Foundry,
  type FoundryDetail,
  type LogTarget,
} from "./types";

type Change = (update: (current: Connection) => Connection) => void;
export type FoundryDiscovery = ReturnType<typeof useFoundryDiscovery>;

const destinationLabels = {
  storage: "存储账户",
  event_hub: "事件中心",
  partner: "合作伙伴",
};

function identityBody(config: Connection) {
  return {
    logs_auth_mode: config.logs_auth_mode,
    managed_identity_client_id:
      config.logs_auth_mode === "managed_identity"
        ? config.managed_identity_client_id
        : "",
  };
}

export function identityLabel(config: Connection): string {
  if (config.logs_auth_mode === "azure_cli") return "Azure CLI 身份";
  return config.managed_identity_client_id ? "用户分配 MI" : "系统分配 MI";
}

function same(a: string, b: string): boolean {
  return a.toLowerCase() === b.toLowerCase();
}

function failure(err: unknown): string {
  const text = String(err);
  return text.includes("HTTP 422")
    ? "身份参数无效：用户分配 MI Client ID 必须是 GUID。"
    : text;
}

// Keep user choices that remain valid for the selected Foundry.
export function applyDetail(
  current: Connection,
  detail: FoundryDetail,
): Connection {
  const deployments = detail.deployments.map((item) => item.name);
  const usable = detail.workspaces.filter(
    (item) => item.workspace_id && item.request_logs,
  );
  return {
    ...current,
    endpoint: detail.endpoint || current.endpoint,
    resource_id: detail.resource_id,
    deployment: deployments.includes(current.deployment)
      ? current.deployment
      : (deployments[0] ?? current.deployment),
    workspace_id: usable.some(
      (item) => item.workspace_id === current.workspace_id,
    )
      ? current.workspace_id
      : (usable[0]?.workspace_id ?? current.workspace_id),
  };
}

export function useFoundryDiscovery(config: Connection | null, change: Change) {
  const [list, setList] = useState<Foundries | null>(null);
  const [listing, setListing] = useState(false);
  const [detail, setDetail] = useState<FoundryDetail | null>(null);
  const [loading, setLoading] = useState("");
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [source, setSource] = useState("");
  const latest = useRef(config);
  const listRequest = useRef<AbortController | null>(null);
  const detailRequest = useRef<AbortController | null>(null);
  const detailKey = useRef("");
  latest.current = config;

  async function refresh() {
    const current = latest.current;
    if (!current) return;
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setListing(true);
    setListError("");
    try {
      setList(
        await api(
          "/discovery/foundries",
          foundriesSchema,
          identityBody(current),
          controller.signal,
        ),
      );
      setSource(identityLabel(current));
      // A manual refresh also re-reads deployments and diagnostic settings.
      detailKey.current = "";
    } catch (err: unknown) {
      if (!controller.signal.aborted) setListError(failure(err));
    } finally {
      if (listRequest.current === controller) setListing(false);
    }
  }

  async function load(resourceId: string, select: boolean) {
    const current = latest.current;
    if (!current) return;
    detailRequest.current?.abort();
    const controller = new AbortController();
    detailRequest.current = controller;
    detailKey.current = resourceId.toLowerCase();
    setLoading(resourceId);
    setDetailError("");
    try {
      const value = await api(
        "/discovery/foundry",
        foundryDetailSchema,
        { ...identityBody(current), resource_id: resourceId },
        controller.signal,
      );
      setDetail(value);
      if (select && value.state !== "error")
        change((config) => applyDetail(config, value));
    } catch (err: unknown) {
      if (!controller.signal.aborted) {
        detailKey.current = "";
        setDetailError(failure(err));
      }
    } finally {
      if (detailRequest.current === controller) setLoading("");
    }
  }

  function select(item: Foundry) {
    change((config) => ({
      ...config,
      resource_id: item.resource_id,
      endpoint: item.endpoint || config.endpoint,
    }));
    void load(item.resource_id, true);
  }

  const mode = config?.logs_auth_mode;
  useEffect(() => {
    if (!mode) return;
    setList(null);
    setDetail(null);
    detailKey.current = "";
    void refresh();
    return () => {
      listRequest.current?.abort();
      detailRequest.current?.abort();
    };
    // Refresh automatically only when the identity type changes, not per keystroke.
  }, [mode]);

  // Silently follow resource IDs set elsewhere, e.g. when opening a history run.
  const resourceId = config?.resource_id ?? "";
  useEffect(() => {
    if (!list || !resourceId || detailKey.current === resourceId.toLowerCase())
      return;
    if (list.items.some((item) => same(item.resource_id, resourceId)))
      void load(resourceId, false);
  }, [list, resourceId]);

  const current =
    detail && resourceId && same(detail.resource_id, resourceId)
      ? detail
      : null;
  return {
    list,
    source,
    listing,
    detail: current,
    loading,
    listError,
    detailError,
    refresh,
    select,
  };
}

export function FoundryPicker({
  discovery,
  config,
}: {
  discovery: FoundryDiscovery;
  config: Connection;
}) {
  const [text, setText] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [notice, setNotice] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  const items = discovery.list?.items ?? [];
  const query = text?.trim().toLowerCase() ?? "";
  const shown = query
    ? items.filter((item) =>
        [item.name, item.location, item.subscription, item.resource_group].some(
          (value) => value.toLowerCase().includes(query),
        ),
      )
    : items;
  const value =
    text ?? (config.resource_id ? resourceName(config.resource_id) : "");
  const detail = discovery.detail;

  function choose(item: Foundry) {
    setText(null);
    setOpen(false);
    setNotice("");
    discovery.select(item);
  }

  function commit() {
    if (text === null) return;
    const name = text.trim();
    if (!name) {
      setText(null);
      return;
    }
    const match = items.find((item) => same(item.name, name));
    if (match) choose(match);
    else
      setNotice(
        discovery.list
          ? `未找到可访问的 Foundry「${name}」。请检查名称，或确认${identityLabel(config)}在其订阅上有 Reader 权限；也可以直接填写下方字段。`
          : "Foundry 列表尚未加载，请先刷新。",
      );
  }

  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((index) =>
        Math.max(0, Math.min(shown.length - 1, index + step)),
      );
    } else if (event.key === "Enter") {
      event.preventDefault();
      const exact = items.find((item) => same(item.name, text?.trim() ?? ""));
      if (exact) choose(exact);
      else if (open && shown[active]) choose(shown[active]);
      else commit();
    } else if (event.key === "Escape") {
      setOpen(false);
      setText(null);
      setNotice("");
    }
  }

  let listStatus = discovery.list
    ? `${discovery.source}：${discovery.list.message}`
    : "";
  let listWarn = discovery.list?.state === "error";
  if (discovery.listing)
    listStatus = `正在通过${identityLabel(config)}发现可访问的 Foundry…`;
  else if (discovery.listError) {
    listStatus = discovery.listError;
    listWarn = true;
  }
  let detailStatus = "";
  let detailWarn = false;
  if (discovery.loading)
    detailStatus = `正在读取 ${resourceName(discovery.loading)} 的部署与诊断设置…`;
  else if (discovery.detailError) {
    detailStatus = discovery.detailError;
    detailWarn = true;
  } else if (detail?.state === "error") {
    detailStatus = detail.message;
    detailWarn = true;
  } else if (detail) {
    const usable = detail.workspaces.filter(
      (item) => item.request_logs && item.workspace_id,
    ).length;
    detailStatus = `已读取 ${detail.name}（${detail.location}）：${detail.deployments.length} 个部署 · ${usable} 个日志 Workspace`;
    if (detail.state === "partial") {
      detailStatus += ` · ${detail.message}`;
      detailWarn = true;
    }
  }

  return (
    <div className="foundry-picker">
      <label htmlFor={`${id}-input`}>
        Foundry 资源 <span className="quiet">按日志查询身份发现</span>
      </label>
      <div className="combo">
        <input
          ref={input}
          id={`${id}-input`}
          role="combobox"
          aria-expanded={open && shown.length > 0}
          aria-controls={`${id}-list`}
          aria-autocomplete="list"
          aria-activedescendant={
            open && shown[active] ? `${id}-option-${active}` : undefined
          }
          autoComplete="off"
          value={value}
          placeholder="输入或选择 Foundry 名称"
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            setText(event.target.value);
            setOpen(true);
            setActive(0);
            setNotice("");
          }}
          onKeyDown={keyDown}
          onBlur={() => {
            setOpen(false);
            commit();
          }}
        />
        <button
          type="button"
          className="icon-button"
          aria-label="展开 Foundry 列表"
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            setOpen((value) => !value);
            input.current?.focus();
          }}
        >
          <ChevronDown size={15} />
        </button>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新 Foundry 列表"
          title={`使用${identityLabel(config)}重新读取 Foundry、部署和诊断设置`}
          disabled={discovery.listing}
          onClick={() => void discovery.refresh()}
        >
          <RefreshCw size={15} className={discovery.listing ? "spin" : ""} />
        </button>
        {open && shown.length > 0 && (
          <ul id={`${id}-list`} role="listbox" className="combo-list">
            {shown.map((item, index) => (
              <li
                key={item.resource_id}
                id={`${id}-option-${index}`}
                role="option"
                aria-selected={same(item.resource_id, config.resource_id)}
                className={index === active ? "active" : ""}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActive(index)}
                onClick={() => choose(item)}
              >
                <span>
                  <strong>{item.name}</strong>
                  <small>
                    {item.location} · {item.kind} ·{" "}
                    {item.subscription || item.resource_group}
                  </small>
                </span>
                {same(item.resource_id, config.resource_id) && (
                  <CheckCircle2 size={14} />
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="foundry-status" aria-live="polite">
        {listStatus && (
          <p className={`hint ${listWarn ? "warn-text" : ""}`}>{listStatus}</p>
        )}
        {detailStatus && (
          <p className={`hint ${detailWarn ? "warn-text" : ""}`}>
            {detailStatus}
          </p>
        )}
      </div>
      {notice && (
        <div className="credential-note warn" role="alert">
          {notice}
        </div>
      )}
    </div>
  );
}

function deploymentLabel(item: Deployment): string {
  let label = item.name;
  if (item.model && item.model !== item.name) label += ` → ${item.model}`;
  if (item.version) label += ` (${item.version})`;
  if (item.sku) label += ` · ${item.sku}`;
  if (item.state && item.state !== "Succeeded") label += ` · ${item.state}`;
  return label;
}

export function DeploymentField({
  detail,
  value,
  onChange,
}: {
  detail: FoundryDetail | null;
  value: string;
  onChange: (value: string) => void;
}) {
  const deployments = detail?.deployments ?? [];
  if (!deployments.length)
    return (
      <>
        <label>
          Deployment
          <input
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder="部署名称"
          />
        </label>
        {detail?.deployments_error && (
          <p className="hint warn-text">
            无法列出部署：{detail.deployments_error}
          </p>
        )}
      </>
    );
  const known = deployments.some((item) => item.name === value);
  return (
    <>
      <label>
        Deployment <span className="quiet">{deployments.length} 个部署</span>
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          {!known && (
            <option value={value}>
              {value ? `${value}（不在该资源的部署列表中）` : "选择部署"}
            </option>
          )}
          {deployments.map((item) => (
            <option key={item.name} value={item.name}>
              {deploymentLabel(item)}
            </option>
          ))}
        </select>
      </label>
      {detail?.deployments_limited && (
        <p className="hint">仅显示前 200 个部署。</p>
      )}
    </>
  );
}

function TargetRow({
  target,
  current,
  onUse,
}: {
  target: LogTarget;
  current: boolean;
  onUse: () => void;
}) {
  return (
    <li className={`log-target ${current ? "current" : ""}`}>
      <div className="log-target-main">
        <strong>{target.name}</strong>
        <small>
          {[target.location, `诊断设置 ${target.settings.join("、")}`]
            .filter(Boolean)
            .join(" · ")}
        </small>
        <span className="target-tags">
          <span className={target.request_logs ? "tag ok" : "tag miss"}>
            请求日志 {target.request_logs ? "✓" : "✗"}
          </span>
          <span className={target.usage_logs ? "tag ok" : "tag miss"}>
            用量日志 {target.usage_logs ? "✓" : "✗"}
          </span>
          {target.dedicated && <span className="tag miss">资源专用表</span>}
        </span>
        {target.error && (
          <small className="warn-text">
            {target.error} 可手动填写 Workspace ID。
          </small>
        )}
      </div>
      {current ? (
        <span className="target-current">
          <CheckCircle2 size={13} />
          当前
        </span>
      ) : (
        <button
          type="button"
          className="button secondary small"
          disabled={!target.workspace_id}
          aria-label={`使用 Workspace ${target.name}`}
          onClick={onUse}
        >
          使用
        </button>
      )}
    </li>
  );
}

export function LogTargets({
  detail,
  workspaceId,
  onUse,
}: {
  detail: FoundryDetail | null;
  workspaceId: string;
  onUse: (workspaceId: string) => void;
}) {
  if (!detail || detail.state === "error") return null;
  if (detail.diagnostics_error)
    return (
      <div className="credential-note warn" role="alert">
        无法读取 {detail.name} 的诊断设置：{detail.diagnostics_error}
        请手动填写 Workspace ID。
      </div>
    );
  const targets = detail.workspaces;
  const sending = targets.filter((item) => item.request_logs);
  const others = detail.other_destinations.map(
    (item) => `${destinationLabels[item.kind]} ${item.name}`,
  );
  const outside =
    !!workspaceId &&
    sending.length > 0 &&
    sending.every((item) => item.workspace_id) &&
    !sending.some((item) => item.workspace_id === workspaceId);
  return (
    <div className="log-targets" role="group" aria-label="诊断设置目标">
      <div className="field-label">
        日志去向 <span className="quiet">{detail.name} 的诊断设置</span>
      </div>
      {targets.length > 0 && (
        <ul>
          {targets.map((item) => (
            <TargetRow
              key={item.resource_id}
              target={item}
              current={!!item.workspace_id && item.workspace_id === workspaceId}
              onUse={() => onUse(item.workspace_id)}
            />
          ))}
        </ul>
      )}
      {!sending.length && (
        <div className="credential-note warn" role="alert">
          {detail.name} 的诊断设置没有把请求日志（RequestResponse）发到任何 Log
          Analytics Workspace
          {others.length ? `，目前只发往：${others.join("、")}` : ""}。 请在该
          Foundry 的“诊断设置”中添加 Workspace 目标，否则查不到日志。
        </div>
      )}
      {outside && (
        <div className="credential-note warn" role="alert">
          当前 Workspace ID 不是 {detail.name}
          的诊断设置目标，这次调用的日志不会出现在其中。
        </div>
      )}
      {sending.some((item) => item.dedicated) && (
        <p className="hint warn-text">
          “资源专用表”模式写入独立表，本实验查询的是 AzureDiagnostics。
        </p>
      )}
      {sending.length > 0 && others.length > 0 && (
        <p className="hint">另外发往：{others.join("、")}</p>
      )}
    </div>
  );
}
