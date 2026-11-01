import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from time import perf_counter
from typing import TypeVar
from uuid import uuid4

import httpx
from azure.core.exceptions import AzureError
from pydantic import JsonValue, TypeAdapter, ValidationError

from .azure import (
    AZURE_ID_HEADERS,
    BUILTIN_MAPPING,
    CLIENT_ID_HEADER,
    AzureLogs,
    aware,
    azure_events,
    build_query,
    candidates_query,
    credential,
    error_label,
    log_events,
    log_query,
    provider_error_code,
    request_ids,
    resources_query,
    response_meta,
)
from .discovery import AzureDiscovery, describe
from .models import (
    CandidateQuery,
    Candidates,
    Connection,
    DiscoveryBase,
    Event,
    Foundries,
    FoundryDetail,
    FoundryQuery,
    LogResult,
    LookupBase,
    LookupResources,
    NewRun,
    Preferences,
    RawLogs,
    Run,
    Settings,
    Trace,
    TraceQuery,
    now,
)
from .store import Store

logger = logging.getLogger(__name__)
JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
SCOPE = "https://cognitiveservices.azure.com/.default"
EXPECTED_CATEGORIES = ("RequestResponse", "AzureOpenAIRequestUsage")
NO_WORKSPACE = "需要配置 Log Analytics Workspace ID 才能回查历史调用。"
RAW_MARGIN = timedelta(minutes=5)
RESPONSE_LIMIT = 4 * 1024 * 1024
API_PATHS = {"chat_completions": "/chat/completions", "responses": "/responses"}
NO_TEXT = "The endpoint did not return text or a refusal message."
T = TypeVar("T")
D = TypeVar("D", Foundries, FoundryDetail)


def request_body(run: Run, prompt: str) -> dict[str, JsonValue]:
    if run.api == "responses":
        return {
            "model": run.connection.deployment,
            "input": prompt,
            "max_output_tokens": run.max_completion_tokens,
            "stream": run.stream,
            # Azure otherwise retains the response for 30 days for later retrieval.
            "store": False,
        }
    body: dict[str, JsonValue] = {
        "model": run.connection.deployment,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": run.max_completion_tokens,
        "stream": run.stream,
    }
    if run.stream:
        # Without include_usage a streamed chat completion reports no token counts.
        body["stream_options"] = {"include_usage": True}
    return body


