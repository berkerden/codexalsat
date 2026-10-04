"""Authenticated Binance Global Spot Testnet transport.

The transport is deliberately pinned to the Spot Testnet origin.  It owns its
HTTP client, signs the exact form-encoded payload sent on the wire, and never
retries an order mutation whose outcome may be unknown.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Final, Literal, cast
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from spotlab.orders import (
    ConfirmedFill,
    ExchangeFilters,
    ExchangeUpdate,
    OrderError,
    OrderInvariantError,
    OrderRequest,
    OrderState,
    RetryableTransportError,
    validate_order_filters,
)

TESTNET_BASE_URL: Final = "https://testnet.binance.vision"
SUPPORTED_SYMBOLS: Final = frozenset({"BTCUSDT", "SOLUSDT"})
CLIENT_ORDER_ID_RE: Final = re.compile(r"^[A-Za-z0-9._:/-]{1,36}$")
RECV_WINDOW_MS: Final = 5_000
TRADE_PAGE_SIZE: Final = 1_000
_AMBIGUOUS_CODES: Final = frozenset({-1006, -1007})
_METHODS: Final = frozenset({"GET", "POST", "DELETE"})
_KNOWN_FILTER_TYPES: Final = frozenset(
    {
        "PRICE_FILTER",
        "LOT_SIZE",
        "ICEBERG_PARTS",
        "MARKET_LOT_SIZE",
        "TRAILING_DELTA",
        "PERCENT_PRICE_BY_SIDE",
        "NOTIONAL",
        "MAX_NUM_ORDERS",
        "MAX_NUM_ORDER_LISTS",
        "MAX_NUM_ALGO_ORDERS",
        "MAX_NUM_ORDER_AMENDS",
        "PERCENT_PRICE",
        "MIN_NOTIONAL",
        "MAX_POSITION",
    }
)
_UNEVALUATED_SUBMISSION_FILTERS: Final = frozenset(
    {
        "MIN_NOTIONAL",
        "MARKET_LOT_SIZE",
        "PERCENT_PRICE_BY_SIDE",
        "MAX_NUM_ORDERS",
        "MAX_NUM_ORDER_LISTS",
        "MAX_NUM_ALGO_ORDERS",
        "MAX_NUM_ORDER_AMENDS",
        "PERCENT_PRICE",
        "MAX_POSITION",
    }
)
_ALLOWED_ENDPOINTS: Final = frozenset(
    {
        ("GET", "/api/v3/time"),
        ("GET", "/api/v3/exchangeInfo"),
        ("GET", "/api/v3/ticker/bookTicker"),
        ("GET", "/api/v3/ticker/price"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/account/commission"),
        ("GET", "/api/v3/order"),
        ("GET", "/api/v3/myTrades"),
        ("GET", "/api/v3/orderList"),
        ("POST", "/api/v3/order"),
        ("POST", "/api/v3/order/test"),
        ("POST", "/api/v3/orderList/oco"),
        ("DELETE", "/api/v3/order"),
        ("DELETE", "/api/v3/orderList"),
    }
)


class TestnetCredentials(BaseModel):
    """Spot Testnet HMAC credentials whose representation is always redacted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr
    secret: SecretStr

    @field_validator("api_key", "secret")
    @classmethod
    def _not_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("testnet credentials cannot be empty")
        return value

    @classmethod
    def from_env(cls) -> TestnetCredentials:
        api_key = os.getenv("SPOTLAB_TESTNET_API_KEY")
        secret = os.getenv("SPOTLAB_TESTNET_API_SECRET")
        if api_key is None or secret is None:
            raise ValueError(
                "SPOTLAB_TESTNET_API_KEY and SPOTLAB_TESTNET_API_SECRET are required"
            )
        return cls.model_validate({"api_key": api_key, "secret": secret})


class TestnetAPIError(RuntimeError):
    """A sanitized, deterministic error returned by the Spot Testnet API."""

    def __init__(self, status_code: int, code: int | None) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(f"Spot Testnet rejected request (HTTP {status_code}, code {code})")


