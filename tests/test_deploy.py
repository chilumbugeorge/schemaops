import subprocess
from pathlib import Path

import pytest

from schemaops import deploy as deploy_module
from schemaops.db import ConnectionConfig
from schemaops.deploy import HistoryRow, MariaDBTarget, deploy, mark_applied
from schemaops.errors import DeployError, GovernanceError, ManualActionRequired
from schemaops.repo import Migration, OnlineChangeSettings

from conftest import DUMP, Repo

ADD_COLUMN = "2026-11-20_add_display_name.sql"
ADD_COLUMN_SQL = "ALTER TABLE users ADD COLUMN display_name VARCHAR(64) NULL;\n"


class FakeTarget:
    """In-memory stand-in for one schema in one environment."""

    def __init__(self, tables: int = 0, rows: dict[str, HistoryRow] | None = None, fail_on: str = "") -> None:
        self.tables = tables
        self.rows: dict[str, HistoryRow] = dict(rows or {})
        self.fail_on = fail_on
        self.calls: list[tuple[str, str]] = []
        self.locked = False

    def lock(self) -> None:
        self.locked = True

    def history(self) -> dict[str, HistoryRow]:
        return dict(self.rows)

    def user_table_count(self) -> int:
        return self.tables

    def start(self, migration: Migration, method: str) -> None:
        self.rows[migration.name] = HistoryRow(migration.name, migration.sha256, "running", method)

    def finish(self, migration: Migration, status: str, error: str = "") -> None:
        row = self.rows[migration.name]
        self.rows[migration.name] = HistoryRow(row.migration, row.sha256, status, row.method)

    def execute(self, sql: str) -> None:
        self.calls.append(("direct", sql.split("(")[0].strip()))
        if self.fail_on and self.fail_on in sql:
            raise DeployError("boom")
        self.tables += 1

    def online_alter(self, table: str, alter: str) -> None:
        self.calls.append(("pt-osc", f"{table}: {alter}"))
        if self.fail_on and self.fail_on in alter:
            raise DeployError("boom")

    def close(self) -> None:
        pass


def done(repo: Repo, environment: str, *names: str, status: str = "success") -> dict[str, HistoryRow]:
    return {
        name: HistoryRow(name, Migration(name, repo.schema.migrations_dir(environment) / name).sha256, status)
        for name in names
    }


def test_initial_dump_is_created_on_an_empty_schema_and_baselined_on_an_existing_one(repo: Repo):
    empty = FakeTarget(tables=0)
    steps = deploy(repo.settings, repo.schema, "dev", empty)
    assert [step.method for step in steps] == ["direct"]
    assert empty.calls == [("direct", "CREATE TABLE users"), ("direct", "CREATE TABLE orders")]
    assert empty.rows[DUMP].status == "success" and empty.locked

    existing = FakeTarget(tables=12)
    deploy(repo.settings, repo.schema, "dev", existing)
    assert existing.calls == [] and existing.rows[DUMP].status == "baseline"


def test_alter_always_goes_through_pt_osc_and_reruns_are_no_ops(repo: Repo):
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    repo.add("dev", "2026-11-21_create_audit.sql", "CREATE TABLE audit (id BIGINT UNSIGNED PRIMARY KEY);\n")
    target = FakeTarget(tables=2, rows=done(repo, "dev", DUMP))
    deploy(repo.settings, repo.schema, "dev", target)
    assert target.calls == [
        ("pt-osc", "users: ADD COLUMN display_name VARCHAR(64) NULL"),
        ("direct", "CREATE TABLE audit"),
    ]
    target.calls.clear()
    assert deploy(repo.settings, repo.schema, "dev", target) == []
    assert target.calls == []


def test_dry_run_reports_without_changing_anything(repo: Repo):
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    target = FakeTarget(tables=2, rows=done(repo, "dev", DUMP))
    steps = deploy(repo.settings, repo.schema, "dev", target, dry_run=True)
    assert [(step.migration.name, step.method) for step in steps] == [(ADD_COLUMN, "pt-osc")]
    assert target.calls == [] and not target.locked and ADD_COLUMN not in target.rows


