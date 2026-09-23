from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from spotlab.contracts import Quote
from spotlab.engine import PaperConfig, millis
from spotlab.main import create_app, paper_cycle
from spotlab.market import MarketDataUnavailable


def paper_config() -> PaperConfig:
    return PaperConfig(
        venue="global",
        symbol="BTCUSDT",
        interval="5m",
        strategy="pullback",
        capital=D("1000"),
        risk_per_trade=D(".01"),
        daily_loss_limit=D(".5"),
        max_drawdown=D(".8"),
        max_open_risk=D(".02"),
        target_pct=D(".02"),
        stop_pct=D(".01"),
        fee_rate=D(".001"),
        slippage_bps=D(0),
        max_spread_bps=D(30),
        hold_minutes=60,
        max_trades_per_day=10,
        max_consecutive_losses=1,
        authorization_minutes=120,
    )


def fresh_quote(price: str = "100", quantity: str = "1000") -> Quote:
    return Quote("BTCUSDT", D(price), D(price), D(quantity), D(quantity), millis())


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SPOTLAB_DATA_DIR", str(tmp_path / "data"))
    app = create_app("sqlite:///" + str(tmp_path / "paper.sqlite"), run_worker=False)
    with TestClient(app) as session:
        yield session


def open_position(client: TestClient) -> None:
    engine = client.app.state.engine
    engine.start(paper_config())
    engine.tick(
        fresh_quote(), signal=True, candle_time=millis() - 1, step=D(".01"), min_notional=D("5")
    )


def test_default_status_and_live_interlock(client: TestClient) -> None:
    status = client.get("/api/status").json()
    assert status["mode"] == "suggestions"
    assert not status["live_enabled"]
    assert not status["runtime"]["real_order_transport"]
    assert client.post("/api/live/activate", json={}).status_code == 409
    assert client.post("/api/paper/start", json={}).status_code == 422
    assert client.get("/api/symbols?venue=tr").status_code == 422


def test_cross_origin_control_and_dns_rebinding_are_denied(client: TestClient) -> None:
    assert (
        client.post("/api/paper/stop", headers={"Origin": "https://evil.invalid"}).status_code
        == 403
    )
    assert client.get("/api/status", headers={"Host": "evil.invalid"}).status_code == 400


def test_manual_close_survives_quote_outage(client: TestClient) -> None:
    open_position(client)
    with patch(
        "spotlab.main.current_quote", AsyncMock(side_effect=MarketDataUnavailable("offline"))
    ):
        assert client.post("/api/paper/close").status_code == 503
    engine = client.app.state.engine
    assert engine.snapshot()["position"]["exit_reason"] == "manual_close"
    assert not engine.snapshot()["entries_enabled"]
    state = engine.tick(
        fresh_quote(), signal=False, candle_time=0, step=D(".01"), min_notional=D(5)
    )
    assert state["position"] is None


@pytest.mark.asyncio
async def test_metadata_outage_cannot_block_quote_driven_stop(client: TestClient) -> None:
    open_position(client)
    app = client.app
    app.state.quote = fresh_quote("98")
    failing = AsyncMock(side_effect=MarketDataUnavailable("metadata offline"))
    original = app.state.market
    app.state.market = SimpleNamespace(exchange_info=failing, clock=failing, latest=failing)
    try:
        await paper_cycle(app)
        assert app.state.engine.snapshot()["position"] is None
        failing.assert_not_awaited()
    finally:
        app.state.market = original


def test_partial_stop_latches_and_aggregate_loss_counts(client: TestClient) -> None:
    open_position(client)
    engine = client.app.state.engine
    partial = engine.tick(
        fresh_quote("98", "8"), signal=False, candle_time=0, step=D(".01"), min_notional=D(5)
    )
    assert D(partial["position"]["quantity"]) == D(".34")
    # The price recovers above the stop and target; the residual exit still executes.
    final = engine.tick(
        fresh_quote("103"), signal=False, candle_time=0, step=D(".01"), min_notional=D(5)
    )
    assert final["position"] is None
    assert D(final["realized_pnl"]) == D("-16.63302")
    assert final["loss_streak"] == 1


def test_research_invalid_input_is_422_without_network(client: TestClient) -> None:
    response = client.post(
        "/api/research",
        json={
            "venue": "global",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "days": 0,
            "hold_minutes": 60,
        },
    )
    assert response.status_code == 422