@dataclass(frozen=True)
class SymbolMetadata:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    server_time_ms: int
    filters: ExchangeFilters
    min_price: Decimal
    max_price: Decimal
    order_types: tuple[str, ...]
    oco_allowed: bool
    filter_types: tuple[str, ...]


@dataclass(frozen=True)
class TestnetQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at_ms: int


@dataclass(frozen=True)
class AssetBalance:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True)
class CommissionRates:
    maker: Decimal
    taker: Decimal
    buyer: Decimal
    seller: Decimal


@dataclass(frozen=True)
class AccountSnapshot:
    can_trade: bool
    balances: tuple[AssetBalance, ...]
    standard_commission: CommissionRates
    observed_at_ms: int

    def balance(self, asset: str) -> AssetBalance:
        for balance in self.balances:
            if balance.asset == asset:
                return balance
        return AssetBalance(asset, Decimal(0), Decimal(0))


def _form_encode(params: Mapping[str, object]) -> str:
    """Encode once before signing, matching the bytes sent to Binance."""

    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            raise ValueError("request parameter names must be non-empty strings")
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (str, int, Decimal)):
            rendered = str(value)
        else:
            raise TypeError(f"unsupported request parameter type for {key}")
        pairs.append((key, rendered))
    return urlencode(pairs, doseq=False, quote_via=quote, safe="")


