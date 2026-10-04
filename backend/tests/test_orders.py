import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import quote as urlquote

import pytest
from sqlalchemy import create_engine, text

from spotlab.orders import (
    ConfirmedFill,
    ExchangeFilters,
    ExchangeUpdate,
    InsufficientInventory,
    OrderConflict,
    OrderError,
    OrderInvariantError,
    OrderLifecycle,
    OrderRequest,
    OrderState,
    OrderStore,
    validate_order_filters,
)


def filters() -> ExchangeFilters:
    return ExchangeFilters(D("0.10"), D("0.01"), D("0.01"), D("100"), D("5"), D("10000"))


def order(
    intent: str = "buy-1", side: str = "BUY", quantity: str = "2", scope: str = "run-1"
) -> OrderRequest:
    return OrderRequest(
        intent_id=intent,
        client_order_id=f"client-{intent}",
        symbol="BTCUSDT",
        side=side,
        quantity=D(quantity),
        price=D("100.00"),
        quote_asset="USDT",
        inventory_scope=scope,
    )


def fill(trade: str, quantity: str = "1", quote: str = "100") -> ConfirmedFill:
    return ConfirmedFill(trade, D(quantity), D(quote), D("0.1"), "USDT")


def update(
    request: OrderRequest,
    state: OrderState,
    cumulative: str,
    fills: tuple[ConfirmedFill, ...] = (),
) -> ExchangeUpdate:
    return ExchangeUpdate(
        request.client_order_id,
        f"exchange-{request.intent_id}",
        state,
        D(cumulative),
        fills,
    )


class FakeTransport:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.query_calls = 0
        self.cancel_calls = 0
        self.submit_result: ExchangeUpdate | BaseException | None = None
        self.query_result: ExchangeUpdate | BaseException | None = None
        self.cancel_result: ExchangeUpdate | BaseException | None = None
        self.cancel_started: asyncio.Event | None = None
        self.release_cancel: asyncio.Event | None = None

    @staticmethod
    def _result(value: ExchangeUpdate | BaseException | None) -> ExchangeUpdate:
        if isinstance(value, BaseException):
            raise value
        assert value is not None
        return value

    async def submit(self, _order: OrderRequest) -> ExchangeUpdate:
        self.submit_calls += 1
        return self._result(self.submit_result)

    async def query(self, _client_order_id: str) -> ExchangeUpdate:
        self.query_calls += 1
        return self._result(self.query_result)

    async def cancel(self, _client_order_id: str) -> ExchangeUpdate:
        self.cancel_calls += 1
        if self.cancel_started is not None and self.release_cancel is not None:
            self.cancel_started.set()
            await self.release_cancel.wait()
        return self._result(self.cancel_result)


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(tmp_path: Path, request: pytest.FixtureRequest):
    if request.param == "sqlite":
        yield "sqlite:///" + str(tmp_path / "orders.sqlite")
        return
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("PostgreSQL service is exercised in CI")
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield url + "?options=" + urlquote("-csearch_path=" + schema)
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_decimal_exchange_filters_are_strict() -> None:
    validate_order_filters(D("1.00"), D("100.00"), filters())
    for quantity, price in (
        (D("1.001"), D("100")),
        (D("1"), D("100.01")),
        (D("0.01"), D("100")),
        (D("101"), D("100")),
        (D("NaN"), D("100")),
    ):
        with pytest.raises((TypeError, ValueError)):
            validate_order_filters(quantity, price, filters())
    with pytest.raises(TypeError, match="must be Decimal"):
        validate_order_filters(1, D("100"), filters())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_same_intent_is_never_resent(database_url: str) -> None:
    transport = FakeTransport()
    transport.submit_result = TimeoutError("submit timed out")
    lifecycle = OrderLifecycle(database_url, transport)
    request = order()
    first = await lifecycle.submit(request, filters())
    second = await lifecycle.submit(request, filters())
    assert first.state is OrderState.UNKNOWN
    assert second.state is OrderState.UNKNOWN
    assert first.error == "TimeoutError"
    assert "submit timed out" not in (first.error or "")
    assert transport.submit_calls == 1
    with pytest.raises(OrderConflict):
        lifecycle.store.create_or_get(replace(request, price=D("101")))


