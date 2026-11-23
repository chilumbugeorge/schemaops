import subprocess
from pathlib import Path

import pytest

from schemaops import deploy as deploy_module
from schemaops.db import ConnectionConfig
from schemaops.deploy import HistoryRow, MariaDBTarget, ServerFacts, TableFacts, deploy, mark_applied
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
        self.known = {"users", "orders"} if tables else set()
        self.facts: dict[str, TableFacts] = {}
        self.server = ServerFacts(oldest_transaction_seconds=0, free_bytes=None)
        self.instant = False  # whether the "server" can apply ALTERs as instant DDL

    def inspect(self, table: str) -> TableFacts:
        return self.facts.get(table, TableFacts(exists=table in self.known, rows=10, size_bytes=16384))

    def server_facts(self) -> ServerFacts:
        return self.server

    def lock(self) -> None:
        self.locked = True

    def history(self) -> dict[str, HistoryRow]:
        return dict(self.rows)

    def user_table_count(self) -> int:
        return self.tables

    def start(self, migration: Migration, method: str) -> None:
        self.rows[migration.name] = HistoryRow(migration.name, migration.sha256, "running", method)

    def finish(self, migration: Migration, status: str, error: str = "", method: str = "") -> None:
        row = self.rows[migration.name]
        self.rows[migration.name] = HistoryRow(row.migration, row.sha256, status, method or row.method)

    def instant_alter(self, table: str, alter: str) -> bool:
        if self.instant:
            self.calls.append(("instant", f"{table}: {alter}"))
        return self.instant

    def execute(self, sql: str) -> None:
        self.calls.append(("direct", sql.split("(")[0].strip()))
        if self.fail_on and self.fail_on in sql:
            raise DeployError("boom")
        self.tables += 1
        if sql.upper().startswith("CREATE TABLE"):
            self.known.add(sql.split()[2].lower())

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
    assert [(step.migration.name, step.method) for step in steps] == [(ADD_COLUMN, "online")]
    assert steps[0].detail.startswith("ALTER `users`, instant DDL, else pt-online-schema-change")
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


# --------------------------------------------------------------- pre-flight


def alter_users(repo: Repo, environment: str = "dev") -> FakeTarget:
    for env in ("dev", "uat", "prod")[: ("dev", "uat", "prod").index(environment) + 1]:
        repo.add(env, ADD_COLUMN, ADD_COLUMN_SQL)
    return FakeTarget(tables=2, rows=done(repo, environment, DUMP))


@pytest.mark.parametrize(
    ("facts", "server", "message"),
    [
        (TableFacts(exists=False), ServerFacts(0), "table `users` does not exist"),
        (TableFacts(True, 10, 1024), ServerFacts(oldest_transaction_seconds=900), "open for 900s"),
    ],
)
def test_preflight_failures_stop_before_anything_changes(repo: Repo, facts: TableFacts, server: ServerFacts, message: str):
    target = alter_users(repo)
    target.facts["users"], target.server = facts, server
    with pytest.raises(DeployError, match="pre-flight checks failed") as error:
        deploy(repo.settings, repo.schema, "dev", target)
    assert message in str(error.value)
    assert target.calls == [] and ADD_COLUMN not in target.rows  # nothing ran, nothing recorded: a rerun retries

    target.facts.clear()
    target.server = ServerFacts(0, free_bytes=10 * 1024**4)
    deploy(repo.settings, repo.schema, "dev", target)
    assert target.rows[ADD_COLUMN].status == "success"


def test_unknown_server_facts_do_not_block(repo: Repo):
    target = alter_users(repo)
    target.server = ServerFacts(oldest_transaction_seconds=None, free_bytes=None)
    deploy(repo.settings, repo.schema, "dev", target)
    assert target.calls == [("pt-osc", "users: ADD COLUMN display_name VARCHAR(64) NULL")]


def test_large_tables_are_escalated_to_the_dba_in_manual_environments_only(repo: Repo):
    big = TableFacts(exists=True, rows=25_000_000, size_bytes=1024**3)
    dev = alter_users(repo, "prod")
    dev.facts["users"] = big
    deploy(repo.settings, repo.schema, "dev", dev)
    assert dev.calls == [("pt-osc", "users: ADD COLUMN display_name VARCHAR(64) NULL")]

    prod = FakeTarget(tables=2, rows=done(repo, "prod", DUMP))
    prod.facts["users"] = big
    with pytest.raises(ManualActionRequired, match=r"about 25,000,000 rows \(limit 10,000,000\)"):
        deploy(repo.settings, repo.schema, "prod", prod, lower_history=done(repo, "uat", DUMP, ADD_COLUMN))
    assert prod.calls == []

    prod.facts["users"] = TableFacts(exists=True, rows=10, size_bytes=11 * 1024**3)
    (step,) = deploy(repo.settings, repo.schema, "prod", prod, lower_history=done(repo, "uat", DUMP, ADD_COLUMN), dry_run=True)
    assert step.method == "manual" and "11.0 GB (limit 10.0 GB)" in step.detail

    (repo.root / "schemaops.yaml").write_text("governance:\n  auto-protect: {max-rows: 0, max-size-gb: 0}\n")
    deploy(repo.settings, repo.schema, "prod", prod, lower_history=done(repo, "uat", DUMP, ADD_COLUMN))
    assert prod.rows[ADD_COLUMN].status == "success"


