import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from azure.monitor.query import LogsQueryStatus, LogsTable
from azure.monitor.query.aio import LogsQueryClient
from pydantic import JsonValue

from .models import (
    REQUEST_ID,
    Candidate,
    Candidates,
    Connection,
    Event,
    LogRecord,
    LogResult,
    LookupResource,
    LookupResources,
    RawLogRow,
    RawLogs,
    Run,
    now,
)

AZURE_ID_HEADERS = ("apim-request-id", "x-request-id", "x-ms-request-id", "request-id")
CLIENT_ID_HEADER = "x-ms-client-request-id"
REQUEST_HEADERS = (*AZURE_ID_HEADERS, CLIENT_ID_HEADER)
# Verified 2026-11-01: RequestResponse and AzureOpenAIRequestUsage rows carry the
# response apim-request-id in CorrelationId; x-request-id and the client ID are not logged.
BUILTIN_MAPPING = ("CorrelationId", "apim-request-id")
META_HEADERS = (
    "x-ms-region",
    "x-ms-served-model",
    "x-ms-is-spilled-over",
    "x-ms-rai-invoked",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
)
DOTNET_UNIX_EPOCH_TICKS = 621_355_968_000_000_000
RAW_ROW_LIMIT = 20
LOOKUP_DAYS = 30
CANDIDATE_LIMIT = 50
RESOURCE_LIMIT = 50
PROPERTIES = 'parse_json(tostring(column_ifexists("properties_s", "{}")))'
PROVIDER_FILTER = '| where ResourceProvider =~ "MICROSOFT.COGNITIVESERVICES"\n'
LOOKUP_MISS = (
    "在所选范围和时间窗口内没有找到该 ID。常见原因：它不是 apim-request-id"
    "（经 LiteLLM 时请用响应头 llm_provider-apim-request-id）；超出时间窗口或保留期；"
    "日志尚未入库（通常 1–3 分钟）；该 Foundry 未配置诊断设置，或日志发往其他 Workspace。"
)
RAW_STRING_LIMIT = 8000
# Integers beyond this lose precision in browser JSON.parse (e.g. properties.requestTime).
JS_SAFE_INTEGER = 2**53 - 1
DEFAULT_ID_FIELDS = (
    "CorrelationId",
    "requestId_s",
    "RequestId",
    "requestId",
    "properties.requestId",
    "properties.request_id",
    "properties.apimRequestId",
    "properties.correlationId",
)
SAFE_FIELDS = {
    "timegenerated",
    "category",
    "operationname",
    "durationms",
    "resultsignature",
    "resourceid",
    "model",
    "modelname",
    "modelversion",
    "modeldeploymentname",
    "deploymentname",
    "region",
    "statuscode",
    "responsecode",
    "prompttokens",
    "completiontokens",
    "totaltokens",
    "inputtokens",
    "outputtokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "requestid",
    "request_id",
    "requestid_s",
    "correlationid",
    "apimrequestid",
    "apim-request-id",
    "x-request-id",
    "x-ms-request-id",
    "streamtype",
    "generatedtokens",
    "cachedtokens",
    "timetofirsttokenms",
    "timetolasttokenms",
    "requesttime",
    "requestlength",
    "responselength",
    "apiname",
    "usagedetails",
    "usage",
    "responseid",
}
SAFE_CONTAINERS = {
    "properties",
    "request",
    "response",
    "usage",
    "usagedetails",
    "metrics",
    "prompt_tokens_details",
    "completion_tokens_details",
}


@asynccontextmanager
async def credential(
    mode: Literal["managed_identity", "azure_cli"], client_id: str
) -> AsyncIterator[AsyncTokenCredential]:
    if mode == "azure_cli":
        async with AzureCliCredential(process_timeout=10) as cli:
            yield cli
    else:
        async with ManagedIdentityCredential(client_id=client_id or None) as identity:
            yield identity


def error_label(exc: Exception) -> str:
    # SDK error strings can contain credential diagnostics and HTTP bodies.
    code = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {code})" if isinstance(code, int) else "")


