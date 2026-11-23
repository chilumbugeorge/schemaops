from schemaops import sqlplan


def ops(sql: str) -> list[sqlplan.Operation]:
    return sqlplan.parse_operations(sql)


def test_alter_keeps_the_original_specification_for_pt_osc():
    (op,) = ops("ALTER TABLE `Users`\n  ADD COLUMN note VARCHAR(10) DEFAULT 'a, b' NULL,\n  ADD INDEX idx_note (note);")
    assert (op.kind, op.table) == (sqlplan.ALTER_TABLE, "users")
    assert op.alter == "ADD COLUMN note VARCHAR(10) DEFAULT 'a, b' NULL,\n  ADD INDEX idx_note (note)"
    assert not op.adds_unique_index
    assert op.name == "Users"  # executed exactly as written


def test_author_supplied_algorithm_and_lock_clauses_are_dropped():
    (op,) = ops("ALTER TABLE t ADD COLUMN a BIGINT NULL, ALGORITHM=INPLACE, LOCK = NONE, ADD COLUMN b BIGINT NULL;")
    assert op.alter == "ADD COLUMN a BIGINT NULL, ADD COLUMN b BIGINT NULL"
    assert ops("ALTER TABLE t ALGORITHM=INPLACE;")[0].kind == sqlplan.UNSUPPORTED


def test_index_statements_become_alter_specifications():
    (create,) = ops("CREATE UNIQUE INDEX uq_a ON t (a, b) ALGORITHM=INPLACE LOCK=NONE;")
    assert (create.kind, create.table, create.alter) == (sqlplan.ALTER_TABLE, "t", "ADD UNIQUE INDEX uq_a (a, b)")
    assert create.adds_unique_index
    (drop,) = ops("DROP INDEX idx_a ON t;")
    assert (drop.kind, drop.alter) == (sqlplan.ALTER_TABLE, "DROP INDEX idx_a")


def test_direct_statements_and_touched_tables():
    assert ops("CREATE TABLE IF NOT EXISTS t (id BIGINT UNSIGNED PRIMARY KEY);")[0].kind == sqlplan.CREATE_TABLE
    assert ops("DROP TABLE IF EXISTS a, b;")[0].tables == ("a", "b")
    assert ops("RENAME TABLE a TO b, c TO d;")[0].tables == ("a", "b", "c", "d")
    assert ops("ALTER TABLE db1.t ADD COLUMN a BIGINT NULL;")[0].qualified


def test_unsupported_statements_are_identified():
    for sql in ("UPDATE t SET a = 1;", "ALTER TABLE t RENAME TO t2;", "ALTER TABLE t DROP PARTITION p1;", "GRANT ALL ON *.* TO x;"):
        assert ops(sql)[0].kind == sqlplan.UNSUPPORTED, sql
    assert ops("SELECT 'unterminated;") == []


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("INSERT INTO users (email) VALUES ('a');", "INSERT changes data, not the table structure. Migrations may only change the schema"),
        ("update users set a = 1;", "UPDATE changes data, not the table structure"),
        ("DELETE FROM users;", "DELETE changes data"),
        ("TRUNCATE TABLE users;", "TRUNCATE TABLE changes data"),
        ("CREATE USER 'app'@'%' IDENTIFIED BY 'x';", "CREATE USER is account management, not a schema change. Users, roles and privileges are managed by the DBA team"),
        ("DROP USER 'app'@'%';", "DROP USER is account management"),
        ("GRANT ALL ON accounts.* TO 'app'@'%';", "GRANT is account management"),
        ("SET PASSWORD FOR 'app'@'%' = PASSWORD('x');", "SET PASSWORD is account management"),
        ("CREATE DATABASE other;", "CREATE DATABASE creates, changes or removes a whole schema"),
        ("DROP SCHEMA accounts;", "DROP SCHEMA creates, changes or removes a whole schema"),
        ("CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW SET NEW.a = 1;", "CREATE TRIGGER manages stored logic"),
        ("DROP VIEW v;", "DROP VIEW manages stored logic"),
        ("SET GLOBAL max_connections = 500;", "SET GLOBAL changes server or session settings"),
        ("USE other;", "USE is session or transaction control"),
        ("START TRANSACTION;", "START TRANSACTION is session or transaction control"),
        ("SELECT * FROM users;", "SELECT is a query or maintenance command"),
        ("HANDLER t OPEN;", "HANDLER is not a supported schema change"),
    ],
)
def test_unsupported_statements_say_what_they_are_and_what_to_do(sql: str, message: str):
    (op,) = ops(sql)
    assert op.kind == sqlplan.UNSUPPORTED and op.reason.startswith(message), op.reason
