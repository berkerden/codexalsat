from decimal import Decimal as D
from types import SimpleNamespace
from typing import cast

import pytest
from backend.tests.test_orders import database_url as database_url

from spotlab.orders import ConfirmedFill, ExchangeUpdate, OrderRequest, OrderState, OrderStore
from spotlab.protection import ProtectionLifecycle, ProtectionRequest
from spotlab.startup_audit import audit_testnet_startup
from spotlab.testnet import AccountOpenOrder, BinanceTestnetTransport


def request(intent: str = "one") -> OrderRequest:
    return OrderRequest(
        intent,
        f"spotlab-{intent:0>24}",
        "BTCUSDT",
        "BUY",
        D("1"),
        D("100"),
        "USDT",
        f"scope-{intent}",
    )


def remote(order: OrderRequest, order_id: int = 7, **changes: object) -> AccountOpenOrder:
    values: dict[str, object] = {
        "symbol": order.symbol,
        "order_id": order_id,
        "client_order_id": order.client_order_id,
        "side": order.side,
        "order_type": "LIMIT",
        "original_quantity": order.quantity,
        "executed_quantity": D(0),
        "order_list_id": -1,
        "price": order.price,
        "stop_price": D(0),
        "time_in_force": "GTC",
        "status": "NEW",
    }
    values.update(changes)
    return AccountOpenOrder(**values)  # type: ignore[arg-type]


class AuditVenue:
    symbol = "BTCUSDT"

    def __init__(self, orders: tuple[AccountOpenOrder, ...] = ()) -> None:
        self.orders = orders

    async def account_wide_snapshot(self):
        return SimpleNamespace(
            account=SimpleNamespace(
                can_trade=True,
                balances=(SimpleNamespace(asset="USDT", free=D("1000"), locked=D(0)),),
                standard_commission=None,
                tax_commission=None,
                special_commission=None,
                discount=None,
                observed_at_ms=1_800_000_000_000,
            ),
            open_orders=self.orders,
        )


@pytest.mark.asyncio
async def test_candidate_exclusion_manual_order_and_remote_collision(database_url: str) -> None:
    store = OrderStore(database_url)
    candidate = request()
    store.create_or_get(candidate)
    manual = remote(
        candidate,
        client_order_id="manual-order",
        symbol="ETHUSDT",
        side="SELL",
    )
    clean = await audit_testnet_startup(
        store,
        cast(BinanceTestnetTransport, AuditVenue((manual,))),
        candidate=candidate,
    )
    assert not clean.blocked
    assert clean.untracked_other_orders == 1

    collision = await audit_testnet_startup(
        store,
        cast(BinanceTestnetTransport, AuditVenue((remote(candidate),))),
        candidate=candidate,
    )
    assert "candidate_remote_open" in collision.reason_codes


@pytest.mark.asyncio
async def test_read_only_audit_preserves_submitting_and_rejection_reason_is_allowlisted(
    database_url: str,
) -> None:
    store = OrderStore(database_url)
    candidate = request()
    store.create_or_get(candidate)
    assert store.claim_submission(candidate.intent_id)
    store.db.dispose()

    audit_store = OrderStore(database_url, recover_submitting=False)
    result = await audit_testnet_startup(
        audit_store,
        cast(BinanceTestnetTransport, AuditVenue()),
        candidate=candidate,
    )
    assert not result.blocked
    assert audit_store.get(candidate.intent_id).state is OrderState.SUBMITTING

    rejected = audit_store.reject_unsent(candidate.intent_id, "fixturesecretvalue")
    assert rejected.error == "SubmissionPrevented"


@pytest.mark.asyncio
async def test_suspicious_orphan_duplicate_and_terminal_incomplete_block(database_url: str) -> None:
    store = OrderStore(database_url)
    local = request("local")
    store.create_or_get(local)
    with store._writer() as conn:
        conn.exec_driver_sql(
            "UPDATE order_intents SET state='CANCELED',exchange_cumulative_quantity='0.5' "
            "WHERE intent_id='local'"
        )
    orphan = remote(
        local,
        client_order_id="entry-0123456789abcdef01234567",
        order_id=99,
        symbol="ETHUSDT",
    )
    result = await audit_testnet_startup(
        store,
        cast(BinanceTestnetTransport, AuditVenue((orphan, orphan))),
    )
    assert {
        "duplicate_remote_client_id",
        "duplicate_remote_order_id",
        "suspicious_orphan_order",
        "terminal_trades_incomplete",
    } <= set(result.reason_codes)


@pytest.mark.asyncio
async def test_terminal_known_oco_child_and_residual_inventory_block(database_url: str) -> None:
    store = OrderStore(database_url)
    buy = request("filled")
    store.create_or_get(buy)
    store.apply_update(
        buy.intent_id,
        ExchangeUpdate(
            buy.client_order_id,
            "BTCUSDT:1",
            OrderState.FILLED,
            D("1"),
            (ConfirmedFill("BTCUSDT:1", D("1"), D("100"), D(0), ""),),
        ),
    )
    parent = OrderRequest(
        "exit",
        "oco-0123456789abcdef01234567",
        "BTCUSDT",
        "SELL",
        D("1"),
        D("110"),
        "USDT",
        buy.inventory_scope,
        "OCO",
    )
    protection = ProtectionRequest(parent, D("90"))
    lifecycle = ProtectionLifecycle(store, cast(BinanceTestnetTransport, AuditVenue()))
    lifecycle._persist_payload(protection)
    store.create_or_get(parent)
    store.apply_update(
        parent.intent_id,
        ExchangeUpdate(parent.client_order_id, "oco:BTCUSDT:9", OrderState.CANCELED, D(0)),
    )
    child = AccountOpenOrder(
        "BTCUSDT",
        2,
        protection.above_id,
        "SELL",
        "LIMIT_MAKER",
        D("1"),
        D(0),
        9,
        D("110"),
        D(0),
        "GTC",
        "NEW",
    )
    result = await audit_testnet_startup(
        store,
        cast(BinanceTestnetTransport, AuditVenue((child,))),
    )
    assert "terminal_order_still_open" in result.reason_codes
    assert "residual_owned_inventory" in result.reason_codes
