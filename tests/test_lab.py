import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from azure.core.exceptions import HttpResponseError
from azure.monitor.query import LogsQueryStatus
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import azure
from backend.app import create_app
from backend.azure import (
    AzureLogs,
    azure_events,
    build_query,
    candidates_query,
    log_query,
    request_time,
    resources_query,
    safe_fields,
)
from backend.discovery import AzureDiscovery
from backend.engine import Engine
from backend.models import (
    CandidateQuery,
    Connection,
    DiscoveryBase,
    Event,
    FoundryQuery,
    LogRecord,
    LogResult,
    LookupBase,
    NewRun,
    Run,
    Settings,
    TraceQuery,
)
from backend.store import Store

RESOURCE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/"
    "resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/example"
)


@pytest.fixture
def connection():
    return Connection(
        endpoint="https://example.openai.azure.com",
        deployment="test-deployment",
        workspace_id="00000000-0000-0000-0000-000000000002",
        resource_id=RESOURCE,
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, foundry_api_key="secret-api-key", database_path=str(tmp_path / "runs.db")
    )


class FakeLogs:
    def __init__(self, results=None):
        self.calls = 0
        self.results = results or [LogResult(state="pending")]

    async def query(self, *args, **kwargs):
        result = self.results[min(self.calls, len(self.results) - 1)].model_copy(deep=True)
        self.calls += 1
        return result


def engine_for(settings, handler, logs=None, delays=(0,), discovery=None):
    return Engine(
        settings,
        Store(settings.database_path),
        logs=logs or FakeLogs(),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        poll_delays=delays,
        discovery=discovery,
    )


async def settle(engine, run):
    await engine.tasks[run.id]
    if run.id in engine.log_tasks:
        await engine.log_tasks[run.id]


def success_response():
    return httpx.Response(
        200,
        headers={
            "apim-request-id": "azure-123",
            "x-ms-region": "East US 2",
            "x-ms-served-model": "gpt-test",
            "set-cookie": "secret-cookie",
        },
        json={
            "id": "chatcmpl-1",
            "model": "gpt-test-2026",
            "choices": [{"message": {"content": "private answer"}}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
                "latency_checkpoint": {"service_ttft_ms": 288, "pre_inference_ms": 68},
            },
        },
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.openai.azure.com",
        "https://evil.example",
        "https://example.openai.azure.com.evil.test",
        "https://user:pass@example.openai.azure.com",
        "https://example.openai.azure.com:8443",
        "https://example.openai.azure.com/?secret=1",
        "https://example.openai.azure.com/redirect",
    ],
)
def test_endpoint_guard(connection, endpoint):
    with pytest.raises(ValidationError):
        Connection.model_validate({**connection.model_dump(), "endpoint": endpoint})


def test_mapping_requires_explicit_field(connection):
    with pytest.raises(ValidationError):
        Connection.model_validate({**connection.model_dump(), "mapping_verified": True})
    with pytest.raises(ValidationError):
        Connection.model_validate(
            {
                **connection.model_dump(),
                "log_id_field": "CorrelationId); union * //",
            }
        )


def test_query_is_scoped_and_exact(connection):
    connection.log_id_field = "properties.requestId"
    query = build_query(connection, ['id-"123'], started_at="2026-10-31T15:00:00+00:00")
    assert RESOURCE in query
    assert "TimeGenerated between" in query
    assert "2026-10-31T14:55:00" in query
    assert "2026-10-31T15:15:00" in query
    assert r'in ("id-\"123")' in query
    assert '["requestId"]' in query
    assert "| take 101" in query


def test_metadata_redaction():
    fields = safe_fields(
        {
            "prompt": "secret",
            "api-key": "secret",
            "Authorization": {"model": "secret"},
            "messages": {"requestId": "secret"},
            "requestId": "real-id",
            "model": "gpt",
            "usage": {"prompt_tokens": 2, "content": "secret"},
        }
    )
    assert "secret" not in json.dumps(fields)
    assert fields["requestId"] == "real-id"
    assert fields["usage"] == {"prompt_tokens": 2}


def test_store_excludes_content_and_recovers(settings, connection):
    store = Store(settings.database_path)
    run = Run(connection=connection, max_completion_tokens=10, status="running", answer="private")
    run.events.append(Event(title="in flight", lane="call"))
    run.logs.polling = True
    store.save(run)
    assert b"private" not in store.path.read_bytes()
    store.recover()
    loaded = store.get(run.id)
    assert loaded.status == "failed"
    assert loaded.answer is None
    assert loaded.events[0].status == "error"
    assert loaded.logs.state == "error"


async def test_success_captures_ids_without_persisting_content(connection, settings):
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.headers["api-key"] == "secret-api-key"
        assert json.loads(request.content)["stream"] is False
        return success_response()

    engine = engine_for(settings, handler)
    run = engine.create(NewRun(connection=connection, prompt="private prompt"))
    await settle(engine, run)
    assert len(calls) == 1
    assert run.status == "succeeded"
    client_id = calls[0].headers["x-ms-client-request-id"]
    assert run.request_ids == {"x-ms-client-request-id": client_id, "apim-request-id": "azure-123"}
    assert run.response_meta == {
        "x-ms-region": "East US 2",
        "x-ms-served-model": "gpt-test",
        "response.id": "chatcmpl-1",
        "response.model": "gpt-test-2026",
    }
    assert run.usage["total_tokens"] == 8
    assert run.usage["latency_checkpoint"] == {"service_ttft_ms": 288, "pre_inference_ms": 68}
    assert run.answer == "private answer"
    assert run.duration_ms is not None and run.duration_ms >= 0
    raw = engine.store.path.read_bytes()
    for secret in (b"private prompt", b"private answer", b"secret-api-key", b"secret-cookie"):
        assert secret not in raw
    assert all(event.status != "running" for event in run.events)
    await engine.close()


