import asyncio
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from backend.tests.test_testnet import NOW, credentials, response_for_basics

from spotlab.orders import (
    ConfirmedFill,
    ExchangeUpdate,
    InsufficientInventory,
    OrderError,
    OrderInvariantError,
    OrderRequest,
    OrderState,
    OrderStore,
    RetryableTransportError,
)
from spotlab.protection import ProtectionLifecycle, ProtectionRequest
from spotlab.testnet import BinanceTestnetTransport


def funded_store(path: Path) -> OrderStore:
    store = OrderStore("sqlite:///" + str(path))
    buy = OrderRequest("buy", "buy-client", "BTCUSDT", "BUY", D("1"), D("100"), "USDT", "scope")
    store.create_or_get(buy)
    store.apply_update(
        "buy",
        ExchangeUpdate(
            "buy-client",
            "BTCUSDT:1",
            OrderState.FILLED,
            D(1),
            (ConfirmedFill("BTCUSDT:1", D(1), D(100), D("0.1"), "BTC", D("0.001")),),
        ),
    )
    return store


def protection() -> ProtectionRequest:
    return ProtectionRequest(
        OrderRequest(
            "protect", "list-protect", "BTCUSDT", "SELL", D("0.999"), D(110), "USDT", "scope", "OCO"
        ),
        D(90),
    )


class Venue:
    def __init__(self, store: OrderStore):
        self.store = store
        self.posts = 0
        self.timeout = False
        self.halted = False
        self.extra_filter: str | None = None
        self.missing_child = False
        self.wrong_stop = False
        self.missing_fills = False
        self.executed = "0"
        self.stop_state = "NEW"
        self.target_state = "NEW"
        self.spec = protection()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        basic = response_for_basics(req)
        if basic is not None:
            if req.url.path == "/api/v3/exchangeInfo":
                data = basic.json()
                data["symbols"][0]["orderTypes"] += ["LIMIT_MAKER", "STOP_LOSS"]
                if self.halted:
                    data["symbols"][0]["status"] = "HALT"
                if self.extra_filter:
                    data["symbols"][0]["filters"].append({"filterType": self.extra_filter})
                return httpx.Response(200, json=data)
            return basic
        path = req.url.path
        if path == "/api/v3/referencePrice":
            return httpx.Response(
                200, json={"symbol": "BTCUSDT", "referencePrice": "100", "timestamp": NOW}
            )
        if path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "100"})
        if path == "/api/v3/orderList/oco":
            self.posts += 1
            assert self.store.get("protect").state == OrderState.SUBMITTING
            assert self.store.available_inventory("scope", "BTCUSDT") == 0
            if self.timeout:
                raise httpx.ReadTimeout("hidden", request=req)
            return httpx.Response(200, json={})
        if path == "/api/v3/orderList":
            children = [
                {"symbol": "BTCUSDT", "orderId": 2, "clientOrderId": self.spec.above_id},
                {"symbol": "BTCUSDT", "orderId": 3, "clientOrderId": self.spec.below_id},
            ]
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderListId": 9,
                    "listClientOrderId": "list-protect",
                    "contingencyType": "OCO",
                    # Even ALL_DONE must not release a still-open child reservation.
                    "listOrderStatus": "ALL_DONE",
                    "orders": children[:1] if self.missing_child else children,
                },
            )
        if path == "/api/v3/order":
            cid = req.url.params["origClientOrderId"]
            above = cid == self.spec.above_id
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderListId": 9,
                    "orderId": 2 if above else 3,
                    "clientOrderId": cid,
                    "side": "SELL",
                    "timeInForce": "GTC",
                    "origQty": "0.999",
                    "price": "110" if above else "0",
                    "stopPrice": "91" if self.wrong_stop else "90",
                    "type": "LIMIT_MAKER" if above else "STOP_LOSS",
                    "status": self.target_state if above else self.stop_state,
                    "executedQty": "0" if above else self.executed,
                },
            )
        if path == "/api/v3/myTrades":
            trades = []
            if req.url.params["orderId"] == "3" and self.executed != "0" and not self.missing_fills:
                trades = [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": 3,
                        "id": 2,
                        "price": "90",
                        "qty": self.executed,
                        "quoteQty": str(D(self.executed) * D(90)),
                        "commission": "0",
                        "commissionAsset": "USDT",
                    }
                ]
            return httpx.Response(200, json=trades)
        raise AssertionError(f"unexpected path: {path}")


