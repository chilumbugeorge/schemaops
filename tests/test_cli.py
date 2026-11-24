from pathlib import Path

import pytest

from schemaops import cli, governance
from schemaops.deploy import HistoryRow, TableFacts
from schemaops.gitlab import MergeRequest, Project
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
    new_schema = [*base, "new-schema", "--cluster", "trades", "--schema", "ledger", "--owner", "trades-team",
                  "--description", "Ledger.", "--tier", "2", "--initial-dump", str(dump)]  # fmt: skip
    assert cli.main(new_schema) == 2  # the cluster must be onboarded first
    assert cli.main([*base, "new-cluster", "--cluster", "trades", "--description", "Trading.", "--version", "10.11",
                     "--dba-contact", "#dba"]) == 0  # fmt: skip
    assert cli.main(new_schema) == 0
    assert "tier: 2" in (root / "clusters" / "trades" / "ledger" / "schema.yaml").read_text()
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
    repo.schema.config_file.unlink()
    assert run(repo, "validate") == 1
    assert "[SCHEMA_CONFIG]" in capsys.readouterr().out


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
    assert "BLOCKED: A member of the DBA team must approve" in capsys.readouterr().out

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


def test_check_mr_writes_a_plan_with_method_size_and_previous_environment_result(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    name = "2026-11-20_add_display_name.sql"
    git_repo.add("dev", name, ALTER)
    base = git_repo.commit("dev merged")
    git_repo.add("uat", name, ALTER)
    sha = Migration(name, git_repo.schema.migrations_dir("dev") / name).sha256
    monkeypatch.setattr(cli, "_lower_history", lambda *call: {name: HistoryRow(name, sha, "success", "pt-osc", 42)})
    monkeypatch.setattr(cli, "_table_facts", lambda *call: TableFacts(True, rows=1_234_567, size_bytes=3 * 1024**2))
    report = tmp_path / "plan.md"

    assert run(git_repo, "check-mr", "--base", base, "--report", str(report)) == 0
    text = report.read_text()
    assert text.startswith("<!-- schemaops-plan -->\n### schemaops checks: passed")
    assert (
        "| `2026-11-20_add_display_name.sql` | trades/accounts | UAT | ALTER `users`: ADD COLUMN display_name VARCHAR(64) NULL "
        "| instant DDL, else pt-online-schema-change | ~1,234,567 rows, 3.0 MB | DEV: success in 42s (pt-osc) |"
    ) in text


def test_check_mr_plan_marks_manual_changes_and_escalates_large_tables(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    (git_repo.root / "schemaops.yaml").write_text("governance:\n  dba-approvers: [dana]\n")
    name = "2026-11-20_add_display_name.sql"
    for environment in ("dev", "uat"):
        git_repo.add(environment, name, ALTER)
    base = git_repo.commit("uat merged")
    git_repo.add("prod", name, ALTER)
    sha = Migration(name, git_repo.schema.migrations_dir("uat") / name).sha256
    monkeypatch.setattr(cli, "_lower_history", lambda *call: {name: HistoryRow(name, sha, "success", "pt-osc", 7)})
    monkeypatch.setattr(cli, "_table_facts", lambda *call: TableFacts(True, rows=50_000_000, size_bytes=1024**3))
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "9")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", base)
    monkeypatch.setattr(cli.MergeRequest, "approvers", lambda self: set())
    report = tmp_path / "plan.md"

    assert run(git_repo, "check-mr", "--report", str(report)) == 1  # a large table needs a DBA approval
    text = report.read_text()
    assert "### schemaops checks: failed (1 blocking problem(s))" in text
    assert "manual by DBA (large table)" in text and "`users` is a large table, about 50,000,000 rows" in text
    assert "A member of the DBA team must approve" in text
    assert "== Plan ==" in capsys.readouterr().out

    monkeypatch.setattr(cli.MergeRequest, "approvers", lambda self: {"dana"})
    assert run(git_repo, "check-mr") == 0


def test_plan_comment_is_created_once_then_updated_and_never_fails_the_check(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch
):
    base = git_repo.commit()
    git_repo.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "7")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", base)
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.example.com/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.setenv("SCHEMAOPS_GITLAB_TOKEN", "glpat-secret-token")
    calls: list[tuple[str, str, str]] = []
    notes: list[dict[str, object]] = [{"id": 1, "body": "an unrelated comment"}]

    def fake_call(self, suffix, method="GET", payload=None):  # noqa: ANN001, ANN202
        calls.append((method, suffix, (payload or {}).get("body", "")))
        if method == "GET":
            return list(notes) if suffix.startswith("/notes") else {"labels": []}
        if method == "POST":
            notes.insert(0, {"id": 2, "body": payload["body"]})
        return {}

    monkeypatch.setattr(MergeRequest, "_call", fake_call)
    assert run(git_repo, "check-mr", "--comment") == 0
    assert run(git_repo, "check-mr", "--comment") == 0
    writes = [(method, suffix) for method, suffix, _body in calls if method != "GET"]
    assert writes == [("POST", "/notes"), ("PUT", "/notes/2")]
    posted = [body for method, _suffix, body in calls if method == "POST"][0]
    assert posted.startswith("<!-- schemaops-plan -->") and "glpat-secret-token" not in posted

    def broken_call(self, suffix, method="GET", payload=None):  # noqa: ANN001, ANN202
        if suffix.startswith("/notes"):
            raise governance.GovernanceError("403 Forbidden")
        return {"labels": []}

    monkeypatch.setattr(MergeRequest, "_call", broken_call)
    assert run(git_repo, "check-mr", "--comment") == 0  # a failed comment never fails the check


