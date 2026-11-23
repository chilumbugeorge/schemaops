"""The monorepo layout: settings, schemas, environments and migration files.

    schemaops.yaml                       framework settings (optional)
    policy/protected_tables.yaml         hot and large tables
    clusters/<cluster>/cluster.yaml      facts shared by every schema on the cluster
    clusters/<cluster>/<schema>/schema.yaml
    clusters/<cluster>/<schema>/environments/<env>.yaml
    clusters/<cluster>/<schema>/migrations/<env>/<YYYY-MM-DD>_<description>.sql
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from schemaops.errors import ConfigError

ENVIRONMENTS: tuple[str, ...] = ("dev", "uat", "prod")  # promotion order
MIGRATION_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")
INITIAL_DUMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_initial_dump\.sql$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def previous_environment(environment: str) -> str | None:
    index = ENVIRONMENTS.index(environment)
    return ENVIRONMENTS[index - 1] if index else None


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: file not found") from exc
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping of keys to values")
    return data


@dataclass(frozen=True)
class OnlineChangeSettings:
    """Limits passed to pt-online-schema-change."""

    lock_wait_timeout: int = 5  # seconds a metadata lock may be waited for
    max_lag: int = 5  # pause while replicas lag more than this many seconds
    prefer_instant: bool = True  # try ALGORITHM=INSTANT first; copy the table only if the server refuses
    max_load: str = "Threads_running=50"  # pause above this load
    critical_load: str = "Threads_running=200"  # abort above this load
    chunk_time: float = 0.5


@dataclass(frozen=True)
class Settings:
    root: Path
    clusters_root: str = "clusters"
    protected_tables_file: str = "policy/protected_tables.yaml"
    bypass_label: str = "ignore-linting"
    blocking_without_label: frozenset[str] = frozenset({"warn", "advise"})
    dba_approvers: tuple[str, ...] = ()
    manual_environments: tuple[str, ...] = ("prod",)
    verify_previous_deployment: bool = True
    # A table larger than either limit is treated as protected even if it is not listed. 0 disables.
    auto_protect_rows: int = 10_000_000
    auto_protect_bytes: int = 10 * 1024**3
    # Pre-flight: refuse to start while a transaction has been open longer than this. 0 disables.
    max_transaction_seconds: int = 120
    # Pre-flight: free disk space required, as a multiple of the table's size. 0 disables.
    disk_headroom: float = 1.5
    connect_timeout: int = 10
    statement_timeout: int = 60
    online_change: OnlineChangeSettings = field(default_factory=OnlineChangeSettings)

    @property
    def clusters_dir(self) -> Path:
        return self.root / self.clusters_root

    @classmethod
    def load(cls, root: Path) -> Settings:
        root = root.resolve()
        path = root / "schemaops.yaml"
        if not path.is_file():
            return cls(root=root)
        data = load_yaml(path)
        lint = data.get("lint") or {}
        governance = data.get("governance") or {}
        connection = data.get("connection") or {}
        online = data.get("online-schema-change") or {}
        auto = governance.get("auto-protect") or {}
        preflight = data.get("preflight") or {}
        defaults = cls(root=root)
        osc = defaults.online_change
        try:
            settings = cls(
                root=root,
                clusters_root=str(data.get("clusters-root", defaults.clusters_root)),
                protected_tables_file=str(governance.get("protected-tables-file", defaults.protected_tables_file)),
                bypass_label=str(lint.get("bypass-label", defaults.bypass_label)),
                blocking_without_label=frozenset(
                    lint.get("blocking-without-label", sorted(defaults.blocking_without_label))
                ),
                dba_approvers=tuple(governance.get("dba-approvers") or ()),
                manual_environments=tuple(governance.get("manual-environments", defaults.manual_environments)),
                verify_previous_deployment=bool(
                    governance.get("verify-previous-deployment", defaults.verify_previous_deployment)
                ),
                auto_protect_rows=int(auto.get("max-rows", defaults.auto_protect_rows)),
                auto_protect_bytes=int(float(auto.get("max-size-gb", defaults.auto_protect_bytes / 1024**3)) * 1024**3),
                max_transaction_seconds=int(preflight.get("max-transaction-seconds", defaults.max_transaction_seconds)),
                disk_headroom=float(preflight.get("disk-headroom", defaults.disk_headroom)),
                connect_timeout=int(connection.get("connect-timeout", defaults.connect_timeout)),
                statement_timeout=int(connection.get("statement-timeout", defaults.statement_timeout)),
                online_change=OnlineChangeSettings(
                    lock_wait_timeout=int(online.get("lock-wait-timeout", osc.lock_wait_timeout)),
                    prefer_instant=bool(online.get("prefer-instant", osc.prefer_instant)),
                    max_lag=int(online.get("max-lag", osc.max_lag)),
                    max_load=str(online.get("max-load", osc.max_load)),
                    critical_load=str(online.get("critical-load", osc.critical_load)),
                    chunk_time=float(online.get("chunk-time", osc.chunk_time)),
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: invalid value ({exc})") from exc
        unknown = settings.blocking_without_label - {"warn", "advise"}
        unknown |= set(settings.manual_environments) - set(ENVIRONMENTS)
        if unknown:
            raise ConfigError(f"{path}: unknown value(s) {sorted(unknown)}")
        if not 1 <= settings.online_change.lock_wait_timeout <= 5:
            raise ConfigError(f"{path}: online-schema-change.lock-wait-timeout must be between 1 and 5 seconds")
        return settings


@dataclass(frozen=True)
class Migration:
    name: str
    path: Path

    @property
    def is_initial_dump(self) -> bool:
        return bool(INITIAL_DUMP_RE.match(self.name))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Schema:
    """One schema of one cluster, i.e. one `clusters/<cluster>/<schema>` folder."""

    cluster: str
    name: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.cluster}/{self.name}"

    @property
    def config_file(self) -> Path:
        return self.path / "schema.yaml"

    def config(self) -> dict[str, Any]:
        """schema.yaml, or an empty mapping if it is missing or invalid (governance reports that)."""
        try:
            return load_yaml(self.config_file)
        except ConfigError:
            return {}

    def environment_file(self, environment: str) -> Path:
        return self.path / "environments" / f"{environment}.yaml"

    def migrations_dir(self, environment: str) -> Path:
        return self.path / "migrations" / environment

    def registered(self, environment: str) -> list[str]:
        """Migration file names listed in the environment file, in order."""
        data = load_yaml(self.environment_file(environment))
        migrations = data.get("migrations") or []
        if not isinstance(migrations, list) or not all(isinstance(item, str) for item in migrations):
            raise ConfigError(f"{self.environment_file(environment)}: `migrations` must be a list of file names")
        return list(migrations)

    def migrations(self, environment: str) -> list[Migration]:
        directory = self.migrations_dir(environment)
        return [Migration(name, directory / name) for name in self.registered(environment)]


def discover_clusters(settings: Settings) -> list[Path]:
    if not settings.clusters_dir.is_dir():
        return []
    return [
        path for path in sorted(settings.clusters_dir.iterdir()) if path.is_dir() and not path.name.startswith(".")
    ]


def cluster_connection(settings: Settings, cluster: str, environment: str) -> dict[str, str]:
    """Non-secret connection details from cluster.yaml: host, port, tls-mode. Empty if absent."""
    path = settings.clusters_dir / cluster / "cluster.yaml"
    if not path.is_file():
        return {}
    try:
        entry = (load_yaml(path).get("environments") or {}).get(environment) or {}
    except ConfigError:
        return {}
    if not isinstance(entry, dict):
        return {}
    return {key: str(entry[key]) for key in ("host", "port", "tls-mode") if entry.get(key) is not None}


def discover_schemas(settings: Settings) -> list[Schema]:
    if not settings.clusters_dir.is_dir():
        return []
    return [
        Schema(cluster.name, schema.name, schema)
        for cluster in sorted(settings.clusters_dir.iterdir())
        if cluster.is_dir() and not cluster.name.startswith(".")
        for schema in sorted(cluster.iterdir())
        if schema.is_dir() and not schema.name.startswith(".")
    ]


def find_schema(settings: Settings, cluster: str, name: str) -> Schema:
    path = settings.clusters_dir / cluster / name
    if not path.is_dir():
        raise ConfigError(f"Unknown schema {cluster}/{name}: {path} does not exist")
    return Schema(cluster, name, path)


def load_protected_tables(settings: Settings) -> dict[tuple[str, str, str], str]:
    """Hot and large tables: {(cluster, schema, table): reason}."""
    path = settings.root / settings.protected_tables_file
    if not path.is_file():
        return {}
    entries = load_yaml(path).get("protected-tables") or []
    tables: dict[tuple[str, str, str], str] = {}
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: `protected-tables` must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not all(entry.get(key) for key in ("cluster", "schema", "table")):
            raise ConfigError(f"{path}: every entry needs `cluster`, `schema` and `table`")
        key = (str(entry["cluster"]), str(entry["schema"]), str(entry["table"]).lower())
        tables[key] = str(entry.get("reason", ""))
    return tables