def safe_fields(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        lower = key.lower()
        if isinstance(item, dict) and lower in SAFE_CONTAINERS:
            nested = safe_fields(item)
            if nested:
                result[key] = nested
        elif lower in SAFE_FIELDS and isinstance(item, (str, int, float, bool)):
            if isinstance(item, str):
                item = item[:500]
            result[key] = item
        elif lower in SAFE_FIELDS and isinstance(item, list) and number_list(item):
            # Usage logs report token counts as arrays, e.g. promptTokens: [9].
            result[key] = item
    return result


def number_list(value: list[JsonValue]) -> bool:
    return len(value) <= 16 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in value
    )


def number(value: JsonValue) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value not in (None, "") else None


def raw_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, int):
        return value if abs(value) <= JS_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): raw_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [raw_value(item) for item in value]
    text = str(value)
    return text if len(text) <= RAW_STRING_LIMIT else text[:RAW_STRING_LIMIT] + "…(truncated)"


def raw_columns(values: dict[str, object]) -> dict[str, JsonValue]:
    columns: dict[str, JsonValue] = {}
    for key, value in values.items():
        if key.startswith("__id") or value is None or value == "":
            continue
        if key == "properties_s" and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        columns[key] = raw_value(value)
    return columns


def aware(value: JsonValue | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def request_time(value: JsonValue, reference: datetime) -> datetime | None:
    # Observed encodings: .NET ticks since 0001-01-01 and 100 ns units since 1970-01-01.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    ticks = value - DOTNET_UNIX_EPOCH_TICKS if value > DOTNET_UNIX_EPOCH_TICKS else value
    try:
        result = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)
    except OverflowError:
        return None
    return result if abs(result - reference) <= timedelta(minutes=10) else None


def id_expression(path: str) -> str:
    pieces = path.split(".")
    if pieces[0] in ("properties", "properties_s"):
        expression = 'parse_json(tostring(column_ifexists("properties_s", "{}")))'
    else:
        expression = f'column_ifexists({json.dumps(pieces[0])}, "")'
    for piece in pieces[1:]:
        expression += f"[{json.dumps(piece)}]"
    return f"tostring({expression})"


def query_window(started_at: str, discovery: bool) -> tuple[datetime, datetime]:
    if discovery:
        return datetime.now(UTC) - timedelta(days=1), datetime.now(UTC)
    start = datetime.fromisoformat(started_at)
    return start - timedelta(minutes=5), start + timedelta(minutes=15)


def id_paths(connection: Connection) -> list[str]:
    return [connection.log_id_field] if connection.log_id_field else list(DEFAULT_ID_FIELDS)


def kql_time(start: datetime, end: datetime) -> str:
    return (
        f"| where TimeGenerated between (datetime({start.isoformat()}) "
        f".. datetime({end.isoformat()}))\n"
    )


def log_query(
    resource: str | None,
    start: datetime,
    end: datetime,
    paths: list[str],
    ids: list[str] | None,
    raw: bool = False,
) -> str:
    # Without a resource the query spans every Foundry that sends logs to the workspace.
    aliases = ", ".join(f"__id{i} = {id_expression(path)}" for i, path in enumerate(paths))
    query = (
        "AzureDiagnostics\n"
        + (f"| where _ResourceId =~ {json.dumps(resource)}\n" if resource else PROVIDER_FILTER)
        + kql_time(start, end)
        + "| extend Ingested = ingestion_time()\n"
        f"| extend {aliases}\n"
    )
    if ids is not None:
        values = ", ".join(json.dumps(value) for value in ids)
        query += (
            "| where " + " or ".join(f"__id{i} in ({values})" for i in range(len(paths))) + "\n"
        )
    if raw:
        return query + f"| order by TimeGenerated desc\n| take {RAW_ROW_LIMIT + 1}"
    query += (
        "| order by TimeGenerated desc\n"
        "| take 101\n"
        "| project TimeGenerated, Ingested, Category, OperationName, "
        'DurationMs = column_ifexists("DurationMs", real(null)), '
        'ResultSignature = column_ifexists("ResultSignature", ""), '
        f"Properties = {PROPERTIES}, _ResourceId, "
        + ", ".join(f"__id{i}" for i in range(len(paths)))
    )
    return query


def build_query(
    connection: Connection,
    ids: list[str],
    discovery: bool = False,
    started_at: str | None = None,
    raw: bool = False,
) -> str:
    start, end = query_window(started_at or now(), discovery)
    return log_query(
        connection.resource_id, start, end, id_paths(connection), None if discovery else ids, raw
    )


