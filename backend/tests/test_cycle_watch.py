from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine

from spotlab.cycle_watch import WatchOptions, run_watch
from spotlab.manager_lock import ManagerBusy, ManagerLock, ManagerLockError
from spotlab.orders import AccountBindingError, RetryableTransportError
from spotlab.testnet_cycle import TestnetCycle as Cycle


def cycle_status(state: str = "ENTRY_OPEN", **changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "cycle_id": "one",
        "mode": "testnet",
        "production_enabled": False,
        "state": state,
        "reason": None,
        "stop_entry_requested": False,
        "entry_state": "NEW",
        "protection_state": None,
        "owned_quantity": "0.5",
        "dust_quantity": "0",
        "unprotected_quantity": "0.5",
        "coverage_observation_fresh": False,
        "observed_at_ms": 1000,
    }
    result.update(changes)
    return result


class FakeLock:
    def __init__(self) -> None:
        self.checks = 0

    def check_alive(self) -> None:
        self.checks += 1


class FakeCycle:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[bool, bool]] = []
        self.now = 1000
        self.lock = FakeLock()
        self.latest = cycle_status()

    @contextmanager
    def exclusive_management(self) -> Iterator[FakeLock]:
        yield self.lock

    def clock_ms(self) -> int:
        return self.now

    async def advance(
        self, _cycle_id: str, *, allow_mutations: bool, stop_entry: bool
    ) -> dict[str, object]:
        self.calls.append((allow_mutations, stop_entry))
        outcome = self.outcomes.pop(0)
        self.now += 1
        if isinstance(outcome, BaseException):
            raise outcome
        self.latest = cast(dict[str, object], outcome)
        return self.latest

    def status(self, _cycle_id: str) -> dict[str, object]:
        return dict(self.latest)


class UnreadableCycle(FakeCycle):
    def status(self, _cycle_id: str) -> dict[str, object]:
        raise LookupError("secret database details")


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


@pytest.mark.asyncio
async def test_read_only_watch_never_requests_mutation_and_stops_at_deadline() -> None:
    cycle = FakeCycle([cycle_status()])
    timer = FakeTime()
    emitted: list[dict[str, object]] = []

    result = await run_watch(
        cast(Cycle, cycle),
        "one",
        WatchOptions(run_seconds=1, poll_seconds=1),
        emitted.append,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
    )

    assert cycle.calls == [(False, False)]
    assert cycle.lock.checks == 1
    assert [item["health"] for item in emitted] == ["healthy", "stopped"]
    assert result["manager_running"] is False
    assert result["resume_required"] is True


@pytest.mark.asyncio
async def test_confirmed_watch_stops_entry_on_first_verified_advance() -> None:
    cycle = FakeCycle(
        [
            cycle_status(stop_entry_requested=True),
            cycle_status("NO_FILL", stop_entry_requested=True, owned_quantity="0"),
        ]
    )
    timer = FakeTime()

    result = await run_watch(
        cast(Cycle, cycle),
        "one",
        WatchOptions(allow_mutations=True, poll_seconds=1),
        lambda _: None,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
    )

    assert cycle.calls == [(True, True), (True, True)]
    assert result["health"] == "closed"
    assert result["resume_required"] is False


@pytest.mark.asyncio
async def test_retryable_failures_back_off_and_clear_after_success() -> None:
    cycle = FakeCycle(
        [
            TimeoutError(),
            RetryableTransportError(),
            cycle_status("NO_FILL", owned_quantity="0"),
        ]
    )
    timer = FakeTime()
    emitted: list[dict[str, object]] = []

    result = await run_watch(
        cast(Cycle, cycle),
        "one",
        WatchOptions(poll_seconds=2),
        emitted.append,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
    )

    assert timer.sleeps == [2, 4]
    assert [item["consecutive_failures"] for item in emitted] == [1, 2, 0]
    assert emitted[0]["coverage_observation_fresh"] is False
    assert emitted[0]["unprotected_quantity"] == "0.5"
    assert result["health"] == "closed"


