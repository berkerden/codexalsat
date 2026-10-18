import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs

import httpx
import pytest
from backend.tests.test_orders import database_url as database_url
from backend.tests.test_testnet import NOW as EXCHANGE_TIME
from backend.tests.test_testnet import credentials, response_for_basics

from spotlab.orders import (
    ConfirmedFill,
    ExchangeFilters,
    ExchangeUpdate,
    OrderConflict,
    OrderRequest,
    OrderState,
)
from spotlab.protection import ProtectionObservation
from spotlab.testnet import AccountOpenOrder, BinanceTestnetTransport
from spotlab.testnet_cycle import CyclePlan
from spotlab.testnet_cycle import TestnetCycle as Cycle

NOW = 1_800_000_000_000
FILTERS = ExchangeFilters(D("0.1"), D("0.001"), D("0.001"), D(100), D(5))


def plan(name: str = "one") -> CyclePlan:
    return CyclePlan(name, "BTCUSDT", D("0.5"), D(100), D(110), D(90), D(50), NOW + 60_000)


def event(order: OrderRequest, state: OrderState, qty: str = "0") -> ExchangeUpdate:
    amount = D(qty)
    fills = (
        ()
        if amount == 0
        else (
            ConfirmedFill(
                "BTCUSDT:1", amount, amount * 100, amount * D("0.1"), "BTC", amount * D("0.001")
            ),
        )
    )
    return ExchangeUpdate(order.client_order_id, "BTCUSDT:100", state, amount, fills)


class Venue:
    symbol = "BTCUSDT"

    def __init__(self) -> None:
        self.posts = 0
        self.cancels = 0
        self.entry = plan().entry()
        self.current = event(self.entry, OrderState.NEW)
        self.cancel_result: ExchangeUpdate | None = None
        self.query_failure = False
        self.queries = 0
        self.submit_failure = False
        self.preflight_failure = False
        self.deadline_seen: int | None = None
        self.during_final_preflight = None
        self.after_final_audit = None
        self.audit_balance = D("100")
        self.audit_orders: tuple[AccountOpenOrder, ...] = ()

    async def account_wide_snapshot(self):
        return SimpleNamespace(
            account=SimpleNamespace(
                can_trade=True,
                balances=(SimpleNamespace(asset="USDT", free=self.audit_balance, locked=D(0)),),
                standard_commission=None,
                tax_commission=None,
                special_commission=None,
                discount=None,
                observed_at_ms=NOW,
            ),
            open_orders=self.audit_orders,
        )

    async def prepare_submission(self, order, **_kwargs):
        if self.preflight_failure:
            raise ValueError("no capacity")
        return SimpleNamespace(filters=FILTERS)

    async def exchange_info(self):
        return SimpleNamespace(filters=FILTERS)

    async def submit(
        self,
        order,
        *,
        deadline_ms=None,
        reserve_protection=False,
        before_send=None,
        entry_audit=None,
        protection_target=None,
        protection_stop=None,
    ):
        if self.during_final_preflight is not None:
            self.during_final_preflight()
        if entry_audit is not None:
            await entry_audit()
        if self.after_final_audit is not None:
            self.after_final_audit()
        if before_send is not None:
            before_send()
        self.posts += 1
        self.deadline_seen = deadline_ms
        assert reserve_protection
        if self.submit_failure:
            raise TimeoutError("ambiguous")
        return self.current

    async def query(self, _client):
        self.queries += 1
        if self.query_failure:
            raise TimeoutError("unavailable")
        return self.current

    async def cancel(self, _client):
        self.cancels += 1
        if self.cancel_result is not None:
            self.current = self.cancel_result
        else:
            self.current = replace(self.current, status=OrderState.CANCELED)
        return self.current


class ProtectiveVenue:
    def __init__(self, cycle: Cycle):
        self.store = cycle.store
        self.posts = 0
        self.unknown = False
        self.reconcile_unknown = False

    async def submit(self, request):
        record, _ = self.store.create_or_get(request.order)
        if not self.store.claim_submission(request.order.intent_id):
            return record
        self.posts += 1
        if self.unknown:
            self.store.mark_unknown(request.order.intent_id, "TimeoutError")
            raise TimeoutError()
        return self.store.apply_update(
            request.order.intent_id,
            ExchangeUpdate(request.order.client_order_id, "oco:BTCUSDT:9", OrderState.NEW, D(0)),
        )

    async def reconcile(self, intent_id):
        if self.reconcile_unknown:
            raise TimeoutError()
        order = self.store.get(intent_id).request
        record = self.store.apply_update(
            intent_id, ExchangeUpdate(order.client_order_id, "oco:BTCUSDT:9", OrderState.NEW, D(0))
        )
        return ProtectionObservation(record, True, NOW)


