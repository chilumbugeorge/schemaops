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
    discover_clusters,
    discover_schemas,
    load_protected_tables,
    load_yaml,
    previous_environment,
)

SCHEMA_KEYS = ("cluster-name", "schema-name", "owner", "description")
SCHEMA_OPTIONAL = {
    "owner-group": None,  # GitLab group that owns the schema, for example @acme/trades-team
    "contact": None,
    "escalation-contact": None,
    "tier": (1, 2, 3),  # 1 is the most critical
    "data-classification": ("public", "internal", "confidential", "restricted"),
    "status": ("active", "frozen", "retired"),
}
CLUSTER_KEYS = ("cluster-name", "description", "engine", "version", "dba-contact")
CLUSTER_OPTIONAL = ("topology", "environments")
ENGINES = ("mariadb", "mysql")
TOPOLOGIES = ("standalone", "primary-replica", "galera")
SECRET_KEYS = ("user", "username", "password", "passwd", "secret", "token")


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
    where = _relative(settings, schema.config_file)
    if not schema.config_file.is_file():
        legacy = " Rename main.yaml to schema.yaml." if (schema.path / "main.yaml").is_file() else ""
        return [Issue("SCHEMA_CONFIG", where, f"schema.yaml is missing. Every schema needs one.{legacy}")]
    try:
        data = load_yaml(schema.config_file)
    except ConfigError as exc:
        return [Issue("SCHEMA_CONFIG", where, str(exc))]
    issues = [
        Issue("SCHEMA_CONFIG", where, f"`{key}` is required and must not be empty.")
        for key in SCHEMA_KEYS
        if not str(data.get(key) or "").strip()
    ]
    for key, expected in (("cluster-name", schema.cluster), ("schema-name", schema.name)):
        if data.get(key) and data[key] != expected:
            issues.append(Issue("SCHEMA_CONFIG", where, f"`{key}` is `{data[key]}` but the folder says `{expected}`."))
    for value in (schema.cluster, schema.name):
        if not NAME_RE.match(value):
            issues.append(Issue("SCHEMA_CONFIG", where, f"Folder name `{value}` must be lower snake_case."))
    for key in data:
        if key not in SCHEMA_KEYS and key not in SCHEMA_OPTIONAL:
            issues.append(Issue("SCHEMA_CONFIG", where, f"Unknown key `{key}`. Allowed: {', '.join([*SCHEMA_KEYS, *SCHEMA_OPTIONAL])}."))
    for key, allowed in SCHEMA_OPTIONAL.items():
        if allowed and key in data and data[key] not in allowed:
            issues.append(Issue("SCHEMA_CONFIG", where, f"`{key}` must be one of {', '.join(str(v) for v in allowed)} (found `{data[key]}`)."))
    issues.extend(_check_ownership(settings, schema, data))
    return issues


def _check_ownership(settings: Settings, schema: Schema, data: dict[str, object]) -> list[Issue]:
    """`owner-group` must be a GitLab group and, if CODEOWNERS exists, own the schema's folder there."""
    group = data.get("owner-group")
    if not group:
        return []
    where = _relative(settings, schema.config_file)
    if not isinstance(group, str) or not group.startswith("@") or " " in group:
        return [Issue("OWNERSHIP", where, f"`owner-group` must be a GitLab group such as @acme/trades-team (found `{group}`).")]
    codeowners = settings.root / "CODEOWNERS"
    if not codeowners.is_file():
        return []
    folder = f"/{settings.clusters_root}/{schema.cluster}/{schema.name}/"
    for line in codeowners.read_text(encoding="utf-8").splitlines():
        parts = line.split("#")[0].split()
        if parts and parts[0] == folder and group in parts[1:]:
            return []
    return [Issue("OWNERSHIP", "CODEOWNERS", f"Add `{folder} {group}` so the owning group must approve changes to {schema.label}.")]


