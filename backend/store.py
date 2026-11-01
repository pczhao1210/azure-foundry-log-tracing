import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .models import Run, now


class Store:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL)"
            )
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def save(self, run: Run) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (run.id, run.created_at, run.model_dump_json(exclude={"answer"})),
            )

    def get(self, run_id: str) -> Run | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list(self) -> list[Run]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM runs ORDER BY created_at DESC LIMIT 100").fetchall()
        return [Run.model_validate_json(row[0]) for row in rows]

    def find_requests(self, request_ids: Iterable[str]) -> dict[str, str]:
        """Map lower-cased apim-request-id values to the local run that received them."""
        ids = sorted({value.lower() for value in request_ids if value})[:200]
        if not ids:
            return {}
        expression = "lower(json_extract(data, '$.request_ids.\"apim-request-id\"'))"
        with self.connect() as db:
            rows = db.execute(
                f"SELECT {expression}, id FROM runs "
                f"WHERE {expression} IN ({', '.join('?' * len(ids))}) ORDER BY created_at",
                ids,
            ).fetchall()
        return {request_id: run_id for request_id, run_id in rows}

    def recover(self) -> None:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM runs").fetchall()
        for row in rows:
            run = Run.model_validate_json(row[0])
            changed = False
            if run.status in ("queued", "running"):
                run.status = "failed"
                run.error = "Server restarted; the request outcome is unknown. It was not retried."
                run.finished_at = now()
                changed = True
            if run.logs.polling or run.logs.state == "querying":
                run.logs.polling = False
                run.logs.state = "error"
                run.logs.message = (
                    "Server restarted. Query logs again; this will not call the model."
                )
                changed = True
            for event in run.events:
                if event.status == "running":
                    event.status = "error"
                    event.ended_at = now()
                    event.details["interrupted"] = True
                    changed = True
            if changed:
                self.save(run)