@pytest.fixture
def setup(database_url: str, monkeypatch):
    venue = Venue()
    clock = [NOW]
    cycle = Cycle(
        database_url,
        cast(BinanceTestnetTransport, venue),
        clock_ms=lambda: clock[0],
    )
    protection = ProtectiveVenue(cycle)
    cycle.protection = protection  # type: ignore[assignment]
    monkeypatch.setattr(
        "spotlab.testnet_cycle.round_protection_quantity",
        lambda qty, _: (qty // D("0.001")) * D("0.001"),
    )
    return cycle, venue, protection, clock


@pytest.mark.asyncio
async def test_partial_cancel_late_fill_sizes_net_inventory_and_reports_dust(setup) -> None:
    cycle, venue, protection, clock = setup
    venue.current = event(plan().entry(), OrderState.PARTIALLY_FILLED, "0.2")
    # The remaining buy fills during cancellation: protect .4995 net, not .1998.
    venue.cancel_result = replace(
        event(plan().entry(), OrderState.FILLED, "0.5"),
        fills=(
            venue.current.fills[0],
            ConfirmedFill("BTCUSDT:2", D("0.3"), D(30), D("0.03"), "BTC", D("0.0003")),
        ),
    )
    result = await cycle.start(plan())
    assert result["state"] == "PROTECTED"
    assert venue.posts == venue.cancels == protection.posts == 1
    assert venue.deadline_seen == plan().entry_deadline_ms
    assert cycle.store.get(plan().protection_id).request.quantity == D("0.499")
    assert D(result["dust_quantity"]) == D("0.0005")
    assert D(result["unprotected_quantity"]) == D("0.0005")
    await cycle.advance("one", allow_mutations=True)
    assert protection.posts == venue.posts == 1
    clock[0] += 5001
    assert not cycle.status("one")["coverage_observation_fresh"]
    assert D(cycle.status("one")["unprotected_quantity"]) == D("0.4995")


@pytest.mark.asyncio
async def test_unknown_submit_is_query_only_on_repeat_and_restart(setup) -> None:
    cycle, venue, _, _clock = setup
    venue.submit_failure = True
    first = await cycle.start(plan())
    assert first["state"] == "ENTRY_OPEN"  # query recovered the ambiguous order
    await cycle.start(plan())
    await cycle.advance("one", allow_mutations=True)
    assert venue.posts == 1


@pytest.mark.asyncio
async def test_incomplete_terminal_fills_never_create_protection(setup) -> None:
    cycle, venue, protection, _ = setup
    venue.current = replace(
        event(plan().entry(), OrderState.CANCELED), cumulative_quantity=D("0.2")
    )
    result = await cycle.start(plan())
    assert result["reason"] == "entry_trade_details_incomplete"
    assert protection.posts == 0


@pytest.mark.asyncio
async def test_no_fill_expiry_cancels_once_and_closes(setup) -> None:
    cycle, venue, protection, clock = setup
    assert (await cycle.start(plan()))["state"] == "ENTRY_OPEN"
    clock[0] += 60_001
    assert (await cycle.advance("one", allow_mutations=True))["state"] == "NO_FILL"
    assert venue.cancels == 1 and protection.posts == 0


@pytest.mark.asyncio
async def test_ambiguous_protection_never_replaced(setup) -> None:
    cycle, venue, protection, _ = setup
    venue.current = event(plan().entry(), OrderState.FILLED, "0.5")
    protection.unknown = True
    protection.reconcile_unknown = True
    assert (await cycle.start(plan()))["state"] == "BLOCKED"
    assert (await cycle.advance("one", allow_mutations=True))["state"] == "BLOCKED"
    assert protection.posts == 1
    protection.reconcile_unknown = False
    assert (await cycle.advance("one", allow_mutations=True))["state"] == "PROTECTED"
    assert protection.posts == 1


@pytest.mark.asyncio
async def test_no_new_buy_on_management_and_safe_abort_of_unsent_plan(setup) -> None:
    cycle, venue, _, _ = setup
    venue.preflight_failure = True
    assert (await cycle.start(plan()))["state"] == "BLOCKED"
    venue.preflight_failure = False
    assert (await cycle.advance("one", allow_mutations=True))["reason"] == "entry_not_sent"
    assert (await cycle.advance("one", allow_mutations=True, stop_entry=True))["state"] == "ABORTED"
    await cycle.start(plan())
    assert venue.posts == 0


@pytest.mark.asyncio
async def test_stop_request_survives_failed_query(setup) -> None:
    cycle, venue, _, _ = setup
    await cycle.start(plan())
    venue.query_failure = True
    await cycle.advance("one", allow_mutations=True, stop_entry=True)
    venue.query_failure = False
    assert (await cycle.advance("one", allow_mutations=True))["state"] == "NO_FILL"
    assert venue.cancels == 1


@pytest.mark.asyncio
async def test_cycle_terms_and_single_active_cycle_are_enforced(setup) -> None:
    cycle, venue, _, _ = setup
    await cycle.start(plan())
    with pytest.raises(OrderConflict):
        await cycle.start(replace(plan(), target_price=D(120)))
    with pytest.raises(OrderConflict):
        await cycle.start(plan("two"))
    assert venue.posts == 1


def test_concurrent_cycle_claim_is_atomic(setup) -> None:
    cycle, _, _, _ = setup

    def claim(name):
        try:
            cycle._persist(plan(name))
            return True
        except OrderConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ["one", "two"])) == [False, True]
    assert len(cycle.store.list_records()) == 1


