import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import quote as urlquote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from spotlab.contracts import Quote
from spotlab.engine import PaperConfig, PaperConflictError, PaperEngine, empty_state

AT = 1_790_000_000_000


def config(**changes: object) -> PaperConfig:
    values = {
        "venue": "global",
        "symbol": "BTCUSDT",
        "interval": "5m",
        "strategy": "pullback",
        "capital": "1000",
        "risk_per_trade": "0.01",
        "daily_loss_limit": "0.05",
        "max_drawdown": "0.1",
        "max_open_risk": "0.02",
        "target_pct": "0.02",
        "stop_pct": "0.01",
        "fee_rate": "0.001",
        "slippage_bps": "0",
        "max_spread_bps": "30",
        "hold_minutes": 60,
        "max_trades_per_day": 10,
        "max_consecutive_losses": 3,
        "authorization_minutes": 120,
    }
    values.update(changes)
    return PaperConfig.model_validate(values)


def quote(price: str, at: int, quantity: str = "1000") -> Quote:
    return Quote("BTCUSDT", D(price), D(price), D(quantity), D(quantity), at)


def fill_round_trip(engine: PaperEngine, session_id: str) -> None:
    engine.tick(
        quote("100", AT),
        signal=True,
        candle_time=AT - 1,
        step=D(".01"),
        min_notional=D(5),
        now=AT,
        expected_session_id=session_id,
    )
    engine.tick(
        quote("102", AT + 1000),
        signal=False,
        candle_time=0,
        step=D(".01"),
        min_notional=D(5),
        now=AT + 1000,
        expected_session_id=session_id,
    )


@pytest.fixture(params=["sqlite", "postgres"])
def engine(tmp_path: Path, request: pytest.FixtureRequest):
    if request.param == "sqlite":
        instance = PaperEngine("sqlite:///" + str(tmp_path / "archive.sqlite"))
        yield instance
        instance.db.dispose()
        return
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("PostgreSQL service is exercised in CI")
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    instance = PaperEngine(database_url + "?options=" + urlquote("-csearch_path=" + schema))
    try:
        yield instance
    finally:
        instance.db.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_archive_is_immutable_and_replaces_with_fresh_empty_session(engine: PaperEngine) -> None:
    old_id = engine.snapshot()["session_id"]
    assert engine.start(config(), AT, expected_session_id=old_id)["started_at"] == AT
    fill_round_trip(engine, old_id)
    engine.stop(expected_session_id=old_id)
    fresh = engine.archive(old_id, AT + 2000)

    assert fresh["session_id"] != old_id
    assert fresh["started_at"] is None
    assert fresh["config"] is None and fresh["cash"] == "0" and fresh["fills"] == []
    detail = engine.archive_detail(old_id)
    archived = detail["state"]
    assert detail["archived_at"] == AT + 2000
    assert archived["cash"] == "1014.99532"
    assert archived["realized_pnl"] == "14.99532"
    assert len(archived["orders"]) == len(archived["fills"]) == 2
    assert D(archived["fills"][0]["fee"]) == D("0.834")
    assert archived["audit"][-1]["event"] == "archive"
    assert engine.archives()["items"][0]["fill_count"] == 2

    replacement = engine.start(
        config(capital="2000"), AT + 3000, expected_session_id=fresh["session_id"]
    )
    assert replacement["cash"] == replacement["equity"] == "2000"
    assert replacement["realized_pnl"] == "0"
    assert replacement["orders"] == replacement["fills"] == []


def test_concurrent_archive_succeeds_once(engine: PaperEngine) -> None:
    session_id = engine.snapshot()["session_id"]
    engine.start(config(), AT, expected_session_id=session_id)
    engine.stop(expected_session_id=session_id)

    def attempt() -> str:
        try:
            engine.archive(session_id)
        except PaperConflictError:
            return "conflict"
        return "archived"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["archived", "conflict"]
    assert engine.archives()["total"] == 1


def test_archive_refuses_active_partial_and_malformed_orders(engine: PaperEngine) -> None:
    session_id = engine.snapshot()["session_id"]
    engine.start(config(), AT, expected_session_id=session_id)
    with pytest.raises(PaperConflictError):
        engine.archive(session_id)
    engine.tick(
        quote("100", AT),
        signal=True,
        candle_time=AT - 1,
        step=D(".01"),
        min_notional=D(5),
        now=AT,
        expected_session_id=session_id,
    )
    engine.stop(expected_session_id=session_id)
    engine.tick(
        quote("102", AT + 1000, "2"),
        signal=False,
        candle_time=0,
        step=D(".01"),
        min_notional=D(5),
        now=AT + 1000,
        expected_session_id=session_id,
    )
    with pytest.raises(PaperConflictError):
        engine.archive(session_id)
    engine.tick(
        quote("102", AT + 2000),
        signal=False,
        candle_time=0,
        step=D(".01"),
        min_notional=D(5),
        now=AT + 2000,
        expected_session_id=session_id,
    )
    with engine.transaction() as (_, state):
        state["orders"].append({"status": "NEW"})
    with pytest.raises(PaperConflictError):
        engine.archive(session_id)


