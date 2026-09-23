from decimal import Decimal

import pytest

from spotlab.finance import CostModel, margin, position_size


def test_margin_matches_independently_calculated_cash_flows() -> None:
    costs = CostModel(
        entry_fee=Decimal("0.001"),
        exit_fee=Decimal("0.002"),
        spread_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
    )

    result = margin(
        Decimal("100"), Decimal("110"), Decimal("95"), Decimal("1"), costs
    )

    # Entry: 100 * 1.001 price adjustment * 1.001 fee = 100.2001 cash.
    # Target: 110 * 0.999 price adjustment * 0.998 after fee = 109.670220 cash.
    assert result["total_entry_cash"] == Decimal("100.2001")
    assert result["target_cash_after_costs"] == Decimal("109.670220")
    assert result["net_target_profit"] == Decimal("9.470120")
    # Stop: 95 * 0.999 * 0.998 = 94.715190 cash returned.
    assert result["modeled_stop_loss"] == Decimal("5.484910")
    assert result["break_even_price"] == Decimal("100.5014032068140284573150305")


def test_position_size_rounds_down_and_never_breaks_risk_budget() -> None:
    costs = CostModel(
        entry_fee=Decimal("0"),
        exit_fee=Decimal("0"),
        spread_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    result = position_size(
        Decimal("1000"),
        Decimal("0.01"),
        Decimal("100"),
        Decimal("93"),
        costs,
        step=Decimal("0.1"),
    )

    # Risk budget is 10, so 10/7 = 1.428... and must round down to 1.4.
    assert result["quantity"] == Decimal("1.4")
    assert result["modeled_stop_loss"] == Decimal("9.8")
    assert result["required_cash"] == Decimal("140.0")
    assert result["constraint"] == "risk"


def test_position_size_rejects_invalid_long_levels_and_minimum() -> None:
    costs = CostModel()
    with pytest.raises(ValueError, match="stop must be below entry"):
        position_size(
            Decimal("1000"), Decimal("0.01"), Decimal("100"), Decimal("100"), costs
        )

    result = position_size(
        Decimal("100"),
        Decimal("0.01"),
        Decimal("10"),
        Decimal("9"),
        costs,
        min_notional=Decimal("1000"),
    )
    assert result["quantity"] == Decimal("0")
    assert result["reason"] == "below_min_notional"


def test_cost_model_rejects_float_money() -> None:
    with pytest.raises(TypeError, match="must be Decimal"):
        CostModel(entry_fee=0.001)  # type: ignore[arg-type]
