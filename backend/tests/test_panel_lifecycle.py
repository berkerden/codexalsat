import asyncio
from pathlib import Path
from types import MethodType
from unittest.mock import Mock

import pytest
from backend.tests.test_orders import database_url as database_url
from backend.tests.test_testnet_panel import make_cycle_ledger
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from spotlab.main import create_app
from spotlab.testnet_panel import PanelError, WatchStartInput, read_ledger
from spotlab.testnet_panel import TestnetPanel as Panel


@pytest.mark.asyncio
async def test_concurrent_start_and_stop_cannot_replace_unwinding_task(tmp_path: Path) -> None:
    panel = Panel(make_cycle_ledger(tmp_path / "ledger.sqlite"), tmp_path / "unused")
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def worker(self, *_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.append(True)

    panel._run = MethodType(worker, panel)
    results = await asyncio.gather(
        panel.start(WatchStartInput(cycle_id="existing")),
        panel.start(WatchStartInput(cycle_id="existing", confirm_testnet=True)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PanelError) for r in results) == 1
    assert next(r for r in results if isinstance(r, PanelError)).code == "watcher_active"
    await entered.wait()
    run = panel._run_id
    with pytest.raises(PanelError, match="stale_run_id"):
        await panel.stop("old-run")
    first_stop = asyncio.create_task(panel.stop(run))
    await cleaning.wait()
    second_stop = asyncio.create_task(panel.stop(run))
    await asyncio.sleep(0)
    assert panel._task is not None and not panel._task.done()
    with pytest.raises(PanelError, match="watcher_active"):
        await panel.start(WatchStartInput(cycle_id="existing"))
    release.set()
    await asyncio.gather(first_stop, second_stop)
    assert cleaned == [True]
    assert (await panel.status())["watcher"]["manager_running"] is False
    assert (await panel.status())["watcher"]["phase"] == "stopped"


@pytest.mark.asyncio
async def test_shutdown_waits_for_cleanup_and_refuses_restart(tmp_path: Path) -> None:
    panel = Panel(make_cycle_ledger(tmp_path / "ledger.sqlite"), tmp_path / "unused")
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker(self, *_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    panel._run = MethodType(worker, panel)
    await panel.start(WatchStartInput(cycle_id="existing"))
    await entered.wait()
    shutdown = asyncio.create_task(panel.shutdown())
    await cleaning.wait()
    assert not shutdown.done()
    with pytest.raises(PanelError, match="server_shutting_down"):
        await panel.start(WatchStartInput(cycle_id="existing"))
    release.set()
    await shutdown
    assert not (await panel.status())["watcher"]["manager_running"]
    restarted = Panel(panel.database_url, tmp_path / "unused")
    assert restarted.control_token != panel.control_token
    assert not restarted.valid_token(panel.control_token)
    assert (await restarted.status())["watcher"]["phase"] == "idle"
    assert (await restarted.status())["watcher"]["snapshot"] is None


def test_read_ledger_never_creates_schema_or_modifies_rows(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.connect():
        pass
    assert read_ledger(database_url).status == "missing"
    assert inspect(engine).get_table_names() == []
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE testnet_cycles (cycle_id TEXT, plan TEXT, state TEXT, "
                "stop_entry INTEGER, updated_at BIGINT)"
            )
        )
        conn.execute(
            text("INSERT INTO testnet_cycles VALUES ('existing', :plan, 'PROTECTED', 1, 10)"),
            {"plan": '{"symbol":"BTCUSDT"}'},
        )
    with engine.connect() as conn:
        before = list(conn.execute(text("SELECT * FROM testnet_cycles")))
    result = read_ledger(database_url)
    assert result.cycles[0].state == "PROTECTION_UNVERIFIED"
    with engine.connect() as conn:
        assert list(conn.execute(text("SELECT * FROM testnet_cycles"))) == before
    assert inspect(engine).get_table_names() == ["testnet_cycles"]
    engine.dispose()


def test_sqlite_special_path_and_invalid_config_fail_safely(tmp_path: Path) -> None:
    url = make_cycle_ledger(tmp_path / "ledger#%.sqlite")
    assert read_ledger(url).status == "ready"
    assert read_ledger("bad secret configuration").status == "unavailable"
    assert not Panel(url, tmp_path / "unused").valid_token("\xff")


def test_custom_cors_does_not_widen_testnet_control(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPOTLAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CORS_ORIGINS", "https://custom.example")
    app = create_app(f"sqlite:///{tmp_path / 'paper.sqlite'}", run_worker=False)
    with TestClient(app) as client:
        loader = Mock(side_effect=AssertionError("must not read credentials"))
        app.state.testnet_panel.credentials_loader = loader
        token = client.get("/api/testnet/session").json()["control_token"]
        assert (
            client.post("/api/paper/stop", headers={"Origin": "https://custom.example"}).status_code
            == 200
        )
        for endpoint in ("status", "session"):
            assert (
                client.get(
                    "/api/testnet/" + endpoint, headers={"Origin": "https://custom.example"}
                ).status_code
                == 403
            )
        for origin in ("https://custom.example", "null", "http://localhost.evil:5173"):
            assert (
                client.post(
                    "/api/testnet/watch/start",
                    json={"cycle_id": "existing"},
                    headers={
                        "Origin": origin,
                        "X-Spotlab-Control-Token": token,
                    },
                ).status_code
                == 403
            )
        assert (
            client.post(
                "/api/testnet/watch/start",
                json={"cycle_id": "missing"},
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-Spotlab-Control-Token": token,
                },
            ).status_code
            == 404
        )
        loader.assert_not_called()
