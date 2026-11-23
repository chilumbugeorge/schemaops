"""Structural SQL syntax checks. Adapted from sqlsniff (rules.py)."""

from __future__ import annotations

import difflib
import re

from schemaops.lint.model import Context, Finding, Severity
from schemaops.lint.parser import (
    COLUMN_TYPE_RE,
    INTEGER_TYPES,
    ColumnDef,
    CreateTable,
    Statement,
    flatten,
    paren_errors,
)

Violation = Finding


def _syntax(context: Context, offset: int, message: str) -> Finding:
    return Finding("SYNTAX_ERROR", Severity.ERROR, context.line(offset), message)


ROUTINE_RE = re.compile(
    r"create\s+(?:or\s+replace\s+)?(?:algorithm\s*=\s*\w+\s+)?(?:definer\s*=\s*\S+\s+)?"
    r"(?:sql\s+security\s+\w+\s+)?(?:aggregate\s+)?"
    r"(view|trigger|function|procedure|event)\b",
    re.IGNORECASE,
)


TEXT_TYPES = {"text", "mediumtext", "longtext"}
BLOB_TYPES = {"blob", "mediumblob", "longblob"}
CHAR_TYPES = {"char", "varchar", "character", "nchar", "nvarchar"}
FLOAT_TYPES = {"float", "double", "real", "float4", "float8"}
DISPLAY_WIDTH_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}

KNOWN_TYPES = (
    INTEGER_TYPES
    | TEXT_TYPES
    | BLOB_TYPES
    | CHAR_TYPES
    | FLOAT_TYPES
    | {
        "bit", "int1", "int2", "int3", "int4", "int8", "middleint",
        "decimal", "dec", "numeric", "fixed",
        "date", "datetime", "timestamp", "time", "year",
        "binary", "varbinary", "tinyblob", "tinytext", "long",
        "enum", "set", "json", "vector", "uuid", "inet4", "inet6",
        "geometry", "point", "linestring", "polygon", "multipoint",
        "multilinestring", "multipolygon", "geometrycollection", "geomcollection",
    }
)  # fmt: skip

# A second data type inside one column definition almost always means a missing comma.
SECOND_TYPE_RE = re.compile(
    r"\b(tinyint|smallint|mediumint|int|integer|bigint|varchar|varbinary|decimal|numeric|"
    r"datetime|timestamp|date|tinytext|text|mediumtext|longtext|tinyblob|blob|mediumblob|"
    r"longblob|json|float|double|boolean|bool|enum)\b",
    re.IGNORECASE,
)

# Words that may follow the data type in a column definition.
COLUMN_ATTRIBUTE_WORDS = {
    "not", "null", "default", "auto_increment", "unique", "primary", "key", "comment",
    "collate", "character", "charset", "set", "unsigned", "signed", "zerofill", "binary",
    "ascii", "unicode", "byte", "generated", "always", "as", "virtual", "stored", "persistent",
    "on", "update", "visible", "invisible", "column_format", "fixed", "dynamic", "storage",
    "disk", "memory", "references", "check", "constraint", "enforced", "srid", "first", "after",
    "serial", "value", "for", "engine_attribute", "secondary_engine_attribute", "secondary",
    "compressed", "with", "without", "system", "versioning",
}  # fmt: skip
# After these words comes a free-form value (a name, literal or function), not a keyword.
VALUE_FOLLOWS = {
    "default", "collate", "set", "charset", "after", "update", "srid", "column_format",
    "storage", "for", "constraint",
}  # fmt: skip
TABLE_OPTION_WORDS = {
    "engine", "auto_increment", "autoextend_size", "avg_row_length", "charset", "checksum",
    "collate", "comment", "compression", "connection", "delay_key_write", "encryption",
    "engine_attribute", "insert_method", "key_block_size", "max_rows", "min_rows", "pack_keys",
    "password", "row_format", "secondary_engine_attribute", "stats_auto_recalc",
    "stats_persistent", "stats_sample_pages", "tablespace", "storage", "union",
    # MariaDB
    "encrypted", "encryption_key_id", "ietf_quotes", "page_checksum", "page_compressed",
    "page_compression_level", "sequence", "transactional",
}  # fmt: skip
TABLE_OPTION_PAIRS = {"character": "set", "data": "directory", "index": "directory", "start": "transaction"}
TABLE_OPTION_STOP = {"partition", "as", "select", "ignore", "replace", "with"}
WORD_RE = re.compile(r"\b[A-Za-z_][\w$]*\b")
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
OPTION_VALUE_RE = re.compile(r"\s*=?\s*(?:'[^']*'|\"[^\"]*\"|\([^)]*\)|[\w$.\-]+)?")