def test_promotion_requires_a_successful_deployment_in_the_previous_environment(repo: Repo):
    for environment in ("dev", "uat"):
        repo.add(environment, ADD_COLUMN, ADD_COLUMN_SQL)
    target = FakeTarget(tables=2, rows=done(repo, "uat", DUMP))

    with pytest.raises(GovernanceError, match="cannot verify the dev deployment"):
        deploy(repo.settings, repo.schema, "uat", target)
    with pytest.raises(GovernanceError, match="DEV deployment is `not deployed`"):
        deploy(repo.settings, repo.schema, "uat", target, lower_history=done(repo, "dev", DUMP))
    with pytest.raises(GovernanceError, match="DEV deployment is `failed`"):
        failed = {**done(repo, "dev", DUMP), **done(repo, "dev", ADD_COLUMN, status="failed")}
        deploy(repo.settings, repo.schema, "uat", target, lower_history=failed)
    assert target.calls == []

    deploy(repo.settings, repo.schema, "uat", target, lower_history=done(repo, "dev", DUMP, ADD_COLUMN))
    assert target.rows[ADD_COLUMN].status == "success"


def test_protected_tables_are_manual_in_prod_but_online_in_lower_environments(repo: Repo):
    name = "2026-11-20_add_orders_note.sql"
    for environment in ("dev", "uat", "prod"):
        repo.add(environment, name, "ALTER TABLE orders ADD COLUMN note VARCHAR(64) NULL;\n")
    dev = FakeTarget(tables=2, rows=done(repo, "dev", DUMP))
    deploy(repo.settings, repo.schema, "dev", dev)
    assert dev.calls == [("pt-osc", "orders: ADD COLUMN note VARCHAR(64) NULL")]

    prod = FakeTarget(tables=2, rows=done(repo, "prod", DUMP))
    lower = done(repo, "uat", DUMP, name)
    with pytest.raises(ManualActionRequired, match="protected table") as error:
        deploy(repo.settings, repo.schema, "prod", prod, lower_history=lower)
    assert error.value.exit_code == 3 and prod.calls == [] and name not in prod.rows

    mark_applied(repo.settings, repo.schema, "prod", prod, name)
    assert (prod.rows[name].status, prod.rows[name].method) == ("manual", "manual")
    assert deploy(repo.settings, repo.schema, "prod", prod, lower_history=lower) == []
    with pytest.raises(GovernanceError, match="next pending migration is nothing"):
        mark_applied(repo.settings, repo.schema, "prod", prod, name)


def test_unique_index_on_an_existing_table_is_never_applied_automatically(repo: Repo):
    repo.add("dev", "2026-11-20_unique_email.sql", "ALTER TABLE users ADD UNIQUE KEY uq_email (email);\n")
    target = FakeTarget(tables=2, rows=done(repo, "dev", DUMP))
    with pytest.raises(ManualActionRequired, match="UNIQUE index"):
        deploy(repo.settings, repo.schema, "dev", target)
    assert target.calls == []


def test_failure_is_recorded_and_blocks_until_a_dba_decides(repo: Repo):
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    repo.add("dev", "2026-11-21_create_audit.sql", "CREATE TABLE audit (id BIGINT UNSIGNED PRIMARY KEY);\n")
    target = FakeTarget(tables=2, rows=done(repo, "dev", DUMP), fail_on="display_name")
    with pytest.raises(DeployError, match="boom"):
        deploy(repo.settings, repo.schema, "dev", target)
    assert target.rows[ADD_COLUMN].status == "failed" and len(target.calls) == 1

    target.fail_on = ""
    with pytest.raises(DeployError, match="state `failed`"):
        deploy(repo.settings, repo.schema, "dev", target)
    deploy(repo.settings, repo.schema, "dev", target, retry_failed=True)
    assert [row.status for row in target.rows.values()] == ["success", "success", "success"]