@pytest.mark.asyncio
async def test_read_only_management_never_creates_protection(setup) -> None:
    cycle, venue, protection, _ = setup
    await cycle.start(plan())
    venue.current = event(plan().entry(), OrderState.FILLED, "0.5")
    result = await cycle.advance("one")
    assert result["state"] == "UNPROTECTED"
    assert protection.posts == 0
    assert (await cycle.advance("one", allow_mutations=True))["state"] == "PROTECTED"


@pytest.mark.parametrize(
    "changes",
    [
        {"max_notional": D(101)},
        {"quantity": D("NaN")},
        {"stop_price": D(110)},
        {"quantity": D(1)},
        {"entry_deadline_ms": NOW},
    ],
)
def test_invalid_or_expired_plan_sends_nothing(setup, changes) -> None:
    cycle, venue, _, _ = setup
    with pytest.raises(ValueError):
        asyncio.run(cycle.start(replace(plan(), **changes)))
    assert venue.posts == 0


class SimulatedProcessLoss(BaseException):
    """Stop a controller after the exchange accepted a mutation."""


class StatefulTestnetVenue:
    """Small immutable-order exchange ledger behind a real Testnet transport."""

    def __init__(self, terms: CyclePlan) -> None:
        self.terms = terms
        self.entry = terms.entry()
        self.entry_status = "NEW"
        self.entry_executed = D(0)
        self.entry_trades: list[dict[str, object]] = []
        self.late_trades_visible = True
        self.oco_quantity: D | None = None
        self.drop_accepted: set[tuple[str, str]] = set()
        self.query_outages: set[str] = set()
        self.cancel_before_accept = False
        self.entry_posts = 0
        self.cancel_attempts = 0
        self.cancel_accepts = 0
        self.oco_posts = 0
        self.mutation_payloads: list[tuple[str, str, dict[str, list[str]]]] = []

    @staticmethod
    def _params(req: httpx.Request) -> dict[str, list[str]]:
        raw = req.content.decode() if req.method == "POST" else req.url.query.decode()
        return parse_qs(raw, keep_blank_values=True)

    @staticmethod
    def _assert_signed(params: dict[str, list[str]], req: httpx.Request) -> None:
        assert req.url.scheme == "https" and req.url.host == "testnet.binance.vision"
        assert req.headers["X-MBX-APIKEY"]
        assert params["recvWindow"] == ["5000"]
        assert params["timestamp"] == [str(EXCHANGE_TIME)]
        assert len(params["signature"]) == 1 and len(params["signature"][0]) == 64

    def _accept_or_drop(self, req: httpx.Request) -> None:
        operation = (req.method, req.url.path)
        if operation in self.drop_accepted:
            self.drop_accepted.remove(operation)
            raise SimulatedProcessLoss(f"process ended after {req.method} acceptance")

    def _entry_reply(self) -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "orderId": 100,
            "clientOrderId": self.entry.client_order_id,
            "side": "BUY",
            "origQty": "0.5",
            "type": "LIMIT",
            "price": "100",
            "timeInForce": "GTC",
            "status": self.entry_status,
            "executedQty": str(self.entry_executed),
        }

    def _oco_child_reply(self, client_id: str) -> dict[str, object]:
        assert self.oco_quantity is not None
        protection = self.terms.protection(self.oco_quantity)
        above = client_id == protection.above_id
        assert above or client_id == protection.below_id
        return {
            "symbol": "BTCUSDT",
            "orderId": 201 if above else 202,
            "orderListId": 9,
            "clientOrderId": client_id,
            "side": "SELL",
            "origQty": str(self.oco_quantity),
            "type": "LIMIT_MAKER" if above else "STOP_LOSS",
            "price": "110" if above else "0",
            "stopPrice": "0" if above else "90",
            "timeInForce": "GTC",
            "status": "NEW",
            "executedQty": "0",
        }

    def __call__(self, req: httpx.Request) -> httpx.Response:
        basic = response_for_basics(req)
        if basic is not None:
            if req.url.path == "/api/v3/exchangeInfo":
                info = basic.json()
                info["symbols"][0]["orderTypes"] += ["LIMIT_MAKER", "STOP_LOSS"]
                return httpx.Response(200, json=info)
            return basic

        path = req.url.path
        params = self._params(req)
        if path in self.query_outages and req.method == "GET":
            raise httpx.ReadTimeout("temporary query outage", request=req)
        if path == "/api/v3/ticker/price":
            assert req.method == "GET" and params == {"symbol": ["BTCUSDT"]}
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "100"})
        if path == "/api/v3/order" and req.method == "POST":
            self._assert_signed(params, req)
            assert params == {
                "symbol": ["BTCUSDT"],
                "side": ["BUY"],
                "type": ["LIMIT"],
                "timeInForce": ["GTC"],
                "quantity": ["0.5"],
                "price": ["100"],
                "newClientOrderId": [self.entry.client_order_id],
                "newOrderRespType": ["RESULT"],
                "recvWindow": ["5000"],
                "timestamp": [str(EXCHANGE_TIME)],
                "signature": params["signature"],
            }
            self.entry_posts += 1
            self.mutation_payloads.append((req.method, path, params))
            self._accept_or_drop(req)
            return httpx.Response(200, json=self._entry_reply())
        if path == "/api/v3/order" and req.method == "DELETE":
            self._assert_signed(params, req)
            assert params == {
                "symbol": ["BTCUSDT"],
                "origClientOrderId": [self.entry.client_order_id],
                "recvWindow": ["5000"],
                "timestamp": [str(EXCHANGE_TIME)],
                "signature": params["signature"],
            }
            self.cancel_attempts += 1
            self.mutation_payloads.append((req.method, path, params))
            if self.cancel_before_accept:
                raise httpx.ConnectError("cancel did not reach venue", request=req)
            self.cancel_accepts += 1
            self.entry_status = "CANCELED"
            self.entry_executed = D("0.4")
            self._accept_or_drop(req)
            return httpx.Response(200, json=self._entry_reply())
        if path == "/api/v3/order" and req.method == "GET":
            self._assert_signed(params, req)
            client_id = params["origClientOrderId"][0]
            if client_id == self.entry.client_order_id:
                return httpx.Response(200, json=self._entry_reply())
            return httpx.Response(200, json=self._oco_child_reply(client_id))
        if path == "/api/v3/myTrades":
            self._assert_signed(params, req)
            order_id = int(params["orderId"][0])
            trades = self.entry_trades if order_id == 100 else []
            if not self.late_trades_visible:
                trades = trades[:1]
            return httpx.Response(200, json=trades)
        if path == "/api/v3/orderList/oco":
            self._assert_signed(params, req)
            assert params["symbol"] == ["BTCUSDT"]
            assert params["side"] == ["SELL"]
            assert params["abovePrice"] == ["110"]
            assert params["belowStopPrice"] == ["90"]
            quantity = D(params["quantity"][0])
            protection = self.terms.protection(quantity)
            assert params == {
                "symbol": ["BTCUSDT"],
                "side": ["SELL"],
                "quantity": [str(quantity)],
                "listClientOrderId": [protection.order.client_order_id],
                "aboveType": ["LIMIT_MAKER"],
                "abovePrice": ["110"],
                "aboveClientOrderId": [protection.above_id],
                "belowType": ["STOP_LOSS"],
                "belowStopPrice": ["90"],
                "belowClientOrderId": [protection.below_id],
                "newOrderRespType": ["RESULT"],
                "recvWindow": ["5000"],
                "timestamp": [str(EXCHANGE_TIME)],
                "signature": params["signature"],
            }
            assert self.oco_quantity in {None, quantity}
            self.oco_quantity = quantity
            self.oco_posts += 1
            self.mutation_payloads.append((req.method, path, params))
            self._accept_or_drop(req)
            return httpx.Response(200, json={})
        if path == "/api/v3/orderList":
            self._assert_signed(params, req)
            assert self.oco_quantity is not None
            protection = self.terms.protection(self.oco_quantity)
            assert params["origClientOrderId"] == [protection.order.client_order_id]
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderListId": 9,
                    "listClientOrderId": protection.order.client_order_id,
                    "contingencyType": "OCO",
                    "orders": [
                        {"symbol": "BTCUSDT", "orderId": 201, "clientOrderId": protection.above_id},
                        {"symbol": "BTCUSDT", "orderId": 202, "clientOrderId": protection.below_id},
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {req.method} {path}")


def recovery_terms() -> CyclePlan:
    return replace(plan(), entry_deadline_ms=EXCHANGE_TIME + 60_000)


def test_real_transport_lost_accepted_buy_recovers_query_only_after_restart(
    database_url: str,
) -> None:
    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)
    venue.drop_accepted.add(("POST", "/api/v3/order"))

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            controller = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            with pytest.raises(SimulatedProcessLoss):
                await controller.start(terms)
            assert controller.store.get(terms.entry_id).state is OrderState.UNKNOWN
            venue.query_outages.add("/api/v3/order")
            assert (await controller.advance("one", allow_mutations=True))["state"] == "BLOCKED"
            assert controller.store.get(terms.entry_id).state is OrderState.UNKNOWN
            assert venue.entry_posts == 1
            controller.store.db.dispose()

        venue.query_outages.clear()
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            restarted = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            assert (await restarted.advance("one", allow_mutations=True))["state"] == "ENTRY_OPEN"
            assert (await restarted.advance("one", allow_mutations=True))["state"] == "ENTRY_OPEN"
            entry = restarted.store.get(terms.entry_id)
            assert entry.state is OrderState.NEW
            assert entry.confirmed_quantity == entry.exchange_cumulative_quantity == 0
            assert venue.entry_posts == 1 and venue.cancel_attempts == venue.oco_posts == 0
            assert [(method, path) for method, path, _ in venue.mutation_payloads] == [
                ("POST", "/api/v3/order")
            ]

    asyncio.run(scenario())


