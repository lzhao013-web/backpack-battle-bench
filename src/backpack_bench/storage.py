"""SQLite run index with a single asynchronous writer queue."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                suite_id TEXT NOT NULL,
                suite_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                profile_hash TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                protocol TEXT NOT NULL,
                model TEXT NOT NULL,
                config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenarios (
                scenario_hash TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                title TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                oracle_attack INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                profile_hash TEXT NOT NULL REFERENCES profiles(profile_hash),
                scenario_hash TEXT NOT NULL REFERENCES scenarios(scenario_hash),
                trial INTEGER NOT NULL,
                weight REAL NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(run_id, profile_hash, scenario_hash, trial)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                attempt_no INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                http_status INTEGER,
                error_type TEXT,
                error_message TEXT,
                latency_ms REAL NOT NULL,
                usage_json TEXT NOT NULL,
                artifact_dir TEXT NOT NULL,
                UNIQUE(job_id, attempt_no)
            );
            CREATE TABLE IF NOT EXISTS results (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                valid INTEGER NOT NULL,
                error_type TEXT,
                actual_attack INTEGER NOT NULL,
                oracle_attack INTEGER NOT NULL,
                ratio REAL NOT NULL,
                finish_reason TEXT,
                validation_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                estimated_cost REAL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_run_status ON jobs(run_id, status);
            CREATE INDEX IF NOT EXISTS idx_runs_suite ON runs(suite_id, suite_hash, status);
            """
        )
        current = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if current is None:
            self.connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(current["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current['value']} is not supported; expected {SCHEMA_VERSION}"
            )
        self.connection.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def create_run(self, value: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO runs(run_id, plan_id, plan_hash, suite_id, suite_hash, status,
                             started_at, completed_at, config_json)
            VALUES(:run_id, :plan_id, :plan_hash, :suite_id, :suite_hash, :status,
                   :started_at, NULL, :config_json)
            """,
            {**value, "config_json": self._json(value["config"])},
        )
        self.connection.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(
        self,
        plan_hash: str | None = None,
        plan_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if plan_hash is not None and plan_id is not None:
            raise ValueError("plan_hash and plan_id are mutually exclusive")
        if plan_hash is not None:
            rows = self.connection.execute(
                "SELECT * FROM runs WHERE plan_hash=? ORDER BY started_at DESC LIMIT ?",
                (plan_hash, limit),
            )
        elif plan_id is not None:
            rows = self.connection.execute(
                "SELECT * FROM runs WHERE plan_id=? ORDER BY started_at DESC LIMIT ?",
                (plan_id, limit),
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in rows]

    def run_profiles(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT p.profile_hash, p.profile_id, p.display_name, p.protocol,
                            p.model, p.config_json
            FROM jobs j
            JOIN profiles p ON p.profile_hash=j.profile_hash
            WHERE j.run_id=?
            ORDER BY p.profile_id
            """,
            (run_id,),
        )
        return [
            {
                "profile_hash": row["profile_hash"],
                "profile_id": row["profile_id"],
                "display_name": row["display_name"],
                "protocol": row["protocol"],
                "model": row["model"],
                "config": json.loads(row["config_json"]),
            }
            for row in rows
        ]

    def run_progress(self, run_id: str) -> dict[str, int]:
        status_rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM jobs WHERE run_id=? GROUP BY status",
            (run_id,),
        )
        counts = {str(row["status"]): int(row["count"]) for row in status_rows}
        attempts = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM attempts a
            JOIN jobs j ON j.job_id=a.job_id
            WHERE j.run_id=?
            """,
            (run_id,),
        ).fetchone()
        valid = self.connection.execute(
            """
            SELECT COALESCE(SUM(r.valid), 0) AS count
            FROM results r
            JOIN jobs j ON j.job_id=r.job_id
            WHERE j.run_id=?
            """,
            (run_id,),
        ).fetchone()
        total = sum(counts.values())
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("completed", 0),
            "attempts": int(attempts["count"]),
            "valid": int(valid["count"]),
        }

    def register_profile(self, value: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO profiles(profile_hash, profile_id, display_name, protocol,
                                           model, config_json)
            VALUES(:profile_hash, :profile_id, :display_name, :protocol, :model, :config_json)
            """,
            {**value, "config_json": self._json(value["config"])},
        )
        self.connection.commit()

    def register_scenario(self, value: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO scenarios(scenario_hash, scenario_id, title, difficulty,
                                            tags_json, oracle_attack)
            VALUES(:scenario_hash, :scenario_id, :title, :difficulty, :tags_json, :oracle_attack)
            """,
            {**value, "tags_json": self._json(value["tags"])},
        )
        self.connection.commit()

    def create_job(self, value: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO jobs(job_id, run_id, profile_hash, scenario_hash, trial,
                                       weight, status)
            VALUES(:job_id, :run_id, :profile_hash, :scenario_hash, :trial, :weight, 'pending')
            """,
            value,
        )
        self.connection.commit()

    def reset_interrupted(self, run_id: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET status='pending' WHERE run_id=? AND status='running'", (run_id,)
        )
        self.connection.execute(
            "UPDATE runs SET status='running', completed_at=NULL WHERE run_id=?", (run_id,)
        )
        self.connection.commit()

    def reset_running_jobs(self, run_id: str) -> None:
        """Return in-flight jobs to pending after a cooperative interruption."""
        self.connection.execute(
            "UPDATE jobs SET status='pending' WHERE run_id=? AND status='running'", (run_id,)
        )
        self.connection.commit()

    def set_job_status(self, job_id: str, status: str) -> None:
        self.connection.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))
        self.connection.commit()

    def completed_job_ids(self, run_id: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT job_id FROM jobs WHERE run_id=? AND status='completed'", (run_id,)
        )
        return {str(row["job_id"]) for row in rows}

    def has_engine_oracle_inconsistency(self, run_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM results r
            JOIN jobs j ON j.job_id=r.job_id
            WHERE j.run_id=? AND r.error_type='engine_oracle_inconsistency'
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return row is not None

    def next_attempt_no(self, job_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) AS value FROM attempts WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row["value"]) + 1

    def insert_attempt(self, value: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO attempts(job_id, attempt_no, started_at, completed_at, http_status,
                                 error_type, error_message, latency_ms, usage_json, artifact_dir)
            VALUES(:job_id, :attempt_no, :started_at, :completed_at, :http_status,
                   :error_type, :error_message, :latency_ms, :usage_json, :artifact_dir)
            """,
            {**value, "usage_json": self._json(value.get("usage", {}))},
        )
        self.connection.commit()

    def save_result(self, value: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO results(job_id, valid, error_type, actual_attack,
                                           oracle_attack, ratio, finish_reason, validation_json,
                                           usage_json, latency_ms, estimated_cost)
            VALUES(:job_id, :valid, :error_type, :actual_attack, :oracle_attack, :ratio,
                   :finish_reason, :validation_json, :usage_json, :latency_ms,
                   :estimated_cost)
            """,
            {
                **value,
                "valid": int(bool(value["valid"])),
                "validation_json": self._json(value["validation"]),
                "usage_json": self._json(value.get("usage", {})),
            },
        )
        self.connection.execute(
            "UPDATE jobs SET status='completed' WHERE job_id=?", (value["job_id"],)
        )
        self.connection.commit()

    def complete_run(self, run_id: str, status: str, completed_at: str) -> None:
        self.connection.execute(
            "UPDATE runs SET status=?, completed_at=? WHERE run_id=?",
            (status, completed_at, run_id),
        )
        self.connection.commit()

    def report_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT j.*, p.profile_id, p.display_name, p.protocol, p.model, p.config_json,
                   s.scenario_id, s.title, s.difficulty, s.tags_json,
                   s.oracle_attack AS exact_oracle_attack,
                   r.valid, r.error_type, r.actual_attack, r.oracle_attack, r.ratio,
                   r.finish_reason, r.validation_json, r.usage_json,
                   r.latency_ms, r.estimated_cost,
                   r.job_id AS result_job_id,
                   (SELECT COUNT(*) FROM attempts a WHERE a.job_id=j.job_id) AS attempt_count
            FROM jobs j
            JOIN profiles p ON p.profile_hash=j.profile_hash
            JOIN scenarios s ON s.scenario_hash=j.scenario_hash
            LEFT JOIN results r ON r.job_id=j.job_id
            WHERE j.run_id=?
            ORDER BY p.profile_id, s.scenario_id, j.trial
            """,
            (run_id,),
        )
        return [dict(row) for row in rows]

    def run_job_rows(self, run_id: str) -> list[dict[str, Any]]:
        """Return the lightweight per-job state used by the live Web view."""
        rows = self.connection.execute(
            """
            SELECT j.job_id, j.trial, j.status,
                   p.profile_id, p.display_name, p.model,
                   s.scenario_id, s.title,
                   r.job_id AS result_job_id, r.valid, r.error_type, r.usage_json,
                   (SELECT COUNT(*) FROM attempts a WHERE a.job_id=j.job_id) AS attempt_count
            FROM jobs j
            JOIN profiles p ON p.profile_hash=j.profile_hash
            JOIN scenarios s ON s.scenario_hash=j.scenario_hash
            LEFT JOIN results r ON r.job_id=j.job_id
            WHERE j.run_id=?
            ORDER BY CASE j.status
                         WHEN 'running' THEN 0
                         WHEN 'pending' THEN 1
                         ELSE 2
                     END,
                     p.profile_id, s.scenario_id, j.trial
            """,
            (run_id,),
        )
        return [dict(row) for row in rows]

    def report_attempt_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT a.job_id, a.attempt_no, a.started_at, a.completed_at,
                   a.http_status, a.error_type, a.error_message, a.latency_ms,
                   a.usage_json
            FROM attempts a
            JOIN jobs j ON j.job_id=a.job_id
            WHERE j.run_id=?
            ORDER BY a.job_id, a.attempt_no
            """,
            (run_id,),
        )
        return [dict(row) for row in rows]

    def latest_completed_runs(self, suite_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM runs
            WHERE suite_id=? AND status='completed'
            ORDER BY completed_at DESC
            """,
            (suite_id,),
        )
        return [dict(row) for row in rows]


class DbWriter:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.queue: asyncio.Queue[
            tuple[Callable[..., Any] | None, tuple[Any, ...], asyncio.Future[Any] | None]
        ] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            function, arguments, future = await self.queue.get()
            try:
                if function is None:
                    return
                result = function(*arguments)
                if future is not None and not future.done():
                    future.set_result(result)
            except Exception as error:
                if future is not None and not future.done():
                    future.set_exception(error)
            finally:
                self.queue.task_done()

    async def submit(self, function: Callable[..., Any], *arguments: Any) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self.queue.put((function, arguments, future))
        return await future

    async def close(self) -> None:
        await self.queue.put((None, (), None))
        await self.queue.join()
        if self.task is not None:
            await self.task