def resources_query() -> str:
    return (
        "AzureDiagnostics\n" + PROVIDER_FILTER + f"| where TimeGenerated > ago({LOOKUP_DAYS}d)\n"
        "| summarize Rows = count(), "
        'Inference = countif(Category == "RequestResponse" '
        f"and isnotempty(tostring({PROPERTIES}.modelDeploymentName))), "
        "LastSeen = max(TimeGenerated) by _ResourceId, Resource\n"
        "| order by Rows desc\n"
        f"| take {RESOURCE_LIMIT}"
    )


def candidates_query(
    scope: str,
    start: datetime,
    end: datetime,
    deployment: str = "",
    object_id: str = "",
    outcome: Literal["all", "success", "failure"] = "all",
    inference_only: bool = True,
) -> str:
    query = (
        "AzureDiagnostics\n"
        + kql_time(start, end)
        + PROVIDER_FILTER
        + '| where Category == "RequestResponse"\n'
        + (f"| where _ResourceId =~ {json.dumps(scope)}\n" if scope else "")
        + f"| extend p = {PROPERTIES}\n"
        "| extend Deployment = tostring(p.modelDeploymentName), "
        "ObjectId = tostring(p.objectId), "
        'Result = tostring(column_ifexists("ResultSignature", ""))\n'
    )
    if inference_only:
        query += "| where isnotempty(Deployment)\n"
    if deployment:
        query += f"| where Deployment =~ {json.dumps(deployment)}\n"
    if object_id:
        query += f"| where ObjectId =~ {json.dumps(object_id)}\n"
    if outcome == "success":
        query += '| where Result startswith "2"\n'
    elif outcome == "failure":
        query += '| where not(Result startswith "2")\n'
    return query + (
        "| order by TimeGenerated desc\n"
        f"| take {CANDIDATE_LIMIT + 1}\n"
        '| project TimeGenerated, RequestId = tostring(column_ifexists("CorrelationId", "")), '
        "Resource, _ResourceId, OperationName, Result, "
        'DurationMs = column_ifexists("DurationMs", real(null)), Deployment, '
        "Model = tostring(p.modelName), ModelVersion = tostring(p.modelVersion), "
        "StreamType = tostring(p.streamType), PromptTokens = toreal(p.promptTokens), "
        "CompletionTokens = toreal(p.completionTokens), ObjectId, "
        'CallerIp = tostring(column_ifexists("CallerIPAddress", ""))'
    )


def parse_records(
    tables: list[LogsTable], paths: list[str], ids: dict[str, str], discovery: bool = False
) -> tuple[list[LogRecord], set[str]]:
    records: list[LogRecord] = []
    discovered: set[str] = set()

    def discover_ids(node: dict, prefix: str) -> None:
        for key, item in node.items():
            path = f"{prefix}.{key}"
            if isinstance(item, dict):
                discover_ids(item, path)
            elif re.search(r"(?:request|correlation)[_]?id$", key, re.IGNORECASE):
                try:
                    Connection.id_path(path)
                except ValueError:
                    continue
                discovered.add(path)

    for table in tables:
        for row in table.rows:
            values = dict(zip(table.columns, row, strict=True))
            properties = values.get("Properties")
            if isinstance(properties, str):
                properties = json.loads(properties)
            fields = safe_fields(properties) if isinstance(properties, dict) else {}
            if discovery:
                discover_ids(fields, "properties")
            for key in ("DurationMs", "ResultSignature", "OperationName"):
                item = values.get(key)
                if isinstance(item, (str, int, float, bool)) and item != "":
                    fields[key] = item
            timestamp = iso(values.get("TimeGenerated")) or ""
            reference = aware(timestamp)
            raw_time = properties.get("requestTime") if isinstance(properties, dict) else None
            service_start = request_time(raw_time, reference) if reference else None
            if service_start is not None:
                fields["requestTime_utc"] = service_start.isoformat()
            matching: list[str] = []
            for i, path in enumerate(paths):
                value = values.get(f"__id{i}")
                if isinstance(value, str) and value:
                    discovered.add(path)
                    fields[path] = value[:500]
                    matching.extend(
                        f"{path} = {header}"
                        for header, header_value in ids.items()
                        if value == header_value
                    )
            resource = values.get("_ResourceId")
            records.append(
                LogRecord(
                    timestamp=timestamp,
                    ingested_at=iso(values.get("Ingested")),
                    category=str(values.get("Category", "")),
                    resource=resource[:500] if isinstance(resource, str) else "",
                    fields=fields,
                    matching_fields=matching,
                )
            )
    return records, discovered


