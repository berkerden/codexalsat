"""Independent open-position equity path and report checks; no exchange access."""

from decimal import Decimal as D

from backend.tests.test_engine import AT, config, quote, tick
from backend.tests.test_engine import engine as engine


def test_open_position_recovery_preserves_worst_marked_drawdown(engine):
    engine.start(config(), AT)
    entered = tick(engine, quote())
    # Independent cash/fee example: buy 8.34 at 100 with 0.834 fee.
    assert D(entered["cash"]) == D("165.166")
    assert D(entered["equity"]) == D("998.332")
    assert D(entered["observed_drawdown"]["maximum_drawdown"]) == D(".001668")

    higher = tick(engine, quote("101", "101", AT + 1000), signal=False)
    assert D(higher["equity"]) == D("1006.66366")
    assert D(higher["drawdown"]) == 0
    lower = tick(engine, quote("99.5", "99.5", AT + 2000), signal=False)
    assert D(lower["equity"]) == D("994.16617")
    worst = (D("1006.66366") - D("994.16617")) / D("1006.66366")
    assert D(lower["observed_drawdown"]["maximum_drawdown"]) == worst
    recovered = tick(engine, quote("100", "100", AT + 3000), signal=False)
    assert recovered["position"] is not None
    assert D(recovered["realized_pnl"]) == 0
    assert D(recovered["drawdown"]) < worst
    assert D(recovered["observed_drawdown"]["maximum_drawdown"]) == worst
    assert recovered["observed_drawdown"]["maximum_drawdown_at"] == AT + 2000
    before = engine.snapshot()
    # Read-only report gets stale; preserved maximum and raw ledger never move.
    report = engine.observation_report(before["session_id"], now=AT + 9000)
    result = report["results"]["observed_drawdown"]
    assert result["freshness"] == "stale"
    assert result["quote_age_ms"] == 6000
    assert result["available"] is True
    assert D(result["maximum_drawdown"]) == worst
    assert engine.snapshot() == before


def test_stale_quote_never_fabricates_drawdown_measurement(engine):
    engine.start(config(), AT)
    entered = tick(engine, quote())
    old = entered["observed_drawdown"]
    rejected = tick(engine, quote("80", "80", AT), now=AT + 5001)
    assert rejected["observed_drawdown"] == old
    assert rejected["position"] is not None
    assert rejected["entries_enabled"] is False


def test_partial_exit_captures_post_fill_slippage_drop(engine):
    engine.start(config(
        risk_per_trade=".1", max_open_risk=".1", daily_loss_limit=".5",
        max_drawdown=".8", stop_pct=".1", target_pct=".5", slippage_bps="100",
    ), AT)
    entered = tick(engine, quote())
    # floor(100 / (101.101 - 89.901009), .01) = 8.92.
    assert entered["position"]["quantity"] == "8.92"
    assert D(entered["cash"]) == D("98.17908")
    # The actual simulated sale pays slippage; marking the remaining lot does not.
    engine.request_close()
    partial = tick(engine, quote("90", "90", AT + 1000, quantity="2"), signal=False)
    assert partial["position"]["quantity"] == "6.92"
    assert D(partial["cash"]) == D("276.20088")
    assert D(partial["equity"]) == D("898.37808")
    assert D(partial["observed_drawdown"]["maximum_drawdown"]) == D(".10162192")
    assert partial["observed_drawdown"]["maximum_drawdown_at"] == AT + 1000