def test_tier_one_schema_needs_dba_approval_for_prod_and_the_plan_names_the_schema(
    git_repo: Repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (git_repo.root / "schemaops.yaml").write_text("governance:\n  dba-approvers: [dana]\n")
    with git_repo.schema.config_file.open("a") as handle:
        handle.write("tier: 1\ndata-classification: restricted\n")
    name = "2026-11-20_add_display_name.sql"
    for environment in ("dev", "uat"):
        git_repo.add(environment, name, ALTER)
    base = git_repo.commit("uat merged")
    git_repo.add("prod", name, ALTER)
    sha = Migration(name, git_repo.schema.migrations_dir("uat") / name).sha256
    monkeypatch.setattr(cli, "_lower_history", lambda *call: {name: HistoryRow(name, sha, "success", "instant", 0)})
    monkeypatch.setattr(cli, "_table_facts", lambda *call: None)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "9")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", base)
    monkeypatch.setattr(cli.MergeRequest, "approvers", lambda self: set())
    report = tmp_path / "plan.md"

    assert run(git_repo, "check-mr", "--report", str(report)) == 1
    text = report.read_text()
    assert "trades/accounts is a tier 1 schema: PROD changes need DBA approval" in text
    assert "- trades/accounts: owner trades-team, tier 1, data restricted" in text
    monkeypatch.setattr(cli.MergeRequest, "approvers", lambda self: {"dana"})
    assert run(git_repo, "check-mr") == 0


