"""Verified, one-shot adoption of an unbound Spot Testnet ledger.

The migration client is deliberately separate from the normal order transport.
It exposes only authenticated GETs to a fixed Testnet host and never mutates the
exchange or the historical local ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Never, cast
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, RowMapping

from spotlab.orders import (
    TERMINAL_STATES,
    TESTNET_ACCOUNT_VENUE,
    ConfirmedFill,
    OrderRequest,
    OrderState,
    OrderStore,
    deterministic_client_order_id,
)
from spotlab.testnet import TESTNET_BASE_URL, TestnetCredentials
from spotlab.testnet_cycle import CyclePlan

D = Decimal
ZERO = D(0)
MIGRATION_VERSION: Final = 1
TRADE_PAGE_SIZE: Final = 1_000
MAX_MIGRATION_SECONDS: Final = 30.0
MAX_LOCAL_CLOCK_DRIFT_MS: Final = 1_000
SUPPORTED_SYMBOLS: Final = frozenset({"BTCUSDT", "SOLUSDT"})
_GET_PATHS: Final = frozenset(
    {
        "/api/v3/time",
        "/api/v3/account",
        "/api/v3/openOrders",
        "/api/v3/order",
        "/api/v3/orderList",
        "/api/v3/myTrades",
    }
)
_TERMINAL_REMOTE: Final = {
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELED,
    "REJECTED": OrderState.REJECTED,
    "EXPIRED": OrderState.CANCELED,
    "EXPIRED_IN_MATCH": OrderState.CANCELED,
}
_REASONS: Final = frozenset(
    {
        "migration_not_required",
        "migration_requires_history",
        "migration_local_inconsistent",
        "migration_unsettled",
        "migration_inventory_nonzero",
        "migration_history_unavailable",
        "migration_evidence_mismatch",
        "migration_identity_changed",
        "migration_ledger_changed",
        "migration_remote_open_orders",
    }
)
_OWNED_NAMESPACE: Final = re.compile(r"^(?:entry|target|stop|oco|spotlab)-[0-9a-f]{24}$")
_SNAPSHOT_TABLES: Final = (
    "order_intents",
    "order_fills",
    "protection_intents",
    "testnet_cycles",
    "ledger_account_binding",
    "ledger_account_incidents",
    "ledger_account_migrations",
)


class LegacyMigrationError(RuntimeError):
    """A sanitized, allowlisted migration failure."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = (
            reason_code if reason_code in _REASONS else "migration_history_unavailable"
        )
        super().__init__(self.reason_code)


def _fail(reason: str) -> Never:
    raise LegacyMigrationError(reason)


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _fail("migration_evidence_mismatch")
    try:
        parsed = D(str(value))
    except (InvalidOperation, ValueError):
        _fail("migration_evidence_mismatch")
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        _fail("migration_evidence_mismatch")
    return parsed