def test_restart_converts_submitting_to_query_only_unknown(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order()
    store.create_or_get(request)
    assert store.claim_submission(request.intent_id)
    assert store.get(request.intent_id).state is OrderState.SUBMITTING
    restarted = OrderStore(database_url)
    assert restarted.get(request.intent_id).state is OrderState.UNKNOWN


@pytest.mark.asyncio
async def test_timeout_is_settled_by_exchange_query_without_resubmit(database_url: str) -> None:
    transport = FakeTransport()
    request = order()
    transport.submit_result = TimeoutError()
    lifecycle = OrderLifecycle(database_url, transport)
    await lifecycle.submit(request, filters())
    transport.query_result = update(request, OrderState.NEW, "0")
    record = await lifecycle.reconcile(request.intent_id)
    assert record.state is OrderState.NEW
    assert transport.submit_calls == 1
    assert transport.query_calls == 1


@pytest.mark.asyncio
async def test_invalid_submit_response_is_durably_unknown(database_url: str) -> None:
    transport = FakeTransport()
    request = order()
    transport.submit_result = update(request, OrderState.FILLED, "2")
    lifecycle = OrderLifecycle(database_url, transport)

    with pytest.raises(OrderInvariantError, match="lacks confirmed fills"):
        await lifecycle.submit(request, filters())

    record = lifecycle.store.get(request.intent_id)
    assert record.state is OrderState.UNKNOWN
    assert record.error == "OrderInvariantError"
    assert record.exchange_order_id is None


@pytest.mark.asyncio
async def test_reconcile_identity_mismatch_stays_unknown(database_url: str) -> None:
    transport = FakeTransport()
    request = order()
    transport.submit_result = TimeoutError()
    lifecycle = OrderLifecycle(database_url, transport)
    await lifecycle.submit(request, filters())
    transport.query_result = replace(
        update(request, OrderState.NEW, "0"),
        symbol="ETHUSDT",
        side="BUY",
        original_quantity=D("2"),
    )

    with pytest.raises(OrderInvariantError, match="symbol does not match"):
        await lifecycle.reconcile(request.intent_id)

    record = lifecycle.store.get(request.intent_id)
    assert record.state is OrderState.UNKNOWN
    assert record.error == "OrderInvariantError"


@pytest.mark.asyncio
async def test_invalid_cancel_response_keeps_sell_reserved(database_url: str) -> None:
    transport = FakeTransport()
    lifecycle = OrderLifecycle(database_url, transport)
    buy = order(quantity="2")
    lifecycle.store.create_or_get(buy)
    lifecycle.store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "2",
            (ConfirmedFill("cancel-buy", D("2"), D("200"), D("0.2"), "USDT"),),
        ),
    )
    sell = order("cancel-sell", "SELL", "1")
    transport.submit_result = update(sell, OrderState.NEW, "0")
    await lifecycle.submit(sell, filters())
    transport.cancel_result = replace(
        update(sell, OrderState.CANCELED, "0"),
        symbol="BTCUSDT",
        side="SELL",
        original_quantity=D("2"),
    )

    with pytest.raises(OrderInvariantError, match="original quantity does not match"):
        await lifecycle.cancel(sell.intent_id)

    record = lifecycle.store.get(sell.intent_id)
    assert record.state is OrderState.UNKNOWN
    assert record.cancel_requested
    assert lifecycle.store.available_inventory("run-1", "BTCUSDT") == D("1")


