from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from backend.tests.test_orders import database_url as database_url
from sqlalchemy import event, inspect, text

from spotlab.legacy_migration import (
    LegacyMigrationError,
    LegacyTestnetReader,
    migrate_legacy,
)
from spotlab.orders import (
    TESTNET_ACCOUNT_VENUE,
    ExchangeUpdate,
    OrderRequest,
    OrderState,
    OrderStore,
)
from spotlab.testnet import TESTNET_BASE_URL
from spotlab.testnet import TestnetCredentials as Credentials

D = Decimal
NOW = 1_800_000_000_000
UID = 12345


def credentials() -> Credentials:
    return Credentials.model_validate({"api_key": "fixture-key", "secret": "fixture-secret"})


def legacy_store(database_url) -> OrderStore:
    store = OrderStore(database_url, recover_submitting=False)
    request = OrderRequest(
        "legacy-entry",
        "entry-0123456789abcdef01234567",
        "BTCUSDT",
        "BUY",
        D("0.01"),
        D("1000"),
        "USDT",
        "legacy-scope",
    )
    store.create_or_get(request)
    store.apply_update(
        request.intent_id,
        ExchangeUpdate(request.client_order_id, "BTCUSDT:42", OrderState.CANCELED, D(0)),
    )
    with store._writer() as conn:
        conn.execute(
            text(
                "UPDATE order_intents SET created_at=:now,updated_at=:now WHERE intent_id=:intent"
            ),
            {"now": NOW, "intent": request.intent_id},
        )
    return store


class Venue:
    def __init__(self, store: OrderStore | None = None) -> None:
        self.store = store
        self.uid = UID
        self.mutate_on_open = False
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        assert request.method == "GET"
        assert f"{request.url.scheme}://{request.url.host}" == TESTNET_BASE_URL
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": NOW})
        assert request.headers["X-MBX-APIKEY"] == "fixture-key"
        if request.url.path == "/api/v3/account":
            return httpx.Response(200, json={"uid": self.uid})
        if request.url.path == "/api/v3/order":
            values = parse_qs(request.url.query.decode())
            assert values["symbol"] == ["BTCUSDT"]
            assert values["orderId"] == ["42"]
            assert values["origClientOrderId"] == ["entry-0123456789abcdef01234567"]
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderId": 42,
                    "orderListId": -1,
                    "clientOrderId": "entry-0123456789abcdef01234567",
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "origQty": "0.01",
                    "price": "1000",
                    "stopPrice": "0",
                    "status": "CANCELED",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0",
                    "time": NOW,
                    "updateTime": NOW,
                },
            )
        if request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v3/openOrders":
            if self.mutate_on_open:
                assert self.store is not None
                with self.store._writer() as conn:
                    conn.execute(
                        text(
                            "UPDATE order_intents SET error='concurrent change' "
                            "WHERE intent_id='legacy-entry'"
                        )
                    )
                self.mutate_on_open = False
            return httpx.Response(200, json=[])
        raise AssertionError(request.url.path)


@pytest.mark.asyncio
async def test_preview_is_read_only_and_apply_only_adds_binding_and_receipt(database_url) -> None:
    store = legacy_store(database_url)
    venue = Venue()
    with store.db.connect() as conn:
        before_orders = tuple(conn.execute(text("SELECT * FROM order_intents")).one())

    async with LegacyTestnetReader(
        credentials(),
        mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: NOW,
    ) as reader:
        preview = await migrate_legacy(store, reader)
    assert preview == {
        "mode": "testnet",
        "production_enabled": False,
        "status": "verified_preview",
        "applied": False,
        "order_count": 1,
        "fill_count": 0,
        "cycle_count": 0,
        "unrelated_open_order_count": 0,
    }
    assert store.account_binding_reason() == "migration_required"

    async with LegacyTestnetReader(
        credentials(),
        mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: NOW,
    ) as reader:
        applied = await migrate_legacy(store, reader, apply=True)
    assert applied["status"] == "migrated"
    store.require_account_binding(TESTNET_ACCOUNT_VENUE, UID)
    with store.db.connect() as conn:
        assert tuple(conn.execute(text("SELECT * FROM order_intents")).one()) == before_orders
        receipt = conn.execute(text("SELECT * FROM ledger_account_migrations")).mappings().one()
    assert receipt["version"] == 1
    assert receipt["order_count"] == 1
    assert receipt["fill_count"] == 0
    assert receipt["cycle_count"] == 0
    assert len(receipt["before_ledger_sha256"]) == 64
    assert len(receipt["remote_evidence_sha256"]) == 64
    assert all(request.method == "GET" for request in venue.seen)
    assert json.dumps(applied).find(str(UID)) == -1
    store.db.dispose()


