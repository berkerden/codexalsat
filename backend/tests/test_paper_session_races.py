"""Held HTTP/market replies must never cross a paper experiment boundary."""

import asyncio
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from backend.tests import test_api as api_helpers
from backend.tests.test_api import fresh_quote, paper_config

from spotlab.engine import millis
from spotlab.main import paper_cycle
from spotlab.market import MarketDataUnavailable

client = api_helpers.client

INFO = {
    "status": "TRADING",
    "isSpotTradingAllowed": True,
    "filters": [
        {"filterType": "LOT_SIZE", "stepSize": "0.01"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
    ],
}


def begin(engine, *, strategy="pullback"):
    cfg = paper_config().model_copy(update={"strategy": strategy})
    return engine.start(cfg, expected_session_id=engine.snapshot()["session_id"])


def fill(engine, *, price="100", signal=True):
    return engine.tick(
        fresh_quote(price),
        signal=signal,
        candle_time=millis() - 1,
        step=D(".01"),
        min_notional=D("5"),
        expected_session_id=engine.snapshot()["session_id"],
    )


def rollover(engine):
    current = engine.snapshot()["session_id"]
    engine.stop(expected_session_id=current)
    engine.archive(current)
    return begin(engine, strategy="breakout")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_old_worker_response_and_error_cannot_touch_replacement(client, monkeypatch, failure):
    app = client.app
    engine = app.state.engine
    begin(engine)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_quote(_symbol):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        if failure:
            raise MarketDataUnavailable("synthetic old session failure")
        return fresh_quote()

    market = app.state.market
    app.state.market = SimpleNamespace(
        quote=delayed_quote,
        exchange_info=AsyncMock(return_value=INFO),
        clock=AsyncMock(return_value=SimpleNamespace(synchronized=True)),
        latest=AsyncMock(return_value=[SimpleNamespace(close_time=millis() - 1)]),
    )
    monkeypatch.setattr("spotlab.main.validate_candles", lambda *a, **k: SimpleNamespace(ok=True))
    monkeypatch.setattr("spotlab.main.strategy_signal", lambda *a, **k: [1])
    work = asyncio.create_task(paper_cycle(app))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        replacement = rollover(engine)
        app.state.worker_error = "replacement session status"
        release.set()
        await asyncio.wait_for(work, 5)
        assert engine.snapshot() == replacement
        assert app.state.worker_error == "replacement session status"
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
        app.state.market = market


@pytest.mark.asyncio
async def test_delayed_start_cannot_reauthorize_replacement(client):
    app = client.app
    engine = app.state.engine
    old = begin(engine)
    engine.stop(expected_session_id=old["session_id"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_info(_symbol):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return INFO

    market = app.state.market
    app.state.market = SimpleNamespace(exchange_info=delayed_info)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as session:
        work = asyncio.create_task(
            session.post(
                "/api/paper/start",
                json={
                    **paper_config().model_dump(mode="json"),
                    "expected_session_id": old["session_id"],
                },
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            replacement = rollover(engine)
            release.set()
            response = await asyncio.wait_for(work, 5)
            assert response.status_code == 409
            assert engine.snapshot() == replacement
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
            app.state.market = market


@pytest.mark.asyncio
async def test_delayed_close_cannot_sell_replacement_position(client):
    app = client.app
    engine = app.state.engine
    old = begin(engine)
    fill(engine)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_quote(_symbol):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return fresh_quote("90")

    market = app.state.market
    app.state.market = SimpleNamespace(quote=delayed_quote)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as session:
        work = asyncio.create_task(
            session.post("/api/paper/close", json={"expected_session_id": old["session_id"]})
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # The old persisted close is completed by another current-session pass.
            assert fill(engine, signal=False)["position"] is None
            rollover(engine)
            replacement = fill(engine)
            assert replacement["position"] is not None
            release.set()
            response = await asyncio.wait_for(work, 5)
            assert response.status_code == 409
            assert engine.snapshot() == replacement
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
            app.state.market = market
