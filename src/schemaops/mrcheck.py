"""Everything a merge request must pass, and the plan reviewers see.

`run` gathers the facts once; `render_text` prints them in the job log and
`render_markdown` formats them as the merge request comment.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from schemaops import governance, sqlplan
from schemaops.deploy import DONE, HistoryRow, TableFacts, oversized, verify_previous_environment
from schemaops.errors import ConfigError, GovernanceError, SchemaOpsError
from schemaops.gitlab import MergeRequest
from schemaops.lint.model import Finding, Severity
from schemaops.lint.rules import lint_sql
from schemaops.repo import INITIAL_DUMP_RE, Schema, Settings, load_protected_tables, previous_environment

MARKER = "<!-- schemaops-plan -->"

LowerHistory = Callable[[Settings, Schema, str], "dict[str, HistoryRow] | None"]
TableInspector = Callable[[Settings, Schema, str, str], "TableFacts | None"]


@dataclass(frozen=True)
class PlanRow:
    migration: str
    schema: str
    environment: str
    change: str
    method: str
    size: str
    previous: str


@dataclass
class Report:
    bypass_label: str
    bypass: bool = False
    added: int = 0
    issues: list[str] = field(default_factory=list)
    findings: list[tuple[str, Finding, bool]] = field(default_factory=list)  # (file, finding, blocking)
    rows: list[PlanRow] = field(default_factory=list)
    dba: list[str] = field(default_factory=list)
    promotion: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # blocking problems outside governance and lint

    @property
    def blocking_findings(self) -> int:
        return sum(blocking for _path, _finding, blocking in self.findings)

    @property
    def problems(self) -> int:
        return len(self.issues) + self.blocking_findings + len(self.blocked)

    @property
    def passed(self) -> bool:
        return self.problems == 0


def _is_blocking(finding: Finding, settings: Settings, *, bypass: bool, promoted: bool) -> bool:
    if finding.severity is Severity.ERROR:
        return True
    if promoted or bypass:
        return False  # promoted files were accepted when they entered DEV
    return finding.severity.value in settings.blocking_without_label


def lint_file(path: Path, settings: Settings, *, bypass: bool, promoted: bool = False) -> list[tuple[str, Finding, bool]]:
    shown = path.relative_to(settings.root).as_posix() if path.is_relative_to(settings.root) else str(path)
    try:
        sql = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [(shown, Finding("FILE", Severity.ERROR, 1, f"Cannot be read as UTF-8 text ({exc})."), True)]
    return [
        (shown, finding, _is_blocking(finding, settings, bypass=bypass, promoted=promoted))
        for finding in lint_sql(sql, initial_dump=bool(INITIAL_DUMP_RE.match(path.name)))
    ]


def _describe(operation: sqlplan.Operation) -> str:
    if operation.kind == sqlplan.ALTER_TABLE:
        spec = " ".join(operation.alter.split())
        return f"ALTER `{operation.table}`: {spec[:70]}{'...' if len(spec) > 70 else ''}"
    tables = ", ".join(f"`{table}`" for table in operation.tables)
    return f"{operation.kind.replace('_', ' ').upper()} {tables}".strip()


def _previous(environment: str, name: str, history: dict[str, HistoryRow] | None) -> str:
    lower = previous_environment(environment)
    if lower is None:
        return "n/a"
    if history is None:
        return f"{lower.upper()}: not checked"
    row = history.get(name)
    if row is None:
        return f"{lower.upper()}: not deployed"
    took = f" in {row.seconds}s" if row.seconds is not None and row.status in DONE else ""
    return f"{lower.upper()}: {row.status}{took} ({row.method})" if row.method else f"{lower.upper()}: {row.status}{took}"


def run(
    settings: Settings,
    base: str,
    *,
    labels: set[str],
    request: MergeRequest | None,
    in_ci: bool,
    lower_history: LowerHistory,
    inspect_table: TableInspector,
) -> Report:
    report = Report(bypass_label=settings.bypass_label, bypass=settings.bypass_label in labels)

    # Governance
    issues = governance.check_repository(settings)
    diff_issues, added = governance.check_merge_request(settings, base)
    report.issues = [issue.format() for issue in [*issues, *diff_issues]]
    report.added = len(added)

    # Lint
    for item in added:
        if item.path.is_file():
            report.findings.extend(
                lint_file(item.path, settings, bypass=report.bypass, promoted=item.environment != "dev")
            )

    # Evidence from the previous environment, fetched once per schema
    histories: dict[tuple[str, str], dict[str, HistoryRow] | None] = {}
    promoted: dict[tuple[Schema, str], list[str]] = {}
    for item in added:
        if previous_environment(item.environment):
            promoted.setdefault((item.schema, item.environment), []).append(item.name)
    for (schema, environment), names in promoted.items():
        lower = (previous_environment(environment) or "").upper()
        key = (schema.label, environment)
        histories[key] = None
        if not settings.verify_previous_deployment:
            report.promotion.append(f"{schema.label}: deployment evidence check is disabled.")
            continue
        try:
            history = lower_history(settings, schema, environment) or {}
            histories[key] = history
            shas = {
                name: hashlib.sha256((schema.migrations_dir(environment) / name).read_bytes()).hexdigest()
                for name in names
                if (schema.migrations_dir(environment) / name).is_file()
            }
            verify_previous_environment(schema, environment, [n for n in names if n in shas], history, shas)
            report.promotion.append(f"{schema.label}: {len(names)} migration(s) confirmed deployed to {lower}.")
        except ConfigError as exc:
            if in_ci:
                report.blocked.append(f"{schema.label}: cannot confirm the {lower} deployment. {exc}")
            else:
                report.promotion.append(f"{schema.label}: {lower} deployment not checked (no read-only credentials locally).")
        except SchemaOpsError as exc:
            report.blocked.append(str(exc))

    # The plan, and what needs the DBA team
    protected = load_protected_tables(settings)
    for item in added:
        if not item.path.is_file():
            continue
        history = histories.get((item.schema.label, item.environment))
        previous = _previous(item.environment, item.name, history)
        operations = sqlplan.parse_operations(item.path.read_text(encoding="utf-8", errors="replace"))
        if INITIAL_DUMP_RE.match(item.name):
            report.rows.append(
                PlanRow(item.name, item.schema.label, item.environment, f"Initial dump, {len(operations)} table(s)",
                        "direct on an empty schema, otherwise baseline", "n/a", previous)  # fmt: skip
            )
            continue
        for operation in operations:
            manual: list[str] = []
            sizes: list[str] = []
            hot = [t for t in operation.tables if (item.schema.cluster, item.schema.name, t) in protected]
            if hot:
                report.dba.append(f"{item.name}: changes protected table(s) {', '.join(hot)}")
                if item.environment in settings.manual_environments:
                    manual.append("protected table")
            if operation.adds_unique_index:
                report.dba.append(f"{item.name}: adds a UNIQUE index to `{operation.table}`")
                manual.append("UNIQUE index")
            if operation.kind != sqlplan.CREATE_TABLE:
                for table in operation.tables[:1]:
                    facts = inspect_table(settings, item.schema, item.environment, table)
                    if facts is None or not facts.exists:
                        continue
                    sizes.append(facts.describe())
                    too_big = oversized(settings, facts)
                    if too_big:
                        report.dba.append(f"{item.name}: `{table}` is a large table, {too_big}")
                        if item.environment in settings.manual_environments:
                            manual.append("large table")
            if operation.kind == sqlplan.UNSUPPORTED:
                method = "not supported"
            elif manual:
                method = f"manual by DBA ({', '.join(manual)})"
            else:
                online = (
                    "instant DDL, else pt-online-schema-change"
                    if settings.online_change.prefer_instant
                    else "pt-online-schema-change"
                )
                method = online if operation.kind == sqlplan.ALTER_TABLE else "direct, 5s lock wait"
            report.rows.append(
                PlanRow(item.name, item.schema.label, item.environment, _describe(operation), method,
                        "; ".join(sizes) or "unknown", previous)  # fmt: skip
            )

    if report.dba:
        if request is None and not in_ci:
            report.dba.append("DBA approval will be required on the merge request (cannot be checked outside GitLab).")
        else:
            try:
                approvers = request.approvers() if request else set()
            except GovernanceError as exc:
                approvers = set()
                report.dba.append(f"Cannot read approvals: {exc}")
            dba = approvers & set(settings.dba_approvers)
            if dba:
                report.dba.append(f"Approved by DBA: {', '.join(sorted(dba))}.")
            else:
                report.blocked.append("A member of the DBA team must approve this merge request, then re-run this job.")
    return report


def render_text(report: Report) -> str:
    lines = ["== Governance ==", *report.issues]
    lines.append(f"{len(report.issues)} issue(s); {report.added} new migration(s) in this merge request.")

    lines.append("\n== Lint ==")
    lines.extend(finding.format(path) for path, finding, _blocking in report.findings)
    note = f" (label `{report.bypass_label}` set: warn and advise findings do not block)" if report.bypass else ""
    lines.append(f"{len(report.findings)} finding(s), {report.blocking_findings} blocking{note}.")
    if report.blocking_findings and not report.bypass:
        lines.append(
            f"Fix the findings, or add the `{report.bypass_label}` label to accept warn/advise findings. Errors always block."
        )

    lines.append("\n== Plan ==")
    for row in report.rows:
        lines.append(f"{row.schema} {row.environment.upper()} {row.migration}")
        lines.append(f"    {row.change} | {row.method} | size: {row.size} | previous: {row.previous}")
    if not report.rows:
        lines.append("No new migrations.")

    lines.append("\n== DBA approval ==")
    lines.extend(report.dba or ["Not required."])

    lines.append("\n== Promotion ==")
    lines.extend(report.promotion or ([] if report.blocked else ["No promotion in this merge request."]))

    lines.extend(f"BLOCKED: {message}" for message in report.blocked)
    lines.append(f"\nResult: {'PASSED' if report.passed else 'FAILED'} ({report.problems} blocking problem(s)).")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Report) -> str:
    """The merge request comment. Starts with a marker so later runs update it in place."""
    status = "passed" if report.passed else f"failed ({report.problems} blocking problem(s))"
    lines = [MARKER, f"### schemaops checks: {status}", ""]
    if report.rows:
        lines += [
            "| Migration | Schema | Env | Change | How it will run | Table now | Previous environment |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{row.migration}` | {row.schema} | {row.environment.upper()} | {_cell(row.change)} | "
            f"{_cell(row.method)} | {_cell(row.size)} | {_cell(row.previous)} |"
            for row in report.rows
        ]
    else:
        lines.append("No new migrations in this merge request.")
    if report.blocked:
        lines += ["", "**Blocked**", *[f"- {_cell(message)}" for message in report.blocked]]
    if report.issues:
        lines += ["", f"**Governance: {len(report.issues)} issue(s)**", *[f"- {_cell(issue)}" for issue in report.issues]]
    if report.dba:
        lines += ["", "**DBA team**", *[f"- {_cell(line)}" for line in report.dba]]
    if report.promotion:
        lines += ["", "**Promotion**", *[f"- {_cell(line)}" for line in report.promotion]]

    lines += ["", f"**Lint: {len(report.findings)} finding(s), {report.blocking_findings} blocking**"]
    if report.bypass:
        lines.append(f"Label `{report.bypass_label}` is set: warn and advise findings are accepted.")
    elif report.blocking_findings:
        lines.append(f"Fix the findings, or add the `{report.bypass_label}` label to accept warn and advise findings. Errors always block.")
    if report.findings:
        lines += ["", "<details><summary>Findings</summary>", ""]
        for path, finding, blocking in report.findings[:50]:
            mark = "blocks" if blocking else "accepted"
            lines.append(f"- `{path}:{finding.line}` **{finding.severity.value}** `{finding.rule_id}` ({mark}): {_cell(finding.message)}")
            if finding.alternative:
                lines.append(f"  - Instead: {_cell(finding.alternative)}")
        if len(report.findings) > 50:
            lines.append(f"- ...and {len(report.findings) - 50} more in the job log.")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"