@pytest.mark.asyncio
async def test_exact_identity_unavailable_retries_but_mismatch_stops() -> None:
    unavailable = FakeCycle(
        [
            AccountBindingError("account_identity_unavailable"),
            cycle_status("NO_FILL", owned_quantity="0"),
        ]
    )
    timer = FakeTime()
    assert (
        await run_watch(
            cast(Cycle, unavailable),
            "one",
            WatchOptions(poll_seconds=1),
            lambda _: None,
            sleep=timer.sleep,
            monotonic=timer.monotonic,
        )
    )["health"] == "closed"

    mismatch = FakeCycle([AccountBindingError("account_identity_mismatch")])
    emitted: list[dict[str, object]] = []
    result = await run_watch(
        cast(Cycle, mismatch),
        "one",
        WatchOptions(poll_seconds=1),
        emitted.append,
        sleep=FakeTime().sleep,
        monotonic=FakeTime().monotonic,
    )
    assert mismatch.calls == [(False, False)]
    assert result["health"] == "attention"
    assert result["reason"] == "account_identity_mismatch"
    assert result["last_success_at_ms"] is None


@pytest.mark.asyncio
async def test_hard_exception_after_protected_observation_never_reports_healthy() -> None:
    protected = cycle_status(
        "PROTECTED",
        protection_state="NEW",
        coverage_observation_fresh=True,
        unprotected_quantity="0",
    )
    cycle = FakeCycle([protected, AccountBindingError("account_binding_mismatch")])
    timer = FakeTime()
    emitted: list[dict[str, object]] = []

    result = await run_watch(
        cast(Cycle, cycle),
        "one",
        WatchOptions(poll_seconds=1),
        emitted.append,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
    )

    assert [item["health"] for item in emitted] == ["healthy", "attention"]
    assert result["state"] == "PROTECTION_UNVERIFIED"
    assert result["coverage_observation_fresh"] is False
    assert result["unprotected_quantity"] == "0.5"
    assert result["last_success_at_ms"] == 1001


@pytest.mark.asyncio
async def test_unreadable_status_reports_unknown_evidence_without_exception_text() -> None:
    cycle = UnreadableCycle([ValueError("secret signed URL")])
    result = await run_watch(
        cast(Cycle, cycle),
        "one",
        WatchOptions(),
        lambda _: None,
        sleep=FakeTime().sleep,
        monotonic=FakeTime().monotonic,
    )

    assert result["health"] == "attention"
    assert result["reason"] == "ValueError"
    assert result["stop_entry_requested"] is None
    assert result["owned_quantity"] is None
    assert result["dust_quantity"] is None
    assert result["unprotected_quantity"] is None
    assert result["observed_at_ms"] is None
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_retry_exhaustion_is_attention() -> None:
    cycle = FakeCycle([TimeoutError(), TimeoutError()])
    timer = FakeTime()
    result = await run_watch(
        cast(Cycle, cycle),
        "one",
        WatchOptions(poll_seconds=1, max_consecutive_failures=2),
        lambda _: None,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
    )
    assert result["health"] == "attention"
    assert result["consecutive_failures"] == 2
    assert timer.sleeps == [1]


@pytest.mark.asyncio
async def test_deadline_does_not_restore_fresh_coverage_after_retry_failure() -> None:
    protected = cycle_status(
        "PROTECTED",
        protection_state="NEW",
        coverage_observation_fresh=True,
        unprotected_quantity="0",
    )
    cycle = FakeCycle(
        [protected, AccountBindingError("account_identity_unavailable")]
    )
    timer = FakeTime()

    result = await run_watch(
        cast(Cycle, cycle),
        "one",
        WatchOptions(poll_seconds=1, run_seconds=2),
        lambda _: None,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
    )

    assert result["health"] == "attention"
    assert result["state"] == "PROTECTION_UNVERIFIED"
    assert result["coverage_observation_fresh"] is False
    assert result["unprotected_quantity"] == "0.5"