async def test_provider_error_has_no_retry_or_body_leak(connection, settings):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"apim-request-id": "failed-id"},
            json={
                "error": {"code": "RateLimitExceeded", "message": "secret provider detail"},
            },
        )

    engine = engine_for(settings, handler)
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.status == "failed"
    assert run.http_status == 429 and calls == 1
    assert "RateLimitExceeded" in run.error
    assert "secret provider detail" not in run.model_dump_json()
    assert run.logs.attempts == 1


async def test_missing_key_does_not_dispatch(connection, settings):
    settings.foundry_api_key = ""

    async def handler(request):
        pytest.fail("Network must not be called without a key.")

    engine = engine_for(settings, handler)
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.status == "failed"
    assert run.logs.state == "unavailable"
    assert run.logs.attempts == 0


async def test_mi_uses_bearer_and_never_key(connection, settings, monkeypatch):
    from backend import engine as engine_module

    used = []

    @asynccontextmanager
    async def token_credential(mode, client_id):
        used.append(mode)

        class Identity:
            async def get_token(self, scope):
                assert scope == "https://cognitiveservices.azure.com/.default"
                return SimpleNamespace(token="mi-token")

        yield Identity()

    monkeypatch.setattr(engine_module, "credential", token_credential)
    connection.auth_mode = "managed_identity"

    async def handler(request):
        assert request.headers["authorization"] == "Bearer mi-token"
        assert "api-key" not in request.headers
        return success_response()

    engine = engine_for(settings, handler)
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert used == ["managed_identity"]
    assert run.status == "succeeded"
    assert b"mi-token" not in engine.store.path.read_bytes()


async def test_identity_failure_no_fallback(connection, settings, monkeypatch):
    from backend import engine as engine_module

    @asynccontextmanager
    async def failing_credential(*args):
        raise HttpResponseError(message="credential secret")
        yield

    monkeypatch.setattr(engine_module, "credential", failing_credential)
    connection.auth_mode = "managed_identity"

    async def handler(request):
        pytest.fail("MI failure must not fall back to the configured API key.")

    engine = engine_for(settings, handler)
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.status == "failed"
    assert "credential secret" not in run.model_dump_json()


async def test_cancel_closes_request(connection, settings):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.sleep(100)
        return success_response()

    engine = engine_for(settings, handler)
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await entered.wait()
    await engine.cancel(run)
    assert run.status == "cancelled"
    assert not run.logs.polling
    assert not any(event.status == "running" for event in run.events)


async def test_delayed_logs_and_manual_query_do_not_recall_model(connection, settings):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return success_response()

    logs = FakeLogs([LogResult(state="pending"), LogResult(state="matched")])
    engine = engine_for(settings, handler, logs, (0, 0))
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.logs.state == "matched"
    assert run.logs.attempts == 2
    assert engine.start_query(run)
    await engine.log_tasks[run.id]
    assert calls == 1
    assert run.logs.attempts == 3


async def test_query_failure_keeps_model_success_and_query(connection, settings):
    class FailingLogs:
        async def query(self, *args, **kwargs):
            raise HttpResponseError(message="secret Azure response")

    async def handler(request):
        return success_response()

    engine = engine_for(settings, handler, FailingLogs())
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.status == "succeeded"
    assert run.logs.state == "error"
    assert "AzureDiagnostics" in run.logs.query
    assert "secret Azure response" not in run.model_dump_json()


def mock_azure_response(monkeypatch, status=LogsQueryStatus.SUCCESS, count=1):
    @asynccontextmanager
    async def identity(*args):
        yield object()

    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["retry_total"] == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def query_workspace(self, workspace, query, **kwargs):
            columns = [
                "TimeGenerated",
                "Category",
                "Properties",
                "DurationMs",
                "ResultSignature",
                "__id0",
            ]
            row = [
                datetime(2026, 10, 31, tzinfo=UTC),
                "RequestResponse",
                {"prompt": "secret prompt", "requestId": "azure-123", "usage": {"total_tokens": 8}},
                23.5,
                "200",
                "azure-123",
            ]
            table = SimpleNamespace(columns=columns, rows=[row] * count)
            return SimpleNamespace(status=status, tables=[table], partial_data=[table])

    monkeypatch.setattr(azure, "credential", identity)
    monkeypatch.setattr(azure, "LogsQueryClient", Client)


@pytest.mark.parametrize(
    ("field", "header", "verified", "expected"),
    [
        ("requestId_s", "apim-request-id", False, "candidate"),
        ("requestId_s", "apim-request-id", True, "matched"),
        ("CorrelationId", "apim-request-id", False, "matched"),
        ("CorrelationId", "x-request-id", False, "candidate"),
    ],
)
async def test_exact_evidence_requires_confirmation(
    connection, monkeypatch, field, header, verified, expected
):
    mock_azure_response(monkeypatch)
    connection.log_id_field = field
    connection.mapping_verified = verified
    result = await AzureLogs().query(
        connection,
        {header: "azure-123", "x-ms-client-request-id": "client-1"},
        "2026-10-31T00:00:00+00:00",
    )
    assert result.state == expected
    assert result.records[0].matching_fields == [f"{field} = {header}"]
    assert "secret prompt" not in result.model_dump_json()


async def test_partial_and_limited_not_complete_success(connection, monkeypatch):
    mock_azure_response(monkeypatch, LogsQueryStatus.PARTIAL, 101)
    connection.log_id_field = "CorrelationId"
    connection.mapping_verified = True
    result = await AzureLogs().query(
        connection, {"apim-request-id": "azure-123"}, "2026-10-31T00:00:00+00:00"
    )
    assert result.state == "partial"
    assert result.limited
    assert len(result.records) == 100


