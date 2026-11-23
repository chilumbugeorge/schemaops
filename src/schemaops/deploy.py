"""Apply pending migrations to one schema in one environment.

Every schema change runs without blocking traffic:

* ALTER TABLE and index changes go through pt-online-schema-change, always.
* CREATE / DROP / RENAME TABLE run directly with a short lock_wait_timeout, so a
  blocked metadata lock gives up instead of queueing every other query behind it.
* Changes to protected (hot or large) tables in a manual environment, and new
  UNIQUE indexes, are never run automatically: deployment stops for the DBA team.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from schemaops import log, sqlplan
from schemaops.db import ConnectionConfig, connect
from schemaops.errors import DeployError, GovernanceError, ManualActionRequired
from schemaops.governance import check_schema
from schemaops.repo import (
    Migration,
    OnlineChangeSettings,
    Schema,
    Settings,
    load_protected_tables,
    previous_environment,
)

logger = log.get("deploy")

HISTORY_TABLE = "_schemaops_history"
DONE = ("success", "baseline", "manual")
HISTORY_DDL = f"""
CREATE TABLE IF NOT EXISTS `{HISTORY_TABLE}` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  migration VARCHAR(200) NOT NULL,
  sha256 CHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  method VARCHAR(16) NOT NULL,
  executed_by VARCHAR(128) NOT NULL,
  pipeline_url VARCHAR(255) NOT NULL DEFAULT '',
  error_message VARCHAR(2000) NOT NULL DEFAULT '',
  started_at DATETIME NOT NULL,
  finished_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_migration (migration)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class HistoryRow:
    migration: str
    sha256: str
    status: str
    method: str = ""


class Target(Protocol):
    """One schema in one environment. `MariaDBTarget` is the real implementation."""

    def lock(self) -> None: ...
    def history(self) -> dict[str, HistoryRow]: ...
    def user_table_count(self) -> int: ...
    def start(self, migration: Migration, method: str) -> None: ...
    def finish(self, migration: Migration, status: str, error: str = "") -> None: ...
    def execute(self, sql: str) -> None: ...
    def online_alter(self, table: str, alter: str) -> None: ...
    def close(self) -> None: ...


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)


