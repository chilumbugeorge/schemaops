"""Exceptions. Each carries the process exit code the CLI should return."""

from __future__ import annotations


class SchemaOpsError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1


class ConfigError(SchemaOpsError):
    """Invalid or missing configuration (YAML files, environment variables)."""

    exit_code = 2


class GovernanceError(SchemaOpsError):
    """A governance, integrity or promotion rule was violated."""


class DeployError(SchemaOpsError):
    """A migration could not be applied."""


class ManualActionRequired(SchemaOpsError):
    """Deployment stopped at a migration the DBA team must apply by hand."""

    exit_code = 3
