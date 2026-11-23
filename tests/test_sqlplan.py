from schemaops import sqlplan


def ops(sql: str) -> list[sqlplan.Operation]:
    return sqlplan.parse_operations(sql)


def test_alter_keeps_the_original_specification_for_pt_osc():
    (op,) = ops("ALTER TABLE `Users`\n  ADD COLUMN note VARCHAR(10) DEFAULT 'a, b' NULL,\n  ADD INDEX idx_note (note);")
    assert (op.kind, op.table) == (sqlplan.ALTER_TABLE, "users")
    assert op.alter == "ADD COLUMN note VARCHAR(10) DEFAULT 'a, b' NULL,\n  ADD INDEX idx_note (note)"
    assert not op.adds_unique_index


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