def summarize(
    records: list[LogRecord],
    query: str,
    partial: bool,
    discovery: bool = False,
    verified_field: str = "",
    discovered: set[str] | None = None,
) -> LogResult:
    limited = len(records) > 100
    records = records[:100]

    def verified(pair: str) -> bool:
        path, _, header = pair.partition(" = ")
        return (path, header) == BUILTIN_MAPPING or (
            bool(verified_field) and path == verified_field
        )

    pairs = sorted({pair for record in records for pair in record.matching_fields})
    exact = [pair for pair in pairs if verified(pair)]
    if partial:
        state = "partial"
        message = "Azure 只返回了部分结果，关联不完整，请重新查询。"
    elif discovery:
        state = "candidate" if records else "pending"
        message = "仅为字段结构样本，不是已关联的请求。"
    elif exact:
        state = "matched"
        message = f"精确匹配：{'、'.join(exact)}" + (
            "（内置规则，已在真实 AzureDiagnostics 中验证）。"
            if any(pair.partition(" = ")[::2] == BUILTIN_MAPPING for pair in exact)
            else "（你已确认的字段映射）。"
        )
    elif pairs:
        state = "candidate"
        message = f"发现 ID 相等：{'、'.join(pairs)}，但该字段映射尚未确认，暂作候选。"
    else:
        state = "pending"
        message = "暂未查到匹配记录。日志入库通常延迟 1–3 分钟，查不到不代表请求失败。"
    if limited:
        message += " 结果最多显示 100 行。"
    return LogResult(
        state=state,
        message=message,
        query=query,
        checked_at=now(),
        records=records,
        id_fields=sorted(discovered or ()),
        limited=limited,
    )


def raw_result(
    status: Literal["success", "partial", "failure"],
    tables: list[LogsTable],
    paths: list[str],
    ids: dict[str, str],
    query: str,
) -> RawLogs:
    if status == "failure":
        return RawLogs(
            state="error",
            message="Azure Logs 查询失败，没有返回原始行。",
            query=query,
            checked_at=now(),
        )
    rows: list[RawLogRow] = []
    for table in tables:
        for row in table.rows:
            values = dict(zip(table.columns, row, strict=True))
            matching = [
                f"{path} = {header}"
                for i, path in enumerate(paths)
                for header, header_value in ids.items()
                if values.get(f"__id{i}") == header_value
            ]
            rows.append(
                RawLogRow(
                    category=str(values.get("Category") or ""),
                    matching_fields=matching,
                    column_count=sum(not key.startswith("__id") for key in values),
                    columns=raw_columns(values),
                )
            )
    limited = len(rows) > RAW_ROW_LIMIT
    rows = rows[:RAW_ROW_LIMIT]
    if status == "partial":
        state, message = "partial", "Azure 只返回了部分原始行，请稍后刷新。"
    elif rows:
        state, message = "ok", f"读取到 {len(rows)} 行原始记录（已省略空列）。"
    else:
        state, message = "empty", "暂无原始行；日志入库通常延迟 1–3 分钟。"
    if limited:
        message += f" 最多显示 {RAW_ROW_LIMIT} 行。"
    return RawLogs(
        state=state,
        message=message,
        query=query,
        checked_at=now(),
        rows=rows,
        limited=limited,
    )


def mask_ip(value: object) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9A-Fa-f.:*]{1,64}", text):
        return ""
    if "*" in text:
        return text
    if v4 := re.fullmatch(r"(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}", text):
        return f"{v4.group(1)}.**"
    return ":".join(text.split(":")[:3]) + ":**" if ":" in text else ""


def optional_number(value: object) -> float | None:
    result = number(value) if isinstance(value, (str, int, float)) else None
    return result if result is not None and math.isfinite(result) else None


