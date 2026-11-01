"""Persisted maximum drawdown from valid marked-equity samples."""

import json
from decimal import Decimal as D

import pytest
from backend.tests.test_api import paper_config

from spotlab.contracts import Quote
from spotlab.engine import PaperEngine

AT = 1_810_000_000_000


def quote(price: str, offset: int = 0, quantity: str = "1000") -> Quote:
    at = AT + offset
    return Quote("BTCUSDT", D(price), D(price), D(quantity), D(quantity), at)


def tick(
    engine: PaperEngine,
    price: str,
    offset: int,
    *,
    signal: bool = False,
    quantity: str = "1000",
    now_offset: int | None = None,
) -> dict:
    observed = quote(price, offset, quantity)
    return engine.tick(
        observed,
        signal=signal,
        candle_time=observed.observed_at - 1,
        step=D(".01"),
        min_notional=D(5),
        now=AT + (offset if now_offset is None else now_offset),
        expected_session_id=engine.snapshot()["session_id"],
    )


def wide_config():
    return paper_config().model_copy(
        update={"stop_pct": D(".5"), "target_pct": D(".5"), "max_drawdown": D(".8")}
    )


def test_new_session_is_unknown_until_first_mark_then_tracks_open_position_trough(tmp_path):
    engine = PaperEngine(f"sqlite:///{tmp_path / 'paper.sqlite'}")
    try:
        engine.start(wide_config(), AT)
        identity = engine.snapshot()["session_id"]
        baseline = engine.observation_report(identity, now=AT)["results"]["observed_drawdown"]
        assert baseline == {
            "schema_version": 1,
            "history_complete": True,
            "maximum_drawdown": None,
            "first_marked_at": None,
            "last_marked_at": None,
            "maximum_drawdown_at": None,
            "last_quote_at": None,
            "available": False,
            "current_drawdown": None,
            "quote_age_ms": None,
            "freshness": "unavailable",
        }

        opened = tick(engine, "100", 0, signal=True)
        entry_max = D(opened["observed_drawdown"]["maximum_drawdown"])
        assert opened["position"] is not None and entry_max > 0
        trough = tick(engine, "90", 1000)
        worst = D(trough["drawdown"])
        assert trough["position"] is not None
        assert D(trough["observed_drawdown"]["maximum_drawdown"]) == worst > entry_max
        assert trough["observed_drawdown"]["maximum_drawdown_at"] == AT + 1000

        recovered = tick(engine, "110", 2000)
        assert D(recovered["drawdown"]) < worst
        assert D(recovered["observed_drawdown"]["maximum_drawdown"]) == worst
        report = engine.observation_report(identity, now=AT + 7001)["results"][
            "observed_drawdown"
        ]
        assert report["current_drawdown"] == recovered["drawdown"]
        assert report["maximum_drawdown"] == trough["observed_drawdown"]["maximum_drawdown"]
        assert report["quote_age_ms"] == 5001 and report["freshness"] == "stale"
    finally:
        engine.db.dispose()


def test_partial_exit_marks_do_not_erase_worst_drawdown(tmp_path):
    engine = PaperEngine(f"sqlite:///{tmp_path / 'paper.sqlite'}")
    try:
        engine.start(wide_config(), AT)
        tick(engine, "100", 0, signal=True)
        engine.request_close()
        partial = tick(engine, "98", 1000, quantity=".05")
        assert partial["position"] is not None
        worst = partial["observed_drawdown"]["maximum_drawdown"]
        assert D(worst) == D(partial["drawdown"])
        finished = tick(engine, "105", 2000)
        assert finished["position"] is None
        assert D(finished["drawdown"]) < D(worst)
        assert finished["observed_drawdown"]["maximum_drawdown"] == worst
    finally:
        engine.db.dispose()


