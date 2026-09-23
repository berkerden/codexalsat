"""Durable spot-order intents and exchange reconciliation.

This module deliberately contains no exchange adapter.  It persists an intent
before an injected transport is called and never retries an ambiguous submit.
All quantities and money values cross the boundary as :class:`Decimal`.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, RowMapping

D = Decimal
ZERO = D(0)
# Serializes all PostgreSQL order-ledger writers. A fixed transaction advisory
# lock is intentionally global: reservations span multiple intent rows, so a
# per-intent FOR UPDATE lock cannot protect two new SELL intents in one scope.
POSTGRES_WRITER_LOCK_ID = 7_311_735_331


class OrderState(StrEnum):
    INTENDED = "INTENDED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}


class OrderError(ValueError):
    """Base error for invalid local order operations."""


class OrderConflict(OrderError):
    """An identifier was reused with different immutable order data."""


class OrderInvariantError(OrderError):
    """Exchange data conflicts with the durable order ledger."""


class InsufficientInventory(OrderError):
    """A SELL intent would reserve more than confirmed bot inventory."""


class RetryableTransportError(RuntimeError):
    """A timeout or server-side transport result with an ambiguous outcome."""


def _decimal(value: Decimal, name: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < 0 or (not allow_zero and value == 0):
        comparison = "non-negative" if allow_zero else "positive"
        raise OrderError(f"{name} must be finite and {comparison}")
    return value


@dataclass(frozen=True)
class ExchangeFilters:
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal
    max_notional: Decimal | None = None


def validate_order_filters(
    quantity: Decimal, price: Decimal, filters: ExchangeFilters
) -> None:
    """Validate an order against a fresh exchange metadata snapshot."""
    quantity = _decimal(quantity, "quantity")
    price = _decimal(price, "price")
    tick = _decimal(filters.tick_size, "tick_size")
    step = _decimal(filters.step_size, "step_size")
    minimum = _decimal(filters.min_quantity, "min_quantity")
    maximum = _decimal(filters.max_quantity, "max_quantity")
    min_notional = _decimal(filters.min_notional, "min_notional")
    max_notional = (
        None
        if filters.max_notional is None
        else _decimal(filters.max_notional, "max_notional")
    )
    if minimum > maximum:
        raise OrderError("min_quantity exceeds max_quantity")
    if max_notional is not None and min_notional > max_notional:
        raise OrderError("min_notional exceeds max_notional")
    if quantity < minimum or quantity > maximum:
        raise OrderError("quantity is outside exchange limits")
    if quantity % step != 0:
        raise OrderError("quantity is not aligned to step_size")
    if price % tick != 0:
        raise OrderError("price is not aligned to tick_size")
    notional = quantity * price
    if notional < min_notional:
        raise OrderError("notional is below min_notional")
    if max_notional is not None and notional > max_notional:
        raise OrderError("notional exceeds max_notional")


@dataclass(frozen=True)
class OrderRequest:
    intent_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    quote_asset: str
    inventory_scope: str
    order_type: str = "LIMIT"

    def validate(self) -> None:
        for value, name in (
            (self.intent_id, "intent_id"),
            (self.client_order_id, "client_order_id"),
            (self.symbol, "symbol"),
            (self.quote_asset, "quote_asset"),
            (self.inventory_scope, "inventory_scope"),
        ):
            if not value or len(value) > 128:
                raise OrderError(f"{name} must contain 1 to 128 characters")
        if self.side not in {"BUY", "SELL"}:
            raise OrderError("side must be BUY or SELL")
        if self.order_type != "LIMIT":
            raise OrderError("only LIMIT intents are supported by this lifecycle core")
        _decimal(self.quantity, "quantity")
        _decimal(self.price, "price")


@dataclass(frozen=True)
class ConfirmedFill:
    trade_id: str
    quantity: Decimal
    quote_quantity: Decimal
    fee_quote: Decimal
    fee_asset: str


@dataclass(frozen=True)
class ExchangeUpdate:
    client_order_id: str
    exchange_order_id: str
    status: OrderState
    cumulative_quantity: Decimal
    fills: Sequence[ConfirmedFill] = ()


@dataclass(frozen=True)
class OrderRecord:
    request: OrderRequest
    state: OrderState
    exchange_order_id: str | None
    confirmed_quantity: Decimal
    confirmed_quote_quantity: Decimal
    confirmed_fee_quote: Decimal
    exchange_cumulative_quantity: Decimal
    cancel_requested: bool
    error: str | None


@runtime_checkable
class OrderTransport(Protocol):
    """Injected async boundary. Implementations may use testnet; none is bundled."""

    async def submit(self, order: OrderRequest) -> ExchangeUpdate: ...

    async def query(self, client_order_id: str) -> ExchangeUpdate: ...

    async def cancel(self, client_order_id: str) -> ExchangeUpdate: ...


def deterministic_client_order_id(intent_id: str, prefix: str = "spotlab") -> str:
    """Return a stable, compact client ID without leaking the full intent value."""
    if not intent_id:
        raise OrderError("intent_id is required")
    digest = hashlib.sha256(intent_id.encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"


class OrderStore:
    """Transactional SQL order store shared with the application's database URL."""

    def __init__(self, database_url: str) -> None:
        options: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.db: Engine = create_engine(database_url, **options)
        self._create_schema()
        # A process that died while awaiting submit cannot know whether the venue
        # accepted it. Query reconciliation is the sole safe recovery operation.
        with self._writer() as conn:
            conn.execute(
                text(
                    "UPDATE order_intents SET state=:unknown,error=:error,updated_at=:at "
                    "WHERE state=:submitting"
                ),
                {
                    "unknown": OrderState.UNKNOWN.value,
                    "submitting": OrderState.SUBMITTING.value,
                    "error": "process restarted during submission; exchange query required",
                    "at": self._millis(),
                },
            )

    @staticmethod
    def _millis() -> int:
        return int(time.time() * 1000)

    def _create_schema(self) -> None:
        with self.db.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS order_intents ("
                    "intent_id VARCHAR(128) PRIMARY KEY,"
                    "client_order_id VARCHAR(128) NOT NULL UNIQUE,"
                    "symbol VARCHAR(64) NOT NULL,side VARCHAR(4) NOT NULL,"
                    "order_type VARCHAR(16) NOT NULL,quantity VARCHAR(128) NOT NULL,"
                    "price VARCHAR(128) NOT NULL,quote_asset VARCHAR(32) NOT NULL,"
                    "inventory_scope VARCHAR(128) NOT NULL,state VARCHAR(32) NOT NULL,"
                    "exchange_order_id VARCHAR(128) UNIQUE,"
                    "confirmed_quantity VARCHAR(128) NOT NULL,"
                    "confirmed_quote_quantity VARCHAR(128) NOT NULL,"
                    "confirmed_fee_quote VARCHAR(128) NOT NULL,"
                    "exchange_cumulative_quantity VARCHAR(128) NOT NULL,"
                    "cancel_requested INTEGER NOT NULL,error TEXT,"
                    "created_at BIGINT NOT NULL,updated_at BIGINT NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS order_fills ("
                    "exchange_trade_id VARCHAR(128) PRIMARY KEY,"
                    "intent_id VARCHAR(128) NOT NULL,quantity VARCHAR(128) NOT NULL,"
                    "quote_quantity VARCHAR(128) NOT NULL,fee_quote VARCHAR(128) NOT NULL,"
                    "fee_asset VARCHAR(32) NOT NULL,FOREIGN KEY(intent_id) "
                    "REFERENCES order_intents(intent_id))"
                )
            )

    @contextmanager
    def _writer(self) -> Iterator[Connection]:
        with self.db.connect() as conn:
            if self.db.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.begin()
                if self.db.dialect.name == "postgresql":
                    conn.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_id)"),
                        {"lock_id": POSTGRES_WRITER_LOCK_ID},
                    )
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _locked_row(self, conn: Connection, intent_id: str) -> RowMapping | None:
        suffix = "" if self.db.dialect.name == "sqlite" else " FOR UPDATE"
        result = conn.execute(
            text("SELECT * FROM order_intents WHERE intent_id=:intent_id" + suffix),
            {"intent_id": intent_id},
        ).mappings()
        return result.one_or_none()

    @staticmethod
    def _request_from_row(row: RowMapping) -> OrderRequest:
        return OrderRequest(
            intent_id=row["intent_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            quantity=D(row["quantity"]),
            price=D(row["price"]),
            quote_asset=row["quote_asset"],
            inventory_scope=row["inventory_scope"],
            order_type=row["order_type"],
        )

    @classmethod
    def _record_from_row(cls, row: RowMapping) -> OrderRecord:
        return OrderRecord(
            request=cls._request_from_row(row),
            state=OrderState(row["state"]),
            exchange_order_id=row["exchange_order_id"],
            confirmed_quantity=D(row["confirmed_quantity"]),
            confirmed_quote_quantity=D(row["confirmed_quote_quantity"]),
            confirmed_fee_quote=D(row["confirmed_fee_quote"]),
            exchange_cumulative_quantity=D(row["exchange_cumulative_quantity"]),
            cancel_requested=bool(row["cancel_requested"]),
            error=row["error"],
        )

    @staticmethod
    def _same_request(existing: OrderRequest, requested: OrderRequest) -> bool:
        return existing == requested

    def get(self, intent_id: str) -> OrderRecord:
        with self.db.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM order_intents WHERE intent_id=:intent_id"),
                {"intent_id": intent_id},
            ).mappings().one_or_none()
        if row is None:
            raise KeyError(intent_id)
        return self._record_from_row(row)

    def create_or_get(self, request: OrderRequest) -> tuple[OrderRecord, bool]:
        request.validate()
        with self._writer() as conn:
            row = self._locked_row(conn, request.intent_id)
            if row is not None:
                existing = self._record_from_row(row)
                if not self._same_request(existing.request, request):
                    raise OrderConflict("intent_id was reused with different order data")
                return existing, False
            other = conn.execute(
                text("SELECT intent_id FROM order_intents WHERE client_order_id=:client"),
                {"client": request.client_order_id},
            ).scalar_one_or_none()
            if other is not None:
                raise OrderConflict("client_order_id is already assigned to another intent")
            if request.side == "SELL":
                available = self._available_inventory(
                    conn, request.inventory_scope, request.symbol
                )
                if request.quantity > available:
                    raise InsufficientInventory(
                        f"SELL quantity {request.quantity} exceeds available {available}"
                    )
            now = self._millis()
            conn.execute(
                text(
                    "INSERT INTO order_intents (intent_id,client_order_id,symbol,side,"
                    "order_type,quantity,price,quote_asset,inventory_scope,state,"
                    "exchange_order_id,confirmed_quantity,confirmed_quote_quantity,"
                    "confirmed_fee_quote,exchange_cumulative_quantity,cancel_requested,"
                    "error,created_at,updated_at) VALUES (:intent,:client,:symbol,:side,"
                    ":order_type,:quantity,:price,:quote,:scope,:state,NULL,'0','0','0',"
                    "'0',0,NULL,:at,:at)"
                ),
                {
                    "intent": request.intent_id,
                    "client": request.client_order_id,
                    "symbol": request.symbol,
                    "side": request.side,
                    "order_type": request.order_type,
                    "quantity": str(request.quantity),
                    "price": str(request.price),
                    "quote": request.quote_asset,
                    "scope": request.inventory_scope,
                    "state": OrderState.INTENDED.value,
                    "at": now,
                },
            )
            row = self._locked_row(conn, request.intent_id)
            assert row is not None
            return self._record_from_row(row), True

    def _available_inventory(self, conn: Connection, scope: str, symbol: str) -> Decimal:
        rows = conn.execute(
            text(
                "SELECT side,state,quantity,confirmed_quantity,"
                "exchange_cumulative_quantity FROM order_intents "
                "WHERE inventory_scope=:scope AND symbol=:symbol"
            ),
            {"scope": scope, "symbol": symbol},
        ).mappings()
        confirmed = ZERO
        reserved = ZERO
        for row in rows:
            filled = D(row["confirmed_quantity"])
            if row["side"] == "BUY":
                confirmed += filled
            else:
                confirmed -= filled
                state = OrderState(row["state"])
                if state not in TERMINAL_STATES:
                    reserved += D(row["quantity"]) - filled
                elif state is OrderState.CANCELED:
                    # A cancel can precede delayed trade details. Keep the
                    # exchange-reported executed remainder unavailable until
                    # its confirmed fills arrive.
                    reserved += max(
                        ZERO, D(row["exchange_cumulative_quantity"]) - filled
                    )
        return confirmed - reserved

    def claim_submission(self, intent_id: str) -> bool:
        with self._writer() as conn:
            row = self._locked_row(conn, intent_id)
            if row is None:
                raise KeyError(intent_id)
            if OrderState(row["state"]) is not OrderState.INTENDED:
                return False
            conn.execute(
                text(
                    "UPDATE order_intents SET state=:state,updated_at=:at "
                    "WHERE intent_id=:intent"
                ),
                {
                    "state": OrderState.SUBMITTING.value,
                    "at": self._millis(),
                    "intent": intent_id,
                },
            )
            return True

    def mark_unknown(self, intent_id: str, reason: str) -> OrderRecord:
        with self._writer() as conn:
            row = self._locked_row(conn, intent_id)
            if row is None:
                raise KeyError(intent_id)
            state = OrderState(row["state"])
            if state not in TERMINAL_STATES:
                conn.execute(
                    text(
                        "UPDATE order_intents SET state=:state,error=:error,updated_at=:at "
                        "WHERE intent_id=:intent"
                    ),
                    {
                        "state": OrderState.UNKNOWN.value,
                        "error": reason[:1000],
                        "at": self._millis(),
                        "intent": intent_id,
                    },
                )
            current = self._locked_row(conn, intent_id)
            assert current is not None
            return self._record_from_row(current)

    def mark_cancel_requested(self, intent_id: str) -> tuple[OrderRecord, bool]:
        with self._writer() as conn:
            row = self._locked_row(conn, intent_id)
            if row is None:
                raise KeyError(intent_id)
            record = self._record_from_row(row)
            if record.state in {OrderState.FILLED, OrderState.REJECTED}:
                return record, False
            if record.cancel_requested:
                return record, False
            conn.execute(
                text(
                    "UPDATE order_intents SET cancel_requested=1,updated_at=:at "
                    "WHERE intent_id=:intent"
                ),
                {"at": self._millis(), "intent": intent_id},
            )
            current = self._locked_row(conn, intent_id)
            assert current is not None
            return self._record_from_row(current), True

    @staticmethod
    def _validate_fill(fill: ConfirmedFill, quote_asset: str) -> None:
        if not fill.trade_id or len(fill.trade_id) > 128:
            raise OrderInvariantError("exchange trade ID is missing or too long")
        _decimal(fill.quantity, "fill quantity")
        _decimal(fill.quote_quantity, "fill quote quantity")
        _decimal(fill.fee_quote, "fill quote fee", allow_zero=True)
        if fill.fee_asset != quote_asset:
            raise OrderInvariantError("only confirmed quote-asset fees are accepted")

    def apply_update(self, intent_id: str, update: ExchangeUpdate) -> OrderRecord:
        if update.status not in {
            OrderState.NEW,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
        }:
            raise OrderInvariantError("exchange update has a non-exchange state")
        cumulative = _decimal(
            update.cumulative_quantity, "cumulative_quantity", allow_zero=True
        )
        with self._writer() as conn:
            row = self._locked_row(conn, intent_id)
            if row is None:
                raise KeyError(intent_id)
            request = self._request_from_row(row)
            if update.client_order_id != request.client_order_id:
                raise OrderInvariantError("client order ID does not match intent")
            if not update.exchange_order_id or len(update.exchange_order_id) > 128:
                raise OrderInvariantError("exchange order ID is missing or too long")
            known_exchange_id = row["exchange_order_id"]
            if known_exchange_id is not None and known_exchange_id != update.exchange_order_id:
                raise OrderInvariantError("exchange order ID conflicts with prior update")
            if cumulative > request.quantity:
                raise OrderInvariantError("exchange cumulative quantity exceeds intent quantity")
            if update.status is OrderState.FILLED and cumulative != request.quantity:
                raise OrderInvariantError("FILLED cumulative quantity must equal intent quantity")
            if update.status is OrderState.REJECTED and (cumulative or update.fills):
                raise OrderInvariantError("REJECTED order cannot contain fills")

            prior_state = OrderState(row["state"])
            if prior_state is OrderState.REJECTED and update.fills:
                raise OrderInvariantError("REJECTED order cannot later contain fills")
            for fill in update.fills:
                self._validate_fill(fill, request.quote_asset)
                duplicate = conn.execute(
                    text("SELECT * FROM order_fills WHERE exchange_trade_id=:trade"),
                    {"trade": fill.trade_id},
                ).mappings().one_or_none()
                if duplicate is not None:
                    same = (
                        duplicate["intent_id"] == intent_id
                        and D(duplicate["quantity"]) == fill.quantity
                        and D(duplicate["quote_quantity"]) == fill.quote_quantity
                        and D(duplicate["fee_quote"]) == fill.fee_quote
                        and duplicate["fee_asset"] == fill.fee_asset
                    )
                    if not same:
                        raise OrderInvariantError("exchange trade ID has conflicting data")
                    continue
                conn.execute(
                    text(
                        "INSERT INTO order_fills (exchange_trade_id,intent_id,quantity,"
                        "quote_quantity,fee_quote,fee_asset) VALUES "
                        "(:trade,:intent,:quantity,:quote,:fee,:asset)"
                    ),
                    {
                        "trade": fill.trade_id,
                        "intent": intent_id,
                        "quantity": str(fill.quantity),
                        "quote": str(fill.quote_quantity),
                        "fee": str(fill.fee_quote),
                        "asset": fill.fee_asset,
                    },
                )

            totals = conn.execute(
                text(
                    "SELECT quantity,quote_quantity,fee_quote FROM order_fills "
                    "WHERE intent_id=:intent"
                ),
                {"intent": intent_id},
            ).mappings()
            confirmed_qty = ZERO
            confirmed_quote = ZERO
            confirmed_fee = ZERO
            for fill_row in totals:
                confirmed_qty += D(fill_row["quantity"])
                confirmed_quote += D(fill_row["quote_quantity"])
                confirmed_fee += D(fill_row["fee_quote"])
            if confirmed_qty > request.quantity:
                raise OrderInvariantError("confirmed fills exceed intent quantity")
            prior_cumulative = D(row["exchange_cumulative_quantity"])
            max_cumulative = max(prior_cumulative, cumulative)
            if confirmed_qty > max_cumulative:
                raise OrderInvariantError("confirmed fills exceed exchange cumulative quantity")
            if update.status is OrderState.FILLED and confirmed_qty != request.quantity:
                raise OrderInvariantError("FILLED update lacks confirmed fills for full quantity")
            state = update.status
            if prior_state is OrderState.FILLED or confirmed_qty == request.quantity:
                state = OrderState.FILLED
            elif prior_state in {OrderState.CANCELED, OrderState.REJECTED}:
                state = prior_state
            conn.execute(
                text(
                    "UPDATE order_intents SET state=:state,exchange_order_id=:exchange,"
                    "confirmed_quantity=:quantity,confirmed_quote_quantity=:quote,"
                    "confirmed_fee_quote=:fee,exchange_cumulative_quantity=:cumulative,"
                    "error=NULL,updated_at=:at WHERE intent_id=:intent"
                ),
                {
                    "state": state.value,
                    "exchange": update.exchange_order_id,
                    "quantity": str(confirmed_qty),
                    "quote": str(confirmed_quote),
                    "fee": str(confirmed_fee),
                    "cumulative": str(max_cumulative),
                    "at": self._millis(),
                    "intent": intent_id,
                },
            )
            if (
                request.side == "SELL"
                and self._available_inventory(
                    conn, request.inventory_scope, request.symbol
                )
                < 0
            ):
                raise OrderInvariantError("confirmed SELL fills exceed scoped bot inventory")
            current = self._locked_row(conn, intent_id)
            assert current is not None
            return self._record_from_row(current)


