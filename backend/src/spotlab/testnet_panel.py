"""Local HTTP control plane for one existing Spot Testnet cycle watcher."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from spotlab.cycle_watch import WatchOptions, run_watch
from spotlab.manager_lock import ManagerBusy, ManagerLockError
from spotlab.orders import AccountBindingError
from spotlab.testnet import BinanceTestnetTransport, TestnetCredentials
from spotlab.testnet_cycle import CLOSED_CYCLES, TestnetCycle

SUPPORTED_SYMBOLS = frozenset({"BTCUSDT", "SOLUSDT"})
FRESH_COVERAGE_MS = 5_000
STALE_WATCH_MS = 10_000

_SNAPSHOT_KEYS = frozenset(
    {
        "cycle_id",
        "mode",
        "production_enabled",
        "state",
        "reason",
        "stop_entry_requested",
        "entry_state",
        "protection_state",
        "owned_quantity",
        "dust_quantity",
        "unprotected_quantity",
        "coverage_observation_fresh",
        "observed_at_ms",
        "manager_running",
        "health",
        "checked_at_ms",
        "last_success_at_ms",
        "consecutive_failures",
        "retry_in_seconds",
        "resume_required",
    }
)
_SAFE_REASONS = frozenset(
    {
        "RetryableTransportError",
        "TimeoutError",
        "account_binding_invalid",
        "account_binding_mismatch",
        "account_binding_required",
        "account_history_missing",
        "account_identity_invalid",
        "account_identity_unavailable",
        "account_open_orders_vs_local_ledger",
        "account_snapshot_changed",
        "below_protection_lot",
        "cancel_pending_query_only",
        "coverage_requires_observation",
        "credentials_unavailable",
        "duplicate_remote_client_id",
        "duplicate_remote_order_id",
        "entry_not_sent",
        "entry_record_missing",
        "entry_trade_details_incomplete",
        "interrupted",
        "known_order_identity_mismatch",
        "local_exchange_identity_invalid",
        "local_inventory_invalid",
        "local_ledger_changed",
        "management_confirmation_required",
        "manager_busy",
        "manager_lock_lost",
        "manager_lock_not_held",
        "migration_required",
        "needs_reconciliation",
        "oco_metadata_invalid",
        "oco_metadata_missing",
        "open_orders_changed_after_preflight",
        "preflight_clock_drift_after_audit",
        "preflight_stale_after_audit",
        "protective_list_terminal_with_inventory",
        "residual_owned_inventory",
        "startup_audit_unavailable",
        "suspicious_orphan_order",
        "terminal_order_still_open",
        "terminal_trades_incomplete",
        "untracked_other_order_count",
        "watch_cancelled",
        "watch_retry_pending",
        "watch_stopped",
        "watcher_failed",
    }
)


class WatchStartInput(BaseModel):
    """The browser may select an existing cycle and grant Testnet mutation authority."""

    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    confirm_testnet: StrictBool = False


class WatchStopInput(BaseModel):
    """Stop carries no browser-selected trading or filesystem parameters."""

    model_config = ConfigDict(extra="forbid")


class PanelError(RuntimeError):
    """Sanitized controller error with an HTTP status suitable for the local API."""

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class LedgerCycle(BaseModel):
    cycle_id: str
    symbol: Literal["BTCUSDT", "SOLUSDT"]
    state: str
    stop_entry_requested: bool
    updated_at_ms: int


class LedgerRead(BaseModel):
    status: Literal["ready", "missing", "unavailable"]
    cycles: list[LedgerCycle]


def _sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).resolve()


def _cycle_from_row(row: Mapping[str, Any]) -> LedgerCycle | None:
    try:
        plan = json.loads(str(row["plan"]))
        symbol = str(plan["symbol"])
        if symbol not in SUPPORTED_SYMBOLS:
            return None
        state = str(row["state"])
        # A database row is only a cached observation after process restart.
        if state == "PROTECTED":
            state = "PROTECTION_UNVERIFIED"
        return LedgerCycle(
            cycle_id=str(row["cycle_id"]),
            symbol=cast(Literal["BTCUSDT", "SOLUSDT"], symbol),
            state=state,
            stop_entry_requested=bool(row["stop_entry"]),
            updated_at_ms=int(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _read_sqlite(database_url: str) -> LedgerRead:
    path = _sqlite_path(database_url)
    if path is None or not path.is_file():
        return LedgerRead(status="missing", cycles=[])
    # mode=ro prevents a status request from creating or changing the ledger.
    connection: sqlite3.Connection | None = None
    try:
        uri_path = quote(path.as_posix(), safe="/")
        connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='testnet_cycles'"
        ).fetchone()
        if table is None:
            return LedgerRead(status="missing", cycles=[])
        rows = connection.execute(
            "SELECT cycle_id,plan,state,stop_entry,updated_at "
            "FROM testnet_cycles ORDER BY updated_at DESC,cycle_id"
        ).fetchall()
        cycles = [cycle for row in rows if (cycle := _cycle_from_row(row)) is not None]
        return LedgerRead(status="ready", cycles=cycles)
    except sqlite3.Error:
        return LedgerRead(status="unavailable", cycles=[])
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()


def read_ledger(database_url: str) -> LedgerRead:
    """Read panel rows without invoking stores, migrations, recovery, or DDL."""

    try:
        if make_url(database_url).get_backend_name() == "sqlite":
            return _read_sqlite(database_url)
        engine = create_engine(database_url)
    except Exception:
        return LedgerRead(status="unavailable", cycles=[])
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
                present = connection.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema=current_schema() AND table_name='testnet_cycles'"
                    )
                ).first()
                if present is None:
                    return LedgerRead(status="missing", cycles=[])
            rows = connection.execute(
                text(
                    "SELECT cycle_id,plan,state,stop_entry,updated_at "
                    "FROM testnet_cycles ORDER BY updated_at DESC,cycle_id"
                )
            ).mappings()
            cycles = [
                cycle
                for row in rows
                if (cycle := _cycle_from_row(cast(Mapping[str, Any], row))) is not None
            ]
            return LedgerRead(status="ready", cycles=cycles)
    except Exception:
        return LedgerRead(status="unavailable", cycles=[])
    finally:
        with contextlib.suppress(Exception):
            engine.dispose()


def load_panel_credentials(path: Path) -> TestnetCredentials:
    """Read only the server-fixed private credential file; panel requests cannot redirect it."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise ValueError("invalid credential file permissions")
        payload = json.load(stream)
    return TestnetCredentials.model_validate(payload)


