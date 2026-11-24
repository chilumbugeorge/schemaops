"""Compare what Git says should be deployed with what each database actually holds.

Read-only: it reports and changes nothing. Meant to run on a schedule, because a
deployment only looks at the schemas changed in that push.
"""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass
from datetime import date

from schemaops.deploy import DONE, LEFTOVER_TABLE_RE, Target, normalize_definition
from schemaops.errors import ConfigError
from schemaops.repo import Schema, Settings


@dataclass(frozen=True)
class Finding:
    kind: str  # PENDING | FAILED | CHECKSUM | UNKNOWN | DRIFT | LEFTOVER
    message: str
    detail: str = ""  # optional multi-line context, for example a diff


def _merged_on(settings: Settings, path: str) -> str:
    """When a file was added to the default branch, as 'YYYY-MM-DD (N days ago)'."""
    completed = subprocess.run(
        ["git", "-C", str(settings.root), "log", "--diff-filter=A", "--format=%cs", "-1", "--", path],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    day = completed.stdout.strip()
    if completed.returncode != 0 or not day:
        return ""
    try:
        age = (date.today() - date.fromisoformat(day)).days
    except ValueError:
        return ""
    return f", merged {day} ({age} day(s) ago)"


def reconcile(settings: Settings, schema: Schema, environment: str, target: Target) -> list[Finding]:
    findings: list[Finding] = []
    try:
        migrations = schema.migrations(environment)
    except ConfigError as exc:
        return [Finding("UNKNOWN", str(exc))]
    history = target.history()
    names = {migration.name for migration in migrations}

    for migration in migrations:
        row = history.get(migration.name)
        if row is None:
            merged = _merged_on(settings, str(migration.path))
            findings.append(Finding("PENDING", f"`{migration.name}` is registered in Git but not applied{merged}."))
        elif row.status not in DONE:
            findings.append(Finding("FAILED", f"`{migration.name}` is in state `{row.status}`; a DBA must resolve it."))
        elif migration.path.is_file() and row.sha256 != migration.sha256:
            findings.append(Finding("CHECKSUM", f"`{migration.name}` was changed after it was applied."))
    findings.extend(
        Finding("UNKNOWN", f"`{name}` is applied in the database but is no longer in Git.")
        for name in history
        if name not in names
    )

    findings.extend(Finding("LEFTOVER", f"{item} looks like a leftover of an interrupted online schema change.") for item in target.leftovers())

    # Drift: the schema now, against the schema as it was after the last deployment.
    recorded = target.snapshot()
    if recorded is not None:
        live = {name: text for name, text in target.definitions().items() if not LEFTOVER_TABLE_RE.match(name)}
        for name in sorted(set(live) - set(recorded)):
            findings.append(Finding("DRIFT", f"Table `{name}` exists but was not created by a migration."))
        for name in sorted(set(recorded) - set(live)):
            findings.append(Finding("DRIFT", f"Table `{name}` was removed outside a migration."))
        for name in sorted(set(live) & set(recorded)):
            before, after = normalize_definition(recorded[name]), normalize_definition(live[name])
            if before != after:
                diff = difflib.unified_diff(before.splitlines(), after.splitlines(), "after last deployment", "now", lineterm="", n=0)
                findings.append(Finding("DRIFT", f"Table `{name}` was changed outside a migration.", "\n".join(list(diff)[:40])))
    return findings
