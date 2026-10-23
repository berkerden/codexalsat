"""Foreground supervision for one already-persisted Testnet cycle."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from spotlab.manager_lock import ManagerLockError
from spotlab.orders import AccountBindingError, RetryableTransportError
from spotlab.testnet_cycle import CLOSED_CYCLES, TestnetCycle

Emit = Callable[[dict[str, object]], None]
Sleep = Callable[[float], Awaitable[None]]

_RETRYABLE_BLOCKED_REASONS = frozenset({"RetryableTransportError", "TimeoutError"})


@dataclass(frozen=True)
class WatchOptions:
    """Bounded retry and cadence settings for an existing-cycle watcher."""

    allow_mutations: bool = False
    poll_seconds: float = 2.0
    run_seconds: float = 0.0
    max_consecutive_failures: int = 5

    def validate(self) -> None:
        if not isinstance(self.allow_mutations, bool):
            raise ValueError("allow mutations must be a boolean")
        if (
            isinstance(self.poll_seconds, bool)
            or not isinstance(self.poll_seconds, (int, float))
            or not math.isfinite(self.poll_seconds)
            or not 1 <= self.poll_seconds <= 5
        ):
            raise ValueError("poll seconds must be between 1 and 5")
        if (
            isinstance(self.run_seconds, bool)
            or not isinstance(self.run_seconds, (int, float))
            or not math.isfinite(self.run_seconds)
            or self.run_seconds < 0
        ):
            raise ValueError("run seconds must be zero or positive")
        if (
            isinstance(self.max_consecutive_failures, bool)
            or not isinstance(self.max_consecutive_failures, int)
            or not 1 <= self.max_consecutive_failures <= 100
        ):
            raise ValueError("max consecutive failures must be between 1 and 100")


def _fallback(cycle_id: str, checked_at_ms: int, reason: str) -> dict[str, object]:
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
    }


def _decorate(
    status: Mapping[str, object],
    *,
    health: str,
    checked_at_ms: int,
    last_success_at_ms: int | None,
    consecutive_failures: int,
    retry_in_seconds: float,
    manager_running: bool,
) -> dict[str, object]:
    result = dict(status)
    result.update(
        {
            "manager_running": manager_running,
            "health": health,
            "checked_at_ms": checked_at_ms,
            "last_success_at_ms": last_success_at_ms,
            "consecutive_failures": consecutive_failures,
            "retry_in_seconds": retry_in_seconds,
            "resume_required": status.get("state") not in CLOSED_CYCLES,
        }
    )
    return result


def _conservative_failure(status: Mapping[str, object], reason: str) -> dict[str, object]:
    result = dict(status)
    if result.get("state") == "PROTECTED":
        result["state"] = "PROTECTION_UNVERIFIED"
    result["reason"] = reason
    result["coverage_observation_fresh"] = False
    result["unprotected_quantity"] = result.get("owned_quantity")
    return result


def _retryable_exception(error: Exception) -> bool:
    return isinstance(error, (RetryableTransportError, TimeoutError)) or (
        isinstance(error, AccountBindingError)
        and error.reason_code == "account_identity_unavailable"
    )


def _safe_reason(error: Exception) -> str:
    if isinstance(error, AccountBindingError):
        return error.reason_code
    if isinstance(error, ManagerLockError):
        if error.args and isinstance(error.args[0], str):
            return error.args[0]
        return "ManagerLockError"
    return type(error).__name__


def _failure_snapshot(
    cycle: TestnetCycle,
    cycle_id: str,
    checked_at_ms: int,
    reason: str,
) -> dict[str, object]:
    try:
        status = cycle.status(cycle_id)
    except Exception:
        status = _fallback(cycle_id, checked_at_ms, reason)
    return _conservative_failure(status, reason)


async def run_watch(
    cycle: TestnetCycle,
    cycle_id: str,
    options: WatchOptions,
    emit: Emit,
    *,
    sleep: Sleep = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Supervise an existing cycle without ever creating or resending its BUY."""
    options.validate()
    started_at = monotonic()
    deadline = None if options.run_seconds == 0 else started_at + options.run_seconds
    failures = 0
    last_success_at_ms: int | None = None
    last_status: dict[str, object] | None = None

    with cycle.exclusive_management() as manager_lock:
        try:
            while True:
                if (
                    deadline is not None
                    and last_status is not None
                    and monotonic() >= deadline
                ):
                    checked_at_ms = cycle.clock_ms()
                    try:
                        stopped_status = cycle.status(cycle_id)
                    except Exception:
                        stopped_status = _conservative_failure(last_status, "watch_stopped")
                    if failures:
                        stopped_status = _conservative_failure(
                            stopped_status, str(last_status.get("reason") or "watch_retry_pending")
                        )
                    final = _decorate(
                        stopped_status,
                        health="attention" if failures else "stopped",
                        checked_at_ms=checked_at_ms,
                        last_success_at_ms=last_success_at_ms,
                        consecutive_failures=failures,
                        retry_in_seconds=0,
                        manager_running=False,
                    )
                    emit(final)
                    return final
                try:
                    manager_lock.check_alive()
                except ManagerLockError as error:
                    checked_at_ms = cycle.clock_ms()
                    reason = _safe_reason(error)
                    status = last_status or _fallback(cycle_id, checked_at_ms, reason)
                    final = _decorate(
                        _conservative_failure(status, reason),
                        health="attention",
                        checked_at_ms=checked_at_ms,
                        last_success_at_ms=last_success_at_ms,
                        consecutive_failures=failures,
                        retry_in_seconds=0,
                        manager_running=False,
                    )
                    emit(final)
                    return final

                try:
                    status = await cycle.advance(
                        cycle_id,
                        allow_mutations=options.allow_mutations,
                        stop_entry=options.allow_mutations,
                    )
                except Exception as error:
                    checked_at_ms = cycle.clock_ms()
                    reason = _safe_reason(error)
                    retryable = _retryable_exception(error)
                    status = _failure_snapshot(cycle, cycle_id, checked_at_ms, reason)
                    thrown = True
                else:
                    checked_at_ms = cycle.clock_ms()
                    reason = str(status.get("reason") or "")
                    retryable = (
                        status.get("state") == "BLOCKED"
                        and reason in _RETRYABLE_BLOCKED_REASONS
                    )
                    thrown = False

                last_status = (
                    _conservative_failure(status, reason) if retryable else dict(status)
                )
                if retryable:
                    failures += 1
                    if failures >= options.max_consecutive_failures:
                        final = _decorate(
                            _conservative_failure(status, reason),
                            health="attention",
                            checked_at_ms=checked_at_ms,
                            last_success_at_ms=last_success_at_ms,
                            consecutive_failures=failures,
                            retry_in_seconds=0,
                            manager_running=False,
                        )
                        emit(final)
                        return final
                    retry_seconds = min(30.0, options.poll_seconds * (2 ** (failures - 1)))
                    update = _decorate(
                        _conservative_failure(status, reason),
                        health="retrying",
                        checked_at_ms=checked_at_ms,
                        last_success_at_ms=last_success_at_ms,
                        consecutive_failures=failures,
                        retry_in_seconds=retry_seconds,
                        manager_running=True,
                    )
                    emit(update)
                    delay = retry_seconds
                elif thrown:
                    final = _decorate(
                        status,
                        health="attention",
                        checked_at_ms=checked_at_ms,
                        last_success_at_ms=last_success_at_ms,
                        consecutive_failures=failures,
                        retry_in_seconds=0,
                        manager_running=False,
                    )
                    emit(final)
                    return final
                else:
                    failures = 0
                    if status.get("state") == "BLOCKED" or status.get("state") == "UNPROTECTED":
                        final = _decorate(
                            status,
                            health="attention",
                            checked_at_ms=checked_at_ms,
                            last_success_at_ms=last_success_at_ms,
                            consecutive_failures=0,
                            retry_in_seconds=0,
                            manager_running=False,
                        )
                        emit(final)
                        return final
                    last_success_at_ms = checked_at_ms
                    if status.get("state") in CLOSED_CYCLES:
                        final = _decorate(
                            status,
                            health="closed",
                            checked_at_ms=checked_at_ms,
                            last_success_at_ms=last_success_at_ms,
                            consecutive_failures=0,
                            retry_in_seconds=0,
                            manager_running=False,
                        )
                        emit(final)
                        return final
                    update = _decorate(
                        status,
                        health="healthy",
                        checked_at_ms=checked_at_ms,
                        last_success_at_ms=last_success_at_ms,
                        consecutive_failures=0,
                        retry_in_seconds=options.poll_seconds,
                        manager_running=True,
                    )
                    emit(update)
                    delay = options.poll_seconds

                if deadline is not None:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        try:
                            stopped_status = cycle.status(cycle_id)
                        except Exception:
                            stopped_status = _conservative_failure(last_status, "watch_stopped")
                        if failures:
                            stopped_status = _conservative_failure(
                                stopped_status,
                                str(last_status.get("reason") or "watch_retry_pending"),
                            )
                        final = _decorate(
                            stopped_status,
                            health="attention" if failures else "stopped",
                            checked_at_ms=cycle.clock_ms(),
                            last_success_at_ms=last_success_at_ms,
                            consecutive_failures=failures,
                            retry_in_seconds=0,
                            manager_running=False,
                        )
                        emit(final)
                        return final
                    delay = min(delay, remaining)
                await sleep(delay)
        except (asyncio.CancelledError, KeyboardInterrupt):
            checked_at_ms = cycle.clock_ms()
            interrupted_status = _conservative_failure(
                last_status or _fallback(cycle_id, checked_at_ms, "interrupted"),
                "interrupted",
            )
            final = _decorate(
                interrupted_status,
                health="stopped",
                checked_at_ms=checked_at_ms,
                last_success_at_ms=last_success_at_ms,
                consecutive_failures=failures,
                retry_in_seconds=0,
                manager_running=False,
            )
            emit(final)
            raise