def test_invalid_quote_and_worker_failure_leave_financial_marks_unchanged(tmp_path):
    engine = PaperEngine(f"sqlite:///{tmp_path / 'paper.sqlite'}")
    try:
        engine.start(wide_config(), AT)
        marked = tick(engine, "100", 0, signal=True)["observed_drawdown"]
        stale = tick(engine, "80", 0, now_offset=5001)
        assert stale["observed_drawdown"] == marked
        identity = stale["session_id"]
        failed = engine.record_worker_failure(
            "management", expected_session_id=identity, now=AT + 6000
        )
        assert failed["observed_drawdown"] == marked
        regressed = engine.observation_report(identity, now=AT - 1)["results"][
            "observed_drawdown"
        ]
        assert regressed["freshness"] == "clock_regression"
        assert regressed["quote_age_ms"] is None
    finally:
        engine.db.dispose()


def test_restart_archive_and_schema_four_restore_retain_maximum(tmp_path):
    path = tmp_path / "source.sqlite"
    engine = PaperEngine(f"sqlite:///{path}")
    engine.start(wide_config(), AT)
    identity = engine.snapshot()["session_id"]
    tick(engine, "100", 0, signal=True)
    trough = tick(engine, "90", 1000)["observed_drawdown"]
    engine.request_close()
    tick(engine, "105", 2000)
    engine.db.dispose()

    restarted = PaperEngine(f"sqlite:///{path}")
    try:
        assert restarted.snapshot()["observed_drawdown"] == trough | {
            "last_marked_at": AT + 2000,
            "last_quote_at": AT + 2000,
        }
        restarted.archive(identity, AT + 3000)
        archived = restarted.archive_observation_report(identity)["results"]["observed_drawdown"]
        assert archived["maximum_drawdown"] == trough["maximum_drawdown"]
        backup = tmp_path / "backup.json"
        restarted.backup(str(backup))
        assert json.loads(backup.read_text())["schema"] == 4

        restored = PaperEngine(f"sqlite:///{tmp_path / 'restored.sqlite'}")
        try:
            restored.restore(str(backup))
            copied = restored.archive_observation_report(identity)["results"]["observed_drawdown"]
            assert copied == archived
        finally:
            restored.db.dispose()
    finally:
        restarted.db.dispose()


def test_schema_three_stays_unknown_then_starts_incomplete_history(tmp_path):
    source = PaperEngine(f"sqlite:///{tmp_path / 'source.sqlite'}")
    source.start(wide_config(), AT)
    backup = tmp_path / "schema3.json"
    source.backup(str(backup))
    payload = json.loads(backup.read_text())
    payload["schema"] = 3
    payload["current"].pop("observed_drawdown")
    backup.write_text(json.dumps(payload))
    source.db.dispose()

    target = PaperEngine(f"sqlite:///{tmp_path / 'target.sqlite'}")
    try:
        target.restore(str(backup))
        identity = target.snapshot()["session_id"]
        unavailable = target.observation_report(identity, now=AT)["results"]["observed_drawdown"]
        assert unavailable["available"] is False
        assert unavailable["history_complete"] is False
        first = tick(target, "100", 1000)["observed_drawdown"]
        assert first["history_complete"] is False
        assert first["first_marked_at"] == AT + 1000
    finally:
        target.db.dispose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("history_complete", 1),
        ("maximum_drawdown", "NaN"),
        ("maximum_drawdown", "1.1"),
        ("last_quote_at", True),
        ("last_quote_at", AT - 6000),
    ],
)
def test_malformed_schema_four_is_rejected_atomically(tmp_path, field, value):
    source = PaperEngine(f"sqlite:///{tmp_path / 'source.sqlite'}")
    source.start(wide_config(), AT)
    tick(source, "100", 0, signal=True)
    backup = tmp_path / "bad.json"
    source.backup(str(backup))
    payload = json.loads(backup.read_text())
    payload["current"]["observed_drawdown"][field] = value
    backup.write_text(json.dumps(payload))
    source.db.dispose()

    target = PaperEngine(f"sqlite:///{tmp_path / 'target.sqlite'}")
    try:
        before = target.snapshot()
        with pytest.raises(ValueError):
            target.restore(str(backup))
        assert target.snapshot() == before
        assert target.archives()["total"] == 0
    finally:
        target.db.dispose()
