import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from typing import cast

import pytest
from backend.tests.test_orders import database_url as database_url

from spotlab.orders import (
    ConfirmedFill,
    ExchangeFilters,
    ExchangeUpdate,
    OrderConflict,
    OrderRequest,
    OrderState,
)
from spotlab.protection import ProtectionObservation
from spotlab.testnet import BinanceTestnetTransport
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
        self.submit_failure = False
        self.preflight_failure = False
        self.deadline_seen: int | None = None
        self.during_final_preflight = None

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
        protection_target=None,
        protection_stop=None,
    ):
        if self.during_final_preflight is not None:
            self.during_final_preflight()
        if before_send is not None:
            before_send()
        self.posts += 1
        self.deadline_seen = deadline_ms
        assert reserve_protection
        if self.submit_failure:
            raise TimeoutError("ambiguous")
        return self.current

    async def query(self, _client):
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
