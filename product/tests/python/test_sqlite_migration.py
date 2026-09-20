from __future__ import annotations

import sqlite3
from pathlib import Path


def test_initial_migration_creates_required_tables_and_wal(tmp_path: Path) -> None:
    database = tmp_path / "foundation.sqlite3"
    migration = Path("infra/sqlite/0001_initial.sql").read_text(encoding="utf-8")

    with sqlite3.connect(database) as connection:
        connection.executescript(migration)
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    assert journal_mode == "wal"
    assert {
        "tasks",
        "spec_versions",
        "runs",
        "steps",
        "artifacts",
        "artifact_edges",
        "events",
        "approvals",
        "model_capabilities",
        "schema_migrations",
    } <= tables


def test_agent_workspace_migration_upgrades_a_0001_database(tmp_path: Path) -> None:
    database = tmp_path / "workspace.sqlite3"
    initial = Path("infra/sqlite/0001_initial.sql").read_text(encoding="utf-8")
    workspace = Path("infra/sqlite/0002_agent_workspace.sql").read_text(encoding="utf-8")

    with sqlite3.connect(database) as connection:
        connection.executescript(initial)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [("0001_initial",)]
        connection.executescript(workspace)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        foreign_keys = {
            row[2] for row in connection.execute("PRAGMA foreign_key_list(conversations)")
        }

    assert {
        "projects",
        "conversations",
        "agent_sessions",
        "messages",
        "approval_requests",
    } <= tables
    assert versions == [("0001_initial",), ("0002_agent_workspace",)]
    assert {"projects", "tasks"} <= foreign_keys


def test_p02_campaign_migration_is_additive_and_enforces_limits(tmp_path: Path) -> None:
    database = tmp_path / "campaign.sqlite3"
    migrations = [
        Path("infra/sqlite/0001_initial.sql").read_text(encoding="utf-8"),
        Path("infra/sqlite/0002_agent_workspace.sql").read_text(encoding="utf-8"),
        Path("infra/sqlite/0003_p02_campaign.sql").read_text(encoding="utf-8"),
    ]
    with sqlite3.connect(database) as connection:
        for migration in migrations:
            connection.executescript(migration)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert {
        "campaigns",
        "campaign_agents",
        "campaign_leases",
        "campaign_actions",
        "campaign_checkpoints",
        "external_requests",
        "quantum_jobs",
        "failure_cards",
    } <= tables
    assert versions == [
        ("0001_initial",),
        ("0002_agent_workspace",),
        ("0003_p02_campaign",),
    ]


def test_p04_migration_adds_run_tool_fault_and_global_hardware_lease_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "p04.sqlite3"
    migrations = [
        Path(f"infra/sqlite/{name}").read_text(encoding="utf-8")
        for name in (
            "0001_initial.sql",
            "0002_agent_workspace.sql",
            "0003_p02_campaign.sql",
            "0004_p03_queue.sql",
            "0005_p04_concurrent_campaign.sql",
        )
    ]
    with sqlite3.connect(database) as connection:
        for migration in migrations:
            connection.executescript(migration)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert {
        "p04_runs",
        "p04_run_leases",
        "p04_run_events",
        "p04_artifact_claims",
        "p04_generated_tools",
        "p04_tool_invocations",
        "p04_fault_injections",
        "p04_hardware_lease",
    } <= tables
