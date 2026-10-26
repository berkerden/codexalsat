import asyncio
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from backend.tests.test_orders import database_url as database_url
from backend.tests.test_testnet import NOW as EXCHANGE_TIME
from backend.tests.test_testnet import credentials
from backend.tests.test_testnet_cycle import StatefulTestnetVenue, recovery_terms
from fastapi.testclient import TestClient

from spotlab.main import create_app
from spotlab.orders import TESTNET_ACCOUNT_VENUE
from spotlab.testnet import BinanceTestnetTransport
from spotlab.testnet_cycle import TestnetCycle as Cycle
from spotlab.testnet_panel import TestnetPanel as PanelController
from spotlab.testnet_panel import WatchStartInput

ORIGIN = "http://127.0.0.1:5173"


def make_cycle_ledger(path: Path, *, state: str = "ENTRY_OPEN") -> str:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE testnet_cycles ("
            "cycle_id VARCHAR(64) PRIMARY KEY,plan TEXT NOT NULL,state VARCHAR(40) NOT NULL,"
            "protection_quantity VARCHAR(128),dust VARCHAR(128),reason VARCHAR(80),"
            "stop_entry INTEGER NOT NULL DEFAULT 0,updated_at BIGINT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO testnet_cycles (cycle_id,plan,state,stop_entry,updated_at) "
            "VALUES (?,?,?,?,?)",
            (
                "existing",
                json.dumps({"symbol": "SOLUSDT"}),
                state,
                0,
                1_700_000_000_000,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return f"sqlite:///{path}"


@pytest.fixture
def panel_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SPOTLAB_DATA_DIR", str(data_dir))
    app = create_app(f"sqlite:///{tmp_path / 'paper.sqlite'}", run_worker=False)
    with TestClient(app) as client:
        yield client


def control_headers(token: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-Spotlab-Control-Token": token}


def test_missing_ledger_status_is_read_only(panel_client: TestClient, tmp_path: Path) -> None:
    ledger = tmp_path / "data" / "testnet-orders.sqlite"
    assert not ledger.exists()
    payload = panel_client.get("/api/testnet/status").json()
    assert payload["ledger_status"] == "missing"
    assert payload["cycles"] == []
    assert payload["watcher"] == {
        "run_id": None,
        "cycle_id": None,
        "symbol": None,
        "allow_mutations": False,
        "phase": "idle",
        "health": "idle",
        "manager_running": False,
        "coverage_observation_fresh": False,
        "snapshot": None,
    }
    assert not ledger.exists()


def test_session_and_post_control_gate(panel_client: TestClient) -> None:
    session = panel_client.get("/api/testnet/session").json()
    assert session["server_instance_id"]
    assert len(session["control_token"]) >= 32
    assert panel_client.get("/api/testnet/session", headers={"Origin": "null"}).status_code == 403
    assert (
        panel_client.get(
            "/api/testnet/session", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )

    body = {"cycle_id": "existing", "confirm_testnet": False}
    assert panel_client.post("/api/testnet/watch/start", json=body).status_code == 403
    assert (
        panel_client.post(
            "/api/testnet/watch/start", json=body, headers={"Origin": ORIGIN}
        ).status_code
        == 403
    )
    assert (
        panel_client.post(
            "/api/testnet/watch/start",
            content=json.dumps(body),
            headers={
                **control_headers(session["control_token"]),
                "Content-Type": "text/plain",
            },
        ).status_code
        == 415
    )


def test_stop_payload_forbids_browser_options(panel_client: TestClient) -> None:
    session = panel_client.get("/api/testnet/session").json()
    response = panel_client.post(
        "/api/testnet/watch/not-current/stop",
        json={"database": "sqlite:///elsewhere.sqlite"},
        headers=control_headers(session["control_token"]),
    )
    assert response.status_code == 422


def test_strict_payload_rejected_before_credentials(
    panel_client: TestClient, tmp_path: Path
) -> None:
    make_cycle_ledger(tmp_path / "data" / "testnet-orders.sqlite")
    panel = panel_client.app.state.testnet_panel
    loader = Mock(side_effect=AssertionError("credentials must not be read"))
    panel.credentials_loader = loader
    session = panel_client.get("/api/testnet/session").json()
    headers = control_headers(session["control_token"])

    assert (
        panel_client.post(
            "/api/testnet/watch/start",
            json={"cycle_id": "existing", "confirm_testnet": "false"},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        panel_client.post(
            "/api/testnet/watch/start",
            json={"cycle_id": "existing", "confirm_testnet": False, "symbol": "BTCUSDT"},
            headers=headers,
        ).status_code
        == 422
    )
    loader.assert_not_called()


def test_start_returns_starting_and_sanitizes_credential_failure(
    panel_client: TestClient, tmp_path: Path
) -> None:
    make_cycle_ledger(tmp_path / "data" / "testnet-orders.sqlite")
    panel = panel_client.app.state.testnet_panel
    panel.credentials_loader = Mock(side_effect=ValueError("secret material"))
    session = panel_client.get("/api/testnet/session").json()
    response = panel_client.post(
        "/api/testnet/watch/start",
        json={"cycle_id": "existing", "confirm_testnet": True},
        headers=control_headers(session["control_token"]),
    )
    assert response.status_code == 202
    watcher = response.json()["watcher"]
    assert watcher["phase"] == "starting"
    assert watcher["manager_running"] is True
    assert watcher["snapshot"] is None

    payload = panel_client.get("/api/testnet/status").json()
    assert payload["watcher"]["phase"] == "attention"
    assert payload["watcher"]["snapshot"]["reason"] == "credentials_unavailable"
    assert "secret" not in response.text + json.dumps(payload)


@pytest.mark.asyncio
async def test_immediate_stop_releases_task_that_never_started(tmp_path: Path) -> None:
    database_url = make_cycle_ledger(tmp_path / "ledger.sqlite")
    panel = PanelController(database_url, tmp_path / "credentials.json")
    started = await panel.start(WatchStartInput(cycle_id="existing", confirm_testnet=False))
    run_id = started["watcher"]["run_id"]
    assert isinstance(run_id, str)

    stopped = await panel.stop(run_id)
    assert stopped["watcher"]["phase"] == "stopped"
    assert stopped["watcher"]["manager_running"] is False
    again = await panel.stop(run_id)
    assert again["watcher"]["phase"] == "stopped"


@pytest.mark.asyncio
async def test_get_projects_stale_protection_conservatively(tmp_path: Path) -> None:
    database_url = make_cycle_ledger(tmp_path / "ledger.sqlite", state="PROTECTED")
    now = [10_000]
    panel = PanelController(database_url, tmp_path / "credentials.json", clock_ms=lambda: now[0])
    blocker = asyncio.Event()
    panel._task = asyncio.create_task(blocker.wait())
    panel._run_id = "run"
    panel._cycle_id = "existing"
    panel._symbol = "SOLUSDT"
    panel._phase = "running"
    panel._health = "healthy"
    panel._snapshot = {
        "cycle_id": "existing",
        "state": "PROTECTED",
        "reason": None,
        "owned_quantity": "1.25",
        "unprotected_quantity": "0",
        "coverage_observation_fresh": True,
        "observed_at_ms": 10_000,
        "checked_at_ms": 10_000,
    }
    try:
        fresh = await panel.status()
        assert fresh["watcher"]["coverage_observation_fresh"] is True
        assert fresh["watcher"]["snapshot"]["state"] == "PROTECTED"
        assert fresh["cycles"][0]["state"] == "PROTECTION_UNVERIFIED"

        now[0] = 16_000
        expired = await panel.status()
        assert expired["watcher"]["health"] == "healthy"
        assert expired["watcher"]["coverage_observation_fresh"] is False
        assert expired["watcher"]["snapshot"]["state"] == "PROTECTION_UNVERIFIED"
        assert expired["watcher"]["snapshot"]["unprotected_quantity"] == "1.25"

        now[0] = 21_000
        stale = await panel.status()
        assert stale["watcher"]["health"] == "stale"
    finally:
        panel._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await panel._task


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_panel_runs_real_watcher_without_resending_buy(
    tmp_path: Path, database_url: str, confirmed: bool
) -> None:
    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)
    async with BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: EXCHANGE_TIME,
    ) as transport:
        cycle = Cycle(database_url, transport, clock_ms=lambda: EXCHANGE_TIME)
        cycle.store.bind_account(TESTNET_ACCOUNT_VENUE, 12345)
        await cycle.start(terms)
        cycle.store.db.dispose()
    mutations_before = list(venue.mutation_payloads)
    if confirmed:
        venue.entry_status = "PARTIALLY_FILLED"
        venue.entry_executed = D("0.4")
        venue.entry_trades = [
            {
                "symbol": "BTCUSDT",
                "orderId": 100,
                "id": 1,
                "qty": "0.4",
                "price": "100",
                "quoteQty": "40",
                "commission": "0.04",
                "commissionAsset": "USDT",
            }
        ]

    panel = PanelController(database_url, tmp_path / "credentials.json")
    panel.credentials_loader = lambda _path: credentials()
    panel.transport_factory = lambda symbol, credentials: BinanceTestnetTransport(
        symbol,
        credentials=credentials,
        mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: EXCHANGE_TIME,
    )
    started = await panel.start(WatchStartInput(cycle_id="one", confirm_testnet=confirmed))
    run_id = started["watcher"]["run_id"]
    assert isinstance(run_id, str)
    for _ in range(100):
        current = await panel.status()
        if current["watcher"]["health"] == "healthy":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("watcher did not reach a healthy observation")

    stopped = await panel.stop(run_id)
    assert stopped["watcher"]["manager_running"] is False
    assert venue.entry_posts == 1
    if confirmed:
        assert current["watcher"]["snapshot"]["state"] == "PROTECTED"
        assert current["watcher"]["snapshot"]["stop_entry_requested"] is True
        assert venue.cancel_attempts == venue.oco_posts == 1
    else:
        assert current["watcher"]["snapshot"]["stop_entry_requested"] is False
        assert venue.mutation_payloads == mutations_before
