import pytest

from schemaops.lint.model import Severity
from schemaops.lint.rules import RULES, lint_sql


def found(sql: str, **kwargs) -> dict[str, str]:
    return {finding.rule_id: finding.severity.value for finding in lint_sql(sql, **kwargs)}


def table(column: str = "name VARCHAR(64) NOT NULL", options: str = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4") -> str:
    return f"CREATE TABLE t (\n  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n  {column},\n  PRIMARY KEY (id)\n) {options};"


def test_clean_table_and_default_engine_pass():
    assert lint_sql(table()) == []
    assert lint_sql(table(options="")) == []


@pytest.mark.parametrize(
    ("sql", "rule", "level"),
    [
        ("CREATE TABLE t (name VARCHAR(10));", "REQUIRE_PRIMARY_KEY", "error"),
        ("CREATE TABLE t (id INT UNSIGNED PRIMARY KEY);", "BIGINT_PRIMARY_KEY", "error"),
        ("CREATE TABLE t (id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY);", "AUTO_INCREMENT_UNSIGNED", "error"),
        (table(options="ENGINE=MyISAM"), "ENGINE_INNODB", "error"),
        (table(options="ENGINE=ROCKSDB"), "ENGINE_INNODB", "error"),
        ("ALTER TABLE t ENGINE=MEMORY;", "ENGINE_INNODB", "error"),
        (table("user_id BIGINT UNSIGNED, FOREIGN KEY (user_id) REFERENCES users (id)"), "NO_FOREIGN_KEY", "error"),
        ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id);", "NO_FOREIGN_KEY", "error"),
        (table("age INT, CONSTRAINT chk CHECK (age > 0)"), "NO_CHECK_CONSTRAINT", "error"),
        (table("age INT CHECK (age > 0)"), "NO_CHECK_CONSTRAINT", "error"),
        (table(options="ENGINE=InnoDB DEFAULT CHARSET=latin1"), "CHARSET_UTF8MB4", "error"),
        ("CREATE VIEW v AS SELECT 1;", "NO_STORED_ROUTINES", "error"),
        ("CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW SET NEW.a = 1;", "NO_STORED_ROUTINES", "error"),
        ("ALTER TABLE t ADD COLUMN age INT NOT NULL;", "ADD_NOT_NULL_NEEDS_DEFAULT", "error"),
        ("INSERT INTO t (a) VALUES (1);", "UNSUPPORTED_STATEMENT", "error"),
        ("ALTER TABLE t ADD PARTITION (PARTITION p1 VALUES LESS THAN (10));", "UNSUPPORTED_STATEMENT", "error"),
        ("ALTER TABLE accounts.t ADD COLUMN a BIGINT NULL;", "NO_SCHEMA_QUALIFIER", "error"),
        ("ALTER TABLE t ADD COLUMN a BIGINT NULL; ALTER TABLE t ADD COLUMN b BIGINT NULL;", "ONE_STATEMENT", "error"),
        ("ALTER TABLE t ADD COLUMN a BIGINT NUL;", "SYNTAX_ERROR", "error"),
        (table("status ENUM('a','b')"), "NO_ENUM", "warn"),
        (table("flags SET('a','b')"), "NO_ENUM", "warn"),
        (table("note VARCHAR(256)"), "VARCHAR_WIDTH", "warn"),
        ("CREATE TABLE t (id CHAR(36) PRIMARY KEY DEFAULT (UUID()));", "UUID_PRIMARY_KEY", "warn"),
        ("CREATE TABLE t (id BINARY(16) PRIMARY KEY DEFAULT (UUID_TO_BIN(UUID(), 1)));", "UUID_PRIMARY_KEY", "warn"),
        (table("total BIGINT AS (id * 2) VIRTUAL"), "NO_VIRTUAL_COLUMN", "warn"),
        (table("total BIGINT GENERATED ALWAYS AS (id * 2) STORED"), "NO_VIRTUAL_COLUMN", "warn"),
        ("DROP TABLE t;", "DESTRUCTIVE_CHANGE", "warn"),
        ("ALTER TABLE t DROP COLUMN a;", "DESTRUCTIVE_CHANGE", "warn"),
        ("RENAME TABLE a TO b;", "RENAME", "warn"),
        ("ALTER TABLE t CHANGE old_name new_name BIGINT NULL;", "RENAME", "warn"),
        ("ALTER TABLE t ADD UNIQUE KEY uq_a (a);", "ADD_UNIQUE_INDEX", "warn"),
        ("CREATE UNIQUE INDEX uq_a ON t (a);", "ADD_UNIQUE_INDEX", "warn"),
        (table("price FLOAT"), "PREFER_DECIMAL", "advise"),
        (table("seen_at TIMESTAMP NULL"), "NO_TIMESTAMP", "advise"),
        (table("payload JSON"), "NO_JSON", "advise"),
        (table("body TEXT"), "NO_TEXT_BLOB", "advise"),
        (table("n INT(11)"), "NO_INT_DISPLAY_WIDTH", "advise"),
        (table("userName VARCHAR(10)"), "NAMING", "advise"),
    ],
)
def test_rule_fires_at_its_level(sql: str, rule: str, level: str):
    assert found(sql).get(rule) == level, lint_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        table("note VARCHAR(255)"),
        table("is_active TINYINT(1) NOT NULL DEFAULT 0"),
        table("note VARCHAR(64) COMMENT 'enum, json, foreign key references'"),
        "ALTER TABLE t ADD COLUMN age INT NOT NULL DEFAULT 0;",
        "ALTER TABLE t ADD COLUMN age INT NULL, ADD INDEX idx_age (age);",
        "ALTER TABLE t DROP INDEX idx_age;",
        "ALTER TABLE t DROP FOREIGN KEY fk_old;",
        "ALTER TABLE t CHANGE age age BIGINT NULL;",
        "CREATE INDEX idx_a ON t (a);",
        "DROP INDEX idx_a ON t;",
    ],
)
def test_acceptable_changes_have_no_findings(sql: str):
    assert lint_sql(sql) == []


def test_every_rule_explains_itself_and_findings_carry_the_explanation():
    for rule in RULES:
        assert rule.reason and rule.alternative and rule.summary, rule.id
    finding = lint_sql(table("status ENUM('a','b')"))[0]
    text = finding.format("x.sql")
    assert "x.sql:3: warn: [NO_ENUM]" in text and "why:" in text and "instead:" in text


def test_initial_dump_is_schema_only_and_its_standards_findings_are_advice():
    dump = "CREATE TABLE legacy (id INT, status ENUM('a')) ENGINE=MyISAM;\nCREATE TABLE b (id BIGINT UNSIGNED PRIMARY KEY);"
    findings = lint_sql(dump, initial_dump=True)
    assert findings and {finding.severity for finding in findings} == {Severity.ADVISE}
    assert found("CREATE TABLE a (id BIGINT UNSIGNED PRIMARY KEY);\nINSERT INTO a VALUES (1);", initial_dump=True) == {
        "INITIAL_DUMP_SCHEMA_ONLY": "error"
    }
    assert found("CREATE TABLE a (id BIGINT UNSIGNED PRIMARY KEY, n INT NOT NUL);", initial_dump=True)["SYNTAX_ERROR"] == "error"