def test_real_transport_lost_accepted_cancel_recovers_late_fills_and_net_fee_size(
    database_url: str,
) -> None:
    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)
    entry_trades = [
        {
            "symbol": "BTCUSDT",
            "orderId": 100,
            "id": 1,
            "qty": "0.2",
            "price": "100",
            "quoteQty": "20",
            "commission": "0.0002",
            "commissionAsset": "BTC",
        },
        {
            "symbol": "BTCUSDT",
            "orderId": 100,
            "id": 2,
            "qty": "0.2",
            "price": "100",
            "quoteQty": "20",
            "commission": "0.0002",
            "commissionAsset": "BTC",
        },
    ]

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            controller = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            assert (await controller.start(terms))["state"] == "ENTRY_OPEN"
            venue.entry_trades = entry_trades
            venue.entry_status = "PARTIALLY_FILLED"
            venue.entry_executed = D("0.2")
            venue.late_trades_visible = False
            venue.drop_accepted.add(("DELETE", "/api/v3/order"))
            with pytest.raises(SimulatedProcessLoss):
                await controller.advance("one", allow_mutations=True, stop_entry=True)
            interrupted = controller.store.get(terms.entry_id)
            assert interrupted.state is OrderState.UNKNOWN
            assert interrupted.cancel_requested
            assert interrupted.confirmed_quantity == D("0.2")
            venue.query_outages.add("/api/v3/order")
            assert (await controller.advance("one", allow_mutations=True))["state"] == "BLOCKED"
            assert venue.entry_posts == venue.cancel_attempts == venue.cancel_accepts == 1
            controller.store.db.dispose()

        venue.query_outages.clear()
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            restarted = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            incomplete = await restarted.advance("one", allow_mutations=True)
            assert incomplete["reason"] == "entry_trade_details_incomplete"
            assert venue.oco_posts == 0
            assert restarted.store.get(terms.entry_id).exchange_cumulative_quantity == D("0.4")
            assert (await restarted.advance("one", allow_mutations=True))["reason"] == (
                "entry_trade_details_incomplete"
            )
            venue.late_trades_visible = True
            protected = await restarted.advance("one", allow_mutations=True)
            assert protected["state"] == "PROTECTED"
            gross = D("0.2") + D("0.2")
            base_fees = D("0.0002") + D("0.0002")
            assert restarted.store.owned_inventory(terms.scope, "BTCUSDT") == gross - base_fees
            assert restarted.store.get(terms.entry_id).confirmed_fee_quote == D("0.04")
            assert restarted.store.get(terms.protection_id).request.quantity == D("0.399")
            assert D(protected["dust_quantity"]) == gross - base_fees - D("0.399")
            records_before = restarted.store.list_records()
            repeated = await restarted.advance("one", allow_mutations=True)
            assert repeated["state"] == "PROTECTED"
            assert repeated["dust_quantity"] == protected["dust_quantity"]
            assert restarted.store.list_records() == records_before
            assert venue.entry_posts == venue.cancel_attempts == venue.cancel_accepts == 1
            assert venue.oco_posts == 1
            assert [(method, path) for method, path, _ in venue.mutation_payloads] == [
                ("POST", "/api/v3/order"),
                ("DELETE", "/api/v3/order"),
                ("POST", "/api/v3/orderList/oco"),
            ]

    asyncio.run(scenario())


