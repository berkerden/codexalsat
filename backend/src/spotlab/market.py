"""Public Binance Global market data, strict quality checks, and Parquet storage.

This module intentionally contains no authenticated or trading endpoint.  REST and
WebSocket defaults point at Binance's market-data-only hosts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, Self, cast

import httpx

from spotlab.contracts import INTERVAL_MS, Candle, Quote

GLOBAL_REST_URL = "https://data-api.binance.vision"
GLOBAL_WS_URL = "wss://data-stream.binance.vision"
MAX_KLINES_PER_REQUEST = 1_000
MAX_CANDLES_PER_QUERY = 1_000_000
DEFAULT_QUOTE_MAX_AGE_MS = 5_000
DEFAULT_CLOCK_MAX_OFFSET_MS = 1_000


def _utc_now_ms() -> int:
    return time.time_ns() // 1_000_000


class MarketDataError(RuntimeError):
    """Base error for public market-data failures."""


class MarketDataUnavailable(MarketDataError):
    """Raised after a bounded number of transport/server retries."""


class VenueUnavailable(MarketDataError):
    """Raised when a requested venue has not been independently verified."""


class DataQualityError(MarketDataError):
    """Raised when candle data violates the strict quality contract."""

    def __init__(self, report: QualityReport) -> None:
        self.report = report
        detail = "; ".join(f"{issue.code}: {issue.message}" for issue in report.issues)
        super().__init__(detail or "candle data failed quality validation")


class StaleMarketData(MarketDataError):
    """Raised when a stream event or quote exceeds its freshness budget."""


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    open_time: int | None = None


@dataclass(frozen=True)
class QualityReport:
    candle_count: int
    interval: str
    issues: tuple[QualityIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def raise_if_invalid(self) -> None:
        if self.issues:
            raise DataQualityError(self)

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON shape consumed by the API layer."""

        return {
            "ok": self.ok,
            "candle_count": self.candle_count,
            "interval": self.interval,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class ClockReport:
    server_time_ms: int
    local_midpoint_ms: int
    offset_ms: int
    round_trip_ms: int
    max_offset_ms: int = DEFAULT_CLOCK_MAX_OFFSET_MS

    @property
    def synchronized(self) -> bool:
        return abs(self.offset_ms) <= self.max_offset_ms


@dataclass(frozen=True)
class ParquetManifest:
    schema_version: int
    venue: str
    symbol: str
    interval: str
    rows: int
    first_open_time: int | None
    last_open_time: int | None
    parquet_sha256: str
    parquet_file: str


def unavailable_tr_market() -> None:
    """Fail closed for Binance TR; it must never fall back to Binance Global."""

    raise VenueUnavailable(
        "Binance TR market data is unavailable until its endpoints and contracts are "
        "independently verified; Binance Global fallback is prohibited"
    )


def is_fresh(observed_at: int, *, now_ms: int | None = None, max_age_ms: int = 5_000) -> bool:
    """Return whether a UTC millisecond observation is recent and not in the future."""

    if max_age_ms < 0:
        raise ValueError("max_age_ms must be non-negative")
    now = _utc_now_ms() if now_ms is None else now_ms
    age = now - observed_at
    return 0 <= age <= max_age_ms


def validate_candles(
    candles: Sequence[Candle],
    interval: str,
    *,
    now_ms: int | None = None,
) -> QualityReport:
    """Check ordering, continuity, UTC boundaries, values, OHLC, and closed state.

    The function reports defects exactly as received.  It never sorts, deduplicates,
    interpolates, or fills gaps.
    """

    interval_ms = _interval_ms(interval)
    issues: list[QualityIssue] = []
    seen: set[int] = set()
    previous: Candle | None = None

    for candle in candles:
        open_time = candle.open_time
        if open_time in seen:
            issues.append(QualityIssue("duplicate", "duplicate open_time", open_time))
        seen.add(open_time)

        if open_time < 0 or candle.close_time < 0:
            issues.append(
                QualityIssue("invalid_time", "timestamps must be non-negative", open_time)
            )
        if open_time % interval_ms != 0:
            issues.append(
                QualityIssue("invalid_time", "open_time is not aligned to the interval", open_time)
            )
        expected_close = open_time + interval_ms - 1
        if candle.close_time != expected_close:
            issues.append(
                QualityIssue(
                    "invalid_time",
                    f"close_time must equal {expected_close}",
                    open_time,
                )
            )
        if now_ms is not None and candle.close_time >= now_ms:
            issues.append(QualityIssue("unclosed", "candle has not closed", open_time))

        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if any(not value.is_finite() for value in values):
            issues.append(QualityIssue("invalid_value", "values must be finite", open_time))
        elif any(value <= 0 for value in values[:4]) or candle.volume < 0:
            issues.append(
                QualityIssue(
                    "invalid_value",
                    "prices must be positive and volume must be non-negative",
                    open_time,
                )
            )
        elif candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(
            candle.open, candle.close, candle.high
        ):
            issues.append(QualityIssue("invalid_ohlc", "inconsistent OHLC bounds", open_time))

        if previous is not None:
            difference = open_time - previous.open_time
            if difference <= 0 and open_time != previous.open_time:
                issues.append(QualityIssue("out_of_order", "open_time decreased", open_time))
            elif difference > interval_ms:
                missing = difference // interval_ms - 1
                issues.append(
                    QualityIssue(
                        "gap",
                        f"missing {missing} interval(s) after {previous.open_time}",
                        open_time,
                    )
                )
            elif 0 < difference < interval_ms:
                issues.append(
                    QualityIssue("invalid_time", "open_time spacing is below interval", open_time)
                )
        previous = candle

    return QualityReport(len(candles), interval, tuple(issues))


class BinanceMarket:
    """Async client for unauthenticated Binance Global Spot market data."""

    def __init__(
        self,
        *,
        base_url: str = GLOBAL_REST_URL,
        client: httpx.AsyncClient | None = None,
        retries: int = 3,
        timeout_s: float = 10.0,
        retry_base_s: float = 0.25,
        retry_cap_s: float = 4.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_ms: Callable[[], int] = _utc_now_ms,
    ) -> None:
        if retries < 0:
            raise ValueError("retries must be non-negative")
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._owns_client = client is None
        self._retries = retries
        self._retry_base_s = retry_base_s
        self._retry_cap_s = retry_cap_s
        self._sleep = sleep
        self._now_ms = now_ms

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def symbols(self) -> list[str]:
        payload = await self._get_json("/api/v3/exchangeInfo")
        entries = _mapping(payload).get("symbols")
        if not isinstance(entries, list):
            raise MarketDataError("exchangeInfo response has no symbols array")
        result: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            permissions = entry.get("permissions", [])
            permission_sets = entry.get("permissionSets", [])
            spot_allowed = "SPOT" in permissions or any(
                isinstance(group, list) and "SPOT" in group for group in permission_sets
            )
            if entry.get("status") == "TRADING" and spot_allowed:
                symbol = entry.get("symbol")
                if isinstance(symbol, str):
                    result.append(symbol)
        return sorted(result)

    async def exchange_info(self, symbol: str) -> dict[str, Any]:
        normalized = _symbol(symbol)
        payload = await self._get_json("/api/v3/exchangeInfo", params={"symbol": normalized})
        entries = _mapping(payload).get("symbols")
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise MarketDataError(f"exchangeInfo returned no unique symbol for {normalized}")
        entry = cast(dict[str, Any], entries[0])
        if entry.get("symbol") != normalized:
            raise MarketDataError(f"exchangeInfo returned a different symbol for {normalized}")
        return entry

    async def server_time(self) -> int:
        payload = _mapping(await self._get_json("/api/v3/time"))
        value = payload.get("serverTime")
        if not isinstance(value, int):
            raise MarketDataError("time response has no integer serverTime")
        return value

    async def clock(self, *, max_offset_ms: int = DEFAULT_CLOCK_MAX_OFFSET_MS) -> ClockReport:
        started = self._now_ms()
        server_time = await self.server_time()
        finished = self._now_ms()
        round_trip = max(0, finished - started)
        midpoint = started + round_trip // 2
        return ClockReport(server_time, midpoint, server_time - midpoint, round_trip, max_offset_ms)

    async def quote(self, symbol: str) -> Quote:
        normalized = _symbol(symbol)
        payload = _mapping(
            await self._get_json("/api/v3/ticker/bookTicker", params={"symbol": normalized})
        )
        try:
            quote = Quote(
                symbol=str(payload["symbol"]),
                bid=Decimal(str(payload["bidPrice"])),
                ask=Decimal(str(payload["askPrice"])),
                bid_quantity=Decimal(str(payload["bidQty"])),
                ask_quantity=Decimal(str(payload["askQty"])),
                observed_at=self._now_ms(),
            )
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise MarketDataError("invalid bookTicker response") from exc
        if quote.symbol != normalized or quote.bid <= 0 or quote.ask <= 0 or quote.bid > quote.ask:
            raise MarketDataError("invalid best bid/ask values")
        if quote.bid_quantity < 0 or quote.ask_quantity < 0:
            raise MarketDataError("best bid/ask quantities must be non-negative")
        return quote

    async def candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[Candle]:
        """Fetch all closed candles with open times inside inclusive bounds."""

        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("require 0 <= start_ms <= end_ms")
        server_time = await self.server_time()
        return await self._candles(symbol, interval, start_ms, end_ms, server_time)

    async def latest(self, symbol: str, interval: str, limit: int = 300) -> list[Candle]:
        if not 1 <= limit <= MAX_KLINES_PER_REQUEST:
            raise ValueError(f"limit must be between 1 and {MAX_KLINES_PER_REQUEST}")
        interval_ms = _interval_ms(interval)
        server_time = await self.server_time()
        current_open = server_time // interval_ms * interval_ms
        end_open = current_open - interval_ms
        start_open = end_open - (limit - 1) * interval_ms
        if start_open < 0:
            start_open = 0
        candles = await self._candles(symbol, interval, start_open, end_open, server_time)
        return candles[-limit:]

    async def _candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        server_time: int,
    ) -> list[Candle]:
        normalized = _symbol(symbol)
        interval_ms = _interval_ms(interval)
        requested_count = (end_ms - start_ms) // interval_ms + 1
        if requested_count > MAX_CANDLES_PER_QUERY:
            raise ValueError(f"candle query exceeds {MAX_CANDLES_PER_QUERY} intervals")
        cursor = start_ms
        candles: list[Candle] = []

        while cursor <= end_ms:
            payload = await self._get_json(
                "/api/v3/klines",
                params={
                    "symbol": normalized,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": MAX_KLINES_PER_REQUEST,
                },
            )
            if not isinstance(payload, list):
                raise MarketDataError("klines response is not an array")
            if not payload:
                break

            page = [_parse_rest_candle(row) for row in payload]
            last_open = page[-1].open_time
            if last_open < cursor:
                raise MarketDataError("klines pagination did not advance")
            candles.extend(
                candle
                for candle in page
                if start_ms <= candle.open_time <= end_ms and candle.close_time < server_time
            )
            next_cursor = last_open + interval_ms
            if next_cursor <= cursor:
                raise MarketDataError("klines pagination did not advance")
            cursor = next_cursor
            if len(payload) < MAX_KLINES_PER_REQUEST:
                break

        report = validate_candles(candles, interval, now_ms=server_time)
        first_expected = ((start_ms + interval_ms - 1) // interval_ms) * interval_ms
        last_closed = server_time // interval_ms * interval_ms - interval_ms
        last_expected = min(end_ms // interval_ms * interval_ms, last_closed)
        boundary_issues: list[QualityIssue] = []
        if first_expected <= last_expected:
            if not candles:
                boundary_issues.append(
                    QualityIssue("gap", "requested closed-candle range returned no data")
                )
            else:
                if candles[0].open_time != first_expected:
                    boundary_issues.append(
                        QualityIssue(
                            "gap",
                            f"first expected open_time is {first_expected}",
                            candles[0].open_time,
                        )
                    )
                if candles[-1].open_time != last_expected:
                    boundary_issues.append(
                        QualityIssue(
                            "gap",
                            f"last expected open_time is {last_expected}",
                            candles[-1].open_time,
                        )
                    )
        if boundary_issues:
            report = QualityReport(
                report.candle_count, report.interval, report.issues + tuple(boundary_issues)
            )
        report.raise_if_invalid()
        return candles

    async def _get_json(
        self, path: str, *, params: Mapping[str, str | int] | None = None
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.get(path, params=params)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == self._retries:
                    break
                await self._sleep(self._retry_delay(attempt, None))
                continue

            if response.status_code in {418, 429} or 500 <= response.status_code < 600:
                last_error = MarketDataUnavailable(
                    f"Binance market data returned HTTP {response.status_code}"
                )
                if attempt == self._retries:
                    break
                await self._sleep(self._retry_delay(attempt, response.headers.get("Retry-After")))
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MarketDataError(
                    f"Binance market data returned HTTP {response.status_code}"
                ) from exc
            try:
                return response.json()
            except ValueError as exc:
                raise MarketDataError("Binance market data returned invalid JSON") from exc

        raise MarketDataUnavailable("Binance market data retries exhausted") from last_error

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(self._retry_cap_s, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return float(min(self._retry_cap_s, self._retry_base_s * (2**attempt)))


class _WebSocket(Protocol):
    async def recv(self) -> str | bytes: ...


class _WebSocketContext(Protocol):
    async def __aenter__(self) -> _WebSocket: ...

    async def __aexit__(self, *exc: object) -> None: ...


class BinanceMarketStream:
    """Direct market-data-only streams with bounded reconnect backoff."""

    def __init__(
        self,
        *,
        base_url: str = GLOBAL_WS_URL,
        connector: Callable[[str], _WebSocketContext] | None = None,
        reconnect_attempts: int = 8,
        retry_base_s: float = 0.25,
        retry_cap_s: float = 8.0,
        freshness_ms: int = DEFAULT_QUOTE_MAX_AGE_MS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_ms: Callable[[], int] = _utc_now_ms,
    ) -> None:
        if reconnect_attempts < 0:
            raise ValueError("reconnect_attempts must be non-negative")
        self._base_url = base_url.rstrip("/")
        self._connector = connector or _default_ws_connector
        self._reconnect_attempts = reconnect_attempts
        self._retry_base_s = retry_base_s
        self._retry_cap_s = retry_cap_s
        self._freshness_ms = freshness_ms
        self._sleep = sleep
        self._now_ms = now_ms

    async def closed_candles(self, symbol: str, interval: str) -> AsyncIterator[Candle]:
        normalized = _symbol(symbol)
        interval_ms = _interval_ms(interval)
        url = f"{self._base_url}/ws/{normalized.lower()}@kline_{interval}"
        last_open: int | None = None
        reconnects = 0

        while reconnects <= self._reconnect_attempts:
            try:
                async with self._connector(url) as websocket:
                    async for candle in self._closed_messages(websocket, normalized, interval):
                        reconnects = 0
                        if last_open is not None:
                            if candle.open_time == last_open:
                                continue
                            if candle.open_time < last_open:
                                raise DataQualityError(
                                    QualityReport(
                                        1,
                                        interval,
                                        (
                                            QualityIssue(
                                                "out_of_order", "stream open_time decreased"
                                            ),
                                        ),
                                    )
                                )
                            if candle.open_time - last_open != interval_ms:
                                raise DataQualityError(
                                    QualityReport(
                                        1,
                                        interval,
                                        (
                                            QualityIssue(
                                                "gap", "closed candle missing after reconnect"
                                            ),
                                        ),
                                    )
                                )
                        last_open = candle.open_time
                        yield candle
            except (DataQualityError, StaleMarketData):
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if reconnects >= self._reconnect_attempts:
                    raise MarketDataUnavailable("WebSocket reconnect attempts exhausted") from exc
                delay = min(self._retry_cap_s, self._retry_base_s * (2**reconnects))
                reconnects += 1
                await self._sleep(delay)

    async def quotes(self, symbol: str) -> AsyncIterator[Quote]:
        normalized = _symbol(symbol)
        url = f"{self._base_url}/ws/{normalized.lower()}@bookTicker"
        reconnects = 0
        while reconnects <= self._reconnect_attempts:
            try:
                async with self._connector(url) as websocket:
                    while True:
                        payload = _decode_ws(await websocket.recv())
                        reconnects = 0
                        observed_at = self._now_ms()
                        try:
                            quote = Quote(
                                symbol=str(payload["s"]),
                                bid=Decimal(str(payload["b"])),
                                ask=Decimal(str(payload["a"])),
                                bid_quantity=Decimal(str(payload["B"])),
                                ask_quantity=Decimal(str(payload["A"])),
                                observed_at=observed_at,
                            )
                        except (KeyError, InvalidOperation, ValueError) as exc:
                            raise MarketDataError("invalid bookTicker stream payload") from exc
                        if quote.symbol != normalized or quote.bid <= 0 or quote.ask < quote.bid:
                            raise MarketDataError("invalid bookTicker stream values")
                        yield quote
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if reconnects >= self._reconnect_attempts:
                    raise MarketDataUnavailable("WebSocket reconnect attempts exhausted") from exc
                delay = min(self._retry_cap_s, self._retry_base_s * (2**reconnects))
                reconnects += 1
                await self._sleep(delay)

    async def _closed_messages(
        self, websocket: _WebSocket, symbol: str, interval: str
    ) -> AsyncIterator[Candle]:
        while True:
            payload = _decode_ws(await websocket.recv())
            event_time = payload.get("E")
            if not isinstance(event_time, int):
                raise MarketDataError("kline stream payload has no event time")
            if not is_fresh(event_time, now_ms=self._now_ms(), max_age_ms=self._freshness_ms):
                raise StaleMarketData("kline stream event is stale or future-dated")
            if payload.get("s") != symbol:
                raise MarketDataError("kline stream returned a different symbol")
            kline = payload.get("k")
            if not isinstance(kline, Mapping) or kline.get("i") != interval:
                raise MarketDataError("invalid kline stream payload")
            if kline.get("x") is not True:
                continue
            candle = _parse_ws_candle(kline)
            report = validate_candles([candle], interval, now_ms=self._now_ms())
            report.raise_if_invalid()
            yield candle


class ParquetStore:
    """Incremental candle archive with strict validation and SHA-256 manifests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def upsert(self, symbol: str, interval: str, candles: Sequence[Candle]) -> ParquetManifest:
        normalized = _symbol(symbol)
        _interval_ms(interval)
        incoming_report = validate_candles(candles, interval)
        incoming_report.raise_if_invalid()
        parquet_path, manifest_path = self._paths(normalized, interval)
        existing = self.read(normalized, interval) if parquet_path.exists() else []
        merged = {candle.open_time: candle for candle in existing}
        merged.update({candle.open_time: candle for candle in candles})
        ordered = [merged[key] for key in sorted(merged)]
        validate_candles(ordered, interval).raise_if_invalid()

        pa, pq = _pyarrow()
        table = pa.table(
            {
                "open_time": pa.array([c.open_time for c in ordered], type=pa.int64()),
                "close_time": pa.array([c.close_time for c in ordered], type=pa.int64()),
                "open": pa.array([str(c.open) for c in ordered], type=pa.string()),
                "high": pa.array([str(c.high) for c in ordered], type=pa.string()),
                "low": pa.array([str(c.low) for c in ordered], type=pa.string()),
                "close": pa.array([str(c.close) for c in ordered], type=pa.string()),
                "volume": pa.array([str(c.volume) for c in ordered], type=pa.string()),
            }
        )
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = parquet_path.with_suffix(".parquet.tmp")
        pq.write_table(table, temporary, compression="zstd")
        os.replace(temporary, parquet_path)
        digest = _sha256_file(parquet_path)
        manifest = ParquetManifest(
            schema_version=1,
            venue="binance-global",
            symbol=normalized,
            interval=interval,
            rows=len(ordered),
            first_open_time=ordered[0].open_time if ordered else None,
            last_open_time=ordered[-1].open_time if ordered else None,
            parquet_sha256=digest,
            parquet_file=parquet_path.name,
        )
        manifest_tmp = manifest_path.with_suffix(".json.tmp")
        manifest_tmp.write_text(
            json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_tmp, manifest_path)
        return manifest

    def read(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        verify_hash: bool = True,
    ) -> list[Candle]:
        normalized = _symbol(symbol)
        _interval_ms(interval)
        parquet_path, manifest_path = self._paths(normalized, interval)
        if not parquet_path.exists():
            return []
        if verify_hash:
            if not manifest_path.exists():
                raise DataQualityError(
                    QualityReport(0, interval, (QualityIssue("manifest", "manifest is missing"),))
                )
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = raw_manifest.get("parquet_sha256")
            if not isinstance(expected, str) or _sha256_file(parquet_path) != expected:
                raise DataQualityError(
                    QualityReport(0, interval, (QualityIssue("hash", "Parquet SHA-256 mismatch"),))
                )
        _, pq = _pyarrow()
        table = pq.read_table(parquet_path)
        columns = table.to_pydict()
        candles = [
            Candle(
                open_time=int(open_time),
                close_time=int(close_time),
                open=Decimal(open_value),
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                volume=Decimal(volume),
            )
            for open_time, close_time, open_value, high, low, close, volume in zip(
                columns["open_time"],
                columns["close_time"],
                columns["open"],
                columns["high"],
                columns["low"],
                columns["close"],
                columns["volume"],
                strict=True,
            )
        ]
        if start_ms is not None:
            candles = [candle for candle in candles if candle.open_time >= start_ms]
        if end_ms is not None:
            candles = [candle for candle in candles if candle.open_time <= end_ms]
        validate_candles(candles, interval).raise_if_invalid()
        return candles

    def _paths(self, symbol: str, interval: str) -> tuple[Path, Path]:
        directory = self.root / symbol
        return directory / f"{interval}.parquet", directory / f"{interval}.manifest.json"


def _parse_rest_candle(row: object) -> Candle:
    if not isinstance(row, list) or len(row) < 7:
        raise MarketDataError("invalid kline row")
    try:
        return Candle(
            open_time=int(row[0]),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            close_time=int(row[6]),
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketDataError("invalid kline values") from exc


def _parse_ws_candle(kline: Mapping[str, object]) -> Candle:
    try:
        return Candle(
            open_time=int(cast(int | str, kline["t"])),
            close_time=int(cast(int | str, kline["T"])),
            open=Decimal(str(kline["o"])),
            high=Decimal(str(kline["h"])),
            low=Decimal(str(kline["l"])),
            close=Decimal(str(kline["c"])),
            volume=Decimal(str(kline["v"])),
        )
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise MarketDataError("invalid kline stream values") from exc


def _decode_ws(message: str | bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MarketDataError("WebSocket returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise MarketDataError("WebSocket payload is not an object")
    return payload


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketDataError("Binance response is not an object")
    return value


def _symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not normalized or not normalized.isascii() or not normalized.isalnum():
        raise ValueError("symbol must contain only ASCII letters and numbers")
    return normalized


def _interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported interval: {interval}") from exc


def _default_ws_connector(url: str) -> _WebSocketContext:
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:  # pragma: no cover - dependency/install fault
        raise MarketDataUnavailable("websockets dependency is not installed") from exc
    return cast(_WebSocketContext, connect(url, open_timeout=10, close_timeout=5))


def _pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow as pa  # type: ignore[import-untyped]
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - dependency/install fault
        raise MarketDataUnavailable("pyarrow dependency is not installed") from exc
    return pa, pq


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
