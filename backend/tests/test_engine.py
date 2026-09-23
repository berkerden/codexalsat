import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import quote as urlquote

import pytest
from sqlalchemy import create_engine, text

from spotlab.contracts import Quote
from spotlab.engine import PaperConfig, PaperEngine

AT = 1_790_000_000_000


def config(**changes: object) -> PaperConfig:
    values = dict(
        venue="global",
        symbol="BTCUSDT",
        interval="5m",
        strategy="pullback",
        capital="1000",
        risk_per_trade="0.01",
        daily_loss_limit="0.05",
        max_drawdown="0.1",
        max_open_risk="0.02",
        target_pct="0.02",
        stop_pct="0.01",
        fee_rate="0.001",
        slippage_bps="0",
        max_spread_bps="30",
        hold_minutes=60,
        max_trades_per_day=10,
        max_consecutive_losses=3,
        authorization_minutes=120,
    )
    values.update(changes)
    return PaperConfig.model_validate(values)


def quote(bid: str = "100", ask: str = "100", at: int = AT, quantity: str = "1000") -> Quote:
    return Quote("BTCUSDT", D(bid), D(ask), D(quantity), D(quantity), at)


def tick(engine: PaperEngine, q: Quote, **kwargs: object) -> dict:
    options = dict(
        signal=True, candle_time=AT - 1, step=D("0.01"), min_notional=D("5"), now=q.observed_at
    )
    options.update(kwargs)
    return engine.tick(q, **options)


@pytest.fixture(params=["sqlite", "postgres"])
def engine(tmp_path: Path, request: pytest.FixtureRequest):
    if request.param == "sqlite":
        instance = PaperEngine("sqlite:///" + str(tmp_path / "paper.sqlite"))
        yield instance
        instance.db.dispose()
        return
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("PostgreSQL service is exercised in CI")
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    instance = PaperEngine(database_url + "?options=" + urlquote("-csearch_path=" + schema))
    try:
        yield instance
    finally:
        instance.db.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_independent_cost_and_profit(engine: PaperEngine) -> None:
    engine.start(config(), AT)
    state = tick(engine, quote())
    # Unit stop loss 100.1 - 99*.999 = 1.199; floor(10/1.199,.01) = 8.34.
    assert state["position"]["quantity"] == "8.34"
    assert D(state["cash"]) == D("165.166")
    assert D(state["fills"][0]["fee"]) == D("0.834")
    state = tick(engine, quote("102", "102", AT + 1000))
    assert state["position"] is None
    # 8.34 * (102*.999 - 100*1.001) = 14.99532 exactly.
    assert D(state["realized_pnl"]) == D("14.99532")
    assert D(state["cash"]) == D("1014.99532")


def test_stop_keeps_position_protection(engine: PaperEngine) -> None:
    engine.start(config(), AT)
    tick(engine, quote())
    stopped = engine.stop()
    assert stopped["position"] and not stopped["entries_enabled"]
    exited = tick(engine, quote("98", "98", AT + 1000))
    assert exited["position"] is None
    assert exited["fills"][-1]["reason"] == "stop"
    assert D(exited["realized_pnl"]) == D("-18.33132")


def test_concurrent_duplicate_signals_spend_once(engine: PaperEngine) -> None:
    engine.start(config(), AT)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: tick(engine, quote()), range(8)))
    assert len(engine.snapshot()["fills"]) == 1
    assert D(engine.snapshot()["cash"]) >= 0


def test_restart_retains_position_but_revokes_entries(engine: PaperEngine) -> None:
    engine.start(config(), AT)
    tick(engine, quote())
    restarted = PaperEngine(engine.db.url.render_as_string(hide_password=False))
    state = restarted.snapshot()
    assert state["position"] and not state["entries_enabled"]
    assert len(state["fills"]) == 1
    restarted.db.dispose()


def test_stale_quote_blocks_and_preserves_position(engine: PaperEngine) -> None:
    engine.start(config(), AT)
    tick(engine, quote())
    state = tick(engine, quote("90", "90"), now=AT + 5001)
    assert state["position"] and not state["entries_enabled"]
    assert len(state["fills"]) == 1


def test_partial_exit_no_short_and_no_double_fee(engine: PaperEngine) -> None:
    engine.start(config(), AT)
    tick(engine, quote())
    partial = tick(engine, quote("102", "102", AT + 1000, "2"))
    assert D(partial["position"]["quantity"]) == D("6.34")
    final = tick(engine, quote("102", "102", AT + 2000, "100"))
    assert final["position"] is None
    assert D(final["cash"]) == D("1014.99532")


def test_expiry_and_daily_loss_are_backend_enforced(engine: PaperEngine) -> None:
    engine.start(config(authorization_minutes=1), AT)
    state = tick(engine, quote(at=AT + 60_001))
    assert not state["entries_enabled"] and state["position"] is None


def test_insufficient_notional_and_high_spread_do_not_fill(engine: PaperEngine) -> None:
    engine.start(config(), AT)
    assert not tick(engine, quote("99", "100"))["position"]
    state = tick(engine, quote(), min_notional=D("10000"))
    assert not state["position"]


def test_restore_verified_and_does_not_restore_authorization(
    engine: PaperEngine, tmp_path: Path
) -> None:
    engine.start(config(), AT)
    tick(engine, quote())
    backup = tmp_path / "backup.json"
    engine.backup(str(backup))
    other = PaperEngine("sqlite:///" + str(tmp_path / "restored.sqlite"))
    other.restore(str(backup))
    restored = other.snapshot()
    assert restored["position"] == engine.snapshot()["position"]
    assert restored["cash"] == engine.snapshot()["cash"]
    assert not restored["entries_enabled"]
    with pytest.raises(ValueError):
        other.restore(str(backup))


def test_nonfinite_and_incoherent_limits_rejected() -> None:
    for change in (
        {"capital": "NaN"},
        {"capital": "Infinity"},
        {"risk_per_trade": "0.05", "max_open_risk": "0.01"},
    ):
        with pytest.raises(ValueError):
            config(**change)