def test_host_can_come_from_cluster_yaml_but_credentials_cannot(repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    with repo.cluster_file.open("a") as handle:
        handle.write("environments:\n  dev: {host: db-dev.internal, port: 3307, tls-mode: disabled}\n")
    assert run(repo, "validate") == 0
    scope = ["plan", "--env", "dev", "--cluster", "trades", "--schema", "accounts"]
    assert run(repo, *scope) == 2
    error = capsys.readouterr().err
    assert "SCHEMAOPS_TRADES_DEV_USER" in error and "SCHEMAOPS_TRADES_DEV_HOST" not in error  # only credentials are missing

    with repo.cluster_file.open("a") as handle:
        handle.write("  uat: {host: db-uat.internal, password: hunter2}\n")
    assert run(repo, "validate") == 1
    assert "credentials must never be stored in Git" in capsys.readouterr().out


# ------------------------------------------------ deployment jobs and reporting


def fake_targets(monkeypatch: pytest.MonkeyPatch, targets: dict[str, object]) -> list[str]:
    """Make the CLI use in-memory targets, and capture notifications."""
    sent: list[str] = []
    monkeypatch.setattr(cli, "_target", lambda settings, schema, environment, read_only=False: targets[schema.label])
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli.notify, "send", lambda text: sent.append(text) or True)
    return sent