def test_api_validation_history_and_no_secret(settings, connection):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        config = client.get("/api/config").json()
        assert config["api_key_configured"] is True
        assert "secret-api-key" not in json.dumps(config)
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400
        assert client.get("/api/runs").json() == []
        assert client.get("/api/runs/not-there").status_code == 404
        response = client.post(
            "/api/runs",
            json={
                "prompt": "private prompt",
                "connection": {"endpoint": "invalid"},
            },
        )
        assert response.status_code == 422
        assert "private prompt" not in response.text
        assert (
            client.post(
                "/api/check",
                json=connection.model_dump(),
                headers={"Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )


def test_mapping_update_requeries_without_model(settings, connection):
    logs = FakeLogs([LogResult(state="matched")])

    async def handler(request):
        pytest.fail("Updating a mapping must not call the model.")

    engine = engine_for(settings, handler, logs)
    run = Run(
        connection=connection,
        max_completion_tokens=10,
        status="succeeded",
        request_ids={"apim-request-id": "azure-123"},
    )
    engine.runs[run.id] = run
    engine.save(run)
    with TestClient(create_app(settings, engine), base_url="http://localhost") as client:
        response = client.post(
            f"/api/runs/{run.id}/query",
            json={
                "log_id_field": "CorrelationId",
                "mapping_verified": True,
            },
        )
        assert response.status_code == 202
        loaded = client.get(f"/api/runs/{run.id}").json()
        assert loaded["connection"]["mapping_verified"]
        assert loaded["connection"]["endpoint"] == connection.endpoint


async def test_body_timeout_retains_response_id_for_logs(connection, settings):
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"choices":'
            raise httpx.ReadTimeout("private diagnostic body")

    async def handler(request):
        return httpx.Response(200, headers={"apim-request-id": "azure-123"}, stream=BrokenBody())

    engine = engine_for(settings, handler)
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.status == "failed"
    assert run.http_status == 200
    assert run.request_ids["apim-request-id"] == "azure-123"
    assert run.logs.attempts == 1
    assert run.usage == {}
    assert "private diagnostic body" not in run.model_dump_json()


async def test_connection_check_does_not_call_model(connection, settings):
    async def handler(request):
        pytest.fail("Connection check must not call the model.")

    logs = FakeLogs()
    engine = engine_for(settings, handler, logs)
    check = await engine.check(connection)
    assert check["model"]["ok"] is True
    assert "not sent" in check["model"]["message"]
    assert logs.calls == 1


async def test_stop_log_polling_preserves_model_success(connection, settings):
    async def handler(request):
        return success_response()

    engine = engine_for(settings, handler, delays=(0, 100))
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await engine.tasks[run.id]
    await asyncio.sleep(0.01)
    await engine.cancel(run)
    assert run.status == "succeeded"
    assert run.logs.state == "cancelled"
    assert not run.logs.polling


def test_invalid_cloud_timestamp_is_not_fabricated(connection):
    run = Run(connection=connection, max_completion_tokens=10)
    run.logs.records = [LogRecord(timestamp=""), LogRecord(timestamp="2026-10-31T12:00:00")]
    azure_events(run)
    assert not run.events
    assert all("timeline_note" in record.fields for record in run.logs.records)


def test_request_time_encodings():
    reference = datetime(2026, 11, 1, 4, 16, 51, tzinfo=UTC)
    unix = request_time(17935066109333602, reference)
    assert unix.isoformat() == "2026-11-01T04:16:50.933360+00:00"
    dotnet = request_time(639289509016723491, datetime(2026, 10, 30, 9, 55, 3, tzinfo=UTC))
    assert dotnet.isoformat() == "2026-10-30T09:55:01.672349+00:00"
    assert request_time(17935066109333602, datetime(2026, 1, 1, tzinfo=UTC)) is None
    assert request_time("17935066109333602", reference) is None


def real_shaped_records():
    request = LogRecord(
        timestamp="2026-11-01T04:16:51.617000+00:00",
        ingested_at="2026-11-01T04:18:09.542983+00:00",
        category="RequestResponse",
        fields={
            "requestTime_utc": "2026-11-01T04:16:50.933360+00:00",
            "DurationMs": 567,
            "CorrelationId": "azure-123",
        },
        matching_fields=["CorrelationId = apim-request-id"],
    )
    usage = LogRecord(
        timestamp="2026-11-01T04:17:39.285423+00:00",
        ingested_at="2026-11-01T04:17:42.138290+00:00",
        category="AzureOpenAIRequestUsage",
        fields=safe_fields(
            {
                "promptTokens": [9],
                "generatedTokens": [4],
                "timeToFirstTokenMs": 542.093,
                "timeToLastTokenMs": 542.093,
                "messages": ["secret"],
            }
        ),
        matching_fields=["CorrelationId = apim-request-id"],
    )
    return request, usage


def test_service_intervals_use_request_time_not_aggregation_time(connection):
    request, usage = real_shaped_records()
    assert usage.fields == {
        "promptTokens": [9],
        "generatedTokens": [4],
        "timeToFirstTokenMs": 542.093,
        "timeToLastTokenMs": 542.093,
    }
    run = Run(connection=connection, max_completion_tokens=10)
    run.logs.records = [usage, request]
    azure_events(run)
    call = {event.title: event for event in run.events if event.lane == "call"}
    service = call["Azure RequestResponse: service processing"]
    assert service.started_at == "2026-11-01T04:16:50.933360+00:00"
    assert service.duration_ms == 567
    ttft = call["Azure usage log: full output (TTFT = TTLT)"]
    assert ttft.started_at == service.started_at and ttft.duration_ms == 542.093
    assert "不是真实首 token" in ttft.details["note"]
    assert "Azure usage log: time to first token" not in call
    assert "Azure usage log: time to last token" not in call
    assert not any(event.started_at.startswith("2026-11-01T04:17:39") for event in run.events)
    ingestion = sorted(
        event.details["ingestion_delay_ms"] for event in run.events if event.lane == "logs"
    )
    assert ingestion == [2853, 77926]


def test_usage_without_request_anchor_is_not_plotted(connection):
    _, usage = real_shaped_records()
    run = Run(connection=connection, max_completion_tokens=10)
    run.logs.records = [usage]
    azure_events(run)
    assert not [event for event in run.events if event.lane == "call"]
    assert "timeline_note" in usage.fields


async def test_polling_waits_for_both_log_categories(connection, settings):
    async def handler(request):
        return success_response()

    request, usage = real_shaped_records()
    logs = FakeLogs(
        [
            LogResult(state="pending"),
            LogResult(state="matched", message="ok.", records=[usage]),
            LogResult(state="matched", message="ok.", records=[usage, request]),
        ]
    )
    engine = engine_for(settings, handler, logs, (0, 0, 0, 0))
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.logs.attempts == 3
    assert run.logs.missing == []
    assert run.logs.state == "matched"


async def test_polling_reports_missing_category_when_schedule_ends(connection, settings):
    async def handler(request):
        return success_response()

    request, _ = real_shaped_records()
    logs = FakeLogs([LogResult(state="matched", message="ok.", records=[request])])
    engine = engine_for(settings, handler, logs, (0, 0))
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.logs.attempts == 2
    assert run.logs.missing == ["AzureOpenAIRequestUsage"]
    assert "AzureOpenAIRequestUsage" in run.logs.message
    assert "重新查日志" in run.logs.message


def test_account_mismatch_compares_endpoint_and_resource(connection):
    assert connection.account_mismatch() is None
    upper = connection.model_copy(update={"resource_id": RESOURCE.replace("example", "EXAMPLE")})
    assert upper.account_mismatch() is None
    assert connection.model_copy(update={"resource_id": ""}).account_mismatch() is None
    other = connection.model_copy(update={"endpoint": "https://other.cognitiveservices.azure.com"})
    assert other.account_mismatch() == ("other", "example")


async def test_pending_logs_warn_about_endpoint_resource_mismatch(connection, settings):
    async def handler(request):
        return success_response()

    other = connection.model_copy(
        update={"endpoint": "https://other-region.cognitiveservices.azure.com/openai/v1"}
    )
    logs = FakeLogs([LogResult(state="pending", message="暂未查到。")])
    engine = engine_for(settings, handler, logs)
    run = engine.create(NewRun(connection=other, prompt="test"))
    await settle(engine, run)
    assert run.logs.state == "pending"
    assert "Endpoint 账户为 other-region" in run.logs.message
    assert "查询限定在资源 example" in run.logs.message

    same = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, same)
    assert "Endpoint 账户" not in same.logs.message


async def test_client_request_id_alone_does_not_trigger_log_query(connection, settings):
    sent = []

    async def handler(request):
        sent.append(request.headers["x-ms-client-request-id"])
        raise httpx.ConnectError("private network detail")

    logs = FakeLogs()
    engine = engine_for(settings, handler, logs)
    run = engine.create(NewRun(connection=connection, prompt="test"))
    await settle(engine, run)
    assert run.status == "failed"
    assert run.request_ids == {"x-ms-client-request-id": sent[0]}
    assert run.logs.state == "unavailable"
    assert logs.calls == 0
    assert "private network detail" not in run.model_dump_json()


def mock_raw_response(monkeypatch, status=LogsQueryStatus.SUCCESS, count=1):
    queries = []

    @asynccontextmanager
    async def identity(*args):
        yield object()

    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["retry_total"] == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def query_workspace(self, workspace, query, **kwargs):
            queries.append(query)
            columns = [
                "TimeGenerated",
                "Category",
                "CorrelationId",
                "DurationMs",
                "CallerIPAddress",
                "ResultSignature",
                "properties_s",
                "Ingested",
                "__id0",
            ]
            properties = (
                '{"requestTime":17935073291553453,"objectId":"principal-1",'
                '"completionTokens":49,"path":""}'
            )
            row = [
                datetime(2026, 10, 31, tzinfo=UTC),
                "RequestResponse",
                "azure-123",
                782.0,
                "",
                None,
                properties,
                datetime(2026, 10, 31, 0, 1, tzinfo=UTC),
                "azure-123",
            ]
            table = SimpleNamespace(columns=columns, rows=[row] * count)
            return SimpleNamespace(status=status, tables=[table], partial_data=[table])

    monkeypatch.setattr(azure, "credential", identity)
    monkeypatch.setattr(azure, "LogsQueryClient", Client)
    return queries


async def test_raw_logs_keep_full_rows_and_precision(connection, monkeypatch):
    queries = mock_raw_response(monkeypatch)
    connection.log_id_field = "CorrelationId"
    result = await AzureLogs().raw(
        connection, {"apim-request-id": "azure-123"}, "2026-10-31T00:00:00+00:00"
    )
    assert result.state == "ok"
    row = result.rows[0]
    assert row.matching_fields == ["CorrelationId = apim-request-id"]
    assert row.column_count == 8
    assert set(row.columns) == {
        "TimeGenerated",
        "Category",
        "CorrelationId",
        "DurationMs",
        "properties_s",
        "Ingested",
    }
    assert row.columns["properties_s"] == {
        "requestTime": "17935073291553453",
        "objectId": "principal-1",
        "completionTokens": 49,
        "path": "",
    }
    assert row.columns["TimeGenerated"] == "2026-10-31T00:00:00+00:00"
    query = queries[0]
    assert f'_ResourceId =~ "{RESOURCE}"' in query
    assert '__id0 in ("azure-123")' in query
    assert query.endswith("| take 21")
    assert "| project" not in query


async def test_raw_logs_partial_and_limited(connection, monkeypatch):
    mock_raw_response(monkeypatch, LogsQueryStatus.PARTIAL, 21)
    result = await AzureLogs().raw(
        connection, {"apim-request-id": "azure-123"}, "2026-10-31T00:00:00+00:00"
    )
    assert result.state == "partial"
    assert result.limited
    assert len(result.rows) == 20


def test_raw_logs_endpoint_is_not_persisted(settings, connection, monkeypatch):
    mock_raw_response(monkeypatch)

    async def handler(request):
        pytest.fail("Reading raw logs must not call the model.")

    engine = engine_for(settings, handler, AzureLogs())
    run = Run(
        connection=connection,
        max_completion_tokens=10,
        status="succeeded",
        request_ids={"apim-request-id": "azure-123"},
    )
    engine.runs[run.id] = run
    engine.save(run)
    with TestClient(create_app(settings, engine), base_url="http://localhost") as client:
        response = client.post(f"/api/runs/{run.id}/raw-logs", json={})
        assert response.status_code == 200
        assert response.json()["rows"][0]["columns"]["properties_s"]["objectId"] == "principal-1"
        assert "principal-1" not in client.get(f"/api/runs/{run.id}").text
        assert (
            client.post(
                f"/api/runs/{run.id}/raw-logs",
                json={},
                headers={"Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )
        assert client.post("/api/runs/not-there/raw-logs", json={}).status_code == 404
    assert "principal-1" not in Store(settings.database_path).get(run.id).model_dump_json()


async def test_raw_logs_need_azure_id_and_hide_errors(connection, settings):
    class FailingLogs(FakeLogs):
        async def raw(self, *args, **kwargs):
            self.calls += 1
            raise HttpResponseError(message="secret Azure response")

    async def handler(request):
        pytest.fail("Reading raw logs must not call the model.")

    logs = FailingLogs()
    engine = engine_for(settings, handler, logs)
    run = Run(
        connection=connection,
        max_completion_tokens=10,
        request_ids={"x-ms-client-request-id": "client-1"},
    )
    assert (await engine.raw_logs(run)).state == "unavailable"
    assert logs.calls == 0
    run.request_ids["apim-request-id"] = "azure-123"
    result = await engine.raw_logs(run)
    assert result.state == "error"
    assert "take 21" in result.query
    assert "secret Azure response" not in result.model_dump_json()


LOWER_RESOURCE = RESOURCE.lower()
UTC_HOUR = datetime(2026, 11, 1, 4, tzinfo=UTC)


def mock_lookup_response(monkeypatch, candidate_rows=1, status=LogsQueryStatus.SUCCESS):
    calls = []

    @asynccontextmanager
    async def identity(*args):
        yield object()

    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["retry_total"] == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def query_workspace(self, workspace, query, **kwargs):
            calls.append((query, kwargs["timespan"]))
            if "summarize" in query:
                columns = ["_ResourceId", "Resource", "Rows", "Inference", "LastSeen"]
                rows = [
                    [LOWER_RESOURCE, "EXAMPLE", 120, 3, UTC_HOUR],
                    ["/not/a/resource", "BAD", 1, 0, UTC_HOUR],
                ]
            elif "RequestId =" in query:
                columns = [
                    "TimeGenerated",
                    "RequestId",
                    "Resource",
                    "_ResourceId",
                    "OperationName",
                    "Result",
                    "DurationMs",
                    "Deployment",
                    "Model",
                    "ModelVersion",
                    "StreamType",
                    "PromptTokens",
                    "CompletionTokens",
                    "ObjectId",
                    "CallerIp",
                ]
                base = [
                    UTC_HOUR,
                    "azure-123",
                    "EXAMPLE",
                    LOWER_RESOURCE,
                    "chatcompletions_create",
                    "200",
                    782.0,
                    "gpt-test",
                    "gpt-test",
                    "2026-03-17T00:00:00.0000000Z",
                    "Non-Streaming",
                    19.0,
                    49.0,
                    "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "10.1.2.3",
                ]
                other = [*base]
                other[1], other[14] = 'bad id"', "203.0.113.**"
                rows = [base, *[other] * (candidate_rows - 1)]
            else:
                columns = [
                    "TimeGenerated",
                    "Ingested",
                    "Category",
                    "OperationName",
                    "DurationMs",
                    "ResultSignature",
                    "Properties",
                    "_ResourceId",
                    "__id0",
                ]
                rows = [
                    [
                        datetime(2026, 11, 1, 4, 28, 50, 65000, tzinfo=UTC),
                        datetime(2026, 11, 1, 4, 30, 9, tzinfo=UTC),
                        "RequestResponse",
                        "chatcompletions_create",
                        782.0,
                        "200",
                        {"requestTime": 17935073291553453, "prompt": "secret prompt"},
                        LOWER_RESOURCE,
                        "azure-123",
                    ],
                    [
                        datetime(2026, 11, 1, 4, 30, 22, tzinfo=UTC),
                        datetime(2026, 11, 1, 4, 30, 25, tzinfo=UTC),
                        "AzureOpenAIRequestUsage",
                        "",
                        None,
                        "",
                        {"timeToFirstTokenMs": 700.5, "timeToLastTokenMs": 700.5},
                        LOWER_RESOURCE,
                        "azure-123",
                    ],
                ]
                rows = rows if '"azure-123"' in query else []
            table = SimpleNamespace(columns=columns, rows=rows)
            return SimpleNamespace(status=status, tables=[table], partial_data=[table])

    monkeypatch.setattr(azure, "credential", identity)
    monkeypatch.setattr(azure, "LogsQueryClient", Client)
    return calls


def test_lookup_queries_are_scoped_and_bounded():
    end = UTC_HOUR + timedelta(hours=1)
    query = candidates_query(
        RESOURCE, UTC_HOUR, end, "gpt-test", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "success"
    )
    assert f'_ResourceId =~ "{RESOURCE}"' in query
    assert 'ResourceProvider =~ "MICROSOFT.COGNITIVESERVICES"' in query
    assert 'Category == "RequestResponse"' in query
    assert "| where isnotempty(Deployment)" in query
    assert 'Deployment =~ "gpt-test"' in query
    assert 'ObjectId =~ "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"' in query
    assert '| where Result startswith "2"' in query
    assert "2026-11-01T04:00:00+00:00" in query and "2026-11-01T05:00:00+00:00" in query
    assert "| take 51" in query
    broad = candidates_query("", UTC_HOUR, end, outcome="failure", inference_only=False)
    assert "_ResourceId =~" not in broad
    assert "isnotempty(Deployment)" not in broad
    assert 'not(Result startswith "2")' in broad
    workspace = log_query(None, UTC_HOUR, end, ["CorrelationId"], ["azure-123"])
    assert "_ResourceId =~" not in workspace
    assert 'ResourceProvider =~ "MICROSOFT.COGNITIVESERVICES"' in workspace
    assert '__id0 in ("azure-123")' in workspace
    assert "_ResourceId, __id0" in workspace
    scoped = log_query(LOWER_RESOURCE, UTC_HOUR, end, ["CorrelationId"], ["azure-123"], raw=True)
    assert f'_ResourceId =~ "{LOWER_RESOURCE}"' in scoped and scoped.endswith("| take 21")
    resources = resources_query()
    assert "by _ResourceId, Resource" in resources and resources.endswith("| take 50")


def test_lookup_inputs_reject_injection_and_wide_windows(connection):
    base = {"connection": connection.model_dump(), "start": UTC_HOUR.isoformat()}
    hour = {**base, "end": (UTC_HOUR + timedelta(hours=1)).isoformat()}
    assert CandidateQuery.model_validate({**hour, "scope": LOWER_RESOURCE}).scope == LOWER_RESOURCE
    assert TraceQuery.model_validate({**hour, "request_id": " azure-123 \n"}).request_id == (
        "azure-123"
    )
    default = TraceQuery.model_validate({"connection": connection.model_dump(), "request_id": "a"})
    assert default.end - default.start == timedelta(days=30)
    invalid = [
        (CandidateQuery, {**hour, "deployment": 'gpt" or 1==1'}),
        (CandidateQuery, {**hour, "object_id": "not-a-guid"}),
        (CandidateQuery, {**hour, "scope": f"{RESOURCE}/../evil"}),
        (CandidateQuery, {**hour, "scope": '") | union *'}),
        (CandidateQuery, {**base, "end": (UTC_HOUR + timedelta(days=8)).isoformat()}),
        (CandidateQuery, {**base, "end": UTC_HOUR.isoformat()}),
        (CandidateQuery, {**base, "end": "2026-11-01T05:00:00"}),
        (TraceQuery, {**hour, "request_id": 'x") | union *'}),
        (TraceQuery, {**hour, "request_id": ""}),
        (TraceQuery, {**base, "request_id": "a", "end": "2026-12-15T00:00:00+00:00"}),
        (
            TraceQuery,
            {
                **base,
                "request_id": "a",
                "end": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "start": datetime.now(UTC).isoformat(),
            },
        ),
    ]
    for model, payload in invalid:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


async def test_trace_builds_azure_only_timeline_and_marks_local_run(
    connection, settings, monkeypatch
):
    calls = mock_lookup_response(monkeypatch)
    engine = engine_for(settings, None, AzureLogs())
    run = Run(
        connection=connection,
        max_completion_tokens=10,
        status="succeeded",
        request_ids={"apim-request-id": "AZURE-123"},
    )
    engine.save(run)
    stored = Store(settings.database_path).get(run.id).model_dump_json()
    trace = await engine.lookup_trace(
        TraceQuery(connection=connection, request_id="azure-123", end=datetime.now(UTC))
    )
    assert trace.logs.state == "matched"
    assert trace.local_run_id == run.id
    assert trace.resources == [LOWER_RESOURCE] and trace.raw_scope == LOWER_RESOURCE
    assert trace.raw_start == "2026-11-01T04:23:50.065000+00:00"
    assert trace.raw_end == "2026-11-01T04:35:22+00:00"
    assert {event.source for event in trace.events} == {"azure"}
    titles = {event.title for event in trace.events}
    assert "Azure RequestResponse: service processing" in titles
    assert "Azure usage log: full output (TTFT = TTLT)" in titles
    assert not any("since_run_start_ms" in event.details for event in trace.events)
    assert "secret prompt" not in trace.model_dump_json()
    query, timespan = calls[0]
    assert "_ResourceId =~" not in query and '__id0 in ("azure-123")' in query
    assert timespan[1] - timespan[0] == timedelta(days=30)
    assert Store(settings.database_path).get(run.id).model_dump_json() == stored
    assert len(Store(settings.database_path).list()) == 1

    missing = await engine.lookup_trace(
        TraceQuery(connection=connection, request_id="unknown", scope=RESOURCE)
    )
    assert missing.logs.state == "pending" and missing.events == []
    assert "llm_provider-apim-request-id" in missing.logs.message
    assert missing.local_run_id is None and missing.raw_start is None
    assert f'_ResourceId =~ "{RESOURCE}"' in calls[-1][0]


async def test_candidates_mark_local_runs_and_mask_ip(connection, settings, monkeypatch):
    mock_lookup_response(monkeypatch, candidate_rows=51)
    engine = engine_for(settings, None, AzureLogs())
    run = Run(
        connection=connection,
        max_completion_tokens=10,
        request_ids={"apim-request-id": "azure-123"},
    )
    engine.save(run)
    result = await engine.lookup_candidates(
        CandidateQuery(connection=connection, start=UTC_HOUR, end=UTC_HOUR + timedelta(hours=1))
    )
    assert result.state == "ok" and result.limited and len(result.rows) == 50
    first, other = result.rows[0], result.rows[1]
    assert first.local_run_id == run.id
    assert first.caller_ip == "10.1.2.**"
    assert first.model_version == "2026-03-17"
    assert first.prompt_tokens == 19 and first.completion_tokens == 49
    assert other.request_id == "" and other.local_run_id is None
    assert other.caller_ip == "203.0.113.**"
    resources = await engine.lookup_resources(LookupBase(connection=connection))
    assert [item.resource_id for item in resources.resources] == [LOWER_RESOURCE]
    assert resources.resources[0].inference == 3


def test_lookup_api_guards_and_hides_errors(settings, connection):
    class FailingLookup(FakeLogs):
        async def trace(self, *args, **kwargs):
            self.calls += 1
            raise HttpResponseError(message="secret Azure response")

        async def candidates(self, *args, **kwargs):
            raise HttpResponseError(message="secret Azure response")

    async def handler(request):
        pytest.fail("Lookups must not call the model.")

    logs = FailingLookup()
    engine = engine_for(settings, handler, logs)
    body = {"connection": connection.model_dump(), "request_id": "azure-123"}
    with TestClient(create_app(settings, engine), base_url="http://localhost") as client:
        response = client.post("/api/lookup/trace", json=body)
        assert response.status_code == 200
        assert response.json()["logs"]["state"] == "error"
        assert '__id0 in ("azure-123")' in response.json()["logs"]["query"]
        assert "secret Azure response" not in response.text
        candidates = client.post(
            "/api/lookup/candidates",
            json={
                "connection": connection.model_dump(),
                "start": UTC_HOUR.isoformat(),
                "end": (UTC_HOUR + timedelta(hours=1)).isoformat(),
            },
        )
        assert candidates.json()["state"] == "error"
        assert "| take 51" in candidates.json()["query"]
        assert "secret Azure response" not in candidates.text
        rejected = client.post(
            "/api/lookup/trace", json={**body, "request_id": 'x") | union secret_table'}
        )
        assert rejected.status_code == 422 and "secret_table" not in rejected.text
        assert (
            client.post(
                "/api/lookup/trace",
                json=body,
                headers={"Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )
        no_workspace = {**body, "connection": {**connection.model_dump(), "workspace_id": ""}}
        assert client.post("/api/lookup/trace", json=no_workspace).json()["logs"]["state"] == (
            "unavailable"
        )
        for _ in range(2):
            asyncio.run(engine.lookup_slots.acquire())
        busy = client.post("/api/lookup/trace", json=body)
        assert busy.status_code == 429
        engine.lookup_slots.release()
        engine.lookup_slots.release()
    assert logs.calls == 1
    assert Store(settings.database_path).list() == []


ARM_HOST = "management.azure.com"
WORKSPACE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/logs"
    "/providers/Microsoft.OperationalInsights/workspaces/shared-logs"
)
LOCKED_WORKSPACE = (
    "/subscriptions/00000000-0000-0000-0000-000000000003/resourceGroups/other"
    "/providers/Microsoft.OperationalInsights/workspaces/locked-logs"
)


def discovery_for(handler, tokens=None):
    tokens = [] if tokens is None else tokens

    async def token(body):
        tokens.append(body.logs_auth_mode)
        return f"token-{len(tokens)}", time.time() + 3600

    return AzureDiscovery(transport=httpx.MockTransport(handler), token=token)


def arm_detail_handler(requests, next_link=None):
    def handler(request):
        requests.append(request)
        assert request.headers["authorization"].startswith("Bearer token-")
        path, version = request.url.path, request.url.params.get("api-version")
        if path == RESOURCE:
            assert version == "2024-10-01"
            return httpx.Response(
                200,
                json={
                    "name": "example",
                    "location": "eastus2",
                    "kind": "AIServices",
                    "properties": {"endpoint": "https://example.cognitiveservices.azure.com/"},
                },
            )
        if path == f"{RESOURCE}/deployments" and "$skiptoken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "zeta",
                            "sku": {"name": "GlobalStandard", "capacity": 50},
                            "properties": {
                                "model": {"name": "gpt-x", "version": "1", "format": "OpenAI"},
                                "provisioningState": "Succeeded",
                            },
                        }
                    ],
                    "nextLink": next_link
                    or f"https://{ARM_HOST}{RESOURCE}/deployments?api-version=2024-10-01"
                    "&$skiptoken=page-2",
                },
            )
        if path == f"{RESOURCE}/deployments":
            assert version == "2024-10-01"
            return httpx.Response(
                200, json={"value": [{"name": "alpha", "sku": {"capacity": True}}, {"bad": 1}]}
            )
        if path == f"{RESOURCE}/providers/Microsoft.Insights/diagnosticSettings":
            assert version == "2021-05-01-preview"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "everything",
                            "properties": {
                                "workspaceId": WORKSPACE,
                                "storageAccountId": "/subscriptions/00000000-0000-0000-0000-"
                                "000000000001/resourceGroups/logs/providers/"
                                "Microsoft.Storage/storageAccounts/archive",
                                "logs": [{"categoryGroup": "allLogs", "enabled": True}],
                            },
                        },
                        {
                            "name": "audit-only",
                            "properties": {
                                "workspaceId": WORKSPACE.upper().replace(
                                    "/SUBSCRIPTIONS", "/subscriptions"
                                ),
                                "logs": [{"categoryGroup": "audit", "enabled": True}],
                            },
                        },
                        {
                            "name": "requests",
                            "properties": {
                                "workspaceId": LOCKED_WORKSPACE,
                                "logAnalyticsDestinationType": "Dedicated",
                                "logs": [
                                    {"category": "RequestResponse", "enabled": True},
                                    {"category": "AzureOpenAIRequestUsage", "enabled": False},
                                ],
                            },
                        },
                        {
                            "name": "stream",
                            "properties": {
                                "eventHubAuthorizationRuleId": "/subscriptions/00000000-0000-"
                                "0000-0000-000000000001/resourceGroups/logs/providers/"
                                "Microsoft.EventHub/namespaces/hub/authorizationrules/send",
                                "logs": [{"categoryGroup": "allLogs", "enabled": True}],
                            },
                        },
                        {"name": "unsafe", "properties": {"workspaceId": "/not/a/workspace"}},
                    ]
                },
            )
        if path.lower() == WORKSPACE.lower():
            assert version == "2023-09-01"
            return httpx.Response(
                200,
                json={
                    "name": "shared-logs",
                    "location": "eastus2",
                    "properties": {"customerId": "00000000-0000-0000-0000-00000000000A"},
                },
            )
        if path == LOCKED_WORKSPACE:
            return httpx.Response(403, json={"error": {"message": "secret ARM detail"}})
        pytest.fail(f"Unexpected ARM request {request.method} {request.url}")

    return handler


