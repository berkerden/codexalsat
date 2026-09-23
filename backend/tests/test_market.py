from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from spotlab.contracts import Candle
from spotlab.market import (
    BinanceMarket,
    BinanceMarketStream,
    DataQualityError,
    ParquetStore,
    StaleMarketData,
    VenueUnavailable,
    is_fresh,
    unavailable_tr_market,
    validate_candles,
)

MINUTE = 60_000


def candle(open_time: int, *, close: str = "101", high: str = "102") -> Candle:
    return Candle(
        open_time=open_time,
        close_time=open_time + MINUTE - 1,
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal("2.5"),
    )


def row(open_time: int) -> list[object]:
    return [
        open_time,
        "100",
        "102",
        "99",
        "101",
        "2.5",
        open_time + MINUTE - 1,
        "0",
        1,
        "0",
        "0",
        "0",
    ]


@pytest.mark.asyncio
async def test_metadata_quote_and_clock_use_public_contracts() -> None:
    now_values = iter([1_000_000, 1_000_020, 1_000_040])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/exchangeInfo" and "symbol" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {"symbol": "NOPE", "status": "BREAK", "permissions": ["SPOT"]},
                        {"symbol": "BTCUSDT", "status": "TRADING", "permissions": ["SPOT"]},
                        {
                            "symbol": "ETHUSDT",
                            "status": "TRADING",
                            "permissionSets": [["SPOT"]],
                        },
                    ]
                },
            )
        if request.url.path == "/api/v3/exchangeInfo":
            assert request.url.params["symbol"] == "BTCUSDT"
            return httpx.Response(
                200,
                json={"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]},
            )
        if request.url.path == "/api/v3/ticker/bookTicker":
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "bidPrice": "100.1",
                    "bidQty": "2",
                    "askPrice": "100.2",
                    "askQty": "3",
                },
            )
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_000_030})
        raise AssertionError(request.url)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://data-api.binance.vision"
    )
    market = BinanceMarket(client=client, now_ms=lambda: next(now_values))
    assert await market.symbols() == ["BTCUSDT", "ETHUSDT"]
    assert (await market.exchange_info("btcusdt"))["symbol"] == "BTCUSDT"
    quote = await market.quote("BTCUSDT")
    assert quote.bid == Decimal("100.1")
    assert quote.ask == Decimal("100.2")
    assert quote.observed_at == 1_000_000
    clock = await market.clock()
    assert clock.round_trip_ms == 20
    assert clock.offset_ms == 0
    assert clock.synchronized
    await client.aclose()


@pytest.mark.asyncio
async def test_candles_paginate_at_1000_and_exclude_open_candle() -> None:
    calls: list[int] = []
    all_rows = [row(index * MINUTE) for index in range(1_002)]
    server_time = 1_001 * MINUTE + 30_000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": server_time})
        start = int(request.url.params["startTime"])
        calls.append(start)
        available = [item for item in all_rows if start <= int(item[0]) <= 1_001 * MINUTE]
        return httpx.Response(200, json=available[:1_000])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://data-api.binance.vision"
    )
    market = BinanceMarket(client=client)
    result = await market.candles("BTCUSDT", "1m", 0, 1_001 * MINUTE)
    assert len(result) == 1_001
    assert result[-1].open_time == 1_000 * MINUTE
    assert calls == [0, 1_000 * MINUTE]
    await client.aclose()


@pytest.mark.asyncio
async def test_429_retry_after_is_bounded_and_retried() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "99"}, json={})
        return httpx.Response(200, json={"serverTime": 123})

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://data-api.binance.vision"
    )
    market = BinanceMarket(client=client, sleep=sleep, retry_cap_s=2)
    assert await market.server_time() == 123
    assert attempts == 2
    assert delays == [2]
    await client.aclose()


@pytest.mark.asyncio
async def test_candles_reject_gap_without_filling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 5 * MINUTE})
        return httpx.Response(200, json=[row(0), row(2 * MINUTE)])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://data-api.binance.vision"
    )
    market = BinanceMarket(client=client)
    with pytest.raises(DataQualityError) as error:
        await market.candles("BTCUSDT", "1m", 0, 2 * MINUTE)
    assert [issue.code for issue in error.value.report.issues] == ["gap"]
    await client.aclose()


@pytest.mark.asyncio
async def test_candles_reject_missing_requested_boundary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 5 * MINUTE})
        return httpx.Response(200, json=[row(MINUTE), row(2 * MINUTE)])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://data-api.binance.vision"
    )
    market = BinanceMarket(client=client)
    with pytest.raises(DataQualityError) as error:
        await market.candles("BTCUSDT", "1m", 0, 2 * MINUTE)
    assert "first expected" in str(error.value)
    await client.aclose()


