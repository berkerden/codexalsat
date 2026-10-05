from dataclasses import dataclass
from decimal import Decimal as D

import pytest

from spotlab.orders import OrderError, OrderInvariantError
from spotlab.venue_filters import (
    VenueBalance,
    VenueFilter,
    VenueOpenOrder,
    VenueOrder,
    VenueSnapshot,
    round_protection_quantity,
    validate_venue_order,
)


@dataclass(frozen=True)
class Metadata:
    symbol: str = "BTCUSDT"
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    status: str = "TRADING"
    order_types: tuple[str, ...] = ("LIMIT", "MARKET", "LIMIT_MAKER", "STOP_LOSS")
    oco_allowed: bool = True
    symbol_filters: tuple[VenueFilter, ...] = ()
    exchange_filters: tuple[VenueFilter, ...] = ()
    asset_filters: tuple[VenueFilter, ...] = ()


def rule(filter_type: str, **values: str | int | bool) -> VenueFilter:
    return VenueFilter(filter_type, tuple(sorted(values.items())))


def snapshot(
    *,
    open_orders: tuple[VenueOpenOrder, ...] = (),
    reference: D | None = None,
    average: D | None = None,
    average_mins: int | None = None,
    last: D | None = None,
    base_free: D = D("20"),
    base_locked: D = D(0),
) -> VenueSnapshot:
    return VenueSnapshot(
        can_trade=True,
        balances=(
            VenueBalance("BTC", base_free, base_locked),
            VenueBalance("USDT", D("1000000"), D(0)),
        ),
        open_orders=open_orders,
        reference_price=reference,
        average_price=average,
        average_price_mins=average_mins,
        last_price=last,
        observed_at_ms=1,
    )


def limit_order(*, side: str = "BUY", price: D = D("100"), quantity: D = D("1")) -> VenueOrder:
    return VenueOrder("BTCUSDT", side, "LIMIT", quantity, price, "USDT")


def test_oco_counts_two_orders_one_algo_and_one_list() -> None:
    existing = (
        VenueOpenOrder("BTCUSDT", "BUY", "LIMIT", D("1"), D(0), -1),
        VenueOpenOrder("ETHUSDT", "SELL", "LIMIT_MAKER", D("1"), D(0), 81),
    )
    order = VenueOrder("BTCUSDT", "SELL", "OCO", D("1"), D("120"), "USDT", D("90"))
    metadata = Metadata(
        symbol_filters=(
            rule("MAX_NUM_ORDERS", maxNumOrders=3),
            rule("MAX_NUM_ALGO_ORDERS", maxNumAlgoOrders=1),
            rule("MAX_NUM_ORDER_LISTS", maxNumOrderLists=1),
        ),
        exchange_filters=(
            rule("EXCHANGE_MAX_NUM_ORDERS", maxNumOrders=4),
            rule("EXCHANGE_MAX_NUM_ALGO_ORDERS", maxNumAlgoOrders=1),
            rule("EXCHANGE_MAX_NUM_ORDER_LISTS", maxNumOrderLists=2),
        ),
    )
    validate_venue_order(order, metadata, snapshot(open_orders=existing, last=D("100")))
    constrained = Metadata(
        symbol_filters=(rule("MAX_NUM_ORDERS", maxNumOrders=2),),
    )
    with pytest.raises(OrderError, match="capacity"):
        validate_venue_order(order, constrained, snapshot(open_orders=existing, last=D("100")))


def test_buy_can_reserve_buy_plus_future_oco_capacity() -> None:
    metadata = Metadata(
        symbol_filters=(
            rule("MAX_NUM_ORDERS", maxNumOrders=3),
            rule("MAX_NUM_ALGO_ORDERS", maxNumAlgoOrders=1),
            rule("MAX_NUM_ORDER_LISTS", maxNumOrderLists=1),
        )
    )
    validate_venue_order(limit_order(), metadata, snapshot(), reserve_protection=True)
    with pytest.raises(OrderError, match="capacity"):
        validate_venue_order(
            limit_order(),
            Metadata(symbol_filters=(rule("MAX_NUM_ORDERS", maxNumOrders=2),)),
            snapshot(),
            reserve_protection=True,
        )


def test_reference_price_supersedes_mismatched_weighted_average_for_percent_filter() -> None:
    metadata = Metadata(
        symbol_filters=(
            rule(
                "PERCENT_PRICE_BY_SIDE",
                bidMultiplierUp="1.10",
                bidMultiplierDown="0.90",
                askMultiplierUp="1.20",
                askMultiplierDown="0.80",
                avgPriceMins=5,
            ),
        )
    )
    validate_venue_order(
        limit_order(price=D("105")),
        metadata,
        snapshot(reference=D("100"), average=D("1"), average_mins=1),
    )
    with pytest.raises(OrderInvariantError, match="interval"):
        validate_venue_order(
            limit_order(price=D("105")),
            metadata,
            snapshot(average=D("100"), average_mins=1),
        )
    with pytest.raises(OrderError, match="percentage"):
        validate_venue_order(
            limit_order(side="SELL", price=D("79")),
            metadata,
            snapshot(reference=D("100")),
        )