def test_real_transport_cancel_not_accepted_is_never_retried(database_url: str) -> None:
    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)
    partial_trade = [
        {
            "symbol": "BTCUSDT",
            "orderId": 100,
            "id": 1,
            "qty": "0.2",
            "price": "100",
            "quoteQty": "20",
            "commission": "0",
            "commissionAsset": "USDT",
        }
    ]

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            controller = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            assert (await controller.start(terms))["state"] == "ENTRY_OPEN"
            venue.entry_trades = partial_trade
            venue.entry_status = "PARTIALLY_FILLED"
            venue.entry_executed = D("0.2")
            venue.cancel_before_accept = True
            settling = await controller.advance("one", allow_mutations=True, stop_entry=True)
            assert settling["state"] == "ENTRY_SETTLING"
            assert settling["reason"] == "cancel_pending_query_only"
            assert controller.store.get(terms.entry_id).cancel_requested
            assert venue.cancel_attempts == 1 and venue.cancel_accepts == 0
            controller.store.db.dispose()

        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            restarted = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            for _ in range(2):
                result = await restarted.advance("one", allow_mutations=True)
                assert result["state"] == "ENTRY_SETTLING"
                assert result["reason"] == "cancel_pending_query_only"
            assert venue.cancel_attempts == 1 and venue.cancel_accepts == venue.oco_posts == 0
            assert [(method, path) for method, path, _ in venue.mutation_payloads] == [
                ("POST", "/api/v3/order"),
                ("DELETE", "/api/v3/order"),
            ]

    asyncio.run(scenario())


