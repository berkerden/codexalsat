"""Local-only HTTP application. No production trading transport is installed."""

import asyncio
import contextlib
import json
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from spotlab import __version__
from spotlab.contracts import INTERVAL_MS, Quote
from spotlab.engine import PaperConfig, PaperEngine, millis
from spotlab.finance import CostModel
from spotlab.market import (
    BinanceMarket,
    BinanceMarketStream,
    MarketDataError,
    ParquetStore,
    is_fresh,
    validate_candles,
)
from spotlab.research import run_research, similar_patterns, strategy_signal
from spotlab.signals import signal_card

D = Decimal
LIVE_BLOCKERS = [
    "Canlı sermaye, risk kapsamı ve süre için kullanıcı yetkisi alınmadı.",
    "Testnet emir yaşam döngüsü ve borsa tarafı koruyucu emirler doğrulanmadı.",
    "Üretim verisiyle ileri paper gözlem süresi ve ekonomik avantaj onayı tamamlanmadı.",
    "Bağımsız canlı kullanım incelemesi tamamlanmadı; üretim emir adaptörü etkin değil.",
]


def version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "uncommitted"


def wire(value: Any) -> Any:
    return jsonable_encoder(value, custom_encoder={Decimal: str})


class CostsInput(BaseModel):
    entry_fee: Decimal = Field(default=D("0.001"), ge=0, le=D("0.02"), allow_inf_nan=False)
    exit_fee: Decimal = Field(default=D("0.001"), ge=0, le=D("0.02"), allow_inf_nan=False)
    spread_bps: Decimal = Field(default=D("5"), ge=0, le=500, allow_inf_nan=False)
    slippage_bps: Decimal = Field(default=D("5"), ge=0, le=500, allow_inf_nan=False)


class ResearchInput(BaseModel):
    venue: str = Field(pattern="^global$")
    symbol: str = Field(pattern="^[A-Z0-9]{5,20}$")
    interval: str = Field(pattern="^(1m|3m|5m|15m|1h)$")
    days: int = Field(default=180, ge=1, le=365)
    hold_minutes: int = Field(default=60, ge=1, le=10080)
    costs: CostsInput = Field(default_factory=CostsInput)


def venue_check(venue: str) -> None:
    if venue != "global":
        raise HTTPException(
            422, "Binance TR adaptörü doğrulanmadı. Global API yerine kullanılamaz."
        )


def filters(info: dict[str, Any]) -> tuple[Decimal, Decimal]:
    if info.get("status") != "TRADING" or not info.get("isSpotTradingAllowed", False):
        raise ValueError("Sembol spot işleme açık değil")
    items = {item["filterType"]: item for item in info.get("filters", [])}
    lot = items.get("LOT_SIZE")
    notional = items.get("NOTIONAL", items.get("MIN_NOTIONAL"))
    if not lot or not notional:
        raise ValueError("Güncel miktar/tutar filtreleri eksik")
    return D(lot["stepSize"]), D(notional["minNotional"])