def test_failed_archive_insert_rolls_back_state(tmp_path: Path) -> None:
    engine = PaperEngine("sqlite:///" + str(tmp_path / "rollback.sqlite"))
    session_id = engine.snapshot()["session_id"]
    engine.start(config(), AT, expected_session_id=session_id)
    engine.stop(expected_session_id=session_id)
    before = engine.snapshot()
    with engine.db.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER reject_archive BEFORE INSERT ON paper_archives "
            "BEGIN SELECT RAISE(ABORT, 'reject'); END"
        )
    with pytest.raises(IntegrityError):
        engine.archive(session_id)
    assert engine.snapshot() == before
    assert engine.archives()["total"] == 0
    engine.db.dispose()


def test_schema_two_backup_restores_all_history(engine: PaperEngine, tmp_path: Path) -> None:
    first_id = engine.snapshot()["session_id"]
    engine.start(config(), AT, expected_session_id=first_id)
    fill_round_trip(engine, first_id)
    engine.stop(expected_session_id=first_id)
    empty = engine.archive(first_id, AT + 2000)
    engine.start(config(capital="2000"), AT + 3000, expected_session_id=empty["session_id"])
    second_id = engine.snapshot()["session_id"]
    engine.stop(expected_session_id=second_id)
    third = engine.archive(second_id, AT + 4000)
    engine.start(config(capital="2000"), AT + 5000, expected_session_id=third["session_id"])
    original_current = engine.snapshot()
    first_archive = engine.archive_detail(first_id)
    second_archive = engine.archive_detail(second_id)
    backup = tmp_path / "schema2.json"
    engine.backup(str(backup))

    restored_engine = PaperEngine("sqlite:///" + str(tmp_path / "restored.sqlite"))
    restored_engine.restore(str(backup))
    restored = restored_engine.snapshot()
    assert restored["session_id"] != original_current["session_id"]
    assert not restored["entries_enabled"]
    assert restored["cash"] == "2000" and restored["fills"] == []
    assert original_current["session_id"] in restored["audit"][-1]["message"]
    assert restored_engine.archives()["total"] == 2
    assert restored_engine.archive_detail(first_id) == first_archive
    assert restored_engine.archive_detail(second_id) == second_archive
    with pytest.raises(ValueError):
        restored_engine.restore(str(backup))
    assert restored_engine.archive_detail(first_id) == first_archive
    archived = restored_engine.archive_detail(first_id)["state"]
    assert archived["cash"] == "1014.99532"
    assert archived["realized_pnl"] == "14.99532"
    assert len(archived["fills"]) == 2
    restored_engine.db.dispose()


def test_legacy_restore_and_invalid_schema_two_are_atomic(tmp_path: Path) -> None:
    legacy = empty_state()
    legacy.pop("session_id")
    legacy.pop("started_at")
    legacy.pop("observation")
    legacy["audit"].append({"time": AT, "event": "paper_start", "message": "legacy"})
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps({"schema": 1, "state": legacy}), encoding="utf-8")
    target = PaperEngine("sqlite:///" + str(tmp_path / "legacy-target.sqlite"))
    old_empty_id = target.snapshot()["session_id"]
    target.restore(str(legacy_path))
    assert target.snapshot()["session_id"] != old_empty_id
    assert target.snapshot()["started_at"] is None
    target.db.dispose()

    source = PaperEngine("sqlite:///" + str(tmp_path / "source.sqlite"))
    backup_path = tmp_path / "bad-schema2.json"
    source.backup(str(backup_path))
    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    forged = dict(payload["current"])
    forged["config"] = config().model_dump(mode="json")
    forged["entries_enabled"] = True
    payload["archives"] = [{"session_id": forged["session_id"], "archived_at": AT, "state": forged}]
    backup_path.write_text(json.dumps(payload), encoding="utf-8")
    atomic = PaperEngine("sqlite:///" + str(tmp_path / "atomic.sqlite"))
    before = atomic.snapshot()
    with pytest.raises(ValueError):
        atomic.restore(str(backup_path))
    assert atomic.snapshot() == before
    assert atomic.archives()["total"] == 0
    source.db.dispose()
    atomic.db.dispose()