def chat_text(parsed: dict[str, JsonValue]) -> str:
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("The endpoint did not return a Chat Completions choices array.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("The endpoint did not return a chat message.")
    content = message.get("content") or message.get("refusal")
    if not isinstance(content, str):
        raise ValueError(NO_TEXT)
    return content


def incomplete(parsed: dict[str, JsonValue]) -> str:
    details = parsed.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if isinstance(reason, str) and re.fullmatch(r"[\w.-]{1,100}", reason):
        return f"The response is incomplete ({reason}) and contains no text."
    return NO_TEXT


def responses_text(parsed: dict[str, JsonValue]) -> str:
    output = parsed.get("output")
    if not isinstance(output, list):
        raise ValueError("The endpoint did not return a Responses output array.")
    parts: list[str] = []
    for item in output:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            text = part.get("text") if part.get("type") == "output_text" else part.get("refusal")
            if isinstance(text, str):
                parts.append(text)
    if not parts:
        raise ValueError(incomplete(parsed))
    return "".join(parts)


def stream_error(prefix: str, error: JsonValue) -> ValueError:
    # Responses "error" events carry code at top level; Chat chunks nest it under "error".
    nested = error.get("error") if isinstance(error, dict) else None
    return ValueError(f"{prefix}: {provider_error_code({'error': nested or error})}")


async def sse_payloads(response: httpx.Response) -> AsyncIterator[tuple[str, int]]:
    """Yield (data payload, decoded bytes so far) for each server-sent event."""
    data: list[str] = []
    received = 0
    async for line in response.aiter_lines():
        received += len(line.encode()) + 1
        if received > RESPONSE_LIMIT:
            raise ValueError("Response exceeds the 4 MiB safety limit.")
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            yield "\n".join(data), received
            data.clear()
    if data:
        yield "\n".join(data), received


def meta_fields(value: JsonValue, meta: dict[str, JsonValue]) -> None:
    if isinstance(value, dict):
        for key in ("id", "model", "system_fingerprint", "status"):
            if isinstance(value.get(key), str) and value[key]:
                meta[key] = value[key]


def chat_chunk(
    chunk: dict[str, JsonValue], meta: dict[str, JsonValue]
) -> tuple[str, dict[str, JsonValue] | None, dict[str, JsonValue] | None]:
    """Return (text delta, usage, final response) for one Chat Completions stream chunk."""
    if "error" in chunk:
        raise stream_error("Stream error", chunk["error"])
    meta_fields(chunk, meta)
    usage = chunk.get("usage")
    text = ""
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta")
        if isinstance(delta, dict):
            value = delta.get("content") or delta.get("refusal")
            text = value if isinstance(value, str) else ""
    # Chat streams end with a literal [DONE] line instead of a final object.
    return text, usage if isinstance(usage, dict) else None, None


def responses_chunk(
    chunk: dict[str, JsonValue], meta: dict[str, JsonValue]
) -> tuple[str, dict[str, JsonValue] | None, dict[str, JsonValue] | None]:
    """Return (text delta, usage, final response) for one Responses API stream event."""
    kind = chunk.get("type")
    if kind in ("response.output_text.delta", "response.refusal.delta"):
        delta = chunk.get("delta")
        return (delta if isinstance(delta, str) else ""), None, None
    if kind == "error":
        raise stream_error("Stream error", chunk)
    response = chunk.get("response")
    if kind == "response.failed":
        raise stream_error(
            "Stream failed", response.get("error") if isinstance(response, dict) else None
        )
    meta_fields(response, meta)
    if kind in ("response.completed", "response.incomplete") and isinstance(response, dict):
        usage = response.get("usage")
        return "", usage if isinstance(usage, dict) else None, response
    return "", None, None


class Engine:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        logs: AzureLogs | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        # Observed ingestion delays are about 1-2 minutes and differ per category.
        poll_delays: tuple[float, ...] = (0, 20, 30, 45, 60, 90, 120),
        discovery: AzureDiscovery | None = None,
    ):
        self.settings = settings
        self.store = store
        self.logs = logs or AzureLogs()
        self.discovery = discovery or AzureDiscovery()
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(120, connect=15), follow_redirects=False
            )
        )
        self.poll_delays = poll_delays
        self.runs: dict[str, Run] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.log_tasks: dict[str, asyncio.Task[None]] = {}
        self.raw_locks: dict[str, asyncio.Lock] = {}
        self.lookup_slots = asyncio.Semaphore(2)
        self.discovery_slots = asyncio.Semaphore(4)

    def get(self, run_id: str) -> Run | None:
        run = self.runs.get(run_id)
        if run is None:
            run = self.store.get(run_id)
            if run is not None:
                self.runs[run_id] = run
        return run

    def save(self, run: Run) -> None:
        self.store.save(run)

    def begin(self, run: Run, title: str, lane: str = "call") -> tuple[Event, float]:
        event = Event(
            title=title,
            lane="logs" if lane == "logs" else "call",
            source="log_query" if lane == "logs" else "client",
        )
        run.events.append(event)
        self.save(run)
        return event, perf_counter()

    def end(self, run: Run, timed: tuple[Event, float], failed: bool = False) -> None:
        event, started = timed
        event.duration_ms = round((perf_counter() - started) * 1000, 2)
        event.ended_at = now()
        event.status = "error" if failed else "success"
        self.save(run)

    def watch(self, task: asyncio.Task[None], run: Run, logs: bool = False) -> None:
        def finished(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                label = type(error).__name__
                logger.error("Background operation failed: run=%s type=%s", run.id, label)
                if logs:
                    run.logs.state = "error"
                    run.logs.polling = False
                    run.logs.message = (
                        f"Unexpected query failure: {label}. No model retry was made."
                    )
                else:
                    run.status = "failed"
                    run.error = f"Unexpected request failure: {label}. Outcome may be unknown."
                    run.finished_at = now()
                for event in run.events:
                    if event.status == "running":
                        event.status = "error"
                        event.ended_at = now()
                self.save(run)

        task.add_done_callback(finished)

    def create(self, request: NewRun) -> Run:
        run = Run(
            connection=request.connection,
            max_completion_tokens=request.max_completion_tokens,
            api=request.api,
            stream=request.stream,
        )
        # Only a submitted call becomes the next default; editing the form does not.
        self.store.save_preferences(
            Preferences(
                connection=request.connection,
                prompt=request.prompt,
                max_completion_tokens=request.max_completion_tokens,
                api=request.api,
                stream=request.stream,
            )
        )
        self.runs[run.id] = run
        self.save(run)
        task = asyncio.create_task(self.execute(run, request.prompt), name=f"model-{run.id}")
        self.tasks[run.id] = task
        self.watch(task, run)
        return run

    async def execute(self, run: Run, prompt: str) -> None:
        started = perf_counter()
        run.status = "running"
        auth = self.begin(run, "Authentication")
        current = auth
        try:
            async with asyncio.timeout(180):
                client_request_id = str(uuid4())
                headers = {
                    "Content-Type": "application/json",
                    CLIENT_ID_HEADER: client_request_id,
                }
                run.request_ids = {CLIENT_ID_HEADER: client_request_id}
                if run.connection.auth_mode == "api_key":
                    if not self.settings.foundry_api_key:
                        raise ValueError("API key is not configured on the server.")
                    headers["api-key"] = self.settings.foundry_api_key
                else:
                    async with credential(
                        "managed_identity", run.connection.managed_identity_client_id
                    ) as identity:
                        token = await identity.get_token(SCOPE)
                    headers["Authorization"] = f"Bearer {token.token}"
                self.end(run, auth)
                current = self.begin(run, "HTTP request / waiting for headers")
                sent = current[1]
                body: bytes | None = None
                async with self.client_factory() as client:
                    async with client.stream(
                        "POST",
                        run.connection.endpoint + API_PATHS[run.api],
                        headers=headers,
                        json=request_body(run, prompt),
                    ) as response:
                        run.http_status = response.status_code
                        response_headers = dict(response.headers)
                        run.request_ids.update(request_ids(response_headers))
                        run.response_meta = response_meta(response_headers, {})
                        self.end(run, current, failed=response.is_error)
                        run.events.append(
                            Event(
                                title="Response headers received",
                                lane="call",
                                kind="point",
                                status="observed",
                                details={
                                    "status": response.status_code,
                                    "request_ids": run.request_ids,
                                },
                            )
                        )
                        # Error responses carry a JSON body even when streaming was requested.
                        if run.stream and not response.is_error:
                            current = self.begin(run, "Receiving stream")
                            meta = await self.read_stream(run, response, current, sent)
                            run.response_meta = response_meta(response_headers, meta)
                            self.end(run, current)
                        else:
                            current = self.begin(run, "Reading non-streaming response body")
                            buffer = bytearray()
                            async for chunk in response.aiter_bytes():
                                buffer.extend(chunk)
                                if len(buffer) > RESPONSE_LIMIT:
                                    raise ValueError("Response exceeds the 4 MiB safety limit.")
                            body = bytes(buffer)
                            self.end(run, current)
                if body is not None:
                    current = self.begin(run, "Parsing response")
                    parsed = JSON_OBJECT.validate_json(body)
                    run.response_meta = response_meta(response_headers, parsed)
                    if run.http_status is not None and not 200 <= run.http_status < 300:
                        raise ValueError(f"HTTP {run.http_status}: {provider_error_code(parsed)}")
                    usage = parsed.get("usage")
                    if isinstance(usage, dict):
                        run.usage = self.numeric_usage(usage)
                    run.answer = (
                        responses_text(parsed) if run.api == "responses" else chat_text(parsed)
                    )
                    self.end(run, current)
                run.status = "succeeded"
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.error = "Cancelled locally. Azure may already have processed or billed the request."
            current[0].status = "cancelled"
            current[0].ended_at = now()
            current[0].duration_ms = round((perf_counter() - current[1]) * 1000, 2)
        except (AzureError, httpx.HTTPError, TimeoutError, ValueError, ValidationError) as exc:
            run.status = "failed"
            # Only our own ValueErrors contain safe display messages; SDK/parser errors do not.
            run.error = str(exc) if type(exc) is ValueError else error_label(exc)
            logger.warning("Model request failed: run=%s type=%s", run.id, type(exc).__name__)
            self.end(run, current, failed=True)
        finally:
            run.finished_at = now()
            run.duration_ms = round((perf_counter() - started) * 1000, 2)
            self.save(run)
        if run.status != "cancelled":
            self.start_query(run, automatic=True)

    @staticmethod
    def numeric_usage(usage: dict[str, JsonValue]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
            elif isinstance(value, dict):
                result[key] = Engine.numeric_usage(value)
        return result

    async def read_stream(
        self, run: Run, response: httpx.Response, timed: tuple[Event, float], sent: float
    ) -> dict[str, JsonValue]:
        """Consume an SSE body, growing run.answer in memory so polling shows it live."""
        handle = responses_chunk if run.api == "responses" else chat_chunk
        event, started = timed
        meta: dict[str, JsonValue] = {}
        final: dict[str, JsonValue] | None = None
        completed = False
        events = deltas = received = 0
        first: float | None = None
        last: float | None = None
        run.answer = ""
        try:
            async for payload, size in sse_payloads(response):
                events, received = events + 1, size
                if payload == "[DONE]":
                    completed = True
                    continue
                text, usage, done = handle(JSON_OBJECT.validate_json(payload), meta)
                if usage is not None:
                    run.usage = self.numeric_usage(usage)
                if done is not None:
                    final, completed = done, True
                if not text:
                    continue
                last = perf_counter()
                deltas += 1
                run.answer += text
                if first is None:
                    first = last
                    run.events.append(
                        Event(
                            title="First content delta",
                            lane="call",
                            kind="point",
                            status="observed",
                            details={
                                "ttft_ms": round((first - sent) * 1000, 2),
                                "after_headers_ms": round((first - started) * 1000, 2),
                            },
                        )
                    )
                    self.save(run)
        finally:
            event.details = {
                "sse_events": events,
                "content_deltas": deltas,
                "bytes": received,
                "completed": completed,
            }
            if first is not None and last is not None:
                event.details["first_delta_after_ms"] = round((first - started) * 1000, 2)
                event.details["last_delta_after_ms"] = round((last - started) * 1000, 2)
        if not completed:
            raise ValueError("The stream ended before completion.")
        if not run.answer:
            raise ValueError(incomplete(final) if final is not None else NO_TEXT)
        return meta

    def start_query(self, run: Run, automatic: bool = False) -> bool:
        previous = self.log_tasks.get(run.id)
        if previous is not None and not previous.done():
            return False
        azure_issued = any(name in run.request_ids for name in AZURE_ID_HEADERS)
        if not azure_issued or not run.connection.workspace_id or not run.connection.resource_id:
            run.logs = LogResult(
                state="unavailable",
                message="未发送查询：需要 Azure 返回的请求 ID、Workspace ID 和 Resource ID。",
            )
            self.save(run)
            return True
        run.logs.polling = True
        self.save(run)
        task = asyncio.create_task(self.poll(run, automatic), name=f"logs-{run.id}")
        self.log_tasks[run.id] = task
        self.watch(task, run, logs=True)
        return True

    async def poll(self, run: Run, automatic: bool) -> None:
        active: tuple[Event, float] | None = None
        try:
            for delay in self.poll_delays if automatic else (0,):
                if delay:
                    active = self.begin(run, "Waiting before next log query", "logs")
                    await asyncio.sleep(delay)
                    self.end(run, active)
                active = self.begin(run, "Read-only Log Analytics query", "logs")
                run.logs.state = "querying"
                run.logs.query = build_query(
                    run.connection, list(run.request_ids.values()), started_at=run.created_at
                )
                run.logs.attempts += 1
                self.save(run)
                attempts = run.logs.attempts
                async with asyncio.timeout(45):
                    result = await self.logs.query(
                        run.connection, dict(run.request_ids), run.created_at
                    )
                result.attempts = attempts
                result.polling = True
                mismatch = run.connection.account_mismatch()
                if result.state == "pending" and mismatch:
                    result.message += (
                        f" 注意：Endpoint 账户为 {mismatch[0]}，但查询限定在资源 {mismatch[1]}；"
                        "若两者不是同一资源，日志不会被查到。请核对 Foundry Resource ID，"
                        "并确认该资源的诊断设置发送到此 Workspace。"
                    )
                if result.state in ("matched", "candidate"):
                    seen = {record.category for record in result.records if record.matching_fields}
                    result.missing = [
                        category
                        for category in EXPECTED_CATEGORIES
                        if category not in seen
                        and (category == "RequestResponse" or run.status == "succeeded")
                    ]
                    if result.missing:
                        result.message += (
                            f" 仍在等待：{'、'.join(result.missing)}（各类日志入库延迟不同）。"
                        )
                run.logs = result
                self.end(run, active, failed=result.state in ("error", "partial"))
                active[0].details = {
                    "state": result.state,
                    "record_count": len(result.records),
                    "query": result.query,
                }
                azure_events(run)
                self.save(run)
                if result.state != "pending" and not result.missing:
                    break
            else:
                if run.logs.state == "pending" or run.logs.missing:
                    run.logs.message += " 本轮查询已结束，可稍后点击“重新查日志”。"
        except asyncio.CancelledError:
            if run.logs.state in ("matched", "candidate"):
                run.logs.message += " 已停止后续轮询。"
            else:
                run.logs.state = "cancelled"
                run.logs.message = "已停止日志轮询，已获得的证据会保留。"
            if active is not None:
                active[0].status = "cancelled"
                active[0].ended_at = now()
                active[0].duration_ms = round((perf_counter() - active[1]) * 1000, 2)
        except (AzureError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            logger.warning("Log query failed: run=%s type=%s", run.id, type(exc).__name__)
            run.logs.state = "error"
            run.logs.message = f"日志查询失败：{error_label(exc)}。请检查身份、RBAC、表和网络。"
            if active is not None:
                self.end(run, active, failed=True)
        finally:
            run.logs.polling = False
            self.save(run)

    async def raw_logs(self, run: Run) -> RawLogs:
        azure_issued = any(name in run.request_ids for name in AZURE_ID_HEADERS)
        if not azure_issued or not run.connection.workspace_id or not run.connection.resource_id:
            return RawLogs(
                state="unavailable",
                message="未查询原始日志：需要 Azure 返回的请求 ID、Workspace ID 和 Resource ID。",
            )
        try:
            async with asyncio.timeout(45):
                return await self.logs.raw(run.connection, dict(run.request_ids), run.created_at)
        except (AzureError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            logger.warning("Raw log query failed: run=%s type=%s", run.id, type(exc).__name__)
            return RawLogs(
                state="error",
                message=f"原始日志查询失败：{error_label(exc)}。请检查身份、RBAC、表和网络。",
                query=build_query(
                    run.connection,
                    list(run.request_ids.values()),
                    started_at=run.created_at,
                    raw=True,
                ),
                checked_at=now(),
            )

    async def lookup(
        self,
        kind: str,
        work: Callable[[], Awaitable[T]],
        failed: Callable[[str], T],
    ) -> T:
        try:
            async with asyncio.timeout(45):
                return await work()
        except (AzureError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            logger.warning("Lookup failed: kind=%s type=%s", kind, type(exc).__name__)
            return failed(
                f"回查失败：{error_label(exc)}。"
                "请检查身份、RBAC（需 Workspace 或表级读取权限）和网络。"
            )

    async def discover(
        self,
        kind: str,
        work: Callable[[], Awaitable[T]],
        failed: Callable[[str], T],
    ) -> T:
        async with self.discovery_slots:
            try:
                async with asyncio.timeout(45):
                    return await work()
            except (AzureError, httpx.HTTPError, TimeoutError, ValueError) as exc:
                logger.warning("Discovery failed: kind=%s type=%s", kind, type(exc).__name__)
                return failed(f"资源发现失败：{describe(exc)}")

    async def foundries(self, body: DiscoveryBase) -> Foundries:
        return await self.cached(
            "foundries",
            body,
            "",
            Foundries,
            lambda: self.discover(
                "foundries",
                lambda: self.discovery.foundries(body),
                lambda message: Foundries(state="error", message=message, checked_at=now()),
            ),
        )

    async def foundry(self, body: FoundryQuery) -> FoundryDetail:
        return await self.cached(
            "foundry",
            body,
            body.resource_id,
            FoundryDetail,
            lambda: self.discover(
                "foundry",
                lambda: self.discovery.foundry(body),
                lambda message: FoundryDetail(
                    state="error", message=message, checked_at=now(), resource_id=body.resource_id
                ),
            ),
        )

    def cache_hit(self, kind: str, identity: str, resource_id: str, model: type[D]) -> D | None:
        data = self.store.cached_discovery(kind, identity, resource_id)
        if data is None:
            return None
        try:
            return model.model_validate_json(data).model_copy(update={"cached": True})
        except ValidationError:
            # A row from an older schema is a cache miss, not an error.
            logger.warning("Ignoring invalid discovery cache: kind=%s.", kind)
            return None

    async def cached(
        self,
        kind: str,
        body: DiscoveryBase,
        resource_id: str,
        model: type[D],
        fetch: Callable[[], Awaitable[D]],
    ) -> D:
        identity = body.identity_key()
        if not body.refresh:
            hit = self.cache_hit(kind, identity, resource_id, model)
            if hit is not None:
                return hit
        fresh = await fetch()
        if fresh.state != "error":
            self.store.cache_discovery(kind, identity, resource_id, fresh)
            return fresh
        # A failed refresh never overwrites the last good result; show both instead.
        previous = self.cache_hit(kind, identity, resource_id, model)
        if previous is None:
            return fresh
        return previous.model_copy(update={"refresh_error": fresh.message})

    async def lookup_resources(self, body: LookupBase) -> LookupResources:
        if not body.connection.workspace_id:
            return LookupResources(state="unavailable", message=NO_WORKSPACE)
        return await self.lookup(
            "resources",
            lambda: self.logs.resources(body.connection),
            lambda message: LookupResources(
                state="error", message=message, query=resources_query(), checked_at=now()
            ),
        )

    async def lookup_candidates(self, body: CandidateQuery) -> Candidates:
        if not body.connection.workspace_id:
            return Candidates(state="unavailable", message=NO_WORKSPACE)
        filters = (
            body.scope,
            body.start,
            body.end,
            body.deployment,
            body.object_id,
            body.outcome,
            body.inference_only,
        )
        result = await self.lookup(
            "candidates",
            lambda: self.logs.candidates(body.connection, *filters),
            lambda message: Candidates(
                state="error",
                message=message,
                query=candidates_query(*filters),
                checked_at=now(),
            ),
        )
        local = self.store.find_requests([row.request_id for row in result.rows])
        for row in result.rows:
            row.local_run_id = local.get(row.request_id.lower())
        return result

    async def lookup_trace(self, body: TraceQuery) -> Trace:
        assert body.start is not None and body.end is not None
        start, end = body.start, body.end
        if not body.connection.workspace_id:
            logs = LogResult(state="unavailable", message=NO_WORKSPACE)
        else:
            logs = await self.lookup(
                "trace",
                lambda: self.logs.trace(body.connection, body.request_id, body.scope, start, end),
                lambda message: LogResult(
                    state="error",
                    message=message,
                    query=log_query(
                        body.scope or None, start, end, [BUILTIN_MAPPING[0]], [body.request_id]
                    ),
                    checked_at=now(),
                ),
            )
        resources = sorted({record.resource for record in logs.records if record.resource})
        times = [stamp for record in logs.records if (stamp := aware(record.timestamp))]
        trace = Trace(
            request_id=body.request_id,
            scope=body.scope,
            start=start.isoformat(),
            end=end.isoformat(),
            logs=logs,
            events=log_events(logs.records),
            resources=resources,
            local_run_id=self.store.find_requests([body.request_id]).get(body.request_id.lower()),
            raw_scope=resources[0] if len(resources) == 1 else body.scope,
        )
        if times:
            # A narrow window keeps the raw-row query cheap once the records are located.
            trace.raw_start = max(start, min(times) - RAW_MARGIN).isoformat()
            trace.raw_end = min(end, max(times) + RAW_MARGIN).isoformat()
        return trace

    async def lookup_raw(self, body: TraceQuery) -> RawLogs:
        assert body.start is not None and body.end is not None
        start, end = body.start, body.end
        if not body.connection.workspace_id:
            return RawLogs(state="unavailable", message=NO_WORKSPACE)
        return await self.lookup(
            "raw",
            lambda: self.logs.trace_raw(body.connection, body.request_id, body.scope, start, end),
            lambda message: RawLogs(
                state="error",
                message=message,
                query=log_query(
                    body.scope or None,
                    start,
                    end,
                    [BUILTIN_MAPPING[0]],
                    [body.request_id],
                    raw=True,
                ),
                checked_at=now(),
            ),
        )

    async def cancel(self, run: Run) -> None:
        was_polling = run.logs.polling
        tasks = [
            task
            for task in (self.tasks.get(run.id), self.log_tasks.get(run.id))
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if run.status == "queued":
            run.status = "cancelled"
            run.finished_at = now()
            run.error = "Cancelled before dispatch."
        if was_polling and run.logs.state not in ("cancelled", "matched", "candidate", "error"):
            run.logs.state = "cancelled"
            run.logs.message = "已停止日志轮询；不会再次调用模型。"
        run.logs.polling = False
        self.save(run)

    async def close(self) -> None:
        for run in list(self.runs.values()):
            await self.cancel(run)

    async def check(self, connection: Connection) -> dict[str, JsonValue]:
        model: dict[str, JsonValue]
        try:
            if connection.auth_mode == "api_key":
                model = {
                    "ok": bool(self.settings.foundry_api_key),
                    "message": (
                        "Key configured; not sent. Deployment access is unverified until execution."
                        if self.settings.foundry_api_key
                        else "Server API key is not configured."
                    ),
                }
            else:
                async with asyncio.timeout(20):
                    async with credential(
                        "managed_identity", connection.managed_identity_client_id
                    ) as identity:
                        await identity.get_token(SCOPE)
                model = {
                    "ok": True,
                    "message": "MI token acquired. Model RBAC is not verified until execution.",
                }
        except (AzureError, TimeoutError, ValueError) as exc:
            logger.warning("Identity check failed: type=%s", type(exc).__name__)
            model = {"ok": False, "message": error_label(exc)}
        try:
            async with asyncio.timeout(45):
                logs = await self.logs.query(connection, {}, now(), discovery=True)
        except (AzureError, TimeoutError, ValueError) as exc:
            logger.warning("Log connection check failed: type=%s", type(exc).__name__)
            logs = LogResult(state="error", message=error_label(exc))
        return {"model": model, "logs": logs.model_dump(mode="json")}