def create_app(database_url: str | None = None, *, run_worker: bool = True) -> FastAPI:
    data_dir = Path(os.getenv("SPOTLAB_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    db_url: str = database_url or os.getenv("DATABASE_URL") or f"sqlite:///{data_dir}/paper.sqlite"
    allowed_origins = os.getenv(
        "CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    ).split(",")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = PaperEngine(db_url)
        app.state.market = BinanceMarket()
        app.state.store = ParquetStore(data_dir / "market")
        app.state.quote = None
        app.state.selected_symbol = None
        app.state.stream_task = None
        app.state.stream_status = "not_connected"
        app.state.worker_error = None
        app.state.research_lock = asyncio.Lock()
        app.state.background_enabled = run_worker
        worker = asyncio.create_task(paper_loop(app)) if run_worker else None
        try:
            yield
        finally:
            for task in (worker, app.state.stream_task):
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            await app.state.market.aclose()
            app.state.engine.db.dispose()

    app = FastAPI(title="SpotLab", version=__version__, lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin not in allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "İzin verilmeyen kaynak"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(MarketDataError)
    async def market_error(_request: Request, _error: MarketDataError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Borsa verisi alınamadı veya kalitesi uygun değil. Yeni işlem açılmaz."
            },
        )

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        state = app.state.engine.snapshot()
        return {
            "version": __version__,
            "commit": version(),
            "venue": state["config"]["venue"] if state["config"] else None,
            "mode": "paper" if state["config"] else "suggestions",
            "live_enabled": False,
            "blockers": LIVE_BLOCKERS,
            "stream_status": app.state.stream_status,
            "worker_error": app.state.worker_error,
            "runtime": {
                "browser_independent": True,
                "sleep_stops_bot": True,
                "daily_timezone": "UTC",
                "real_order_transport": False,
            },
        }

    @app.get("/api/symbols")
    async def symbols(venue: str = "unconfigured") -> dict[str, Any]:
        venue_check(venue)
        return {"symbols": await app.state.market.symbols()}

    @app.get("/api/market")
    async def market_view(
        venue: str,
        symbol: str = Query(pattern="^[A-Z0-9]{5,20}$"),
        interval: str = Query(pattern="^(1m|3m|5m|15m|1h)$"),
    ) -> Any:
        venue_check(venue)
        candles, quote, info, clock = await asyncio.gather(
            app.state.market.latest(symbol, interval),
            current_quote(app, symbol),
            app.state.market.exchange_info(symbol),
            app.state.market.clock(),
        )
        await select_stream(app, symbol)
        quality = validate_candles(candles, interval, now_ms=millis())
        issues = [asdict(issue) for issue in quality.issues]
        if not clock.synchronized:
            issues.append({"code": "clock_skew", "message": "Saat farkı sınırı aşıldı"})
        if not is_fresh(quote.observed_at):
            issues.append({"code": "stale", "message": "Kotasyon eski"})
        try:
            step, minimum = filters(info)
        except ValueError as exc:
            step, minimum = D("0.00000001"), D(0)
            issues.append({"code": "symbol_rules", "message": str(exc)})
        signal: dict[str, Any] = {
            "action": "İŞLEM YAPMA",
            "strategy": "pullback",
            "reason": "Örneklem dışı avantaj ve maliyet tamponu doğrulanmadı.",
            "evidence": "Bu görünüm araştırma kanıtı değildir.",
            "data_time": quote.observed_at,
            "margin": None,
            "quantity": None,
            "expected_net_result": None,
            "holding_minutes": None,
        }
        if candles:
            signal["entry"] = str(quote.ask)
            signal["reason"] = "Veri sağlığı uygun değil." if issues else signal["reason"]
        report_path = data_dir / "reports" / f"{symbol}-{interval}.json"
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        signal = signal_card(
            quote, candles, not issues, app.state.engine.snapshot(), report, step, minimum
        )
        return wire(
            {
                "symbol": symbol,
                "interval": interval,
                "source": "Binance Global REST/WS",
                "candles": [asdict(c) for c in candles],
                "quote": {**asdict(quote), "spread_bps": quote.spread_bps},
                "quality": {"healthy": not issues, "issues": issues},
                "clock": asdict(clock),
                "signal": signal,
            }
        )

    @app.post("/api/research")
    async def research(body: ResearchInput) -> Any:
        if app.state.research_lock.locked():
            raise HTTPException(409, "Bir araştırma sürüyor; bitmesini bekleyin.")
        async with app.state.research_lock:
            end = millis()
            start = (
                (end - body.days * 86_400_000)
                // INTERVAL_MS[body.interval]
                * INTERVAL_MS[body.interval]
            )
            cached = app.state.store.read(body.symbol, body.interval)
            cursor = start
            if cached and cached[0].open_time <= start:
                cursor = max(start, cached[-1].open_time + INTERVAL_MS[body.interval])
            fresh = await app.state.market.candles(body.symbol, body.interval, cursor, end)
            if fresh:
                app.state.store.upsert(body.symbol, body.interval, fresh)
            candles = [
                c
                for c in app.state.store.read(body.symbol, body.interval)
                if start <= c.open_time and c.close_time < end
            ]
            costs = CostModel(**body.costs.model_dump())
            report = await asyncio.to_thread(
                run_research, candles, body.interval, body.hold_minutes, costs, version()
            )
            patterns = await asyncio.to_thread(similar_patterns, candles)
            report_dir = data_dir / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report["live_evidence_approved"] = False
            report["research_use"] = "Keşifsel araştırma; canlı strateji onayı değildir."
            (report_dir / f"{body.symbol}-{body.interval}.json").write_text(
                json.dumps(wire(report)), encoding="utf-8"
            )
            return wire(
                {
                    **report,
                    "patterns": patterns,
                    "symbol": body.symbol,
                    "source": "Binance Global",
                    "interval": body.interval,
                }
            )

    @app.get("/api/paper")
    async def paper() -> Any:
        return app.state.engine.snapshot()

    @app.post("/api/paper/start")
    async def paper_start(body: PaperConfig) -> Any:
        venue_check(body.venue)
        info = await app.state.market.exchange_info(body.symbol)
        try:
            filters(info)
            result = app.state.engine.start(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        await select_stream(app, body.symbol)
        return result

    @app.post("/api/paper/stop")
    async def paper_stop() -> Any:
        return app.state.engine.stop()

    @app.post("/api/paper/close")
    async def paper_close() -> Any:
        state = app.state.engine.request_close()
        if state["position"]:
            cfg = PaperConfig.model_validate(state["config"])
            q = await current_quote(app, cfg.symbol)
            return app.state.engine.tick(
                q,
                signal=False,
                candle_time=0,
                step=D("0.00000001"),
                min_notional=D(0),
                force_close=True,
            )
        return state

    @app.post("/api/live/activate")
    async def live_activate() -> None:
        raise HTTPException(409, {"message": "Canlı işlem kilitli", "blockers": LIVE_BLOCKERS})

    return app


async def current_quote(app: FastAPI, symbol: str) -> Quote:
    quote: Quote | None = app.state.quote
    if quote and quote.symbol == symbol and is_fresh(quote.observed_at):
        return quote
    result: Quote = await app.state.market.quote(symbol)
    return result


async def stream_loop(app: FastAPI, symbol: str) -> None:
    while True:
        try:
            app.state.stream_status = "connecting"
            async for quote in BinanceMarketStream().quotes(symbol):
                app.state.quote = quote
                app.state.stream_status = "connected"
        except (MarketDataError, OSError, TimeoutError):
            app.state.stream_status = "disconnected"
            app.state.quote = None
            await asyncio.sleep(5)


async def select_stream(app: FastAPI, symbol: str) -> None:
    if not app.state.background_enabled:
        return
    cfg = app.state.engine.snapshot()["config"]
    desired = cfg["symbol"] if cfg else symbol
    if app.state.selected_symbol != desired:
        if app.state.stream_task:
            app.state.stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.stream_task
        app.state.quote = None
        app.state.selected_symbol = desired
        app.state.stream_task = asyncio.create_task(stream_loop(app, desired))


async def paper_cycle(app: FastAPI) -> None:
    """Exit lifecycle depends only on a fresh executable quote, never entry research."""
    state = app.state.engine.snapshot()
    if not state["config"] or not (state["entries_enabled"] or state["position"]):
        return
    cfg = PaperConfig.model_validate(state["config"])
    try:
        quote = await current_quote(app, cfg.symbol)
        if state["position"]:
            app.state.engine.tick(
                quote, signal=False, candle_time=0, step=D("0.00000001"), min_notional=D(0)
            )
            app.state.worker_error = None
            return
        info, clock, candles = await asyncio.gather(
            app.state.market.exchange_info(cfg.symbol),
            app.state.market.clock(),
            app.state.market.latest(cfg.symbol, cfg.interval),
        )
        quality = validate_candles(candles, cfg.interval, now_ms=millis())
        step, minimum = filters(info)
        signals = strategy_signal(candles, cfg.strategy)
        # Fetch again after slower entry prerequisites; no fill at an old pre-signal quote.
        quote = await current_quote(app, cfg.symbol)
        app.state.engine.tick(
            quote,
            signal=bool(signals[-1]) if signals else False,
            candle_time=candles[-1].close_time if candles else 0,
            step=step,
            min_notional=minimum,
            healthy=quality.ok and clock.synchronized,
        )
        app.state.worker_error = None
    except (MarketDataError, OSError, ValueError, TimeoutError):
        app.state.engine.stop("Veri bağlantısı/kalitesi bozuldu; girişler durduruldu.")
        app.state.worker_error = "Veri hatası; kayıtlı çıkış isteği ve pozisyon yönetimi korunuyor."


async def paper_loop(app: FastAPI) -> None:
    while True:
        await paper_cycle(app)
        await asyncio.sleep(2)


app = create_app()
