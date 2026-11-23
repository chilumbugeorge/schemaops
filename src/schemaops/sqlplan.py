"""Turn a migration file into the list of schema operations it performs."""

from __future__ import annotations

import re
from dataclasses import dataclass

from schemaops.lint.parser import (
    ALTER_TABLE_RE,
    CREATE_TABLE_NAME_RE,
    IDENT,
    QUALIFIED_IDENT,
    Statement,
    line_number_for_offset,
    mask_sql,
    normalize_identifier,
    split_statements,
    split_top_level,
)

CREATE_TABLE = "create_table"
ALTER_TABLE = "alter_table"
DROP_TABLE = "drop_table"
RENAME_TABLE = "rename_table"
UNSUPPORTED = "unsupported"

DROP_TABLE_RE = re.compile(r"drop\s+table\s+(?:if\s+exists\s+)?(?P<names>.+)$", re.IGNORECASE | re.DOTALL)
RENAME_TABLE_RE = re.compile(r"rename\s+table\s+(?P<pairs>.+)$", re.IGNORECASE | re.DOTALL)
CREATE_INDEX_RE = re.compile(
    rf"create\s+(?P<kind>unique\s+|fulltext\s+|spatial\s+)?index\s+(?:if\s+not\s+exists\s+)?"
    rf"(?P<name>{IDENT})\s+(?:using\s+\w+\s+)?on\s+(?P<table>{QUALIFIED_IDENT})\s*(?P<rest>\(.*)$",
    re.IGNORECASE | re.DOTALL,
)
DROP_INDEX_RE = re.compile(
    rf"drop\s+index\s+(?:if\s+exists\s+)?(?P<name>{IDENT})\s+on\s+(?P<table>{QUALIFIED_IDENT})\s*$",
    re.IGNORECASE,
)
INDEX_CLAUSE_RE = re.compile(r"\s+(?:algorithm|lock)\s*=?\s*\w+", re.IGNORECASE)
# ALTER clauses pt-online-schema-change cannot perform (it rebuilds a copy of the table).
ALTER_NOT_ONLINE_RE = re.compile(
    r"^(?:rename\s+(?:to|as)\b|rename\s+(?!column|index|key)|(?:add|drop|truncate|coalesce|reorganize|"
    r"exchange|analyze|check|optimize|rebuild|repair|remove)\s+partition(?:ing)?\b|"
    r"(?:discard|import)\s+tablespace\b|partition\s+by\b)",
    re.IGNORECASE,
)
ALGORITHM_CLAUSE_RE = re.compile(r"^(?:algorithm|lock)\s*=?\s*\w+$", re.IGNORECASE)
ADD_UNIQUE_RE = re.compile(
    r"^add\s+(?:constraint\s+\S*\s*)?unique\b|^add\s+(?:column\s+)?.*\bunique\b", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class Operation:
    """One schema change statement."""

    kind: str
    tables: tuple[str, ...]  # lower-cased table names the statement touches
    sql: str  # the original statement text, without its terminator
    line: int
    alter: str = ""  # ALTER specification handed to pt-online-schema-change
    qualified: bool = False  # the statement names a schema explicitly (db.table)
    adds_unique_index: bool = False
    reason: str = ""  # why an operation is unsupported
    name: str = ""  # the table name exactly as written (ALTER only), for execution

    @property
    def table(self) -> str:
        return self.tables[0] if self.tables else ""


def _is_qualified(name: str) -> bool:
    return len(re.findall(IDENT, name)) > 1


def _as_written(name: str) -> str:
    """The last part of a possibly qualified, possibly quoted identifier, case preserved."""
    quoted, bare = re.findall(r"`((?:[^`]|``)+)`|([\w$]+)", name)[-1]
    return quoted.replace("``", "`") if quoted else bare


def _original(sql: str, statement: Statement) -> str:
    return sql[statement.offset : statement.end]


def _alter_operation(sql: str, masked: str, statement: Statement, line: int) -> Operation:
    match = ALTER_TABLE_RE.match(statement.text)
    assert match is not None
    name = match.group("name")
    original = _original(sql, statement)
    # schemaops chooses the algorithm and lock level itself, so drop any the author wrote.
    pieces = [
        (offset, spec)
        for offset, spec in split_top_level(statement.text[match.end() :])
        if spec.strip() and not ALGORITHM_CLAUSE_RE.match(spec.strip())
    ]
    specs = [spec.strip() for _offset, spec in pieces]
    alter = ",".join(original[match.end() + offset : match.end() + offset + len(spec)] for offset, spec in pieces).strip()
    common = {
        "tables": (normalize_identifier(name),),
        "sql": original,
        "line": line,
        "qualified": _is_qualified(name),
        "name": _as_written(original[match.start("name") : match.end("name")]),
    }
    for spec in specs:
        if ALTER_NOT_ONLINE_RE.match(spec):
            return Operation(
                UNSUPPORTED,
                reason=f"ALTER TABLE ... {spec.split('(')[0].strip()[:40]} cannot run through "
                "pt-online-schema-change",
                **common,
            )
    if not alter:
        return Operation(UNSUPPORTED, reason="ALTER TABLE has no change specification", **common)
    return Operation(
        ALTER_TABLE,
        alter=alter,
        adds_unique_index=any(ADD_UNIQUE_RE.match(spec) for spec in specs),
        **common,
    )


def parse_operations(sql: str) -> list[Operation]:
    """Classify every statement in a migration. Never raises on odd SQL."""
    masked, errors = mask_sql(sql)
    if errors:
        return []
    operations: list[Operation] = []
    for statement in split_statements(masked):
        text = statement.text
        line = line_number_for_offset(masked, statement.offset)
        original = _original(sql, statement)

        create = CREATE_TABLE_NAME_RE.match(text)
        if create and not re.match(r"create\s+temporary\b", text, re.IGNORECASE):
            name = create.group("name")
            operations.append(
                Operation(CREATE_TABLE, (normalize_identifier(name),), original, line, qualified=_is_qualified(name))
            )
            continue
        if ALTER_TABLE_RE.match(text):
            operations.append(_alter_operation(sql, masked, statement, line))
            continue
        drop = DROP_TABLE_RE.match(text)
        if drop:
            names = [raw.strip() for _o, raw in split_top_level(drop.group("names")) if raw.strip()]
            operations.append(
                Operation(
                    DROP_TABLE,
                    tuple(normalize_identifier(name) for name in names),
                    original,
                    line,
                    qualified=any(_is_qualified(name) for name in names),
                )
            )
            continue
        rename = RENAME_TABLE_RE.match(text)
        if rename:
            names = re.findall(QUALIFIED_IDENT, re.sub(r"\bto\b", ",", rename.group("pairs"), flags=re.IGNORECASE))
            operations.append(
                Operation(
                    RENAME_TABLE,
                    tuple(normalize_identifier(name) for name in names),
                    original,
                    line,
                    qualified=any(_is_qualified(name) for name in names),
                )
            )
            continue
        index = CREATE_INDEX_RE.match(text)
        if index:
            kind = (index.group("kind") or "").strip().upper()
            rest = INDEX_CLAUSE_RE.sub("", original[index.start("rest") :]).strip()
            name = original[index.start("name") : index.end("name")]
            operations.append(
                Operation(
                    ALTER_TABLE,
                    (normalize_identifier(index.group("table")),),
                    original,
                    line,
                    alter=" ".join(part for part in ("ADD", kind, "INDEX", name, rest) if part),
                    qualified=_is_qualified(index.group("table")),
                    adds_unique_index=kind == "UNIQUE",
                    name=_as_written(original[index.start("table") : index.end("table")]),
                )
            )
            continue
        index = DROP_INDEX_RE.match(text)
        if index:
            name = original[index.start("name") : index.end("name")]
            operations.append(
                Operation(
                    ALTER_TABLE,
                    (normalize_identifier(index.group("table")),),
                    original,
                    line,
                    alter=f"DROP INDEX {name}",
                    qualified=_is_qualified(index.group("table")),
                    name=_as_written(original[index.start("table") : index.end("table")]),
                )
            )
            continue
        keyword = text.split()[0].upper()
        operations.append(
            Operation(UNSUPPORTED, (), original, line, reason=f"{keyword} is not a supported schema change")
        )
    return operations