class OrderLifecycle:
    """Coordinates durable local transitions with an injected async transport."""

    def __init__(self, database_url: str, transport: OrderTransport) -> None:
        self.store = OrderStore(database_url)
        self.transport = transport

    async def submit(
        self, request: OrderRequest, filters: ExchangeFilters
    ) -> OrderRecord:
        validate_order_filters(request.quantity, request.price, filters)
        record, created = self.store.create_or_get(request)
        if not created:
            return record
        if not self.store.claim_submission(request.intent_id):
            return self.store.get(request.intent_id)
        try:
            update = await self.transport.submit(request)
        except (TimeoutError, RetryableTransportError) as error:
            return self.store.mark_unknown(request.intent_id, str(error) or type(error).__name__)
        except BaseException as error:
            self.store.mark_unknown(request.intent_id, str(error) or type(error).__name__)
            raise
        return self.store.apply_update(request.intent_id, update)

    async def reconcile(self, intent_id: str) -> OrderRecord:
        record = self.store.get(intent_id)
        update = await self.transport.query(record.request.client_order_id)
        return self.store.apply_update(intent_id, update)

    async def cancel(self, intent_id: str) -> OrderRecord:
        record, claimed = self.store.mark_cancel_requested(intent_id)
        if not claimed:
            return record
        try:
            update = await self.transport.cancel(record.request.client_order_id)
        except (TimeoutError, RetryableTransportError) as error:
            return self.store.mark_unknown(intent_id, str(error) or type(error).__name__)
        except BaseException as error:
            self.store.mark_unknown(intent_id, str(error) or type(error).__name__)
            raise
        return self.store.apply_update(intent_id, update)