def check_cluster(settings: Settings, path: Path) -> list[Issue]:
    """Validate clusters/<cluster>/cluster.yaml."""
    from schemaops.db import HOST_RE, STRICT_TLS_MODES, TLS_MODES

    config = path / "cluster.yaml"
    where = _relative(settings, config)
    if not config.is_file():
        return [Issue("CLUSTER_CONFIG", where, "cluster.yaml is missing. Every cluster needs one (`schemaops new-cluster`).")]
    try:
        data = load_yaml(config)
    except ConfigError as exc:
        return [Issue("CLUSTER_CONFIG", where, str(exc))]

    def issue(message: str) -> Issue:
        return Issue("CLUSTER_CONFIG", where, message)

    issues = [issue(f"`{key}` is required and must not be empty.") for key in CLUSTER_KEYS if not str(data.get(key) or "").strip()]
    if data.get("cluster-name") and data["cluster-name"] != path.name:
        issues.append(issue(f"`cluster-name` is `{data['cluster-name']}` but the folder says `{path.name}`."))
    if not NAME_RE.match(path.name):
        issues.append(issue(f"Folder name `{path.name}` must be lower snake_case."))
    if data.get("engine") and data["engine"] not in ENGINES:
        issues.append(issue(f"`engine` must be one of {', '.join(ENGINES)} (found `{data['engine']}`)."))
    if "version" in data and not isinstance(data["version"], str):
        issues.append(issue('`version` must be quoted text such as "10.11", so that 10.10 is not read as 10.1.'))
    if data.get("topology") and data["topology"] not in TOPOLOGIES:
        issues.append(issue(f"`topology` must be one of {', '.join(TOPOLOGIES)} (found `{data['topology']}`)."))
    for key in data:
        if key not in CLUSTER_KEYS and key not in CLUSTER_OPTIONAL:
            issues.append(issue(f"Unknown key `{key}`. Allowed: {', '.join([*CLUSTER_KEYS, *CLUSTER_OPTIONAL])}."))

    environments = data.get("environments") or {}
    if not isinstance(environments, dict):
        return [*issues, issue("`environments` must map dev, uat and prod to connection details.")]
    for environment, entry in environments.items():
        if environment not in ENVIRONMENTS:
            issues.append(issue(f"Unknown environment `{environment}`. Allowed: {', '.join(ENVIRONMENTS)}."))
            continue
        if not isinstance(entry, dict):
            issues.append(issue(f"`environments.{environment}` must be a mapping with host, port and tls-mode."))
            continue
        for key in entry:
            if str(key).lower() in SECRET_KEYS:
                issues.append(issue(f"`environments.{environment}.{key}`: credentials must never be stored in Git. Use CI/CD variables."))
            elif key not in ("host", "port", "tls-mode"):
                issues.append(issue(f"Unknown key `environments.{environment}.{key}`. Allowed: host, port, tls-mode."))
        if "host" in entry and not HOST_RE.match(str(entry["host"])):
            issues.append(issue(f"`environments.{environment}.host` is not a valid host name."))
        if "port" in entry and not (isinstance(entry["port"], int) and 1 <= entry["port"] <= 65535):
            issues.append(issue(f"`environments.{environment}.port` must be a number between 1 and 65535."))
        mode = entry.get("tls-mode")
        if mode is not None and mode not in TLS_MODES:
            issues.append(issue(f"`environments.{environment}.tls-mode` must be one of {', '.join(TLS_MODES)}."))
        elif mode is not None and environment != "dev" and mode not in STRICT_TLS_MODES:
            issues.append(issue(f"`environments.{environment}.tls-mode` must verify the server certificate outside DEV."))
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


def check_schema(settings: Settings, schema: Schema, *, with_cluster: bool = True) -> list[Issue]:
    issues = check_cluster(settings, schema.path.parent) if with_cluster else []
    issues += _check_main(settings, schema)
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
    if schemas is None:
        for cluster in discover_clusters(settings):
            issues.extend(check_cluster(settings, cluster))
    for schema in schemas if schemas is not None else discover_schemas(settings):
        issues.extend(check_schema(settings, schema, with_cluster=schemas is not None))
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

    # A frozen or retired schema accepts no new migrations.
    for schema in {item.schema for item in added}:
        status = schema.config().get("status", "active")
        if status in ("frozen", "retired"):
            issues.append(
                Issue(
                    "FROZEN",
                    _relative(settings, schema.config_file),
                    f"{schema.label} is {status}: no new migrations are accepted. Its owners must set `status: active` first, in a separate merge request.",
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
        # cluster.yaml decides where credentials are sent, so only a brand-new one may ride along.
        outside = sorted(
            path
            for status, path in changed
            if not PurePosixPath(path).is_relative_to(clusters)
            or (PurePosixPath(path).name == "cluster.yaml" and status != "A")
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
