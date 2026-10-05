import sys

import pytest

from spotlab import cycle_cli


def test_start_requires_explicit_testnet_confirmation_before_loading_credentials(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cycle_cli", "start", "--cycle", "one"])
    monkeypatch.setattr(
        cycle_cli, "load_credentials", lambda _: pytest.fail("must not load secrets")
    )
    with pytest.raises(SystemExit) as error:
        cycle_cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "options",
    [
        ["--watch-seconds", "601"],
        ["--entry-seconds", "0"],
        ["--stop-entry"],
    ],
)
def test_invalid_authority_or_duration_rejected_before_network(monkeypatch, options):
    monkeypatch.setattr(sys, "argv", ["cycle_cli", "manage", "--cycle", "one", *options])
    monkeypatch.setattr(
        cycle_cli, "load_credentials", lambda _: pytest.fail("must not load secrets")
    )
    with pytest.raises(SystemExit):
        cycle_cli.main()


def test_cli_sanitizes_failures_and_provides_resume_id(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cycle_cli", "manage", "--cycle", "one"])

    async def fail(_):
        raise RuntimeError("signed-url-and-secret")

    monkeypatch.setattr(cycle_cli, "run", fail)
    assert cycle_cli.main() == 1
    output = capsys.readouterr().out
    assert "signed-url-and-secret" not in output
    assert '"cycle_id": "one"' in output
    assert '"resume_required": true' in output