@pytest.mark.parametrize(
    ("mismatch", "message"),
    [
        ({"order_type": "MARKET"}, "order type does not match"),
        ({"limit_price": D("101")}, "limit price does not match"),
        ({"time_in_force": "IOC"}, "time in force does not match"),
    ],
)
@pytest.mark.asyncio
async def test_fresh_submit_identity_mismatch_keeps_sell_reserved(
    database_url: str, mismatch: dict[str, object], message: str
) -> None:
    transport = FakeTransport()
    lifecycle = OrderLifecycle(database_url, transport)
    buy = order(quantity="2")
    lifecycle.store.create_or_get(buy)
    lifecycle.store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "2",
            (ConfirmedFill("identity-buy", D("2"), D("200"), D("0.2"), "USDT"),),
        ),
    )
    sell = order("identity-sell", "SELL", "1")
    identity: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "original_quantity": D("1"),
        "order_type": "LIMIT",
        "limit_price": D("100"),
        "time_in_force": "GTC",
    }
    identity.update(mismatch)
    transport.submit_result = replace(update(sell, OrderState.NEW, "0"), **identity)

    with pytest.raises(OrderInvariantError, match=message):
        await lifecycle.submit(sell, filters())

    assert lifecycle.store.get(sell.intent_id).state is OrderState.UNKNOWN
    assert lifecycle.store.available_inventory("run-1", "BTCUSDT") == D("1")


@pytest.mark.parametrize(
    ("mismatch", "message"),
    [
        ({"order_type": "MARKET"}, "order type does not match"),
        ({"limit_price": D("101")}, "limit price does not match"),
        ({"time_in_force": "IOC"}, "time in force does not match"),
    ],
)
@pytest.mark.asyncio
async def test_restart_query_identity_mismatch_keeps_sell_reserved(
    database_url: str, mismatch: dict[str, object], message: str
) -> None:
    store = OrderStore(database_url)
    buy = order(quantity="2")
    store.create_or_get(buy)
    store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "2",
            (ConfirmedFill("restart-buy", D("2"), D("200"), D("0.2"), "USDT"),),
        ),
    )
    sell = order("restart-sell", "SELL", "1")
    store.create_or_get(sell)
    assert store.claim_submission(sell.intent_id)

    transport = FakeTransport()
    lifecycle = OrderLifecycle(database_url, transport)
    assert lifecycle.store.get(sell.intent_id).state is OrderState.UNKNOWN
    identity: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "original_quantity": D("1"),
        "order_type": "LIMIT",
        "limit_price": D("100"),
        "time_in_force": "GTC",
    }
    identity.update(mismatch)
    transport.query_result = replace(update(sell, OrderState.NEW, "0"), **identity)

    with pytest.raises(OrderInvariantError, match=message):
        await lifecycle.reconcile(sell.intent_id)

    assert lifecycle.store.get(sell.intent_id).state is OrderState.UNKNOWN
    assert lifecycle.store.available_inventory("run-1", "BTCUSDT") == D("1")