def test_quality_reports_duplicate_order_time_ohlc_and_unclosed() -> None:
    invalid = Candle(
        open_time=1,
        close_time=9,
        open=Decimal("100"),
        high=Decimal("90"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("-1"),
    )
    items = [candle(MINUTE), candle(MINUTE), candle(0), invalid]
    report = validate_candles(items, "1m", now_ms=MINUTE + 10)
    codes = {issue.code for issue in report.issues}
    assert {
        "duplicate",
        "out_of_order",
        "invalid_time",
        "invalid_value",
        "unclosed",
    } <= codes
    assert report.to_dict()["ok"] is False

    ohlc = candle(0, close="103", high="102")
    assert [issue.code for issue in validate_candles([ohlc], "1m").issues] == [
        "invalid_ohlc"
    ]


def test_freshness_and_tr_fail_closed() -> None:
    assert is_fresh(10_000, now_ms=15_000)
    assert not is_fresh(9_999, now_ms=15_000)
    assert not is_fresh(15_001, now_ms=15_000)
    with pytest.raises(VenueUnavailable, match="fallback is prohibited"):
        unavailable_tr_market()


def test_parquet_incremental_upsert_manifest_and_hash(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    first = store.upsert("BTCUSDT", "1m", [candle(0), candle(MINUTE)])
    assert first.rows == 2
    assert len(first.parquet_sha256) == 64

    changed = candle(MINUTE, close="100.5")
    second = store.upsert("BTCUSDT", "1m", [changed, candle(2 * MINUTE)])
    loaded = store.read("BTCUSDT", "1m")
    assert second.rows == 3
    assert [item.open_time for item in loaded] == [0, MINUTE, 2 * MINUTE]
    assert loaded[1].close == Decimal("100.5")

    parquet_path = tmp_path / "data" / "BTCUSDT" / "1m.parquet"
    with parquet_path.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(DataQualityError, match="SHA-256"):
        store.read("BTCUSDT", "1m")


class FakeWebSocket:
    def __init__(self, messages: list[str | Exception]) -> None:
        self.messages = iter(messages)

    async def recv(self) -> str:
        message = next(self.messages)
        if isinstance(message, Exception):
            raise message
        return message


class FakeContext:
    def __init__(
        self, websocket: FakeWebSocket | None = None, error: Exception | None = None
    ) -> None:
        self.websocket = websocket
        self.error = error

    async def __aenter__(self) -> FakeWebSocket:
        if self.error is not None:
            raise self.error
        assert self.websocket is not None
        return self.websocket

    async def __aexit__(self, *_exc: object) -> None:
        return None


def ws_kline(open_time: int, event_time: int, *, closed: bool = True) -> str:
    return json.dumps(
        {
            "e": "kline",
            "E": event_time,
            "s": "BTCUSDT",
            "k": {
                "t": open_time,
                "T": open_time + MINUTE - 1,
                "s": "BTCUSDT",
                "i": "1m",
                "o": "100",
                "h": "102",
                "l": "99",
                "c": "101",
                "v": "2.5",
                "x": closed,
            },
        }
    )


@pytest.mark.asyncio
async def test_stream_reconnects_and_emits_closed_candles_only() -> None:
    contexts = iter(
        [
            FakeContext(error=OSError("disconnect")),
            FakeContext(
                FakeWebSocket(
                    [
                        ws_kline(0, 120_000, closed=False),
                        ws_kline(0, 120_000, closed=True),
                    ]
                )
            ),
        ]
    )
    urls: list[str] = []
    delays: list[float] = []

    def connector(url: str) -> FakeContext:
        urls.append(url)
        return next(contexts)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    stream = BinanceMarketStream(
        connector=connector,
        sleep=sleep,
        now_ms=lambda: 120_000,
        freshness_ms=5_000,
    ).closed_candles("BTCUSDT", "1m")
    result = await anext(stream)
    assert result.open_time == 0
    assert delays == [0.25]
    assert urls[0].endswith("/ws/btcusdt@kline_1m")
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_rejects_stale_event() -> None:
    context = FakeContext(FakeWebSocket([ws_kline(0, 100_000)]))
    stream: AsyncIterator[Candle] = BinanceMarketStream(
        connector=lambda _url: context,
        now_ms=lambda: 110_001,
        freshness_ms=5_000,
    ).closed_candles("BTCUSDT", "1m")
    with pytest.raises(StaleMarketData):
        await anext(stream)
