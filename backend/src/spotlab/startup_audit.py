"""Read-only, fail-closed Testnet startup audit for a new BUY entry."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from spotlab.orders import (
    TERMINAL_STATES,
    OrderRecord,
    OrderRequest,
    OrderState,
    OrderStore,
    deterministic_client_order_id,
)
from spotlab.testnet import (
    AccountOpenOrder,
    AccountSnapshot,
    BinanceTestnetTransport,
    CapacityOrderMarker,
)

ZERO = Decimal(0)
_OWNED_NAMESPACE = re.compile(r"^(?:entry|target|stop|oco|spotlab)-[0-9a-f]{24}$")
_ORDER_ID = re.compile(r"^([^:]+):(\d+)$")
_OCO_ID = re.compile(r"^oco:([^:]+):(\d+)$")


@dataclass(frozen=True)
class _ExpectedOpenOrder:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    stop_price: Decimal
    time_in_force: str
    order_id: int | None
    order_list_id: int
    terminal: bool
    candidate: bool

    def matches(self, observed: AccountOpenOrder) -> bool:
        return (
            observed.client_order_id == self.client_order_id
            and observed.symbol == self.symbol
            and observed.side == self.side
            and observed.order_type == self.order_type
            and observed.original_quantity == self.quantity
            and observed.price == self.price
            and observed.stop_price == self.stop_price
            and observed.time_in_force == self.time_in_force
            and (self.order_id is None or observed.order_id == self.order_id)
            and (self.order_list_id == -1 or observed.order_list_id == self.order_list_id)
        )


AccountMarker = tuple[object, ...]
LedgerMarker = tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class StartupAuditResult:
    reason_counts: tuple[tuple[str, int], ...]
    local_intents: int
    remote_open_orders: int
    untracked_other_orders: int
    account_marker: AccountMarker
    ledger_marker: LedgerMarker
    open_order_marker: CapacityOrderMarker
    observed_at_ms: int
    account_binding_verified: bool = False
    remote_snapshot_observed: bool = True

    @property
    def blocked(self) -> bool:
        return bool(self.reason_counts)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(reason for reason, _count in self.reason_counts)

    def diagnostic(self) -> dict[str, object]:
        return {
            "mode": "testnet",
            "production_enabled": False,
            "status": "blocked" if self.blocked else "passed",
            "reason_codes": list(self.reason_codes),
            "reason_counts": dict(self.reason_counts),
            "local_intent_count": self.local_intents,
            "remote_open_order_count": self.remote_open_orders
            if self.remote_snapshot_observed
            else None,
            "untracked_other_order_count": self.untracked_other_orders
            if self.remote_snapshot_observed
            else None,
            "observed_at_ms": self.observed_at_ms if self.remote_snapshot_observed else None,
            "remote_snapshot_observed": self.remote_snapshot_observed,
            "scope": "account_open_orders_vs_local_ledger",
            "account_binding_verified": self.account_binding_verified,
            "reset_resolution_verified": False,
        }


def _account_marker(snapshot: AccountSnapshot) -> AccountMarker:
    balances = tuple(sorted((item.asset, item.free, item.locked) for item in snapshot.balances))
    discount: object = snapshot.discount
    return (
        snapshot.can_trade,
        balances,
        snapshot.standard_commission,
        snapshot.tax_commission,
        snapshot.special_commission,
        discount,
    )


def _exchange_order_id(value: str | None, symbol: str) -> tuple[int | None, bool]:
    if value is None:
        return None, True
    match = _ORDER_ID.fullmatch(value)
    if match is None or match.group(1) != symbol:
        return None, False
    return int(match.group(2)), True


def _exchange_list_id(value: str | None, symbol: str) -> tuple[int, bool]:
    if value is None:
        return -1, True
    match = _OCO_ID.fullmatch(value)
    if match is None or match.group(1) != symbol:
        return -1, False
    return int(match.group(2)), True


def local_ledger_marker(conn: Connection) -> LedgerMarker:
    """Return all order evidence while the caller holds the ledger writer lock."""

    order_rows = conn.execute(text("SELECT * FROM order_intents ORDER BY intent_id")).mappings()
    markers: list[tuple[object, ...]] = [("order", *tuple(row.values())) for row in order_rows]
    fill_rows = conn.execute(
        text("SELECT * FROM order_fills ORDER BY exchange_trade_id")
    ).mappings()
    markers.extend(("fill", *tuple(row.values())) for row in fill_rows)
    if inspect(conn).has_table("protection_intents"):
        protection_rows = conn.execute(
            text("SELECT * FROM protection_intents ORDER BY intent_id")
        ).mappings()
        markers.extend(("protection", *tuple(row.values())) for row in protection_rows)
    for table in ("ledger_account_binding", "ledger_account_incidents"):
        if inspect(conn).has_table(table):
            rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings()
            markers.extend((table, *tuple(row.values())) for row in rows)
    return tuple(markers)


def _local_snapshot(
    store: OrderStore,
) -> tuple[
    tuple[OrderRecord, ...],
    dict[str, tuple[Decimal, str, str]],
    dict[tuple[str, str], Decimal],
    LedgerMarker,
]:
    with store._writer() as conn:
        order_rows = tuple(
            conn.execute(text("SELECT * FROM order_intents ORDER BY created_at,intent_id"))
            .mappings()
            .all()
        )
        records = tuple(store._record_from_row(row) for row in order_rows)
        fill_rows = tuple(
            conn.execute(text("SELECT * FROM order_fills ORDER BY exchange_trade_id"))
            .mappings()
            .all()
        )
        payloads: dict[str, tuple[Decimal, str, str]] = {}
        if inspect(conn).has_table("protection_intents"):
            protection_rows = tuple(
                conn.execute(text("SELECT * FROM protection_intents ORDER BY intent_id"))
                .mappings()
                .all()
            )
            payloads = {
                row["intent_id"]: (Decimal(row["stop_price"]), row["above_id"], row["below_id"])
                for row in protection_rows
            }
        marker = local_ledger_marker(conn)

    base_fees: dict[str, Decimal] = {}
    for row in fill_rows:
        base_fees[row["intent_id"]] = base_fees.get(row["intent_id"], ZERO) + Decimal(
            row["fee_base"]
        )
    inventory: dict[tuple[str, str], Decimal] = {}
    for record in records:
        key = (record.request.inventory_scope, record.request.symbol)
        signed = record.confirmed_quantity - base_fees.get(record.request.intent_id, ZERO)
        if record.request.side == "SELL":
            signed = -(record.confirmed_quantity + base_fees.get(record.request.intent_id, ZERO))
        inventory[key] = inventory.get(key, ZERO) + signed
    return records, payloads, inventory, marker


def _expected_orders(
    records: tuple[OrderRecord, ...],
    payloads: dict[str, tuple[Decimal, str, str]],
    candidate: OrderRequest | None,
    reasons: Counter[str],
) -> tuple[
    dict[str, _ExpectedOpenOrder],
    dict[tuple[str, int], _ExpectedOpenOrder],
    dict[tuple[str, int], tuple[_ExpectedOpenOrder, _ExpectedOpenOrder]],
    set[str],
]:
    by_client: dict[str, _ExpectedOpenOrder] = {}
    by_order: dict[tuple[str, int], _ExpectedOpenOrder] = {}
    by_list: dict[tuple[str, int], tuple[_ExpectedOpenOrder, _ExpectedOpenOrder]] = {}
    known_clients: set[str] = set()
    candidate_seen = False
    for record in records:
        request = record.request
        known_clients.add(request.client_order_id)
        is_candidate = (
            candidate is not None
            and request == candidate
            and record.state in {OrderState.INTENDED, OrderState.SUBMITTING}
        )
        candidate_seen = candidate_seen or is_candidate
        if record.state not in TERMINAL_STATES and not is_candidate:
            reasons["needs_reconciliation"] += 1
        if (
            record.state in TERMINAL_STATES
            and record.exchange_cumulative_quantity != record.confirmed_quantity
        ):
            reasons["terminal_trades_incomplete"] += 1
        if request.order_type == "LIMIT":
            order_id, valid = _exchange_order_id(record.exchange_order_id, request.symbol)
            if not valid:
                reasons["local_exchange_identity_invalid"] += 1
            expected = _ExpectedOpenOrder(
                request.client_order_id,
                request.symbol,
                request.side,
                "LIMIT",
                request.quantity,
                request.price,
                ZERO,
                "GTC",
                order_id,
                -1,
                record.state in TERMINAL_STATES,
                is_candidate,
            )
            by_client[expected.client_order_id] = expected
            if order_id is not None:
                by_order[(request.symbol, order_id)] = expected
            continue
        payload = payloads.get(request.intent_id)
        derived_above = deterministic_client_order_id(request.intent_id, "target")
        derived_below = deterministic_client_order_id(request.intent_id, "stop")
        known_clients.update((derived_above, derived_below))
        if payload is None:
            reasons["oco_metadata_missing"] += 1
            continue
        stop_price, above_id, below_id = payload
        if (
            above_id != derived_above
            or below_id != derived_below
            or not stop_price.is_finite()
            or stop_price <= ZERO
            or stop_price >= request.price
        ):
            reasons["oco_metadata_invalid"] += 1
        above_id, below_id = derived_above, derived_below
        list_id, valid = _exchange_list_id(record.exchange_order_id, request.symbol)
        if not valid:
            reasons["local_exchange_identity_invalid"] += 1
        above = _ExpectedOpenOrder(
            above_id,
            request.symbol,
            "SELL",
            "LIMIT_MAKER",
            request.quantity,
            request.price,
            ZERO,
            "GTC",
            None,
            list_id,
            record.state in TERMINAL_STATES,
            False,
        )
        below = _ExpectedOpenOrder(
            below_id,
            request.symbol,
            "SELL",
            "STOP_LOSS",
            request.quantity,
            ZERO,
            stop_price,
            "GTC",
            None,
            list_id,
            record.state in TERMINAL_STATES,
            False,
        )
        known_clients.update((above_id, below_id))
        by_client[above_id] = above
        by_client[below_id] = below
        if list_id != -1:
            by_list[(request.symbol, list_id)] = (above, below)
    if candidate is not None and not candidate_seen:
        expected_candidate = _ExpectedOpenOrder(
            candidate.client_order_id,
            candidate.symbol,
            candidate.side,
            candidate.order_type,
            candidate.quantity,
            candidate.price,
            ZERO,
            "GTC",
            None,
            -1,
            False,
            True,
        )
        known_clients.add(candidate.client_order_id)
        by_client[candidate.client_order_id] = expected_candidate
    return by_client, by_order, by_list, known_clients


async def audit_testnet_startup(
    store: OrderStore,
    transport: BinanceTestnetTransport,
    *,
    candidate: OrderRequest | None = None,
    expected_account_marker: AccountMarker | None = None,
) -> StartupAuditResult:
    """Compare the complete local ledger with one account-wide remote snapshot."""

    verified = False
    attach = getattr(transport, "attach_order_store", None)
    if attach is not None:
        attach(store)
        reason = await transport.account_binding_reason()
        if reason is not None:
            records, _payloads, _inventory, ledger_marker = _local_snapshot(store)
            return StartupAuditResult(
                ((reason, 1),),
                len(records),
                0,
                0,
                (),
                ledger_marker,
                (),
                store._millis(),
                account_binding_verified=reason == "account_history_missing",
                remote_snapshot_observed=False,
            )
        verified = True
    snapshot = await transport.account_wide_snapshot()
    reasons: Counter[str] = Counter()
    records, payloads, inventory, ledger_marker = _local_snapshot(store)
    by_client, by_order, by_list, known_clients = _expected_orders(
        records, payloads, candidate, reasons
    )

    marker = _account_marker(snapshot.account)
    if not snapshot.account.can_trade:
        reasons["account_cannot_trade"] += 1
    if expected_account_marker is not None and marker != expected_account_marker:
        reasons["account_snapshot_changed"] += 1

    for owned in inventory.values():
        if owned > ZERO:
            reasons["residual_owned_inventory"] += 1
        elif owned < ZERO:
            reasons["local_inventory_invalid"] += 1

    client_keys: set[tuple[str, str]] = set()
    order_keys: set[tuple[str, int]] = set()
    untracked = 0
    for remote in snapshot.open_orders:
        client_key = (remote.symbol, remote.client_order_id)
        order_key = (remote.symbol, remote.order_id)
        if client_key in client_keys:
            reasons["duplicate_remote_client_id"] += 1
        if order_key in order_keys:
            reasons["duplicate_remote_order_id"] += 1
        client_keys.add(client_key)
        order_keys.add(order_key)

        expected = by_client.get(remote.client_order_id)
        order_expected = by_order.get(order_key)
        list_expected = by_list.get((remote.symbol, remote.order_list_id))
        if expected is None and order_expected is not None:
            expected = order_expected
        if expected is None and list_expected is not None:
            matches = [
                item for item in list_expected if item.client_order_id == remote.client_order_id
            ]
            expected = matches[0] if len(matches) == 1 else list_expected[0]
        if expected is not None:
            if not expected.matches(remote):
                reasons["known_order_identity_mismatch"] += 1
            elif expected.candidate:
                reasons["candidate_remote_open"] += 1
            elif expected.terminal:
                reasons["terminal_order_still_open"] += 1
            continue
        if remote.client_order_id in known_clients:
            reasons["known_order_identity_mismatch"] += 1
        elif _OWNED_NAMESPACE.fullmatch(remote.client_order_id):
            reasons["suspicious_orphan_order"] += 1
        else:
            untracked += 1

    return StartupAuditResult(
        tuple(sorted(reasons.items())),
        len(records),
        len(snapshot.open_orders),
        untracked,
        marker,
        ledger_marker,
        tuple(sorted(item.capacity_key for item in snapshot.open_orders)),
        snapshot.account.observed_at_ms,
        account_binding_verified=verified,
    )
