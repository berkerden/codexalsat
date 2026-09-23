"""Decimal-only transaction cost, margin, and position sizing calculations."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Final

ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")
TEN_THOUSAND: Final = Decimal("10000")


def _decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class CostModel:
    """Round-trip spot cost assumptions.

    Fees are fractions of notional (``0.001`` means 10 bps).  Spread is a
    quoted full spread; a market fill crosses half of it on each side.
    Slippage is charged once per fill.  These are deliberately visible
    assumptions, rather than claims about a particular account or symbol.
    """

    entry_fee: Decimal = Decimal("0.001")
    exit_fee: Decimal = Decimal("0.001")
    spread_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("5")
    assumptions: tuple[str, ...] = field(
        default=(
            "fees are assumed because account- and symbol-specific rates were not supplied",
            "spread is split equally across entry and exit",
            "slippage is applied once to each simulated fill",
            "fees are valued in quote currency",
        )
    )

    def __post_init__(self) -> None:
        for name in ("entry_fee", "exit_fee", "spread_bps", "slippage_bps"):
            value = _decimal(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")
        if self.entry_fee >= ONE or self.exit_fee >= ONE:
            raise ValueError("fee rates must be less than one")
        if not self.assumptions:
            raise ValueError("cost assumptions must be visible")

    @property
    def adverse_fill_rate(self) -> Decimal:
        """Adverse price adjustment applied to each side of a market fill."""

        return (self.spread_bps / Decimal("2") + self.slippage_bps) / TEN_THOUSAND

    def as_dict(self) -> dict[str, object]:
        return {
            "entry_fee": str(self.entry_fee),
            "exit_fee": str(self.exit_fee),
            "spread_bps": str(self.spread_bps),
            "slippage_bps": str(self.slippage_bps),
            "assumptions": list(self.assumptions),
        }

    def stressed(self) -> CostModel:
        """A fixed, pre-declared stress scenario for research comparisons."""

        return CostModel(
            entry_fee=self.entry_fee * Decimal("1.5"),
            exit_fee=self.exit_fee * Decimal("1.5"),
            spread_bps=self.spread_bps * Decimal("2"),
            slippage_bps=self.slippage_bps * Decimal("2"),
            assumptions=self.assumptions + ("stress uses 1.5x fees and 2x spread/slippage",),
        )


def margin(
    entry: Decimal,
    target: Decimal,
    stop: Decimal,
    quantity: Decimal,
    costs: CostModel,
) -> dict[str, Decimal]:
    """Return a complete long-only spot margin calculation.

    ``entry``, ``target`` and ``stop`` are unadjusted market prices.  The
    returned execution prices include spread/slippage exactly once.  Fees are
    then applied to executed notional exactly once.
    """

    entry = _decimal(entry, "entry")
    target = _decimal(target, "target")
    stop = _decimal(stop, "stop")
    quantity = _decimal(quantity, "quantity")
    if min(entry, target, stop, quantity) <= ZERO:
        raise ValueError("prices and quantity must be positive")
    if not stop < entry < target:
        raise ValueError("long spot levels must satisfy stop < entry < target")
    if not isinstance(costs, CostModel):
        raise TypeError("costs must be CostModel")

    adverse = costs.adverse_fill_rate
    entry_fill = entry * (ONE + adverse)
    target_fill = target * (ONE - adverse)
    stop_fill = stop * (ONE - adverse)
    entry_notional = entry_fill * quantity
    target_notional = target_fill * quantity
    stop_notional = stop_fill * quantity
    entry_fee_amount = entry_notional * costs.entry_fee
    target_exit_fee = target_notional * costs.exit_fee
    stop_exit_fee = stop_notional * costs.exit_fee
    total_entry_cash = entry_notional + entry_fee_amount
    target_cash = target_notional - target_exit_fee
    stop_cash = stop_notional - stop_exit_fee
    net_target_profit = target_cash - total_entry_cash
    modeled_stop_loss = total_entry_cash - stop_cash
    gross_target_profit = (target - entry) * quantity
    gross_stop_loss = (entry - stop) * quantity
    break_even = (
        entry_fill * (ONE + costs.entry_fee) / ((ONE - adverse) * (ONE - costs.exit_fee))
    )
    net_target_return = net_target_profit / total_entry_cash
    net_stop_return = -modeled_stop_loss / total_entry_cash

    return {
        "entry_price": entry,
        "target_price": target,
        "stop_price": stop,
        "quantity": quantity,
        "entry_fill_price": entry_fill,
        "target_fill_price": target_fill,
        "stop_fill_price": stop_fill,
        "entry_notional": entry_notional,
        "target_notional": target_notional,
        "stop_notional": stop_notional,
        "entry_fee": entry_fee_amount,
        "target_exit_fee": target_exit_fee,
        "stop_exit_fee": stop_exit_fee,
        "gross_target_profit": gross_target_profit,
        "gross_stop_loss": gross_stop_loss,
        "net_target_profit": net_target_profit,
        "modeled_stop_loss": modeled_stop_loss,
        "break_even_price": break_even,
        "net_target_return": net_target_return,
        "net_stop_return": net_stop_return,
        "net_reward_risk": (
            net_target_profit / modeled_stop_loss if modeled_stop_loss > ZERO else ZERO
        ),
        "total_entry_cash": total_entry_cash,
        "target_cash_after_costs": target_cash,
        "stop_cash_after_costs": stop_cash,
    }


def position_size(
    capital: Decimal,
    risk_fraction: Decimal,
    entry: Decimal,
    stop: Decimal,
    costs: CostModel,
    *,
    step: Decimal = Decimal("0.00000001"),
    min_notional: Decimal = ZERO,
) -> dict[str, Decimal | str]:
    """Size a long spot position under loss and available-cash constraints.

    Quantity is always rounded down to ``step``.  A zero result is explicit
    when the exchange minimum cannot be met without breaking a constraint.
    """

    capital = _decimal(capital, "capital")
    risk_fraction = _decimal(risk_fraction, "risk_fraction")
    entry = _decimal(entry, "entry")
    stop = _decimal(stop, "stop")
    step = _decimal(step, "step")
    min_notional = _decimal(min_notional, "min_notional")
    if capital <= ZERO or entry <= ZERO or stop <= ZERO or step <= ZERO:
        raise ValueError("capital, prices, and step must be positive")
    if not ZERO < risk_fraction < ONE:
        raise ValueError("risk_fraction must be between zero and one")
    if stop >= entry:
        raise ValueError("stop must be below entry for long spot sizing")
    if min_notional < ZERO:
        raise ValueError("min_notional cannot be negative")
    if not isinstance(costs, CostModel):
        raise TypeError("costs must be CostModel")

    adverse = costs.adverse_fill_rate
    entry_unit_cash = entry * (ONE + adverse) * (ONE + costs.entry_fee)
    stop_unit_cash = stop * (ONE - adverse) * (ONE - costs.exit_fee)
    loss_per_unit = entry_unit_cash - stop_unit_cash
    if loss_per_unit <= ZERO:
        raise ValueError("modeled stop must produce a positive loss")

    risk_budget = capital * risk_fraction
    risk_limited = risk_budget / loss_per_unit
    cash_limited = capital / entry_unit_cash
    raw_quantity = min(risk_limited, cash_limited)
    quantity = (raw_quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
    notional = quantity * entry
    reason = "ok"
    if quantity <= ZERO:
        quantity = ZERO
        reason = "quantity_below_step"
    elif notional < min_notional:
        quantity = ZERO
        notional = ZERO
        reason = "below_min_notional"

    modeled_loss = quantity * loss_per_unit
    required_cash = quantity * entry_unit_cash
    if modeled_loss > risk_budget or required_cash > capital:
        raise ArithmeticError("rounded position violates a sizing constraint")
    return {
        "quantity": quantity,
        "raw_quantity": raw_quantity,
        "risk_budget": risk_budget,
        "loss_per_unit": loss_per_unit,
        "modeled_stop_loss": modeled_loss,
        "required_cash": required_cash,
        "entry_notional": notional,
        "constraint": "risk" if risk_limited <= cash_limited else "capital",
        "reason": reason,
    }
