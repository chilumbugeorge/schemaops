"""Database connections.

Credentials come only from environment variables (GitLab protected CI variables
in the pipeline) and are never written to logs. For cluster `trades` in `prod`:

    SCHEMAOPS_TRADES_PROD_USER       required
    SCHEMAOPS_TRADES_PROD_PASSWORD   required
    SCHEMAOPS_TRADES_PROD_TLS_CA     path to the CA certificate (GitLab "File" variable)
    SCHEMAOPS_TRADES_PROD_HOST       required unless cluster.yaml gives the host
    SCHEMAOPS_TRADES_PROD_PORT       default 3306
    SCHEMAOPS_TRADES_PROD_TLS_MODE   verify-identity (default) | verify-ca | required | disabled

Host, port and TLS mode are not secrets and may instead be listed per environment in
clusters/<cluster>/cluster.yaml; a variable, when set, wins.

`..._RO_USER` / `..._RO_PASSWORD` name a read-only account that may only read the
migration history; a higher environment uses it to prove a migration was deployed.
"""

from __future__ import annotations

import os
import re
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from schemaops import log
from schemaops.errors import ConfigError, DeployError

TLS_MODES = ("verify-identity", "verify-ca", "required", "disabled")
STRICT_TLS_MODES = ("verify-identity", "verify-ca")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")


def variable_prefix(cluster: str, environment: str) -> str:
    return f"SCHEMAOPS_{cluster}_{environment}".upper()


@dataclass(frozen=True)
class ConnectionConfig:
    cluster: str
    environment: str
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    tls_mode: str = "verify-identity"
    tls_ca: str = ""
    connect_timeout: int = 10
    statement_timeout: int = 60
    read_only: bool = False

    @classmethod
    def from_env(
        cls,
        cluster: str,
        environment: str,
        *,
        read_only: bool = False,
        connect_timeout: int = 10,
        statement_timeout: int = 60,
        environ: Mapping[str, str] | None = None,
        defaults: Mapping[str, str] | None = None,
    ) -> ConnectionConfig:
        """`defaults` are the non-secret details from cluster.yaml: host, port, tls-mode."""
        env = os.environ if environ is None else environ
        known = defaults or {}
        prefix = variable_prefix(cluster, environment)
        role = "_RO" if read_only else ""
        host = env.get(f"{prefix}_HOST") or known.get("host", "")
        missing = [
            f"{prefix}{name}" for name in (f"{role}_USER", f"{role}_PASSWORD") if not env.get(f"{prefix}{name}")
        ]
        if not host:
            missing.insert(0, f"{prefix}_HOST (or environments.{environment}.host in cluster.yaml)")
        if missing:
            raise ConfigError(
                f"Missing connection variable(s) for {cluster}/{environment}: {', '.join(missing)}. "
                "Set them as protected, environment-scoped CI/CD variables; never commit credentials."
            )
        port = env.get(f"{prefix}_PORT") or known.get("port", "3306")
        if not port.isdigit():
            raise ConfigError(f"{prefix}_PORT must be a number")
        config = cls(
            cluster=cluster,
            environment=environment,
            host=host,
            port=int(port),
            user=env[f"{prefix}{role}_USER"],
            password=env[f"{prefix}{role}_PASSWORD"],
            tls_mode=(env.get(f"{prefix}_TLS_MODE") or known.get("tls-mode", "verify-identity")).lower(),
            tls_ca=env.get(f"{prefix}_TLS_CA", ""),
            connect_timeout=connect_timeout,
            statement_timeout=statement_timeout,
            read_only=read_only,
        )
        log.register_secret(config.password)
        config.validate()
        return config

    def validate(self) -> None:
        """Reject an unusable or unsafe configuration before any connection is opened."""
        prefix = variable_prefix(self.cluster, self.environment)
        problems: list[str] = []
        if not HOST_RE.match(self.host):
            problems.append(f"{prefix}_HOST is not a valid host name")
        if not 1 <= self.port <= 65535:
            problems.append(f"{prefix}_PORT must be between 1 and 65535")
        if self.user.lower() in ("root", "admin"):
            problems.append(f"{prefix} user `{self.user}` is an administrative account; use a least-privilege account")
        if self.tls_mode not in TLS_MODES:
            problems.append(f"{prefix}_TLS_MODE must be one of {', '.join(TLS_MODES)}")
        elif self.environment != "dev" and self.tls_mode not in STRICT_TLS_MODES:
            problems.append(f"{prefix}_TLS_MODE must verify the server certificate outside DEV (verify-identity or verify-ca)")
        if self.tls_mode in STRICT_TLS_MODES and self.tls_ca and not Path(self.tls_ca).is_file():
            problems.append(f"{prefix}_TLS_CA points to a file that does not exist")
        if self.connect_timeout <= 0 or self.statement_timeout <= 0:
            problems.append("connection timeouts must be positive")
        if problems:
            raise ConfigError("Invalid connection configuration: " + "; ".join(problems))

    def ssl_context(self) -> ssl.SSLContext | None:
        if self.tls_mode == "disabled":
            return None
        context = ssl.create_default_context(cafile=self.tls_ca or None)
        if self.tls_mode == "required":
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        elif self.tls_mode == "verify-ca":
            context.check_hostname = False
        return context

    def describe(self) -> dict[str, object]:
        """Safe-to-log summary. Never includes the password."""
        return {"host": self.host, "port": self.port, "user": self.user, "tls": self.tls_mode}

    def client_options_file(self, path: Path) -> None:
        """Write a 0600 MySQL option file so the password never appears on a command line."""

        def quote(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        lines = ["[client]", f"user={quote(self.user)}", f"password={quote(self.password)}"]
        if self.tls_mode != "disabled":
            lines.append(f"ssl-ca={quote(self.tls_ca)}" if self.tls_ca else "ssl")
            if self.tls_mode in STRICT_TLS_MODES:
                lines.append("ssl-verify-server-cert")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def connect(config: ConnectionConfig, database: str) -> Any:
    """Open a connection to one schema. Errors never include credentials."""
    import pymysql

    try:
        return pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=database,
            ssl=config.ssl_context(),
            connect_timeout=config.connect_timeout,
            read_timeout=config.statement_timeout,
            write_timeout=config.statement_timeout,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as exc:
        raise DeployError(
            f"Cannot connect to {config.cluster}/{database} in {config.environment.upper()} "
            f"({config.host}:{config.port}): {log.redact(str(exc))}"
        ) from exc