@pytest.mark.asyncio
async def test_identity_change_and_local_race_fail_without_binding(database_url) -> None:
    store = legacy_store(database_url)
    venue = Venue(store)
    venue.mutate_on_open = True
    async with LegacyTestnetReader(
        credentials(),
        mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: NOW,
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_ledger_changed"):
            await migrate_legacy(store, reader, apply=True)
    assert store.account_binding_reason() == "migration_required"

    venue.uid = UID + 1
    calls = 0

    def changing_uid(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = venue(request)
        if request.url.path == "/api/v3/account":
            calls += 1
            return httpx.Response(200, json={"uid": UID if calls == 1 else UID + 1})
        return response

    async with LegacyTestnetReader(
        credentials(),
        mock_transport=httpx.MockTransport(changing_uid),
        clock_ms=lambda: NOW,
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_identity_changed"):
            await migrate_legacy(store, reader)
    assert store.account_binding_reason() == "migration_required"
    store.db.dispose()


@pytest.mark.asyncio
async def test_suspicious_open_order_blocks_but_unrelated_manual_is_counted(database_url) -> None:
    store = legacy_store(database_url)
    venue = Venue()

    def open_order(request: httpx.Request) -> httpx.Response:
        response = venue(request)
        if request.url.path == "/api/v3/openOrders":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "ETHUSDT",
                        "orderId": 9,
                        "orderListId": -1,
                        "clientOrderId": "manual-order",
                    }
                ],
            )
        return response

    async with LegacyTestnetReader(
        credentials(),
        mock_transport=httpx.MockTransport(open_order),
        clock_ms=lambda: NOW,
    ) as reader:
        result = await migrate_legacy(store, reader)
    assert result["unrelated_open_order_count"] == 1

    def orphan(request: httpx.Request) -> httpx.Response:
        response = venue(request)
        if request.url.path == "/api/v3/openOrders":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "SOLUSDT",
                        "orderId": 10,
                        "orderListId": -1,
                        "clientOrderId": "stop-ffffffffffffffffffffffff",
                    }
                ],
            )
        return response

    async with LegacyTestnetReader(
        credentials(),
        mock_transport=httpx.MockTransport(orphan),
        clock_ms=lambda: NOW,
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_remote_open_orders"):
            await migrate_legacy(store, reader)
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "SOLUSDT"),
        ("orderId", 99),
        ("clientOrderId", "different"),
        ("side", "SELL"),
        ("type", "MARKET"),
        ("origQty", "0.02"),
        ("price", "1001"),
        ("status", "NEW"),
        ("executedQty", "0.001"),
        ("time", NOW - 1001),
        ("updateTime", NOW + 1),
        ("orderListId", 100),
    ],
)
async def test_order_evidence_mismatch_never_binds(database_url, field, value) -> None:
    store = legacy_store(database_url)
    venue = Venue()

    def changed(request):
        response = venue(request)
        if request.url.path == "/api/v3/order":
            payload = response.json()
            payload[field] = value
            return httpx.Response(200, json=payload)
        return response

    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(changed), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(LegacyMigrationError):
            await migrate_legacy(store, reader, apply=True)
    assert store.account_binding_reason() == "migration_required"
    assert not inspect(store.db).has_table("ledger_account_migrations")
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "auth", "redirect", "timeout", "malformed"])
async def test_failed_history_requests_are_sanitized_and_read_only(database_url, failure) -> None:
    store = legacy_store(database_url)
    venue = Venue()

    def failed(request):
        if request.url.path == "/api/v3/order":
            if failure == "timeout":
                raise httpx.ReadTimeout("private request details", request=request)
            if failure == "malformed":
                return httpx.Response(200, content=b"private invalid json")
            code = {"missing": 400, "auth": 401, "redirect": 302}[failure]
            return httpx.Response(
                code,
                headers={"Location": "https://example.com"},
                json={"code": -2013, "msg": "private details"},
            )
        return venue(request)

    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(failed), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="^migration_history_unavailable$"):
            await migrate_legacy(store, reader, apply=True)
    assert store.account_binding_reason() == "migration_required"
    store.db.dispose()


