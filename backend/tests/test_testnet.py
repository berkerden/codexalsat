import hashlib
import hmac
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from spotlab.orders import (
    ExchangeFilters,
    OrderInvariantError,
    OrderLifecycle,
    OrderRequest,
    OrderState,
    RetryableTransportError,
)
from spotlab.testnet import (
    TESTNET_BASE_URL,
    BinanceTestnetTransport,
    _form_encode,
    _signature,
)
from spotlab.testnet import (
    TestnetAPIError as APIError,
)
from spotlab.testnet import (
    TestnetCredentials as Credentials,
)

NOW = 1_700_000_000_000
SECRET = "fixture-secret"
KEY = "test-api-key-that-must-never-leak"


def credentials() -> Credentials:
    return Credentials.model_validate({"api_key": KEY, "secret": SECRET})


def exchange_info() -> dict[str, object]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS_LIMIT"],
                "ocoAllowed": True,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "1.00",
                        "maxPrice": "1000000.00",
                        "tickSize": "0.10",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "100.000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": "5.00",
                        "maxNotional": "10000000.00",
                    },
                ],
            }
        ]
    }


def account() -> dict[str, object]:
    return {
        "canTrade": True,
        "balances": [
            {"asset": "BTC", "free": "5.0", "locked": "0.0"},
            {"asset": "USDT", "free": "100000.0", "locked": "0.0"},
            {"asset": "SHOULD_NOT_ESCAPE", "free": "9", "locked": "0"},
        ],
    }


def commission() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "standardCommission": {
            "maker": "0.001",
            "taker": "0.001",
            "buyer": "0",
            "seller": "0",
        },
    }


def order_response(
    client_id: str,
    *,
    status: str = "NEW",
    executed: str = "0",
    order_id: int = 42,
    quantity: str = "0.010",
) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "orderId": order_id,
        "clientOrderId": client_id,
        "side": "BUY",
        "type": "LIMIT",
        "price": "1000.00",
        "timeInForce": "GTC",
        "origQty": quantity,
        "executedQty": executed,
        "status": status,
    }


def request(client_id: str = "spotlab-safe-id") -> OrderRequest:
    return OrderRequest(
        intent_id="intent-1",
        client_order_id=client_id,
        symbol="BTCUSDT",
        side="BUY",
        quantity=D("0.010"),
        price=D("1000.00"),
        quote_asset="USDT",
        inventory_scope="scope-1",
    )


def local_filters() -> ExchangeFilters:
    return ExchangeFilters(D("0.10"), D("0.001"), D("0.001"), D("100"), D("5"))


def response_for_basics(http_request: httpx.Request) -> httpx.Response | None:
    path = http_request.url.path
    if path == "/api/v3/time":
        return httpx.Response(200, json={"serverTime": NOW})
    if path == "/api/v3/exchangeInfo":
        return httpx.Response(200, json=exchange_info())
    if path == "/api/v3/account":
        return httpx.Response(200, json=account())
    if path == "/api/v3/account/commission":
        return httpx.Response(200, json=commission())
    return None


def test_credentials_are_secret_and_environment_names_are_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPOTLAB_TESTNET_API_KEY", KEY)
    monkeypatch.setenv("SPOTLAB_TESTNET_API_SECRET", SECRET)
    loaded = Credentials.from_env()
    rendered = repr(loaded)
    assert KEY not in rendered
    assert SECRET not in rendered
    assert "**********" in rendered


def test_hmac_known_vector_and_percent_encoded_wire_payload() -> None:
    message = "The quick brown fox jumps over the lazy dog"
    assert _signature(message, SecretStr("key")) == (
        "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    )
    assert _form_encode({"symbol": "BTC USDT", "id": "a/b+c", "price": D("1.20")}) == (
        "symbol=BTC%20USDT&id=a%2Fb%2Bc&price=1.20"
    )


@pytest.mark.asyncio
async def test_request_is_fixed_host_signed_once_and_client_is_internal() -> None:
    seen: list[httpx.Request] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return httpx.Response(200, json={})

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    )
    await transport.request("POST", "/api/v3/order/test", {"symbol": "BTCUSDT", "memo": "a/b c"})
    await transport.aclose()
    assert len(seen) == 1
    sent = seen[0]
    assert f"{sent.url.scheme}://{sent.url.host}" == TESTNET_BASE_URL
    assert sent.headers["X-MBX-APIKEY"] == KEY
    body = sent.content.decode()
    unsigned, signature = body.rsplit("&signature=", 1)
    assert "memo=a%2Fb%20c" in unsigned
    assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(TypeError, match="MockTransport"):
        BinanceTestnetTransport(  # type: ignore[arg-type]
            "BTCUSDT", credentials=credentials(), mock_transport=httpx.AsyncHTTPTransport()
        )


