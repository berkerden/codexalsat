"""Independent HTTP/worker checks for prospective evidence, using synthetic quotes only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from backend.tests import test_api as api_helpers
from backend.tests.test_api import fresh_quote, open_position, paper_config
from backend.tests.test_paper_session_races import INFO

from spotlab.engine import millis
from spotlab.main import paper_cycle
from spotlab.market import MarketDataUnavailable

client = api_helpers.client


def report(client):
    identity = client.app.state.engine.snapshot()["session_id"]
    response = client.get("/api/paper/observation", params={"expected_session_id": identity})
    assert response.status_code == 200
    return response.json()


def test_observation_reads_do_not_create_evidence_or_authorize(client):
    engine = client.app.state.engine
    before = engine.snapshot()
    empty = report(client)
    assert empty["evidence_status"] == "no_observations"
    assert empty["observation"] is None
    assert empty["live_eligible"] is False
    assert empty["simulation"] is True
    assert engine.snapshot() == before
    engine.start(paper_config())
    started = engine.snapshot()
    assert report(client)["observation"] is None
    assert engine.snapshot() == started
    assert client.get(
        "/api/paper/observation", params={"expected_session_id": "previous-session"}
    ).status_code == 409
    assert client.get("/api/paper/observation").status_code == 422


@pytest.mark.asyncio
async def test_worker_management_only_cannot_become_entry_evidence(client, monkeypatch):
    app = client.app
    open_position(client)
    # The manual engine tick which created the position is not a worker observation.
    assert report(client)["observation"] is None
    monkeypatch.setattr(app.state.market, "quote", AsyncMock(return_value=fresh_quote()))
    forbidden = AsyncMock(side_effect=AssertionError("exit cannot depend on entry research"))
    monkeypatch.setattr(app.state.market, "exchange_info", forbidden)
    monkeypatch.setattr(app.state.market, "clock", forbidden)
    monkeypatch.setattr(app.state.market, "latest", forbidden)
    await paper_cycle(app)
    result = report(client)
    counts = result["observation"]["counts"]
    assert counts == {"attempts": 1, "healthy_entry": 0, "healthy_management": 1, "unhealthy": 0}
    assert result["observation"]["coverage_ms"] == {"entry": 0, "management": 0}
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("candle_age", [None, 900_000, 1])
async def test_no_signal_requires_fresh_candle_for_entry_evidence(client, monkeypatch, candle_age):
    app = client.app
    app.state.engine.start(paper_config())
    candles = [] if candle_age is None else [SimpleNamespace(close_time=millis() - candle_age)]
    monkeypatch.setattr(app.state.market, "quote", AsyncMock(side_effect=lambda *_: fresh_quote()))
    monkeypatch.setattr(app.state.market, "exchange_info", AsyncMock(return_value=INFO))
    monkeypatch.setattr(
        app.state.market, "clock", AsyncMock(return_value=SimpleNamespace(synchronized=True))
    )
    monkeypatch.setattr(app.state.market, "latest", AsyncMock(return_value=candles))
    monkeypatch.setattr("spotlab.main.validate_candles", lambda *a, **k: SimpleNamespace(ok=True))
    monkeypatch.setattr("spotlab.main.strategy_signal", lambda *a, **k: [0])
    await paper_cycle(app)
    result = report(client)
    counts = result["observation"]["counts"]
    assert counts["attempts"] == 1
    assert counts["healthy_management"] == 0
    assert counts["healthy_entry"] == int(candle_age == 1)
    assert counts["unhealthy"] == int(candle_age != 1)
    assert result["results"]["fill_count"] == 0
    assert result["results"]["scope"] == "entire_paper_session"
    assert result["live_eligible"] is False


@pytest.mark.asyncio
async def test_failed_worker_evidence_survives_archive_without_polling_growth(client, monkeypatch):
    app = client.app
    engine = app.state.engine
    engine.start(paper_config())
    monkeypatch.setattr(
        app.state.market, "quote", AsyncMock(side_effect=MarketDataUnavailable("synthetic outage"))
    )
    await paper_cycle(app)
    current = report(client)
    assert current["observation"]["counts"]["unhealthy"] == 1
    assert engine.snapshot()["entries_enabled"] is False
    # Stopped and flat means no further worker attempt, even when polled.
    await paper_cycle(app)
    assert report(client)["observation"] == current["observation"]
    old_id = current["session_id"]
    engine.archive(old_id)
    before = engine.archive_detail(old_id)
    path = f"/api/paper/archives/{old_id}/observation"
    archived = client.get(path)
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert archived.json()["observation"]["counts"] == current["observation"]["counts"]
    assert client.get(path).json() == archived.json()
    assert engine.archive_detail(old_id) == before
    assert report(client)["observation"] is None
    assert client.get("/api/paper/archives/unknown/observation").status_code == 404