def test_discovery_lists_accessible_foundries_from_resource_graph():
    requests, tokens = [], []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.host == ARM_HOST
        assert request.url.path == "/providers/Microsoft.ResourceGraph/resources"
        assert request.url.params["api-version"] == "2022-10-01"
        body = json.loads(request.content)
        assert "subscriptions" not in body
        assert body["options"] == {"resultFormat": "objectArray", "$top": 201}
        assert '"microsoft.cognitiveservices/accounts"' in body["query"]
        return httpx.Response(
            200,
            json={
                "resultTruncated": "false",
                "data": [
                    {
                        "id": RESOURCE,
                        "name": "example",
                        "location": "eastus2",
                        "kind": "AIServices",
                        "resourceGroup": "test",
                        "subscriptionId": "00000000-0000-0000-0000-000000000001",
                        "subscription": "Lab subscription",
                        "endpoint": "https://example.cognitiveservices.azure.com/",
                    },
                    {
                        "id": RESOURCE.replace("example", "Alpha"),
                        "name": "Alpha",
                        "endpoint": "https://alpha.evil.example/",
                    },
                    {"id": "/subscriptions/x/providers/Microsoft.Web/sites/nope", "name": "x"},
                    "not-an-object",
                ],
            },
        )

    discovery = discovery_for(handler, tokens)
    body = DiscoveryBase(logs_auth_mode="azure_cli")
    found = asyncio.run(discovery.foundries(body))
    assert found.state == "ok" and not found.limited
    assert [item.name for item in found.items] == ["Alpha", "example"]
    assert found.items[0].endpoint == ""
    assert found.items[1].endpoint == "https://example.cognitiveservices.azure.com/openai/v1"
    assert found.items[1].subscription == "Lab subscription"
    assert "2 个" in found.message
    asyncio.run(discovery.foundries(body))
    assert tokens == ["azure_cli"]
    assert all(r.headers["authorization"] == "Bearer token-1" for r in requests)