class MariaDBTarget:
    def __init__(
        self,
        config: ConnectionConfig,
        schema: str,
        online: OnlineChangeSettings,
        runner: Runner = _run,
        connection: Any = None,
    ) -> None:
        self.config = config
        self.schema = schema
        self.online = online
        self.runner = runner
        self.connection = connection if connection is not None else connect(config, schema)

    def _query(self, sql: str, params: Sequence[object] = ()) -> list[dict[str, Any]]:
        import pymysql

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params or None)  # None: a literal % in DDL is not a placeholder
                return list(cursor.fetchall() or [])
        except pymysql.MySQLError as exc:
            raise DeployError(f"{self.schema} ({self.config.environment.upper()}): {log.redact(str(exc))}") from exc

    def lock(self) -> None:
        rows = self._query("SELECT GET_LOCK(%s, 0) AS acquired", (f"schemaops.{self.schema}",))
        if not rows or rows[0]["acquired"] != 1:
            raise DeployError(f"Another deployment of {self.schema} is running in {self.config.environment.upper()}.")
        self._query(HISTORY_DDL)

    def history(self) -> dict[str, HistoryRow]:
        exists = self._query(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (self.schema, HISTORY_TABLE),
        )
        if not exists:
            return {}
        rows = self._query(f"SELECT migration, sha256, status, method FROM `{HISTORY_TABLE}` ORDER BY id")
        return {row["migration"]: HistoryRow(row["migration"], row["sha256"], row["status"], row["method"]) for row in rows}

    def user_table_count(self) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS total FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' AND table_name <> %s",
            (self.schema, HISTORY_TABLE),
        )
        return int(rows[0]["total"])

    def start(self, migration: Migration, method: str) -> None:
        actor = os.environ.get("GITLAB_USER_LOGIN") or getpass.getuser()
        self._query(
            f"INSERT INTO `{HISTORY_TABLE}` (migration, sha256, status, method, executed_by, pipeline_url, started_at) "
            "VALUES (%s, %s, 'running', %s, %s, %s, UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE sha256 = VALUES(sha256), status = 'running', method = VALUES(method), "
            "executed_by = VALUES(executed_by), pipeline_url = VALUES(pipeline_url), error_message = '', "
            "started_at = UTC_TIMESTAMP(), finished_at = NULL",
            (migration.name, migration.sha256, method, actor[:128], os.environ.get("CI_PIPELINE_URL", "")[:255]),
        )

    def finish(self, migration: Migration, status: str, error: str = "") -> None:
        self._query(
            f"UPDATE `{HISTORY_TABLE}` SET status = %s, error_message = %s, finished_at = UTC_TIMESTAMP() "
            "WHERE migration = %s",
            (status, log.redact(error)[:2000], migration.name),
        )

    def execute(self, sql: str) -> None:
        self._query(f"SET SESSION lock_wait_timeout = {int(self.online.lock_wait_timeout)}")
        self._query(sql)

    def online_alter(self, table: str, alter: str) -> None:
        if shutil.which("pt-online-schema-change") is None and self.runner is _run:
            raise DeployError("pt-online-schema-change is not installed (package: percona-toolkit).")
        online = self.online
        with tempfile.TemporaryDirectory(prefix="schemaops-") as directory:
            options = Path(directory) / "client.cnf"
            self.config.client_options_file(options)
            base = [
                "pt-online-schema-change",
                "--alter", alter,
                "--set-vars", f"lock_wait_timeout={online.lock_wait_timeout}",
                "--max-lag", str(online.max_lag),
                "--max-load", online.max_load,
                "--critical-load", online.critical_load,
                "--chunk-time", str(online.chunk_time),
                "--tries", "create_triggers:10:1,drop_triggers:10:1,swap_tables:10:1,update_foreign_keys:10:1",
                "--no-version-check",
                "--progress", "time,30",
            ]  # fmt: skip
            dsn = f"h={self.config.host},P={self.config.port},D={self.schema},t={table},F={options}"
            for mode in ("--dry-run", "--execute"):
                completed = self.runner([*base, mode, dsn])
                output = log.redact((completed.stdout or "") + (completed.stderr or "")).strip()
                logger.info("pt-online-schema-change finished", extra={"mode": mode, "table": table, "exit_code": completed.returncode})
                if completed.returncode != 0:
                    raise DeployError(f"pt-online-schema-change {mode} failed for `{table}`:\n{output[-1500:]}")

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001 - closing must never mask the real error
            pass


@dataclass(frozen=True)
class Step:
    migration: Migration
    method: str  # direct | pt-osc | baseline | manual
    detail: str = ""


def _manual_reasons(
    settings: Settings, schema: Schema, environment: str, operations: Sequence[sqlplan.Operation]
) -> list[str]:
    protected = load_protected_tables(settings)
    reasons: list[str] = []
    for operation in operations:
        for table in operation.tables:
            reason = protected.get((schema.cluster, schema.name, table))
            if reason is not None and environment in settings.manual_environments:
                reasons.append(f"`{table}` is a protected table" + (f" ({reason})" if reason else ""))
        if operation.adds_unique_index:
            reasons.append(f"it adds a UNIQUE index to `{operation.table}`")
    return reasons


def pending_steps(
    settings: Settings,
    schema: Schema,
    environment: str,
    history: dict[str, HistoryRow],
    *,
    schema_is_empty: bool,
    retry_failed: bool = False,
) -> list[Step]:
    """Work out what still has to run, refusing if history and Git disagree."""
    migrations = schema.migrations(environment)
    where = f"{schema.label} ({environment.upper()})"
    names = [migration.name for migration in migrations]

    unknown = [name for name in history if name not in names]
    if unknown:
        raise GovernanceError(f"{where}: applied but no longer in Git: {', '.join(unknown)}. History must never be rewritten.")

    steps: list[Step] = []
    seen_pending = False
    for migration in migrations:
        row = history.get(migration.name)
        if row and row.status in DONE:
            if seen_pending:
                raise GovernanceError(f"{where}: `{migration.name}` is applied but an earlier migration is not. Migrations must apply in order.")
            if row.sha256 != migration.sha256:
                raise GovernanceError(f"{where}: `{migration.name}` was changed after it was applied (checksum mismatch).")
            continue
        if row and not retry_failed:
            raise DeployError(
                f"{where}: `{migration.name}` is in state `{row.status}` from an earlier run. A DBA must check "
                "the table, then re-run with --retry-failed or record it with `schemaops mark-applied`."
            )
        seen_pending = True
        operations = sqlplan.parse_operations(migration.read())
        if migration.is_initial_dump:
            steps.append(Step(migration, "direct" if schema_is_empty else "baseline", f"{len(operations)} table(s)"))
            continue
        if len(operations) != 1 or operations[0].kind == sqlplan.UNSUPPORTED:
            raise DeployError(f"{where}: `{migration.name}` is not a single supported schema change. Run `schemaops lint`.")
        reasons = _manual_reasons(settings, schema, environment, operations)
        if reasons:
            steps.append(Step(migration, "manual", "; ".join(reasons)))
        elif operations[0].kind == sqlplan.ALTER_TABLE:
            steps.append(Step(migration, "pt-osc", f"ALTER `{operations[0].table}`"))
        else:
            steps.append(Step(migration, "direct", operations[0].kind.replace("_", " ")))
    return steps


