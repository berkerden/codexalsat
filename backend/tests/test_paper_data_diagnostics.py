"""Fixed-copy paper data diagnostics without network access or raw failure persistence."""

import json
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from backend.tests.test_api import client as client
from backend.tests.test_api import fresh_quote, paper_config
from backend.tests.test_paper_session_races import INFO

from spotlab.contracts import Quote
from spotlab.engine import DATA_ISSUE_MESSAGES, PaperConflictError, PaperEngine, millis
from spotlab.main import paper_cycle
from spotlab.market import MarketDataUnavailable


def issue_events(engine: PaperEngine) -> list[dict[str, object]]:
    return [event for event in engine.snapshot()["audit"] if event["event"] == "data_issue"]


def observed_tick(
    engine: PaperEngine,
    at: int,
    *,
    healthy: bool = True,
    diagnostic_issue: str | None = None,
    candle_time: int | None = None,
    step: D = D(".01"),
    quote: Quote | None = None,
    scope: str = "entry",
) -> dict[str, object]:
    return engine.tick(
        quote or Quote("BTCUSDT", D(100), D(100), D(1000), D(1000), at),
        signal=False,
        candle_time=at - 1 if candle_time is None else candle_time,
        step=step,
        min_notional=D(5),
        healthy=healthy,
        diagnostic_issue=diagnostic_issue,
        observation_scope=scope,
        expected_session_id=engine.snapshot()["session_id"],
        now=at,
    )


def test_tick_diagnostic_priority_and_hints_do_not_drive_trading(tmp_path):
    engine = PaperEngine(f"sqlite:///{tmp_path / 'paper.sqlite'}")
    try:
        at = 1_900_000_000_000
        engine.start(paper_config(), at)
        observed_tick(engine, at, healthy=True, diagnostic_issue="candle_quality")
        assert issue_events(engine) == []

        stale = Quote("WRONG", D(-1), D(0), D(-1), D(-1), at - 6000)
        observed_tick(engine, at + 1, quote=stale, step=D(0))
        assert issue_events(engine)[-1]["message"] == DATA_ISSUE_MESSAGES["quote_freshness"]

        engine.start(paper_config(), at + 2)
        invalid = Quote("WRONG", D(100), D(100), D(1), D(1), at + 3)
        observed_tick(engine, at + 3, quote=invalid, step=D(0))
        assert issue_events(engine)[-1]["message"] == DATA_ISSUE_MESSAGES["invalid_quote"]

        engine.start(paper_config(), at + 4)
        observed_tick(engine, at + 5, step=D(0), healthy=False, diagnostic_issue="clock_skew")
        assert issue_events(engine)[-1]["message"] == DATA_ISSUE_MESSAGES["symbol_rules"]

        engine.start(paper_config(), at + 6)
        observed_tick(engine, at + 7, healthy=False, diagnostic_issue="clock_skew")
        assert issue_events(engine)[-1]["message"] == DATA_ISSUE_MESSAGES["clock_skew"]

        engine.start(paper_config(), at + 8)
        observed_tick(engine, at + 9, candle_time=0)
        assert issue_events(engine)[-1]["message"] == DATA_ISSUE_MESSAGES["candle_freshness"]

        with pytest.raises(ValueError, match="sınıflandırması"):
            observed_tick(engine, at + 10, diagnostic_issue="raw-exception-text")
    finally:
        engine.db.dispose()


def test_management_exit_ignores_entry_hint_and_preserves_manual_intent(tmp_path):
    engine = PaperEngine(f"sqlite:///{tmp_path / 'paper.sqlite'}")
    try:
        at = 1_900_000_000_000
        engine.start(paper_config(), at)
        engine.tick(
            Quote("BTCUSDT", D(100), D(100), D(1000), D(1000), at),
            signal=True,
            candle_time=at - 1,
            step=D(".01"),
            min_notional=D(5),
            now=at,
        )
        engine.request_close()
        stale = Quote("BTCUSDT", D(99), D(99), D(1000), D(1000), at - 6000)
        blocked = observed_tick(engine, at + 1, quote=stale, scope="management")
        assert blocked["reason"] == (
            "Eski veya geçersiz veri: girişler durdu; çıkış güncel fiyat bekliyor."
        )
        assert blocked["position"]["exit_reason"] == "manual_close"

        closed = observed_tick(
            engine,
            at + 2,
            healthy=False,
            diagnostic_issue="candle_quality",
            quote=Quote("BTCUSDT", D(99), D(99), D(1000), D(1000), at + 2),
            scope="management",
        )
        assert closed["position"] is None
        assert issue_events(engine)[-1]["message"] == DATA_ISSUE_MESSAGES["quote_freshness"]
    finally:
        engine.db.dispose()