def _signature(payload: str, secret: SecretStr) -> str:
    return hmac.new(
        secret.get_secret_value().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _response_decimal(value: object, name: str, *, allow_zero: bool = True) -> Decimal:
    if not isinstance(value, str):
        raise OrderInvariantError(f"testnet {name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise OrderInvariantError(f"testnet {name} is not decimal") from error
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise OrderInvariantError(f"testnet {name} is outside the allowed range")
    return parsed


def _response_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OrderInvariantError(f"testnet {name} must be a non-negative integer")
    return value


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise OrderInvariantError(f"testnet {name} must be an object")
    return cast(dict[str, Any], value)


def validate_submission_metadata(metadata: SymbolMetadata) -> None:
    """Fail closed when fresh rules cannot all be evaluated for a mutation."""

    if metadata.status != "TRADING":
        raise OrderError("bound testnet symbol is not trading")
    if "LIMIT" not in metadata.order_types:
        raise OrderError("bound testnet symbol does not allow LIMIT orders")
    unknown = set(metadata.filter_types) - _KNOWN_FILTER_TYPES
    if unknown:
        raise OrderError("exchangeInfo contains unsupported symbol filters")
    if set(metadata.filter_types) & _UNEVALUATED_SUBMISSION_FILTERS:
        raise OrderError("exchangeInfo contains filters not evaluated for safe submission")


class BinanceTestnetTransport:
    """A symbol-bound implementation of :class:`OrderTransport` for Spot Testnet."""

    def __init__(
        self,
        symbol: Literal["BTCUSDT", "SOLUSDT"],
        *,
        credentials: TestnetCredentials | None = None,
        mock_transport: httpx.MockTransport | None = None,
        timeout_s: float = 10.0,
        clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("testnet transport symbol must be BTCUSDT or SOLUSDT")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if mock_transport is not None and not isinstance(mock_transport, httpx.MockTransport):
            raise TypeError("only httpx.MockTransport may be injected")
        self.symbol = symbol
        self._credentials = credentials or TestnetCredentials.from_env()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic = monotonic or time.monotonic
        self._clock_offset_ms = 0
        self._cooldown_until = 0.0
        self._metadata: SymbolMetadata | None = None
        self._expected: dict[str, OrderRequest] = {}
        self._client = httpx.AsyncClient(
            base_url=TESTNET_BASE_URL,
            timeout=httpx.Timeout(float(timeout_s)),
            follow_redirects=False,
            trust_env=False,
            transport=mock_transport,
        )

    async def __aenter__(self) -> BinanceTestnetTransport:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _timestamp(self) -> int:
        return self._clock_ms() + self._clock_offset_ms

    def _check_cooldown(self) -> None:
        remaining = self._cooldown_until - self._monotonic()
        if remaining > 0:
            raise RetryableTransportError(
                f"Spot Testnet rate-limit cooldown is active for {remaining:.3f} seconds"
            )

    def _set_cooldown(self, response: httpx.Response) -> None:
        raw = response.headers.get("Retry-After")
        seconds = 60.0
        if raw is not None:
            try:
                seconds = max(0.0, float(raw))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(raw).timestamp()
                    seconds = max(0.0, retry_at - time.time())
                except (TypeError, ValueError, OverflowError):
                    seconds = 60.0
        self._cooldown_until = max(self._cooldown_until, self._monotonic() + seconds)

    @staticmethod
    def _validate_path(path: str) -> None:
        if (
            not path.startswith("/api/v3/")
            or "?" in path
            or "#" in path
            or "//" in path
            or ".." in path
        ):
            raise ValueError("path must be a relative /api/v3 endpoint")

    async def request(
        self,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        signed: bool = True,
    ) -> dict[str, Any] | list[Any]:
        """Call a fixed-host API endpoint once and return decoded JSON.

        This low-level surface exists for protection-list operations.  It still
        enforces the bound symbol and cannot select another origin.
        """

        method = method.upper()  # type: ignore[assignment]
        if method not in _METHODS:
            raise ValueError("method must be GET, POST, or DELETE")
        self._validate_path(path)
        if (method, path) not in _ALLOWED_ENDPOINTS:
            raise ValueError("endpoint is not in the Spot Testnet transport allowlist")
        values: dict[str, object] = dict(params or {})
        supplied_symbol = values.get("symbol")
        if supplied_symbol is not None and supplied_symbol != self.symbol:
            raise OrderError("request symbol does not match transport symbol")
        if signed:
            if "signature" in values or "timestamp" in values:
                raise ValueError("signature and timestamp are managed by the transport")
            values["recvWindow"] = RECV_WINDOW_MS
            values["timestamp"] = self._timestamp()
        payload = _form_encode(values)
        if signed:
            payload = f"{payload}&signature={_signature(payload, self._credentials.secret)}"
        self._check_cooldown()
        headers: dict[str, str] = {}
        if signed:
            headers["X-MBX-APIKEY"] = self._credentials.api_key.get_secret_value()
        request_path = path
        content: bytes | None = None
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            content = payload.encode("ascii")
        elif payload:
            request_path = f"{path}?{payload}"
        try:
            response = await self._client.request(
                method, request_path, headers=headers, content=content
            )
        except httpx.TimeoutException:
            raise RetryableTransportError(
                f"Spot Testnet {method} request timed out; outcome may be unknown"
            ) from None
        except httpx.TransportError:
            raise RetryableTransportError(
                f"Spot Testnet {method} transport failed; outcome may be unknown"
            ) from None

        if response.status_code in {418, 429}:
            self._set_cooldown(response)
            raise RetryableTransportError(
                f"Spot Testnet rate limited request (HTTP {response.status_code})"
            )
        if 300 <= response.status_code < 400:
            raise TestnetAPIError(response.status_code, None)
        try:
            decoded: object = response.json()
        except ValueError as error:
            if response.status_code >= 500:
                raise RetryableTransportError(
                    f"Spot Testnet {method} returned HTTP {response.status_code}; "
                    "outcome may be unknown"
                ) from error
            raise OrderInvariantError("Spot Testnet returned malformed JSON") from error
        error_code: int | None = None
        if isinstance(decoded, dict):
            raw_code = decoded.get("code")
            if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                error_code = raw_code
        if response.status_code >= 500 or error_code in _AMBIGUOUS_CODES:
            raise RetryableTransportError(
                f"Spot Testnet {method} outcome may be unknown "
                f"(HTTP {response.status_code}, code {error_code})"
            )
        if response.status_code >= 400 or (error_code is not None and error_code < 0):
            raise TestnetAPIError(response.status_code, error_code)
        if not isinstance(decoded, (dict, list)):
            raise OrderInvariantError("Spot Testnet JSON response has an invalid shape")
        return cast(dict[str, Any] | list[Any], decoded)

    async def _sync_time(self) -> int:
        before = self._clock_ms()
        raw = await self.request("GET", "/api/v3/time", signed=False)
        after = self._clock_ms()
        data = _mapping(raw, "server-time response")
        server_time = _response_int(data.get("serverTime"), "serverTime")
        self._clock_offset_ms = server_time - ((before + after) // 2)
        return server_time

    async def exchange_info(self) -> SymbolMetadata:
        """Fetch fresh server time and trading filters for the bound symbol."""

        server_time = await self._sync_time()
        raw = await self.request(
            "GET", "/api/v3/exchangeInfo", {"symbol": self.symbol}, signed=False
        )
        data = _mapping(raw, "exchangeInfo response")
        symbols = data.get("symbols")
        if not isinstance(symbols, list) or len(symbols) != 1:
            raise OrderInvariantError("exchangeInfo did not return exactly one symbol")
        symbol = _mapping(symbols[0], "exchangeInfo symbol")
        if symbol.get("symbol") != self.symbol:
            raise OrderInvariantError("exchangeInfo symbol does not match transport")
        base_asset = symbol.get("baseAsset")
        quote_asset = symbol.get("quoteAsset")
        status = symbol.get("status")
        if not all(isinstance(item, str) and item for item in (base_asset, quote_asset, status)):
            raise OrderInvariantError("exchangeInfo asset metadata is malformed")
        raw_order_types = symbol.get("orderTypes")
        if not isinstance(raw_order_types, list) or not all(
            isinstance(item, str) for item in raw_order_types
        ):
            raise OrderInvariantError("exchangeInfo order types are malformed")
        order_types = tuple(cast(list[str], raw_order_types))
        oco_allowed = symbol.get("ocoAllowed")
        if not isinstance(oco_allowed, bool):
            raise OrderInvariantError("exchangeInfo ocoAllowed is malformed")
        filters = symbol.get("filters")
        if not isinstance(filters, list):
            raise OrderInvariantError("exchangeInfo filters are malformed")
        indexed: dict[str, dict[str, Any]] = {}
        for item in filters:
            current = _mapping(item, "exchangeInfo filter")
            filter_type = current.get("filterType")
            if not isinstance(filter_type, str) or not filter_type:
                raise OrderInvariantError("exchangeInfo filter type is malformed")
            if filter_type in indexed:
                raise OrderInvariantError("exchangeInfo filter type is duplicated")
            indexed[filter_type] = current
        try:
            price_filter = indexed["PRICE_FILTER"]
            lot_filter = indexed["LOT_SIZE"]
            notional_filter = indexed["NOTIONAL"]
        except KeyError as error:
            raise OrderInvariantError(
                "exchangeInfo requires PRICE_FILTER, LOT_SIZE, and NOTIONAL filters"
            ) from error
        min_price = _response_decimal(price_filter.get("minPrice"), "minimum price")
        max_price = _response_decimal(
            price_filter.get("maxPrice"), "maximum price", allow_zero=False
        )
        minimum_notional = _response_decimal(
            notional_filter.get("minNotional"), "minimum notional", allow_zero=False
        )
        raw_max_notional = _response_decimal(
            notional_filter.get("maxNotional"), "maximum notional"
        )
        metadata = SymbolMetadata(
            symbol=self.symbol,
            base_asset=cast(str, base_asset),
            quote_asset=cast(str, quote_asset),
            status=cast(str, status),
            server_time_ms=server_time,
            filters=ExchangeFilters(
                tick_size=_response_decimal(
                    price_filter.get("tickSize"), "tick size", allow_zero=False
                ),
                step_size=_response_decimal(
                    lot_filter.get("stepSize"), "step size", allow_zero=False
                ),
                min_quantity=_response_decimal(
                    lot_filter.get("minQty"), "minimum quantity", allow_zero=False
                ),
                max_quantity=_response_decimal(
                    lot_filter.get("maxQty"), "maximum quantity", allow_zero=False
                ),
                min_notional=minimum_notional,
                max_notional=raw_max_notional or None,
            ),
            min_price=min_price,
            max_price=max_price,
            order_types=order_types,
            oco_allowed=oco_allowed,
            filter_types=tuple(indexed),
        )
        if metadata.min_price > metadata.max_price:
            raise OrderInvariantError("exchangeInfo price bounds are inverted")
        self._metadata = metadata
        return metadata

    async def metadata(self) -> SymbolMetadata:
        """Compatibility name for a fresh bound-symbol metadata snapshot."""

        return await self.exchange_info()

    async def quote(self) -> TestnetQuote:
        raw = await self.request(
            "GET", "/api/v3/ticker/bookTicker", {"symbol": self.symbol}, signed=False
        )
        data = _mapping(raw, "bookTicker response")
        if data.get("symbol") != self.symbol:
            raise OrderInvariantError("bookTicker symbol does not match transport")
        bid = _response_decimal(data.get("bidPrice"), "bid price", allow_zero=False)
        ask = _response_decimal(data.get("askPrice"), "ask price", allow_zero=False)
        if bid > ask:
            raise OrderInvariantError("bookTicker is crossed")
        return TestnetQuote(self.symbol, bid, ask, self._clock_ms())

    async def account_snapshot(self) -> AccountSnapshot:
        """Return only bound-symbol balances and sanitized standard commission rates."""

        if self._metadata is None:
            await self.exchange_info()
        else:
            await self._sync_time()
        account_raw = await self.request("GET", "/api/v3/account")
        commission_raw = await self.request(
            "GET", "/api/v3/account/commission", {"symbol": self.symbol}
        )
        account = _mapping(account_raw, "account response")
        commission = _mapping(commission_raw, "commission response")
        can_trade = account.get("canTrade")
        if not isinstance(can_trade, bool):
            raise OrderInvariantError("account canTrade is malformed")
        metadata = self._metadata
        if metadata is None:
            raise OrderInvariantError("exchange metadata is required before account preflight")
        balances_raw = account.get("balances")
        if not isinstance(balances_raw, list):
            raise OrderInvariantError("account balances are malformed")
        wanted = {metadata.base_asset, metadata.quote_asset}
        balances: dict[str, AssetBalance] = {}
        for item in balances_raw:
            balance = _mapping(item, "account balance")
            asset = balance.get("asset")
            if asset in wanted:
                if not isinstance(asset, str) or asset in balances:
                    raise OrderInvariantError("account balance asset is malformed or duplicated")
                balances[asset] = AssetBalance(
                    asset,
                    _response_decimal(balance.get("free"), "free balance"),
                    _response_decimal(balance.get("locked"), "locked balance"),
                )
        standard = _mapping(commission.get("standardCommission"), "standard commission")
        if commission.get("symbol") != self.symbol:
            raise OrderInvariantError("commission symbol does not match transport")
        rates = CommissionRates(
            maker=_response_decimal(standard.get("maker"), "maker commission"),
            taker=_response_decimal(standard.get("taker"), "taker commission"),
            buyer=_response_decimal(standard.get("buyer"), "buyer commission"),
            seller=_response_decimal(standard.get("seller"), "seller commission"),
        )
        ordered = tuple(
            balances.get(asset, AssetBalance(asset, Decimal(0), Decimal(0)))
            for asset in (metadata.base_asset, metadata.quote_asset)
        )
        return AccountSnapshot(can_trade, ordered, rates, self._clock_ms())

    @staticmethod
    def _validate_client_id(client_order_id: str) -> None:
        if not CLIENT_ORDER_ID_RE.fullmatch(client_order_id):
            raise OrderError(
                "client_order_id must be 1 to 36 Binance-safe ASCII characters"
            )

    async def submit(self, order: OrderRequest) -> ExchangeUpdate:
        order.validate()
        if order.symbol != self.symbol:
            raise OrderError("order symbol does not match transport symbol")
        if order.order_type != "LIMIT":
            raise OrderError("testnet transport only submits LIMIT orders")
        self._validate_client_id(order.client_order_id)
        metadata = await self.exchange_info()
        validate_submission_metadata(metadata)
        if order.quote_asset != metadata.quote_asset:
            raise OrderError("order quote asset does not match exchange metadata")
        if order.price < metadata.min_price or order.price > metadata.max_price:
            raise OrderError("price is outside exchange limits")
        validate_order_filters(order.quantity, order.price, metadata.filters)
        snapshot = await self.account_snapshot()
        if not snapshot.can_trade:
            raise OrderError("Spot Testnet account is not permitted to trade")
        required_asset = metadata.quote_asset if order.side == "BUY" else metadata.base_asset
        required = order.quantity * order.price if order.side == "BUY" else order.quantity
        if snapshot.balance(required_asset).free < required:
            raise OrderError("Spot Testnet account has insufficient available balance")
        self._expected[order.client_order_id] = order
        raw = await self.request(
            "POST",
            "/api/v3/order",
            {
                "symbol": self.symbol,
                "side": order.side,
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": order.quantity,
                "price": order.price,
                "newClientOrderId": order.client_order_id,
                "newOrderRespType": "RESULT",
            },
        )
        return await self._parse_order(_mapping(raw, "new-order response"), order.client_order_id)

    async def query(self, client_order_id: str) -> ExchangeUpdate:
        self._validate_client_id(client_order_id)
        if self._metadata is None:
            await self.exchange_info()
        else:
            await self._sync_time()
        raw = await self.request(
            "GET",
            "/api/v3/order",
            {"symbol": self.symbol, "origClientOrderId": client_order_id},
        )
        return await self._parse_order(_mapping(raw, "query-order response"), client_order_id)

    async def cancel(self, client_order_id: str) -> ExchangeUpdate:
        self._validate_client_id(client_order_id)
        if self._metadata is None:
            await self.exchange_info()
        else:
            await self._sync_time()
        raw = await self.request(
            "DELETE",
            "/api/v3/order",
            {"symbol": self.symbol, "origClientOrderId": client_order_id},
        )
        return await self._parse_order(_mapping(raw, "cancel-order response"), client_order_id)

    async def _parse_order(
        self, data: dict[str, Any], expected_client_order_id: str
    ) -> ExchangeUpdate:
        if data.get("symbol") != self.symbol:
            raise OrderInvariantError("order response symbol does not match transport")
        response_client_id = data.get("origClientOrderId", data.get("clientOrderId"))
        if response_client_id != expected_client_order_id:
            raise OrderInvariantError("order response client ID does not match request")
        order_id = _response_int(data.get("orderId"), "orderId")
        side = data.get("side")
        if side not in {"BUY", "SELL"}:
            raise OrderInvariantError("order response side is malformed")
        original_quantity = _response_decimal(
            data.get("origQty"), "original quantity", allow_zero=False
        )
        order_type = data.get("type")
        if order_type not in {
            "LIMIT",
            "MARKET",
            "STOP_LOSS",
            "STOP_LOSS_LIMIT",
            "TAKE_PROFIT",
            "TAKE_PROFIT_LIMIT",
            "LIMIT_MAKER",
        }:
            raise OrderInvariantError("order response type is malformed")
        limit_price = _response_decimal(data.get("price"), "order price")
        if order_type == "LIMIT" and limit_price == 0:
            raise OrderInvariantError("LIMIT order response price must be positive")
        time_in_force = data.get("timeInForce")
        if not isinstance(time_in_force, str) or not time_in_force:
            raise OrderInvariantError("order response timeInForce is malformed")
        if order_type == "LIMIT" and time_in_force != "GTC":
            raise OrderInvariantError("LIMIT order response timeInForce must be GTC")
        expected = self._expected.get(expected_client_order_id)
        if expected is not None:
            if side != expected.side:
                raise OrderInvariantError("order response side conflicts with submitted order")
            if original_quantity != expected.quantity:
                raise OrderInvariantError("order response quantity conflicts with submitted order")
            if order_type != expected.order_type:
                raise OrderInvariantError("order response type conflicts with submitted order")
            if limit_price != expected.price:
                raise OrderInvariantError("order response price conflicts with submitted order")
        raw_status = data.get("status")
        statuses = {
            "NEW": OrderState.NEW,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
            "REJECTED": OrderState.REJECTED,
            "EXPIRED": OrderState.CANCELED,
            "EXPIRED_IN_MATCH": OrderState.CANCELED,
        }
        if not isinstance(raw_status, str) or raw_status not in statuses:
            raise OrderInvariantError("order response status is unsupported or pending")
        cumulative = _response_decimal(data.get("executedQty"), "executed quantity")
        fills = await self._trades(order_id)
        fill_total = sum((fill.quantity for fill in fills), Decimal(0))
        if fill_total > cumulative:
            raise OrderInvariantError("trade quantities exceed order executed quantity")
        if statuses[raw_status] is OrderState.FILLED and fill_total != cumulative:
            raise OrderInvariantError("FILLED order is missing confirmed trades")
        return ExchangeUpdate(
            client_order_id=expected_client_order_id,
            exchange_order_id=f"{self.symbol}:{order_id}",
            status=statuses[raw_status],
            cumulative_quantity=cumulative,
            fills=tuple(fills),
            symbol=self.symbol,
            side=cast(str, side),
            original_quantity=original_quantity,
            order_type=cast(str, order_type),
            limit_price=limit_price,
            time_in_force=time_in_force,
        )

    async def _trades(self, order_id: int) -> list[ConfirmedFill]:
        by_id: dict[int, ConfirmedFill] = {}
        from_id = 0
        while True:
            raw = await self.request(
                "GET",
                "/api/v3/myTrades",
                {
                    "symbol": self.symbol,
                    "orderId": order_id,
                    "fromId": from_id,
                    "limit": TRADE_PAGE_SIZE,
                },
            )
            if not isinstance(raw, list):
                raise OrderInvariantError("myTrades response must be a list")
            highest = from_id - 1
            for item in raw:
                trade = _mapping(item, "trade")
                if trade.get("symbol") != self.symbol:
                    raise OrderInvariantError("trade symbol does not match order")
                if _response_int(trade.get("orderId"), "trade orderId") != order_id:
                    raise OrderInvariantError("trade order ID does not match order")
                trade_id = _response_int(trade.get("id"), "trade ID")
                _response_decimal(trade.get("price"), "trade price", allow_zero=False)
                quantity = _response_decimal(
                    trade.get("qty"), "trade quantity", allow_zero=False
                )
                quote_quantity = _response_decimal(
                    trade.get("quoteQty"), "trade quote quantity", allow_zero=False
                )
                commission = _response_decimal(trade.get("commission"), "trade commission")
                commission_asset = trade.get("commissionAsset")
                metadata = self._metadata
                if metadata is None or not isinstance(commission_asset, str):
                    raise OrderInvariantError("trade commission metadata is unavailable")
                fee_base = Decimal(0)
                if commission_asset == metadata.quote_asset:
                    fee_quote = commission
                elif commission_asset == metadata.base_asset:
                    fee_base = commission
                    fee_quote = commission * quote_quantity / quantity
                else:
                    raise OrderInvariantError("trade commission uses an unsupported asset")
                parsed = ConfirmedFill(
                    trade_id=f"{self.symbol}:{trade_id}",
                    quantity=quantity,
                    quote_quantity=quote_quantity,
                    fee_quote=fee_quote,
                    fee_asset=commission_asset,
                    fee_base=fee_base,
                )
                prior = by_id.get(trade_id)
                if prior is not None and prior != parsed:
                    raise OrderInvariantError("duplicate trade ID has conflicting data")
                by_id[trade_id] = parsed
                highest = max(highest, trade_id)
            if len(raw) < TRADE_PAGE_SIZE:
                break
            if highest < from_id:
                raise OrderInvariantError("myTrades pagination did not advance")
            from_id = highest + 1
        return [by_id[key] for key in sorted(by_id)]