def test_discovery_detail_resolves_workspaces_deployments_and_destinations():
    requests = []
    discovery = discovery_for(arm_detail_handler(requests))
    detail = asyncio.run(discovery.foundry(FoundryQuery(resource_id=RESOURCE)))
    assert detail.endpoint == "https://example.cognitiveservices.azure.com/openai/v1"
    assert (detail.name, detail.location, detail.kind) == ("example", "eastus2", "AIServices")
    assert [item.name for item in detail.deployments] == ["alpha", "zeta"]
    assert detail.deployments[0].capacity is None
    assert detail.deployments[1].model_dump() == {
        "name": "zeta",
        "model": "gpt-x",
        "version": "1",
        "format": "OpenAI",
        "sku": "GlobalStandard",
        "capacity": 50,
        "state": "Succeeded",
    }
    shared, locked = detail.workspaces
    assert shared.workspace_id == "00000000-0000-0000-0000-00000000000a"
    assert shared.settings == ["everything", "audit-only"]
    assert (shared.request_logs, shared.usage_logs, shared.dedicated) == (True, True, False)
    assert locked.workspace_id == "" and locked.name == "locked-logs"
    assert (locked.request_logs, locked.usage_logs, locked.dedicated) == (True, False, True)
    assert "Reader" in locked.error and "secret" not in locked.error
    assert [(item.kind, item.name, item.setting) for item in detail.other_destinations] == [
        ("storage", "archive", "everything"),
        ("event_hub", "hub", "stream"),
    ]
    assert detail.state == "partial" and not detail.diagnostics_error
    assert all(r.method == "GET" for r in requests)


