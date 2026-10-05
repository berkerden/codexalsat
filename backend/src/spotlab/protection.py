"""Durable SELL OCO protection for confirmed bot-owned Spot Testnet inventory.

One synthetic SELL reserves inventory for both mutually exclusive children.
Recovery only queries existing IDs; it never creates replacement orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text

from spotlab.orders import (
    ExchangeUpdate,
    OrderConflict,
    OrderError,
    OrderInvariantError,
    OrderRecord,
    OrderRequest,
    OrderState,
    OrderStore,
    deterministic_client_order_id,
)
from spotlab.testnet import (
    BinanceTestnetTransport,
    _mapping,
    _response_decimal,
    _response_int,
)

D = Decimal


@dataclass(frozen=True)
class ProtectionRequest:
    order: OrderRequest
    stop_price: Decimal

    @property
    def above_id(self) -> str:
        return deterministic_client_order_id(self.order.intent_id, "target")

    @property
    def below_id(self) -> str:
        return deterministic_client_order_id(self.order.intent_id, "stop")

    def validate(self) -> None:
        self.order.validate()
        if self.order.order_type != "OCO" or self.order.side != "SELL":
            raise OrderError("protection requires a SELL OCO parent")
        if (
            not isinstance(self.stop_price, Decimal)
            or not self.stop_price.is_finite()
            or self.stop_price <= 0
            or self.stop_price >= self.order.price
        ):
            raise OrderError("stop must be positive and below target")
        BinanceTestnetTransport._validate_client_id(self.order.client_order_id)
        if len({self.order.client_order_id, self.above_id, self.below_id}) != 3:
            raise OrderError("protection client IDs must be distinct")


@dataclass(frozen=True)
class ProtectionObservation:
    order: OrderRecord
    # This is a point-in-time observation, never a lasting protection guarantee.
    both_legs_open: bool
    observed_at_ms: int


class ProtectionLifecycle:
    def __init__(self, store: OrderStore, transport: BinanceTestnetTransport) -> None:
        self.store = store
        self.transport = transport
        with store._writer() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS protection_intents ("
                    "intent_id VARCHAR(128) PRIMARY KEY,stop_price VARCHAR(128) NOT NULL,"
                    "above_id VARCHAR(36) NOT NULL UNIQUE,below_id VARCHAR(36) NOT NULL UNIQUE)"
                )
            )

    def _persist_payload(self, request: ProtectionRequest) -> None:
        # Payload precedes parent creation; an orphan payload is safe and has no
        # reservation or network effect. Both exist before claim_submission.
        with self.store._writer() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM protection_intents WHERE intent_id=:intent"),
                    {"intent": request.order.intent_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                if (
                    D(row["stop_price"]) != request.stop_price
                    or row["above_id"] != request.above_id
                    or row["below_id"] != request.below_id
                ):
                    raise OrderConflict("protection payload differs from persisted intent")
                return
            conn.execute(
                text(
                    "INSERT INTO protection_intents (intent_id,stop_price,above_id,below_id) "
                    "VALUES (:intent,:stop,:above,:below)"
                ),
                {
                    "intent": request.order.intent_id,
                    "stop": str(request.stop_price),
                    "above": request.above_id,
                    "below": request.below_id,
                },
            )

    def _load(self, intent_id: str) -> ProtectionRequest:
        order = self.store.get(intent_id).request
        with self.store.db.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM protection_intents WHERE intent_id=:intent"),
                    {"intent": intent_id},
                )
                .mappings()
                .one()
            )
        request = ProtectionRequest(order, D(row["stop_price"]))
        request.validate()
        if row["above_id"] != request.above_id or row["below_id"] != request.below_id:
            raise OrderInvariantError("persisted child IDs do not match protection")
        if order.symbol != self.transport.symbol:
            raise OrderError("protection symbol does not match transport")
        return request

    async def submit(self, request: ProtectionRequest) -> OrderRecord:
        request.validate()
        order = request.order
        if order.symbol != self.transport.symbol:
            raise OrderError("protection symbol does not match transport")
        # Local checks precede reservation/claim. Failure here proves no POST was
        # attempted, so a later explicit management pass may retry the same plan.
        try:
            existing = self.store.get(order.intent_id)
        except KeyError:
            existing = None
        if existing is not None:
            self._persist_payload(request)
            record, _ = self.store.create_or_get(order)
            if record.state is not OrderState.INTENDED:
                return record
        await self.transport.prepare_submission(order, stop_price=request.stop_price)
        self._persist_payload(request)
        record, created = self.store.create_or_get(order)
        if not created and record.state is not OrderState.INTENDED:
            return record
        if not self.store.claim_submission(order.intent_id):
            return self.store.get(order.intent_id)
        try:
            # Ignore optimistic POST status. Only a subsequent list + both child
            # queries with authenticated trade details can establish coverage.
            await self.transport.request(
                "POST",
                "/api/v3/orderList/oco",
                {
                    "symbol": order.symbol,
                    "side": "SELL",
                    "quantity": order.quantity,
                    "listClientOrderId": order.client_order_id,
                    "aboveType": "LIMIT_MAKER",
                    "abovePrice": order.price,
                    "aboveClientOrderId": request.above_id,
                    "belowType": "STOP_LOSS",
                    "belowStopPrice": request.stop_price,
                    "belowClientOrderId": request.below_id,
                    "newOrderRespType": "RESULT",
                },
            )
            return (await self.reconcile(order.intent_id)).order
        except BaseException as error:
            self.store.mark_unknown(order.intent_id, type(error).__name__)
            raise

    async def reconcile(self, intent_id: str) -> ProtectionObservation:
        try:
            request = self._load(intent_id)
            order = request.order
            await self.transport.exchange_info()
            raw = _mapping(
                await self.transport.request(
                    "GET",
                    "/api/v3/orderList",
                    {
                        "origClientOrderId": order.client_order_id,
                    },
                ),
                "OCO list",
            )
            list_id = _response_int(raw.get("orderListId"), "order list ID")
            if (
                raw.get("listClientOrderId") != order.client_order_id
                or raw.get("symbol") != order.symbol
                or raw.get("contingencyType") != "OCO"
            ):
                raise OrderInvariantError("OCO list identity mismatch")
            children = raw.get("orders")
            if not isinstance(children, list) or len(children) != 2:
                raise OrderInvariantError("OCO list must identify both children")
            members: dict[str, int] = {}
            for value in children:
                child = _mapping(value, "OCO child")
                client_id = child.get("clientOrderId")
                if (
                    client_id not in {request.above_id, request.below_id}
                    or child.get("symbol") != order.symbol
                    or client_id in members
                ):
                    raise OrderInvariantError("OCO child identity mismatch")
                members[client_id] = _response_int(child.get("orderId"), "child order ID")
            if len(set(members.values())) != 2:
                raise OrderInvariantError("OCO child exchange IDs must be distinct")
            updates = []
            for client_id, order_id in members.items():
                child = _mapping(
                    await self.transport.request(
                        "GET",
                        "/api/v3/order",
                        {
                            "symbol": order.symbol,
                            "origClientOrderId": client_id,
                        },
                    ),
                    "OCO child order",
                )
                expected_type = "LIMIT_MAKER" if client_id == request.above_id else "STOP_LOSS"
                if (
                    child.get("symbol") != order.symbol
                    or child.get("side") != "SELL"
                    or child.get("clientOrderId") != client_id
                    or child.get("orderId") != order_id
                    or child.get("orderListId") != list_id
                    or child.get("type") != expected_type
                    or _response_decimal(child.get("origQty"), "child quantity") != order.quantity
                ):
                    raise OrderInvariantError("OCO child terms mismatch")
                if expected_type == "LIMIT_MAKER":
                    if _response_decimal(child.get("price"), "target price") != order.price:
                        raise OrderInvariantError("OCO target mismatch")
                elif _response_decimal(child.get("stopPrice"), "stop price") != request.stop_price:
                    raise OrderInvariantError("OCO stop mismatch")
                # Parse this exact validated response, avoiding a second order
                # query whose changed identity could bypass these checks.
                updates.append(await self.transport._parse_order(child, client_id))
            cumulative = sum((u.cumulative_quantity for u in updates), D(0))
            fills = tuple(fill for u in updates for fill in u.fills)
            confirmed = sum((fill.quantity for fill in fills), D(0))
            if cumulative > order.quantity or confirmed != cumulative:
                raise OrderInvariantError("OCO execution totals incomplete or exceed reservation")
            if len({fill.trade_id for fill in fills}) != len(fills):
                raise OrderInvariantError("OCO children contain duplicate trade IDs")
            terminal = {OrderState.CANCELED, OrderState.REJECTED, OrderState.FILLED}
            if confirmed == order.quantity:
                state = OrderState.FILLED
            elif all(update.status in terminal for update in updates):
                state = OrderState.CANCELED
            else:
                state = OrderState.PARTIALLY_FILLED if confirmed else OrderState.NEW
            record = self.store.apply_update(
                intent_id,
                ExchangeUpdate(
                    order.client_order_id, f"oco:{order.symbol}:{list_id}", state, cumulative, fills
                ),
            )
            both_open = record.state is OrderState.NEW and all(
                update.status is OrderState.NEW for update in updates
            )
            return ProtectionObservation(record, both_open, self.store._millis())
        except BaseException as error:
            self.store.mark_unknown(intent_id, type(error).__name__)
            raise