def test_history_and_git_must_agree(repo: Repo):
    repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    tampered = {**done(repo, "dev", DUMP), ADD_COLUMN: HistoryRow(ADD_COLUMN, "0" * 64, "success")}
    with pytest.raises(GovernanceError, match="checksum mismatch"):
        deploy(repo.settings, repo.schema, "dev", FakeTarget(tables=2, rows=tampered))

    with pytest.raises(GovernanceError, match="no longer in Git"):
        ghost = {**done(repo, "dev", DUMP), "2026-11-19_removed.sql": HistoryRow("2026-11-19_removed.sql", "a", "success")}
        deploy(repo.settings, repo.schema, "dev", FakeTarget(tables=2, rows=ghost))

    with pytest.raises(GovernanceError, match="earlier migration is not"):
        deploy(repo.settings, repo.schema, "dev", FakeTarget(tables=2, rows=done(repo, "dev", ADD_COLUMN)))


def test_governance_failures_stop_deployment_before_any_database_work(repo: Repo):
    (repo.schema.migrations_dir("dev") / "2026-11-20_unregistered.sql").write_text(ADD_COLUMN_SQL)
    target = FakeTarget()
    with pytest.raises(GovernanceError, match="Not registered"):
        deploy(repo.settings, repo.schema, "dev", target)
    assert not target.locked


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self):  # noqa: ANN201
        return self

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql if params is None else sql % params)

    def fetchall(self) -> list[dict[str, object]]:
        return []


def real_target(runner, connection=None) -> MariaDBTarget:  # noqa: ANN001
    config = ConnectionConfig("trades", "dev", "db-dev.internal", 3306, "schemaops_dev", "s3cr3t-pw", tls_mode="disabled")
    return MariaDBTarget(config, "accounts", OnlineChangeSettings(), runner=runner, connection=connection or FakeConnection())


def test_pt_osc_is_dry_run_first_with_a_five_second_lock_wait_and_no_password_on_the_command_line():
    commands: list[list[str]] = []
    seen_options: list[str] = []

    def runner(command):  # noqa: ANN001, ANN202
        commands.append(list(command))
        options = Path(command[-1].split("F=")[1])
        seen_options.append(options.read_text())
        return subprocess.CompletedProcess(command, 0, "ok", "")

    real_target(runner).online_alter("users", "ADD COLUMN a BIGINT NULL")
    assert [command[-2] for command in commands] == ["--dry-run", "--execute"]
    first = commands[0]
    assert first[0] == "pt-online-schema-change"
    assert first[first.index("--alter") + 1] == "ADD COLUMN a BIGINT NULL"
    assert first[first.index("--set-vars") + 1] == "lock_wait_timeout=5"
    assert first[-1].startswith("h=db-dev.internal,P=3306,D=accounts,t=users,F=")
    assert all("s3cr3t-pw" not in part for command in commands for part in command)
    assert 'password="s3cr3t-pw"' in seen_options[0]
    assert not Path(first[-1].split("F=")[1]).exists()  # removed afterwards


def test_pt_osc_failure_stops_before_execute_and_never_leaks_the_password():
    commands: list[list[str]] = []

    def runner(command):  # noqa: ANN001, ANN202
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 1, "", "Access denied (using password: s3cr3t-pw)")

    deploy_module.log.register_secret("s3cr3t-pw")
    with pytest.raises(DeployError) as error:
        real_target(runner).online_alter("users", "ADD COLUMN a BIGINT NULL")
    assert len(commands) == 1 and commands[0][-2] == "--dry-run"
    assert "s3cr3t-pw" not in str(error.value)


def test_direct_statements_set_a_short_metadata_lock_wait():
    connection = FakeConnection()
    ddl = "CREATE TABLE t (id BIGINT UNSIGNED PRIMARY KEY) COMMENT='top 10% of users'"
    real_target(None, connection).execute(ddl)
    assert connection.statements == ["SET SESSION lock_wait_timeout = 5", ddl]
