"""Prospective coverage arithmetic and transactional evidence, with no external services."""

import json
from decimal import Decimal as D

import pytest
from backend.tests.test_api import paper_config
from backend.tests.test_orders import database_url as database_url
from sqlalchemy import event, text

from spotlab.contracts import Quote
from spotlab.engine import PaperConflictError, PaperEngine

AT = 1_800_000_000_000


@pytest.fixture
def engine(database_url):
    value = PaperEngine(database_url)
    value.start(paper_config(), AT)
    yield value
    value.db.dispose()


def sample(engine, offset, *, scope="entry", healthy=True, signal=False):
    at = AT + offset
    return engine.tick(
        Quote("BTCUSDT", D(100), D(100), D(1000), D(1000), at),
        signal=signal, candle_time=at - 1, step=D(".01"), min_notional=D(5),
        healthy=healthy, now=at, observation_scope=scope,
        expected_session_id=engine.snapshot()["session_id"],
    )["observation"]


def test_coverage_is_scoped_bounded_and_never_accrues_on_read(engine):
    assert sample(engine, 0)["coverage_ms"] == {"entry": 0, "management": 0}
    assert sample(engine, 2000)["coverage_ms"]["entry"] == 2000
    assert sample(engine, 12_000)["coverage_ms"]["entry"] == 12_000
    assert sample(engine, 14_000, scope="management")["coverage_ms"]["management"] == 0
    assert sample(engine, 16_000, scope="management")["coverage_ms"]["management"] == 2000
    sample(engine, 18_000, healthy=False)
    sample(engine, 20_000)
    sample(engine, 22_000)
    summary = sample(engine, 40_000)
    assert summary["coverage_ms"] == {"entry": 14_000, "management": 2000}
    assert summary["counts"] == {
        "attempts": 9, "healthy_entry": 6, "healthy_management": 2, "unhealthy": 1,
    }
    assert summary["gap_count"] == 1
    assert summary["max_gap_ms"] == 18_000
    before = engine.snapshot()
    report = engine.observation_report(before["session_id"], now=AT + 86_400_000)
    assert report["observation"]["coverage_ms"] == summary["coverage_ms"]
    assert report["observation_span_ms"] == 86_400_000
    assert report["live_eligible"] is False
    assert engine.snapshot() == before
    assert "continuity" not in summary and "last_process_id" not in summary


def test_clock_regression_cannot_credit_the_same_time_twice(engine):
    for offset in (0, 2000, 1000, 1500, 2000, 3000, 5000):
        summary = sample(engine, offset)
    assert summary["coverage_ms"]["entry"] == 4000
    assert summary["last_observed_at"] == AT + 5000
    assert summary["interruptions"]["clock_regression"] == 3
    report = engine.observation_report(engine.snapshot()["session_id"], now=AT + 1000)
    assert report["observation_span_ms"] >= 5000
    assert any("saat" in item for item in report["limitations"])


@pytest.mark.parametrize("action", ["stop", "request_close", "reauthorize"])
def test_control_actions_break_sampling_chain(engine, action):
    sample(engine, 0)
    sample(engine, 2000)
    if action == "reauthorize":
        engine.start(paper_config(), AT + 2500)
    else:
        getattr(engine, action)()
    summary = sample(engine, 4000)
    assert summary["coverage_ms"]["entry"] == 2000


def test_restart_code_change_and_new_session_keep_separate_provenance(engine, database_url):
    sample(engine, 0)
    sample(engine, 2000)
    previous = engine.snapshot()["session_id"]
    restarted = PaperEngine(database_url)
    try:
        restarted._code_identity = "synthetic-other-source"
        summary = sample(restarted, 4000)
        assert summary["coverage_ms"]["entry"] == 2000
        assert summary["process_count"] == 2
        assert summary["mixed_code_identity"] is True
        assert summary["interruptions"]["restart"] == 1
        assert summary["interruptions"]["code_change"] == 1
        restarted.archive(previous, AT + 5000)
        restarted.start(paper_config(), AT + 6000)
        assert sample(restarted, 7000)["process_count"] == 1
        assert sample(restarted, 8000)["coverage_ms"]["entry"] == 1000
        with pytest.raises(PaperConflictError):
            restarted.record_worker_failure("entry", expected_session_id=previous)
    finally:
        restarted.db.dispose()