def model_version(value: object) -> str:
    # parse_json turns date-like versions such as "2026-03-17" into datetimes.
    return re.sub(r"T00:00:00(?:\.0+)?Z$", "", short_text(value, r"[\w.:-]{1,64}"))


def short_text(value: object, pattern: str = r"[\w .:/()-]{1,200}") -> str:
    text = str(value or "")
    return text if re.fullmatch(pattern, text) else ""


class AzureLogs:
    async def execute(
        self, connection: Connection, query: str, start: datetime, end: datetime
    ) -> tuple[Literal["success", "partial", "failure"], list[LogsTable]]:
        async with credential(
            connection.logs_auth_mode, connection.managed_identity_client_id
        ) as identity:
            async with LogsQueryClient(identity, retry_total=0) as client:
                response = await client.query_workspace(
                    connection.workspace_id,
                    query,
                    timespan=(start, end),
                    server_timeout=30,
                )
        if response.status == LogsQueryStatus.FAILURE:
            return "failure", []
        if response.status == LogsQueryStatus.PARTIAL:
            return "partial", list(response.partial_data or [])
        return "success", list(response.tables or [])

    async def raw(self, connection: Connection, ids: dict[str, str], started_at: str) -> RawLogs:
        query = build_query(connection, list(ids.values()), started_at=started_at, raw=True)
        start, end = query_window(started_at, False)
        status, tables = await self.execute(connection, query, start, end)
        return raw_result(status, tables, id_paths(connection), ids, query)

    async def query(
        self,
        connection: Connection,
        ids: dict[str, str],
        started_at: str,
        discovery: bool = False,
    ) -> LogResult:
        if not connection.workspace_id or not connection.resource_id:
            return LogResult(
                state="unavailable",
                message="需要配置 Workspace ID 和 Foundry Resource ID 才能查询现有日志。",
            )
        if not ids and not discovery:
            return LogResult(
                state="unavailable",
                message="未收到 Azure 响应请求 ID，无法精确关联。",
            )
        query = build_query(connection, list(ids.values()), discovery, started_at)
        start, end = query_window(started_at, discovery)
        status, tables = await self.execute(connection, query, start, end)
        if status == "failure":
            return LogResult(
                state="error",
                message="Azure Logs 查询失败，没有完整结果。",
                query=query,
                checked_at=now(),
            )
        records, discovered = parse_records(tables, id_paths(connection), ids, discovery)
        return summarize(
            records,
            query,
            status == "partial",
            discovery,
            connection.log_id_field if connection.mapping_verified else "",
            discovered,
        )

    async def resources(self, connection: Connection) -> LookupResources:
        query = resources_query()
        end = datetime.now(UTC)
        status, tables = await self.execute(
            connection, query, end - timedelta(days=LOOKUP_DAYS), end
        )
        if status == "failure":
            return LookupResources(
                state="error", message="Azure Logs 查询失败。", query=query, checked_at=now()
            )
        resources: list[LookupResource] = []
        for table in tables:
            for row in table.rows:
                values = dict(zip(table.columns, row, strict=True))
                resource_id = str(values.get("_ResourceId") or "")
                try:
                    resource_id = Connection.resource_path(resource_id)
                except ValueError:
                    continue
                if not resource_id:
                    continue
                resources.append(
                    LookupResource(
                        resource_id=resource_id,
                        name=short_text(values.get("Resource"), r"[\w.()-]{1,200}")
                        or resource_id.rsplit("/", 1)[-1],
                        rows=int(optional_number(values.get("Rows")) or 0),
                        inference=int(optional_number(values.get("Inference")) or 0),
                        last_seen=iso(values.get("LastSeen")),
                    )
                )
        if status == "partial":
            state, message = "partial", "Azure 只返回了部分结果。"
        elif resources:
            state = "ok"
            message = (
                f"最近 {LOOKUP_DAYS} 天有 {len(resources)} 个 Foundry 资源向该 Workspace 写入日志。"
            )
        else:
            state = "empty"
            message = f"最近 {LOOKUP_DAYS} 天该 Workspace 中没有 Cognitive Services 诊断日志。"
        return LookupResources(
            state=state, message=message, query=query, checked_at=now(), resources=resources
        )

    async def candidates(
        self,
        connection: Connection,
        scope: str,
        start: datetime,
        end: datetime,
        deployment: str = "",
        object_id: str = "",
        outcome: Literal["all", "success", "failure"] = "all",
        inference_only: bool = True,
    ) -> Candidates:
        query = candidates_query(scope, start, end, deployment, object_id, outcome, inference_only)
        status, tables = await self.execute(connection, query, start, end)
        if status == "failure":
            return Candidates(
                state="error", message="Azure Logs 查询失败。", query=query, checked_at=now()
            )
        rows: list[Candidate] = []
        for table in tables:
            for row in table.rows:
                values = dict(zip(table.columns, row, strict=True))
                request_id = str(values.get("RequestId") or "")
                rows.append(
                    Candidate(
                        time=iso(values.get("TimeGenerated")) or "",
                        request_id=request_id if re.fullmatch(REQUEST_ID, request_id) else "",
                        resource=short_text(values.get("Resource"), r"[\w.()-]{1,200}"),
                        resource_id=str(values.get("_ResourceId") or "")[:500],
                        operation=short_text(values.get("OperationName")),
                        result=short_text(values.get("Result"), r"[\w.-]{1,40}"),
                        duration_ms=optional_number(values.get("DurationMs")),
                        deployment=short_text(values.get("Deployment"), r"[\w.:-]{1,200}"),
                        model=short_text(values.get("Model"), r"[\w.:-]{1,200}"),
                        model_version=model_version(values.get("ModelVersion")),
                        stream_type=short_text(values.get("StreamType"), r"[\w -]{1,40}"),
                        prompt_tokens=optional_number(values.get("PromptTokens")),
                        completion_tokens=optional_number(values.get("CompletionTokens")),
                        object_id=short_text(values.get("ObjectId"), r"[0-9A-Fa-f-]{36}"),
                        caller_ip=mask_ip(values.get("CallerIp")),
                    )
                )
        limited = len(rows) > CANDIDATE_LIMIT
        rows = rows[:CANDIDATE_LIMIT]
        if status == "partial":
            state, message = "partial", "Azure 只返回了部分结果，候选列表可能不完整。"
        elif rows:
            state, message = "ok", f"找到 {len(rows)} 条候选调用（按时间倒序）。"
        else:
            state = "empty"
            message = "该时间范围和条件下没有调用记录；日志入库通常延迟 1–3 分钟。"
        if limited:
            message += f" 仅显示最近 {CANDIDATE_LIMIT} 条，请缩小时间范围或增加条件。"
        return Candidates(
            state=state, message=message, query=query, checked_at=now(), rows=rows, limited=limited
        )

    async def trace(
        self, connection: Connection, request_id: str, scope: str, start: datetime, end: datetime
    ) -> LogResult:
        paths, ids = [BUILTIN_MAPPING[0]], {BUILTIN_MAPPING[1]: request_id}
        query = log_query(scope or None, start, end, paths, [request_id])
        status, tables = await self.execute(connection, query, start, end)
        if status == "failure":
            return LogResult(
                state="error",
                message="Azure Logs 查询失败，没有完整结果。",
                query=query,
                checked_at=now(),
            )
        records, discovered = parse_records(tables, paths, ids)
        result = summarize(records, query, status == "partial", discovered=discovered)
        if result.state == "pending":
            result.message = LOOKUP_MISS
        return result

    async def trace_raw(
        self, connection: Connection, request_id: str, scope: str, start: datetime, end: datetime
    ) -> RawLogs:
        paths, ids = [BUILTIN_MAPPING[0]], {BUILTIN_MAPPING[1]: request_id}
        query = log_query(scope or None, start, end, paths, [request_id], raw=True)
        status, tables = await self.execute(connection, query, start, end)
        return raw_result(status, tables, paths, ids, query)