@pytest.mark.asyncio
async def test_apply_rollback_and_lock_expiry(database_url, monkeypatch) -> None:
    from contextlib import contextmanager

    store = legacy_store(database_url)
    venue = Venue()

    def fail_receipt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO ledger_account_migrations"):
            raise RuntimeError("injected receipt failure")

    event.listen(store.db, "before_cursor_execute", fail_receipt)
    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(RuntimeError, match="injected receipt failure"):
            await migrate_legacy(store, reader, apply=True)
    event.remove(store.db, "before_cursor_execute", fail_receipt)
    assert store.account_binding_reason() == "migration_required"
    assert not inspect(store.db).has_table("ledger_account_migrations")

    original_writer = store._writer
    ticks = [0.0]
    acquisitions = 0

    @contextmanager
    def delayed_writer():
        nonlocal acquisitions
        with original_writer() as conn:
            acquisitions += 1
            if acquisitions == 2:
                ticks[0] = 31.0
            yield conn

    monkeypatch.setattr(store, "_writer", delayed_writer)
    async with LegacyTestnetReader(
        credentials(),
        mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: NOW,
        monotonic=lambda: ticks[0],
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_history_unavailable"):
            await migrate_legacy(store, reader, apply=True)
    assert store.account_binding_reason() == "migration_required"
    store.db.dispose()


def closed_cycle(database_url):
    from types import SimpleNamespace

    from spotlab.orders import ConfirmedFill
    from spotlab.testnet_cycle import CyclePlan
    from spotlab.testnet_cycle import TestnetCycle as Cycle

    cycle = Cycle(database_url, SimpleNamespace(symbol="BTCUSDT"))
    store = cycle.store
    plan = CyclePlan(
        "legacy-closed", "BTCUSDT", D(".01"), D(1000), D(1100), D(900), D(10), NOW + 10_000
    )
    cycle._persist(plan)
    store.apply_update(
        plan.entry_id,
        ExchangeUpdate(
            plan.entry().client_order_id,
            "BTCUSDT:42",
            OrderState.FILLED,
            D(".01"),
            (ConfirmedFill("BTCUSDT:1", D(".01"), D(10), D(0), "BTC"),),
        ),
    )
    protection = plan.protection(D(".01"))
    cycle.protection._persist_payload(protection)
    store.create_or_get(protection.order)
    store.apply_update(
        plan.protection_id,
        ExchangeUpdate(
            protection.order.client_order_id,
            "oco:BTCUSDT:100",
            OrderState.FILLED,
            D(".01"),
            (ConfirmedFill("BTCUSDT:2", D(".01"), D(9), D(0), "USDT"),),
        ),
    )
    with store._writer() as conn:
        conn.execute(text("UPDATE order_intents SET created_at=:at,updated_at=:at"), {"at": NOW})
        conn.execute(
            text(
                "UPDATE testnet_cycles SET state='EXITED',protection_quantity='0.01',"
                "dust='0',stop_entry=1,updated_at=:at"
            ),
            {"at": NOW},
        )
    return store, plan


class ClosedVenue(Venue):
    def __init__(self, plan):
        super().__init__()
        entry = plan.entry()
        protection = plan.protection(D(".01"))
        self.orders = {}
        for oid, client, side, kind, price, stop, state, executed, quote, list_id in [
            (42, entry.client_order_id, "BUY", "LIMIT", "1000", "0", "FILLED", ".01", "10", -1),
            (43, protection.above_id, "SELL", "LIMIT_MAKER", "1100", "0", "EXPIRED", "0", "0", 100),
            (44, protection.below_id, "SELL", "STOP_LOSS", "0", "900", "FILLED", ".01", "9", 100),
        ]:
            self.orders[oid] = dict(
                symbol="BTCUSDT",
                orderId=oid,
                orderListId=list_id,
                clientOrderId=client,
                side=side,
                type=kind,
                timeInForce="GTC",
                origQty=".01",
                price=price,
                stopPrice=stop,
                status=state,
                executedQty=executed,
                cummulativeQuoteQty=quote,
                time=NOW,
                updateTime=NOW,
            )
        self.order_list = dict(
            symbol="BTCUSDT",
            orderListId=100,
            listClientOrderId=protection.order.client_order_id,
            contingencyType="OCO",
            listStatusType="ALL_DONE",
            listOrderStatus="ALL_DONE",
            transactionTime=NOW,
            orders=[
                {key: self.orders[oid][key] for key in ("symbol", "orderId", "clientOrderId")}
                for oid in (43, 44)
            ],
        )
        self.trades = {
            42: [
                dict(
                    id=1,
                    orderId=42,
                    symbol="BTCUSDT",
                    isBuyer=True,
                    time=NOW,
                    price="1000",
                    qty=".01",
                    quoteQty="10",
                    commission="0",
                    commissionAsset="BTC",
                )
            ],
            43: [],
            44: [
                dict(
                    id=2,
                    orderId=44,
                    symbol="BTCUSDT",
                    isBuyer=False,
                    time=NOW,
                    price="900",
                    qty=".01",
                    quoteQty="9",
                    commission="0",
                    commissionAsset="USDT",
                )
            ],
        }
        self.open_orders = []

    def __call__(self, request):
        path = request.url.path
        if path not in {
            "/api/v3/order",
            "/api/v3/orderList",
            "/api/v3/myTrades",
            "/api/v3/openOrders",
        }:
            return super().__call__(request)
        self.seen.append(request)
        assert request.method == "GET"
        assert str(request.url).startswith(TESTNET_BASE_URL + "/")
        values = parse_qs(request.url.query.decode())
        if path == "/api/v3/order":
            payload = self.orders[int(values["orderId"][0])]
            assert values["origClientOrderId"] == [payload["clientOrderId"]]
        elif path == "/api/v3/orderList":
            payload = self.order_list
        elif path == "/api/v3/openOrders":
            payload = self.open_orders
        else:
            from_id, limit = int(values["fromId"][0]), int(values["limit"][0])
            payload = [t for t in self.trades[int(values["orderId"][0])] if t["id"] >= from_id][
                :limit
            ]
        return httpx.Response(200, json=payload)


def historical_rows(store):
    with store.db.connect() as conn:
        return tuple(
            (
                table,
                tuple(
                    tuple(row) for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY 1"))
                ),
            )
            for table in ("order_intents", "order_fills", "protection_intents", "testnet_cycles")
        )


@pytest.mark.asyncio
async def test_closed_buy_oco_cycle_pagination_and_restart(database_url, monkeypatch) -> None:
    import spotlab.legacy_migration as migration

    store, plan = closed_cycle(database_url)
    before = historical_rows(store)
    venue = ClosedVenue(plan)
    # One result fills each page; the next empty page is necessary to prove completeness.
    monkeypatch.setattr(migration, "TRADE_PAGE_SIZE", 1)
    for apply in (False, True):
        async with LegacyTestnetReader(
            credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
        ) as reader:
            result = await migrate_legacy(store, reader, apply=apply)
        assert result["fill_count"] == 2
        assert result["cycle_count"] == 1
        assert historical_rows(store) == before
    store.db.dispose()
    restarted = OrderStore(database_url, recover_submitting=False)
    restarted.require_account_binding(TESTNET_ACCOUNT_VENUE, UID)
    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_not_required"):
            await migrate_legacy(restarted, reader, apply=True)
    assert historical_rows(restarted) == before
    restarted.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("commission", ["0", "0.01"])
async def test_missing_commission_asset_blocks_without_mutation(
    database_url, commission
) -> None:
    store, plan = closed_cycle(database_url)
    venue = ClosedVenue(plan)
    trade = venue.trades[44][0]
    trade["commission"] = commission
    del trade["commissionAsset"]
    if commission == "0":
        with store._writer() as conn:
            conn.execute(
                text(
                    "UPDATE order_fills SET fee_asset='' "
                    "WHERE exchange_trade_id='BTCUSDT:2'"
                )
            )
    before = historical_rows(store)

    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_evidence_mismatch"):
            await migrate_legacy(store, reader, apply=True)

    assert historical_rows(store) == before
    with store.db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ledger_account_binding")).scalar_one() == 0
        assert not inspect(conn).has_table("ledger_account_migrations")
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("commission_asset", [None, ""])
async def test_explicit_empty_commission_asset_accepts_zero_fee(
    database_url, commission_asset
) -> None:
    store, plan = closed_cycle(database_url)
    venue = ClosedVenue(plan)
    venue.trades[44][0]["commissionAsset"] = commission_asset
    with store._writer() as conn:
        conn.execute(
            text(
                "UPDATE order_fills SET fee_asset='' WHERE exchange_trade_id='BTCUSDT:2'"
            )
        )
    before = historical_rows(store)

    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
    ) as reader:
        result = await migrate_legacy(store, reader, apply=True)

    assert result["status"] == "migrated"
    store.require_account_binding(TESTNET_ACCOUNT_VENUE, UID)
    assert historical_rows(store) == before
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["LIMIT", "OCO"])
async def test_noncanonical_exchange_id_blocks_without_mutation(
    database_url, order_type
) -> None:
    if order_type == "LIMIT":
        store = legacy_store(database_url)
        venue = Venue()
        with store._writer() as conn:
            conn.execute(
                text(
                    "UPDATE order_intents SET exchange_order_id='BTCUSDT:042' "
                    "WHERE intent_id='legacy-entry'"
                )
            )
        with store.db.connect() as conn:
            before = tuple(conn.execute(text("SELECT * FROM order_intents")).one())
    else:
        store, plan = closed_cycle(database_url)
        venue = ClosedVenue(plan)
        with store._writer() as conn:
            conn.execute(
                text(
                    "UPDATE order_intents SET exchange_order_id='oco:BTCUSDT:0100' "
                    "WHERE order_type='OCO'"
                )
            )
        before = historical_rows(store)

    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_local_inconsistent"):
            await migrate_legacy(store, reader, apply=True)

    with store.db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ledger_account_binding")).scalar_one() == 0
        assert not inspect(conn).has_table("ledger_account_migrations")
        if order_type == "LIMIT":
            assert tuple(conn.execute(text("SELECT * FROM order_intents")).one()) == before
    if order_type == "OCO":
        assert historical_rows(store) == before
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("offset_ms", "passes"), [(1_000, True), (1_001, False)])
async def test_cycle_remote_entry_creation_respects_persisted_deadline(
    database_url, offset_ms, passes
) -> None:
    store, plan = closed_cycle(database_url)
    venue = ClosedVenue(plan)
    remote_time = plan.entry_deadline_ms + offset_ms
    for order in venue.orders.values():
        order["time"] = remote_time
        order["updateTime"] = remote_time
    for trades in venue.trades.values():
        for trade in trades:
            trade["time"] = remote_time
    venue.order_list["transactionTime"] = remote_time
    with store._writer() as conn:
        conn.execute(
            text("UPDATE order_intents SET updated_at=:at"),
            {"at": remote_time},
        )
        conn.execute(
            text("UPDATE testnet_cycles SET updated_at=:at"),
            {"at": remote_time},
        )
    before = historical_rows(store)

    def late_server(request):
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": remote_time})
        return venue(request)

    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(late_server), clock_ms=lambda: remote_time
    ) as reader:
        if passes:
            result = await migrate_legacy(store, reader)
            assert result["status"] == "verified_preview"
        else:
            with pytest.raises(LegacyMigrationError, match="migration_evidence_mismatch"):
                await migrate_legacy(store, reader, apply=True)

    assert historical_rows(store) == before
    with store.db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ledger_account_binding")).scalar_one() == 0
        assert not inspect(conn).has_table("ledger_account_migrations")
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing_trade",
        "extra_trade",
        "changed_trade_id",
        "fee",
        "fee_asset",
        "trade_side",
        "trade_time",
        "trade_order",
        "child_active",
        "child_quantity",
        "list_status",
        "list_time",
        "list_members",
        "child_open_id",
        "child_open_list",
        "cycle_stop",
        "cycle_dust",
        "entry_incomplete",
        "orphan_protection",
        "incident",
        "receipt_table",
    ],
)
async def test_closed_cycle_conflicts_block_without_historical_changes(database_url, fault) -> None:
    store, plan = closed_cycle(database_url)
    venue = ClosedVenue(plan)
    trade = venue.trades[44][0]
    if fault == "missing_trade":
        venue.trades[44] = []
    elif fault == "extra_trade":
        venue.trades[44].append({**trade, "id": 3})
    elif fault == "changed_trade_id":
        trade["id"] = 3
    elif fault == "fee":
        trade["commission"] = "0.01"
    elif fault == "fee_asset":
        trade["commissionAsset"] = "BNB"
    elif fault == "trade_side":
        trade["isBuyer"] = True
    elif fault == "trade_time":
        trade["time"] = NOW - 1
    elif fault == "trade_order":
        trade["orderId"] = 43
    elif fault == "child_active":
        venue.orders[43]["status"] = "NEW"
    elif fault == "child_quantity":
        venue.orders[44]["origQty"] = ".02"
    elif fault == "list_status":
        venue.order_list["listStatusType"] = "EXEC_STARTED"
    elif fault == "list_time":
        venue.order_list["transactionTime"] = NOW - 1001
    elif fault == "list_members":
        venue.order_list["orders"].pop()
    elif fault.startswith("child_open"):
        venue.open_orders = [
            dict(
                symbol="BTCUSDT",
                orderId=43 if fault.endswith("id") else 99,
                orderListId=-1 if fault.endswith("id") else 100,
                clientOrderId="renamed-manual-looking",
            )
        ]
    else:
        with store._writer() as conn:
            if fault == "cycle_stop":
                conn.execute(text("UPDATE protection_intents SET stop_price='901'"))
            elif fault == "cycle_dust":
                conn.execute(text("UPDATE testnet_cycles SET dust='0.001'"))
            elif fault == "entry_incomplete":
                conn.execute(
                    text(
                        "UPDATE order_intents SET exchange_cumulative_quantity='0.02' "
                        "WHERE side='BUY'"
                    )
                )
            elif fault == "orphan_protection":
                conn.execute(text("UPDATE protection_intents SET intent_id='orphan'"))
            elif fault == "incident":
                conn.execute(
                    text(
                        "INSERT INTO ledger_account_incidents VALUES "
                        "('account_history_missing',:at)"
                    ),
                    {"at": NOW},
                )
            elif fault == "receipt_table":
                conn.execute(text("CREATE TABLE ledger_account_migrations (singleton_id INTEGER)"))
    before = historical_rows(store)
    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(LegacyMigrationError):
            await migrate_legacy(store, reader, apply=True)
    assert historical_rows(store) == before
    with store.db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ledger_account_binding")).scalar_one() == 0
    store.db.dispose()