def _integer(value: object, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("migration_evidence_mismatch")
    if value < 0 or (not allow_zero and value == 0):
        _fail("migration_evidence_mismatch")
    return value


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("migration_evidence_mismatch")
    return cast(dict[str, Any], value)


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class _LocalSnapshot:
    marker: tuple[tuple[object, ...], ...]
    digest: str
    orders: tuple[RowMapping, ...]
    fills: tuple[RowMapping, ...]
    protections: tuple[RowMapping, ...]
    cycles: tuple[RowMapping, ...]
    binding_count: int
    incident_count: int
    receipt_count: int


@dataclass(frozen=True, repr=False)
class LegacyMigrationEvidence:
    local_marker: tuple[tuple[object, ...], ...]
    before_ledger_sha256: str
    remote_evidence_sha256: str
    account_uid: int
    verified_at_ms: int
    order_count: int
    fill_count: int
    cycle_count: int
    unrelated_open_order_count: int

    def diagnostic(self, *, applied: bool = False) -> dict[str, object]:
        return {
            "mode": "testnet",
            "production_enabled": False,
            "status": "migrated" if applied else "verified_preview",
            "applied": applied,
            "order_count": self.order_count,
            "fill_count": self.fill_count,
            "cycle_count": self.cycle_count,
            "unrelated_open_order_count": self.unrelated_open_order_count,
        }


def _table_rows(conn: Connection, table: str) -> tuple[RowMapping, ...]:
    if not inspect(conn).has_table(table):
        return ()
    return tuple(conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings())


def _read_local_snapshot(conn: Connection) -> _LocalSnapshot:
    tables: dict[str, tuple[RowMapping, ...]] = {}
    marker: list[tuple[object, ...]] = []
    inspector = inspect(conn)
    for table in _SNAPSHOT_TABLES:
        present = inspector.has_table(table)
        marker.append(("table", table, present))
        rows = _table_rows(conn, table) if present else ()
        tables[table] = rows
        for row in rows:
            marker.append((table, *tuple((key, row[key]) for key in row.keys())))
    frozen = tuple(marker)
    return _LocalSnapshot(
        marker=frozen,
        digest=_canonical_hash(frozen),
        orders=tables["order_intents"],
        fills=tables["order_fills"],
        protections=tables["protection_intents"],
        cycles=tables["testnet_cycles"],
        binding_count=len(tables["ledger_account_binding"]),
        incident_count=len(tables["ledger_account_incidents"]),
        receipt_count=len(tables["ledger_account_migrations"]),
    )


class LegacyTestnetReader:
    """Pinned-host GET-only reader used only by legacy verification."""

    def __init__(
        self,
        credentials: TestnetCredentials,
        *,
        mock_transport: httpx.MockTransport | None = None,
        timeout_s: float = 10.0,
        clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if mock_transport is not None and not isinstance(mock_transport, httpx.MockTransport):
            raise TypeError("only httpx.MockTransport may be injected")
        self._credentials = credentials
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic = monotonic or time.monotonic
        self._clock_offset_ms = 0
        self._server_time_ms = 0
        self._verification_deadline: float | None = None
        self._client = httpx.AsyncClient(
            base_url=TESTNET_BASE_URL,
            timeout=httpx.Timeout(float(timeout_s)),
            follow_redirects=False,
            trust_env=False,
            transport=mock_transport,
        )

    async def __aenter__(self) -> LegacyTestnetReader:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def server_time_ms(self) -> int:
        return self._server_time_ms

    def begin_verification(self) -> None:
        self._verification_deadline = self._monotonic() + MAX_MIGRATION_SECONDS

    def ensure_fresh(self) -> None:
        if self._verification_deadline is None or self._monotonic() > self._verification_deadline:
            _fail("migration_history_unavailable")

    async def _get(
        self, path: str, params: Mapping[str, object] | None = None, *, signed: bool = True
    ) -> dict[str, Any] | list[Any]:
        if self._verification_deadline is not None:
            self.ensure_fresh()
        if path not in _GET_PATHS:
            _fail("migration_history_unavailable")
        values = dict(params or {})
        if "signature" in values or "timestamp" in values:
            _fail("migration_history_unavailable")
        if signed:
            values["recvWindow"] = 5_000
            values["timestamp"] = self._clock_ms() + self._clock_offset_ms
        encoded = urlencode(values, doseq=True, quote_via=quote, safe="", encoding="utf-8")
        headers: dict[str, str] = {}
        if signed:
            signature = hmac.new(
                self._credentials.secret.get_secret_value().encode("utf-8"),
                encoded.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            encoded = f"{encoded}&signature={signature}"
            headers["X-MBX-APIKEY"] = self._credentials.api_key.get_secret_value()
        try:
            response = await self._client.get(
                f"{path}?{encoded}" if encoded else path, headers=headers
            )
        except httpx.HTTPError:
            _fail("migration_history_unavailable")
        if response.status_code != 200:
            _fail("migration_history_unavailable")
        try:
            decoded = response.json()
        except ValueError:
            _fail("migration_history_unavailable")
        if not isinstance(decoded, (dict, list)):
            _fail("migration_evidence_mismatch")
        if self._verification_deadline is not None:
            self.ensure_fresh()
        return cast(dict[str, Any] | list[Any], decoded)

    async def _sync_time(self) -> None:
        before = self._clock_ms()
        raw = _mapping(await self._get("/api/v3/time", signed=False))
        after = self._clock_ms()
        server = _integer(raw.get("serverTime"), allow_zero=False)
        self._clock_offset_ms = server - ((before + after) // 2)
        self._server_time_ms = server

    async def account_uid(self) -> int:
        await self._sync_time()
        account = _mapping(await self._get("/api/v3/account"))
        uid = account.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            _fail("migration_history_unavailable")
        return uid

    async def verified_get(
        self, expected_uid: int, path: str, params: Mapping[str, object] | None = None
    ) -> dict[str, Any] | list[Any]:
        if await self.account_uid() != expected_uid:
            _fail("migration_identity_changed")
        result = await self._get(path, params)
        if await self.account_uid() != expected_uid:
            _fail("migration_identity_changed")
        self.ensure_fresh()
        return result


def _local_request(row: RowMapping) -> OrderRequest:
    try:
        request = OrderRequest(
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
        request.validate()
    except Exception:
        _fail("migration_local_inconsistent")
    if request.symbol not in SUPPORTED_SYMBOLS or request.quote_asset != "USDT":
        _fail("migration_local_inconsistent")
    return request


def _local_fill(row: RowMapping, request: OrderRequest) -> ConfirmedFill:
    try:
        fill = ConfirmedFill(
            trade_id=row["exchange_trade_id"],
            quantity=D(row["quantity"]),
            quote_quantity=D(row["quote_quantity"]),
            fee_quote=D(row["fee_quote"]),
            fee_asset=row["fee_asset"],
            fee_base=D(row["fee_base"]),
        )
        OrderStore._validate_fill(fill, request.symbol, request.quote_asset)
    except Exception:
        _fail("migration_local_inconsistent")
    return fill


def _validate_local(
    snapshot: _LocalSnapshot,
) -> tuple[dict[str, OrderRequest], dict[str, tuple[ConfirmedFill, ...]], dict[str, RowMapping]]:
    if snapshot.binding_count or snapshot.receipt_count:
        _fail("migration_not_required")
    if snapshot.incident_count or ("table", "ledger_account_migrations", True) in snapshot.marker:
        _fail("migration_local_inconsistent")
    if (
        not snapshot.orders
        and not snapshot.fills
        and not snapshot.protections
        and not snapshot.cycles
    ):
        _fail("migration_not_required")

    order_rows: dict[str, RowMapping] = {}
    requests: dict[str, OrderRequest] = {}
    for row in snapshot.orders:
        request = _local_request(row)
        if request.intent_id in requests:
            _fail("migration_local_inconsistent")
        requests[request.intent_id] = request
        order_rows[request.intent_id] = row
        try:
            state = OrderState(row["state"])
            confirmed = D(row["confirmed_quantity"])
            quote = D(row["confirmed_quote_quantity"])
            fee = D(row["confirmed_fee_quote"])
            cumulative = D(row["exchange_cumulative_quantity"])
        except Exception:
            _fail("migration_local_inconsistent")
        if state not in TERMINAL_STATES:
            _fail("migration_unsettled")
        if not row["exchange_order_id"]:
            _fail("migration_requires_history")
        if any(not value.is_finite() or value < 0 for value in (confirmed, quote, fee, cumulative)):
            _fail("migration_local_inconsistent")
        if confirmed != cumulative or confirmed > request.quantity:
            _fail("migration_requires_history")
        if state is OrderState.FILLED and confirmed != request.quantity:
            _fail("migration_local_inconsistent")
        if state is OrderState.REJECTED and any(value != 0 for value in (confirmed, quote, fee)):
            _fail("migration_local_inconsistent")

    grouped: dict[str, list[ConfirmedFill]] = {intent: [] for intent in requests}
    seen_fill_ids: set[str] = set()
    for row in snapshot.fills:
        intent = row["intent_id"]
        fill_request = requests.get(intent)
        if fill_request is None or row["exchange_trade_id"] in seen_fill_ids:
            _fail("migration_local_inconsistent")
        seen_fill_ids.add(row["exchange_trade_id"])
        grouped[intent].append(_local_fill(row, fill_request))
    frozen_fills = {key: tuple(value) for key, value in grouped.items()}
    for intent in requests:
        row = order_rows[intent]
        fills = frozen_fills[intent]
        if (
            sum((item.quantity for item in fills), ZERO) != D(row["confirmed_quantity"])
            or sum((item.quote_quantity for item in fills), ZERO)
            != D(row["confirmed_quote_quantity"])
            or sum((item.fee_quote for item in fills), ZERO) != D(row["confirmed_fee_quote"])
        ):
            _fail("migration_local_inconsistent")
        if OrderState(row["state"]) is OrderState.REJECTED and fills:
            _fail("migration_local_inconsistent")

    protections: dict[str, RowMapping] = {}
    for row in snapshot.protections:
        intent = row["intent_id"]
        if intent in protections or intent not in requests or requests[intent].order_type != "OCO":
            _fail("migration_local_inconsistent")
        request = requests[intent]
        try:
            stop = D(row["stop_price"])
        except Exception:
            _fail("migration_local_inconsistent")
        if (
            not stop.is_finite()
            or stop <= 0
            or stop >= request.price
            or row["above_id"] != deterministic_client_order_id(intent, "target")
            or row["below_id"] != deterministic_client_order_id(intent, "stop")
        ):
            _fail("migration_local_inconsistent")
        protections[intent] = row
    if {key for key, value in requests.items() if value.order_type == "OCO"} != set(protections):
        _fail("migration_local_inconsistent")

    for row in snapshot.cycles:
        try:
            plan = CyclePlan.deserialize(row["plan"])
        except Exception:
            _fail("migration_local_inconsistent")
        if row["state"] not in {"EXITED", "NO_FILL"}:
            _fail("migration_unsettled")
        if plan.serialize() != row["plan"] or plan.cycle_id != row["cycle_id"]:
            _fail("migration_local_inconsistent")
        if row["reason"] is not None:
            _fail("migration_local_inconsistent")
        entry = requests.get(plan.entry_id)
        if entry != plan.entry():
            _fail("migration_local_inconsistent")
        entry_row = order_rows[plan.entry_id]
        if _integer(entry_row["created_at"], allow_zero=False) > plan.entry_deadline_ms + 1_000:
            _fail("migration_local_inconsistent")
        dust = D(row["dust"] or "0")
        if not dust.is_finite() or dust != 0:
            _fail("migration_inventory_nonzero")
        if row["state"] == "NO_FILL":
            if D(entry_row["confirmed_quantity"]) != 0 or plan.protection_id in requests:
                _fail("migration_local_inconsistent")
            if row["protection_quantity"] is not None:
                _fail("migration_local_inconsistent")
        else:
            if D(entry_row["confirmed_quantity"]) <= 0:
                _fail("migration_local_inconsistent")
            try:
                quantity = D(row["protection_quantity"])
            except Exception:
                _fail("migration_local_inconsistent")
            protection = requests.get(plan.protection_id)
            protection_payload = protections.get(plan.protection_id)
            if (
                not quantity.is_finite()
                or quantity <= 0
                or protection != plan.protection(quantity).order
                or protection_payload is None
                or D(protection_payload["stop_price"]) != plan.stop_price
            ):
                _fail("migration_local_inconsistent")

    inventory: dict[tuple[str, str], Decimal] = {}
    for intent, request in requests.items():
        fills = frozen_fills[intent]
        base_fee = sum((item.fee_base for item in fills), ZERO)
        quantity = D(order_rows[intent]["confirmed_quantity"])
        signed = quantity - base_fee if request.side == "BUY" else -(quantity + base_fee)
        key = (request.inventory_scope, request.symbol)
        inventory[key] = inventory.get(key, ZERO) + signed
    if any(value != ZERO for value in inventory.values()):
        _fail("migration_inventory_nonzero")
    return requests, frozen_fills, protections


def _parse_exchange_id(value: object, symbol: str, *, oco: bool) -> int:
    prefix = f"oco:{symbol}:" if oco else f"{symbol}:"
    if not isinstance(value, str) or not value.startswith(prefix):
        _fail("migration_local_inconsistent")
    suffix = value[len(prefix) :]
    if not suffix.isascii() or not suffix.isdecimal():
        _fail("migration_local_inconsistent")
    return int(suffix)


def _remote_state(value: object) -> OrderState:
    if not isinstance(value, str) or value not in _TERMINAL_REMOTE:
        _fail("migration_unsettled")
    return _TERMINAL_REMOTE[value]


def _order_times(data: Mapping[str, object], reader: LegacyTestnetReader) -> tuple[int, int]:
    created = _integer(data.get("time"), allow_zero=False)
    updated = _integer(data.get("updateTime"), allow_zero=False)
    if created > updated or updated > reader.server_time_ms:
        _fail("migration_evidence_mismatch")
    return created, updated


async def _remote_trades(
    reader: LegacyTestnetReader,
    uid: int,
    request: OrderRequest,
    order_id: int,
    created_at: int,
    updated_at: int,
) -> tuple[ConfirmedFill, ...]:
    by_id: dict[int, ConfirmedFill] = {}
    from_id = 0
    while True:
        raw = await reader.verified_get(
            uid,
            "/api/v3/myTrades",
            {
                "symbol": request.symbol,
                "orderId": order_id,
                "fromId": from_id,
                "limit": TRADE_PAGE_SIZE,
            },
        )
        if not isinstance(raw, list) or len(raw) > TRADE_PAGE_SIZE:
            _fail("migration_evidence_mismatch")
        highest = from_id - 1
        for value in raw:
            trade = _mapping(value)
            trade_id = _integer(trade.get("id"))
            if trade_id in by_id or trade_id < from_id:
                _fail("migration_evidence_mismatch")
            if trade.get("symbol") != request.symbol or _integer(trade.get("orderId")) != order_id:
                _fail("migration_evidence_mismatch")
            is_buyer = trade.get("isBuyer")
            if not isinstance(is_buyer, bool) or is_buyer != (request.side == "BUY"):
                _fail("migration_evidence_mismatch")
            trade_time = _integer(trade.get("time"), allow_zero=False)
            if not created_at <= trade_time <= updated_at or trade_time > reader.server_time_ms:
                _fail("migration_evidence_mismatch")
            _decimal(trade.get("price"), positive=True)
            quantity = _decimal(trade.get("qty"), positive=True)
            quote_quantity = _decimal(trade.get("quoteQty"), positive=True)
            commission = _decimal(trade.get("commission"))
            asset = trade.get("commissionAsset")
            base_asset = request.symbol[: -len(request.quote_asset)]
            fee_base = ZERO
            if asset in {None, ""}:
                if commission != 0:
                    _fail("migration_evidence_mismatch")
                asset = ""
                fee_quote = ZERO
            elif asset == request.quote_asset:
                fee_quote = commission
            elif asset == base_asset:
                fee_base = commission
                fee_quote = commission * quote_quantity / quantity
            else:
                _fail("migration_evidence_mismatch")
            by_id[trade_id] = ConfirmedFill(
                f"{request.symbol}:{trade_id}",
                quantity,
                quote_quantity,
                fee_quote,
                cast(str, asset),
                fee_base,
            )
            highest = max(highest, trade_id)
        if len(raw) < TRADE_PAGE_SIZE:
            break
        if highest < from_id:
            _fail("migration_evidence_mismatch")
        from_id = highest + 1
    return tuple(by_id[key] for key in sorted(by_id))


def _assert_fills(local: tuple[ConfirmedFill, ...], remote: tuple[ConfirmedFill, ...]) -> None:
    local_by_id = {item.trade_id: item for item in local}
    remote_by_id = {item.trade_id: item for item in remote}
    if (
        len(local_by_id) != len(local)
        or len(remote_by_id) != len(remote)
        or local_by_id != remote_by_id
    ):
        _fail("migration_evidence_mismatch")


def _assert_local_time(row: RowMapping, remote_created: int, remote_updated: int) -> None:
    created = _integer(row["created_at"], allow_zero=False)
    updated = _integer(row["updated_at"], allow_zero=False)
    if created > updated:
        _fail("migration_local_inconsistent")
    if (
        remote_created < created - MAX_LOCAL_CLOCK_DRIFT_MS
        or remote_updated > updated + MAX_LOCAL_CLOCK_DRIFT_MS
    ):
        _fail("migration_evidence_mismatch")


async def _verify_limit(
    reader: LegacyTestnetReader,
    uid: int,
    row: RowMapping,
    request: OrderRequest,
    fills: tuple[ConfirmedFill, ...],
) -> object:
    order_id = _parse_exchange_id(row["exchange_order_id"], request.symbol, oco=False)
    remote = _mapping(
        await reader.verified_get(
            uid,
            "/api/v3/order",
            {
                "symbol": request.symbol,
                "orderId": order_id,
                "origClientOrderId": request.client_order_id,
            },
        )
    )
    remote_state = _remote_state(remote.get("status"))
    remote_executed = _decimal(remote.get("executedQty"))
    remote_quote = _decimal(remote.get("cummulativeQuoteQty"))
    if (
        remote.get("symbol") != request.symbol
        or remote.get("clientOrderId") != request.client_order_id
        or _integer(remote.get("orderId")) != order_id
        or isinstance(remote.get("orderListId"), bool)
        or remote.get("orderListId") != -1
        or remote.get("side") != request.side
        or remote.get("type") != "LIMIT"
        or remote.get("timeInForce") != "GTC"
        or _decimal(remote.get("origQty"), positive=True) != request.quantity
        or _decimal(remote.get("price"), positive=True) != request.price
        or _decimal(remote.get("stopPrice")) != ZERO
        or remote_state is not OrderState(row["state"])
        or remote_executed != D(row["exchange_cumulative_quantity"])
        or remote_quote != D(row["confirmed_quote_quantity"])
        or (remote_state is OrderState.FILLED and remote_executed != request.quantity)
        or (remote_state is OrderState.REJECTED and (remote_executed != 0 or remote_quote != 0))
    ):
        _fail("migration_evidence_mismatch")
    created, updated = _order_times(remote, reader)
    _assert_local_time(row, created, updated)
    remote_fills = await _remote_trades(reader, uid, request, order_id, created, updated)
    if remote_state is OrderState.REJECTED and remote_fills:
        _fail("migration_evidence_mismatch")
    _assert_fills(fills, remote_fills)
    return ("limit", request.intent_id, remote, remote_fills)


async def _verify_oco(
    reader: LegacyTestnetReader,
    uid: int,
    row: RowMapping,
    request: OrderRequest,
    fills: tuple[ConfirmedFill, ...],
    protection: RowMapping,
) -> object:
    list_id = _parse_exchange_id(row["exchange_order_id"], request.symbol, oco=True)
    remote_list = _mapping(
        await reader.verified_get(
            uid,
            "/api/v3/orderList",
            {"orderListId": list_id, "origClientOrderId": request.client_order_id},
        )
    )
    if (
        remote_list.get("symbol") != request.symbol
        or _integer(remote_list.get("orderListId")) != list_id
        or remote_list.get("listClientOrderId") != request.client_order_id
        or remote_list.get("contingencyType") != "OCO"
        or remote_list.get("listStatusType") != "ALL_DONE"
        or remote_list.get("listOrderStatus") != "ALL_DONE"
    ):
        _fail("migration_evidence_mismatch")
    transaction_time = _integer(remote_list.get("transactionTime"), allow_zero=False)
    local_created = _integer(row["created_at"], allow_zero=False)
    local_updated = _integer(row["updated_at"], allow_zero=False)
    if (
        transaction_time < local_created - MAX_LOCAL_CLOCK_DRIFT_MS
        or transaction_time > local_updated + MAX_LOCAL_CLOCK_DRIFT_MS
        or transaction_time > reader.server_time_ms
    ):
        _fail("migration_evidence_mismatch")
    members = remote_list.get("orders")
    if not isinstance(members, list) or len(members) != 2:
        _fail("migration_evidence_mismatch")
    expected_clients = {str(protection["above_id"]), str(protection["below_id"])}
    remote_members: dict[str, int] = {}
    for value in members:
        member = _mapping(value)
        client = member.get("clientOrderId")
        if (
            client not in expected_clients
            or client in remote_members
            or member.get("symbol") != request.symbol
        ):
            _fail("migration_evidence_mismatch")
        remote_members[cast(str, client)] = _integer(member.get("orderId"))
    if set(remote_members) != expected_clients or len(set(remote_members.values())) != 2:
        _fail("migration_evidence_mismatch")

    combined: list[ConfirmedFill] = []
    child_evidence: list[object] = []
    executed = ZERO
    quoted = ZERO
    earliest: int | None = None
    latest = 0
    for client, order_id in sorted(remote_members.items()):
        child = _mapping(
            await reader.verified_get(
                uid,
                "/api/v3/order",
                {"symbol": request.symbol, "orderId": order_id, "origClientOrderId": client},
            )
        )
        above = client == protection["above_id"]
        expected_type = "LIMIT_MAKER" if above else "STOP_LOSS"
        if (
            child.get("symbol") != request.symbol
            or child.get("clientOrderId") != client
            or _integer(child.get("orderId")) != order_id
            or _integer(child.get("orderListId")) != list_id
            or child.get("side") != "SELL"
            or child.get("type") != expected_type
            or child.get("timeInForce") != "GTC"
            or _decimal(child.get("origQty"), positive=True) != request.quantity
            or _decimal(child.get("price")) != (request.price if above else ZERO)
            or _decimal(child.get("stopPrice")) != (ZERO if above else D(protection["stop_price"]))
        ):
            _fail("migration_evidence_mismatch")
        child_state = _remote_state(child.get("status"))
        child_executed = _decimal(child.get("executedQty"))
        child_quote = _decimal(child.get("cummulativeQuoteQty"))
        if child_state is OrderState.FILLED and child_executed != request.quantity:
            _fail("migration_evidence_mismatch")
        if child_state is OrderState.REJECTED and (child_executed != 0 or child_quote != 0):
            _fail("migration_evidence_mismatch")
        created, updated = _order_times(child, reader)
        earliest = created if earliest is None else min(earliest, created)
        latest = max(latest, updated)
        child_fills = await _remote_trades(reader, uid, request, order_id, created, updated)
        if child_state is OrderState.REJECTED and child_fills:
            _fail("migration_evidence_mismatch")
        if sum((item.quantity for item in child_fills), ZERO) != child_executed:
            _fail("migration_evidence_mismatch")
        if sum((item.quote_quantity for item in child_fills), ZERO) != child_quote:
            _fail("migration_evidence_mismatch")
        executed += child_executed
        quoted += child_quote
        combined.extend(child_fills)
        child_evidence.append((child, child_fills))
    if len({item.trade_id for item in combined}) != len(combined):
        _fail("migration_evidence_mismatch")
    derived = OrderState.FILLED if executed == request.quantity else OrderState.CANCELED
    if (
        derived is not OrderState(row["state"])
        or executed != D(row["exchange_cumulative_quantity"])
        or quoted != D(row["confirmed_quote_quantity"])
        or executed > request.quantity
    ):
        _fail("migration_evidence_mismatch")
    assert earliest is not None
    _assert_local_time(row, earliest, latest)
    _assert_fills(fills, tuple(combined))
    return ("oco", request.intent_id, remote_list, tuple(child_evidence))


async def verify_legacy_migration(
    store: OrderStore, reader: LegacyTestnetReader
) -> LegacyMigrationEvidence:
    """Verify an immutable local snapshot against complete remote Testnet history."""

    reader.begin_verification()
    with store._writer() as conn:
        snapshot = _read_local_snapshot(conn)
    requests, fills, protections = _validate_local(snapshot)
    rows = {row["intent_id"]: row for row in snapshot.orders}
    uid = await reader.account_uid()
    remote_evidence: list[object] = []
    for intent in sorted(requests):
        reader.ensure_fresh()
        request = requests[intent]
        row = rows[intent]
        if request.order_type == "LIMIT":
            evidence = await _verify_limit(reader, uid, row, request, fills[intent])
        else:
            evidence = await _verify_oco(
                reader, uid, row, request, fills[intent], protections[intent]
            )
        remote_evidence.append(evidence)

    raw_open = await reader.verified_get(uid, "/api/v3/openOrders")
    if not isinstance(raw_open, list):
        _fail("migration_evidence_mismatch")
    known_clients = {request.client_order_id for request in requests.values()}
    known_ids: set[tuple[str, int]] = set()
    known_list_ids: set[tuple[str, int]] = set()
    for intent, request in requests.items():
        if request.order_type == "LIMIT":
            known_ids.add(
                (
                    request.symbol,
                    _parse_exchange_id(
                        rows[intent]["exchange_order_id"], request.symbol, oco=False
                    ),
                )
            )
        else:
            known_clients.update((protections[intent]["above_id"], protections[intent]["below_id"]))
            known_list_ids.add(
                (
                    request.symbol,
                    _parse_exchange_id(rows[intent]["exchange_order_id"], request.symbol, oco=True),
                )
            )
    for evidence in remote_evidence:
        if not isinstance(evidence, tuple) or not evidence or evidence[0] != "oco":
            continue
        children = evidence[3]
        if not isinstance(children, tuple):
            _fail("migration_evidence_mismatch")
        for child_evidence in children:
            if not isinstance(child_evidence, tuple) or len(child_evidence) != 2:
                _fail("migration_evidence_mismatch")
            child = _mapping(child_evidence[0])
            symbol = child.get("symbol")
            if not isinstance(symbol, str):
                _fail("migration_evidence_mismatch")
            known_ids.add((symbol, _integer(child.get("orderId"))))
    unrelated = 0
    seen_open_ids: set[tuple[str, int]] = set()
    seen_open_clients: set[tuple[str, str]] = set()
    for value in raw_open:
        item = _mapping(value)
        symbol = item.get("symbol")
        client = item.get("clientOrderId")
        order_id = _integer(item.get("orderId"))
        order_list_id = item.get("orderListId")
        if not isinstance(symbol, str) or not isinstance(client, str):
            _fail("migration_evidence_mismatch")
        if (
            isinstance(order_list_id, bool)
            or not isinstance(order_list_id, int)
            or order_list_id < -1
        ):
            _fail("migration_evidence_mismatch")
        order_key = (symbol, order_id)
        client_key = (symbol, client)
        if order_key in seen_open_ids or client_key in seen_open_clients:
            _fail("migration_evidence_mismatch")
        seen_open_ids.add(order_key)
        seen_open_clients.add(client_key)
        if (
            client in known_clients
            or order_key in known_ids
            or (symbol, order_list_id) in known_list_ids
            or _OWNED_NAMESPACE.fullmatch(client)
        ):
            _fail("migration_remote_open_orders")
        unrelated += 1
    if await reader.account_uid() != uid:
        _fail("migration_identity_changed")
    reader.ensure_fresh()
    return LegacyMigrationEvidence(
        snapshot.marker,
        snapshot.digest,
        _canonical_hash(remote_evidence),
        uid,
        reader._clock_ms(),
        len(snapshot.orders),
        len(snapshot.fills),
        len(snapshot.cycles),
        unrelated,
    )


async def migrate_legacy(
    store: OrderStore, reader: LegacyTestnetReader, *, apply: bool = False
) -> dict[str, object]:
    """Preview or atomically apply a freshly verified legacy-ledger binding."""

    evidence = await verify_legacy_migration(store, reader)
    if not apply:
        return evidence.diagnostic()
    with store._writer() as conn:
        reader.ensure_fresh()
        current = _read_local_snapshot(conn)
        if current.marker != evidence.local_marker:
            _fail("migration_ledger_changed")
        if current.binding_count or current.incident_count or current.receipt_count:
            _fail("migration_ledger_changed")
        conn.execute(
            text(
                "CREATE TABLE ledger_account_migrations ("
                "singleton_id INTEGER PRIMARY KEY,version INTEGER NOT NULL,"
                "before_ledger_sha256 VARCHAR(64) NOT NULL,"
                "remote_evidence_sha256 VARCHAR(64) NOT NULL,"
                "verified_at_ms BIGINT NOT NULL,applied_at_ms BIGINT NOT NULL,"
                "order_count INTEGER NOT NULL,fill_count INTEGER NOT NULL,"
                "cycle_count INTEGER NOT NULL)"
            )
        )
        applied_at = store._millis()
        conn.execute(
            text(
                "INSERT INTO ledger_account_binding "
                "(singleton_id,venue,account_uid,created_at) VALUES (1,:venue,:uid,:at)"
            ),
            {
                "venue": TESTNET_ACCOUNT_VENUE,
                "uid": str(evidence.account_uid),
                "at": applied_at,
            },
        )
        conn.execute(
            text(
                "INSERT INTO ledger_account_migrations "
                "(singleton_id,version,before_ledger_sha256,remote_evidence_sha256,"
                "verified_at_ms,applied_at_ms,order_count,fill_count,cycle_count) VALUES "
                "(1,:version,:local,:remote,:verified,:applied,:orders,:fills,:cycles)"
            ),
            {
                "version": MIGRATION_VERSION,
                "local": evidence.before_ledger_sha256,
                "remote": evidence.remote_evidence_sha256,
                "verified": evidence.verified_at_ms,
                "applied": applied_at,
                "orders": evidence.order_count,
                "fills": evidence.fill_count,
                "cycles": evidence.cycle_count,
            },
        )
    return evidence.diagnostic(applied=True)
