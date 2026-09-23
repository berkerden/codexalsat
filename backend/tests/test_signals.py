from decimal import Decimal as D

from spotlab.contracts import Quote
from spotlab.engine import empty_state
from spotlab.signals import signal_card


def test_unit_scenario_is_not_an_expected_profit_or_position_recommendation() -> None:
    q = Quote("BTCUSDT", D("100"), D("100"), D(10), D(10), 123000)
    report = {
        "selected_candidate": {"strategy": "breakout", "target_pct": ".02", "stop_pct": ".01"},
        "parameters": {
            "hold_minutes": 60,
            "costs": {"entry_fee": ".001", "exit_fee": ".001", "slippage_bps": "0"},
        },
    }
    result = signal_card(q, [], True, empty_state(), report, D(".01"), D(5))
    assert result["action"] == "İŞLEM YAPMA"
    assert result["quantity"] is None and result["expected_net_result"] is None
    assert result["scenario_only"]
    assert result["margin"]["net_target_profit"] == D("1.798")
    assert result["valid_until"] == 128000