def test_data_issues_do_not_replace_expiry_or_risk_reasons(tmp_path):
    at = 1_900_000_000_000
    expired = PaperEngine(f"sqlite:///{tmp_path / 'expired.sqlite'}")
    try:
        expired.start(paper_config(), at)
        expiry_at = at + paper_config().authorization_minutes * 60_000
        observed_tick(expired, expiry_at, healthy=False, diagnostic_issue="clock_skew")
        expiry_reason = expired.snapshot()["reason"]
        assert expiry_reason == "Paper giriş yetkisinin süresi doldu; pozisyon yönetimi sürüyor."
        assert issue_events(expired)[-1]["message"] == DATA_ISSUE_MESSAGES["clock_skew"]
        observed_tick(
            expired,
            expiry_at + 1,
            quote=Quote("BTCUSDT", D(100), D(100), D(1), D(1), at),
        )
        assert expired.snapshot()["reason"] == (
            "Eski veya geçersiz veri: girişler durdu; çıkış güncel fiyat bekliyor."
        )
        expired.record_worker_failure(
            "entry",
            expected_session_id=expired.snapshot()["session_id"],
            now=expiry_at + 2,
            issue_code="latest",
        )
        assert expired.snapshot()["reason"] == (
            "Veri bağlantısı/kalitesi bozuldu; girişler durduruldu."
        )
    finally:
        expired.db.dispose()

    risk = PaperEngine(f"sqlite:///{tmp_path / 'risk.sqlite'}")
    try:
        cfg = paper_config().model_copy(update={"max_drawdown": D(".01")})
        risk.start(cfg, at)
        with risk.transaction() as (_, state):
            state["cash"] = "900"
            state["equity"] = "900"
            state["day_start_equity"] = "1000"
            state["peak"] = "1000"
        risk.tick(
            Quote("BTCUSDT", D(100), D(100), D(1000), D(1000), at + 1),
            signal=False,
            candle_time=at,
            step=D(".01"),
            min_notional=D(5),
            now=at + 1,
            healthy=False,
            diagnostic_issue="clock_skew",
            observation_scope="entry",
        )
        risk_reason = risk.snapshot()["reason"]
        assert risk_reason == "Günlük zarar veya düşüş sınırı: yeni girişler durduruldu."
        assert issue_events(risk)[-1]["message"] == DATA_ISSUE_MESSAGES["clock_skew"]
        observed_tick(
            risk,
            at + 2,
            quote=Quote("BTCUSDT", D(90), D(90), D(1), D(1), at - 6000),
            scope="management",
        )
        assert risk.snapshot()["reason"] == (
            "Eski veya geçersiz veri: girişler durdu; çıkış güncel fiyat bekliyor."
        )
    finally:
        risk.db.dispose()


def test_issue_dedup_rearms_after_health_authorization_and_restart(tmp_path):
    path = tmp_path / "paper.sqlite"
    engine = PaperEngine(f"sqlite:///{path}")
    at = 1_900_000_000_000
    try:
        engine.start(paper_config(), at)
        identity = engine.snapshot()["session_id"]
        for offset in (1, 2):
            engine.record_worker_failure(
                "entry", expected_session_id=identity, now=at + offset, issue_code="latest"
            )
        assert len(issue_events(engine)) == 1

        engine.start(paper_config(), at + 3)
        observed_tick(engine, at + 4)
        engine.record_worker_failure(
            "entry", expected_session_id=identity, now=at + 5, issue_code="latest"
        )
        assert len(issue_events(engine)) == 2

        engine.start(paper_config(), at + 6)
        engine.record_worker_failure(
            "entry", expected_session_id=identity, now=at + 7, issue_code="latest"
        )
        assert len(issue_events(engine)) == 3
    finally:
        engine.db.dispose()

    restarted = PaperEngine(f"sqlite:///{path}")
    try:
        restarted.record_worker_failure(
            "entry", expected_session_id=identity, now=at + 8, issue_code="latest"
        )
        assert len(issue_events(restarted)) == 4
    finally:
        restarted.db.dispose()