def test_oco_single_reservation_partial_and_terminal_reconciliation(tmp_path: Path) -> None:
    store = funded_store(tmp_path / "orders.sqlite")
    venue = Venue(store)

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: NOW,
        ) as transport:
            life = ProtectionLifecycle(store, transport)
            assert (await life.submit(protection())).state == OrderState.NEW
            assert (await life.reconcile("protect")).both_legs_open
            assert store.available_inventory("scope", "BTCUSDT") == 0
            with pytest.raises(InsufficientInventory):
                store.create_or_get(
                    replace(
                        protection().order,
                        intent_id="extra",
                        client_order_id="extra",
                        order_type="LIMIT",
                        quantity=D("0.001"),
                    )
                )
            venue.target_state = "CANCELED"
            venue.stop_state = "PARTIALLY_FILLED"
            venue.executed = "0.4"
            observed = await life.reconcile("protect")
            assert observed.order.confirmed_quantity == D("0.4")
            assert not observed.both_legs_open
            assert store.available_inventory("scope", "BTCUSDT") == 0
            # A duplicate observation cannot consume inventory twice.
            await life.reconcile("protect")
            venue.stop_state = "CANCELED"
            assert (await life.reconcile("protect")).order.state == OrderState.CANCELED
            assert store.available_inventory("scope", "BTCUSDT") == D("0.599")
            assert venue.posts == 1

    asyncio.run(scenario())


def test_timeout_restart_queries_without_resubmission(tmp_path: Path) -> None:
    path = tmp_path / "orders.sqlite"
    store = funded_store(path)
    venue = Venue(store)
    venue.timeout = True

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: NOW,
        ) as transport:
            with pytest.raises(RetryableTransportError):
                await ProtectionLifecycle(store, transport).submit(protection())
            assert store.get("protect").state == OrderState.UNKNOWN
        restarted = OrderStore("sqlite:///" + str(path))
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: NOW,
        ) as transport:
            life = ProtectionLifecycle(restarted, transport)
            assert (await life.submit(protection())).state == OrderState.UNKNOWN
            assert (await life.reconcile("protect")).both_legs_open
            assert venue.posts == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["missing_child", "wrong_stop", "missing_fills"])
def test_invalid_observation_keeps_reservation_unknown(tmp_path: Path, fault: str) -> None:
    store = funded_store(tmp_path / "orders.sqlite")
    venue = Venue(store)

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: NOW,
        ) as transport:
            life = ProtectionLifecycle(store, transport)
            await life.submit(protection())
            setattr(venue, fault, True)
            if fault == "missing_fills":
                venue.target_state = "CANCELED"
                venue.stop_state = "CANCELED"
                venue.executed = "0.4"
            with pytest.raises(OrderInvariantError):
                await life.reconcile("protect")
            assert store.get("protect").state == OrderState.UNKNOWN
            assert store.available_inventory("scope", "BTCUSDT") == 0
            assert venue.posts == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("rule", ["UNKNOWN_RULE", "PERCENT_PRICE_BY_SIDE", "MAX_NUM_ORDERS"])
def test_oco_unevaluated_filters_block_mutation_but_not_recovery(tmp_path: Path, rule: str) -> None:
    store = funded_store(tmp_path / "orders.sqlite")
    venue = Venue(store)
    venue.extra_filter = rule

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: NOW,
        ) as transport:
            life = ProtectionLifecycle(store, transport)
            with pytest.raises(OrderError):
                await life.submit(protection())
            assert venue.posts == 0
            with pytest.raises(KeyError):
                store.get("protect")
            venue.extra_filter = None
            await life.submit(protection())
            # A later rule change or halt must not block existing-order recovery.
            venue.extra_filter = rule
            venue.halted = True
            assert (await life.reconcile("protect")).order.state == OrderState.NEW

    asyncio.run(scenario())
