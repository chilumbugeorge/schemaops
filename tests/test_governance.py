from schemaops import governance

from conftest import DUMP, Repo

ALTER = "ALTER TABLE users ADD COLUMN display_name VARCHAR(64) NULL;\n"


def checks(repo: Repo) -> set[str]:
    return {issue.check for issue in governance.check_repository(repo.settings)}


def test_valid_repository_has_no_issues(repo: Repo):
    repo.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    assert governance.check_repository(repo.settings) == []


def test_missing_configuration_files(repo: Repo):
    repo.schema.config_file.unlink()
    repo.schema.environment_file("uat").unlink()
    assert {"SCHEMA_CONFIG", "ENV_CONFIG"} <= checks(repo)


def test_configuration_must_match_its_folder(repo: Repo):
    repo.schema.config_file.write_text("cluster-name: other\nschema-name: accounts\nowner: x\n")
    issues = [issue.message for issue in governance.check_repository(repo.settings)]
    assert any("`cluster-name` is `other`" in message for message in issues)
    assert any("`description` is required" in message for message in issues)


def test_every_file_must_be_registered_and_every_entry_must_exist(repo: Repo):
    (repo.schema.migrations_dir("dev") / "2026-11-20_orphan.sql").write_text(ALTER)
    repo.set("dev", [DUMP, "2026-11-21_missing.sql"])
    messages = [issue.message for issue in governance.check_repository(repo.settings)]
    assert any("Not registered" in message for message in messages)
    assert any("does not exist" in message for message in messages)


def test_file_names_order_and_initial_dump(repo: Repo):
    repo.add("dev", "2026-11-22_b.sql", ALTER)
    repo.add("dev", "2026-11-21_a.sql", ALTER)
    repo.add("dev", "Bad Name.sql", ALTER)
    repo.add("dev", "2026-13-45_bad_date.sql", ALTER)
    assert {"ORDER", "FILE_NAME"} <= checks(repo)

    other = Repo(repo.root / "second")
    other.add("dev", "2026-11-20_create_users.sql", ALTER)
    assert "INITIAL_DUMP" in {issue.check for issue in governance.check_repository(other.settings)}


def test_same_day_migrations_follow_the_listed_order(repo: Repo):
    repo.add("dev", "2026-11-20_b_second.sql", ALTER)
    repo.add("dev", "2026-11-20_a_third.sql", ALTER)
    assert governance.check_repository(repo.settings) == []


def test_promotion_must_follow_dev_uat_prod_without_skipping(repo: Repo):
    repo.add("prod", "2026-11-20_straight_to_prod.sql", ALTER)
    assert any("Not present in UAT" in issue.message for issue in governance.check_repository(repo.settings))

    second = Repo(repo.root / "second")
    for environment in ("dev", "uat"):
        second.add(environment, DUMP)
    second.add("dev", "2026-11-20_first.sql", ALTER)
    second.add("dev", "2026-11-21_second.sql", ALTER)
    second.add("uat", "2026-11-21_second.sql", ALTER)  # skips 2026-11-20_first
    assert any("none may be skipped" in issue.message for issue in governance.check_repository(second.settings))


def test_promoted_copy_must_be_identical(repo: Repo):
    repo.add("dev", "2026-11-20_a.sql", ALTER)
    repo.add("uat", "2026-11-20_a.sql", ALTER.replace("64", "128"))
    assert "INTEGRITY" in checks(repo)


def test_protected_tables_file_is_validated(repo: Repo):
    (repo.root / "policy" / "protected_tables.yaml").write_text("protected-tables:\n  - {cluster: nope, schema: x, table: t}\n")
    assert "POLICY" in checks(repo)


# ------------------------------------------------------------ merge requests


def mr(repo: Repo, base: str) -> tuple[set[str], list[str]]:
    issues, added = governance.check_merge_request(repo.settings, base)
    return {issue.check for issue in issues}, [f"{item.environment}:{item.name}" for item in added]


def test_new_dev_migration_is_a_valid_merge_request(git_repo: Repo):
    base = git_repo.commit()
    git_repo.add("dev", "2026-11-20_a.sql", ALTER)
    assert mr(git_repo, base) == (set(), ["dev:2026-11-20_a.sql"])


def test_promotion_requires_the_previous_environment_to_be_merged_first(git_repo: Repo):
    base = git_repo.commit()
    # DEV and UAT in the same merge request: rejected twice over.
    git_repo.add("dev", "2026-11-20_a.sql", ALTER)
    git_repo.add("uat", "2026-11-20_a.sql", ALTER)
    found, _ = mr(git_repo, base)
    assert {"ONE_ENVIRONMENT", "PROMOTION"} <= found

    # Once the DEV merge request is merged, the UAT promotion alone is valid...
    git_repo.git("checkout", "-q", "--", ".")
    git_repo.git("clean", "-qfd")
    git_repo.add("dev", "2026-11-20_a.sql", ALTER)
    merged = git_repo.commit("dev merged")
    git_repo.add("uat", "2026-11-20_a.sql", ALTER)
    assert mr(git_repo, merged) == (set(), ["uat:2026-11-20_a.sql"])

    # ...but PROD cannot be reached before UAT is merged.
    git_repo.git("checkout", "-q", "--", ".")
    git_repo.git("clean", "-qfd")
    git_repo.add("prod", "2026-11-20_a.sql", ALTER)
    found, _ = mr(git_repo, merged)
    assert "PROMOTION" in found


