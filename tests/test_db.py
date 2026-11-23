import logging
import stat
from pathlib import Path

import pytest

from schemaops import log
from schemaops.db import ConnectionConfig
from schemaops.errors import ConfigError

DEV = {
    "SCHEMAOPS_TRADES_DEV_HOST": "db-dev.internal",
    "SCHEMAOPS_TRADES_DEV_USER": "schemaops_dev",
    "SCHEMAOPS_TRADES_DEV_PASSWORD": "s3cr3t-dev",
}


def prod(tmp_path: Path, **overrides: str) -> dict[str, str]:
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    values = {
        "SCHEMAOPS_TRADES_PROD_HOST": "db-prod.internal",
        "SCHEMAOPS_TRADES_PROD_USER": "schemaops_prod",
        "SCHEMAOPS_TRADES_PROD_PASSWORD": 's3cr3t"prod\\',
        "SCHEMAOPS_TRADES_PROD_TLS_CA": str(ca),
    }
    values.update(overrides)
    return values


def test_configuration_is_read_from_the_environment_only():
    config = ConnectionConfig.from_env("trades", "dev", environ=DEV)
    assert (config.host, config.port, config.user, config.tls_mode) == ("db-dev.internal", 3306, "schemaops_dev", "verify-identity")


def test_missing_variables_are_named_without_values():
    with pytest.raises(ConfigError) as error:
        ConnectionConfig.from_env("trades", "uat", environ=DEV)
    assert "SCHEMAOPS_TRADES_UAT_HOST" in str(error.value) and "SCHEMAOPS_TRADES_UAT_PASSWORD" in str(error.value)


def test_environments_are_isolated_and_read_only_accounts_are_separate(tmp_path: Path):
    both = {**DEV, **prod(tmp_path)}
    assert ConnectionConfig.from_env("trades", "dev", environ=both).password == "s3cr3t-dev"
    assert ConnectionConfig.from_env("trades", "prod", environ=both).user == "schemaops_prod"
    with pytest.raises(ConfigError, match="SCHEMAOPS_TRADES_DEV_RO_USER"):
        ConnectionConfig.from_env("trades", "dev", read_only=True, environ=both)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"SCHEMAOPS_TRADES_PROD_TLS_MODE": "disabled"}, "must verify the server certificate"),
        ({"SCHEMAOPS_TRADES_PROD_TLS_MODE": "required"}, "must verify the server certificate"),
        ({"SCHEMAOPS_TRADES_PROD_TLS_MODE": "sometimes"}, "TLS_MODE must be one of"),
        ({"SCHEMAOPS_TRADES_PROD_TLS_CA": "/nonexistent/ca.pem"}, "does not exist"),
        ({"SCHEMAOPS_TRADES_PROD_USER": "root"}, "least-privilege"),
        ({"SCHEMAOPS_TRADES_PROD_HOST": "db prod;rm"}, "not a valid host name"),
        ({"SCHEMAOPS_TRADES_PROD_PORT": "99999"}, "between 1 and 65535"),
        ({"SCHEMAOPS_TRADES_PROD_PORT": "abc"}, "must be a number"),
    ],
)
def test_unsafe_configuration_is_rejected_before_connecting(tmp_path: Path, overrides: dict[str, str], message: str):
    with pytest.raises(ConfigError, match=message) as error:
        ConnectionConfig.from_env("trades", "prod", environ=prod(tmp_path, **overrides))
    assert "s3cr3t" not in str(error.value)


def test_tls_contexts(tmp_path: Path):
    assert ConnectionConfig.from_env("trades", "dev", environ={**DEV, "SCHEMAOPS_TRADES_DEV_TLS_MODE": "disabled"}).ssl_context() is None
    strict = ConnectionConfig.from_env("trades", "dev", environ=DEV).ssl_context()
    assert strict is not None and strict.check_hostname
    verify_ca = ConnectionConfig.from_env("trades", "dev", environ={**DEV, "SCHEMAOPS_TRADES_DEV_TLS_MODE": "verify-ca"}).ssl_context()
    assert verify_ca is not None and not verify_ca.check_hostname


def test_password_never_appears_in_repr_summary_or_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config = ConnectionConfig.from_env("trades", "prod", environ=prod(tmp_path))
    assert config.password not in repr(config)
    assert "password" not in config.describe()
    for fmt in ("json", "text"):
        log.configure(fmt)
        logging.getLogger("schemaops.test").error("connect failed for %s", config.password, extra={"dsn": f"pw={config.password}"})
    assert "s3cr3t" not in capsys.readouterr().err


def test_client_options_file_is_private_and_escaped(tmp_path: Path):
    config = ConnectionConfig.from_env("trades", "prod", environ=prod(tmp_path))
    path = tmp_path / "client.cnf"
    config.client_options_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert 'password="s3cr3t\\"prod\\\\"' in text
    assert "ssl-verify-server-cert" in text and "ssl-ca=" in text


def test_cluster_yaml_supplies_non_secret_details_and_variables_override_them():
    credentials = {"SCHEMAOPS_TRADES_DEV_USER": "schemaops_dev", "SCHEMAOPS_TRADES_DEV_PASSWORD": "s3cr3t-dev"}
    defaults = {"host": "from-yaml.internal", "port": "3307", "tls-mode": "verify-ca"}
    config = ConnectionConfig.from_env("trades", "dev", environ=credentials, defaults=defaults)
    assert (config.host, config.port, config.tls_mode) == ("from-yaml.internal", 3307, "verify-ca")

    overridden = {**credentials, "SCHEMAOPS_TRADES_DEV_HOST": "from-variable.internal", "SCHEMAOPS_TRADES_DEV_PORT": "3308"}
    config = ConnectionConfig.from_env("trades", "dev", environ=overridden, defaults=defaults)
    assert (config.host, config.port) == ("from-variable.internal", 3308)

    with pytest.raises(ConfigError, match="tls-mode|TLS_MODE"):  # cluster.yaml cannot weaken TLS outside DEV
        prod = {"SCHEMAOPS_TRADES_PROD_USER": "u", "SCHEMAOPS_TRADES_PROD_PASSWORD": "p"}
        ConnectionConfig.from_env("trades", "prod", environ=prod, defaults={"host": "db", "tls-mode": "disabled"})