def _reason(value: object) -> str | None:
    if value is None:
        return None
    candidate = str(value)
    return candidate if candidate in _SAFE_REASONS else "watcher_failed"


def _sanitize_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    result = {key: snapshot[key] for key in _SNAPSHOT_KEYS if key in snapshot}
    result["mode"] = "testnet"
    result["production_enabled"] = False
    result["reason"] = _reason(result.get("reason"))
    return result


def _failure_snapshot(cycle_id: str, reason: str, now_ms: int) -> dict[str, object]:
    return {
        "cycle_id": cycle_id,
        "mode": "testnet",
        "production_enabled": False,
        "state": "BLOCKED",
        "reason": reason,
        "stop_entry_requested": None,
        "entry_state": None,
        "protection_state": None,
        "owned_quantity": None,
        "dust_quantity": None,
        "unprotected_quantity": None,
        "coverage_observation_fresh": False,
        "observed_at_ms": None,
        "manager_running": False,
        "health": "attention",
        "checked_at_ms": now_ms,
        "last_success_at_ms": None,
        "consecutive_failures": 0,
        "retry_in_seconds": 0,
        "resume_required": True,
    }


class TestnetPanel:
    """Own one browser-independent watcher task for this server lifespan."""

    def __init__(
        self,
        database_url: str,
        credential_path: Path,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.database_url = database_url
        self.credential_path = credential_path
        self.server_instance_id = secrets.token_urlsafe(18)
        self.control_token = secrets.token_urlsafe(32)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._run_id: str | None = None
        self._cycle_id: str | None = None
        self._symbol: Literal["BTCUSDT", "SOLUSDT"] | None = None
        self._allow_mutations = False
        self._phase = "idle"
        self._health = "idle"
        self._snapshot: dict[str, object] | None = None
        self._closing = False
        self.credentials_loader: Callable[[Path], TestnetCredentials] = load_panel_credentials
        self.transport_factory: Callable[..., BinanceTestnetTransport] = BinanceTestnetTransport

    def session(self) -> dict[str, str]:
        return {
            "server_instance_id": self.server_instance_id,
            "control_token": self.control_token,
        }

    def valid_token(self, supplied: str | None) -> bool:
        if supplied is None:
            return False
        try:
            supplied_bytes = supplied.encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(supplied_bytes, self.control_token.encode("ascii"))

    def _active(self) -> bool:
        return self._task is not None and not self._task.done()

    def _project_snapshot(self, now_ms: int, active: bool) -> tuple[dict[str, object] | None, str]:
        if self._snapshot is None:
            return None, self._health
        snapshot = dict(self._snapshot)
        health = self._health
        checked_at = snapshot.get("checked_at_ms")
        checked_age = (
            now_ms - checked_at
            if isinstance(checked_at, int) and not isinstance(checked_at, bool)
            else STALE_WATCH_MS + 1
        )
        if active and health == "healthy" and not 0 <= checked_age <= STALE_WATCH_MS:
            health = "stale"
        observed_at = snapshot.get("observed_at_ms")
        observed_age = (
            now_ms - observed_at
            if isinstance(observed_at, int) and not isinstance(observed_at, bool)
            else FRESH_COVERAGE_MS + 1
        )
        coverage = bool(
            active
            and health == "healthy"
            and snapshot.get("state") == "PROTECTED"
            and snapshot.get("coverage_observation_fresh") is True
            and 0 <= observed_age <= FRESH_COVERAGE_MS
        )
        if not coverage:
            snapshot["coverage_observation_fresh"] = False
            if snapshot.get("state") == "PROTECTED":
                snapshot["state"] = "PROTECTION_UNVERIFIED"
            snapshot["unprotected_quantity"] = snapshot.get("owned_quantity")
        snapshot["manager_running"] = active
        snapshot["health"] = health
        return snapshot, health

    async def status(self) -> dict[str, object]:
        ledger = await asyncio.to_thread(read_ledger, self.database_url)
        async with self._lock:
            return self._envelope(ledger)

    def _envelope(self, ledger: LedgerRead) -> dict[str, object]:
        now_ms = self._clock_ms()
        active = self._active()
        snapshot, health = self._project_snapshot(now_ms, active)
        return {
            "mode": "testnet",
            "production_enabled": False,
            "server_instance_id": self.server_instance_id,
            "observed_at_ms": now_ms,
            "ledger_status": ledger.status,
            "cycles": [cycle.model_dump() for cycle in ledger.cycles],
            "watcher": {
                "run_id": self._run_id,
                "cycle_id": self._cycle_id,
                "symbol": self._symbol,
                "allow_mutations": self._allow_mutations,
                "phase": self._phase,
                "health": health,
                "manager_running": active,
                "coverage_observation_fresh": bool(
                    snapshot and snapshot.get("coverage_observation_fresh") is True
                ),
                "snapshot": snapshot,
            },
        }

    async def start(self, body: WatchStartInput) -> dict[str, object]:
        async with self._lock:
            if self._closing:
                raise PanelError(503, "server_shutting_down")
            if self._active():
                raise PanelError(409, "watcher_active")
            ledger = await asyncio.to_thread(read_ledger, self.database_url)
            if ledger.status == "unavailable":
                raise PanelError(503, "ledger_unavailable")
            selected = next(
                (cycle for cycle in ledger.cycles if cycle.cycle_id == body.cycle_id), None
            )
            if selected is None:
                raise PanelError(404, "cycle_not_found")
            if selected.state in CLOSED_CYCLES:
                raise PanelError(409, "cycle_closed")
            run_id = secrets.token_urlsafe(18)
            self._run_id = run_id
            self._cycle_id = selected.cycle_id
            self._symbol = selected.symbol
            self._allow_mutations = bool(body.confirm_testnet)
            self._phase = "starting"
            self._health = "starting"
            self._snapshot = None
            self._task = asyncio.create_task(
                self._run(run_id, selected.cycle_id, selected.symbol, bool(body.confirm_testnet))
            )
            return self._envelope(ledger)

    def _emit(self, run_id: str, update: dict[str, object]) -> None:
        if self._run_id != run_id:
            return
        snapshot = _sanitize_snapshot(update)
        self._snapshot = snapshot
        emitted_health = str(snapshot.get("health") or "attention")
        if emitted_health not in {
            "healthy",
            "retrying",
            "stopped",
            "attention",
            "closed",
        }:
            emitted_health = "attention"
        self._health = emitted_health
        self._phase = (
            "running" if emitted_health in {"healthy", "retrying"} else emitted_health
        )

    async def _run(
        self,
        run_id: str,
        cycle_id: str,
        symbol: Literal["BTCUSDT", "SOLUSDT"],
        allow_mutations: bool,
    ) -> None:
        cycle: TestnetCycle | None = None
        try:
            try:
                credentials = self.credentials_loader(self.credential_path)
            except Exception:
                self._snapshot = _failure_snapshot(
                    cycle_id, "credentials_unavailable", self._clock_ms()
                )
                self._phase = self._health = "attention"
                return
            async with self.transport_factory(symbol, credentials=credentials) as transport:
                cycle = TestnetCycle(self.database_url, transport)
                await run_watch(
                    cycle,
                    cycle_id,
                    WatchOptions(allow_mutations=allow_mutations),
                    lambda update: self._emit(run_id, update),
                )
        except asyncio.CancelledError:
            if self._run_id == run_id:
                if self._snapshot is None:
                    self._snapshot = _failure_snapshot(
                        cycle_id, "watch_cancelled", self._clock_ms()
                    )
                self._snapshot = _sanitize_snapshot(self._snapshot)
                self._snapshot["coverage_observation_fresh"] = False
                if self._snapshot.get("state") == "PROTECTED":
                    self._snapshot["state"] = "PROTECTION_UNVERIFIED"
                self._snapshot["unprotected_quantity"] = self._snapshot.get("owned_quantity")
                self._phase = self._health = "stopped"
            raise
        except (ManagerBusy, ManagerLockError) as error:
            reason = "manager_busy" if isinstance(error, ManagerBusy) else "manager_lock_lost"
            self._snapshot = _failure_snapshot(cycle_id, reason, self._clock_ms())
            self._phase = self._health = "attention"
        except AccountBindingError as error:
            reason = error.reason_code if error.reason_code in _SAFE_REASONS else "watcher_failed"
            self._snapshot = _failure_snapshot(cycle_id, reason, self._clock_ms())
            self._phase = self._health = "attention"
        except Exception:
            self._snapshot = _failure_snapshot(cycle_id, "watcher_failed", self._clock_ms())
            self._phase = self._health = "attention"
        finally:
            if cycle is not None:
                cycle.store.db.dispose()
            async with self._lock:
                if self._run_id == run_id and self._phase in {"starting", "running", "stopping"}:
                    self._phase = "stopped"
                    self._health = "stopped"

    async def stop(self, run_id: str) -> dict[str, object]:
        async with self._lock:
            if run_id != self._run_id:
                raise PanelError(409, "stale_run_id")
            task = self._task
            if task is None or task.done():
                return await self._status_while_locked()
            self._phase = "stopping"
            self._health = "stopping"
            if not task.cancelling():
                task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
                raise
        async with self._lock:
            if self._run_id == run_id and self._phase == "stopping":
                # A task cancelled before its first bytecode never enters _run's finally block.
                self._phase = self._health = "stopped"
        return await self.status()

    async def _status_while_locked(self) -> dict[str, object]:
        # Used only for an idempotent stop while the controller lock is held.
        ledger = await asyncio.to_thread(read_ledger, self.database_url)
        return self._envelope(ledger)

    async def shutdown(self) -> None:
        async with self._lock:
            self._closing = True
            task = self._task
            if task is not None and not task.done():
                self._phase = "stopping"
                self._health = "stopping"
                if not task.cancelling():
                    task.cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        async with self._lock:
            if task is self._task and self._phase == "stopping":
                self._phase = self._health = "stopped"
