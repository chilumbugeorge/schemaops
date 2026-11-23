"""Integration tests against a real MariaDB server and the real pt-online-schema-change.

Skipped unless SCHEMAOPS_IT_HOST is set. They create and drop the database `schemaops_it`
and the accounts `schemaops_it` and `schemaops_it_ro`, so point them at a disposable server:

    docker run -d --name schemaops-it -p 3306:3306 -e MARIADB_ROOT_PASSWORD=it-root-pw mariadb:11.4
    SCHEMAOPS_IT_HOST=127.0.0.1 SCHEMAOPS_IT_ROOT_PASSWORD=it-root-pw python -m pytest tests/integration
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemaops import cli
from schemaops.db import ConnectionConfig
from schemaops.deploy import MariaDBTarget, deploy
from schemaops.errors import DeployError
from schemaops.repo import OnlineChangeSettings

from conftest import DUMP, Repo

HOST = os.environ.get("SCHEMAOPS_IT_HOST", "")
PORT = int(os.environ.get("SCHEMAOPS_IT_PORT", "3306"))
DATABASE = "schemaops_it"
PASSWORD = "it-migrate-pw"
RO_PASSWORD = "it-readonly-pw"

pytestmark = [
    pytest.mark.skipif(not HOST, reason="SCHEMAOPS_IT_HOST is not set"),
    pytest.mark.skipif(shutil.which("pt-online-schema-change") is None, reason="pt-online-schema-change is not installed"),
]

ADD_COLUMN = "2026-11-20_add_display_name.sql"
ADD_COLUMN_SQL = "ALTER TABLE users ADD COLUMN display_name VARCHAR(64) NULL COMMENT 'shown to 100% of users';\n"


def admin():  # noqa: ANN201
    import pymysql

    return pymysql.connect(
        host=HOST,
        port=PORT,
        user=os.environ.get("SCHEMAOPS_IT_ROOT_USER", "root"),
        password=os.environ.get("SCHEMAOPS_IT_ROOT_PASSWORD", ""),
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def sql(connection, statement: str) -> list[dict]:  # noqa: ANN001
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return list(cursor.fetchall() or [])


@pytest.fixture
def root() -> Iterator[object]:
    connection = admin()
    for statement in (
        f"DROP DATABASE IF EXISTS {DATABASE}",
        f"CREATE DATABASE {DATABASE} CHARACTER SET utf8mb4",
        "DROP USER IF EXISTS 'schemaops_it'@'%', 'schemaops_it_ro'@'%'",
        f"CREATE USER 'schemaops_it'@'%' IDENTIFIED BY '{PASSWORD}'",
        f"CREATE USER 'schemaops_it_ro'@'%' IDENTIFIED BY '{RO_PASSWORD}'",
        # The grants documented in the README, exactly.
        f"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, TRIGGER ON {DATABASE}.* TO 'schemaops_it'@'%'",
        "GRANT PROCESS, REPLICATION SLAVE, REPLICATION MASTER ADMIN, SLAVE MONITOR ON *.* TO 'schemaops_it'@'%'",
    ):
        sql(connection, statement)
    yield connection
    sql(connection, f"DROP DATABASE IF EXISTS {DATABASE}")
    sql(connection, "DROP USER IF EXISTS 'schemaops_it'@'%', 'schemaops_it_ro'@'%'")
    connection.close()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: object) -> Repo:
    repo = Repo(tmp_path, cluster="itc", schema=DATABASE)
    repo.add("dev", DUMP)
    for name, value in {
        "HOST": HOST,
        "PORT": str(PORT),
        "USER": "schemaops_it",
        "PASSWORD": PASSWORD,
        "TLS_MODE": "disabled",
        "RO_USER": "schemaops_it_ro",
        "RO_PASSWORD": RO_PASSWORD,
    }.items():
        monkeypatch.setenv(f"SCHEMAOPS_ITC_DEV_{name}", value)
    return repo


def target(read_only: bool = False) -> MariaDBTarget:
    config = ConnectionConfig.from_env("itc", "dev", read_only=read_only)
    return MariaDBTarget(config, DATABASE, OnlineChangeSettings())


def columns(root: object, table: str) -> set[str]:
    rows = sql(root, f"SELECT column_name AS name FROM information_schema.columns WHERE table_schema = '{DATABASE}' AND table_name = '{table}'")
    return {row["name"] for row in rows}


def test_full_cycle_with_real_pt_online_schema_change(repo: Repo, root: object):
    first = target()
    steps = deploy(repo.settings, repo.schema, "dev", first)
    first.close()
    assert [step.method for step in steps] == ["direct"]  # empty schema: the initial dump is executed
    assert columns(root, "users") == {"id", "email"} and columns(root, "orders") == {"id"}
    sql(root, f"INSERT INTO {DATABASE}.users (email) VALUES ('a@example.com'), ('b@example.com'), ('c@example.com')")

    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    repo.add("dev", "2026-11-21_create_index.sql", "CREATE INDEX idx_users_display_name ON users (display_name);\n")
    repo.add("dev", "2026-11-22_create_audit.sql", "CREATE TABLE audit (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY) COMMENT='100% audited';\n")
    second = target()
    steps = deploy(repo.settings, repo.schema, "dev", second)
    assert [step.method for step in steps] == ["online", "online", "direct"]
    assert "display_name" in columns(root, "users") and columns(root, "audit") == {"id"}
    assert sql(root, f"SELECT COUNT(*) AS total FROM {DATABASE}.users")[0]["total"] == 3  # rows survived the copy
    indexes = {row["Key_name"] for row in sql(root, f"SHOW INDEX FROM {DATABASE}.users")}
    assert "idx_users_display_name" in indexes
    leftovers = sql(root, f"SELECT table_name AS name FROM information_schema.tables WHERE table_schema = '{DATABASE}' AND table_name LIKE '\\_%\\_new'")
    assert leftovers == [] and sql(root, f"SELECT COUNT(*) AS total FROM information_schema.triggers WHERE trigger_schema = '{DATABASE}'")[0]["total"] == 0

    history = second.history()
    assert [row.status for row in history.values()] == ["success"] * 4
    # The server decides: adding a column is instant, adding an index needs the online copy.
    assert history[ADD_COLUMN].method == "instant" and history[ADD_COLUMN].seconds is not None
    assert history["2026-11-21_create_index.sql"].method == "pt-osc"
    assert deploy(repo.settings, repo.schema, "dev", second) == []  # nothing pending: a rerun is a no-op
    second.close()


def test_existing_schema_is_baselined_not_recreated(repo: Repo, root: object):
    sql(root, f"CREATE TABLE {DATABASE}.legacy (id BIGINT UNSIGNED PRIMARY KEY)")
    live = target()
    (step,) = deploy(repo.settings, repo.schema, "dev", live)
    assert step.method == "baseline" and live.history()[DUMP].status == "baseline"
    assert columns(root, "users") == set()
    live.close()


def test_live_table_and_server_facts(repo: Repo, root: object):
    live = target()
    deploy(repo.settings, repo.schema, "dev", live)
    sql(root, f"INSERT INTO {DATABASE}.users (email) VALUES ('a@example.com')")
    sql(root, f"ANALYZE TABLE {DATABASE}.users")
    sql(root, f"CREATE TRIGGER {DATABASE}.trg BEFORE INSERT ON {DATABASE}.users FOR EACH ROW SET NEW.email = LOWER(NEW.email)")
    sql(root, f"CREATE TABLE {DATABASE}.child (id BIGINT UNSIGNED PRIMARY KEY, user_id BIGINT UNSIGNED, CONSTRAINT fk_u FOREIGN KEY (user_id) REFERENCES {DATABASE}.users (id))")
    facts = live.inspect("users")
    assert facts.exists and facts.rows >= 1 and facts.size_bytes > 0
    assert (facts.triggers, facts.referenced_by) == (1, 1)
    assert not live.inspect("no_such_table").exists

    blocker = admin()
    sql(blocker, "START TRANSACTION")
    sql(blocker, f"SELECT * FROM {DATABASE}.users")
    time.sleep(2)
    server = live.server_facts()
    assert server.oldest_transaction_seconds is not None and server.oldest_transaction_seconds >= 1
    assert server.free_bytes is None or server.free_bytes > 0
    blocker.close()

    # Triggers and child foreign keys only matter to the table copy: instant DDL still works...
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    deploy(repo.settings, repo.schema, "dev", live)
    assert "display_name" in columns(root, "users") and live.history()[ADD_COLUMN].method == "instant"

    # ...but a change that needs the copy is refused, and nothing is altered.
    repo.add("dev", "2026-11-21_create_index.sql", "CREATE INDEX idx_users_display_name ON users (display_name);\n")
    with pytest.raises(DeployError, match="cannot run as instant DDL") as error:
        deploy(repo.settings, repo.schema, "dev", live)
    assert "trigger" in str(error.value) and "foreign key" in str(error.value)
    assert "idx_users_display_name" not in {row["Key_name"] for row in sql(root, f"SHOW INDEX FROM {DATABASE}.users")}
    live.close()


def test_pt_osc_is_used_for_everything_when_instant_ddl_is_off(repo: Repo, root: object):
    (repo.root / "schemaops.yaml").write_text("online-schema-change:\n  prefer-instant: false\n")
    live = target()
    deploy(repo.settings, repo.schema, "dev", live)
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    deploy(repo.settings, repo.schema, "dev", live)
    assert "display_name" in columns(root, "users") and live.history()[ADD_COLUMN].method == "pt-osc"
    live.close()


def test_blocked_instant_ddl_gives_up_after_five_seconds_without_copying(repo: Repo, root: object):
    live = target()
    deploy(repo.settings, repo.schema, "dev", live)
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    blocker = admin()
    sql(blocker, "START TRANSACTION")
    sql(blocker, f"SELECT * FROM {DATABASE}.users")

    started = time.monotonic()
    with pytest.raises(DeployError, match="(?i)lock wait timeout"):
        deploy(repo.settings, repo.schema, "dev", live)
    assert 4 <= time.monotonic() - started <= 12
    assert "display_name" not in columns(root, "users") and live.history()[ADD_COLUMN].status == "failed"
    blocker.close()
    deploy(repo.settings, repo.schema, "dev", live, retry_failed=True)
    assert live.history()[ADD_COLUMN].method == "instant"
    live.close()


def test_blocked_metadata_lock_gives_up_after_five_seconds(repo: Repo, root: object):
    live = target()
    deploy(repo.settings, repo.schema, "dev", live)
    repo.add("dev", "2026-11-20_rename_orders.sql", "RENAME TABLE orders TO orders_old;\n")
    blocker = admin()
    sql(blocker, "START TRANSACTION")
    sql(blocker, f"SELECT * FROM {DATABASE}.orders")  # holds a metadata lock on `orders`

    started = time.monotonic()
    with pytest.raises(DeployError, match="(?i)lock wait timeout"):
        deploy(repo.settings, repo.schema, "dev", live)
    assert 4 <= time.monotonic() - started <= 12
    assert live.history()["2026-11-20_rename_orders.sql"].status == "failed"
    assert columns(root, "orders") == {"id"}  # untouched

    blocker.close()
    deploy(repo.settings, repo.schema, "dev", live, retry_failed=True)
    assert columns(root, "orders_old") == {"id"}
    live.close()


def test_only_one_deployment_per_schema_at_a_time(repo: Repo):
    first, second = target(), target()
    first.lock()
    with pytest.raises(DeployError, match="Another deployment"):
        second.lock()
    first.close()
    second.lock()
    second.close()


def test_read_only_account_reads_history_and_nothing_else(repo: Repo, root: object):
    live = target()
    deploy(repo.settings, repo.schema, "dev", live)
    live.close()
    sql(root, f"GRANT SELECT ON {DATABASE}._schemaops_history TO 'schemaops_it_ro'@'%'")
    reader = target(read_only=True)
    assert reader.history()[DUMP].status == "success"
    with pytest.raises(DeployError):
        reader.execute("CREATE TABLE sneaky (id BIGINT UNSIGNED PRIMARY KEY)")
    with pytest.raises(DeployError):
        reader._query(f"SELECT * FROM {DATABASE}.users")
    reader.close()


def test_command_line_plan_deploy_and_status(repo: Repo, capsys: pytest.CaptureFixture[str]):
    base = ["--root", str(repo.root)]
    scope = ["--env", "dev", "--cluster", "itc", "--schema", DATABASE]
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    assert cli.main([*base, "plan", *scope]) == 0
    assert "would apply 2 migration(s)" in capsys.readouterr().out
    assert cli.main([*base, "deploy", *scope]) == 0
    assert "applied 2 migration(s)" in capsys.readouterr().out
    assert cli.main([*base, "status", *scope]) == 0
    output = capsys.readouterr().out
    assert f"success   {ADD_COLUMN}" in output and PASSWORD not in output
    assert cli.main([*base, "plan", *scope]) == 0
    assert "would apply 0 migration(s)" in capsys.readouterr().out