def test_existing_migrations_and_entries_are_immutable(git_repo: Repo):
    git_repo.add("dev", "2026-11-20_a.sql", ALTER)
    base = git_repo.commit()
    (git_repo.schema.migrations_dir("dev") / "2026-11-20_a.sql").write_text(ALTER + "-- edited\n")
    assert "IMMUTABLE" in mr(git_repo, base)[0]

    git_repo.git("checkout", "-q", "--", ".")
    git_repo.set("dev", ["2026-11-20_a.sql", DUMP])
    assert "APPEND_ONLY" in mr(git_repo, base)[0]

    git_repo.git("checkout", "-q", "--", ".")
    (git_repo.schema.migrations_dir("dev") / "2026-11-20_a.sql").unlink()
    git_repo.set("dev", [DUMP])
    assert {"IMMUTABLE", "APPEND_ONLY"} <= mr(git_repo, base)[0]


def test_migrations_cannot_be_combined_with_policy_or_framework_changes(git_repo: Repo):
    base = git_repo.commit()
    git_repo.add("dev", "2026-11-20_a.sql", ALTER)
    (git_repo.root / "policy" / "protected_tables.yaml").write_text("protected-tables: []\n")
    assert "SEPARATE_CHANGES" in mr(git_repo, base)[0]


# ------------------------------------------------------- cluster and schema files


def messages(repo: Repo) -> list[str]:
    return [issue.format() for issue in governance.check_repository(repo.settings)]


def test_every_cluster_needs_a_valid_cluster_yaml(repo: Repo):
    repo.cluster_file.unlink()
    assert any("[CLUSTER_CONFIG] cluster.yaml is missing" in message for message in messages(repo))

    repo.cluster_file.write_text(
        "cluster-name: other\ndescription: x\nengine: oracle\nversion: 10.11\ntopology: ring\nwindow: sunday\n"
        "environments:\n  qa: {host: a}\n  dev: {host: 'bad host', port: 99999, tls-mode: maybe, user: app}\n"
        "  prod: {host: db-prod.internal, tls-mode: disabled}\n"
    )
    found = "\n".join(messages(repo))
    for expected in (
        "`dba-contact` is required",
        "`cluster-name` is `other` but the folder says `trades`",
        "`engine` must be one of mariadb, mysql",
        "`version` must be quoted text",
        "`topology` must be one of",
        "Unknown key `window`",
        "Unknown environment `qa`",
        "`environments.dev.host` is not a valid host name",
        "`environments.dev.port` must be a number between 1 and 65535",
        "`environments.dev.tls-mode` must be one of",
        "`environments.dev.user`: credentials must never be stored in Git",
        "`environments.prod.tls-mode` must verify the server certificate outside DEV",
    ):
        assert expected in found, expected


def test_a_cluster_without_schemas_is_still_checked(repo: Repo):
    (repo.root / "clusters" / "empty_cluster").mkdir()
    assert any("clusters/empty_cluster/cluster.yaml" in message for message in messages(repo))


def test_schema_yaml_optional_fields_are_validated(repo: Repo):
    with repo.schema.config_file.open("a") as handle:
        handle.write("tier: 5\nstatus: paused\ndata-classification: secret\nownr: typo\n")
    found = "\n".join(messages(repo))
    for expected in ("`tier` must be one of 1, 2, 3", "`status` must be one of active, frozen, retired",
                     "`data-classification` must be one of", "Unknown key `ownr`"):  # fmt: skip
        assert expected in found, expected


def test_legacy_main_yaml_gets_a_rename_hint(repo: Repo):
    repo.schema.config_file.rename(repo.schema.path / "main.yaml")
    assert any("Rename main.yaml to schema.yaml" in message for message in messages(repo))


def test_owner_group_must_own_the_schema_in_codeowners(repo: Repo):
    with repo.schema.config_file.open("a") as handle:
        handle.write('owner-group: "@acme/trades-team"\n')
    assert messages(repo) == []  # no CODEOWNERS file: nothing to compare with

    (repo.root / "CODEOWNERS").write_text("/src/ @acme/dba\n/clusters/trades/accounts/ @acme/someone-else\n")
    assert any("[OWNERSHIP] Add `/clusters/trades/accounts/ @acme/trades-team`" in message for message in messages(repo))
    (repo.root / "CODEOWNERS").write_text("/clusters/trades/accounts/   @acme/trades-team @acme/dba  # owners\n")
    assert messages(repo) == []

    repo.schema.config_file.write_text(repo.schema.config_file.read_text().replace('"@acme/trades-team"', "trades team"))
    assert any("`owner-group` must be a GitLab group" in message for message in messages(repo))


def test_frozen_and_retired_schemas_accept_no_new_migrations(git_repo: Repo):
    with git_repo.schema.config_file.open("a") as handle:
        handle.write("status: frozen\n")
    base = git_repo.commit("freeze")
    git_repo.add("dev", "2026-11-20_a.sql", ALTER)
    assert "FROZEN" in mr(git_repo, base)[0]

    git_repo.schema.config_file.write_text(git_repo.schema.config_file.read_text().replace("frozen", "active"))
    base = git_repo.commit("thaw")
    git_repo.add("dev", "2026-11-21_b.sql", ALTER)
    assert mr(git_repo, base)[0] == set()


def test_cluster_yaml_cannot_be_changed_together_with_migrations(git_repo: Repo):
    base = git_repo.commit()
    git_repo.add("dev", "2026-11-20_a.sql", ALTER)
    with git_repo.cluster_file.open("a") as handle:
        handle.write("environments:\n  prod: {host: somewhere-else.example.com}\n")
    assert "SEPARATE_CHANGES" in mr(git_repo, base)[0]

    # A brand-new cluster may arrive together with its first schema.
    git_repo.git("checkout", "-q", "--", ".")
    git_repo.git("clean", "-qfd")
    fresh = Repo(git_repo.root, cluster="payments", schema="ledger")
    fresh.add("dev", DUMP)
    assert mr(git_repo, base)[0] == set()
