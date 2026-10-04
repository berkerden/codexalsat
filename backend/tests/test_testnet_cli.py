import json
import os
import sys
from pathlib import Path

import pytest

from spotlab import testnet_cli


def test_configure_private_file_and_no_secret_output(tmp_path: Path, monkeypatch, capsys) -> None:
    values = iter(["fixture-key", "fixture-secret"])
    monkeypatch.setattr(testnet_cli.getpass, "getpass", lambda _: next(values))
    path = tmp_path / "private" / "credentials.json"
    testnet_cli.configure(path)
    assert path.stat().st_mode & 0o777 == 0o600
    credentials = testnet_cli.load_credentials(path)
    assert credentials.api_key.get_secret_value() == "fixture-key"
    assert "fixture-key" not in capsys.readouterr().out
    with pytest.raises(ValueError):
        testnet_cli.configure(path)


def test_credentials_reject_public_file_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"api_key": "fixture-key", "secret": "fixture-secret"}))
    path.chmod(0o644)
    with pytest.raises(ValueError):
        testnet_cli.load_credentials(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        testnet_cli.load_credentials(link)


def test_cli_validation_error_does_not_print_secret(tmp_path: Path, monkeypatch, capsys) -> None:
    for name in ("SPOTLAB_TESTNET_API_KEY", "SPOTLAB_TESTNET_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "credentials.json"
    path.write_text('{"api_key": "fixture-key", "secret": "", "extra": "secret-value"}')
    path.chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["testnet_cli", "preflight", "--credentials", str(path)])
    assert testnet_cli.main() == 1
    output = capsys.readouterr().out
    assert "fixture-key" not in output and "secret-value" not in output
    assert json.loads(output)["production_enabled"] is False


def test_status_requires_no_credentials_or_network(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["testnet_cli", "status", "--database", "sqlite:///" + str(tmp_path / "orders.sqlite")],
    )
    assert testnet_cli.main() == 0
    assert json.loads(capsys.readouterr().out)["orders"] == []


def test_credentials_environment_precedence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPOTLAB_TESTNET_API_KEY", "fixture-env-key")
    monkeypatch.setenv("SPOTLAB_TESTNET_API_SECRET", "fixture-env-secret")
    assert (
        testnet_cli.load_credentials(tmp_path / "absent").api_key.get_secret_value()
        == os.environ["SPOTLAB_TESTNET_API_KEY"]
    )