def test_market_notional_flags_and_reference_price_are_applied() -> None:
    market = VenueOrder("BTCUSDT", "BUY", "MARKET", D("1"), None, "USDT")
    enforced = Metadata(
        symbol_filters=(
            rule(
                "NOTIONAL",
                minNotional="10",
                maxNotional="20",
                applyMinToMarket=True,
                applyMaxToMarket=False,
                avgPriceMins=5,
            ),
        )
    )
    validate_venue_order(
        market,
        enforced,
        snapshot(reference=D("12"), average=D("1"), average_mins=1),
    )
    with pytest.raises(OrderError, match="minimum"):
        validate_venue_order(market, enforced, snapshot(average=D("8"), average_mins=5))
    disabled = Metadata(
        symbol_filters=(
            rule(
                "MIN_NOTIONAL",
                minNotional="10",
                applyToMarket=False,
                avgPriceMins=5,
            ),
        )
    )
    validate_venue_order(market, disabled, snapshot(average=D("1"), average_mins=5))


def test_oco_market_child_uses_market_lot_and_market_notional_rules() -> None:
    order = VenueOrder("BTCUSDT", "SELL", "OCO", D("0.15"), D("120"), "USDT", D("90"))
    market_lot = Metadata(
        symbol_filters=(
            rule("LOT_SIZE", minQty="0", maxQty="0", stepSize="0.01"),
            rule("MARKET_LOT_SIZE", minQty="0", maxQty="0", stepSize="0.1"),
        )
    )
    with pytest.raises(OrderError, match="quantity"):
        validate_venue_order(order, market_lot, snapshot(last=D("100")))

    market_notional = Metadata(
        symbol_filters=(
            rule(
                "MIN_NOTIONAL",
                minNotional="10",
                applyToMarket=True,
                avgPriceMins=5,
            ),
        )
    )
    with pytest.raises(OrderError, match="minimum"):
        validate_venue_order(
            order,
            market_notional,
            snapshot(reference=D("50"), average=D("100"), average_mins=5, last=D("100")),
        )
    validate_venue_order(
        order,
        market_notional,
        snapshot(reference=D("100"), average=D("1"), average_mins=1, last=D("100")),
    )


def test_oco_requires_strict_target_last_stop_bracket() -> None:
    order = VenueOrder("BTCUSDT", "SELL", "OCO", D("1"), D("120"), "USDT", D("90"))
    with pytest.raises(OrderError, match="target > last price > stop"):
        validate_venue_order(order, Metadata(), snapshot(last=D("120")))


def test_max_position_conservatively_counts_original_open_buy_quantity() -> None:
    open_buy = VenueOpenOrder("BTCUSDT", "BUY", "LIMIT", D("4"), D("3"), -1)
    metadata = Metadata(symbol_filters=(rule("MAX_POSITION", maxPosition="8"),))
    validate_venue_order(
        limit_order(quantity=D("1")),
        metadata,
        snapshot(open_orders=(open_buy,), base_free=D("2"), base_locked=D("1")),
    )
    with pytest.raises(OrderError, match="position"):
        validate_venue_order(
            limit_order(quantity=D("1.001")),
            metadata,
            snapshot(open_orders=(open_buy,), base_free=D("2"), base_locked=D("1")),
        )


def test_zero_static_rules_are_disabled_and_unknown_or_max_asset_rules_fail_closed() -> None:
    disabled = Metadata(
        symbol_filters=(
            rule("PRICE_FILTER", minPrice="0", maxPrice="0", tickSize="0"),
            rule("LOT_SIZE", minQty="0", maxQty="0", stepSize="0"),
            rule("MARKET_LOT_SIZE", minQty="0", maxQty="0", stepSize="0"),
        )
    )
    validate_venue_order(limit_order(price=D("1.2345"), quantity=D("0.1234")), disabled, snapshot())
    with pytest.raises(OrderError, match="MAX_ASSET"):
        validate_venue_order(
            limit_order(price=D("100"), quantity=D("1")),
            Metadata(asset_filters=(rule("MAX_ASSET", asset="USDT", limit="99"),)),
            snapshot(),
        )
    with pytest.raises(OrderError, match="unsupported"):
        validate_venue_order(
            limit_order(), Metadata(symbol_filters=(rule("FUTURE_FILTER", value="1"),)), snapshot()
        )


def test_protection_quantity_uses_common_enabled_lot_increments() -> None:
    metadata = Metadata(
        symbol_filters=(
            rule("LOT_SIZE", minQty="0", maxQty="0", stepSize="0.002"),
            rule("MARKET_LOT_SIZE", minQty="0", maxQty="0", stepSize="0.003"),
        )
    )
    assert round_protection_quantity(D("1.007"), metadata) == D("1.002")


@pytest.mark.parametrize("kind", ["MARKET", "OCO"])
def test_quote_max_asset_does_not_treat_reference_as_fill_price_bound(kind):
    order = VenueOrder(
        "BTCUSDT",
        "SELL",
        kind,
        D(1),
        D(120) if kind == "OCO" else None,
        "USDT",
        D(90) if kind == "OCO" else None,
    )
    metadata = Metadata(asset_filters=(rule("MAX_ASSET", asset="USDT", limit="99999"),))
    with pytest.raises(OrderError, match="cannot bound"):
        validate_venue_order(order, metadata, snapshot(reference=D(100), last=D(100)))