def test_partial_fills_deduplicate_and_accept_out_of_order(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order()
    store.create_or_get(request)
    store.claim_submission(request.intent_id)
    second = store.apply_update(
        request.intent_id,
        update(request, OrderState.PARTIALLY_FILLED, "2", (fill("trade-2"),)),
    )
    assert second.confirmed_quantity == D("1")
    duplicate = store.apply_update(
        request.intent_id,
        update(request, OrderState.PARTIALLY_FILLED, "2", (fill("trade-2"),)),
    )
    assert duplicate.confirmed_quantity == D("1")
    complete = store.apply_update(
        request.intent_id,
        update(request, OrderState.PARTIALLY_FILLED, "1", (fill("trade-1"),)),
    )
    assert complete.confirmed_quantity == D("2")
    assert complete.confirmed_quote_quantity == D("200")
    assert complete.confirmed_fee_quote == D("0.2")
    assert complete.exchange_cumulative_quantity == D("2")
    assert complete.state is OrderState.FILLED


def test_partial_fill_cannot_regress_to_new(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order()
    store.create_or_get(request)
    partial = store.apply_update(
        request.intent_id,
        update(request, OrderState.PARTIALLY_FILLED, "1", (fill("partial"),)),
    )
    assert partial.state is OrderState.PARTIALLY_FILLED

    stale_new = store.apply_update(
        request.intent_id,
        update(request, OrderState.NEW, "1"),
    )
    assert stale_new.state is OrderState.PARTIALLY_FILLED
    assert stale_new.confirmed_quantity == D("1")


def test_conflicting_or_inconsistent_exchange_events_roll_back(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order()
    store.create_or_get(request)
    store.apply_update(
        request.intent_id,
        update(request, OrderState.PARTIALLY_FILLED, "1", (fill("trade-1"),)),
    )
    conflict = ConfirmedFill("trade-1", D("1"), D("99"), D("0.1"), "USDT")
    with pytest.raises(OrderInvariantError, match="conflicting"):
        store.apply_update(
            request.intent_id,
            update(request, OrderState.PARTIALLY_FILLED, "1", (conflict,)),
        )
    wrong_fee_asset = ConfirmedFill("trade-2", D("1"), D("100"), D("0.1"), "BNB")
    with pytest.raises(OrderInvariantError, match="base or quote"):
        store.apply_update(
            request.intent_id,
            update(request, OrderState.FILLED, "2", (wrong_fee_asset,)),
        )
    assert store.get(request.intent_id).confirmed_quantity == D("1")


def test_base_asset_buy_fee_reduces_sellable_inventory_once(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order(quantity="1")
    store.create_or_get(request)
    base_fee_fill = ConfirmedFill("base-fee-buy", D("1"), D("100"), D("0.1"), "BTC", D("0.001"))
    event = update(request, OrderState.FILLED, "1", (base_fee_fill,))

    first = store.apply_update(request.intent_id, event)
    duplicate = store.apply_update(request.intent_id, event)

    assert first.confirmed_quantity == D("1")
    assert first.confirmed_fee_quote == D("0.1")
    assert duplicate.confirmed_quantity == D("1")
    assert store.available_inventory("run-1", "BTCUSDT") == D("0.999")


def test_base_asset_sell_fee_cannot_oversell_inventory(database_url: str) -> None:
    store = OrderStore(database_url)
    buy = order(quantity="1")
    store.create_or_get(buy)
    store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "1",
            (ConfirmedFill("buy-one", D("1"), D("100"), D("0.1"), "USDT"),),
        ),
    )
    sell = order("sell-with-base-fee", "SELL", "1")
    store.create_or_get(sell)
    sell_fill = ConfirmedFill("sell-base-fee", D("1"), D("100"), D("0.1"), "BTC", D("0.001"))

    with pytest.raises(OrderInvariantError, match="base fees exceed"):
        store.apply_update(
            sell.intent_id,
            update(sell, OrderState.FILLED, "1", (sell_fill,)),
        )

    assert store.get(sell.intent_id).confirmed_quantity == D("0")


def test_base_asset_fee_requires_matching_quote_valuation(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order(quantity="1")
    store.create_or_get(request)
    inconsistent = ConfirmedFill("bad-valuation", D("1"), D("100"), D("0.2"), "BTC", D("0.001"))

    with pytest.raises(OrderInvariantError, match="valuation is inconsistent"):
        store.apply_update(
            request.intent_id,
            update(request, OrderState.FILLED, "1", (inconsistent,)),
        )


def test_list_records_can_filter_inventory_scope_and_symbol(database_url: str) -> None:
    store = OrderStore(database_url)
    btc = order()
    sol = replace(order("sol", scope="other"), symbol="SOLUSDT")
    store.create_or_get(btc)
    store.create_or_get(sol)

    assert [record.request.intent_id for record in store.list_records()] == [
        "buy-1",
        "sol",
    ]
    assert store.list_records(inventory_scope="run-1") == (store.get("buy-1"),)
    assert store.list_records(symbol="SOLUSDT") == (store.get("sol"),)


def test_oco_intents_are_limited_to_sell_orders() -> None:
    replace(order(side="SELL"), order_type="OCO").validate()
    with pytest.raises(ValueError, match="OCO intents must be SELL"):
        replace(order(), order_type="OCO").validate()


@pytest.mark.asyncio
async def test_generic_lifecycle_refuses_oco_without_side_effects(
    database_url: str,
) -> None:
    transport = FakeTransport()
    lifecycle = OrderLifecycle(database_url, transport)
    oco = replace(order("oco", "SELL", "1"), order_type="OCO")

    with pytest.raises(OrderError, match="ProtectionLifecycle"):
        await lifecycle.submit(oco, filters())
    with pytest.raises(KeyError):
        lifecycle.store.get(oco.intent_id)
    assert transport.submit_calls == 0

    buy = order(quantity="1")
    lifecycle.store.create_or_get(buy)
    lifecycle.store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "1",
            (ConfirmedFill("oco-buy", D("1"), D("100"), D("0.1"), "USDT"),),
        ),
    )
    lifecycle.store.create_or_get(oco)

    with pytest.raises(OrderError, match="ProtectionLifecycle"):
        await lifecycle.reconcile(oco.intent_id)
    with pytest.raises(OrderError, match="ProtectionLifecycle"):
        await lifecycle.cancel(oco.intent_id)

    record = lifecycle.store.get(oco.intent_id)
    assert record.state is OrderState.INTENDED
    assert not record.cancel_requested
    assert transport.query_calls == 0
    assert transport.cancel_calls == 0


def test_legacy_fill_table_migrates_base_fee_with_zero_default(tmp_path: Path) -> None:
    database_url = "sqlite:///" + str(tmp_path / "legacy.sqlite")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE order_fills ("
                "exchange_trade_id VARCHAR(128) PRIMARY KEY,"
                "intent_id VARCHAR(128) NOT NULL,quantity VARCHAR(128) NOT NULL,"
                "quote_quantity VARCHAR(128) NOT NULL,fee_quote VARCHAR(128) NOT NULL,"
                "fee_asset VARCHAR(32) NOT NULL)"
            )
        )
    engine.dispose()

    store = OrderStore(database_url)
    request = order(quantity="1")
    store.create_or_get(request)
    store.apply_update(
        request.intent_id,
        update(request, OrderState.FILLED, "1", (fill("post-migration"),)),
    )

    with store.db.connect() as connection:
        fee_base = connection.execute(
            text("SELECT fee_base FROM order_fills WHERE exchange_trade_id='post-migration'")
        ).scalar_one()
    assert D(fee_base) == D("0")
    assert store.available_inventory("run-1", "BTCUSDT") == D("1")