STATEMENT_KEYWORDS = {
    "alter", "analyze", "begin", "call", "check", "commit", "create", "deallocate", "delete",
    "desc", "describe", "do", "drop", "execute", "explain", "flush", "grant", "handler",
    "insert", "install", "load", "lock", "optimize", "prepare", "release", "rename", "repair",
    "replace", "revoke", "rollback", "savepoint", "select", "set", "show", "start", "table",
    "truncate", "uninstall", "unlock", "update", "use", "values", "with",
    # Compound-statement keywords, seen when a routine body is split on ';'.
    "case", "close", "declare", "else", "elseif", "end", "fetch", "get", "if", "iterate",
    "leave", "loop", "open", "repeat", "resignal", "return", "signal", "until", "while",
}  # fmt: skip
OBJECT_KEYWORDS = {
    "create": (
        "table|index|view|trigger|function|procedure|event|database|schema|user|role|server|"
        "tablespace|logfile|resource|spatial|undo"
    ),
    "alter": (
        "table|view|event|function|procedure|database|schema|user|server|tablespace|instance|"
        "logfile|resource|undo"
    ),
    "drop": (
        "table|tables|index|view|trigger|function|procedure|event|database|schema|user|role|"
        "server|tablespace|prepare|logfile|resource|spatial|undo"
    ),
}
EMBEDDED_STATEMENT_HEADS = {
    "alter", "create", "delete", "drop", "insert", "rename", "replace", "set", "truncate",
    "update", "use",
}  # fmt: skip
EMBEDDED_STATEMENT_RE = re.compile(
    r"\b(create\s+(?:temporary\s+)?table|create\s+(?:unique\s+)?index|alter\s+table|"
    r"drop\s+table|rename\s+table|truncate\s+table|insert\s+into|delete\s+from)\b",
    re.IGNORECASE,
)




def _suggestion(word: str, candidates: set[str]) -> str:
    close = difflib.get_close_matches(word.lower(), sorted(candidates), n=1, cutoff=0.6)
    return f" Did you mean `{close[0].upper()}`?" if close else ""


def _blank_strings(text: str) -> str:
    return QUOTED_RE.sub(lambda match: " " * len(match.group(0)), text)


def _attribute_syntax(context: Context, column: ColumnDef, type_end: int) -> list[Violation]:
    """Every bare word after the data type must be a known column attribute keyword."""
    flat = flatten(column.tail)
    bare = _blank_strings(flat)
    previous = ""
    previous_end = type_end
    for match in WORD_RE.finditer(bare, type_end):
        word = match.group(0).lower()
        if flat[match.end() : match.end() + 1] in ("'", '"'):
            previous, previous_end = "", match.end()  # literal prefix: b'0', x'1F', _utf8mb4'..'
            continue
        gap = column.tail[previous_end : match.start()].strip()
        is_value = previous in VALUE_FOLLOWS and gap in ("", "=", "+", "-")
        if is_value and not (previous == "constraint" and word == "check"):
            previous, previous_end = "", match.end()
            continue
        if word == "references":
            break  # table name and referential actions follow
        if word not in COLUMN_ATTRIBUTE_WORDS:
            return [
                _syntax(
                    context,
                    column.tail_offset + match.start(),
                    f"Unexpected `{match.group(0).upper()}` in the definition of column "
                    f"`{column.name}`.{_suggestion(word, COLUMN_ATTRIBUTE_WORDS)}",
                )
            ]
        previous, previous_end = word, match.end()
    return []


def _table_option_syntax(context: Context, table: CreateTable) -> list[Violation]:
    """Table options are `NAME [=] value` pairs; flag an unknown option name."""
    text = _blank_strings(table.options)
    position = 0
    while True:
        match = WORD_RE.search(text, position)
        if not match or text[position : match.start()].strip(" \t\r\n,"):
            return []  # nothing left, or something this check does not model
        word = match.group(0).lower()
        position = match.end()
        if word in TABLE_OPTION_STOP:
            return []
        if word == "default":
            continue
        if word in TABLE_OPTION_PAIRS:
            follower = re.compile(r"\s*([A-Za-z_]+)").match(text, position)
            if not follower or follower.group(1).lower() != TABLE_OPTION_PAIRS[word]:
                return []
            position = follower.end()
            if word == "start":
                continue
        elif word not in TABLE_OPTION_WORDS:
            candidates = TABLE_OPTION_WORDS | set(TABLE_OPTION_PAIRS) | {"default"}
            return [
                _syntax(
                    context,
                    table.options_offset + match.start(),
                    f"Unknown table option `{match.group(0).upper()}` for table "
                    f"`{table.name}`.{_suggestion(word, candidates)}",
                )
            ]
        position = OPTION_VALUE_RE.match(table.options, position).end()