def test_deploy_carries_on_past_a_failing_schema_and_notifies(git_repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from test_deploy import FakeTarget, done

    other = Repo(git_repo.root, cluster="payments", schema="ledger")
    other.add("dev", DUMP)
    git_repo.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    other.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    broken = FakeTarget(tables=2, rows=done(other, "dev", DUMP), fail_on="display_name")
    working = FakeTarget(tables=2, rows=done(git_repo, "dev", DUMP))
    sent = fake_targets(monkeypatch, {"payments/ledger": broken, "trades/accounts": working})

    assert run(git_repo, "deploy", "--env", "dev", "--all") == 1
    assert working.rows["2026-11-20_add_display_name.sql"].status == "success"  # deployed despite the other failure
    assert "trades/accounts (DEV): applied 1 migration(s)" in capsys.readouterr().out
    assert len(sent) == 1 and "payments/ledger: FAILED" in sent[0] and "trades/accounts: applied 1 migration(s)" in sent[0]

    # --cluster narrows --all to one cluster.
    assert run(git_repo, "deploy", "--env", "dev", "--all", "--cluster", "trades") == 0


def test_a_failure_is_never_reported_as_merely_waiting_for_the_dba(git_repo: Repo, monkeypatch: pytest.MonkeyPatch):
    from test_deploy import FakeTarget, done

    other = Repo(git_repo.root, cluster="payments", schema="ledger")
    other.add("dev", DUMP)
    other.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    git_repo.add("dev", "2026-11-20_unique_email.sql", "ALTER TABLE users ADD UNIQUE KEY uq_email (email);\n")
    waiting = FakeTarget(tables=2, rows=done(git_repo, "dev", DUMP))
    broken = FakeTarget(tables=2, rows=done(other, "dev", DUMP), fail_on="display_name")
    fake_targets(monkeypatch, {"trades/accounts": waiting, "payments/ledger": broken})
    assert run(git_repo, "deploy", "--env", "dev", "--all", "--cluster", "trades") == 3  # waiting only
    assert run(git_repo, "deploy", "--env", "dev", "--all") == 1  # a failure outranks waiting


def test_successful_deployment_posts_the_new_table_structure_on_its_merge_request(git_repo: Repo, monkeypatch: pytest.MonkeyPatch):
    from test_deploy import FakeTarget, done

    name = "2026-11-20_add_display_name.sql"
    git_repo.add("dev", name, ALTER)
    commit = git_repo.commit("add display name")
    target = FakeTarget(tables=2, rows=done(git_repo, "dev", DUMP))
    target.instant = True
    fake_targets(monkeypatch, {"trades/accounts": target})
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.example.com/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    monkeypatch.setenv("SCHEMAOPS_GITLAB_TOKEN", "glpat-secret-token")
    calls: list[tuple[str, str, str]] = []

    def fake_call(self, suffix, method="GET", payload=None):  # noqa: ANN001, ANN202
        calls.append((method, suffix, (payload or {}).get("body", "")))
        return [{"iid": 17}] if "merge_requests" in suffix and method == "GET" else {}

    monkeypatch.setattr(Project, "_call", fake_call)
    assert run(git_repo, "deploy", "--env", "dev", "--all") == 0
    assert ("GET", f"/repository/commits/{commit}/merge_requests", "") in calls
    (posted,) = [(suffix, body) for method, suffix, body in calls if method == "POST"]
    assert posted[0] == "/merge_requests/17/notes"
    assert posted[1].startswith("### schemaops: deployed to DEV")
    assert f"**trades/accounts** `{name}`: instant DDL in 0s" in posted[1]
    assert "Table `users` is now:" in posted[1] and "```sql\nCREATE TABLE `users` (id) + ADD COLUMN display_name" in posted[1]

    calls.clear()
    assert run(git_repo, "deploy", "--env", "dev", "--all") == 0  # nothing applied: nothing posted
    assert calls == []


def test_reconcile_command_reports_and_fails_only_when_something_is_wrong(git_repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from test_deploy import FakeTarget, done

    target = FakeTarget(tables=2, rows=done(git_repo, "dev", DUMP))
    target.saved = dict(target.ddl)
    sent = fake_targets(monkeypatch, {"trades/accounts": target})
    assert run(git_repo, "reconcile", "--env", "dev", "--all") == 0
    assert "0 finding(s)" in capsys.readouterr().out and sent == []

    git_repo.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    target.ddl["users"] += " + hotfix"
    assert run(git_repo, "reconcile", "--env", "dev", "--all") == 1
    output = capsys.readouterr().out
    assert "trades/accounts: PENDING `2026-11-20_add_display_name.sql`" in output
    assert "trades/accounts: DRIFT Table `users` was changed outside a migration." in output
    assert len(sent) == 1 and "2 finding(s)" in sent[0]
    assert target.calls == [] and target.rows.keys() == {DUMP}  # read-only


def test_pipeline_command_writes_one_deployment_job_per_changed_cluster(git_repo: Repo, tmp_path: Path):
    import yaml

    base = git_repo.commit()
    other = Repo(git_repo.root, cluster="payments", schema="ledger")
    other.add("dev", DUMP)
    git_repo.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    git_repo.commit("two clusters changed")
    output = tmp_path / "child.yml"
    assert run(git_repo, "pipeline", "--env", "dev", "--changed-since", base, "--output", str(output)) == 0
    child = yaml.safe_load(output.read_text())
    assert child["include"] == [{"local": "ci/common.yml"}]
    assert set(child) - {"include", "stages"} == {"deploy-payments-dev", "deploy-trades-dev"}
    job = child["deploy-trades-dev"]
    assert job["extends"] == ".schemaops-deploy" and job["environment"] == "dev"
    assert job["resource_group"] == "schemaops-trades-dev"
    assert job["variables"] == {"SCHEMAOPS_ENV": "dev", "SCHEMAOPS_CLUSTER": "trades", "SCHEMAOPS_SINCE": base}

    assert run(git_repo, "pipeline", "--env", "dev", "--changed-since", "HEAD", "--output", str(output)) == 0
    assert "nothing-to-deploy" in yaml.safe_load(output.read_text())  # a child pipeline needs at least one job


def test_notifications_are_optional_and_never_raise(monkeypatch: pytest.MonkeyPatch):
    import urllib.error

    from schemaops import notify

    monkeypatch.delenv("SCHEMAOPS_NOTIFY_WEBHOOK", raising=False)
    assert notify.send("hello") is False
    monkeypatch.setenv("SCHEMAOPS_NOTIFY_WEBHOOK", "https://hooks.example.com/services/T000/SECRET")
    seen: list[bytes] = []

    class Response:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def fake_urlopen(request, timeout=0):  # noqa: ANN001, ANN202
        seen.append(request.data)
        return Response()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.send("deployed") is True and b"deployed" in seen[0]

    def broken(request, timeout=0):  # noqa: ANN001, ANN202
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(notify.urllib.request, "urlopen", broken)
    assert notify.send("deployed") is False