@pytest.mark.asyncio
async def test_cancel_racing_with_fill_accounts_fill_but_keeps_canceled(database_url: str) -> None:
    transport = FakeTransport()
    request = order()
    transport.submit_result = update(request, OrderState.NEW, "0")
    lifecycle = OrderLifecycle(database_url, transport)
    await lifecycle.submit(request, filters())
    transport.cancel_started = asyncio.Event()
    transport.release_cancel = asyncio.Event()
    transport.cancel_result = update(request, OrderState.CANCELED, "0")
    cancel_task = asyncio.create_task(lifecycle.cancel(request.intent_id))
    await transport.cancel_started.wait()
    lifecycle.store.apply_update(
        request.intent_id,
        update(request, OrderState.PARTIALLY_FILLED, "1", (fill("late-fill"),)),
    )
    transport.release_cancel.set()
    canceled = await cancel_task
    assert canceled.state is OrderState.CANCELED
    assert canceled.confirmed_quantity == D("1")
    repeated = await lifecycle.cancel(request.intent_id)
    assert repeated.confirmed_quantity == D("1")
    assert transport.cancel_calls == 1


def test_delayed_full_fill_advances_canceled_order_to_filled(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order(quantity="1")
    store.create_or_get(request)
    store.apply_update(request.intent_id, update(request, OrderState.CANCELED, "0"))
    result = store.apply_update(
        request.intent_id,
        update(request, OrderState.PARTIALLY_FILLED, "1", (fill("late-full"),)),
    )
    assert result.state is OrderState.FILLED
    assert result.confirmed_quantity == D("1")


def test_sell_intents_cannot_oversell_confirmed_scoped_inventory(database_url: str) -> None:
    store = OrderStore(database_url)
    buy = order(quantity="2")
    store.create_or_get(buy)
    store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "2",
            (ConfirmedFill("buy-fill", D("2"), D("200"), D("0.2"), "USDT"),),
        ),
    )
    sell = order("sell-1", "SELL", "1.5")
    store.create_or_get(sell)
    with pytest.raises(InsufficientInventory):
        store.create_or_get(order("sell-2", "SELL", "1"))
    store.apply_update(sell.intent_id, update(sell, OrderState.CANCELED, "0"))
    store.create_or_get(order("sell-2", "SELL", "1"))