@pytest.mark.asyncio
async def test_submit_runs_fresh_filter_account_and_commission_preflight() -> None:
    paths: list[str] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        paths.append(http_request.url.path)
        basic = response_for_basics(http_request)
        if basic is not None:
            return basic
        if http_request.url.path == "/api/v3/order":
            params = parse_qs(http_request.content.decode())
            assert params["newClientOrderId"] == ["spotlab-safe-id"]
            assert params["quantity"] == ["0.010"]
            return httpx.Response(200, json=order_response("spotlab-safe-id"))
        if http_request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=[])
        raise AssertionError(http_request.url.path)

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    )
    result = await transport.submit(request())
    snapshot = await transport.account_snapshot()
    await transport.aclose()
    assert result.status is OrderState.NEW
    assert result.exchange_order_id == "BTCUSDT:42"
    assert [balance.asset for balance in snapshot.balances] == ["BTC", "USDT"]
    assert paths[:6] == [
        "/api/v3/time",
        "/api/v3/exchangeInfo",
        "/api/v3/time",
        "/api/v3/account",
        "/api/v3/account/commission",
        "/api/v3/order",
    ]


@pytest.mark.asyncio
async def test_submit_timeout_is_never_resent_by_lifecycle(tmp_path: Path) -> None:
    posts = 0

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        basic = response_for_basics(http_request)
        if basic is not None:
            return basic
        if http_request.url.path == "/api/v3/order":
            posts += 1
            raise httpx.ReadTimeout("wire details must be redacted", request=http_request)
        raise AssertionError(http_request.url.path)

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    )
    lifecycle = OrderLifecycle("sqlite:///" + str(tmp_path / "orders.sqlite"), transport)
    first = await lifecycle.submit(request(), local_filters())
    second = await lifecycle.submit(request(), local_filters())
    await transport.aclose()
    assert first.state is OrderState.UNKNOWN
    assert second.state is OrderState.UNKNOWN
    assert posts == 1
    assert SECRET not in (first.error or "")


@pytest.mark.asyncio
async def test_fresh_transport_restart_query_loads_metadata_and_base_fee() -> None:
    async def handler(http_request: httpx.Request) -> httpx.Response:
        basic = response_for_basics(http_request)
        if basic is not None:
            return basic
        if http_request.url.path == "/api/v3/order":
            return httpx.Response(
                200,
                json=order_response("spotlab-safe-id", status="FILLED", executed="0.010"),
            )
        if http_request.url.path == "/api/v3/myTrades":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "id": 7,
                        "orderId": 42,
                        "price": "1000.00",
                        "qty": "0.010",
                        "quoteQty": "10.00",
                        "commission": "0.00001",
                        "commissionAsset": "BTC",
                    }
                ],
            )
        raise AssertionError(http_request.url.path)

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    )
    result = await transport.query("spotlab-safe-id")
    await transport.aclose()
    assert result.status is OrderState.FILLED
    assert result.symbol == "BTCUSDT"
    assert result.side == "BUY"
    assert result.original_quantity == D("0.010")
    assert result.order_type == "LIMIT"
    assert result.limit_price == D("1000.00")
    assert result.time_in_force == "GTC"
    assert result.fills[0].trade_id == "BTCUSDT:7"
    assert result.fills[0].fee_base == D("0.00001")
    assert result.fills[0].fee_quote == D("0.01")


@pytest.mark.asyncio
async def test_rate_limit_cooldown_blocks_followup_without_network() -> None:
    calls = 0
    monotonic = 100.0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"code": -1003})

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
        monotonic=lambda: monotonic,
    )
    with pytest.raises(RetryableTransportError, match="rate limited"):
        await transport.request("GET", "/api/v3/time", signed=False)
    with pytest.raises(RetryableTransportError, match="cooldown"):
        await transport.request("GET", "/api/v3/time", signed=False)
    await transport.aclose()
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(503, {}), (400, {"code": -1007})])
async def test_ambiguous_server_results_are_retryable(status: int, body: object) -> None:
    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=body)),
    )
    with pytest.raises(RetryableTransportError, match="unknown"):
        await transport.request("POST", "/api/v3/order/test", {"symbol": "BTCUSDT"})
    await transport.aclose()


