"""Deterministic, causal spot-strategy research with a sealed chronological test."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final, TypedDict

from .contracts import INTERVAL_MS, Candle
from .finance import CostModel

ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")
RESEARCH_SEED: Final = 1729
MAX_CANDLES: Final = 300_000
BOOTSTRAP_SAMPLES: Final = 400

STRATEGIES: Final = ("pullback", "breakout", "mean_reversion")
_STRATEGY_ALIASES: Final = {
    "trend_pullback": "pullback",
    "volume_breakout": "breakout",
}
_CANDIDATE_LEVELS: Final = (
    (Decimal("0.004"), Decimal("0.003")),
    (Decimal("0.007"), Decimal("0.004")),
    (Decimal("0.010"), Decimal("0.006")),
)


class _Candidate(TypedDict):
    strategy: str
    target_pct: Decimal
    stop_pct: Decimal


def _validate_candles(candles: Sequence[Candle]) -> list[Candle]:
    result = list(candles)
    previous_close = -1
    for candle in result:
        if not isinstance(candle, Candle):
            raise TypeError("candles must contain Candle values")
        if candle.open_time <= previous_close or candle.close_time <= candle.open_time:
            raise ValueError("candles must be strictly chronological and non-overlapping")
        if min(candle.open, candle.high, candle.low, candle.close) <= ZERO:
            raise ValueError("candle prices must be positive")
        if candle.low > min(candle.open, candle.close) or candle.high < max(
            candle.open, candle.close
        ):
            raise ValueError("invalid OHLC candle")
        if candle.volume < ZERO:
            raise ValueError("candle volume cannot be negative")
        previous_close = candle.close_time
    return result


def _validate_interval(data: Sequence[Candle], interval_ms: int | None = None) -> int | None:
    """Require exact candle widths and an uninterrupted regular time grid."""

    if not data:
        return interval_ms
    inferred = data[0].close_time - data[0].open_time + 1
    expected = interval_ms if interval_ms is not None else inferred
    if expected not in INTERVAL_MS.values():
        raise ValueError("candle interval is not a supported Binance interval")
    for index, candle in enumerate(data):
        if candle.close_time - candle.open_time + 1 != expected:
            raise ValueError("candle width does not match interval")
        if index and candle.open_time - data[index - 1].open_time != expected:
            raise ValueError("candles must be continuous at the declared interval")
    return expected


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values))


def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def strategy_signal(candles: Sequence[Candle], strategy: str) -> list[int]:
    """Return causal long-entry signals aligned to candle closes.

    Each rule uses only the current closed candle and earlier candles.  Values
    are 1 (eligible long entry next bar) or 0 (wait); spot short signals do not
    exist.
    """

    data = _validate_candles(candles)
    strategy = _STRATEGY_ALIASES.get(strategy, strategy)
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    signals = [0] * len(data)
    for index in range(20, len(data)):
        recent_closes = [item.close for item in data[index - 19 : index + 1]]
        slow = _mean(recent_closes)
        fast = _mean(recent_closes[-5:])
        previous_fast = _mean([item.close for item in data[index - 5 : index]])
        current = data[index]
        if strategy == "pullback":
            # Established rising trend, shallow pullback, then positive close.
            signals[index] = int(
                fast > slow
                and fast > previous_fast
                and current.close < fast
                and current.close > current.open
                and current.close > data[index - 1].close
            )
        elif strategy == "breakout":
            prior = data[index - 20 : index]
            prior_high = max(item.high for item in prior)
            average_volume = _mean([item.volume for item in prior])
            signals[index] = int(
                current.close > prior_high
                and current.close > current.open
                and current.volume > average_volume * Decimal("1.5")
            )
        else:
            variance = _mean([(value - slow) ** 2 for value in recent_closes])
            standard_deviation = variance.sqrt()
            flat_regime = abs(fast / slow - ONE) <= Decimal("0.006")
            signals[index] = int(
                flat_regime
                and standard_deviation > ZERO
                and current.close < slow - standard_deviation * Decimal("1.25")
                and current.close > current.open
            )
    return signals


def _net_return(entry: Decimal, exit_price: Decimal, costs: CostModel) -> Decimal:
    adjustment = costs.adverse_fill_rate
    paid = entry * (ONE + adjustment) * (ONE + costs.entry_fee)
    received = exit_price * (ONE - adjustment) * (ONE - costs.exit_fee)
    return received / paid - ONE


def _trades(
    candles: Sequence[Candle],
    signals: Sequence[int],
    start: int,
    end: int,
    horizon: int,
    target_pct: Decimal,
    stop_pct: Decimal,
    costs: CostModel,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    next_entry = start
    first_signal = max(0, start - 1)
    for signal_index in range(first_signal, max(first_signal, end - 1)):
        entry_index = signal_index + 1
        if not signals[signal_index] or entry_index < next_entry:
            continue
        final_index = entry_index + horizon - 1
        if entry_index < start or final_index >= end:
            continue
        entry = candles[entry_index].open
        target = entry * (ONE + target_pct)
        stop = entry * (ONE - stop_pct)
        exit_price = candles[final_index].close
        exit_index = final_index
        reason = "timeout"
        for index in range(entry_index, final_index + 1):
            bar = candles[index]
            if bar.open <= stop:  # adverse gap receives the worse opening price
                exit_price, exit_index, reason = bar.open, index, "stop_gap"
                break
            if bar.open >= target:
                exit_price, exit_index, reason = target, index, "target"
                break
            stop_hit = bar.low <= stop
            target_hit = bar.high >= target
            if stop_hit:  # stop wins an ambiguous same-candle path
                exit_price, exit_index = stop, index
                reason = "stop_ambiguous" if target_hit else "stop"
                break
            if target_hit:
                exit_price, exit_index, reason = target, index, "target"
                break
        trades.append(
            {
                "entry_index": entry_index,
                "exit_index": exit_index,
                "entry_time": candles[entry_index].open_time,
                "exit_time": candles[exit_index].close_time,
                "return": _net_return(entry, exit_price, costs),
                "duration_bars": exit_index - entry_index + 1,
                "exit_reason": reason,
            }
        )
        next_entry = exit_index + 1
    return trades


def _block_bootstrap_ci(
    returns: Sequence[Decimal], alpha: Decimal, seed: int
) -> tuple[Decimal | None, Decimal | None, int]:
    if not returns:
        return None, None, 0
    count = len(returns)
    block = max(1, math.ceil(math.sqrt(count)))
    rng = random.Random(seed)
    estimates: list[Decimal] = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample: list[Decimal] = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(returns[(start + offset) % count] for offset in range(block))
        estimates.append(_mean(sample[:count]))
    estimates.sort()
    lower_index = max(0, int(Decimal(BOOTSTRAP_SAMPLES) * alpha / Decimal("2")))
    upper_index = min(
        BOOTSTRAP_SAMPLES - 1,
        int(Decimal(BOOTSTRAP_SAMPLES) * (ONE - alpha / Decimal("2"))),
    )
    return estimates[lower_index], estimates[upper_index], block


def _metrics(trades: Sequence[dict[str, Any]], alpha: Decimal, seed: int) -> dict[str, Any]:
    returns = [item["return"] for item in trades]
    if not returns:
        return {
            "trade_count": 0,
            "wins": 0,
            "win_rate": None,
            "net_compound_return": "0",
            "net_expectancy": None,
            "median_trade_return": None,
            "closed_trade_equity_drawdown": "0",
            "portfolio_maximum_drawdown": None,
            "portfolio_maximum_drawdown_available": False,
            "profit_factor": None,
            "median_duration_bars": None,
            "expectancy_ci": {"lower": None, "upper": None},
            "bootstrap_block_trades": 0,
        }
    equity = ONE
    peak = ONE
    maximum_drawdown = ZERO
    for value in returns:
        equity *= ONE + value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, ONE - equity / peak)
    gains = sum((item for item in returns if item > ZERO), ZERO)
    losses = -sum((item for item in returns if item < ZERO), ZERO)
    lower, upper, block = _block_bootstrap_ci(returns, alpha, seed)
    return {
        "trade_count": len(returns),
        "wins": sum(item > ZERO for item in returns),
        "win_rate": str(Decimal(sum(item > ZERO for item in returns)) / Decimal(len(returns))),
        "net_compound_return": str(equity - ONE),
        "net_expectancy": str(_mean(returns)),
        "median_trade_return": str(_median_decimal(returns)),
        "closed_trade_equity_drawdown": str(maximum_drawdown),
        "portfolio_maximum_drawdown": None,
        "portfolio_maximum_drawdown_available": False,
        "profit_factor": str(gains / losses) if losses > ZERO else None,
        "median_duration_bars": str(
            _median_decimal([Decimal(item["duration_bars"]) for item in trades])
        ),
        "expectancy_ci": {
            "lower": str(lower) if lower is not None else None,
            "upper": str(upper) if upper is not None else None,
        },
        "bootstrap_block_trades": block,
    }


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    return f"{candidate['strategy']}:{candidate['target_pct']}:{candidate['stop_pct']}"


def _seed_for(label: str) -> int:
    digest = hashlib.sha256(label.encode()).digest()
    return RESEARCH_SEED + int.from_bytes(digest[:4], "big")


def _data_hash(candles: Sequence[Candle]) -> str:
    rows = [
        [
            item.open_time,
            item.close_time,
            str(item.open),
            str(item.high),
            str(item.low),
            str(item.close),
            str(item.volume),
        ]
        for item in candles
    ]
    encoded = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _buy_hold(candles: Sequence[Candle], start: int, end: int, costs: CostModel) -> str | None:
    if end - start < 1:
        return None
    return str(_net_return(candles[start].open, candles[end - 1].close, costs))


def run_research(
    candles: Sequence[Candle],
    interval: str,
    hold_minutes: int,
    costs: CostModel,
    code_version: str = "unknown",
    *,
    comparison_trials: int = 9,
) -> dict[str, Any]:
    """Run the fixed, deterministic protocol and return a JSON-safe report."""

    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    if not isinstance(hold_minutes, int) or isinstance(hold_minutes, bool) or hold_minutes <= 0:
        raise ValueError("hold_minutes must be a positive integer")
    if not isinstance(costs, CostModel):
        raise TypeError("costs must be CostModel")
    all_data = _validate_candles(candles)
    _validate_interval(all_data, INTERVAL_MS[interval])
    input_count = len(all_data)
    capped = input_count > MAX_CANDLES
    data = all_data[-MAX_CANDLES:] if capped else all_data
    count = len(data)
    interval_minutes = INTERVAL_MS[interval] // 60_000
    horizon = math.ceil(hold_minutes / interval_minutes)
    candidates: list[_Candidate] = [
        {"strategy": strategy, "target_pct": target, "stop_pct": stop}
        for strategy in STRATEGIES
        for target, stop in _CANDIDATE_LEVELS
    ]
    trial_count = len(candidates)
    if comparison_trials < trial_count:
        raise ValueError("comparison_trials must include every strategy and parameter candidate")
    adjusted_alpha = Decimal("0.05") / Decimal(comparison_trials)
    train_boundary = count * 60 // 100
    validation_boundary = count * 80 // 100
    train_end = max(0, train_boundary - horizon)
    validation_end = max(train_boundary, validation_boundary - horizon)
    signals = {name: strategy_signal(data, name) for name in STRATEGIES}

    validation_results: list[dict[str, Any]] = []
    for candidate in candidates:
        label = _candidate_key(candidate)
        candidate_trades = _trades(
            data,
            signals[candidate["strategy"]],
            train_boundary,
            validation_end,
            horizon,
            candidate["target_pct"],
            candidate["stop_pct"],
            costs,
        )
        validation_results.append(
            {
                **candidate,
                "target_pct": str(candidate["target_pct"]),
                "stop_pct": str(candidate["stop_pct"]),
                "metrics": _metrics(
                    candidate_trades, adjusted_alpha, _seed_for(label + ":validation")
                ),
            }
        )

    def selection_key(item: dict[str, Any]) -> tuple[Decimal, Decimal, str]:
        metrics = item["metrics"]
        lower = metrics["expectancy_ci"]["lower"]
        expectancy = metrics["net_expectancy"]
        return (
            Decimal(lower) if lower is not None else Decimal("-Infinity"),
            Decimal(expectancy) if expectancy is not None else Decimal("-Infinity"),
            _candidate_key(item),
        )

    selected = max(validation_results, key=selection_key) if validation_results else None
    selected_raw = next(
        (
            item
            for item in candidates
            if selected and _candidate_key(item) == _candidate_key(selected)
        ),
        None,
    )

    normal_test_trades: list[dict[str, Any]] = []
    stress_test_trades: list[dict[str, Any]] = []
    if selected_raw is not None:
        selected_signals = signals[selected_raw["strategy"]]
        normal_test_trades = _trades(
            data,
            selected_signals,
            validation_boundary,
            count,
            horizon,
            selected_raw["target_pct"],
            selected_raw["stop_pct"],
            costs,
        )
        stress_test_trades = _trades(
            data,
            selected_signals,
            validation_boundary,
            count,
            horizon,
            selected_raw["target_pct"],
            selected_raw["stop_pct"],
            costs.stressed(),
        )
    normal_metrics = _metrics(normal_test_trades, adjusted_alpha, _seed_for("sealed-test"))
    stress_metrics = _metrics(stress_test_trades, adjusted_alpha, _seed_for("stress-test"))

    walk_forward: list[dict[str, Any]] = []
    if train_end > 0:
        fold_size = max(1, train_end // 4)
        for fold in range(1, 4):
            fold_start = fold * fold_size
            fold_end = min(train_end, (fold + 1) * fold_size)
            selection_end = max(0, fold_start - horizon)
            if selection_end <= 0 or fold_start >= fold_end:
                continue
            fold_candidates: list[dict[str, Any]] = []
            for candidate in candidates:
                label = _candidate_key(candidate)
                fit_trades = _trades(
                    data,
                    signals[candidate["strategy"]],
                    0,
                    selection_end,
                    horizon,
                    candidate["target_pct"],
                    candidate["stop_pct"],
                    costs,
                )
                fold_candidates.append(
                    {
                        **candidate,
                        "target_pct": str(candidate["target_pct"]),
                        "stop_pct": str(candidate["stop_pct"]),
                        "metrics": _metrics(
                            fit_trades,
                            adjusted_alpha,
                            _seed_for(f"{label}:train-fold:{fold}:selection"),
                        ),
                    }
                )
            fold_selected = max(fold_candidates, key=selection_key)
            fold_selected_raw = next(
                item
                for item in candidates
                if _candidate_key(item) == _candidate_key(fold_selected)
            )
            fold_trades = _trades(
                data,
                signals[fold_selected_raw["strategy"]],
                fold_start,
                fold_end,
                horizon,
                fold_selected_raw["target_pct"],
                fold_selected_raw["stop_pct"],
                costs,
            )
            walk_forward.append(
                {
                    "fold": fold,
                    "selection_start_index": 0,
                    "selection_end_index_exclusive": selection_end,
                    "purge_bars_before_evaluation": horizon,
                    "evaluation_start_index": fold_start,
                    "evaluation_end_index_exclusive": fold_end,
                    "selected_candidate": {
                        "strategy": fold_selected_raw["strategy"],
                        "target_pct": str(fold_selected_raw["target_pct"]),
                        "stop_pct": str(fold_selected_raw["stop_pct"]),
                    },
                    "metrics": _metrics(
                        fold_trades, adjusted_alpha, _seed_for(f"train-fold:{fold}")
                    ),
                }
            )

    duration_days = (
        Decimal(data[-1].close_time - data[0].open_time + 1) / Decimal(86_400_000)
        if len(data) >= 2
        else ZERO
    )
    test_lower = normal_metrics["expectancy_ci"]["lower"]
    stress_expectancy = stress_metrics["net_expectancy"]
    evidence_ready = duration_days >= Decimal("180") and len(normal_test_trades) >= 100
    positive_after_uncertainty = test_lower is not None and Decimal(test_lower) > ZERO
    stress_positive = stress_expectancy is not None and Decimal(stress_expectancy) > ZERO
    authorized = evidence_ready and positive_after_uncertainty and stress_positive
    blockers: list[str] = []
    if duration_days < Decimal("180"):
        blockers.append("less_than_180_days")
    if len(normal_test_trades) < 100:
        blockers.append("fewer_than_100_effective_oos_trades")
    if not positive_after_uncertainty:
        blockers.append("multiple_comparison_adjusted_ci_not_positive")
    if not stress_positive:
        blockers.append("stress_cost_expectancy_not_positive")

    report: dict[str, Any] = {
        "protocol": {
            "version": "spotlab-fixed-v1",
            "fixed_before_results": True,
            "split": {"train": "60%", "validation": "20%", "sealed_test": "20%"},
            "purge_bars": horizon,
            "selection_source": "validation_only",
            "sealed_test_evaluations": 1,
            "execution": "signal close; next bar open; adverse costs once per fill",
            "same_candle_rule": "stop_before_target",
            "gap_stop_rule": "exit_at_worse_open",
            "limit_fill_claimed": False,
        },
        "reproducibility": {
            "data_sha256": _data_hash(data),
            "code_version": code_version,
            "seed": RESEARCH_SEED,
            "trial_count": trial_count,
            "comparison_trial_count": comparison_trials,
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
        },
        "parameters": {
            "interval": interval,
            "hold_minutes": hold_minutes,
            "hold_bars": horizon,
            "costs": costs.as_dict(),
            "stress_costs": costs.stressed().as_dict(),
            "candidate_families": list(STRATEGIES),
            "familywise_alpha": "0.05",
            "per_trial_alpha": str(adjusted_alpha),
        },
        "data": {
            "input_candles": input_count,
            "used_candles": count,
            "max_candles": MAX_CANDLES,
            "capped": capped,
            "duration_days": str(duration_days),
            "start_time": data[0].open_time if data else None,
            "end_time": data[-1].close_time if data else None,
            "pilot": not evidence_ready,
        },
        "splits": {
            "train": [0, train_end],
            "train_purged_tail": [train_end, train_boundary],
            "validation": [train_boundary, validation_end],
            "validation_purged_tail": [validation_end, validation_boundary],
            "sealed_test": [validation_boundary, count],
        },
        "walk_forward_train_diagnostic": walk_forward,
        "validation_candidates": validation_results,
        "selected_candidate": selected,
        "sealed_test": {"normal_costs": normal_metrics, "stress_costs": stress_metrics},
        "benchmarks": {
            "cash_return": "0",
            "buy_hold_test_return": _buy_hold(data, validation_boundary, count, costs),
        },
        "decision": {
            "action": "AL" if authorized else "İŞLEM YAPMA",
            "evidence_threshold_met": evidence_ready,
            "advantage_supported": authorized,
            "blockers": blockers,
            "note": (
                "Operational evidence thresholds are minimums, not a guarantee of advantage."
                if evidence_ready
                else "Small-input result is a pilot and cannot authorize a period or live trade."
            ),
        },
    }
    # This assertion guards the API contract as part of the core itself.
    json.dumps(report, sort_keys=True, ensure_ascii=False)
    return report


def similar_patterns(
    candles: Sequence[Candle],
    window: int = 12,
    horizon: int = 12,
    *,
    interval: str | None = None,
    target_pct: Decimal = Decimal("0.01"),
    stop_pct: Decimal = Decimal("0.01"),
    limit: int = 20,
) -> dict[str, Any]:
    """Find past normalized-return paths without future or overlap leakage."""

    data = _validate_candles(candles)
    if interval is not None and interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    interval_ms = INTERVAL_MS[interval] if interval is not None else None
    _validate_interval(data, interval_ms)
    if window < 2 or horizon < 1 or limit < 1:
        raise ValueError("window >= 2, horizon >= 1, and limit >= 1 are required")
    if target_pct <= ZERO or stop_pct <= ZERO:
        raise ValueError("target_pct and stop_pct must be positive Decimal values")
    query_start = len(data) - window
    if query_start < 1:
        return {
            "method": "z-normalized close-return Euclidean distance",
            "status": "insufficient_data",
            "sample_count": 0,
            "matches": [],
        }

    def returns_for(start: int) -> list[Decimal]:
        return [
            data[index].close / data[index - 1].close - ONE
            for index in range(start, start + window)
        ]

    def normalize(values: Sequence[Decimal]) -> list[Decimal]:
        center = _mean(values)
        scale = _mean([(value - center) ** 2 for value in values]).sqrt()
        if scale == ZERO:
            return [ZERO for _ in values]
        return [(value - center) / scale for value in values]

    query = normalize(returns_for(query_start))
    ranked: list[tuple[Decimal, int]] = []
    # Candidate outcome must finish before the query feature window starts.
    latest_start = query_start - window - horizon
    for start in range(1, max(1, latest_start + 1)):
        candidate = normalize(returns_for(start))
        squared = [
            (left - right) ** 2 for left, right in zip(query, candidate, strict=True)
        ]
        distance = _mean(squared).sqrt()
        ranked.append((distance, start))
    ranked.sort(key=lambda item: (item[0], item[1]))

    selected: list[tuple[Decimal, int]] = []
    occupied: list[tuple[int, int]] = []
    for distance, start in ranked:
        sample_span = (start - 1, start + window + horizon - 1)
        if any(
            not (sample_span[1] < left or sample_span[0] > right)
            for left, right in occupied
        ):
            continue
        selected.append((distance, start))
        occupied.append(sample_span)
        if len(selected) == limit:
            break

    # Block bootstrap requires observations in time order, not similarity rank order.
    selected.sort(key=lambda item: item[1])

    matches: list[dict[str, Any]] = []
    outcome_returns: list[Decimal] = []
    counts = {"target": 0, "stop": 0, "neither": 0, "ambiguous": 0}
    for distance, start in selected:
        decision_index = start + window - 1
        base = data[decision_index].close
        target = base * (ONE + target_pct)
        stop = base * (ONE - stop_pct)
        outcome = "neither"
        outcome_index = decision_index + horizon
        for index in range(decision_index + 1, decision_index + horizon + 1):
            bar = data[index]
            target_hit = bar.high >= target
            stop_hit = bar.low <= stop
            if target_hit and stop_hit:
                outcome, outcome_index = "ambiguous", index
                break
            if target_hit:
                outcome, outcome_index = "target", index
                break
            if stop_hit:
                outcome, outcome_index = "stop", index
                break
        final_return = data[decision_index + horizon].close / base - ONE
        outcome_returns.append(final_return)
        counts[outcome] += 1
        matches.append(
            {
                "feature_start_time": data[start - 1].close_time,
                "decision_time": data[decision_index].close_time,
                "outcome_end_time": data[decision_index + horizon].close_time,
                "distance": str(distance),
                "outcome": outcome,
                "first_event_time": data[outcome_index].close_time,
                "horizon_return": str(final_return),
            }
        )
    lower, upper, block = _block_bootstrap_ci(outcome_returns, Decimal("0.05"), RESEARCH_SEED)
    result = {
        "method": "z-normalized close-return Euclidean distance",
        "distance_definition": "sqrt(mean((z(query_returns)-z(candidate_returns))^2))",
        "interpretation": "distance ranking only; it is not a success score or probability",
        "leakage_control": (
            "candidate outcome ends before query window; selected samples do not overlap"
        ),
        "window": window,
        "horizon": horizon,
        "target_pct": str(target_pct),
        "stop_pct": str(stop_pct),
        "sample_count": len(matches),
        "outcome_counts": counts,
        "median_horizon_return": (
            str(_median_decimal(outcome_returns)) if outcome_returns else None
        ),
        "mean_horizon_return_ci": {
            "lower": str(lower) if lower is not None else None,
            "upper": str(upper) if upper is not None else None,
            "temporal_block_samples": block,
            "observation_order": "chronological",
        },
        "matches": matches,
    }
    json.dumps(result, sort_keys=True, ensure_ascii=False)
    return result
