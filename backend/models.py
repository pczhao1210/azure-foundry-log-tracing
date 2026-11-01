import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

REQUEST_ID = r"[\w.:-]{1,200}"
# Lookups may not reach beyond the observed 30-day workspace retention.
LOOKUP_WINDOW = timedelta(days=30)
CANDIDATE_WINDOW = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=10)


def now() -> str:
    return datetime.now(UTC).isoformat()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    foundry_endpoint: str = ""
    foundry_deployment: str = ""
    foundry_api_key: str = ""
    llm_auth_mode: Literal["managed_identity", "api_key"] = "api_key"
    azure_client_id: str = ""
    logs_auth_mode: Literal["managed_identity", "azure_cli"] = "managed_identity"
    log_analytics_workspace_id: str = ""
    foundry_resource_id: str = ""
    log_id_field: str = ""
    log_id_mapping_verified: bool = False
    database_path: str = ".data/experiments.sqlite3"
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]"]


class Connection(BaseModel):
    endpoint: str
    deployment: str = Field(min_length=1, max_length=200)
    auth_mode: Literal["managed_identity", "api_key"] = "api_key"
    managed_identity_client_id: str = ""
    logs_auth_mode: Literal["managed_identity", "azure_cli"] = "managed_identity"
    workspace_id: str = ""
    resource_id: str = ""
    log_id_field: str = ""
    mapping_verified: bool = False

    @field_validator("endpoint")
    @classmethod
    def azure_endpoint(cls, value: str) -> str:
        url = urlsplit(value.strip())
        suffixes = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.hostname.endswith(suffixes)
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/") not in ("", "/openai/v1")
        ):
            raise ValueError("Use a public Azure HTTPS resource endpoint, optionally /openai/v1.")
        return f"https://{url.hostname}/openai/v1"

    @field_validator("workspace_id", "managed_identity_client_id")
    @classmethod
    def optional_uuid(cls, value: str) -> str:
        value = value.strip()
        return str(UUID(value)) if value else ""

    @field_validator("resource_id")
    @classmethod
    def resource_path(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not re.fullmatch(
            r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[\w.()-]+"
            r"/providers/Microsoft\.CognitiveServices/accounts/[\w-]+",
            value,
            flags=re.IGNORECASE,
        ):
            raise ValueError("Expected a Cognitive Services account resource ID.")
        return value

    @field_validator("log_id_field")
    @classmethod
    def id_path(cls, value: str) -> str:
        value = value.strip()
        if value and (
            not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){0,4}", value)
            or not re.search(r"(?:request|correlation)[_]?id(?:_s|_g)?$", value, re.IGNORECASE)
        ):
            raise ValueError("Use a request/correlation ID path, e.g. properties.requestId.")
        return value

    @model_validator(mode="after")
    def require_mapping(self) -> "Connection":
        if self.mapping_verified and not self.log_id_field:
            raise ValueError("A verified mapping requires an explicit log ID field.")
        return self


class NewRun(BaseModel):
    connection: Connection
    prompt: str = Field(min_length=1, max_length=32000)
    max_completion_tokens: int = Field(default=256, ge=1, le=4096)


class QueryMapping(BaseModel):
    log_id_field: str = ""
    mapping_verified: bool = False


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    title: str
    lane: Literal["call", "logs"]
    source: Literal["client", "log_query", "azure"] = "client"
    kind: Literal["interval", "point"] = "interval"
    started_at: str = Field(default_factory=now)
    ended_at: str | None = None
    duration_ms: float | None = None
    status: Literal["running", "success", "error", "cancelled", "observed"] = "running"
    details: dict[str, JsonValue] = Field(default_factory=dict)


class LogRecord(BaseModel):
    timestamp: str = ""
    ingested_at: str | None = None
    category: str = ""
    resource: str = ""
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    # Evidence pairs such as "CorrelationId = apim-request-id".
    matching_fields: list[str] = Field(default_factory=list)


class LogResult(BaseModel):
    state: Literal[
        "idle",
        "querying",
        "pending",
        "matched",
        "candidate",
        "unavailable",
        "error",
        "partial",
        "cancelled",
    ] = "idle"
    message: str = ""
    query: str = ""
    checked_at: str | None = None
    attempts: int = 0
    polling: bool = False
    records: list[LogRecord] = Field(default_factory=list)
    id_fields: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    limited: bool = False


