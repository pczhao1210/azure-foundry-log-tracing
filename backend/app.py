import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .engine import Engine
from .models import (
    CandidateQuery,
    Candidates,
    Connection,
    LookupBase,
    LookupResources,
    NewRun,
    QueryMapping,
    RawLogs,
    Run,
    Settings,
    Trace,
    TraceQuery,
)
from .store import Store

T = TypeVar("T")


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if engine is None:
            store = Store(settings.database_path)
            store.recover()
            app.state.engine = Engine(settings, store)
        else:
            app.state.engine = engine
        yield
        await app.state.engine.close()

    app = FastAPI(title="Foundry Request Lab", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse(
                {"detail": "Cross-origin requests are not allowed."}, status_code=403
            )
        if request.method == "POST" and not request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return JSONResponse({"detail": "application/json is required."}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"detail": [{"loc": list(err["loc"]), "msg": err["msg"]} for err in exc.errors()]},
            status_code=422,
        )

    def service(request: Request) -> Engine:
        return request.app.state.engine

    def find(request: Request, run_id: str) -> Run:
        run = service(request).get(run_id)
        if run is None:
            raise HTTPException(404, "Experiment not found.")
        return run

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/config")
    async def config():
        return {
            "endpoint": settings.foundry_endpoint,
            "deployment": settings.foundry_deployment,
            "auth_mode": settings.llm_auth_mode,
            "managed_identity_client_id": settings.azure_client_id,
            "logs_auth_mode": settings.logs_auth_mode,
            "workspace_id": settings.log_analytics_workspace_id,
            "resource_id": settings.foundry_resource_id,
            "log_id_field": settings.log_id_field,
            "mapping_verified": settings.log_id_mapping_verified,
            "api_key_configured": bool(settings.foundry_api_key),
        }

    @app.post("/api/check")
    async def check(body: Connection, request: Request):
        return await service(request).check(body)

    @app.post("/api/runs", status_code=202)
    async def create(body: NewRun, request: Request):
        active = sum(not task.done() for task in service(request).tasks.values())
        if active >= 3:
            raise HTTPException(429, "At most three model requests may run concurrently.")
        return service(request).create(body)

    @app.get("/api/runs")
    async def history(request: Request):
        return [
            {
                "id": run.id,
                "created_at": run.created_at,
                "status": run.status,
                "deployment": run.connection.deployment,
                "auth_mode": run.connection.auth_mode,
                "log_state": run.logs.state,
            }
            for run in service(request).store.list()
        ]

    @app.get("/api/runs/{run_id}")
    async def detail(run_id: str, request: Request):
        return find(request, run_id)

    @app.post("/api/runs/{run_id}/query", status_code=202)
    async def query(run_id: str, body: QueryMapping, request: Request):
        run = find(request, run_id)
        if run.status in ("queued", "running"):
            raise HTTPException(409, "Wait for the model attempt to finish before querying logs.")
        previous = service(request).log_tasks.get(run.id)
        if previous is not None and not previous.done():
            raise HTTPException(409, "A log query or automatic polling is already active.")
        try:
            run.connection = Connection.model_validate(
                {
                    **run.connection.model_dump(),
                    **body.model_dump(),
                }
            )
        except ValueError:
            raise HTTPException(422, "Invalid request ID mapping.") from None
        if not service(request).start_query(run):
            raise HTTPException(409, "A log query or automatic polling is already active.")
        return run

    @app.post("/api/runs/{run_id}/raw-logs")
    async def raw_logs(run_id: str, request: Request):
        run = find(request, run_id)
        engine = service(request)
        # Serialize per run: an aborted browser fetch must not stack Azure queries.
        async with engine.raw_locks.setdefault(run.id, asyncio.Lock()):
            return await engine.raw_logs(run)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        run = find(request, run_id)
        await service(request).cancel(run)
        return run

    async def lookup(request: Request, work: Callable[[Engine], Awaitable[T]]) -> T:
        engine = service(request)
        # Reject instead of queueing: a 30-day workspace scan is expensive.
        if engine.lookup_slots.locked():
            raise HTTPException(429, "At most two log lookups may run concurrently.")
        async with engine.lookup_slots:
            return await work(engine)

    @app.post("/api/lookup/resources")
    async def lookup_resources(body: LookupBase, request: Request) -> LookupResources:
        return await lookup(request, lambda engine: engine.lookup_resources(body))

    @app.post("/api/lookup/candidates")
    async def lookup_candidates(body: CandidateQuery, request: Request) -> Candidates:
        return await lookup(request, lambda engine: engine.lookup_candidates(body))

    @app.post("/api/lookup/trace")
    async def lookup_trace(body: TraceQuery, request: Request) -> Trace:
        return await lookup(request, lambda engine: engine.lookup_trace(body))

    @app.post("/api/lookup/raw-logs")
    async def lookup_raw(body: TraceQuery, request: Request) -> RawLogs:
        return await lookup(request, lambda engine: engine.lookup_raw(body))

    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app