def verify_previous_environment(
    schema: Schema, environment: str, names: Sequence[str], lower_history: dict[str, HistoryRow], shas: dict[str, str]
) -> None:
    lower = previous_environment(environment)
    assert lower is not None
    for name in names:
        row = lower_history.get(name)
        if row is None or row.status not in DONE:
            state = row.status if row else "not deployed"
            raise GovernanceError(
                f"{schema.label}: `{name}` cannot go to {environment.upper()}: its {lower.upper()} deployment is `{state}`."
            )
        if row.sha256 != shas[name]:
            raise GovernanceError(f"{schema.label}: `{name}` differs from the version deployed to {lower.upper()}.")


def deploy(
    settings: Settings,
    schema: Schema,
    environment: str,
    target: Target,
    *,
    lower_history: dict[str, HistoryRow] | None = None,
    dry_run: bool = False,
    retry_failed: bool = False,
) -> list[Step]:
    """Apply (or, with dry_run, only report) the pending migrations. Returns the steps."""
    issues = check_schema(settings, schema)
    if issues:
        raise GovernanceError(f"{schema.label} fails governance checks:\n" + "\n".join(issue.format() for issue in issues))

    if not dry_run:
        target.lock()
    steps = pending_steps(
        settings,
        schema,
        environment,
        target.history(),
        schema_is_empty=target.user_table_count() == 0,
        retry_failed=retry_failed,
    )
    if steps and previous_environment(environment) and settings.verify_previous_deployment:
        if lower_history is None:
            raise GovernanceError(f"{schema.label}: cannot verify the {previous_environment(environment)} deployment.")
        verify_previous_environment(
            schema, environment, [s.migration.name for s in steps], lower_history, {s.migration.name: s.migration.sha256 for s in steps}
        )
    if dry_run:
        return steps

    for step in steps:
        migration = step.migration
        context = {"schema": schema.label, "environment": environment, "migration": migration.name, "method": step.method}
        if step.method == "manual":
            raise ManualActionRequired(
                f"{schema.label} ({environment.upper()}): `{migration.name}` must be applied manually by the DBA team "
                f"because {step.detail}. Afterwards record it with `schemaops mark-applied`."
            )
        logger.info("applying migration", extra=context)
        target.start(migration, step.method)
        try:
            if step.method == "baseline":
                pass  # the schema already exists; record the dump as its starting point
            else:
                for operation in sqlplan.parse_operations(migration.read()):
                    if operation.kind == sqlplan.ALTER_TABLE:
                        target.online_alter(operation.table, operation.alter)
                    else:
                        target.execute(operation.sql)
        except Exception as exc:
            target.finish(migration, "failed", str(exc))
            logger.error("migration failed", extra=context)
            raise
        target.finish(migration, "baseline" if step.method == "baseline" else "success")
        logger.info("migration applied", extra=context)
    return steps


def mark_applied(settings: Settings, schema: Schema, environment: str, target: Target, name: str) -> None:
    """Record a migration the DBA team applied by hand. It must be the next pending one."""
    target.lock()
    steps = pending_steps(
        settings, schema, environment, target.history(), schema_is_empty=False, retry_failed=True
    )
    if not steps or steps[0].migration.name != name:
        expected = steps[0].migration.name if steps else "nothing"
        raise GovernanceError(f"{schema.label} ({environment.upper()}): the next pending migration is {expected}, not `{name}`.")
    target.start(steps[0].migration, "manual")
    target.finish(steps[0].migration, "manual")
    logger.info("migration recorded as manually applied", extra={"schema": schema.label, "environment": environment, "migration": name})
