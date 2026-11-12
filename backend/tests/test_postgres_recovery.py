"""Opt-in pg_dump/pg_restore drill against CI's disposable PostgreSQL 16 only."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from contextlib import ExitStack
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError

from spotlab.contracts import Quote
from spotlab.engine import PaperConfig, PaperEngine
from spotlab.orders import (
    TESTNET_ACCOUNT_VENUE,
    AccountBindingError,
    OrderRequest,
    OrderState,
    OrderStore,
)
from spotlab.testnet_cycle import CyclePlan
from spotlab.testnet_cycle import TestnetCycle as Cycle
from spotlab.testnet_panel import TestnetPanel as Panel

_OPT_IN = "SPOTLAB_REQUIRE_POSTGRES_RECOVERY"
_CONTAINER = "TEST_POSTGRES_RECOVERY_CONTAINER"
_ADMIN_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/spotlab_recovery"
_SAFE_CONTAINER = re.compile(r"^[a-f0-9]{12,64}$")
_SAFE_DATABASE = re.compile(r"^spotlab_recovery_[a-f0-9]{24}_(source|target|bad|guard)$")
_AT = 1_790_000_000_000
_UID = 12345


def _required_environment() -> str:
    if os.getenv(_OPT_IN) != "1":
        pytest.skip("dedicated PostgreSQL recovery drill is not enabled")
    container = os.getenv(_CONTAINER)
    if not container or not _SAFE_CONTAINER.fullmatch(container):
        pytest.fail(f"{_CONTAINER} must be the dedicated service's hexadecimal container ID")
    return container


def _database_url(name: str) -> str:
    assert _SAFE_DATABASE.fullmatch(name)
    return make_url(_ADMIN_URL).set(database=name).render_as_string(hide_password=False)


def _quote(price: str, at: int) -> Quote:
    return Quote("BTCUSDT", D(price), D(price), D("1000"), D("1000"), at)


def _config() -> PaperConfig:
    return PaperConfig.model_validate(
        {
            "venue": "global", "symbol": "BTCUSDT", "interval": "5m",
            "strategy": "pullback", "capital": "1000", "risk_per_trade": ".01",
            "daily_loss_limit": ".2", "max_drawdown": ".8", "max_open_risk": ".02",
            "target_pct": ".5", "stop_pct": ".5", "fee_rate": ".001",
            "slippage_bps": "0", "max_spread_bps": "30", "hold_minutes": 60,
            "max_trades_per_day": 10, "max_consecutive_losses": 3,
            "authorization_minutes": 120,
        }
    )


def _tick(engine: PaperEngine, price: str, at: int, *, signal: bool, scope: str) -> dict:
    return engine.tick(
        _quote(price, at), signal=signal, candle_time=at - 1, step=D(".01"),
        min_notional=D("5"), now=at, observation_scope=scope,
        expected_session_id=engine.snapshot()["session_id"],
    )


def _seed_source(url: str) -> tuple[dict, dict]:
    with ExitStack() as resources:
        paper = PaperEngine(url)
        resources.callback(paper.db.dispose)
        archived_id = paper.snapshot()["session_id"]
        paper.start(_config(), _AT, expected_session_id=archived_id)
        _tick(paper, "100", _AT, signal=True, scope="entry")
        _tick(paper, "160", _AT + 1_000, signal=False, scope="management")
        paper.stop(expected_session_id=archived_id)
        fresh = paper.archive(archived_id, _AT + 2_000)
        paper.start(_config(), _AT + 3_000, expected_session_id=fresh["session_id"])
        _tick(paper, "100", _AT + 3_000, signal=True, scope="entry")
        _tick(paper, "90", _AT + 4_000, signal=False, scope="management")
        active = _tick(paper, "90", _AT + 5_000, signal=False, scope="management")
        assert active["entries_enabled"] and active["position"] is not None
        assert active["observation"]["coverage_ms"]["management"] > 0
        assert D(active["observed_drawdown"]["maximum_drawdown"]) > 0
        archived = paper.archive_detail(archived_id)
        paper.db.dispose()

        store = OrderStore(url, recover_submitting=False)
        resources.callback(store.db.dispose)
        store.bind_account(TESTNET_ACCOUNT_VENUE, _UID)
        store.db.dispose()
        cycle = Cycle(url, Mock())
        resources.callback(cycle.store.db.dispose)
        cycle.store.db.dispose()
        store = OrderStore(url, recover_submitting=False)
        resources.callback(store.db.dispose)
        filled = OrderRequest(
            "filled-buy", "recovery-filled", "BTCUSDT", "BUY", D(".5"), D("100"),
            "USDT", "recovery-scope",
        )
        pending = OrderRequest(
            "pending-oco", "recovery-pending", "BTCUSDT", "SELL", D(".2"), D("120"),
            "USDT", "recovery-scope", "OCO",
        )
        store.create_or_get(filled)
        with store._writer() as connection:
            connection.execute(
                text(
                    "UPDATE order_intents SET state='FILLED',exchange_order_id='BTCUSDT:77',"
                    "confirmed_quantity='.5',confirmed_quote_quantity='50',confirmed_fee_quote='.05',"
                    "exchange_cumulative_quantity='.5',updated_at=:at WHERE intent_id='filled-buy'"
                ), {"at": _AT},
            )
            connection.execute(
                text(
                    "INSERT INTO order_fills (exchange_trade_id,intent_id,quantity,quote_quantity,"
                    "fee_quote,fee_asset,fee_base) VALUES "
                    "('trade-77','filled-buy','.5','50','.05','USDT','0')"
                )
            )
        store.create_or_get(pending)
        assert store.claim_submission(pending.intent_id)
        store.record_account_incident("account_history_missing")
        with store._writer() as connection:
            connection.execute(text(
                "INSERT INTO protection_intents VALUES ('pending-oco','80','target-1','stop-1')"
            ))
            plan = CyclePlan(
                "recovery-cycle", "BTCUSDT", D(".2"), D("100"), D("120"),
                D("80"), D("25"), _AT + 60_000,
            )
            connection.execute(
                text("INSERT INTO testnet_cycles VALUES "
                     "('recovery-cycle',:plan,'PROTECTED','.2','0',NULL,0,:at)"),
                {"plan": plan.serialize(), "at": _AT},
            )
        store.db.dispose()
        return active, archived


def _schema_and_rows(engine: Engine) -> dict[str, object]:
    inspector = inspect(engine)
    schema: dict[str, object] = {}
    rows: dict[str, object] = {}
    with engine.connect() as connection:
        for table in sorted(inspector.get_table_names()):
            columns = inspector.get_columns(table)
            schema[table] = {
                "columns": tuple(
                    (c["name"], str(c["type"]), bool(c["nullable"]), c.get("default"))
                    for c in columns
                ),
                "primary_key": tuple(
                    inspector.get_pk_constraint(table).get("constrained_columns") or ()
                ),
                "foreign_keys": tuple(sorted(
                    (
                        tuple(f.get("constrained_columns") or ()),
                        f.get("referred_table"),
                        tuple(f.get("referred_columns") or ()),
                    )
                    for f in inspector.get_foreign_keys(table)
                )),
                "uniques": tuple(sorted(
                    tuple(u.get("column_names") or ())
                    for u in inspector.get_unique_constraints(table)
                )),
            }
            quoted = '"' + table.replace('"', '""') + '"'
            values = [tuple(row) for row in connection.execute(text(f"SELECT * FROM {quoted}"))]
            rows[table] = tuple(sorted(values, key=repr))
    return {"schema": schema, "rows": rows}


def _image(url: str) -> dict[str, object]:
    engine = create_engine(url)
    try:
        return _schema_and_rows(engine)
    finally:
        engine.dispose()


def _raw_paper_state(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            payload = connection.execute(
                text("SELECT payload FROM bot_state WHERE id=1")
            ).scalar_one()
        return json.loads(payload)
    finally:
        engine.dispose()


def _assert_identical(source: Mapping[str, object], target: Mapping[str, object]) -> None:
    assert target["schema"] == source["schema"]
    assert target["rows"] == source["rows"]


def _without_paper_tables(image: Mapping[str, object]) -> dict[str, object]:
    omitted = {"bot_state", "paper_archives"}
    return {
        section: {
            table: value
            for table, value in mapping.items()
            if table not in omitted
        }
        for section, mapping in image.items()
        if isinstance(mapping, dict)
    }


def _assert_restored_order_constraints(engine: Engine, pristine: Mapping[str, object]) -> None:
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO order_intents SELECT * FROM order_intents "
                    "WHERE intent_id='filled-buy'"
                )
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO order_fills "
                    "SELECT 'orphan-trade','missing-intent',quantity,quote_quantity,"
                    "fee_quote,fee_asset,fee_base FROM order_fills "
                    "WHERE exchange_trade_id='trade-77'"
                )
            )
    _assert_identical(pristine, _schema_and_rows(engine))


def _docker(
    container: str, *command: str, stdin=None, stdout=None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", "exec", *(["-i"] if stdin is not None else []), container, *command],
        check=False, stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60,
    )


def _verify_same_pg16(container: str, admin: Engine) -> None:
    with admin.connect() as connection:
        host_id = str(
            connection.execute(
                text("SELECT system_identifier FROM pg_control_system()")
            ).scalar_one()
        )
        host_major = int(connection.execute(text("SHOW server_version_num")).scalar_one()) // 10_000
    inside_id = _docker(
        container, "psql", "--no-password", "--username=postgres",
        "--dbname=spotlab_recovery", "--tuples-only",
        "--no-align", "--command=SELECT system_identifier FROM pg_control_system()",
    )
    tools = _docker(container, "pg_dump", "--version")
    assert inside_id.returncode == 0, inside_id.stderr.decode(errors="replace")
    assert host_id == inside_id.stdout.decode().strip()
    assert host_major == 16
    assert tools.returncode == 0 and tools.stdout.decode().startswith("pg_dump (PostgreSQL) 16.")


def _run_dump(container: str, source: str, archive: Path) -> None:
    assert _SAFE_DATABASE.fullmatch(source)
    with archive.open("wb") as output:
        os.chmod(archive, 0o600)
        result = _docker(
            container, "pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-acl",
            "--username=postgres", "--dbname", source, stdout=output,
        )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert archive.read_bytes().startswith(b"PGDMP")


def _restore(
    container: str, target_url: str, target: str, archive: Path
) -> subprocess.CompletedProcess[bytes]:
    assert _SAFE_DATABASE.fullmatch(target)
    guard = create_engine(target_url)
    try:
        if inspect(guard).get_table_names():
            raise ValueError("recovery target must be an empty database")
    finally:
        guard.dispose()
    with archive.open("rb") as payload:
        return _docker(
            container, "pg_restore", "--single-transaction", "--exit-on-error", "--no-owner",
            "--no-acl", "--no-password", "--username=postgres", "--dbname", target,
            stdin=payload,
        )


def _create_database(admin: Engine, name: str, created: list[str]) -> None:
    assert _SAFE_DATABASE.fullmatch(name)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0')
    created.append(name)


def _drop_created(admin: Engine, created: list[str]) -> None:
    failures: list[BaseException] = []
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        for name in reversed(created):
            assert _SAFE_DATABASE.fullmatch(name)
            try:
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except BaseException as error:
                failures.append(error)
    if failures:
        raise ExceptionGroup("failed to remove recovery drill databases", failures)


def test_comparison_rejects_an_altered_or_omitted_row() -> None:
    expected = {"schema": {"t": ("id",)}, "rows": {"t": ((1, "preserve"),)}}
    for bad in (
        {"schema": expected["schema"], "rows": {"t": ((1, "changed"),)}},
        {"schema": expected["schema"], "rows": {"t": ()}},
    ):
        with pytest.raises(AssertionError):
            _assert_identical(expected, bad)


def test_pg16_dump_restore_recovery_drill(tmp_path: Path) -> None:
    container = _required_environment()
    fixture_id = uuid.uuid4().hex[:24]
    names = {
        suffix: f"spotlab_recovery_{fixture_id}_{suffix}"
        for suffix in ("source", "target", "bad", "guard")
    }
    admin = create_engine(_ADMIN_URL)
    created: list[str] = []
    resources = ExitStack()
    try:
        _verify_same_pg16(container, admin)
        for name in names.values():
            _create_database(admin, name, created)
        source_url, target_url = _database_url(names["source"]), _database_url(names["target"])
        original_paper, original_archive = _seed_source(source_url)
        source_engine = create_engine(source_url)
        resources.callback(source_engine.dispose)
        source_image = _schema_and_rows(source_engine)
        source_engine.dispose()

        archive = tmp_path / "spotlab.pgcustom"
        _run_dump(container, names["source"], archive)
        restored = _restore(container, target_url, names["target"], archive)
        assert restored.returncode == 0, restored.stderr.decode(errors="replace")
        target_engine = create_engine(target_url)
        resources.callback(target_engine.dispose)
        target_image = _schema_and_rows(target_engine)
        _assert_identical(source_image, target_image)
        _assert_restored_order_constraints(target_engine, target_image)
        target_engine.dispose()

        raw_before_startup = _raw_paper_state(target_url)
        paper = PaperEngine(target_url)
        resources.callback(paper.db.dispose)
        raw_after_startup = _raw_paper_state(target_url)
        expected_raw = copy.deepcopy(raw_before_startup)
        expected_raw["entries_enabled"] = False
        expected_raw["reason"] = raw_after_startup["reason"]
        expected_raw["audit"] = raw_after_startup["audit"]
        expected_raw["observation"]["interruptions"]["restart"] += 1
        expected_raw["observation"]["continuity"] = None
        assert raw_after_startup["audit"][:-1] == raw_before_startup["audit"]
        assert raw_after_startup["audit"][-1]["event"] == "restart"
        assert raw_after_startup == expected_raw
        restarted = paper.snapshot()
        assert restarted["session_id"] == original_paper["session_id"]
        expected = copy.deepcopy(original_paper)
        expected["entries_enabled"] = False
        expected["reason"] = restarted["reason"]
        expected["observation"]["interruptions"]["restart"] += 1
        assert restarted["audit"][:-1] == expected["audit"]
        assert restarted["audit"][-1]["event"] == "restart"
        expected["audit"] = restarted["audit"]
        assert restarted == expected
        assert paper.archives()["total"] == 1
        assert paper.archive_detail(original_archive["state"]["session_id"]) == original_archive
        paper.db.dispose()

        before_false_store = _image(target_url)
        unchanged = OrderStore(target_url, recover_submitting=False)
        resources.callback(unchanged.db.dispose)
        assert unchanged.get("pending-oco").state is OrderState.SUBMITTING
        unchanged.require_account_binding(TESTNET_ACCOUNT_VENUE, _UID)
        with pytest.raises(AccountBindingError, match="account_binding_mismatch"):
            unchanged.require_account_binding(TESTNET_ACCOUNT_VENUE, _UID + 1)
        assert unchanged.account_binding_reason() == "account_history_missing"
        unchanged.db.dispose()
        after_false_store = _image(target_url)
        assert _without_paper_tables(after_false_store) == _without_paper_tables(before_false_store)
        recovered_store = OrderStore(target_url)
        resources.callback(recovered_store.db.dispose)
        assert recovered_store.get("pending-oco").state is OrderState.UNKNOWN
        assert recovered_store.account_binding_reason() == "account_history_missing"
        recovered_store.db.dispose()

        before_panel = _image(target_url)
        panel = Panel(target_url, tmp_path / "credentials-must-not-be-read.json")
        panel.credentials_loader = Mock(
            side_effect=AssertionError("no credentials or exchange I/O")
        )
        panel.transport_factory = Mock(side_effect=AssertionError("no exchange transport"))
        panel_status = asyncio.run(panel.status())
        assert panel_status["ledger_status"] == "ready"
        assert panel_status["cycles"] == [{
            "cycle_id": "recovery-cycle", "symbol": "BTCUSDT",
            "state": "PROTECTION_UNVERIFIED", "stop_entry_requested": False,
            "updated_at_ms": _AT,
        }]
        assert panel_status["watcher"]["phase"] == "idle"
        assert not panel_status["watcher"]["allow_mutations"]
        assert not panel_status["watcher"]["manager_running"]
        panel.credentials_loader.assert_not_called()
        panel.transport_factory.assert_not_called()
        after_panel = _image(target_url)
        _assert_identical(before_panel, after_panel)

        corrupt = tmp_path / "truncated.pgcustom"
        corrupt.write_bytes(archive.read_bytes()[: max(16, archive.stat().st_size // 3)])
        os.chmod(corrupt, 0o600)
        failed = _restore(container, _database_url(names["bad"]), names["bad"], corrupt)
        assert failed.returncode != 0
        bad_engine = create_engine(_database_url(names["bad"]))
        resources.callback(bad_engine.dispose)
        assert inspect(bad_engine).get_table_names() == []
        bad_engine.dispose()

        guard_engine = create_engine(_database_url(names["guard"]))
        resources.callback(guard_engine.dispose)
        with guard_engine.begin() as connection:
            connection.execute(text("CREATE TABLE recovery_sentinel (value TEXT NOT NULL)"))
            connection.execute(text("INSERT INTO recovery_sentinel VALUES ('preserve')"))
        with pytest.raises(ValueError, match="empty database"):
            _restore(container, _database_url(names["guard"]), names["guard"], archive)
        with guard_engine.connect() as connection:
            value = connection.execute(
                text("SELECT value FROM recovery_sentinel")
            ).scalar_one()
            assert value == "preserve"
        guard_engine.dispose()
    finally:
        try:
            resources.close()
        finally:
            try:
                _drop_created(admin, created)
            finally:
                admin.dispose()


def test_recovery_opt_in_is_explicit_and_missing_service_fails(monkeypatch) -> None:
    monkeypatch.delenv(_OPT_IN, raising=False)
    monkeypatch.delenv(_CONTAINER, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _required_environment()
    monkeypatch.setenv(_OPT_IN, '1')
    for container in ('', 'postgres', '--privileged', 'g' * 64):
        monkeypatch.setenv(_CONTAINER, container)
        with pytest.raises(pytest.fail.Exception):
            _required_environment()
    monkeypatch.setenv(_CONTAINER, 'a' * 64)
    assert _required_environment() == 'a' * 64
