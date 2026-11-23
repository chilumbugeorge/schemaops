from pathlib import Path

import pytest

from schemaops import cli, governance
from schemaops.deploy import HistoryRow
from schemaops.repo import Migration

from conftest import DUMP, Repo

ALTER = "ALTER TABLE users ADD COLUMN display_name VARCHAR(64) NULL;\n"


def run(repo: Repo, *args: str) -> int:
    return cli.main(["--root", str(repo.root), *args])


def test_scaffolding_commands_produce_a_repository_that_passes_governance(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    dump = tmp_path / "dump.sql"
    dump.write_text("CREATE TABLE users (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB;\n")
    root = tmp_path / "repo"
    root.mkdir()
    base = ["--root", str(root)]
    assert cli.main([*base, "new-schema", "--cluster", "trades", "--schema", "ledger", "--owner", "trades-team",
                     "--description", "Ledger.", "--initial-dump", str(dump)]) == 0  # fmt: skip
    assert cli.main([*base, "new-migration", "--cluster", "trades", "--schema", "ledger", "--name", "add_users_email"]) == 0
    schema_dir = root / "clusters" / "trades" / "ledger"
    (created,) = [path for path in (schema_dir / "migrations" / "dev").iterdir() if "add_users_email" in path.name]
    created.write_text("ALTER TABLE users ADD COLUMN email VARCHAR(128) NULL;\n")

    assert cli.main([*base, "validate"]) == 0
    assert cli.main([*base, "promote", "--cluster", "trades", "--schema", "ledger", "--to", "uat", "--count", "1"]) == 0
    assert cli.main([*base, "promote", "--cluster", "trades", "--schema", "ledger", "--to", "prod"]) == 0
    assert cli.main([*base, "validate"]) == 0
    assert "environment: uat" in (schema_dir / "environments" / "uat.yaml").read_text()
    assert len(list((schema_dir / "migrations" / "uat").glob("*.sql"))) == 1
    assert cli.main([*base, "lint"]) == 0
    assert "0 governance issue(s)" in capsys.readouterr().out


def test_validate_fails_on_governance_issues(repo: Repo, capsys: pytest.CaptureFixture[str]):
    repo.schema.main_file.unlink()
    assert run(repo, "validate") == 1
    assert "[MAIN_CONFIG]" in capsys.readouterr().out


def test_check_mr_label_bypasses_warn_and_advise_but_never_errors(git_repo: Repo, capsys: pytest.CaptureFixture[str]):
    base = git_repo.commit()
    path = git_repo.add("dev", "2026-11-20_add_status.sql", "ALTER TABLE users ADD COLUMN status ENUM('a','b') NULL;\n")
    assert run(git_repo, "check-mr", "--base", base) == 1
    output = capsys.readouterr().out
    assert "warn: [NO_ENUM]" in output and "add the `ignore-linting` label" in output

    assert run(git_repo, "check-mr", "--base", base, "--label", "ignore-linting") == 0
    assert "Result: PASSED" in capsys.readouterr().out

    path.write_text("ALTER TABLE users ADD COLUMN status ENUM('a','b') NOT NULL;\n")
    assert run(git_repo, "check-mr", "--base", base, "--label", "ignore-linting") == 1
    assert "error: [ADD_NOT_NULL_NEEDS_DEFAULT]" in capsys.readouterr().out


def test_check_mr_reads_labels_from_gitlab_and_advise_blocking_is_configurable(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch
):
    base = git_repo.commit()
    git_repo.add("dev", "2026-11-20_add_price.sql", "ALTER TABLE users ADD COLUMN price FLOAT NULL;\n")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "7")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", base)
    assert run(git_repo, "check-mr") == 1  # advise blocks by default
    monkeypatch.setenv("CI_MERGE_REQUEST_LABELS", "backend,ignore-linting")
    assert run(git_repo, "check-mr") == 0
    monkeypatch.setenv("CI_MERGE_REQUEST_LABELS", "backend")
    (git_repo.root / "schemaops.yaml").write_text("lint:\n  blocking-without-label: [warn]\n")
    base = git_repo.git("stash", "create") or base  # settings change is not part of the MR under test
    (git_repo.root / "schemaops.yaml").unlink()
    assert run(git_repo, "check-mr") == 1


def test_check_mr_requires_dba_approval_for_protected_tables(git_repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    (git_repo.root / "schemaops.yaml").write_text("governance:\n  dba-approvers: [dana]\n")
    base = git_repo.commit()
    git_repo.add("dev", "2026-11-20_add_orders_note.sql", "ALTER TABLE orders ADD COLUMN note VARCHAR(64) NULL;\n")

    assert run(git_repo, "check-mr", "--base", base) == 0  # local run: reported, checked in GitLab
    assert "changes protected table(s) orders" in capsys.readouterr().out

    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "7")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", base)
    assert run(git_repo, "check-mr") == 1  # no token: fails closed
    assert "BLOCKED: a member of the DBA team must approve" in capsys.readouterr().out

    monkeypatch.setattr(cli.MergeRequest, "approvers", lambda self: {"sam"})
    assert run(git_repo, "check-mr") == 1
    monkeypatch.setattr(cli.MergeRequest, "approvers", lambda self: {"sam", "dana"})
    assert run(git_repo, "check-mr") == 0
    assert "Approved by DBA: dana" in capsys.readouterr().out


def test_check_mr_promotion_needs_deployment_evidence(git_repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    name = "2026-11-20_add_display_name.sql"
    git_repo.add("dev", name, ALTER)
    base = git_repo.commit("dev merged")
    git_repo.add("uat", name, ALTER)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "8")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", base)

    assert run(git_repo, "check-mr") == 1  # no read-only DEV credentials: fails closed
    assert "cannot confirm the DEV deployment" in capsys.readouterr().out

    sha = Migration(name, git_repo.schema.migrations_dir("dev") / name).sha256
    history = {name: HistoryRow(name, sha, "failed")}
    monkeypatch.setattr(cli, "_lower_history", lambda settings, schema, environment: history)
    assert run(git_repo, "check-mr") == 1
    assert "DEV deployment is `failed`" in capsys.readouterr().out

    history[name] = HistoryRow(name, sha, "success")
    assert run(git_repo, "check-mr") == 0
    assert "confirmed deployed to DEV" in capsys.readouterr().out


def test_deployments_refuse_to_run_from_merge_request_or_unprotected_pipelines(repo: Repo, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "9")
    with pytest.raises(governance.GovernanceError, match="never run from merge request"):
        cli._guard_pipeline("dev")
    monkeypatch.delenv("CI_MERGE_REQUEST_IID")
    with pytest.raises(governance.GovernanceError, match="protected branch"):
        cli._guard_pipeline("prod")
    monkeypatch.setenv("CI_COMMIT_REF_PROTECTED", "true")
    cli._guard_pipeline("prod")
    assert run(repo, "deploy", "--env", "dev", "--all") != 0  # no pt-osc / credentials here: refuses cleanly


def test_missing_credentials_are_a_clean_configuration_error(repo: Repo):
    assert run(repo, "plan", "--env", "dev", "--cluster", "trades", "--schema", "accounts") == 2
    assert run(repo, "status", "--env", "dev") == 2