def test_sqlite_manager_lock_is_exclusive_and_private(tmp_path: Path) -> None:
    database = tmp_path / "orders.sqlite"
    engine = create_engine("sqlite:///" + str(database))
    first = ManagerLock(engine)
    second = ManagerLock(engine)

    with first:
        first.check_alive()
        with pytest.raises(ManagerBusy):
            with second:
                pass
        lock_path = Path(str(database.resolve()) + ".manager.lock")
        assert lock_path.stat().st_mode & 0o777 == 0o600
    with second:
        second.check_alive()
    engine.dispose()


def test_sqlite_manager_lock_detects_replaced_lock_file(tmp_path: Path) -> None:
    database = tmp_path / "orders.sqlite"
    engine = create_engine("sqlite:///" + str(database))
    lock = ManagerLock(engine)
    with lock:
        lock_path = Path(str(database.resolve()) + ".manager.lock")
        lock_path.unlink()
        lock_path.touch(mode=0o600)
        with pytest.raises(ManagerLockError, match="manager_lock_lost"):
            lock.check_alive()
    engine.dispose()


def test_in_memory_manager_lock_fails_closed() -> None:
    engine = create_engine("sqlite:///:memory:")
    with pytest.raises(ManagerLockError, match="sqlite_manager_lock_requires_file"):
        with ManagerLock(engine):
            pass
    engine.dispose()


@pytest.mark.asyncio
async def test_cycle_management_lock_is_reentrant_only_for_owner_task(tmp_path: Path) -> None:
    database = "sqlite:///" + str(tmp_path / "cycle.sqlite")
    cycle = Cycle(database, cast(Any, SimpleNamespace(symbol="BTCUSDT")))
    competitor = Cycle(database, cast(Any, SimpleNamespace(symbol="BTCUSDT")))
    with cycle.exclusive_management() as first:
        with cycle.exclusive_management() as nested:
            assert nested is first

        async def contend_same_object() -> None:
            with cycle.exclusive_management():
                pass

        with pytest.raises(ManagerBusy):
            await asyncio.create_task(contend_same_object())
        with pytest.raises(ManagerBusy):
            with competitor.exclusive_management():
                pass
    cycle.store.db.dispose()
    competitor.store.db.dispose()


@pytest.mark.asyncio
async def test_retry_cap_and_returned_blocked_reason() -> None:
    cycle = FakeCycle([cycle_status("BLOCKED", reason="TimeoutError")] * 7)
    timer = FakeTime()
    result = await run_watch(
        cast(Cycle, cycle), "one", WatchOptions(poll_seconds=5, max_consecutive_failures=7),
        lambda _: None, sleep=timer.sleep, monotonic=timer.monotonic,
    )
    assert timer.sleeps == [5, 10, 20, 30, 30, 30]
    assert result["health"] == "attention"


@pytest.mark.asyncio
async def test_lock_loss_stops_before_exchange_access(monkeypatch: pytest.MonkeyPatch) -> None:
    cycle = FakeCycle([])

    def lost() -> None:
        raise ManagerLockError("manager_lock_lost")

    monkeypatch.setattr(cycle.lock, "check_alive", lost)
    result = await run_watch(cast(Cycle, cycle), "one", WatchOptions(), lambda _: None)
    assert not cycle.calls
    assert result["health"] == "attention"
    assert result["coverage_observation_fresh"] is False


@pytest.mark.asyncio
async def test_interrupt_emits_conservative_status_without_extra_advance() -> None:
    cycle = FakeCycle([cycle_status("PROTECTED", coverage_observation_fresh=True)])
    emitted: list[dict[str, object]] = []

    async def interrupt(_seconds: float) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_watch(cast(Cycle, cycle), "one", WatchOptions(), emitted.append, sleep=interrupt)
    assert len(cycle.calls) == 1
    assert emitted[-1]["manager_running"] is False
    assert emitted[-1]["coverage_observation_fresh"] is False
