import json
from dataclasses import replace
from decimal import Decimal

import pytest

from spotlab.contracts import Candle
from spotlab.finance import CostModel
from spotlab.research import _metrics, _trades, run_research, similar_patterns, strategy_signal


def candle(index: int, close: str, *, volume: str = "100") -> Candle:
    price = Decimal(close)
    interval = 60_000
    return Candle(
        open_time=index * interval,
        close_time=(index + 1) * interval - 1,
        open=price - Decimal("0.1"),
        high=price + Decimal("0.5"),
        low=price - Decimal("0.5"),
        close=price,
        volume=Decimal(volume),
    )


def zero_costs() -> CostModel:
    return CostModel(
        entry_fee=Decimal("0"),
        exit_fee=Decimal("0"),
        spread_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def test_breakout_signal_is_causal_and_accepts_ui_name() -> None:
    candles = [candle(index, "100") for index in range(20)]
    candles.append(candle(20, "102", volume="200"))
    original = strategy_signal(candles, "breakout")

    future = candle(21, "50", volume="10000")
    extended = strategy_signal([*candles, future], "breakout")

    assert original[20] == 1
    assert extended[: len(original)] == original
    assert set(original) <= {0, 1}


def test_same_candle_target_and_stop_uses_stop_first() -> None:
    candles = [candle(0, "100")]
    candles.append(
        Candle(
            open_time=60_000,
            close_time=119_999,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=Decimal("100"),
        )
    )
    candles.append(candle(2, "100"))

    trades = _trades(
        candles,
        [1, 0, 0],
        start=1,
        end=3,
        horizon=1,
        target_pct=Decimal("0.01"),
        stop_pct=Decimal("0.01"),
        costs=zero_costs(),
    )

    assert len(trades) == 1
    assert trades[0]["return"] == Decimal("-0.01")
    assert trades[0]["exit_reason"] == "stop_ambiguous"


def test_research_is_repeatable_json_and_small_data_cannot_authorize_trade() -> None:
    candles = [
        candle(index, str(Decimal("100") + Decimal(index % 11) / Decimal("10")))
        for index in range(240)
    ]

    first = run_research(candles, "1m", 5, zero_costs(), code_version="abc123")
    second = run_research(candles, "1m", 5, zero_costs(), code_version="abc123")

    assert first == second
    json.dumps(first)
    assert first["reproducibility"]["data_sha256"]
    assert first["reproducibility"]["code_version"] == "abc123"
    assert first["reproducibility"]["trial_count"] == 9
    assert first["protocol"]["sealed_test_evaluations"] == 1
    assert first["decision"]["action"] == "İŞLEM YAPMA"
    assert first["data"]["pilot"] is True
    assert "less_than_180_days" in first["decision"]["blockers"]


def test_research_rejects_gaps_and_wrong_candle_widths() -> None:
    candles = [candle(index, "100") for index in range(30)]
    candles[15] = replace(
        candles[15],
        open_time=candles[15].open_time + 86_400_000,
        close_time=candles[15].close_time + 86_400_000,
    )
    for index in range(16, len(candles)):
        candles[index] = replace(
            candles[index],
            open_time=candles[index].open_time + 86_400_000,
            close_time=candles[index].close_time + 86_400_000,
        )
    with pytest.raises(ValueError, match="continuous"):
        run_research(candles, "1m", 5, zero_costs())

    wrong_width = [candle(index, "100") for index in range(30)]
    wrong_width[10] = replace(wrong_width[10], close_time=wrong_width[10].close_time - 1)
    with pytest.raises(ValueError, match="width"):
        run_research(wrong_width, "1m", 5, zero_costs())


def test_walk_forward_selection_is_independent_of_later_validation_data() -> None:
    original = [
        candle(index, str(Decimal("100") + Decimal(index % 7) / Decimal("10")))
        for index in range(240)
    ]
    changed = original[:144]
    changed.extend(
        candle(index, str(Decimal("200") + Decimal(index % 5)))
        for index in range(144, 240)
    )

    first = run_research(original, "1m", 5, zero_costs())
    second = run_research(changed, "1m", 5, zero_costs())

    assert first["walk_forward_train_diagnostic"] == second["walk_forward_train_diagnostic"]


def test_drawdown_metric_does_not_claim_intrabar_portfolio_drawdown() -> None:
    candles = [candle(0, "100")]
    candles.append(
        Candle(
            open_time=60_000,
            close_time=119_999,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("95"),
            close=Decimal("101"),
            volume=Decimal("1"),
        )
    )
    trades = _trades(
        candles,
        [1, 0],
        1,
        2,
        1,
        Decimal("0.1"),
        Decimal("0.1"),
        zero_costs(),
    )
    metrics = _metrics(trades, Decimal("0.05"), 1)

    assert metrics["closed_trade_equity_drawdown"] == "0"
    assert metrics["portfolio_maximum_drawdown"] is None
    assert metrics["portfolio_maximum_drawdown_available"] is False
    assert "maximum_drawdown" not in metrics


def test_similarity_uses_only_past_non_overlapping_labeled_windows() -> None:
    candles = [
        candle(index, str(Decimal("100") + Decimal((index * 7) % 13) / Decimal("10")))
        for index in range(140)
    ]

    result = similar_patterns(candles, window=6, horizon=4, limit=8)

    assert result["interpretation"].endswith("not a success score or probability")
    assert result["mean_horizon_return_ci"]["observation_order"] == "chronological"
    assert [item["decision_time"] for item in result["matches"]] == sorted(
        item["decision_time"] for item in result["matches"]
    )
    assert sum(result["outcome_counts"].values()) == result["sample_count"]
    query_start_time = candles[-6].close_time
    spans: list[tuple[int, int]] = []
    for match in result["matches"]:
        assert match["outcome"] in {"target", "stop", "neither", "ambiguous"}
        assert match["outcome_end_time"] < query_start_time
        span = (match["feature_start_time"], match["outcome_end_time"])
        assert all(span[1] < left or span[0] > right for left, right in spans)
        spans.append(span)


def test_similarity_rejects_irregular_or_mismatched_intervals() -> None:
    candles = [candle(index, "100") for index in range(40)]
    for index in range(20, len(candles)):
        candles[index] = replace(
            candles[index],
            open_time=candles[index].open_time + 1,
            close_time=candles[index].close_time + 1,
        )
    with pytest.raises(ValueError, match="continuous|chronological"):
        similar_patterns(candles, window=6, horizon=4)

    regular = [candle(index, "100") for index in range(40)]
    with pytest.raises(ValueError, match="width"):
        similar_patterns(regular, window=6, horizon=4, interval="5m")
