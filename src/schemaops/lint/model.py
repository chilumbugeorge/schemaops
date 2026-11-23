"""Shared lint types: severities, findings and the parsed file context."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from schemaops.lint.parser import (
    AlterTable,
    ColumnDef,
    CreateTable,
    Statement,
    line_number_for_offset,
    paren_errors,
    parse_alter_table,
    parse_create_table,
    split_statements,
)


class Severity(str, Enum):
    ERROR = "error"  # always fails the merge request
    WARN = "warn"  # fails unless the bypass label is set
    ADVISE = "advise"  # fails unless the bypass label is set (configurable)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: Severity
    line: int
    message: str
    reason: str = ""
    alternative: str = ""

    def format(self, path: str) -> str:
        text = f"{path}:{self.line}: {self.severity.value}: [{self.rule_id}] {self.message}"
        if self.reason:
            text += f"\n    why: {self.reason}"
        if self.alternative:
            text += f"\n    instead: {self.alternative}"
        return text


@dataclass
class Context:
    """One migration file, parsed once and shared by every rule."""

    sql: str
    masked: str
    statements: list[Statement] = field(default_factory=list)
    broken: set[int] = field(default_factory=set)
    creates: list[CreateTable] = field(default_factory=list)
    alters: list[AlterTable] = field(default_factory=list)

    def line(self, offset: int) -> int:
        return line_number_for_offset(self.masked, offset)

    @property
    def columns(self) -> list[ColumnDef]:
        columns = [column for table in self.creates for column in table.columns]
        columns.extend(column for alter in self.alters for column in alter.columns)
        return columns


def build_context(sql: str, masked: str) -> Context:
    context = Context(sql=sql, masked=masked, statements=split_statements(masked))
    for index, statement in enumerate(context.statements):
        if paren_errors(statement.text):
            context.broken.add(index)
            continue
        create = parse_create_table(statement)
        if create is not None:
            context.creates.append(create)
            continue
        alter = parse_alter_table(statement)
        if alter is not None:
            context.alters.append(alter)
    return context