def test_retention_report_validation_backup_and_stale_fence(tmp_path):
    source = PaperEngine(f"sqlite:///{tmp_path / 'source.sqlite'}")
    at = 1_900_000_000_000
    try:
        source.start(paper_config(), at)
        identity = source.snapshot()["session_id"]
        with source.transaction() as (_, state):
            state["audit"].append({"time": at + 9999, "event": "data_issue", "message": "legacy"})
            state["audit"].append({"time": at + 9998, "event": "other", "message": "keep"})
        assert source.observation_report(identity, now=at)["last_data_issue"] is None

        for offset in range(105):
            code = "latest" if offset % 2 else "clock"
            source.record_worker_failure(
                "entry",
                expected_session_id=identity,
                now=at + offset,
                issue_code=code,
            )
        issues = issue_events(source)
        assert len(issues) == 100
        assert any(event["event"] == "other" for event in source.snapshot()["audit"])
        report = source.observation_report(identity, now=at + 200)
        assert report["last_data_issue"] == {
            "time": at + 104,
            "message": DATA_ISSUE_MESSAGES["clock"],
        }
        with source.transaction() as (_, state):
            state["audit"].append(
                {
                    "time": at,
                    "event": "data_issue",
                    "message": DATA_ISSUE_MESSAGES["latest"],
                }
            )
        assert source.observation_report(identity, now=at + 200)["last_data_issue"]["time"] == at
        before = source.snapshot()
        source.observation_report(identity, now=at + 300)
        assert source.snapshot() == before
        with pytest.raises(PaperConflictError):
            source.record_worker_failure(
                "entry", expected_session_id="stale-session", issue_code="latest"
            )
        assert source.snapshot() == before

        backup = tmp_path / "backup.json"
        with source.transaction() as (_, state):
            state["audit"].append(
                {"time": at + 9999, "event": "data_issue", "message": "legacy"}
            )
        source.backup(str(backup))
        assert "legacy" in backup.read_text(encoding="utf-8")
        target = PaperEngine(f"sqlite:///{tmp_path / 'target.sqlite'}")
        try:
            target.restore(str(backup))
            restored = target.observation_report(target.snapshot()["session_id"], now=at + 400)
            assert restored["last_data_issue"] == {
                "time": at,
                "message": DATA_ISSUE_MESSAGES["latest"],
            }
        finally:
            target.db.dispose()
    finally:
        source.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_code"),
    [
        ("initial_quote", "initial_quote"),
        ("exchange_info", "exchange_info"),
        ("clock", "clock"),
        ("latest", "latest"),
        ("refreshed_quote", "refreshed_quote"),
        ("clock_skew", "clock_skew"),
        ("candle_quality", "candle_quality"),
        ("candle_freshness", "candle_freshness"),
        ("symbol_rules", "symbol_rules"),
        ("malformed_rules", "symbol_rules"),
    ],
)
async def test_worker_stage_failures_persist_only_fixed_copy(
    client, monkeypatch, stage, expected_code
):
    app = client.app
    app.state.engine.start(paper_config())
    failure_text = "synthetic private exchange failure detail"
    failure = MarketDataUnavailable(failure_text)
    quote_results = [fresh_quote(), fresh_quote()]
    if stage == "initial_quote":
        quote_results = [failure]
    elif stage == "refreshed_quote":
        quote_results = [fresh_quote(), failure]
    monkeypatch.setattr(app.state.market, "quote", AsyncMock(side_effect=quote_results))
    monkeypatch.setattr(
        app.state.market,
        "exchange_info",
        AsyncMock(
            side_effect=failure if stage == "exchange_info" else None,
            return_value=(
                {**INFO, "filters": None} if stage == "malformed_rules"
                else {**INFO, "status": "HALT"} if stage == "symbol_rules"
                else INFO
            ),
        ),
    )
    monkeypatch.setattr(
        app.state.market,
        "clock",
        AsyncMock(
            side_effect=failure if stage == "clock" else None,
            return_value=SimpleNamespace(synchronized=stage != "clock_skew"),
        ),
    )
    monkeypatch.setattr(
        app.state.market,
        "latest",
        AsyncMock(
            side_effect=failure if stage == "latest" else None,
            return_value=(
                [] if stage == "candle_freshness"
                else [SimpleNamespace(close_time=millis() - 1)]
            ),
        ),
    )
    monkeypatch.setattr(
        "spotlab.main.validate_candles",
        lambda *a, **k: SimpleNamespace(ok=stage != "candle_quality"),
    )
    monkeypatch.setattr("spotlab.main.strategy_signal", lambda *a, **k: [0])

    await paper_cycle(app)

    raw = json.dumps(app.state.engine.snapshot(), ensure_ascii=False)
    assert failure_text not in raw
    event = issue_events(app.state.engine)[-1]
    assert event["message"] == DATA_ISSUE_MESSAGES[expected_code]
    state = app.state.engine.snapshot()
    assert state["orders"] == [] and state["fills"] == []
    if stage != "candle_freshness":
        assert state["entries_enabled"] is False
    else:
        # Existing no-signal stale-candle behavior is still wait-without-trading.
        assert state["entries_enabled"] is True
    assert app.state.engine.observation_report(
        app.state.engine.snapshot()["session_id"]
    )["last_data_issue"] == {"time": event["time"], "message": event["message"]}
