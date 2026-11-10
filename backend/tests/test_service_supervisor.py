from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def supervisor() -> ModuleType:
    path = REPO_ROOT / "scripts" / "service_supervisor.py"
    spec = importlib.util.spec_from_file_location("service_supervisor_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def health(instance: str, **worker_changes: object) -> dict[str, object]:
    worker: dict[str, object] = {
        "state": "running",
        "cycle_in_progress": False,
        "last_progress_age_seconds": 1.0,
        "cycle_age_seconds": None,
        "data_degraded": False,
    }
    worker.update(worker_changes)
    return {
        "schema_version": 1,
        "instance_id": instance,
        "backend_pid": 123,
        "uptime_seconds": 3.5,
        "worker": worker,
    }


def local_listener() -> socket.socket:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
    except PermissionError:
        listener.close()
        pytest.skip("loopback listeners are unavailable in this test sandbox")
    listener.listen()
    return listener


def test_health_schema_accepts_advisory_worker_states(supervisor: ModuleType) -> None:
    instance = "a" * 32
    for state in ("disabled", "starting", "running", "stalled", "stopped"):
        assert supervisor.validate_health(health(instance, state=state), instance)["worker"][
            "state"
        ] == state


@pytest.mark.parametrize(
    "change",
    [
        {"instance_id": "b" * 32},
        {"schema_version": True},
        {"worker": {"state": []}},
        {"uptime_seconds": float("nan")},
    ],
)
def test_health_schema_rejects_wrong_identity_and_malformed_values(
    supervisor: ModuleType, change: dict[str, object]
) -> None:
    value = health("a" * 32)
    value.update(change)
    with pytest.raises(supervisor.SupervisorError):
        supervisor.validate_health(value, "a" * 32)


def test_fixed_binary_log_cap_handles_newline_free_flood_and_permissions(
    supervisor: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "stdout.log"
    log = supervisor.RotatingBinaryLog(path, limit=101, archives=3)
    log.open()
    log.write(b"x" * 1000)
    log.close()

    files = [candidate for candidate in tmp_path.iterdir() if candidate.name.startswith("stdout")]
    assert sum(candidate.stat().st_size for candidate in files) <= 404
    assert all(candidate.stat().st_size <= 101 for candidate in files)
    assert all(candidate.stat().st_mode & 0o777 == 0o600 for candidate in files)


def test_logs_reject_symlinks_and_hardlinks_without_touching_target(
    supervisor: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "sentinel"
    target.write_bytes(b"keep me")
    symlink = tmp_path / "stdout.log"
    symlink.symlink_to(target)
    with pytest.raises(supervisor.SupervisorError):
        supervisor.RotatingBinaryLog(symlink, limit=2).open()
    symlink.unlink()
    os.link(target, symlink)
    with pytest.raises(supervisor.SupervisorError):
        supervisor.RotatingBinaryLog(symlink, limit=2).open()
    assert target.read_bytes() == b"keep me"


def test_probe_has_absolute_deadline_and_no_redirect_following(
    supervisor: ModuleType,
) -> None:
    listener = local_listener()
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
            for _ in range(20):
                try:
                    connection.sendall(b"x")
                except OSError:
                    break
                time.sleep(0.05)
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    began = time.monotonic()
    with pytest.raises(supervisor.SupervisorError, match="deadline"):
        supervisor.probe_health(port, "a" * 32, timeout=0.2)
    assert time.monotonic() - began < 0.5


def test_probe_rejects_redirect_and_oversized_body(supervisor: ModuleType) -> None:
    for response in (
        b"HTTP/1.1 302 Found\r\nLocation: http://example.com/\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n",
    ):
        listener = local_listener()
        port = listener.getsockname()[1]

        def serve(server: socket.socket = listener, wire: bytes = response) -> None:
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(wire)
            server.close()

        threading.Thread(target=serve, daemon=True).start()
        with pytest.raises(supervisor.SupervisorError):
            supervisor.probe_health(port, "a" * 32)


def test_legacy_oversized_logs_keep_only_bounded_tail(
    supervisor: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "stdout.log"
    for suffix in ("", ".1", ".2", ".3"):
        path.with_name(path.name + suffix).write_bytes(b"old" * 200 + b"recent" * 10)
    log = supervisor.RotatingBinaryLog(path, limit=60, archives=3)
    log.open()
    log.close()
    files = list(tmp_path.iterdir())
    assert sum(file.stat().st_size for file in files) <= 240
    assert all(file.read_bytes() in (b"", b"recent" * 10) for file in files)


def test_huge_numbers_and_invalid_worker_are_health_errors(supervisor: ModuleType) -> None:
    for change in ({"uptime_seconds": 10**1000}, {"schema_version": True}):
        value = health("a" * 32)
        value.update(change)
        with pytest.raises(supervisor.SupervisorError):
            supervisor.validate_health(value, "a" * 32)
    value = health("a" * 32, state=[])
    with pytest.raises(supervisor.SupervisorError):
        supervisor.validate_health(value, "a" * 32)
    with pytest.raises(supervisor.SupervisorError):
        supervisor._http_body(b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n")


def test_drainer_keeps_consuming_after_disk_failure(supervisor: ModuleType) -> None:
    import io

    class BrokenLog:
        def write(self, chunk: bytes) -> None:
            raise OSError("synthetic disk failure")

    class Source(io.BytesIO):
        consumed = 0

        def read(self, size: int = -1) -> bytes:
            result = super().read(size)
            self.consumed += len(result)
            return result

    source = Source(b"x" * (supervisor.READ_SIZE * 3))
    failed = threading.Event()
    supervisor._drain(source, BrokenLog(), failed)
    assert failed.is_set()
    assert source.consumed == supervisor.READ_SIZE * 3
    assert source.closed


def test_local_notifications_are_opt_in_and_use_only_static_osascript(
    supervisor: ModuleType,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    assert supervisor.send_local_notification(run) == "osascript_exit_0"
    assert len(calls) == 1
    calls.clear()

    sends: list[None] = []

    def send() -> str:
        sends.append(None)
        return "osascript_exit_0"

    disabled = supervisor.LocalNotifier(False, sender=send)
    disabled.transition("worker_attention")
    disabled.close()
    assert sends == []

    notifier = supervisor.LocalNotifier(True, sender=send)
    notifier.transition("worker_attention")
    deadline = time.monotonic() + 1.0
    while notifier.status()["last_result"] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    notifier.close()

    assert len(sends) == 1
    assert supervisor.send_local_notification(run) == "osascript_exit_0"
    command, kwargs = calls[0]
    assert command == list(supervisor.NOTIFICATION_COMMAND)
    assert command[0] == "/usr/bin/osascript"
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 2.0
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    notification = notifier.status()
    assert notification["last_result"] == "osascript_exit_0"
    assert "delivered" not in notification["last_result"]


def test_notification_timeout_consumes_shared_cooldown_and_burst_is_nonblocking(
    supervisor: ModuleType,
) -> None:
    calls: list[list[str]] = []
    clock_value = [10.0]
    first_call = threading.Event()

    def clock() -> float:
        return clock_value[0]

    def send() -> str:
        calls.append(list(supervisor.NOTIFICATION_COMMAND))
        first_call.set()
        raise subprocess.TimeoutExpired(supervisor.NOTIFICATION_COMMAND, 2.0)

    notifier = supervisor.LocalNotifier(
        True, monotonic=clock, sender=send
    )
    began = time.monotonic()
    for warning in (
        "http_unresponsive",
        "worker_attention",
        "logging_failed",
    ):
        notifier.transition(warning)
    assert time.monotonic() - began < 0.1
    assert first_call.wait(1.0)
    deadline = time.monotonic() + 1.0
    while notifier.status()["last_result"] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    notification = notifier.status()
    assert notification["last_result"] == "timeout"

    notifier.transition("worker_attention")
    time.sleep(0.05)
    assert len(calls) == 1
    notifier.transition(None)
    clock_value[0] += supervisor.NOTIFICATION_COOLDOWN
    # Clearing a warning cancels a coalesced stale alert and emits no recovery.
    notifier.transition(None)
    time.sleep(0.05)
    assert len(calls) == 1
    # A recurrence after the shared cooldown is eligible again.
    notifier.transition("logging_failed")
    deadline = time.monotonic() + 1.0
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    notifier.close()
    assert len(calls) == 2


def test_notification_failures_and_close_never_escape(supervisor: ModuleType) -> None:
    def broken_sender() -> str:
        raise OSError("osascript unavailable")

    notifier = supervisor.LocalNotifier(True, sender=broken_sender)
    notifier.transition("http_unresponsive")
    time.sleep(0.05)
    notifier.close()
    notifier.transition("logging_failed")
    assert notifier.worker is not None
    assert not notifier.worker.is_alive()


def test_notification_worker_start_failure_is_advisory(
    supervisor: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread capacity exhausted")

    monkeypatch.setattr(supervisor.threading.Thread, "start", fail_start)
    notifier = supervisor.LocalNotifier(True, sender=lambda: "should_not_run")
    notifier.transition("worker_attention")
    notifier.close()

    assert notifier.worker is None
    notification = notifier.status()
    assert notification == {
        "enabled": True,
        "last_attempt": None,
        "last_result": "worker_start_error",
    }


def test_slow_notifier_never_writes_snapshots_or_delays_health(
    supervisor: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    sending = threading.Event()
    release = threading.Event()
    stop = threading.Event()
    writes: list[dict[str, object]] = []

    def sender() -> str:
        sending.set()
        release.wait(2)
        return 'osascript_exit_0'

    def forbidden_write(*args: object) -> None:
        pytest.fail('notification worker must not write files')

    monkeypatch.setattr(supervisor, '_atomic_json', forbidden_write)
    notifier = supervisor.LocalNotifier(True, sender=sender)
    try:
        notifier.transition('http_unresponsive')
        assert sending.wait(1)
        snapshot = SimpleNamespace(value={}, update=lambda **changes: writes.append(changes))

        def probe(port: int, instance: str) -> dict[str, object]:
            stop.set()
            return health(instance, state='stalled')

        monitor = supervisor.HealthMonitor('a' * 32, snapshot, stop, probe=probe, notifier=notifier)
        # Let exactly one health cycle run; the probe ends further scheduling.
        stop.clear()
        began = time.monotonic()
        monitor.run()
        assert time.monotonic() - began < 0.5
        assert len(writes) == 1
        assert writes[0]['current_warning'] == 'worker_attention'
        assert writes[0]['notifications']['last_result'] is None
    finally:
        release.set()
        notifier.close()
    assert notifier.status()['last_result'] == 'osascript_exit_0'