def test_discovery_refuses_next_link_outside_arm():
    requests = []
    handler = arm_detail_handler(requests, next_link="https://attacker.example/steal?$skiptoken=1")
    detail = asyncio.run(discovery_for(handler).foundry(FoundryQuery(resource_id=RESOURCE)))
    assert detail.deployments == [] and detail.deployments_error == "ValueError"
    assert detail.state == "partial" and detail.workspaces
    assert {r.url.host for r in requests} == {ARM_HOST}


def test_discovery_api_validates_input_and_hides_arm_errors(settings):
    tokens = []

    def handler(request):
        if request.url.path == RESOURCE:
            return httpx.Response(404, json={"error": {"message": "secret ARM detail"}})
        return httpx.Response(401, text="secret ARM detail")

    async def model(request):
        pytest.fail("Discovery must not call the model.")

    engine = engine_for(settings, model, discovery=discovery_for(handler, tokens))
    with TestClient(create_app(settings, engine), base_url="http://localhost") as client:
        listed = client.post("/api/discovery/foundries", json={"logs_auth_mode": "azure_cli"})
        assert listed.status_code == 200 and listed.json()["state"] == "error"
        assert "Reader" in listed.json()["message"] and "secret" not in listed.text
        client.post("/api/discovery/foundries", json={"logs_auth_mode": "azure_cli"})
        assert tokens == ["azure_cli", "azure_cli"]
        detail = client.post(
            "/api/discovery/foundry",
            json={"logs_auth_mode": "azure_cli", "resource_id": RESOURCE},
        )
        assert detail.json()["state"] == "error"
        assert detail.json()["resource_id"] == RESOURCE
        assert "404" in detail.json()["message"] and "secret" not in detail.text
        for invalid in (
            {"resource_id": ""},
            {"resource_id": WORKSPACE},
            {"resource_id": f"{RESOURCE}/../../other"},
            {"resource_id": RESOURCE, "managed_identity_client_id": "not-a-guid"},
        ):
            assert client.post("/api/discovery/foundry", json=invalid).status_code == 422
        assert client.post("/api/discovery/foundries", content="{}").status_code == 415
    assert Store(settings.database_path).list() == []