def _column_syntax(context: Context, column: ColumnDef) -> list[Violation]:
    if not column.type_name:
        return [_syntax(context, column.offset, f"Column `{column.name}` has no data type.")]
    if column.type_name not in KNOWN_TYPES:
        return [
            _syntax(
                context,
                column.offset,
                f"Unknown data type `{column.type_name.upper()}` for column `{column.name}`.",
            )
        ]
    type_match = COLUMN_TYPE_RE.match(column.tail)
    type_end = type_match.end() if type_match else 0
    extra = SECOND_TYPE_RE.search(flatten(column.tail), type_end)
    if extra and column.type_name != "long":  # LONG VARCHAR / LONG VARBINARY are valid
        return [
            _syntax(
                context,
                column.tail_offset + extra.start(),
                f"Unexpected `{extra.group(1).upper()}` in the definition of column "
                f"`{column.name}` - a comma is probably missing.",
            )
        ]
    return _attribute_syntax(context, column, type_end)


def _create_table_syntax(context: Context, table: CreateTable) -> list[Violation]:
    violations: list[Violation] = []
    if not table.has_body:
        # CREATE TABLE ... LIKE other / ... [options] [AS] SELECT ... need no column list.
        if not table.remainder.startswith("(") and not re.search(
            r"\b(?:like|select)\b", flatten(table.remainder), re.IGNORECASE
        ):
            violations.append(
                _syntax(context, table.offset, f"CREATE TABLE `{table.name}` has no '(' column list.")
            )
        return violations

    if not table.columns and not table.constraints:
        return [_syntax(context, table.offset, f"CREATE TABLE `{table.name}` has no columns.")]
    violations.extend(
        _syntax(context, offset, "Empty definition - stray or trailing comma in the column list.")
        for offset in table.empty_pieces
    )

    seen: set[str] = set()
    for column in table.columns:
        violations.extend(_column_syntax(context, column))
        if column.name in seen:
            violations.append(
                _syntax(context, column.offset, f"Duplicate column name `{column.name}` in table `{table.name}`.")
            )
        seen.add(column.name)

    inline = [column for column in table.columns if column.is_primary_key]
    if len(inline) + len(table.primary_keys) > 1:
        violations.append(
            _syntax(context, table.offset, f"Table `{table.name}` defines more than one PRIMARY KEY.")
        )
    for offset, names in table.primary_keys:
        violations.extend(
            _syntax(context, offset, f"PRIMARY KEY refers to unknown column `{name}`.")
            for name in names
            if name not in seen
        )
    violations.extend(_table_option_syntax(context, table))
    return violations


def _statement_syntax(context: Context, statement: Statement) -> list[Violation]:
    violations: list[Violation] = []
    text = statement.text
    word = re.match(r"[A-Za-z_]+", text)
    keyword = word.group(0).lower() if word else ""

    if text.startswith("("):
        keyword = "select"  # parenthesised query
    if keyword not in STATEMENT_KEYWORDS:
        shown = text.split(None, 1)[0][:30]
        return [_syntax(context, statement.offset, f"Unrecognised statement starting with `{shown}`.")]

    is_routine = bool(ROUTINE_RE.match(text))
    objects = OBJECT_KEYWORDS.get(keyword)
    if objects and not is_routine:
        if not re.match(rf"{keyword}\b[^(]*?\b(?:{objects})\b", text, re.IGNORECASE):
            return [
                _syntax(
                    context,
                    statement.offset,
                    f"Unrecognised {keyword.upper()} statement - check the object keyword "
                    "(TABLE, INDEX, DATABASE...).",
                )
            ]

    if keyword in EMBEDDED_STATEMENT_HEADS and not is_routine:
        embedded = EMBEDDED_STATEMENT_RE.search(flatten(text), 1)
        if embedded:
            start = " ".join(embedded.group(1).upper().split())
            violations.append(
                _syntax(
                    context,
                    statement.offset + embedded.start(),
                    f"`{start}` begins inside another statement - a ';' is probably missing before it.",
                )
            )
    return violations


def rule_syntax(context: Context) -> list[Violation]:
    violations: list[Violation] = []
    for index, statement in enumerate(context.statements):
        if index in context.broken:
            violations.extend(
                _syntax(context, statement.offset + offset, message)
                for offset, message in paren_errors(statement.text)
            )
            continue
        violations.extend(_statement_syntax(context, statement))
        if not statement.terminated:
            violations.append(
                _syntax(context, statement.end - 1, "Statement is not terminated with ';'.")
            )

    for table in context.creates:
        violations.extend(_create_table_syntax(context, table))
    for alter in context.alters:
        for column in alter.columns:
            violations.extend(_column_syntax(context, column))

    # CREATE TABLE with no table name never reaches the parser above.
    for index, statement in enumerate(context.statements):
        if index in context.broken:
            continue
        if re.match(r"create\s+(?:temporary\s+)?table\b", statement.text, re.IGNORECASE) and not any(
            table.offset == statement.offset for table in context.creates
        ):
            violations.append(_syntax(context, statement.offset, "CREATE TABLE is missing a table name."))
    return violations