@pytest.mark.parametrize("apply", [False, True])
def test_cli_requires_explicit_apply_and_emits_only_diagnostic(
    database_url, monkeypatch, capsys, apply
):
    import sys

    from spotlab import testnet_cli

    store = legacy_store(database_url)
    venue = Venue()
    monkeypatch.setattr(testnet_cli, "load_credentials", lambda _: credentials())
    monkeypatch.setattr(
        testnet_cli,
        "LegacyTestnetReader",
        lambda _: LegacyTestnetReader(
            credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
        ),
    )
    args = ["testnet_cli", "migrate-legacy", "--database", database_url]
    if apply:
        args.append("--confirm-testnet")
    monkeypatch.setattr(sys, "argv", args)
    assert testnet_cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["applied"] is apply
    assert str(UID) not in json.dumps(output)
    assert "legacy-entry" not in json.dumps(output)
    if apply:
        store.require_account_binding(TESTNET_ACCOUNT_VENUE, UID)
    else:
        assert store.account_binding_reason() == "migration_required"
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["SUBMITTING", "UNKNOWN", "NEW", "PARTIALLY_FILLED"])
async def test_unsettled_local_order_blocks_before_network(database_url, state):
    store = legacy_store(database_url)
    with store._writer() as conn:
        conn.execute(text("UPDATE order_intents SET state=:state"), {"state": state})
    venue = Venue()
    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(venue), clock_ms=lambda: NOW
    ) as reader:
        with pytest.raises(LegacyMigrationError, match="migration_unsettled"):
            await migrate_legacy(store, reader, apply=True)
    assert venue.seen == []
    assert store.list_records()[0].state.value == state
    store.db.dispose()


@pytest.mark.asyncio
async def test_reader_signs_exact_get_payload_and_rejects_other_routes():
    import hashlib
    import hmac

    seen = []

    def handle(request):
        seen.append(request)
        query, signature = request.url.query.rsplit(b"&signature=", 1)
        assert hmac.new(b"fixture-secret", query, hashlib.sha256).hexdigest() == signature.decode()
        assert b"origClientOrderId=a%2Fb%20c" in query
        return httpx.Response(200, json={})

    async with LegacyTestnetReader(
        credentials(), mock_transport=httpx.MockTransport(handle), clock_ms=lambda: NOW
    ) as reader:
        await reader._get("/api/v3/order", {"origClientOrderId": "a/b c"})
        for path in ("https://example.com", "/api/v3/order/oco", "/api/v3/userDataStream"):
            with pytest.raises(LegacyMigrationError):
                await reader._get(path)
    assert len(seen) == 1
    assert seen[0].method == "GET"