def test_plan_shows_table_size_and_problems_and_knows_tables_created_earlier(repo: Repo):
    repo.add("dev", "2026-11-20_create_audit.sql", "CREATE TABLE audit (id BIGINT UNSIGNED PRIMARY KEY);\n")
    repo.add("dev", "2026-11-21_add_audit_note.sql", "ALTER TABLE audit ADD COLUMN note VARCHAR(64) NULL;\n")
    repo.add("dev", "2026-11-22_add_users_note.sql", "ALTER TABLE users ADD COLUMN note VARCHAR(64) NULL;\n")
    target = FakeTarget(tables=2, rows=done(repo, "dev", DUMP))
    target.facts["users"] = TableFacts(True, rows=1_234_567, size_bytes=3 * 1024**2, triggers=1)
    create, alter_new, alter_users_step = deploy(repo.settings, repo.schema, "dev", target, dry_run=True)
    assert create.problems == () and alter_new.problems == ()
    assert "~1,234,567 rows, 3.0 MB" in alter_users_step.detail
    assert alter_users_step.problems == () and "trigger" in alter_users_step.copy_problems[0]

    target.known.add("audit")
    (create, *_rest) = deploy(repo.settings, repo.schema, "dev", target, dry_run=True)
    assert create.problems == ("table `audit` already exists",)


# -------------------------------------------------------------- instant DDL

COPY_BLOCKERS = [
    (TableFacts(True, 10, 1024, triggers=2), ServerFacts(0), "has 2 trigger"),
    (TableFacts(True, 10, 1024, referenced_by=1), ServerFacts(0), "referenced by 1 foreign key"),
    (TableFacts(True, 10, 4 * 1024**3), ServerFacts(0, free_bytes=5 * 1024**3), "not enough free disk space"),
]


def test_instant_ddl_is_tried_first_and_recorded(repo: Repo):
    target = alter_users(repo)
    target.instant = True
    deploy(repo.settings, repo.schema, "dev", target)
    assert target.calls == [("instant", "users: ADD COLUMN display_name VARCHAR(64) NULL")]  # no table copy
    assert (target.rows[ADD_COLUMN].status, target.rows[ADD_COLUMN].method) == ("success", "instant")


def test_falls_back_to_pt_osc_when_the_server_cannot_do_it_instantly(repo: Repo):
    target = alter_users(repo)
    deploy(repo.settings, repo.schema, "dev", target)
    assert target.calls == [("pt-osc", "users: ADD COLUMN display_name VARCHAR(64) NULL")]
    assert target.rows[ADD_COLUMN].method == "pt-osc"


def test_instant_ddl_can_be_switched_off(repo: Repo):
    (repo.root / "schemaops.yaml").write_text("online-schema-change:\n  prefer-instant: false\n")
    target = alter_users(repo)
    target.instant = True
    (step,) = deploy(repo.settings, repo.schema, "dev", target, dry_run=True)
    assert step.detail.startswith("ALTER `users`, pt-online-schema-change")
    deploy(repo.settings, repo.schema, "dev", target)
    assert target.calls == [("pt-osc", "users: ADD COLUMN display_name VARCHAR(64) NULL")]


@pytest.mark.parametrize(("facts", "server", "message"), COPY_BLOCKERS)
def test_copy_only_problems_do_not_block_instant_ddl_but_do_block_the_fallback(
    repo: Repo, facts: TableFacts, server: ServerFacts, message: str
):
    target = alter_users(repo)
    target.facts["users"], target.server = facts, server
    with pytest.raises(DeployError, match="cannot run as instant DDL") as error:
        deploy(repo.settings, repo.schema, "dev", target)
    assert message in str(error.value) and target.calls == []
    assert target.rows[ADD_COLUMN].status == "failed"

    target.instant = True
    deploy(repo.settings, repo.schema, "dev", target, retry_failed=True)
    assert target.rows[ADD_COLUMN].method == "instant"


@pytest.mark.parametrize(("facts", "server", "message"), COPY_BLOCKERS)
def test_copy_problems_block_up_front_when_instant_ddl_is_off(repo: Repo, facts: TableFacts, server: ServerFacts, message: str):
    (repo.root / "schemaops.yaml").write_text("online-schema-change:\n  prefer-instant: false\n")
    target = alter_users(repo)
    target.facts["users"], target.server = facts, server
    with pytest.raises(DeployError, match="pre-flight checks failed") as error:
        deploy(repo.settings, repo.schema, "dev", target)
    assert message in str(error.value) and ADD_COLUMN not in target.rows


class InstantConnection(FakeConnection):
    def __init__(self, errno: int | None) -> None:
        super().__init__()
        self.errno = errno

    def execute(self, sql: str, params: object = None) -> None:
        import pymysql

        super().execute(sql, params)
        if "ALGORITHM=INSTANT" in sql and self.errno:
            raise pymysql.err.OperationalError(self.errno, "ALGORITHM=INSTANT is not supported. Reason: ADD INDEX")


def test_instant_alter_statement_and_error_handling():
    connection = InstantConnection(None)
    assert real_target(None, connection).instant_alter("Users", "ADD COLUMN a BIGINT NULL") is True
    assert connection.statements == [
        "SET SESSION lock_wait_timeout = 5",
        "ALTER TABLE `Users` ADD COLUMN a BIGINT NULL, ALGORITHM=INSTANT",
    ]
    for errno in (1800, 1845, 1846):
        assert real_target(None, InstantConnection(errno)).instant_alter("users", "ADD INDEX i (a)") is False
    with pytest.raises(DeployError):  # for example a lock wait timeout: fail, never fall back to a copy
        real_target(None, InstantConnection(1205)).instant_alter("users", "ADD COLUMN a BIGINT NULL")