def test_legacy_database_migration_preserves_money_and_history(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    state = empty_state()
    state.pop("session_id")
    state.pop("started_at")
    state["cash"] = "123.45"
    state["equity"] = "234.56"
    state["audit"].append({"time": AT, "event": "paper_start", "message": "legacy"})
    db = create_engine("sqlite:///" + str(path))
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE bot_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"))
        conn.execute(
            text("INSERT INTO bot_state (id,payload) VALUES (1,:payload)"),
            {"payload": json.dumps(state)},
        )
    db.dispose()

    migrated = PaperEngine("sqlite:///" + str(path))
    snapshot = migrated.snapshot()
    uuid.UUID(snapshot["session_id"])
    assert snapshot["started_at"] == AT
    assert snapshot["cash"] == "123.45" and snapshot["equity"] == "234.56"
    assert snapshot["audit"] == state["audit"]
    migrated.db.dispose()


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize(
    "bad_path,bad_value",
    [
        (("position",), {"quantity": "8.34"}),
        (("position", "quantity"), "-1"),
        (("position", "quantity"), "0"),
        (("position", "symbol"), "SOLUSDT"),
        (("position", "entry"), "NaN"),
        (("position", "cost_basis"), -2),
        (("position", "exit_reason"), 4),
        (("cash",), 165.166),
        (("cash",), True),
        (("cash",), "NaN"),
        (("loss_streak",), -1),
        (("expires_at",), True),
        (("config", "fee_rate"), 0.001),
        (("day",), "invalid"),
        (("orders", 0, "status"), "NEW"),
        (("fills", 0, "price"), 100.0),
        (("fills", 0, "quantity"), "-1"),
        (("audit", 0, "message"), []),
    ],
)
def test_invalid_backup_rejected_without_modifying_target(tmp_path, schema, bad_path, bad_value):
    source = PaperEngine("sqlite:///" + str(tmp_path / "source.sqlite"))
    session_id = source.snapshot()["session_id"]
    source.start(config(), AT, expected_session_id=session_id)
    source.tick(
        quote("100", AT),
        signal=True,
        candle_time=AT - 1,
        step=D(".01"),
        min_notional=D(5),
        now=AT,
        expected_session_id=session_id,
    )
    path = tmp_path / "bad.json"
    source.backup(str(path))
    payload = json.loads(path.read_text())
    node = payload["current"]
    for part in bad_path[:-1]:
        node = node[part]
    node[bad_path[-1]] = bad_value
    if schema == 1:
        state = payload["current"]
        state.pop("session_id")
        state.pop("started_at")
        state.pop("observation")
        payload = {"schema": 1, "state": state}
    path.write_text(json.dumps(payload))
    target = PaperEngine("sqlite:///" + str(tmp_path / "target.sqlite"))
    before = target.snapshot()
    with pytest.raises(ValueError):
        target.restore(str(path))
    assert target.snapshot() == before
    assert target.archives()["total"] == 0
    source.db.dispose()
    target.db.dispose()


def test_archive_update_failure_rolls_back_insert(engine):
    from sqlalchemy import event

    session_id = engine.snapshot()["session_id"]
    engine.start(config(), AT, expected_session_id=session_id)
    engine.stop(expected_session_id=session_id)
    before = engine.snapshot()

    def fail_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE bot_state"):
            raise RuntimeError("synthetic failure after archive insertion")

    event.listen(engine.db, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError, match="after archive"):
            engine.archive(session_id)
    finally:
        event.remove(engine.db, "before_cursor_execute", fail_update)
    assert engine.snapshot() == before
    assert engine.archives()["total"] == 0


def test_archive_and_start_serialize_without_moving_authority(engine):
    session_id = engine.snapshot()["session_id"]
    engine.start(config(), AT, expected_session_id=session_id)
    engine.stop(expected_session_id=session_id)

    def mutate(action):
        try:
            if action == "start":
                engine.start(config(), AT, expected_session_id=session_id)
            else:
                engine.archive(session_id)
            return action
        except PaperConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(mutate, ["start", "archive"]))
    assert results.count("conflict") == 1
    state = engine.snapshot()
    if "archive" in results:
        assert state["session_id"] != session_id
        assert state["config"] is None and not state["entries_enabled"]
        assert engine.archives()["total"] == 1
    else:
        assert state["session_id"] == session_id and state["entries_enabled"]
        assert engine.archives()["total"] == 0


@pytest.mark.parametrize("schema", [1, 2])
def test_missing_config_cannot_mix_historical_profit_with_new_capital(tmp_path, schema):
    source = PaperEngine("sqlite:///" + str(tmp_path / "source.sqlite"))
    session_id = source.snapshot()["session_id"]
    source.start(config(), AT, expected_session_id=session_id)
    fill_round_trip(source, session_id)
    source.stop(expected_session_id=session_id)
    path = tmp_path / "missing-config.json"
    source.backup(str(path))
    payload = json.loads(path.read_text())
    state = payload["current"]
    state["config"] = None
    state["mode"] = "suggestions"
    state["started_at"] = None
    if schema == 1:
        state.pop("session_id")
        state.pop("started_at")
        state.pop("observation")
        payload = {"schema": 1, "state": state}
    path.write_text(json.dumps(payload))
    target = PaperEngine("sqlite:///" + str(tmp_path / "target.sqlite"))
    before = target.snapshot()
    with pytest.raises(ValueError, match="boş olmalıdır"):
        target.restore(str(path))
    assert target.snapshot() == before
    assert target.archives()["total"] == 0
    source.db.dispose()
    target.db.dispose()
