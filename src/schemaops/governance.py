"""Governance checks.

`check_repository` validates the repository as it stands. `check_merge_request`
compares a merge request with its target branch and enforces immutability and
the DEV -> UAT -> PROD promotion order.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml

from schemaops.errors import ConfigError, GovernanceError
from schemaops.repo import (
    ENVIRONMENTS,
    INITIAL_DUMP_RE,
    MIGRATION_NAME_RE,
    NAME_RE,
    Schema,
    Settings,
    discover_schemas,
    load_protected_tables,
    load_yaml,
    previous_environment,
)

MAIN_KEYS = ("cluster-name", "schema-name", "owner", "description")


@dataclass(frozen=True)
class Issue:
    check: str
    where: str
    message: str

    def format(self) -> str:
        return f"{self.where}: [{self.check}] {self.message}"


@dataclass(frozen=True)
class Added:
    """A migration this merge request adds to an environment."""

    schema: Schema
    environment: str
    name: str

    @property
    def path(self) -> Path:
        return self.schema.migrations_dir(self.environment) / self.name


# ------------------------------------------------------- repository checks


def _relative(settings: Settings, path: Path) -> str:
    try:
        return path.relative_to(settings.root).as_posix()
    except ValueError:
        return str(path)


def _check_main(settings: Settings, schema: Schema) -> list[Issue]:
    where = _relative(settings, schema.main_file)
    if not schema.main_file.is_file():
        return [Issue("MAIN_CONFIG", where, "main.yaml is missing. Every schema needs one.")]
    try:
        data = load_yaml(schema.main_file)
    except ConfigError as exc:
        return [Issue("MAIN_CONFIG", where, str(exc))]
    issues = [
        Issue("MAIN_CONFIG", where, f"`{key}` is required and must not be empty.")
        for key in MAIN_KEYS
        if not str(data.get(key) or "").strip()
    ]
    for key, expected in (("cluster-name", schema.cluster), ("schema-name", schema.name)):
        if data.get(key) and data[key] != expected:
            issues.append(Issue("MAIN_CONFIG", where, f"`{key}` is `{data[key]}` but the folder says `{expected}`."))
    for value in (schema.cluster, schema.name):
        if not NAME_RE.match(value):
            issues.append(Issue("MAIN_CONFIG", where, f"Folder name `{value}` must be lower snake_case."))
    return issues


def _check_environment(settings: Settings, schema: Schema, environment: str) -> tuple[list[Issue], list[str]]:
    """Validate one environment file and its migrations folder. Returns (issues, registered)."""
    path = schema.environment_file(environment)
    where = _relative(settings, path)
    if not path.is_file():
        return [Issue("ENV_CONFIG", where, f"Environment file for `{environment}` is missing.")], []
    try:
        data = load_yaml(path)
        registered = schema.registered(environment)
    except ConfigError as exc:
        return [Issue("ENV_CONFIG", where, str(exc))], []

    issues: list[Issue] = []
    for key, expected in (
        ("cluster-name", schema.cluster),
        ("schema-name", schema.name),
        ("environment", environment),
    ):
        if data.get(key) != expected:
            issues.append(Issue("ENV_CONFIG", where, f"`{key}` must be `{expected}` (found `{data.get(key)}`)."))

    duplicates = sorted({name for name in registered if registered.count(name) > 1})
    if duplicates:
        issues.append(Issue("REGISTRATION", where, f"Listed more than once: {', '.join(duplicates)}."))

    directory = schema.migrations_dir(environment)
    on_disk = (
        {item.name for item in directory.iterdir() if item.is_file() and not item.name.startswith(".")}
        if directory.is_dir()
        else set()
    )
    for name in registered:
        if name not in on_disk:
            issues.append(Issue("REGISTRATION", where, f"`{name}` is listed but migrations/{environment}/{name} does not exist."))
    for name in sorted(on_disk - set(registered)):
        issues.append(
            Issue("REGISTRATION", _relative(settings, directory / name), f"Not registered in environments/{environment}.yaml.")
        )

    for name in registered:
        match = MIGRATION_NAME_RE.match(name)
        valid_date = False
        if match:
            try:
                datetime.strptime(match.group(1), "%Y-%m-%d")
                valid_date = True
            except ValueError:
                pass
        if not valid_date:
            issues.append(
                Issue("FILE_NAME", where, f"`{name}` must be named YYYY-MM-DD_<lower_snake_description>.sql with a real date.")
            )

    if registered:
        if not INITIAL_DUMP_RE.match(registered[0]):
            issues.append(
                Issue("INITIAL_DUMP", where, f"The first migration must be <date>_initial_dump.sql (found `{registered[0]}`).")
            )
        extra = [name for name in registered[1:] if INITIAL_DUMP_RE.match(name)]
        if extra:
            issues.append(Issue("INITIAL_DUMP", where, f"Only one initial dump is allowed (extra: {', '.join(extra)})."))
        dates = [name[:10] for name in registered]
        if dates != sorted(dates):
            issues.append(
                Issue("ORDER", where, "Migrations must be listed in chronological order (by the date in the file name).")
            )
    return issues, registered


def check_schema(settings: Settings, schema: Schema) -> list[Issue]:
    issues = _check_main(settings, schema)
    registered: dict[str, list[str]] = {}
    for environment in ENVIRONMENTS:
        found, registered[environment] = _check_environment(settings, schema, environment)
        issues.extend(found)

    for environment in ENVIRONMENTS[1:]:
        lower = previous_environment(environment)
        assert lower is not None
        where = _relative(settings, schema.environment_file(environment))
        current, below = registered[environment], registered[lower]
        missing = [name for name in current if name not in below]
        if missing:
            issues.append(
                Issue("PROMOTION", where, f"Not present in {lower.upper()}: {', '.join(missing)}. Promote through {lower.upper()} first.")
            )
        elif current != below[: len(current)]:
            issues.append(
                Issue("PROMOTION", where, f"Must be the first {len(current)} migrations of {lower.upper()}, in the same order; none may be skipped.")
            )
        for name in current:
            ours = schema.migrations_dir(environment) / name
            theirs = schema.migrations_dir(lower) / name
            if ours.is_file() and theirs.is_file() and ours.read_bytes() != theirs.read_bytes():
                issues.append(
                    Issue("INTEGRITY", _relative(settings, ours), f"Content differs from the {lower.upper()} copy. A promoted migration must be identical.")
                )
    return issues


def check_repository(settings: Settings, schemas: list[Schema] | None = None) -> list[Issue]:
    issues: list[Issue] = []
    try:
        protected = load_protected_tables(settings)
    except ConfigError as exc:
        protected = {}
        issues.append(Issue("POLICY", settings.protected_tables_file, str(exc)))
    known = {(schema.cluster, schema.name) for schema in discover_schemas(settings)}
    for cluster, schema_name, table in protected:
        if (cluster, schema_name) not in known:
            issues.append(
                Issue("POLICY", settings.protected_tables_file, f"Protected table {cluster}.{schema_name}.{table} refers to an unknown schema.")
            )
    for schema in schemas if schemas is not None else discover_schemas(settings):
        issues.extend(check_schema(settings, schema))
    return issues


# --------------------------------------------------- merge request checks


def _git(settings: Settings, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(settings.root), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise GovernanceError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _registered_at(settings: Settings, base: str, schema: Schema, environment: str) -> list[str]:
    """The environment's migration list as it is on the target branch."""
    path = _relative(settings, schema.environment_file(environment))
    completed = subprocess.run(
        ["git", "-C", str(settings.root), "show", f"{base}:{path}"], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return []  # the file is new in this merge request
    try:
        data = yaml.safe_load(completed.stdout) or {}
    except yaml.YAMLError:
        return []
    migrations = data.get("migrations") if isinstance(data, dict) else None
    return [str(item) for item in migrations] if isinstance(migrations, list) else []


def added_migrations(settings: Settings, base: str) -> list[Added]:
    added: list[Added] = []
    for schema in discover_schemas(settings):
        for environment in ENVIRONMENTS:
            try:
                current = schema.registered(environment)
            except ConfigError:
                continue
            before = set(_registered_at(settings, base, schema, environment))
            added.extend(Added(schema, environment, name) for name in current if name not in before)
    return added


def check_merge_request(settings: Settings, base: str) -> tuple[list[Issue], list[Added]]:
    """Compare the working tree with `base` (the merge request's target commit)."""
    issues: list[Issue] = []
    clusters = PurePosixPath(settings.clusters_root)
    changed: list[tuple[str, str]] = []
    for line in _git(settings, "diff", "--name-status", "--no-renames", base, "--").splitlines():
        status, _, path = line.partition("\t")
        changed.append((status.strip(), path.strip()))

    # Applied history is immutable: existing migration files never change or disappear.
    for status, path in changed:
        parts = PurePosixPath(path).parts
        in_migrations = PurePosixPath(path).is_relative_to(clusters) and "migrations" in parts and path.endswith(".sql")
        if in_migrations and status in ("M", "D"):
            verb = "modified" if status == "M" else "deleted"
            issues.append(
                Issue("IMMUTABLE", path, f"An existing migration was {verb}. Migrations are immutable; add a new one instead.")
            )

    added = added_migrations(settings, base)
    for schema in discover_schemas(settings):
        for environment in ENVIRONMENTS:
            before = _registered_at(settings, base, schema, environment)
            try:
                current = schema.registered(environment)
            except ConfigError:
                continue  # reported by check_repository
            if current[: len(before)] != before:
                issues.append(
                    Issue(
                        "APPEND_ONLY",
                        _relative(settings, schema.environment_file(environment)),
                        "Existing entries were removed or reordered. New migrations may only be added at the end.",
                    )
                )

    environments = sorted({item.environment for item in added}, key=ENVIRONMENTS.index)
    if len(environments) > 1:
        issues.append(
            Issue(
                "ONE_ENVIRONMENT",
                "merge request",
                f"Adds migrations to {', '.join(env.upper() for env in environments)}. "
                "Each promotion needs its own merge request: DEV first, then UAT, then PROD.",
            )
        )

    # A promotion is only valid if the previous environment's merge request is already merged.
    for item in added:
        lower = previous_environment(item.environment)
        if lower and item.name not in _registered_at(settings, base, item.schema, lower):
            issues.append(
                Issue(
                    "PROMOTION",
                    _relative(settings, item.path),
                    f"Cannot be added to {item.environment.upper()}: it is not yet merged for {lower.upper()} "
                    f"on the target branch. Merge and deploy the {lower.upper()} merge request first.",
                )
            )

    if added:
        outside = sorted(
            path for _status, path in changed if not PurePosixPath(path).is_relative_to(clusters)
        )
        if outside:
            issues.append(
                Issue(
                    "SEPARATE_CHANGES",
                    "merge request",
                    "Migrations must not be combined with framework, policy or pipeline changes "
                    f"({', '.join(outside[:5])}{'...' if len(outside) > 5 else ''}).",
                )
            )
    return issues, added