def provider_error_code(body: dict[str, JsonValue]) -> str:
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return (
        code if isinstance(code, str) and re.fullmatch(r"[\w.-]{1,100}", code) else "provider_error"
    )


def request_ids(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: headers[name]
        for name in REQUEST_HEADERS
        if name in headers and re.fullmatch(r"[\w.:-]{1,200}", headers[name])
    }


def response_meta(headers: dict[str, str], body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    meta: dict[str, JsonValue] = {
        name: headers[name][:200]
        for name in META_HEADERS
        if name in headers and re.fullmatch(r"[\w .:/-]{1,200}", headers[name])
    }
    for key in ("id", "model", "system_fingerprint", "status"):
        value = body.get(key)
        if isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,200}", value):
            meta[f"response.{key}"] = value
    return meta


def azure_events(run: Run) -> None:
    run.events = [event for event in run.events if event.source != "azure"]
    run.events.extend(log_events(run.logs.records, run.created_at))


def log_events(records: list[LogRecord], created_at: str | None = None) -> list[Event]:
    events: list[Event] = []
    created = aware(created_at)
    anchor: datetime | None = None
    for record in records:
        timestamp = aware(record.timestamp)
        if timestamp is None:
            record.fields["timeline_note"] = "服务时间戳无效或缺少时区，未绘制。"
            continue
        ingested = aware(record.ingested_at)
        if ingested is not None:
            events.append(
                Event(
                    title=f"Azure ingestion: {record.category or 'record'}",
                    lane="logs",
                    source="azure",
                    kind="point",
                    started_at=ingested.isoformat(),
                    status="observed",
                    details={
                        "category": record.category,
                        "TimeGenerated": record.timestamp,
                        "ingestion_time": record.ingested_at,
                        "ingestion_delay_ms": round((ingested - timestamp).total_seconds() * 1000),
                        **(
                            {
                                "since_run_start_ms": round(
                                    (ingested - created).total_seconds() * 1000
                                )
                            }
                            if created
                            else {}
                        ),
                        "note": "Log Analytics ingestion_time()，表示记录可被查询的时间。",
                    },
                )
            )
        if record.category != "RequestResponse":
            continue
        start = aware(record.fields.get("requestTime_utc"))
        duration = number(record.fields.get("DurationMs", ""))
        evidence = {"fields": record.fields, "matching_fields": record.matching_fields}
        if start is not None and duration is not None and duration >= 0:
            anchor = start if anchor is None else min(anchor, start)
            events.append(
                Event(
                    title="Azure RequestResponse: service processing",
                    lane="call",
                    source="azure",
                    started_at=start.isoformat(),
                    ended_at=(start + timedelta(milliseconds=duration)).isoformat(),
                    duration_ms=duration,
                    status="observed",
                    details={
                        **evidence,
                        "note": "服务时钟：properties.requestTime + DurationMs；"
                        "与本机时钟可能存在偏差。",
                    },
                )
            )
        else:
            events.append(
                Event(
                    title="Azure: RequestResponse",
                    lane="call",
                    source="azure",
                    kind="point",
                    started_at=timestamp.isoformat(),
                    status="observed",
                    details={**evidence, "note": "仅有 TimeGenerated 时间点，不是实测执行区间。"},
                )
            )
    for record in records:
        if record.category != "AzureOpenAIRequestUsage" or aware(record.timestamp) is None:
            continue
        first = number(record.fields.get("timeToFirstTokenMs", ""))
        last = number(record.fields.get("timeToLastTokenMs", ""))
        if anchor is None or first is None:
            record.fields["timeline_note"] = (
                "用量日志的 TimeGenerated 是汇总时间；缺少 RequestResponse 起点，未绘制阶段。"
            )
            continue
        phases = [("Azure usage log: time to first token", first)]
        note = ""
        if last is not None and last - first >= 1:
            phases.append(("Azure usage log: time to last token", last))
        elif last is not None and abs(last - first) < 1:
            # Non-streaming Chat Completions report TTFT == TTLT; the value marks full output.
            phases = [("Azure usage log: full output (TTFT = TTLT)", first)]
            note = (
                "TTFT 与 TTLT 相同（非流式 Chat Completions 的表现），"
                "表示完整输出耗时，不是真实首 token；"
            )
        for title, duration in phases:
            events.append(
                Event(
                    title=title,
                    lane="call",
                    source="azure",
                    started_at=anchor.isoformat(),
                    ended_at=(anchor + timedelta(milliseconds=duration)).isoformat(),
                    duration_ms=duration,
                    status="observed",
                    details={
                        "fields": record.fields,
                        "matching_fields": record.matching_fields,
                        "note": note + "时长来自 AzureOpenAIRequestUsage；起点按 RequestResponse."
                        "requestTime 对齐（日志未给出该阶段的绝对起点，属近似对齐）。",
                    },
                )
            )
    return events