@pytest.mark.asyncio
async def test_redirect_and_unapproved_or_cross_symbol_routes_fail_closed() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://evil.invalid/steal"})

    transport = BinanceTestnetTransport(
        "BTCUSDT", credentials=credentials(), mock_transport=httpx.MockTransport(handler)
    )
    with pytest.raises(APIError):
        await transport.request("GET", "/api/v3/time", signed=False)
    with pytest.raises(ValueError, match="allowlist"):
        await transport.request("DELETE", "/api/v3/openOrders", {"symbol": "BTCUSDT"})
    with pytest.raises(ValueError, match="relative"):
        await transport.request("GET", "https://evil.invalid/api/v3/time", signed=False)
    with pytest.raises(ValueError, match="symbol"):
        await transport.request("GET", "/api/v3/ticker/price", {"symbol": "SOLUSDT"})
    await transport.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_trade_pagination_deduplicates_and_rejects_malformed_match() -> None:
    def trade(trade_id: int, order_id: int = 42) -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "id": trade_id,
            "orderId": order_id,
            "price": "100.00",
            "qty": "0.001",
            "quoteQty": "0.100",
            "commission": "0",
            "commissionAsset": "USDT",
        }

    malformed = False

    async def handler(http_request: httpx.Request) -> httpx.Response:
        basic = response_for_basics(http_request)
        if basic is not None:
            return basic
        if http_request.url.path == "/api/v3/order":
            return httpx.Response(
                200,
                json=order_response(
                    "spotlab-safe-id", status="PARTIALLY_FILLED", executed="1.001", quantity="2"
                ),
            )
        if http_request.url.path == "/api/v3/myTrades":
            from_id = int(http_request.url.params["fromId"])
            if from_id == 0:
                return httpx.Response(200, json=[trade(index) for index in range(1000)])
            if malformed:
                return httpx.Response(200, json=[trade(1000, order_id=99)])
            return httpx.Response(200, json=[trade(999), trade(1000)])
        raise AssertionError(http_request.url.path)

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    )
    result = await transport.query("spotlab-safe-id")
    assert len(result.fills) == 1001
    malformed = True
    with pytest.raises(OrderInvariantError, match="order ID"):
        await transport.query("spotlab-safe-id")
    await transport.aclose()


@pytest.mark.asyncio
async def test_pending_status_and_malformed_numeric_response_fail_closed() -> None:
    pending = True

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal pending
        basic = response_for_basics(http_request)
        if basic is not None:
            return basic
        if http_request.url.path == "/api/v3/order":
            data = order_response("spotlab-safe-id", status="PENDING_NEW" if pending else "NEW")
            if not pending:
                data["executedQty"] = "NaN"
            return httpx.Response(200, json=data)
        if http_request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=[])
        raise AssertionError(http_request.url.path)

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    )
    with pytest.raises(OrderInvariantError, match="pending"):
        await transport.query("spotlab-safe-id")
    pending = False
    with pytest.raises(OrderInvariantError, match="range"):
        await transport.query("spotlab-safe-id")
    await transport.aclose()


@pytest.mark.asyncio
async def test_halted_symbol_can_be_reconciled_but_cannot_be_submitted() -> None:
    async def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": NOW})
        if http_request.url.path == "/api/v3/exchangeInfo":
            info = exchange_info()
            symbol = info["symbols"][0]  # type: ignore[index]
            symbol["status"] = "HALT"  # type: ignore[index]
            symbol["orderTypes"] = ["MARKET"]  # type: ignore[index]
            return httpx.Response(200, json=info)
        if http_request.url.path == "/api/v3/order":
            return httpx.Response(200, json=order_response("spotlab-safe-id"))
        if http_request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=[])
        raise AssertionError(http_request.url.path)

    transport = BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    )
    reconciled = await transport.query("spotlab-safe-id")
    assert reconciled.status is OrderState.NEW
    with pytest.raises(ValueError, match="not trading"):
        await transport.submit(request())
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_rule", ["MIN_NOTIONAL", "FUTURE_UNSUPPORTED_RULE"])
async def test_unevaluated_rules_block_submit_without_blocking_queries(extra_rule: str) -> None:
    mutations = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method != "GET":
            mutations.append(http_request.url.path)
        if http_request.url.path == "/api/v3/exchangeInfo":
            info = exchange_info()
            info["symbols"][0]["filters"].append(  # type: ignore[index]
                {"filterType": extra_rule, "minNotional": "99999"}
            )
            return httpx.Response(200, json=info)
        basic = response_for_basics(http_request)
        if basic is not None:
            return basic
        if http_request.url.path == "/api/v3/order":
            return httpx.Response(200, json=order_response("spotlab-safe-id"))
        if http_request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=[])
        raise AssertionError(http_request.url.path)

    async with BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=lambda: NOW,
    ) as transport:
        with pytest.raises(ValueError):
            await transport.submit(request())
        assert mutations == []
        assert (await transport.query("spotlab-safe-id")).status is OrderState.NEW
