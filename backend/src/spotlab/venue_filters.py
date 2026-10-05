"""Fail-closed Binance Spot venue-filter validation.

The pure validator in this module deliberately consumes an already-fetched
snapshot.  Network I/O and signing stay in :mod:`spotlab.testnet`, while both
ordinary LIMIT orders and SELL OCO protection use the same rule engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import gcd
from typing import Protocol

from spotlab.orders import OrderError, OrderInvariantError, OrderRequest

D = Decimal
ZERO = D(0)

KNOWN_SYMBOL_FILTERS = frozenset(
    {
        "PRICE_FILTER",
        "PERCENT_PRICE",
        "PERCENT_PRICE_BY_SIDE",
        "LOT_SIZE",
        "MIN_NOTIONAL",
        "NOTIONAL",
        "ICEBERG_PARTS",
        "MARKET_LOT_SIZE",
        "MAX_NUM_ORDERS",
        "MAX_NUM_ALGO_ORDERS",
        "MAX_NUM_ICEBERG_ORDERS",
        "MAX_POSITION",
        "TRAILING_DELTA",
        "MAX_NUM_ORDER_AMENDS",
        "MAX_NUM_ORDER_LISTS",
    }
)
KNOWN_EXCHANGE_FILTERS = frozenset(
    {
        "EXCHANGE_MAX_NUM_ORDERS",
        "EXCHANGE_MAX_NUM_ALGO_ORDERS",
        "EXCHANGE_MAX_NUM_ICEBERG_ORDERS",
        "EXCHANGE_MAX_NUM_ORDER_LISTS",
    }
)
KNOWN_ASSET_FILTERS = frozenset({"MAX_ASSET"})
ALGO_TYPES = frozenset({"STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"})

FilterValue = str | int | bool


@dataclass(frozen=True)
class VenueFilter:
    filter_type: str
    values: tuple[tuple[str, FilterValue], ...]

    def get(self, name: str) -> FilterValue | None:
        for key, value in self.values:
            if key == name:
                return value
        return None


@dataclass(frozen=True)
class VenueBalance:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True)
class VenueOpenOrder:
    symbol: str
    side: str
    order_type: str
    original_quantity: Decimal
    executed_quantity: Decimal
    order_list_id: int

    @property
    def remaining_quantity(self) -> Decimal:
        return max(ZERO, self.original_quantity - self.executed_quantity)


@dataclass(frozen=True)
class VenueSnapshot:
    can_trade: bool
    balances: tuple[VenueBalance, ...]
    open_orders: tuple[VenueOpenOrder, ...]
    reference_price: Decimal | None
    average_price: Decimal | None
    average_price_mins: int | None
    last_price: Decimal | None
    observed_at_ms: int

    def balance(self, asset: str) -> VenueBalance:
        for balance in self.balances:
            if balance.asset == asset:
                return balance
        return VenueBalance(asset, ZERO, ZERO)


@dataclass(frozen=True)
class VenueOrder:
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    quote_asset: str
    stop_price: Decimal | None = None


class SubmissionMetadata(Protocol):
    @property
    def symbol(self) -> str: ...

    @property
    def base_asset(self) -> str: ...

    @property
    def quote_asset(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def order_types(self) -> tuple[str, ...]: ...

    @property
    def oco_allowed(self) -> bool: ...

    @property
    def symbol_filters(self) -> tuple[VenueFilter, ...]: ...

    @property
    def exchange_filters(self) -> tuple[VenueFilter, ...]: ...

    @property
    def asset_filters(self) -> tuple[VenueFilter, ...]: ...


def _decimal(value: FilterValue | None, name: str) -> Decimal:
    if not isinstance(value, str):
        raise OrderInvariantError(f"{name} must be a decimal string")
    try:
        parsed = D(value)
    except InvalidOperation as error:
        raise OrderInvariantError(f"{name} is not decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise OrderInvariantError(f"{name} is outside the allowed range")
    return parsed


def _integer(value: FilterValue | None, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OrderInvariantError(f"{name} must be a non-negative integer")
    return value


def _boolean(value: FilterValue | None, name: str) -> bool:
    if not isinstance(value, bool):
        raise OrderInvariantError(f"{name} must be boolean")
    return value


def _enabled_bounds(value: Decimal, minimum: Decimal, maximum: Decimal, name: str) -> None:
    if minimum and value < minimum:
        raise OrderError(f"{name} is below the exchange minimum")
    if maximum and value > maximum:
        raise OrderError(f"{name} exceeds the exchange maximum")
    if minimum and maximum and minimum > maximum:
        raise OrderInvariantError(f"{name} bounds are inverted")


def _validate_increment(value: Decimal, increment: Decimal, name: str) -> None:
    if increment and value % increment != 0:
        raise OrderError(f"{name} is not aligned to the exchange increment")


def _prices(order: VenueOrder) -> tuple[Decimal, ...]:
    if order.order_type == "MARKET":
        return ()
    if order.price is None:
        raise OrderInvariantError("priced order is missing its price")
    if order.order_type == "OCO":
        if order.stop_price is None:
            raise OrderError("SELL OCO validation requires stop_price")
        return (order.price, order.stop_price)
    return (order.price,)


def _dynamic_price(rule: VenueFilter, snapshot: VenueSnapshot) -> Decimal:
    if snapshot.reference_price is not None:
        return snapshot.reference_price
    mins = _integer(rule.get("avgPriceMins"), f"{rule.filter_type}.avgPriceMins")
    if mins == 0:
        if snapshot.last_price is None:
            raise OrderInvariantError("last price is missing for avgPriceMins=0")
        return snapshot.last_price
    if snapshot.average_price is None or snapshot.average_price_mins != mins:
        raise OrderInvariantError("average-price interval does not match venue filter")
    return snapshot.average_price


def _notional_price(rule: VenueFilter, order: VenueOrder, snapshot: VenueSnapshot) -> Decimal:
    if order.order_type != "MARKET":
        if order.price is None:
            raise OrderInvariantError("priced order is missing its price")
        return order.price
    return _dynamic_price(rule, snapshot)


def _market_price(snapshot: VenueSnapshot) -> Decimal:
    price = snapshot.reference_price or snapshot.average_price or snapshot.last_price
    if price is None:
        raise OrderInvariantError("market reference price is missing")
    return price


def _oco_child(order: VenueOrder, order_type: str) -> VenueOrder:
    return VenueOrder(
        symbol=order.symbol,
        side=order.side,
        order_type=order_type,
        quantity=order.quantity,
        price=order.price if order_type == "LIMIT_MAKER" else None,
        quote_asset=order.quote_asset,
    )


def _validate_price_filter(rule: VenueFilter, order: VenueOrder) -> None:
    minimum = _decimal(rule.get("minPrice"), "PRICE_FILTER.minPrice")
    maximum = _decimal(rule.get("maxPrice"), "PRICE_FILTER.maxPrice")
    tick = _decimal(rule.get("tickSize"), "PRICE_FILTER.tickSize")
    for price in _prices(order):
        _enabled_bounds(price, minimum, maximum, "price")
        _validate_increment(price, tick, "price")


def _validate_lot_filter(rule: VenueFilter, order: VenueOrder) -> None:
    minimum = _decimal(rule.get("minQty"), f"{rule.filter_type}.minQty")
    maximum = _decimal(rule.get("maxQty"), f"{rule.filter_type}.maxQty")
    step = _decimal(rule.get("stepSize"), f"{rule.filter_type}.stepSize")
    _enabled_bounds(order.quantity, minimum, maximum, "quantity")
    _validate_increment(order.quantity, step, "quantity")


def _validate_percent_filter(rule: VenueFilter, order: VenueOrder, snapshot: VenueSnapshot) -> None:
    reference = _dynamic_price(rule, snapshot)
    if rule.filter_type == "PERCENT_PRICE":
        up = _decimal(rule.get("multiplierUp"), "PERCENT_PRICE.multiplierUp")
        down = _decimal(rule.get("multiplierDown"), "PERCENT_PRICE.multiplierDown")
    elif order.side == "BUY":
        up = _decimal(rule.get("bidMultiplierUp"), "PERCENT_PRICE_BY_SIDE.bidMultiplierUp")
        down = _decimal(rule.get("bidMultiplierDown"), "PERCENT_PRICE_BY_SIDE.bidMultiplierDown")
    else:
        up = _decimal(rule.get("askMultiplierUp"), "PERCENT_PRICE_BY_SIDE.askMultiplierUp")
        down = _decimal(rule.get("askMultiplierDown"), "PERCENT_PRICE_BY_SIDE.askMultiplierDown")
    if not up or not down:
        raise OrderInvariantError(f"{rule.filter_type} multipliers must be positive")
    for price in _prices(order):
        if price > reference * up or price < reference * down:
            raise OrderError("price is outside the dynamic percentage range")


def _validate_notional_filter(
    rule: VenueFilter, order: VenueOrder, snapshot: VenueSnapshot
) -> None:
    if rule.filter_type == "MIN_NOTIONAL":
        apply_min = order.order_type != "MARKET" or _boolean(
            rule.get("applyToMarket"), "MIN_NOTIONAL.applyToMarket"
        )
        apply_max = False
        minimum = _decimal(rule.get("minNotional"), "MIN_NOTIONAL.minNotional")
        maximum = ZERO
    else:
        apply_min = order.order_type != "MARKET" or _boolean(
            rule.get("applyMinToMarket"), "NOTIONAL.applyMinToMarket"
        )
        apply_max = order.order_type != "MARKET" or _boolean(
            rule.get("applyMaxToMarket"), "NOTIONAL.applyMaxToMarket"
        )
        minimum = _decimal(rule.get("minNotional"), "NOTIONAL.minNotional")
        maximum = _decimal(rule.get("maxNotional"), "NOTIONAL.maxNotional")
        if minimum and maximum and minimum > maximum:
            raise OrderInvariantError("NOTIONAL bounds are inverted")
    if order.order_type == "MARKET" and not apply_min and not apply_max:
        return
    notionals: tuple[Decimal, ...]
    if order.order_type == "MARKET":
        notionals = (_notional_price(rule, order, snapshot) * order.quantity,)
    else:
        notionals = tuple(price * order.quantity for price in _prices(order))
    for notional in notionals:
        if apply_min and minimum and notional < minimum:
            raise OrderError("notional is below the exchange minimum")
        if apply_max and maximum and notional > maximum:
            raise OrderError("notional exceeds the exchange maximum")


def _incoming_counts(order: VenueOrder, reserve_protection: bool) -> tuple[int, int, int]:
    if reserve_protection:
        if order.order_type != "LIMIT" or order.side != "BUY":
            raise OrderError("protection capacity may only be reserved for a LIMIT BUY")
        return 3, 1, 1
    if order.order_type == "OCO":
        return 2, 1, 1
    return 1, int(order.order_type in ALGO_TYPES), 0


def _open_counts(orders: tuple[VenueOpenOrder, ...], symbol: str | None) -> tuple[int, int, int]:
    selected = tuple(order for order in orders if symbol is None or order.symbol == symbol)
    normal = len(selected)
    algo = sum(order.order_type in ALGO_TYPES for order in selected)
    lists = len({order.order_list_id for order in selected if order.order_list_id >= 0})
    return normal, algo, lists


def _validate_count_filter(
    rule: VenueFilter,
    order: VenueOrder,
    snapshot: VenueSnapshot,
    reserve_protection: bool,
) -> None:
    incoming_orders, incoming_algo, incoming_lists = _incoming_counts(order, reserve_protection)
    exchange = rule.filter_type.startswith("EXCHANGE_")
    current_orders, current_algo, current_lists = _open_counts(
        snapshot.open_orders, None if exchange else order.symbol
    )
    if rule.filter_type in {"MAX_NUM_ORDERS", "EXCHANGE_MAX_NUM_ORDERS"}:
        limit = _integer(rule.get("maxNumOrders"), f"{rule.filter_type}.maxNumOrders")
        if current_orders + incoming_orders > limit:
            raise OrderError("open-order capacity would be exceeded")
    elif rule.filter_type in {"MAX_NUM_ALGO_ORDERS", "EXCHANGE_MAX_NUM_ALGO_ORDERS"}:
        limit = _integer(rule.get("maxNumAlgoOrders"), f"{rule.filter_type}.maxNumAlgoOrders")
        if current_algo + incoming_algo > limit:
            raise OrderError("open-algo-order capacity would be exceeded")
    elif rule.filter_type in {"MAX_NUM_ORDER_LISTS", "EXCHANGE_MAX_NUM_ORDER_LISTS"}:
        limit = _integer(rule.get("maxNumOrderLists"), f"{rule.filter_type}.maxNumOrderLists")
        if current_lists + incoming_lists > limit:
            raise OrderError("open-order-list capacity would be exceeded")


def _validate_max_position(
    rule: VenueFilter, order: VenueOrder, metadata: SubmissionMetadata, snapshot: VenueSnapshot
) -> None:
    maximum = _decimal(rule.get("maxPosition"), "MAX_POSITION.maxPosition")
    if order.side != "BUY":
        return
    balance = snapshot.balance(metadata.base_asset)
    pending = sum(
        (
            item.original_quantity
            for item in snapshot.open_orders
            if item.symbol == order.symbol and item.side == "BUY"
        ),
        ZERO,
    )
    if balance.free + balance.locked + pending + order.quantity > maximum:
        raise OrderError("maximum base-asset position would be exceeded")


def _validate_max_asset(
    rule: VenueFilter,
    order: VenueOrder,
    metadata: SubmissionMetadata,
    snapshot: VenueSnapshot,
) -> None:
    asset = rule.get("asset")
    if not isinstance(asset, str) or not asset:
        raise OrderInvariantError("MAX_ASSET.asset must be a non-empty string")
    limit = _decimal(rule.get("limit"), "MAX_ASSET.limit")
    values: tuple[Decimal, ...]
    if asset == metadata.base_asset:
        values = (order.quantity,)
    elif asset == metadata.quote_asset:
        if order.order_type in {"MARKET", "OCO"}:
            raise OrderInvariantError("quote MAX_ASSET cannot bound a market fill notional")
        elif order.price is None:
            raise OrderInvariantError("cannot evaluate quote MAX_ASSET without an order price")
        else:
            values = (order.price * order.quantity,)
    else:
        return
    if any(value > limit for value in values):
        raise OrderError("MAX_ASSET transaction limit would be exceeded")


def validate_venue_order(
    order: VenueOrder,
    metadata: SubmissionMetadata,
    snapshot: VenueSnapshot,
    *,
    reserve_protection: bool = False,
) -> None:
    """Validate one order against a complete, point-in-time venue snapshot."""

    if order.symbol != metadata.symbol:
        raise OrderError("order symbol does not match exchange metadata")
    if order.quote_asset != metadata.quote_asset:
        raise OrderError("order quote asset does not match exchange metadata")
    if metadata.status != "TRADING":
        raise OrderError("bound testnet symbol is not trading")
    if order.side not in {"BUY", "SELL"}:
        raise OrderError("order side must be BUY or SELL")
    if order.order_type == "OCO":
        if order.side != "SELL" or not metadata.oco_allowed:
            raise OrderError("symbol does not allow SELL OCO protection")
        if not {"LIMIT_MAKER", "STOP_LOSS"}.issubset(metadata.order_types):
            raise OrderError("symbol does not support required OCO child types")
        if snapshot.last_price is None:
            raise OrderInvariantError("last price is required for SELL OCO validation")
        if order.price is None or order.stop_price is None:
            raise OrderError("SELL OCO validation requires target and stop prices")
        if not order.stop_price < snapshot.last_price < order.price:
            raise OrderError("SELL OCO requires target > last price > stop")
    elif order.order_type not in metadata.order_types:
        raise OrderError("symbol does not support the requested order type")
    if not order.quantity.is_finite() or order.quantity <= 0:
        raise OrderError("quantity must be finite and positive")
    for price in _prices(order):
        if not price.is_finite() or price <= 0:
            raise OrderError("price must be finite and positive")
    unknown_symbol = {rule.filter_type for rule in metadata.symbol_filters} - KNOWN_SYMBOL_FILTERS
    unknown_exchange = {
        rule.filter_type for rule in metadata.exchange_filters
    } - KNOWN_EXCHANGE_FILTERS
    unknown_asset = {rule.filter_type for rule in metadata.asset_filters} - KNOWN_ASSET_FILTERS
    if unknown_symbol or unknown_exchange or unknown_asset:
        raise OrderError("venue metadata contains an unsupported filter")
    if not snapshot.can_trade:
        raise OrderError("Spot Testnet account is not permitted to trade")

    for rule in metadata.symbol_filters:
        if rule.filter_type == "PRICE_FILTER":
            _validate_price_filter(rule, order)
        elif rule.filter_type == "LOT_SIZE":
            _validate_lot_filter(rule, order)
        elif rule.filter_type == "MARKET_LOT_SIZE" and order.order_type in {
            "MARKET",
            "OCO",
        }:
            _validate_lot_filter(rule, order)
        elif rule.filter_type in {"PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"}:
            _validate_percent_filter(rule, order, snapshot)
        elif rule.filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
            if order.order_type == "OCO":
                _validate_notional_filter(rule, _oco_child(order, "LIMIT_MAKER"), snapshot)
                _validate_notional_filter(rule, _oco_child(order, "MARKET"), snapshot)
            else:
                _validate_notional_filter(rule, order, snapshot)
        elif rule.filter_type in {
            "MAX_NUM_ORDERS",
            "MAX_NUM_ALGO_ORDERS",
            "MAX_NUM_ORDER_LISTS",
        }:
            _validate_count_filter(rule, order, snapshot, reserve_protection)
        elif rule.filter_type == "MAX_POSITION":
            _validate_max_position(rule, order, metadata, snapshot)
        # ICEBERG, TRAILING, and AMEND filters are known but inapplicable: this
        # transport never submits iceberg quantities, trailing deltas, or amends.

    for rule in metadata.exchange_filters:
        if rule.filter_type in {
            "EXCHANGE_MAX_NUM_ORDERS",
            "EXCHANGE_MAX_NUM_ALGO_ORDERS",
            "EXCHANGE_MAX_NUM_ORDER_LISTS",
        }:
            _validate_count_filter(rule, order, snapshot, reserve_protection)
        # EXCHANGE_MAX_NUM_ICEBERG_ORDERS is inapplicable without icebergQty.

    for rule in metadata.asset_filters:
        _validate_max_asset(rule, order, metadata, snapshot)

    required_asset = metadata.quote_asset if order.side == "BUY" else metadata.base_asset
    if order.side == "BUY":
        balance_price = order.price
        if balance_price is None:
            balance_price = _market_price(snapshot)
        required = order.quantity * balance_price
    else:
        required = order.quantity
    if snapshot.balance(required_asset).free < required:
        raise OrderError("Spot Testnet account has insufficient available balance")


def validate_submission(
    order: OrderRequest,
    metadata: SubmissionMetadata,
    snapshot: VenueSnapshot,
    *,
    stop_price: Decimal | None = None,
    reserve_protection: bool = False,
) -> None:
    """Adapt the durable order contract to the shared venue validator."""

    order.validate()
    validate_venue_order(
        VenueOrder(
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            quote_asset=order.quote_asset,
            stop_price=stop_price,
        ),
        metadata,
        snapshot,
        reserve_protection=reserve_protection,
    )


def _common_increment(values: tuple[Decimal, ...]) -> Decimal:
    enabled = tuple(value for value in values if value)
    if not enabled:
        return ZERO
    scale = max(-int(value.as_tuple().exponent) for value in enabled)
    factor = 10**scale
    integers = tuple(int(value * factor) for value in enabled)
    result = integers[0]
    for value in integers[1:]:
        result = abs(result * value) // gcd(result, value)
    return D(result) / D(factor)


def round_protection_quantity(quantity: Decimal, metadata: SubmissionMetadata) -> Decimal:
    """Round down to the common enabled LOT_SIZE/MARKET_LOT_SIZE increment."""

    if not quantity.is_finite() or quantity < 0:
        raise OrderError("protection quantity must be finite and non-negative")
    steps = tuple(
        _decimal(rule.get("stepSize"), f"{rule.filter_type}.stepSize")
        for rule in metadata.symbol_filters
        if rule.filter_type in {"LOT_SIZE", "MARKET_LOT_SIZE"}
    )
    increment = _common_increment(steps)
    if not increment:
        return quantity
    return quantity - (quantity % increment)
