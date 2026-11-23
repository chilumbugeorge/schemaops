from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from schemaops.repo import ENVIRONMENTS, Schema, Settings

DUMP = "2026-11-15_initial_dump.sql"
DUMP_SQL = (
    "CREATE TABLE users (\n"
    "  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  email VARCHAR(128) NOT NULL,\n"
    "  PRIMARY KEY (id)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n"
    "CREATE TABLE orders (\n"
    "  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  PRIMARY KEY (id)\n"
    ") ENGINE=InnoDB;\n"
)


class Repo:
    """A throwaway schemaops repository with one schema, trades/accounts."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.schema = Schema("trades", "accounts", root / "clusters" / "trades" / "accounts")
        self.schema.path.mkdir(parents=True)
        (self.schema.path / "environments").mkdir()
        self.schema.main_file.write_text(
            "cluster-name: trades\nschema-name: accounts\nowner: trades-team\ndescription: Accounts.\n"
        )
        for environment in ENVIRONMENTS:
            self.schema.migrations_dir(environment).mkdir(parents=True)
            self.set(environment, [])
        (root / "policy").mkdir()
        (root / "policy" / "protected_tables.yaml").write_text(
            "protected-tables:\n  - {cluster: trades, schema: accounts, table: orders, reason: hot}\n"
        )

    @property
    def settings(self) -> Settings:
        return Settings.load(self.root)

    def set(self, environment: str, names: list[str]) -> None:
        body = "".join(f"  - {name}\n" for name in names)
        self.schema.environment_file(environment).write_text(
            f"cluster-name: trades\nschema-name: accounts\nenvironment: {environment}\n"
            + ("migrations:\n" + body if names else "migrations: []\n")
        )

    def add(self, environment: str, name: str, sql: str = DUMP_SQL) -> Path:
        path = self.schema.migrations_dir(environment) / name
        path.write_text(sql)
        self.set(environment, [*self.schema.registered(environment), name])
        return path

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    repo = Repo(tmp_path)
    for environment in ENVIRONMENTS:
        repo.add(environment, DUMP)
    return repo


@pytest.fixture
def git_repo(repo: Repo) -> Repo:
    repo.git("init", "-q", "-b", "main")
    repo.commit("baseline")
    return repo


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "CI_MERGE_REQUEST_IID", "CI_COMMIT_REF_PROTECTED", "CI_MERGE_REQUEST_LABELS"):
        monkeypatch.delenv(name, raising=False)
