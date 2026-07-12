import sqlite3
from pathlib import Path

from backpack_bench.storage import SCHEMA_VERSION, Storage


def test_schema_v1_database_migrates_live_token_columns(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '1');
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                profile_hash TEXT NOT NULL,
                scenario_hash TEXT NOT NULL,
                trial INTEGER NOT NULL,
                weight REAL NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(run_id, profile_hash, scenario_hash, trial)
            );
            """
        )

    storage = Storage(database)
    try:
        version = storage.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        columns = {row["name"] for row in storage.connection.execute("PRAGMA table_info(jobs)")}
    finally:
        storage.close()

    assert int(version) == SCHEMA_VERSION
    assert {"live_output_tokens", "live_tokens_estimated"} <= columns