def test_real_transport_lost_accepted_oco_keeps_one_reservation_across_restart(
    database_url: str,
) -> None:
    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)
    venue.entry_status = "FILLED"
    venue.entry_executed = D("0.5")
    venue.entry_trades = [
        {
            "symbol": "BTCUSDT",
            "orderId": 100,
            "id": 1,
            "qty": "0.5",
            "price": "100",
            "quoteQty": "50",
            "commission": "0.05",
            "commissionAsset": "USDT",
        }
    ]
    venue.drop_accepted.add(("POST", "/api/v3/orderList/oco"))

    async def scenario() -> None:
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            controller = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            with pytest.raises(SimulatedProcessLoss):
                await controller.start(terms)
            protection = controller.store.get(terms.protection_id)
            assert protection.state is OrderState.UNKNOWN
            assert controller.store.available_inventory(terms.scope, "BTCUSDT") == 0
            venue.query_outages.add("/api/v3/orderList")
            assert (await controller.advance("one", allow_mutations=True))["state"] == "BLOCKED"
            assert venue.entry_posts == venue.oco_posts == 1
            controller.store.db.dispose()

        venue.query_outages.clear()
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            restarted = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            assert (await restarted.advance("one", allow_mutations=True))["state"] == "PROTECTED"
            records = restarted.store.list_records()
            assert len(records) == 2
            assert restarted.store.available_inventory(terms.scope, "BTCUSDT") == 0
            assert (await restarted.advance("one", allow_mutations=True))["state"] == "PROTECTED"
            assert restarted.store.list_records() == records
            assert venue.entry_posts == venue.oco_posts == 1 and venue.cancel_attempts == 0
            assert [(method, path) for method, path, _ in venue.mutation_payloads] == [
                ("POST", "/api/v3/order"),
                ("POST", "/api/v3/orderList/oco"),
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("lose_oco_reply", [False, True])
def test_real_transports_full_cycle_and_restart_without_duplicate_orders(
    database_url: str, lose_oco_reply: bool
) -> None:
    from urllib.parse import parse_qs

    import httpx
    from backend.tests.test_testnet import NOW as EXCHANGE_TIME
    from backend.tests.test_testnet import credentials, response_for_basics

    entry_posts = []
    oco_posts = []
    exited = False
    terms = replace(plan(), entry_deadline_ms=EXCHANGE_TIME + 60_000)
    entry = terms.entry()
    protective = terms.protection(D("0.5"))

    def order_reply(client_id):
        is_entry = client_id == entry.client_order_id
        above = client_id == protective.above_id
        return {
            "symbol": "BTCUSDT",
            "orderId": 100 if is_entry else (2 if above else 3),
            "orderListId": -1 if is_entry else 9,
            "clientOrderId": client_id,
            "side": "BUY" if is_entry else "SELL",
            "origQty": "0.5",
            "type": "LIMIT" if is_entry else ("LIMIT_MAKER" if above else "STOP_LOSS"),
            "price": "100" if is_entry else ("110" if above else "0"),
            "stopPrice": "0" if is_entry or above else "90",
            "timeInForce": "GTC",
            "status": "FILLED"
            if is_entry or (exited and not above)
            else ("CANCELED" if exited else "NEW"),
            "executedQty": "0.5" if is_entry or (exited and not above) else "0",
        }

    def handle(req):
        path = req.url.path
        basic = response_for_basics(req)
        if basic is not None:
            if path == "/api/v3/exchangeInfo":
                info = basic.json()
                info["symbols"][0]["orderTypes"] += ["LIMIT_MAKER", "STOP_LOSS"]
                return httpx.Response(200, json=info)
            return basic
        if path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "100"})
        if path == "/api/v3/order":
            if req.method == "POST":
                entry_posts.append(parse_qs(req.content.decode()))
                return httpx.Response(200, json=order_reply(entry.client_order_id))
            return httpx.Response(200, json=order_reply(req.url.params["origClientOrderId"]))
        if path == "/api/v3/orderList/oco":
            oco_posts.append(parse_qs(req.content.decode()))
            if lose_oco_reply:
                raise httpx.ReadTimeout("response lost after exchange acceptance", request=req)
            return httpx.Response(200, json={})
        if path == "/api/v3/orderList":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderListId": 9,
                    "listClientOrderId": protective.order.client_order_id,
                    "contingencyType": "OCO",
                    "orders": [
                        {"symbol": "BTCUSDT", "orderId": 2, "clientOrderId": protective.above_id},
                        {"symbol": "BTCUSDT", "orderId": 3, "clientOrderId": protective.below_id},
                    ],
                },
            )
        if path == "/api/v3/myTrades":
            order_id = int(req.url.params["orderId"])
            trades = []
            if order_id == 100 or (order_id == 3 and exited):
                trades = [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": order_id,
                        "id": order_id,
                        "qty": "0.5",
                        "price": "100" if order_id == 100 else "90",
                        "quoteQty": "50" if order_id == 100 else "45",
                        "commission": "0.05",
                        "commissionAsset": "USDT",
                    }
                ]
            return httpx.Response(200, json=trades)
        raise AssertionError(path)

    async def scenario():
        nonlocal exited
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(handle),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            controller = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            result = await controller.start(terms)
            assert result["state"] == ("BLOCKED" if lose_oco_reply else "PROTECTED")
            controller.store.db.dispose()
        async with BinanceTestnetTransport(
            "BTCUSDT",
            credentials=credentials(),
            mock_transport=httpx.MockTransport(handle),
            clock_ms=lambda: EXCHANGE_TIME,
        ) as wire:
            restarted = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
            assert (await restarted.advance("one", allow_mutations=True))["state"] == "PROTECTED"
            exited = True
            assert (await restarted.advance("one", allow_mutations=True))["state"] == "EXITED"
            assert restarted.store.owned_inventory(terms.scope, "BTCUSDT") == 0
        assert len(entry_posts) == len(oco_posts) == 1
        assert oco_posts[0]["quantity"] == ["0.500"] or oco_posts[0]["quantity"] == ["0.5"]

    asyncio.run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("during_preflight", [False, True])
