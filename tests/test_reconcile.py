from schemaops.deploy import HistoryRow
from schemaops.reconcile import reconcile

from conftest import DUMP, Repo
from test_deploy import ADD_COLUMN, ADD_COLUMN_SQL, FakeTarget, done


def kinds(findings) -> list[str]:  # noqa: ANN001
    return [finding.kind for finding in findings]


def healthy(repo: Repo) -> FakeTarget:
    target = FakeTarget(tables=2, rows=done(repo, "dev", DUMP))
    target.saved = dict(target.ddl)
    return target


def test_a_database_that_matches_git_has_no_findings(repo: Repo):
    assert reconcile(repo.settings, repo.schema, "dev", healthy(repo)) == []


def test_pending_failed_tampered_and_unknown_migrations_are_reported(git_repo: Repo):
    git_repo.add("dev", ADD_COLUMN, ADD_COLUMN_SQL)
    git_repo.add("dev", "2026-11-21_b.sql", ADD_COLUMN_SQL.replace("display_name", "b"))
    git_repo.commit("two migrations")
    target = healthy(git_repo)
    target.rows[DUMP] = HistoryRow(DUMP, "0" * 64, "success")
    target.rows[ADD_COLUMN] = HistoryRow(ADD_COLUMN, "x", "failed")
    target.rows["2026-11-01_gone.sql"] = HistoryRow("2026-11-01_gone.sql", "x", "success")
    findings = reconcile(git_repo.settings, git_repo.schema, "dev", target)
    assert sorted(kinds(findings)) == ["CHECKSUM", "FAILED", "PENDING", "UNKNOWN"]
    pending = next(finding for finding in findings if finding.kind == "PENDING")
    assert "`2026-11-21_b.sql` is registered in Git but not applied, merged" in pending.message
    assert "0 day(s) ago" in pending.message


def test_changes_made_outside_migrations_are_reported_as_drift(repo: Repo):
    target = healthy(repo)
    target.ddl["users"] = "CREATE TABLE `users` (\n  id,\n  hotfix INT\n) AUTO_INCREMENT=99"
    target.saved = {"users": "CREATE TABLE `users` (\n  id\n) AUTO_INCREMENT=5", "orders": target.ddl["orders"], "gone": "x"}
    target.ddl["scratch"] = "CREATE TABLE `scratch` (id)"
    findings = reconcile(repo.settings, repo.schema, "dev", target)
    assert kinds(findings) == ["DRIFT", "DRIFT", "DRIFT"]
    messages = " ".join(finding.message for finding in findings)
    assert "`scratch` exists but was not created by a migration" in messages
    assert "`gone` was removed outside a migration" in messages
    changed = next(finding for finding in findings if "`users` was changed" in finding.message)
    assert "+  hotfix INT" in changed.detail and "AUTO_INCREMENT" not in changed.detail  # counters are not drift


def test_leftovers_are_reported_once_and_not_as_drift(repo: Repo):
    target = healthy(repo)
    target.ddl["_users_new"] = "CREATE TABLE `_users_new` (id)"
    target.left = ["table `_users_new`", "trigger `pt_osc_accounts_users_ins`"]
    assert kinds(reconcile(repo.settings, repo.schema, "dev", target)) == ["LEFTOVER", "LEFTOVER"]


def test_no_snapshot_yet_means_no_drift_report(repo: Repo):
    target = FakeTarget(tables=2, rows=done(repo, "dev", DUMP))
    assert reconcile(repo.settings, repo.schema, "dev", target) == []