class RawLogRow(BaseModel):
    category: str = ""
    matching_fields: list[str] = Field(default_factory=list)
    column_count: int = 0
    columns: dict[str, JsonValue] = Field(default_factory=dict)


class RawLogs(BaseModel):
    # Fetched on demand for display only; never persisted or exported.
    state: Literal["ok", "empty", "partial", "error", "unavailable"] = "empty"
    message: str = ""
    query: str = ""
    checked_at: str | None = None
    rows: list[RawLogRow] = Field(default_factory=list)
    limited: bool = False


class Run(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=now)
    finished_at: str | None = None
    duration_ms: float | None = None
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"] = "queued"
    connection: Connection
    max_completion_tokens: int
    http_status: int | None = None
    request_ids: dict[str, str] = Field(default_factory=dict)
    response_meta: dict[str, JsonValue] = Field(default_factory=dict)
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    answer: str | None = None
    error: str | None = None
    events: list[Event] = Field(default_factory=list)
    logs: LogResult = Field(default_factory=LogResult)


def check_window(start: datetime, end: datetime, limit: timedelta) -> None:
    if start >= end:
        raise ValueError("start must be earlier than end.")
    if end > datetime.now(UTC) + CLOCK_SKEW:
        raise ValueError("end cannot be in the future.")
    if end - start > limit:
        raise ValueError(f"The time window may not exceed {limit.days} days.")


class LookupBase(BaseModel):
    connection: Connection


class LookupScope(LookupBase):
    # Empty means every Foundry resource that sends logs to the workspace.
    scope: str = ""

    @field_validator("scope")
    @classmethod
    def scope_path(cls, value: str) -> str:
        return Connection.resource_path(value)


class CandidateQuery(LookupScope):
    start: AwareDatetime
    end: AwareDatetime
    deployment: str = ""
    object_id: str = ""
    outcome: Literal["all", "success", "failure"] = "all"
    inference_only: bool = True

    @field_validator("deployment")
    @classmethod
    def deployment_name(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[\w.-]{1,64}", value):
            raise ValueError("Use a deployment name of letters, digits, '.', '_' or '-'.")
        return value

    @field_validator("object_id")
    @classmethod
    def caller_object_id(cls, value: str) -> str:
        value = value.strip()
        return str(UUID(value)) if value else ""

    @model_validator(mode="after")
    def window(self) -> "CandidateQuery":
        check_window(self.start, self.end, CANDIDATE_WINDOW)
        return self


class TraceQuery(LookupScope):
    request_id: str
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @field_validator("request_id")
    @classmethod
    def azure_request_id(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(REQUEST_ID, value):
            raise ValueError("Use an apim-request-id value (letters, digits, '.', ':', '_', '-').")
        return value

    @model_validator(mode="after")
    def window(self) -> "TraceQuery":
        self.end = self.end or datetime.now(UTC)
        self.start = self.start or self.end - LOOKUP_WINDOW
        check_window(self.start, self.end, LOOKUP_WINDOW)
        return self


class LookupResource(BaseModel):
    resource_id: str
    name: str = ""
    rows: int = 0
    inference: int = 0
    last_seen: str | None = None


class LookupResources(BaseModel):
    state: Literal["ok", "empty", "partial", "error", "unavailable"] = "empty"
    message: str = ""
    query: str = ""
    checked_at: str | None = None
    resources: list[LookupResource] = Field(default_factory=list)


class Candidate(BaseModel):
    time: str
    request_id: str = ""
    resource: str = ""
    resource_id: str = ""
    operation: str = ""
    result: str = ""
    duration_ms: float | None = None
    deployment: str = ""
    model: str = ""
    model_version: str = ""
    stream_type: str = ""
    prompt_tokens: float | None = None
    completion_tokens: float | None = None
    object_id: str = ""
    # Azure already masks the last octet; the backend re-masks defensively.
    caller_ip: str = ""
    local_run_id: str | None = None


class Candidates(BaseModel):
    state: Literal["ok", "empty", "partial", "error", "unavailable"] = "empty"
    message: str = ""
    query: str = ""
    checked_at: str | None = None
    rows: list[Candidate] = Field(default_factory=list)
    limited: bool = False


class Trace(BaseModel):
    # Built on demand and never persisted.
    request_id: str
    scope: str = ""
    start: str
    end: str
    logs: LogResult
    events: list[Event] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    local_run_id: str | None = None
    raw_scope: str = ""
    raw_start: str | None = None
    raw_end: str | None = None