async def test_persisted_stop_prevents_unsent_entry_even_during_final_preflight(
    setup, during_preflight: bool
) -> None:
    from sqlalchemy import text

    cycle, venue, _, _ = setup
    cycle._persist(plan())

    def request_stop():
        with cycle.store._writer() as conn:
            conn.execute(text("UPDATE testnet_cycles SET stop_entry=1 WHERE cycle_id='one'"))

    if during_preflight:
        venue.during_final_preflight = request_stop
    else:
        request_stop()
    assert (await cycle.start(plan()))["state"] == "ABORTED"
    assert venue.posts == venue.cancels == 0
    venue.query_failure = True  # A never-sent order must never be looked up.
    assert (await cycle.advance("one", allow_mutations=True))["state"] == "ABORTED"
    assert venue.posts == venue.cancels == 0


@pytest.mark.asyncio
async def test_unknown_exit_ledger_invalidates_recent_cached_coverage(setup) -> None:
    cycle, venue, _, _ = setup
    venue.current = event(plan().entry(), OrderState.FILLED, "0.5")
    assert (await cycle.start(plan()))["coverage_observation_fresh"]
    cycle.store.mark_unknown(plan().protection_id, "query_failed")
    result = cycle.status("one")
    assert not result["coverage_observation_fresh"]
    assert D(result["unprotected_quantity"]) == D("0.4995")


