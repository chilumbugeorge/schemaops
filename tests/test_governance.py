from schemaops import governance

from conftest import DUMP, Repo

ALTER = "ALTER TABLE users ADD COLUMN display_name VARCHAR(64) NULL;\n"


def checks(repo: Repo) -> set[str]:
    return {issue.check for issue in governance.check_repository(repo.settings)}


def test_valid_repository_has_no_issues(repo: Repo):
    repo.add("dev", "2026-11-20_add_display_name.sql", ALTER)
    assert governance.check_repository(repo.settings) == []


def test_missing_configuration_files(repo: Repo):
    repo.schema.main_file.unlink()
    repo.schema.environment_file("uat").unlink()
    assert {"MAIN_CONFIG", "ENV_CONFIG"} <= checks(repo)


def test_configuration_must_match_its_folder(repo: Repo):
    repo.schema.main_file.write_text("cluster-name: other\nschema-name: accounts\nowner: x\n")
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
