"""Database standards enforced on every migration.

Each rule states *why* it exists and what to do instead, so a finding is
actionable without asking the DBA team. Severities:

* error  - always fails the merge request
* warn   - fails unless the merge request carries the bypass label
* advise - a recommendation; blocking is configurable (see schemaops.yaml)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from schemaops import sqlplan
from schemaops.lint.model import Context, Finding, Severity, build_context
from schemaops.lint.parser import (
    NON_BIGINT_INTEGER_TYPES,
    INTEGER_TYPES,
    CreateTable,
    mask_sql,
    line_number_for_offset,
    split_top_level,
)
from schemaops.lint.syntax import (
    BLOB_TYPES,
    CHAR_TYPES,
    DISPLAY_WIDTH_TYPES,
    FLOAT_TYPES,
    ROUTINE_RE,
    TEXT_TYPES,
    rule_syntax,
)

Hit = tuple[int, str]  # (offset in the file, specific message)
CheckFn = Callable[[Context], list[Hit]]

ENGINE_RE = re.compile(r"\bengine\s*=?\s*['\"`]?", re.IGNORECASE)
CHARSET_RE = re.compile(r"\b(?:character\s+set|charset)\s*=?\s*['\"`]?", re.IGNORECASE)
COLLATE_RE = re.compile(r"\bcollate\s*=?\s*['\"`]?", re.IGNORECASE)
NAME_RE = re.compile(r"[A-Za-z0-9_]+")
SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ADD_COLUMN_RE = re.compile(
    r"add\s+(?:column\s+)?(?!(?:constraint|primary|unique|index|key|foreign|fulltext|spatial|check|partition)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    summary: str
    reason: str
    alternative: str
    check: CheckFn


def _pk_columns(table: CreateTable) -> set[str]:
    names = {column.name for column in table.columns if column.is_primary_key}
    names.update(table.table_primary_key)
    return names


def _column_types(types: set[str], label: str) -> CheckFn:
    def check(context: Context) -> list[Hit]:
        return [
            (column.offset, f"Column `{column.name}` uses {label}.")
            for column in context.columns
            if column.type_name in types
        ]

    return check


def _named_values(context: Context, regex: re.Pattern[str], offset: int, text: str) -> list[tuple[int, str]]:
    values = []
    for match in regex.finditer(text):
        name = NAME_RE.match(context.sql, offset + match.end())
        if name:
            values.append((offset + match.start(), name.group(0).lower()))
    return values


def _attribute_segments(context: Context) -> list[tuple[int, str]]:
    segments = [(column.tail_offset, column.tail) for column in context.columns]
    segments.extend((table.options_offset, table.options) for table in context.creates)
    for alter in context.alters:
        segments.extend(alter.other_specs)
    return segments


def _alter_specs(context: Context) -> list[tuple[int, str]]:
    """Every comma-separated clause of every ALTER TABLE, with its file offset."""
    from schemaops.lint.parser import ALTER_TABLE_RE

    specs: list[tuple[int, str]] = []
    for index, statement in enumerate(context.statements):
        match = ALTER_TABLE_RE.match(statement.text)
        if index in context.broken or not match:
            continue
        base = statement.offset + match.end()
        for raw_offset, raw in split_top_level(statement.text[match.end() :]):
            lead = len(raw) - len(raw.lstrip())
            if raw.strip():
                specs.append((base + raw_offset + lead, raw.strip()))
    return specs


# ---------------------------------------------------------------- errors


def check_one_statement(context: Context) -> list[Hit]:
    if len(context.statements) <= 1:
        return []
    second = context.statements[1]
    return [(second.offset, f"The file contains {len(context.statements)} statements; only one is allowed.")]


def check_supported(context: Context) -> list[Hit]:
    return [
        (_offset_of_line(context, operation.line), f"{operation.reason}.")
        for operation in sqlplan.parse_operations(context.sql)
        if operation.kind == sqlplan.UNSUPPORTED
    ]


def check_schema_qualifier(context: Context) -> list[Hit]:
    return [
        (_offset_of_line(context, operation.line), f"`{operation.table}` is written with a schema name.")
        for operation in sqlplan.parse_operations(context.sql)
        if operation.qualified
    ]


def _offset_of_line(context: Context, line: int) -> int:
    offset = 0
    for _ in range(line - 1):
        offset = context.masked.find("\n", offset) + 1
    return offset


def check_primary_key(context: Context) -> list[Hit]:
    return [
        (table.offset, f"Table `{table.name}` has no PRIMARY KEY.")
        for table in context.creates
        if table.has_body and not table.primary_keys and not _pk_columns(table)
    ]


def check_bigint_primary_key(context: Context) -> list[Hit]:
    hits: list[Hit] = []
    for table in context.creates:
        pk = _pk_columns(table)
        hits.extend(
            (column.offset, f"Primary key `{table.name}.{column.name}` is {column.type_name.upper()}.")
            for column in table.columns
            if column.name in pk and column.type_name in NON_BIGINT_INTEGER_TYPES
        )
    for alter in context.alters:
        hits.extend(
            (column.offset, f"Primary key `{alter.name}.{column.name}` is {column.type_name.upper()}.")
            for column in alter.columns
            if column.is_primary_key and column.type_name in NON_BIGINT_INTEGER_TYPES
        )
    return hits


def check_auto_increment(context: Context) -> list[Hit]:
    return [
        (column.offset, f"AUTO_INCREMENT column `{column.name}` is not UNSIGNED.")
        for column in context.columns
        if column.auto_increment and column.type_name in INTEGER_TYPES - {"serial"} and not column.unsigned
    ]


def check_engine(context: Context) -> list[Hit]:
    segments = [(table.options_offset, table.options) for table in context.creates if table.has_body]
    for alter in context.alters:
        segments.extend(spec for spec in alter.other_specs if ENGINE_RE.match(spec[1]))
    return [
        (position, f"Table engine `{engine}` is not allowed.")
        for offset, text in segments
        for position, engine in _named_values(context, ENGINE_RE, offset, text)
        if engine != "innodb"
    ]


def check_foreign_keys(context: Context) -> list[Hit]:
    offsets = [
        column.offset for column in context.columns if re.search(r"\breferences\b", column.tail, re.IGNORECASE)
    ]
    for table in context.creates:
        offsets.extend(o for o, text in table.constraints if re.search(r"\bforeign\s+key\b", text, re.IGNORECASE))
    offsets.extend(
        o
        for o, text in _alter_specs(context)
        if re.match(r"add\b", text, re.IGNORECASE) and re.search(r"\bforeign\s+key\b", text, re.IGNORECASE)
    )
    return [(offset, "FOREIGN KEY constraint.") for offset in offsets]


def check_check_constraints(context: Context) -> list[Hit]:
    offsets = [column.offset for column in context.columns if re.search(r"\bcheck\s*\(", column.tail, re.IGNORECASE)]
    pattern = re.compile(r"(?:constraint\s+\S*\s*)?check\s*\(", re.IGNORECASE)
    for table in context.creates:
        offsets.extend(o for o, text in table.constraints if pattern.match(text))
    offsets.extend(
        o for o, text in _alter_specs(context) if re.match(r"add\s+(?:constraint\s+\S*\s*)?check\s*\(", text, re.IGNORECASE)
    )
    return [(offset, "CHECK constraint.") for offset in offsets]


def check_charset(context: Context) -> list[Hit]:
    hits: list[Hit] = []
    for offset, text in _attribute_segments(context):
        hits.extend(
            (position, f"Character set `{name}`.")
            for position, name in _named_values(context, CHARSET_RE, offset, text)
            if name != "utf8mb4"
        )
        hits.extend(
            (position, f"Collation `{name}`.")
            for position, name in _named_values(context, COLLATE_RE, offset, text)
            if not name.startswith("utf8mb4")
        )
    return hits


def check_routines(context: Context) -> list[Hit]:
    hits: list[Hit] = []
    for statement in context.statements:
        match = ROUTINE_RE.match(statement.text)
        if match:
            hits.append((statement.offset, f"CREATE {match.group(1).upper()}."))
    return hits


def check_add_not_null_without_default(context: Context) -> list[Hit]:
    hits: list[Hit] = []
    for offset, spec in _alter_specs(context):
        if not ADD_COLUMN_RE.match(spec) or spec.lower().startswith(("add (", "add column (")):
            continue
        lowered = spec.lower()
        if re.search(r"\bnot\s+null\b", lowered) and not re.search(r"\b(?:default|auto_increment)\b", lowered):
            hits.append((offset, "New NOT NULL column has no DEFAULT."))
    return hits


# ----------------------------------------------------------------- warns


def check_varchar_width(context: Context) -> list[Hit]:
    return [
        (column.offset, f"Column `{column.name}` is {column.type_name.upper()}({column.type_args}).")
        for column in context.columns
        if column.type_name in CHAR_TYPES and column.type_args.isdigit() and int(column.type_args) >= 256
    ]


def check_uuid_primary_key(context: Context) -> list[Hit]:
    hits: list[Hit] = []
    for table in context.creates:
        pk = _pk_columns(table)
        for column in table.columns:
            if column.name not in pk:
                continue
            textual = column.type_name in CHAR_TYPES and (
                column.type_args in ("32", "36") or re.search(r"\buuid\w*\s*\(", column.tail, re.IGNORECASE)
            )
            binary = column.type_name in ("binary", "uuid") and (
                column.type_name == "uuid" or re.search(r"\buuid\w*\s*\(", column.tail, re.IGNORECASE)
            )
            if textual or binary:
                hits.append((column.offset, f"Primary key `{table.name}.{column.name}` is a UUID."))
    return hits


def check_generated_columns(context: Context) -> list[Hit]:
    pattern = re.compile(r"\b(?:generated\s+always\s+)?as\s*\(|\b(?:virtual|persistent|stored)\b", re.IGNORECASE)
    return [
        (column.offset, f"Column `{column.name}` is a generated (virtual) column.")
        for column in context.columns
        if pattern.search(column.tail)
    ]


def check_destructive(context: Context) -> list[Hit]:
    hits = [
        (_offset_of_line(context, operation.line), f"DROP TABLE `{', '.join(operation.tables)}`.")
        for operation in sqlplan.parse_operations(context.sql)
        if operation.kind == sqlplan.DROP_TABLE
    ]
    hits.extend(
        (offset, "DROP COLUMN.")
        for offset, spec in _alter_specs(context)
        if re.match(r"drop\s+(?:column\s+)?(?!(?:index|key|primary|foreign|constraint|check|partition)\b)", spec, re.IGNORECASE)
    )
    return hits


def check_rename(context: Context) -> list[Hit]:
    hits = [
        (_offset_of_line(context, operation.line), "RENAME TABLE.")
        for operation in sqlplan.parse_operations(context.sql)
        if operation.kind == sqlplan.RENAME_TABLE
    ]
    for offset, spec in _alter_specs(context):
        if re.match(r"rename\s+column\b", spec, re.IGNORECASE):
            hits.append((offset, "RENAME COLUMN."))
            continue
        change = re.match(r"change\s+(?:column\s+)?(`[^`]+`|\S+)\s+(`[^`]+`|\S+)", spec, re.IGNORECASE)
        if change and change.group(1).strip("`").lower() != change.group(2).strip("`").lower():
            hits.append((offset, f"CHANGE renames column {change.group(1)} to {change.group(2)}."))
    return hits


def check_add_unique(context: Context) -> list[Hit]:
    return [
        (_offset_of_line(context, operation.line), f"Adds a UNIQUE index to existing table `{operation.table}`.")
        for operation in sqlplan.parse_operations(context.sql)
        if operation.adds_unique_index
    ]


def check_zerofill(context: Context) -> list[Hit]:
    return [
        (column.offset, f"Column `{column.name}` uses ZEROFILL.")
        for column in context.columns
        if re.search(r"\bzerofill\b", column.tail, re.IGNORECASE)
    ]


# --------------------------------------------------------------- advises


def check_display_width(context: Context) -> list[Hit]:
    return [
        (column.offset, f"Column `{column.name}` is {column.type_name.upper()}({column.type_args}).")
        for column in context.columns
        if column.type_name in DISPLAY_WIDTH_TYPES
        and column.type_args.isdigit()
        and not (column.type_name == "tinyint" and column.type_args == "1")
    ]


def check_naming(context: Context) -> list[Hit]:
    hits = [
        (table.offset, f"Table name `{table.name}` is not lower snake_case.")
        for table in context.creates
        if not SNAKE_CASE_RE.match(_raw_name(context, table))
    ]
    hits.extend(
        (column.offset, f"Column name `{name}` is not lower snake_case.")
        for column in context.columns
        if not SNAKE_CASE_RE.match(name := _raw_column_name(context, column.offset))
    )
    return hits


def _raw_name(context: Context, table: CreateTable) -> str:
    from schemaops.lint.parser import CREATE_TABLE_NAME_RE

    match = CREATE_TABLE_NAME_RE.match(context.sql, table.offset)
    raw = match.group("name") if match else table.name
    return re.findall(r"`([^`]+)`|([\w$]+)", raw)[-1][0] or re.findall(r"`([^`]+)`|([\w$]+)", raw)[-1][1]


def _raw_column_name(context: Context, offset: int) -> str:
    match = re.match(r"`([^`]+)`|([\w$]+)", context.sql[offset:])
    return (match.group(1) or match.group(2)) if match else ""


RULES: tuple[Rule, ...] = (
    Rule(
        "ONE_STATEMENT", Severity.ERROR, "One schema change per migration file",
        "DDL cannot be rolled back. With one statement per file a migration either applied or it "
        "did not, so a failure never leaves a half-applied file to untangle.",
        "Split the change into several migration files, one statement each.",
        check_one_statement,
    ),
    Rule(
        "UNSUPPORTED_STATEMENT", Severity.ERROR, "Only online-safe schema changes are allowed",
        "Migrations are executed automatically and without blocking traffic. Data changes, grants, "
        "session settings and partition or tablespace maintenance cannot be run that way.",
        "Use CREATE TABLE, ALTER TABLE, CREATE/DROP INDEX, RENAME TABLE or DROP TABLE. Ask the DBA "
        "team to run anything else manually.",
        check_supported,
    ),
    Rule(
        "NO_SCHEMA_QUALIFIER", Severity.ERROR, "Do not write the schema name in a migration",
        "A migration applies to the schema whose folder it lives in. A hard-coded schema name can "
        "send the change to a different database.",
        "Write `users`, not `accounts.users`.",
        check_schema_qualifier,
    ),
    Rule(
        "REQUIRE_PRIMARY_KEY", Severity.ERROR, "Every table must have a primary key",
        "InnoDB stores rows in primary-key order; without one it invents a hidden key that hurts "
        "replication performance. pt-online-schema-change also needs a key to copy rows.",
        "Add `id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY`, or a natural key.",
        check_primary_key,
    ),
    Rule(
        "BIGINT_PRIMARY_KEY", Severity.ERROR, "Integer primary keys must be BIGINT",
        "INT runs out at about 2.1 billion (4.2 billion unsigned). Widening a primary key later "
        "means rebuilding the table and every table that refers to it.",
        "Use BIGINT UNSIGNED.",
        check_bigint_primary_key,
    ),
    Rule(
        "AUTO_INCREMENT_UNSIGNED", Severity.ERROR, "AUTO_INCREMENT columns must be UNSIGNED",
        "AUTO_INCREMENT never produces negative values, so a signed column wastes half its range.",
        "Declare the column as BIGINT UNSIGNED.",
        check_auto_increment,
    ),
    Rule(
        "ENGINE_INNODB", Severity.ERROR, "Only the InnoDB engine is allowed",
        "InnoDB gives transactions, row-level locking and crash recovery. MyISAM, MEMORY and others "
        "lock whole tables, lose data on a crash, or break replication and backups.",
        "Use ENGINE=InnoDB, or leave ENGINE out (InnoDB is the server default).",
        check_engine,
    ),
    Rule(
        "NO_FOREIGN_KEY", Severity.ERROR, "Foreign key constraints are not allowed",
        "Every write must check the parent table, taking extra locks; foreign keys also block "
        "online schema changes and make sharding or moving a table much harder.",
        "Keep the indexed reference column and enforce the relationship in the application.",
        check_foreign_keys,
    ),
    Rule(
        "NO_CHECK_CONSTRAINT", Severity.ERROR, "CHECK constraints are not allowed",
        "Business rules in the schema are evaluated on every write and must be changed with a "
        "table rebuild; their support also differs between MariaDB and MySQL versions.",
        "Validate values in the application.",
        check_check_constraints,
    ),
    Rule(
        "CHARSET_UTF8MB4", Severity.ERROR, "Use utf8mb4 for character sets and collations",
        "`utf8` in MariaDB/MySQL stores at most 3 bytes per character and silently rejects emoji "
        "and some CJK characters. Mixed character sets also prevent index use in joins.",
        "Use CHARACTER SET utf8mb4 with a utf8mb4 collation, or omit both to inherit the default.",
        check_charset,
    ),
    Rule(
        "NO_STORED_ROUTINES", Severity.ERROR, "No views, triggers, functions, procedures or events",
        "Logic hidden in the database is hard to test, version and observe. Triggers also conflict "
        "with pt-online-schema-change, which uses triggers of its own.",
        "Implement the behaviour in application code.",
        check_routines,
    ),
    Rule(
        "ADD_NOT_NULL_NEEDS_DEFAULT", Severity.ERROR, "A new NOT NULL column needs a DEFAULT",
        "Existing rows need a value for the new column. pt-online-schema-change refuses the change "
        "without a default, and writes from application versions that do not know the column fail.",
        "Add a DEFAULT, or add the column as NULL, backfill it, then tighten it in a later migration.",
        check_add_not_null_without_default,
    ),
    Rule(
        "NO_ENUM", Severity.WARN, "Avoid ENUM and SET columns",
        "Changing the list of values is a schema change, values are stored as hidden integers that "
        "sort unexpectedly, and an invalid value can be stored as an empty string.",
        "Use a VARCHAR validated by the application, or a small lookup table.",
        _column_types({"enum", "set"}, "ENUM/SET"),
    ),
    Rule(
        "VARCHAR_WIDTH", Severity.WARN, "Avoid VARCHAR(256) or wider",
        "Above 255 characters the length prefix takes 2 bytes, in-memory temporary tables and sort "
        "buffers reserve the full declared width, and index prefixes are limited.",
        "Size the column to the real maximum (255 or less), or use TEXT for long free-form text.",
        check_varchar_width,
    ),
    Rule(
        "UUID_PRIMARY_KEY", Severity.WARN, "Avoid UUID primary keys",
        "Random UUIDs insert all over the clustered index (page splits, fragmentation, cold cache), "
        "and the wide key is copied into every secondary index.",
        "Use BIGINT UNSIGNED AUTO_INCREMENT; give each cluster node its own auto_increment_offset "
        "with a shared auto_increment_increment if data must later be merged. If a UUID is required "
        "externally, keep it in a separate UNIQUE column.",
        check_uuid_primary_key,
    ),
    Rule(
        "NO_VIRTUAL_COLUMN", Severity.WARN, "Avoid generated (virtual) columns",
        "The expression is evaluated by the database on every read or write, it cannot be changed "
        "without a rebuild, and generated columns restrict online schema change and replication.",
        "Compute the value in the application and store it in a normal column.",
        check_generated_columns,
    ),
    Rule(
        "DESTRUCTIVE_CHANGE", Severity.WARN, "DROP TABLE and DROP COLUMN destroy data",
        "The data cannot be recovered without a restore, and application versions still reading "
        "the table or column fail immediately.",
        "First release code that no longer uses it, confirm a backup, then drop in a later migration.",
        check_destructive,
    ),
    Rule(
        "RENAME", Severity.WARN, "Renaming a table or column breaks running code",
        "Old and new application versions run side by side during a deploy; a rename breaks whichever "
        "one uses the other name. Renames through pt-online-schema-change can also lose data.",
        "Add the new column or table, write to both, migrate readers, then drop the old one.",
        check_rename,
    ),
    Rule(
        "ADD_UNIQUE_INDEX", Severity.WARN, "Adding a UNIQUE index to an existing table needs a DBA",
        "pt-online-schema-change copies rows with INSERT IGNORE, so existing duplicates would be "
        "silently deleted. schemaops therefore never applies this change automatically.",
        "Check for duplicates first; the DBA team applies the change manually.",
        check_add_unique,
    ),
    Rule(
        "NO_ZEROFILL", Severity.WARN, "ZEROFILL is deprecated",
        "It is display formatting stored in the schema, deprecated in MySQL 8.0.17, and it silently "
        "makes the column UNSIGNED.",
        "Pad values in the application.",
        check_zerofill,
    ),
    Rule(
        "PREFER_DECIMAL", Severity.ADVISE, "FLOAT and DOUBLE are approximate",
        "Binary floating point cannot represent most decimal fractions, so sums and comparisons of "
        "money or quantities drift.",
        "Use DECIMAL(p,s) for exact values; keep FLOAT/DOUBLE for measurements only.",
        _column_types(FLOAT_TYPES, "an approximate floating-point type"),
    ),
    Rule(
        "NO_TIMESTAMP", Severity.ADVISE, "Prefer DATETIME to TIMESTAMP",
        "TIMESTAMP ends in January 2038 and is converted using the session time zone, so the same "
        "row can read differently from two connections.",
        "Use DATETIME (or DATETIME(6)) and store UTC.",
        _column_types({"timestamp"}, "TIMESTAMP"),
    ),
    Rule(
        "NO_JSON", Severity.ADVISE, "Use JSON columns sparingly",
        "JSON hides structure from the schema: values cannot be indexed directly, validated or "
        "migrated, and whole documents are rewritten on update.",
        "Use typed columns for anything you filter, join or sort on.",
        _column_types({"json"}, "JSON"),
    ),
    Rule(
        "NO_TEXT_BLOB", Severity.ADVISE, "Use TEXT and BLOB sparingly",
        "Large values are stored off-page, force on-disk temporary tables and bloat backups and "
        "replication.",
        "Use a sized VARCHAR where a limit exists; keep large binaries in object storage.",
        _column_types(TEXT_TYPES | BLOB_TYPES, "a large TEXT/BLOB type"),
    ),
    Rule(
        "NO_INT_DISPLAY_WIDTH", Severity.ADVISE, "Integer display widths are deprecated",
        "INT(11) does not limit the stored range; the width is ignored and deprecated in MySQL 8.0.17.",
        "Write INT, BIGINT and so on without a width (TINYINT(1) for booleans is fine).",
        check_display_width,
    ),
    Rule(
        "NAMING", Severity.ADVISE, "Use lower snake_case names",
        "Identifier case sensitivity differs between operating systems (lower_case_table_names), so "
        "mixed-case names break when a schema moves between hosts.",
        "Use names such as `user_accounts` and `created_at`.",
        check_naming,
    ),
)  # fmt: skip

RULES_BY_ID = {rule.id: rule for rule in RULES}


def lint_sql(sql: str, *, initial_dump: bool = False) -> list[Finding]:
    """Lint one migration file.

    The initial dump records a schema that already exists, so its standards findings are
    reported as advice only; it must still be valid, schema-only SQL.
    """
    masked, lex_errors = mask_sql(sql)
    if lex_errors:
        return [
            Finding("SYNTAX_ERROR", Severity.ERROR, line_number_for_offset(sql, error.offset), error.message)
            for error in lex_errors
        ]
    context = build_context(sql, masked)
    findings = list(rule_syntax(context))

    if initial_dump:
        findings.extend(
            Finding(
                "INITIAL_DUMP_SCHEMA_ONLY",
                Severity.ERROR,
                operation.line,
                "The initial dump may contain only CREATE TABLE statements, without data.",
                "It is the structural baseline that later migrations build on.",
                "Create it with `mysqldump --no-data --skip-add-drop-table --skip-comments`.",
            )
            for operation in sqlplan.parse_operations(sql)
            if operation.kind != sqlplan.CREATE_TABLE
        )

    for rule in RULES:
        if initial_dump and rule.id in ("ONE_STATEMENT", "UNSUPPORTED_STATEMENT"):
            continue
        severity = Severity.ADVISE if initial_dump else rule.severity
        for offset, message in rule.check(context):
            findings.append(
                Finding(rule.id, severity, context.line(offset), message, rule.reason, rule.alternative)
            )

    unique = list(dict.fromkeys(findings))
    unique.sort(key=lambda finding: (finding.line, finding.rule_id))
    return unique