def test_inventory_scope_does_not_mix_symbols(database_url: str) -> None:
    store = OrderStore(database_url)
    buy_btc = order(quantity="2", scope="shared-strategy")
    store.create_or_get(buy_btc)
    store.apply_update(
        buy_btc.intent_id,
        update(
            buy_btc,
            OrderState.FILLED,
            "2",
            (ConfirmedFill("btc-fill", D("2"), D("200"), D("0.2"), "USDT"),),
        ),
    )
    sell_sol = replace(
        order("sell-sol", "SELL", "2", scope="shared-strategy"),
        symbol="SOLUSDT",
    )
    with pytest.raises(InsufficientInventory):
        store.create_or_get(sell_sol)


def test_canceled_executed_quantity_stays_reserved_until_fill_arrives(
    database_url: str,
) -> None:
    store = OrderStore(database_url)
    buy = order(quantity="2")
    store.create_or_get(buy)
    store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "2",
            (ConfirmedFill("buy-two", D("2"), D("200"), D("0.2"), "USDT"),),
        ),
    )
    sell_a = order("sell-a", "SELL", "2")
    store.create_or_get(sell_a)
    store.apply_update(sell_a.intent_id, update(sell_a, OrderState.CANCELED, "1"))

    with pytest.raises(InsufficientInventory):
        store.create_or_get(order("sell-b-too-large", "SELL", "2"))
    sell_b = order("sell-b", "SELL", "1")
    store.create_or_get(sell_b)

    late = store.apply_update(
        sell_a.intent_id,
        update(sell_a, OrderState.PARTIALLY_FILLED, "1", (fill("sell-a-late"),)),
    )
    assert late.state is OrderState.CANCELED
    assert late.confirmed_quantity == D("1")
    with pytest.raises(InsufficientInventory):
        store.create_or_get(order("sell-c", "SELL", "0.01"))


def test_distinct_concurrent_sells_share_one_inventory_reservation(database_url: str) -> None:
    store = OrderStore(database_url)
    buy = order(quantity="2")
    store.create_or_get(buy)
    store.apply_update(
        buy.intent_id,
        update(
            buy,
            OrderState.FILLED,
            "2",
            (ConfirmedFill("concurrent-buy", D("2"), D("200"), D("0.2"), "USDT"),),
        ),
    )
    requests = (order("sell-left", "SELL", "2"), order("sell-right", "SELL", "2"))

    def reserve(request: OrderRequest) -> bool:
        try:
            store.create_or_get(request)
        except InsufficientInventory:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(reserve, requests))
    assert sorted(accepted) == [False, True]


def test_postgres_writer_takes_global_advisory_lock_before_mutation() -> None:
    events: list[str] = []

    class FakeConnection:
        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> None:
            events.append("begin")

        def execute(self, statement: object, _params: object) -> None:
            assert "pg_advisory_xact_lock" in str(statement)
            events.append("advisory-lock")

        def commit(self) -> None:
            events.append("commit")

        def rollback(self) -> None:
            events.append("rollback")

    class FakeDialect:
        name = "postgresql"

    class FakeEngine:
        dialect = FakeDialect()

        def connect(self) -> FakeConnection:
            return FakeConnection()

    store = object.__new__(OrderStore)
    store.db = FakeEngine()  # type: ignore[assignment]
    with store._writer():
        events.append("mutation")
    assert events == ["begin", "advisory-lock", "mutation", "commit"]


def test_concurrent_creation_has_one_durable_intent(database_url: str) -> None:
    store = OrderStore(database_url)
    request = order()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: store.create_or_get(request), range(12)))
    assert sum(created for _, created in results) == 1
    assert all(record.request == request for record, _ in results)
