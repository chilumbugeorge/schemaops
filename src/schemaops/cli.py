"""Command line interface: `schemaops <command>`."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from schemaops import __version__, governance, log, mrcheck
from schemaops.db import ConnectionConfig
from schemaops.deploy import (
    HistoryRow,
    MariaDBTarget,
    TableFacts,
    deploy,
    mark_applied,
    pending_steps,
    verify_previous_environment,
)
from schemaops.errors import ConfigError, GovernanceError, ManualActionRequired, SchemaOpsError
from schemaops.gitlab import MergeRequest
from schemaops.lint.rules import RULES
from schemaops.repo import (
    ENVIRONMENTS,
    INITIAL_DUMP_RE,
    MIGRATION_NAME_RE,
    NAME_RE,
    Schema,
    Settings,
    discover_schemas,
    find_schema,
    previous_environment,
)

logger = log.get("cli")


def _out(text: str = "") -> None:
    print(log.redact(text))


# ------------------------------------------------------------------ helpers


def _environment_yaml(schema: Schema, environment: str, migrations: Sequence[str]) -> str:
    lines = [
        f"# Migrations applied to {environment.upper()} for {schema.label}, oldest first.",
        "# Add new entries at the end only. Never edit or remove an existing entry.",
        f"cluster-name: {schema.cluster}",
        f"schema-name: {schema.name}",
        f"environment: {environment}",
    ]
    lines.append("migrations:" if migrations else "migrations: []")
    lines.extend(f"  - {name}" for name in migrations)
    return "\n".join(lines) + "\n"


def _register(schema: Schema, environment: str, name: str) -> None:
    current = schema.registered(environment)
    schema.environment_file(environment).write_text(
        _environment_yaml(schema, environment, [*current, name]), encoding="utf-8"
    )


def _select_schemas(settings: Settings, args: argparse.Namespace) -> list[Schema]:
    if args.cluster and args.schema:
        return [find_schema(settings, args.cluster, args.schema)]
    if args.cluster or args.schema:
        raise ConfigError("Give both --cluster and --schema, or use --all / --changed-since.")
    schemas = discover_schemas(settings)
    changed_since = getattr(args, "changed_since", None)
    if changed_since and not changed_since.strip("0"):
        return schemas  # GitLab sends all zeros when there is no previous commit
    if changed_since:
        changed = governance._git(settings, "diff", "--name-only", args.changed_since, "HEAD", "--").splitlines()
        wanted = (f"/environments/{args.env}.yaml", f"/migrations/{args.env}/")
        return [
            schema
            for schema in schemas
            if any(
                path.startswith(f"{settings.clusters_root}/{schema.cluster}/{schema.name}/")
                and any(marker in path for marker in wanted)
                for path in changed
            )
        ]
    if getattr(args, "all", False):
        return schemas
    raise ConfigError("Choose what to act on: --cluster and --schema, --all, or --changed-since REF.")


def _target(settings: Settings, schema: Schema, environment: str, *, read_only: bool = False) -> MariaDBTarget:
    config = ConnectionConfig.from_env(
        schema.cluster,
        environment,
        read_only=read_only,
        connect_timeout=settings.connect_timeout,
        statement_timeout=settings.statement_timeout,
    )
    logger.info("connecting", extra={"schema": schema.label, "environment": environment, **config.describe()})
    return MariaDBTarget(config, schema.name, settings.online_change)


def _lower_history(settings: Settings, schema: Schema, environment: str) -> dict[str, HistoryRow] | None:
    lower = previous_environment(environment)
    if lower is None or not settings.verify_previous_deployment:
        return None
    target = _target(settings, schema, lower, read_only=True)
    try:
        return target.history()
    finally:
        target.close()


def _table_facts(settings: Settings, schema: Schema, environment: str, table: str) -> TableFacts | None:
    """Size of a table in its target environment, if the read-only account can see it."""
    try:
        target = _target(settings, schema, environment, read_only=True)
    except SchemaOpsError:
        return None
    try:
        return target.inspect(table)
    except SchemaOpsError:
        return None
    finally:
        target.close()


# ----------------------------------------------------------------- commands


def cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    issues = governance.check_repository(settings)
    for issue in issues:
        _out(issue.format())
    _out(f"Checked {len(discover_schemas(settings))} schema(s): {len(issues)} governance issue(s).")
    return 1 if issues else 0


def cmd_lint(settings: Settings, args: argparse.Namespace) -> int:
    if args.paths:
        paths = [Path(path).resolve() for path in args.paths]
    else:
        paths = [
            migration.path
            for schema in discover_schemas(settings)
            for migration in schema.migrations("dev")
            if migration.path.is_file()
        ]
    found = [entry for path in paths for entry in mrcheck.lint_file(path, settings, bypass=args.ignore_linting)]
    for shown, finding, _blocking in found:
        _out(finding.format(shown))
    blocking = sum(entry[2] for entry in found)
    _out(f"Linted {len(paths)} file(s): {len(found)} finding(s), {blocking} blocking.")
    return 1 if blocking else 0


def cmd_rules(settings: Settings, args: argparse.Namespace) -> int:
    _out("| Rule | Level | Standard | Why | Instead |")
    _out("| --- | --- | --- | --- | --- |")
    for rule in RULES:
        _out(f"| `{rule.id}` | {rule.severity.value} | {rule.summary} | {rule.reason} | {rule.alternative} |")
    return 0


def cmd_check_mr(settings: Settings, args: argparse.Namespace) -> int:
    request = MergeRequest.from_env()
    base = args.base or (request.base_sha if request else "")
    if not base:
        raise ConfigError("No target commit: pass --base REF (for example --base origin/main).")
    labels = set(request.labels) if request else set()
    labels.update(args.label or [])
    report = mrcheck.run(
        settings,
        base,
        labels=labels,
        request=request,
        in_ci=bool(os.environ.get("CI")),
        lower_history=lambda *call: _lower_history(*call),
        inspect_table=lambda *call: _table_facts(*call),
    )
    _out(mrcheck.render_text(report))
    markdown = log.redact(mrcheck.render_markdown(report))
    if args.report:
        Path(args.report).write_text(markdown, encoding="utf-8")
    if args.comment:
        if request is None:
            logger.warning("--comment ignored: not running in a merge request pipeline")
        elif request.post_comment(markdown, mrcheck.MARKER):
            logger.info("plan posted to the merge request")
    return 0 if report.passed else 1


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _guard_pipeline(environment: str) -> None:
    """Defence in depth: real deployments only run from a protected branch pipeline."""
    if not os.environ.get("CI"):
        return
    if os.environ.get("CI_MERGE_REQUEST_IID"):
        raise GovernanceError("Deployments never run from merge request pipelines.")
    if os.environ.get("CI_COMMIT_REF_PROTECTED") != "true":
        raise GovernanceError(f"{environment.upper()} deployments only run from a protected branch.")


def cmd_deploy(settings: Settings, args: argparse.Namespace) -> int:
    dry_run = args.command == "plan" or args.dry_run
    if not dry_run:
        _guard_pipeline(args.env)
        if shutil.which("pt-online-schema-change") is None:
            raise ConfigError("pt-online-schema-change is not installed (package: percona-toolkit).")
    schemas = _select_schemas(settings, args)
    if not schemas:
        _out(f"No schema has changes for {args.env.upper()}.")
        return 0
    exit_code = 0
    for schema in schemas:
        target = _target(settings, schema, args.env)
        try:
            steps = deploy(
                settings,
                schema,
                args.env,
                target,
                lower_history=_lower_history(settings, schema, args.env),
                dry_run=dry_run,
                retry_failed=getattr(args, "retry_failed", False),
            )
            verb = "would apply" if dry_run else "applied"
            _out(f"{schema.label} ({args.env.upper()}): {verb} {len(steps)} migration(s).")
            for step in steps:
                _out(f"  {step.migration.name}  [{step.method}] {step.detail}")
                for problem in step.problems:
                    _out(f"      PRE-FLIGHT: {problem}")
        except ManualActionRequired as exc:
            _out(str(exc))
            exit_code = max(exit_code, exc.exit_code)
        finally:
            target.close()
    return exit_code


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    for schema in _select_schemas(settings, args):
        target = _target(settings, schema, args.env, read_only=args.read_only)
        try:
            history = target.history()
            empty = target.user_table_count() == 0
        finally:
            target.close()
        _out(f"{schema.label} ({args.env.upper()})")
        for migration in schema.migrations(args.env):
            row = history.get(migration.name)
            _out(f"  {row.status if row else 'pending':9} {migration.name}")
        try:
            pending_steps(settings, schema, args.env, history, schema_is_empty=empty, retry_failed=True)
        except SchemaOpsError as exc:
            _out(f"  PROBLEM: {exc}")
    return 0


def cmd_mark_applied(settings: Settings, args: argparse.Namespace) -> int:
    _guard_pipeline(args.env)
    schema = find_schema(settings, args.cluster, args.schema)
    lower = _lower_history(settings, schema, args.env)
    if lower is not None:
        path = schema.migrations_dir(args.env) / args.file
        verify_previous_environment(schema, args.env, [args.file], lower, {args.file: _sha(path)})
    target = _target(settings, schema, args.env)
    try:
        mark_applied(settings, schema, args.env, target, args.file)
    finally:
        target.close()
    _out(f"Recorded {args.file} as manually applied to {schema.label} ({args.env.upper()}).")
    return 0


def cmd_new_schema(settings: Settings, args: argparse.Namespace) -> int:
    for value in (args.cluster, args.schema):
        if not NAME_RE.match(value):
            raise ConfigError(f"`{value}` must be lower snake_case.")
    schema = Schema(args.cluster, args.schema, settings.clusters_dir / args.cluster / args.schema)
    if schema.main_file.exists():
        raise ConfigError(f"{schema.label} already exists.")
    dump = Path(args.initial_dump)
    if not dump.is_file():
        raise ConfigError(f"Initial dump {dump} not found. Create it with: mysqldump --no-data --skip-add-drop-table --skip-comments")
    schema.path.mkdir(parents=True)
    schema.main_file.write_text(
        f"# Identity and ownership of the {schema.label} schema. Created once.\n"
        f"cluster-name: {schema.cluster}\n"
        f"schema-name: {schema.name}\n"
        f"owner: {args.owner}\n"
        f"description: {args.description}\n"
        f"contact: \"{args.contact}\"\n",
        encoding="utf-8",
    )
    name = f"{date.today().isoformat()}_initial_dump.sql"
    (schema.path / "environments").mkdir()
    for environment in ENVIRONMENTS:
        schema.migrations_dir(environment).mkdir(parents=True)
        (schema.migrations_dir(environment) / ".gitkeep").touch()
        listed = [name] if environment == "dev" else []
        schema.environment_file(environment).write_text(_environment_yaml(schema, environment, listed), encoding="utf-8")
    shutil.copyfile(dump, schema.migrations_dir("dev") / name)
    (schema.migrations_dir("dev") / ".gitkeep").unlink()
    _out(f"Created {schema.path.relative_to(settings.root)} with {name} registered in DEV.")
    return 0


def cmd_new_migration(settings: Settings, args: argparse.Namespace) -> int:
    schema = find_schema(settings, args.cluster, args.schema)
    name = f"{date.today().isoformat()}_{args.name}.sql"
    if not MIGRATION_NAME_RE.match(name):
        raise ConfigError("--name must be a lower snake_case description, for example add_users_email.")
    path = schema.migrations_dir("dev") / name
    if path.exists():
        raise ConfigError(f"{path} already exists.")
    path.write_text("-- One schema change statement per file. See `schemaops rules`.\n", encoding="utf-8")
    _register(schema, "dev", name)
    _out(f"Created {path.relative_to(settings.root)} and registered it in environments/dev.yaml.")
    return 0


def cmd_promote(settings: Settings, args: argparse.Namespace) -> int:
    schema = find_schema(settings, args.cluster, args.schema)
    lower = previous_environment(args.to)
    if lower is None:
        raise ConfigError("Migrations start in DEV; promote --to uat or --to prod.")
    done = schema.registered(args.to)
    candidates = [name for name in schema.registered(lower) if name not in done]
    if args.count:
        candidates = candidates[: args.count]
    if not candidates:
        _out(f"Nothing to promote: {args.to.upper()} already has every {lower.upper()} migration.")
        return 0
    schema.migrations_dir(args.to).mkdir(parents=True, exist_ok=True)
    for name in candidates:
        shutil.copyfile(schema.migrations_dir(lower) / name, schema.migrations_dir(args.to) / name)
        _register(schema, args.to, name)
        _out(f"Promoted {name}: {lower.upper()} -> {args.to.upper()}")
    keep = schema.migrations_dir(args.to) / ".gitkeep"
    if keep.exists():
        keep.unlink()
    _out("Commit the changes and open a merge request for this environment only.")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schemaops", description=__doc__)
    parser.add_argument("--version", action="version", version=f"schemaops {__version__}")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root (default: current directory).")
    parser.add_argument("--log-format", choices=("text", "json"), default="json" if os.environ.get("CI") else "text")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    def schema_args(sub: argparse.ArgumentParser, *, required: bool) -> None:
        sub.add_argument("--cluster", required=required)
        sub.add_argument("--schema", required=required)

    def scope_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--env", required=True, choices=ENVIRONMENTS)
        schema_args(sub, required=False)
        sub.add_argument("--all", action="store_true", help="Every schema in the repository.")
        sub.add_argument("--changed-since", metavar="REF", help="Only schemas changed for this environment since REF.")

    commands.add_parser("validate", help="Run the repository governance checks.").set_defaults(run=cmd_validate)

    sub = commands.add_parser("lint", help="Lint migration files against the database standards.")
    sub.add_argument("paths", nargs="*", help="Files to lint (default: every DEV migration).")
    sub.add_argument("--ignore-linting", action="store_true", help="Treat warn and advise findings as accepted.")
    sub.set_defaults(run=cmd_lint)

    commands.add_parser("rules", help="Print the lint rules with reasons and alternatives.").set_defaults(run=cmd_rules)

    sub = commands.add_parser("check-mr", help="All merge request checks: governance, lint, approvals, promotion.")
    sub.add_argument("--base", help="Target commit (default: CI_MERGE_REQUEST_DIFF_BASE_SHA).")
    sub.add_argument("--label", action="append", help="Merge request label, for local runs.")
    sub.add_argument(
        "--report",
        metavar="FILE",
        default=os.environ.get("SCHEMAOPS_MR_REPORT"),
        help="Also write the plan as Markdown to FILE (or set SCHEMAOPS_MR_REPORT).",
    )
    sub.add_argument(
        "--comment",
        action="store_true",
        default=os.environ.get("SCHEMAOPS_MR_COMMENT", "").lower() in ("1", "true", "yes"),
        help="Post the plan as a comment on the merge request (or set SCHEMAOPS_MR_COMMENT=1).",
    )
    sub.set_defaults(run=cmd_check_mr)

    sub = commands.add_parser("plan", help="Show what a deployment would apply; changes nothing.")
    scope_args(sub)
    sub.set_defaults(run=cmd_deploy, dry_run=True)

    sub = commands.add_parser("deploy", help="Apply pending migrations to one environment.")
    scope_args(sub)
    sub.add_argument("--dry-run", action="store_true")
    sub.add_argument("--retry-failed", action="store_true", help="Retry a migration left failed or running, after a DBA check.")
    sub.set_defaults(run=cmd_deploy)

    sub = commands.add_parser("status", help="Show applied and pending migrations.")
    scope_args(sub)
    sub.add_argument("--read-only", action="store_true", help="Connect with the read-only history account.")
    sub.set_defaults(run=cmd_status)

    sub = commands.add_parser("mark-applied", help="DBA only: record a migration that was applied by hand.")
    sub.add_argument("--env", required=True, choices=ENVIRONMENTS)
    schema_args(sub, required=True)
    sub.add_argument("--file", required=True, help="Migration file name.")
    sub.set_defaults(run=cmd_mark_applied)

    sub = commands.add_parser("new-schema", help="Onboard a schema: main.yaml, environment files, initial dump.")
    schema_args(sub, required=True)
    sub.add_argument("--owner", required=True, help="Owning team.")
    sub.add_argument("--description", required=True)
    sub.add_argument("--contact", default="", help="Channel or address for questions.")
    sub.add_argument("--initial-dump", required=True, help="Schema-only dump of the existing schema.")
    sub.set_defaults(run=cmd_new_schema)

    sub = commands.add_parser("new-migration", help="Create and register a new DEV migration.")
    schema_args(sub, required=True)
    sub.add_argument("--name", required=True, help="Description, for example add_users_email.")
    sub.set_defaults(run=cmd_new_migration)

    sub = commands.add_parser("promote", help="Copy migrations to the next environment and register them.")
    schema_args(sub, required=True)
    sub.add_argument("--to", required=True, choices=ENVIRONMENTS[1:])
    sub.add_argument("--count", type=int, default=0, help="Promote only the next N migrations (default: all).")
    sub.set_defaults(run=cmd_promote)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log.configure(args.log_format, args.verbose)
    try:
        settings = Settings.load(args.root)
        return int(args.run(settings, args))
    except SchemaOpsError as exc:
        logger.error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        logger.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