@pytest.mark.asyncio
async def test_changed_final_account_snapshot_blocks_post_and_restart_never_queries(setup) -> None:
    cycle, venue, _, _ = setup
    venue.during_final_preflight = lambda: setattr(venue, "audit_balance", D("99"))
    result = await cycle.start(plan())
    assert result["state"] == "BLOCKED"
    assert result["reason"] == "account_snapshot_changed"
    assert venue.posts == venue.queries == 0
    restarted = Cycle(
        cycle.store.db.url.render_as_string(hide_password=False),
        cast(BinanceTestnetTransport, venue),
    )
    repeated = await restarted.advance("one", allow_mutations=True)
    assert repeated["state"] == "ABORTED"
    assert repeated["reason"] == "account_snapshot_changed"
    assert venue.posts == venue.queries == 0


@pytest.mark.asyncio
async def test_matching_candidate_remote_order_at_final_audit_blocks_post(setup) -> None:
    cycle, venue, _, _ = setup
    entry = plan().entry()

    def reveal_candidate() -> None:
        venue.audit_orders = (
            AccountOpenOrder(
                entry.symbol,
                7,
                entry.client_order_id,
                "BUY",
                "LIMIT",
                entry.quantity,
                D(0),
                -1,
                entry.price,
                D(0),
                "GTC",
                "NEW",
            ),
        )

    venue.during_final_preflight = reveal_candidate
    result = await cycle.start(plan())
    assert result["state"] == "BLOCKED"
    assert result["reason"] == "candidate_remote_open"
    assert venue.posts == venue.queries == 0


@pytest.mark.asyncio
async def test_local_ledger_change_after_final_audit_blocks_post(setup) -> None:
    cycle, venue, _, _ = setup

    def add_local_intent() -> None:
        cycle.store.create_or_get(
            OrderRequest(
                "concurrent",
                "spotlab-0123456789abcdef01234567",
                "SOLUSDT",
                "BUY",
                D("1"),
                D("10"),
                "USDT",
                "other-scope",
            )
        )

    venue.after_final_audit = add_local_intent
    result = await cycle.start(plan())
    assert result["state"] == "BLOCKED"
    assert result["reason"] == "local_ledger_changed"
    assert venue.posts == venue.queries == 0
