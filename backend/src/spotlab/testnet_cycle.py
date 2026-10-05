"""One bounded, durable Testnet entry followed by exchange-side protection.

This controller does not select trades, run production orders, or authorize a
second entry. A restarted manager handles existing exposure only.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import text

from spotlab.orders import (
    TERMINAL_STATES,
    ExchangeUpdate,
    OrderConflict,
    OrderError,
    OrderLifecycle,
    OrderRecord,
    OrderRequest,
    OrderState,
    SubmissionPrevented,
    deterministic_client_order_id,
)
from spotlab.protection import ProtectionLifecycle, ProtectionRequest
from spotlab.testnet import BinanceTestnetTransport
from spotlab.venue_filters import round_protection_quantity

D = Decimal
CLOSED_CYCLES = {"EXITED", "NO_FILL", "ABORTED"}


@dataclass(frozen=True)
class CyclePlan:
    cycle_id: str
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    target_price: Decimal
    stop_price: Decimal
    max_notional: Decimal
    entry_deadline_ms: int

    @property
    def scope(self) -> str:
        return deterministic_client_order_id(self.cycle_id, "cycle")

    @property
    def entry_id(self) -> str:
        return self.scope + ":entry"

    @property
    def protection_id(self) -> str:
        return self.scope + ":protection"

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.cycle_id):
            raise OrderError("cycle ID must contain 1-64 letters, digits, underscores or hyphens")
        if self.symbol not in {"BTCUSDT", "SOLUSDT"}:
            raise OrderError("unsupported Testnet cycle symbol")
        for value in (
            self.quantity,
            self.entry_price,
            self.target_price,
            self.stop_price,
            self.max_notional,
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise OrderError("cycle numbers must be finite positive Decimals")
        if not self.stop_price < self.entry_price < self.target_price:
            raise OrderError("cycle requires stop < entry < target")
        if self.max_notional > 100 or self.quantity * self.entry_price > self.max_notional:
            raise OrderError("cycle exceeds explicit notional cap (maximum 100 virtual USDT)")
        if isinstance(self.entry_deadline_ms, bool) or not isinstance(self.entry_deadline_ms, int):
            raise OrderError("entry deadline must be integer milliseconds")

    def entry(self) -> OrderRequest:
        return OrderRequest(
            self.entry_id,
            deterministic_client_order_id(self.entry_id, "entry"),
            self.symbol,
            "BUY",
            self.quantity,
            self.entry_price,
            "USDT",
            self.scope,
        )

    def protection(self, quantity: Decimal) -> ProtectionRequest:
        return ProtectionRequest(
            OrderRequest(
                self.protection_id,
                deterministic_client_order_id(self.protection_id, "oco"),
                self.symbol,
                "SELL",
                quantity,
                self.target_price,
                "USDT",
                self.scope,
                "OCO",
            ),
            self.stop_price,
        )

    def serialize(self) -> str:
        return json.dumps(asdict(self), default=str, sort_keys=True)

    @classmethod
    def deserialize(cls, payload: str) -> CyclePlan:
        data = json.loads(payload)
        for name in ("quantity", "entry_price", "target_price", "stop_price", "max_notional"):
            data[name] = D(data[name])
        plan = cls(**data)
        plan.validate()
        return plan


class _EntryTransport:
    """Carry a persisted, bounded entry deadline into the final transport send."""

    def __init__(
        self, transport: BinanceTestnetTransport, plan: CyclePlan, before_send: Callable[[], None]
    ) -> None:
        self.transport = transport
        self.plan = plan
        self.before_send = before_send

    async def submit(self, order: OrderRequest) -> ExchangeUpdate:
        return await self.transport.submit(
            order,
            deadline_ms=self.plan.entry_deadline_ms,
            reserve_protection=True,
            before_send=self.before_send,
            protection_target=self.plan.target_price,
            protection_stop=self.plan.stop_price,
        )

    async def query(self, client_order_id: str) -> ExchangeUpdate:
        return await self.transport.query(client_order_id)

    async def cancel(self, client_order_id: str) -> ExchangeUpdate:
        return await self.transport.cancel(client_order_id)


class TestnetCycle:
    def __init__(
        self,
        database_url: str,
        transport: BinanceTestnetTransport,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.transport = transport
        self.orders = OrderLifecycle(database_url, transport)
        self.store = self.orders.store
        self.protection = ProtectionLifecycle(self.store, transport)
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        with self.store._writer() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS testnet_cycles ("
                    "cycle_id VARCHAR(64) PRIMARY KEY,plan TEXT NOT NULL,"
                    "state VARCHAR(40) NOT NULL,"
                    "protection_quantity VARCHAR(128),dust VARCHAR(128),reason VARCHAR(80),"
                    "stop_entry INTEGER NOT NULL DEFAULT 0,"
                    "updated_at BIGINT NOT NULL)"
                )
            )

    def _plan(self, cycle_id: str) -> CyclePlan:
        with self.store.db.connect() as conn:
            payload = conn.execute(
                text("SELECT plan FROM testnet_cycles WHERE cycle_id=:id"), {"id": cycle_id}
            ).scalar_one()
        plan = CyclePlan.deserialize(payload)
        if plan.symbol != self.transport.symbol:
            raise OrderError("cycle symbol differs from bound transport")
        return plan

    def _persist(self, plan: CyclePlan) -> None:
        with self.store._writer() as conn:
            row = conn.execute(
                text("SELECT plan FROM testnet_cycles WHERE cycle_id=:id"), {"id": plan.cycle_id}
            ).scalar_one_or_none()
            if row is not None:
                if row != plan.serialize():
                    raise OrderConflict("cycle terms are immutable")
                return
            active = conn.execute(
                text(
                    "SELECT cycle_id FROM testnet_cycles "
                    "WHERE state NOT IN ('EXITED','NO_FILL','ABORTED')"
                )
            ).first()
            if active is not None:
                raise OrderConflict("another unfinished Testnet cycle requires management")
            self.store._create_or_get(conn, plan.entry())
            conn.execute(
                text(
                    "INSERT INTO testnet_cycles (cycle_id,plan,state,updated_at) "
                    "VALUES (:id,:plan,'PLANNED',:at)"
                ),
                {"id": plan.cycle_id, "plan": plan.serialize(), "at": self.clock_ms()},
            )

    def _set(self, cycle_id: str, state: str, reason: str | None = None) -> None:
        with self.store._writer() as conn:
            conn.execute(
                text(
                    "UPDATE testnet_cycles SET state=:state,reason=:reason,updated_at=:at "
                    "WHERE cycle_id=:id"
                ),
                {"id": cycle_id, "state": state, "reason": reason, "at": self.clock_ms()},
            )

    def _record(self, intent_id: str) -> OrderRecord | None:
        try:
            return self.store.get(intent_id)
        except KeyError:
            return None

    def status(self, cycle_id: str) -> dict[str, object]:
        plan = self._plan(cycle_id)
        with self.store.db.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM testnet_cycles WHERE cycle_id=:id"), {"id": cycle_id}
                )
                .mappings()
                .one()
            )
        owned = self.store.owned_inventory(plan.scope, plan.symbol)
        entry = self._record(plan.entry_id)
        exit_order = self._record(plan.protection_id)
        # Cached observations expire quickly. Reservation is never proof of coverage.
        recent = 0 <= self.clock_ms() - row["updated_at"] <= 5_000
        covered = (
            row["state"] == "PROTECTED"
            and recent
            and exit_order is not None
            and exit_order.state is OrderState.NEW
            and exit_order.error is None
        )
        amount = D(row["protection_quantity"] or "0") if covered else D(0)
        return {
            "cycle_id": cycle_id,
            "mode": "testnet",
            "production_enabled": False,
            "state": (
                "PROTECTION_UNVERIFIED"
                if row["state"] == "PROTECTED" and not covered
                else row["state"]
            ),
            "reason": row["reason"],
            "stop_entry_requested": bool(row["stop_entry"]),
            "entry_state": None if entry is None else entry.state.value,
            "protection_state": None if exit_order is None else exit_order.state.value,
            "owned_quantity": str(owned),
            "dust_quantity": row["dust"] or "0",
            "unprotected_quantity": str(max(D(0), owned - amount)),
            "coverage_observation_fresh": covered,
            "observed_at_ms": row["updated_at"],
        }

    def _authorize_send(self, plan: CyclePlan) -> None:
        # The transport invokes this after all awaited preflight work. The
        # writer serializes this send decision against a durable stop request.
        with self.store._writer() as conn:
            row = (
                conn.execute(
                    text("SELECT stop_entry,state FROM testnet_cycles WHERE cycle_id=:id"),
                    {"id": plan.cycle_id},
                )
                .mappings()
                .one()
            )
            if (
                row["stop_entry"]
                or row["state"] in CLOSED_CYCLES
                or self.clock_ms() >= plan.entry_deadline_ms
            ):
                raise SubmissionPrevented("entry stopped before send")

    async def start(self, plan: CyclePlan) -> dict[str, object]:
        """Explicit new entry only. Caller must obtain Testnet confirmation first."""
        plan.validate()
        if plan.symbol != self.transport.symbol:
            raise OrderError("cycle symbol differs from bound transport")
        if not 0 < plan.entry_deadline_ms - self.clock_ms() <= 600_000:
            raise OrderError("entry authority must expire within ten minutes")
        self._persist(plan)
        current = self.status(plan.cycle_id)
        if current["state"] in CLOSED_CYCLES:
            return current
        if current["stop_entry_requested"]:
            return await self.advance(plan.cycle_id, allow_mutations=True)
        entry = self._record(plan.entry_id)
        if entry is None or entry.state is OrderState.INTENDED:
            try:
                metadata = await self.transport.prepare_submission(
                    plan.entry(),
                    reserve_protection=True,
                    protection_target=plan.target_price,
                    protection_stop=plan.stop_price,
                )
                if self.clock_ms() >= plan.entry_deadline_ms:
                    raise OrderError("entry authority expired during preflight")
                self.orders.transport = _EntryTransport(
                    self.transport, plan, lambda: self._authorize_send(plan)
                )
                await self.orders.submit(plan.entry(), metadata.filters)
            except Exception as error:
                self._set(plan.cycle_id, "BLOCKED", type(error).__name__)
                return self.status(plan.cycle_id)
        return await self.advance(plan.cycle_id, allow_mutations=True)

    async def advance(
        self, cycle_id: str, *, allow_mutations: bool = False, stop_entry: bool = False
    ) -> dict[str, object]:
        """Reconcile existing intents; optional management never creates a BUY."""
        plan = self._plan(cycle_id)
        with self.store._writer() as conn:
            if stop_entry:
                conn.execute(
                    text("UPDATE testnet_cycles SET stop_entry=1 WHERE cycle_id=:id"),
                    {"id": cycle_id},
                )
            stop_entry = bool(
                conn.execute(
                    text("SELECT stop_entry FROM testnet_cycles WHERE cycle_id=:id"),
                    {"id": cycle_id},
                ).scalar_one()
            )
        try:
            entry = self._record(plan.entry_id)
            if entry is None:
                self._set(cycle_id, "BLOCKED", "entry_record_missing")
                return self.status(cycle_id)
            if entry.state is OrderState.INTENDED:
                if stop_entry:
                    with self.store._writer() as conn:
                        row = self.store._locked_row(conn, plan.entry_id)
                        if row is not None and row["state"] == OrderState.INTENDED.value:
                            # Serialized against claim_submission: an aborted intent
                            # cannot race with a delayed start and reach the venue.
                            conn.execute(
                                text(
                                    "UPDATE order_intents SET state='REJECTED',"
                                    "error='abandoned_before_send' WHERE intent_id=:id"
                                ),
                                {"id": plan.entry_id},
                            )
                            conn.execute(
                                text(
                                    "UPDATE testnet_cycles SET state='ABORTED',"
                                    "reason='entry_not_sent',updated_at=:at WHERE cycle_id=:id"
                                ),
                                {"id": cycle_id, "at": self.clock_ms()},
                            )
                    return self.status(cycle_id)
                self._set(cycle_id, "BLOCKED", "entry_not_sent")
                return self.status(cycle_id)
            if (
                entry.state is OrderState.REJECTED
                and entry.exchange_order_id is None
                and entry.error in {"abandoned_before_send", "SubmissionPrevented"}
            ):
                self._set(cycle_id, "ABORTED", "entry_not_sent")
                return self.status(cycle_id)
            entry = await self.orders.reconcile(plan.entry_id)
            if entry.state not in TERMINAL_STATES:
                cancel_needed = (
                    entry.exchange_cumulative_quantity > 0
                    or stop_entry
                    or self.clock_ms() >= plan.entry_deadline_ms
                )
                if cancel_needed and allow_mutations and not entry.cancel_requested:
                    await self.orders.cancel(plan.entry_id)
                    entry = await self.orders.reconcile(plan.entry_id)
                if entry.state not in TERMINAL_STATES:
                    self._set(
                        cycle_id,
                        "ENTRY_SETTLING" if cancel_needed else "ENTRY_OPEN",
                        "cancel_pending_query_only" if entry.cancel_requested else None,
                    )
                    return self.status(cycle_id)
            if entry.confirmed_quantity != entry.exchange_cumulative_quantity:
                self._set(cycle_id, "BLOCKED", "entry_trade_details_incomplete")
                return self.status(cycle_id)
            if not entry.confirmed_quantity:
                self._set(cycle_id, "NO_FILL")
                return self.status(cycle_id)
            exit_order = self._record(plan.protection_id)
            if exit_order is None or exit_order.state is OrderState.INTENDED:
                if not allow_mutations:
                    self._set(cycle_id, "UNPROTECTED", "management_confirmation_required")
                    return self.status(cycle_id)
                metadata = await self.transport.exchange_info()
                # Serialize sizing independently from reservation. Concurrent
                # managers reuse the exact persisted size even after reservation.
                with self.store._writer() as conn:
                    prepared = conn.execute(
                        text("SELECT protection_quantity FROM testnet_cycles WHERE cycle_id=:id"),
                        {"id": cycle_id},
                    ).scalar_one()
                    if prepared is None:
                        available = self.store._available_inventory(conn, plan.scope, plan.symbol)
                        quantity = round_protection_quantity(available, metadata)
                        conn.execute(
                            text(
                                "UPDATE testnet_cycles SET protection_quantity=:qty,dust=:dust "
                                "WHERE cycle_id=:id"
                            ),
                            {
                                "id": cycle_id,
                                "qty": str(quantity),
                                "dust": str(available - quantity),
                            },
                        )
                    else:
                        quantity = D(prepared)
                if quantity <= 0:
                    self._set(cycle_id, "UNPROTECTED", "below_protection_lot")
                    return self.status(cycle_id)
                await self.protection.submit(plan.protection(quantity))
            observation = await self.protection.reconcile(plan.protection_id)
            owned = self.store.owned_inventory(plan.scope, plan.symbol)
            if owned == 0:
                self._set(cycle_id, "EXITED")
            elif observation.both_legs_open:
                self._set(cycle_id, "PROTECTED")
            elif observation.order.state in TERMINAL_STATES:
                self._set(cycle_id, "UNPROTECTED", "protective_list_terminal_with_inventory")
            else:
                self._set(cycle_id, "EXIT_IN_PROGRESS", "coverage_requires_observation")
        except Exception as error:
            self._set(cycle_id, "BLOCKED", type(error).__name__)
        return self.status(cycle_id)