def test_failed_persistence_rolls_back_trade_and_observation(engine):
    before = engine.snapshot()

    def reject(_conn, _cursor, statement, *_args):
        if statement.startswith("UPDATE bot_state"):
            raise RuntimeError("synthetic write failure")

    event.listen(engine.db, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="write failure"):
            sample(engine, 0, signal=True)
    finally:
        event.remove(engine.db, "before_cursor_execute", reject)
    assert engine.snapshot() == before
    assert sample(engine, 2000)["process_count"] == 1
    assert engine.snapshot()["fills"] == []


def test_backup_restores_evidence_and_archive_without_bridging(engine, tmp_path):
    sample(engine, 0)
    sample(engine, 2000)
    identity = engine.snapshot()["session_id"]
    engine.stop()
    engine.archive(identity, AT + 3000)
    old = engine.archive_observation_report(identity)
    engine.start(paper_config(), AT + 4000)
    sample(engine, 5000)
    sample(engine, 7000)
    source_id = engine.snapshot()["session_id"]
    backup = tmp_path / "copy.json"
    engine.backup(str(backup))
    assert json.loads(backup.read_text())["schema"] == 3
    target = PaperEngine(f"sqlite:///{tmp_path / 'restored.sqlite'}")
    try:
        target.restore(str(backup))
        assert target.snapshot()["session_id"] != source_id
        assert target.snapshot()["entries_enabled"] is False
        assert target.archive_observation_report(identity) == old
        summary = sample(target, 8000)
        assert summary["source_session_id"] == source_id
        assert summary["coverage_ms"]["entry"] == 2000
        assert summary["interruptions"]["restore"] == 1
        assert summary["process_count"] == 2
    finally:
        target.db.dispose()


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("counts", {"attempts": 999}),
    ("coverage_ms", {"entry": 999999, "management": 0}),
    ("config_sha256", "changed"), ("source_session_id", "bad"),
    ("last_process_id", ""), ("gap_count", -1), ("first_observed_at", AT + 9000),
])
def test_malformed_observation_backup_rejected_atomically(tmp_path, field, value):
    source = PaperEngine(f"sqlite:///{tmp_path / 'source.sqlite'}")
    target = PaperEngine(f"sqlite:///{tmp_path / 'target.sqlite'}")
    try:
        source.start(paper_config(), AT)
        sample(source, 0)
        sample(source, 2000)
        path = tmp_path / "backup.json"
        source.backup(str(path))
        backup = json.loads(path.read_text())
        backup["current"]["observation"][field] = value
        path.write_text(json.dumps(backup))
        before = target.snapshot()
        with pytest.raises(ValueError):
            target.restore(str(path))
        assert target.snapshot() == before
        assert target.archives()["total"] == 0
    finally:
        source.db.dispose()
        target.db.dispose()


def test_schema_two_and_existing_archive_never_backfill_observation(tmp_path):
    source = PaperEngine(f"sqlite:///{tmp_path / 'source.sqlite'}")
    target = PaperEngine(f"sqlite:///{tmp_path / 'target.sqlite'}")
    try:
        source.start(paper_config(), AT)
        sample(source, 0)
        source.stop()
        identity = source.snapshot()["session_id"]
        source.archive(identity, AT + 2000)
        path = tmp_path / "legacy.json"
        source.backup(str(path))
        backup = json.loads(path.read_text())
        backup["schema"] = 2
        backup["current"].pop("observation")
        backup["archives"][0]["state"].pop("observation")
        path.write_text(json.dumps(backup))
        target.restore(str(path))
        assert target.archive_observation_report(identity)["observation"] is None
        assert target.snapshot()["observation"] is None
        # An actual old stored archive is also readable, without mutation.
        with target.db.begin() as conn:
            raw = json.dumps(backup["archives"][0]["state"])
            conn.execute(text("UPDATE paper_archives SET payload=:raw"), {"raw": raw})
        assert target.archive_observation_report(identity)["evidence_status"] == "no_observations"
        with target.db.connect() as conn:
            assert conn.execute(text("SELECT payload FROM paper_archives")).scalar_one() == raw
    finally:
        source.db.dispose()
        target.db.dispose()
