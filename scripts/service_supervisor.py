#!/usr/bin/env python3
"""Small, fail-closed supervisor for the native SpotLab launchd job.

This module deliberately does not decide whether trading is safe.  It only keeps
bounded logs and records whether the two local HTTP surfaces are responsive.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import math
import os
import select
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO

LOG_LIMIT = 5 * 1024 * 1024
LOG_ARCHIVES = 3
READ_SIZE = 64 * 1024
HEALTH_LIMIT = 16 * 1024
HEALTH_TIMEOUT = 2.0
POLL_INTERVAL = 5.0
STARTUP_GRACE = 15.0
STOP_WAIT = 10.0
HEALTH_PORTS = (8000, 5173)
HEALTH_PATH = "/api/health"
INSTANCE_ENV = "SPOTLAB_SERVICE_INSTANCE_ID"
NOTIFICATION_TIMEOUT = 2.0
NOTIFICATION_COOLDOWN = 300.0
NOTIFICATION_WARNINGS = frozenset(
    {"http_unresponsive", "worker_attention", "logging_failed"}
)
NOTIFICATION_COMMAND = (
    "/usr/bin/osascript",
    "-e",
    'display notification "SpotLab dikkat gerektiriyor. Yerel servis durumunu kontrol edin." '
    'with title "SpotLab"',
)


class SupervisorError(RuntimeError):
    pass


def _safe_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise SupervisorError("unsafe service file")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > HEALTH_LIMIT:
        raise SupervisorError("runtime snapshot too large")
    _safe_file(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class RuntimeSnapshot:
    def __init__(self, path: Path, base: dict[str, Any], clock: Callable[[], float] = time.time):
        self.path = path
        self.value = base
        self.clock = clock
        self.lock = threading.Lock()

    def update(self, **changes: Any) -> None:
        with self.lock:
            self.value.update(changes)
            self.value["checked_at"] = self.clock()
            _atomic_json(self.path, self.value)


def send_local_notification(
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """Attempt the one static notification and report process outcome, not delivery."""
    try:
        completed = runner(
            list(NOTIFICATION_COMMAND),
            shell=False,
            timeout=NOTIFICATION_TIMEOUT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return "osascript_exit_0" if completed.returncode == 0 else "osascript_exit_nonzero"
    except subprocess.TimeoutExpired:
        return "timeout"
    except BaseException:
        return "error"


class LocalNotifier:
    """Best-effort local alerts with one worker and one coalescing slot."""

    def __init__(
        self,
        enabled: bool,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        cooldown: float = NOTIFICATION_COOLDOWN,
        sender: Callable[[], str] | None = None,
    ):
        self.enabled = enabled
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.cooldown = max(NOTIFICATION_COOLDOWN, cooldown)
        self.sender = sender if sender is not None else send_local_notification
        self.condition = threading.Condition()
        self.pending: str | None = None
        self.pending_set = False
        self.closed = False
        self.last_attempt_monotonic: float | None = None
        self.last_attempt: float | None = None
        self.last_result: str | None = None
        self.worker: threading.Thread | None = None
        if enabled:
            try:
                worker = threading.Thread(target=self._run, daemon=True)
                worker.start()
                self.worker = worker
            except BaseException:
                self.worker = None
                self.last_result = "worker_start_error"

    def transition(self, warning: str | None) -> None:
        """Record the newest alert transition without delaying health checks."""
        if (
            not self.enabled
            or self.worker is None
            or (warning is not None and warning not in NOTIFICATION_WARNINGS)
        ):
            return
        with self.condition:
            if self.closed:
                return
            self.pending = warning
            self.pending_set = True
            self.condition.notify()

    def _record(self, attempted_at: float, result: str) -> None:
        with self.condition:
            self.last_attempt = attempted_at
            self.last_result = result

    def status(self) -> dict[str, Any]:
        with self.condition:
            return {
                "enabled": self.enabled,
                "last_attempt": self.last_attempt,
                "last_result": self.last_result,
            }

    def _attempt(self) -> None:
        attempted_at = self.wall_clock()
        # Every attempt consumes the cooldown, including timeouts and launch errors.
        self.last_attempt_monotonic = self.monotonic()
        result = "error"
        try:
            result = self.sender()
        except subprocess.TimeoutExpired:
            result = "timeout"
        except BaseException:
            result = "error"
        self._record(attempted_at, result)

    def _run(self) -> None:
        event: str | None = None
        while True:
            try:
                with self.condition:
                    while not self.closed and event is None and not self.pending_set:
                        self.condition.wait()
                    if self.closed:
                        return
                    if self.pending_set:
                        event = self.pending
                        self.pending = None
                        self.pending_set = False
                    while not self.closed and self.last_attempt_monotonic is not None:
                        remaining = self.cooldown - (
                            self.monotonic() - self.last_attempt_monotonic
                        )
                        if remaining <= 0:
                            break
                        self.condition.wait(remaining)
                        if self.pending_set:
                            event = self.pending
                            self.pending = None
                            self.pending_set = False
                    if self.closed:
                        return
                if event in NOTIFICATION_WARNINGS:
                    self._attempt()
                event = None
            except BaseException:
                # Nothing in this worker may affect supervision or the child.
                event = None

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.pending = None
            self.pending_set = False
            self.condition.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=NOTIFICATION_TIMEOUT + 0.5)


class RotatingBinaryLog:
    def __init__(self, path: Path, limit: int = LOG_LIMIT, archives: int = LOG_ARCHIVES):
        self.path = path
        self.limit = limit
        self.archives = archives
        self.stream: BinaryIO | None = None
        self.size = 0

    def _archive(self, number: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{number}")

    def open(self) -> None:
        for candidate in (self.path, *(self._archive(i) for i in range(1, self.archives + 1))):
            _safe_file(candidate)
            self._trim_existing(candidate)
            self._secure_existing(candidate)
        self._open_active()
        if self.size >= self.limit:
            self._rotate()

    def _secure_existing(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def _trim_existing(self, path: Path) -> None:
        """Normalize legacy unbounded logs while retaining their newest bytes."""
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return
        if metadata.st_size <= self.limit:
            return
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
            ):
                raise SupervisorError("unsafe legacy log")
            os.lseek(fd, -self.limit, os.SEEK_END)
            retained = bytearray()
            while len(retained) < self.limit:
                chunk = os.read(fd, min(READ_SIZE, self.limit - len(retained)))
                if not chunk:
                    break
                retained.extend(chunk)
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(retained)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.ftruncate(fd, len(retained))
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def _open_active(self) -> None:
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            os.close(fd)
            raise SupervisorError("unsafe active log")
        os.fchmod(fd, 0o600)
        self.size = metadata.st_size
        self.stream = os.fdopen(fd, "ab", buffering=0)

    def _rotate(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        oldest = self._archive(self.archives)
        with contextlib.suppress(FileNotFoundError):
            oldest.unlink()
        for number in range(self.archives - 1, 0, -1):
            source = self._archive(number)
            if source.exists():
                os.replace(source, self._archive(number + 1))
        if self.path.exists():
            os.replace(self.path, self._archive(1))
        self._open_active()

    def write(self, chunk: bytes) -> None:
        offset = 0
        while offset < len(chunk):
            if self.size >= self.limit:
                self._rotate()
            portion = chunk[offset : offset + min(self.limit - self.size, READ_SIZE)]
            assert self.stream is not None
            self.stream.write(portion)
            self.size += len(portion)
            offset += len(portion)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def _drain(source: BinaryIO, sink: RotatingBinaryLog, failed: threading.Event) -> None:
    try:
        while True:
            try:
                chunk = source.read(READ_SIZE)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if failed.is_set():
                continue
            try:
                sink.write(chunk)
            except (OSError, SupervisorError):
                failed.set()  # continue draining and discard; never block the child
    finally:
        with contextlib.suppress(OSError):
            source.close()


def validate_health(value: Any, instance_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "instance_id", "backend_pid", "uptime_seconds", "worker"
    }:
        raise SupervisorError("invalid health schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SupervisorError("invalid health version")
    if type(value["instance_id"]) is not str or value["instance_id"] != instance_id:
        raise SupervisorError("health identity mismatch")
    if type(value["backend_pid"]) is not int or value["backend_pid"] <= 0:
        raise SupervisorError("invalid backend pid")
    uptime = value["uptime_seconds"]
    if not _finite_nonnegative(uptime):
        raise SupervisorError("invalid uptime")
    worker = value["worker"]
    expected = {
        "state", "cycle_in_progress", "last_progress_age_seconds",
        "cycle_age_seconds", "data_degraded",
    }
    if not isinstance(worker, dict) or set(worker) != expected:
        raise SupervisorError("invalid worker schema")
    if type(worker["state"]) is not str or worker["state"] not in {
        "disabled", "starting", "running", "stalled", "stopped"
    }:
        raise SupervisorError("invalid worker state")
    if type(worker["cycle_in_progress"]) is not bool or type(worker["data_degraded"]) is not bool:
        raise SupervisorError("invalid worker flag")
    for name in ("last_progress_age_seconds", "cycle_age_seconds"):
        field = worker[name]
        if field is not None and not _finite_nonnegative(field):
            raise SupervisorError("invalid worker age")
    return value


def _finite_nonnegative(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, TypeError):
        return False


def probe_health(
    port: int, instance_id: str, *, timeout: float = HEALTH_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    deadline = clock() + timeout
    request = (
        f"GET {HEALTH_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Connection: close\r\nAccept: application/json\r\n\r\n"
    ).encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        error = sock.connect_ex(("127.0.0.1", port))
        if error not in {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY}:
            raise SupervisorError("health connect failed")
        sent = 0
        raw = bytearray()
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise SupervisorError("health deadline")
            readable, writable, exceptional = select.select(
                [sock] if sent == len(request) else [],
                [sock] if sent < len(request) else [],
                [sock],
                remaining,
            )
            if exceptional:
                raise SupervisorError("health socket failed")
            if writable:
                socket_error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if socket_error:
                    raise SupervisorError("health connect failed")
                sent += sock.send(request[sent:])
                continue
            if not readable:
                raise SupervisorError("health deadline")
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > HEALTH_LIMIT:
                raise SupervisorError("health response too large")
        body = _http_body(bytes(raw))
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise SupervisorError("invalid health JSON") from exc
        try:
            return validate_health(value, instance_id)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            raise SupervisorError("invalid health payload") from exc
    except OSError as exc:
        raise SupervisorError("health request failed") from exc
    finally:
        sock.close()


def _http_body(raw: bytes) -> bytes:
    header, marker, body = raw.partition(b"\r\n\r\n")
    if not marker or len(header) > 8192:
        raise SupervisorError("invalid health response")
    lines = header.split(b"\r\n")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2 or parts[1] != b"200":
        raise SupervisorError("health HTTP status")
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator:
            raise SupervisorError("invalid health header")
        headers[name.strip().lower()] = value.strip().lower()
    length = headers.get(b"content-length")
    if length is not None:
        if (
            not length.isdigit()
            or len(length) > 6
            or int(length) > HEALTH_LIMIT
            or len(body) != int(length)
        ):
            raise SupervisorError("invalid health length")
    elif headers.get(b"transfer-encoding") == b"chunked":
        body = _decode_chunked(body)
    if len(body) > HEALTH_LIMIT:
        raise SupervisorError("health body too large")
    return body


def _decode_chunked(wire: bytes) -> bytes:
    output = bytearray()
    offset = 0
    while True:
        end = wire.find(b"\r\n", offset)
        if end < 0:
            raise SupervisorError("incomplete chunk")
        size_text = wire[offset:end].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise SupervisorError("invalid chunk") from exc
        offset = end + 2
        if size == 0:
            if wire[offset:] not in {b"\r\n", b""}:
                # Trailers are unnecessary for this private endpoint.
                raise SupervisorError("unexpected health trailer")
            return bytes(output)
        complete = offset + size + 2 <= len(wire)
        terminated = wire[offset + size : offset + size + 2] == b"\r\n"
        if size < 0 or not complete or not terminated:
            raise SupervisorError("incomplete chunk")
        output.extend(wire[offset : offset + size])
        if len(output) > HEALTH_LIMIT:
            raise SupervisorError("health body too large")
        offset += size + 2


class HealthMonitor:
    def __init__(
        self, instance_id: str, snapshot: RuntimeSnapshot, stop: threading.Event, *,
        ports: Sequence[int] = HEALTH_PORTS, monotonic: Callable[[], float] = time.monotonic,
        probe: Callable[[int, str], dict[str, Any]] = probe_health,
        notifier: LocalNotifier | None = None,
    ):
        self.instance_id = instance_id
        self.snapshot = snapshot
        self.stop = stop
        self.ports = ports
        self.monotonic = monotonic
        self.probe = probe
        self.notifier = notifier

    def run(self) -> None:
        began = self.monotonic()
        next_check = began
        failures = {port: 0 for port in self.ports}
        previous_warning: str | None = None
        while not self.stop.is_set():
            components: dict[str, Any] = {}
            workers: list[dict[str, Any]] = []
            for port in self.ports:
                try:
                    health = self.probe(port, self.instance_id)
                    failures[port] = 0
                    components[str(port)] = {
                        "responsive": True,
                        "worker": health["worker"],
                    }
                    workers.append(health["worker"])
                except SupervisorError:
                    failures[port] += 1
                    components[str(port)] = {"responsive": False, "failures": failures[port]}
            warning: str | None = None
            if self.monotonic() - began >= STARTUP_GRACE and any(v >= 3 for v in failures.values()):
                warning = "http_unresponsive"
            if any(w["state"] in {"stalled", "stopped"} or w["data_degraded"] for w in workers):
                warning = "worker_attention"
            if self.snapshot.value.get("logging", {}).get("failed"):
                warning = "logging_failed"
            transition = self.snapshot.value.get("last_transition")
            if warning != previous_warning:
                transition = {"at": time.time(), "warning": warning}
                previous_warning = warning
                if self.notifier is not None:
                    try:
                        self.notifier.transition(warning)
                    except BaseException:
                        pass
            self.snapshot.update(
                components=components,
                current_warning=warning,
                last_transition=transition,
                notifications=(
                    self.notifier.status()
                    if self.notifier is not None
                    else {"enabled": False, "last_attempt": None, "last_result": None}
                ),
            )
            next_check += POLL_INTERVAL
            now = self.monotonic()
            while next_check <= now:
                next_check += POLL_INTERVAL
            self.stop.wait(next_check - now)


def _log_directory(home: Path) -> Path:
    path = home / "Library" / "Logs" / "SpotLab"
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise SupervisorError("unsafe log directory")
    os.chmod(path, 0o700)
    return path


def supervise(args: argparse.Namespace, environment: Mapping[str, str] = os.environ) -> int:
    root = args.root.resolve(strict=True)
    snapshot_path = args.snapshot
    expected_snapshot = root / "data" / "service" / "runtime.json"
    if (
        snapshot_path != expected_snapshot
        or snapshot_path.parent.resolve(strict=True) != expected_snapshot.parent
    ):
        raise SupervisorError("invalid runtime snapshot path")
    instance_id = str(environment.get(INSTANCE_ENV, ""))
    if len(instance_id) != 32 or any(c not in "0123456789abcdef" for c in instance_id):
        raise SupervisorError("invalid service instance")
    log_dir = _log_directory(Path(environment["HOME"]).resolve(strict=True))
    lock_path = log_dir / ".supervisor.lock"
    _safe_file(lock_path)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(lock_fd)
        raise
    base = {
        "schema_version": 1, "run_id": instance_id, "revision": args.revision,
        "root": str(root), "supervisor_pid": os.getpid(), "phase": "starting",
        "components": {}, "logging": {"failed": False}, "current_warning": None,
        "last_transition": {"at": time.time(), "warning": None},
        "notifications": {
            "enabled": bool(getattr(args, "notify_local", False)),
            "last_attempt": None,
            "last_result": None,
        },
    }
    snapshot = RuntimeSnapshot(snapshot_path, base)
    snapshot.update()
    stdout_log = RotatingBinaryLog(log_dir / "stdout.log")
    stderr_log = RotatingBinaryLog(log_dir / "stderr.log")
    child: subprocess.Popen[bytes] | None = None
    monitor_stop = threading.Event()
    logging_failed = threading.Event()
    forwarded = threading.Event()
    old_handlers: dict[int, Any] = {}
    drainers: list[threading.Thread] = []
    monitor: threading.Thread | None = None
    monitor_failed = threading.Event()
    notifier = LocalNotifier(bool(getattr(args, "notify_local", False)))
    try:
        stdout_log.open()
        stderr_log.open()
        command = ["/bin/bash", str(root / "scripts" / "run-local.sh")]
        if args.keep_awake:
            command.append("--keep-awake")
        child = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(environment), bufsize=0,
        )
        assert child.stdout is not None and child.stderr is not None

        def forward(signum: int, _frame: Any) -> None:
            forwarded.set()
            if child is not None and child.poll() is None:
                with contextlib.suppress(OSError):
                    child.send_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, forward)
        drainers = [
            threading.Thread(
                target=_drain, args=(child.stdout, stdout_log, logging_failed), daemon=True
            ),
            threading.Thread(
                target=_drain, args=(child.stderr, stderr_log, logging_failed), daemon=True
            ),
        ]
        for thread in drainers:
            thread.start()
        def monitor_health() -> None:
            try:
                HealthMonitor(instance_id, snapshot, monitor_stop, notifier=notifier).run()
            except BaseException:
                monitor_failed.set()

        monitor = threading.Thread(target=monitor_health, daemon=True)
        monitor.start()
        snapshot.update(phase="running")
        while child.poll() is None and not forwarded.is_set():
            if monitor_failed.is_set():
                with contextlib.suppress(OSError, SupervisorError):
                    snapshot.update(phase="failed", current_warning="supervisor_failure")
                return 1
            if logging_failed.is_set() and not snapshot.value["logging"]["failed"]:
                snapshot.update(logging={"failed": True}, current_warning="logging_failed")
            time.sleep(0.1)
        if forwarded.is_set() and child.poll() is None:
            try:
                child.wait(timeout=STOP_WAIT)
            except subprocess.TimeoutExpired:
                snapshot.update(phase="stopping", current_warning="child_stop_timeout")
                return 1
        return int(child.returncode or 0)
    except BaseException:
        with contextlib.suppress(OSError, SupervisorError):
            snapshot.update(phase="failed", current_warning="supervisor_failure")
        raise
    finally:
        monitor_stop.set()
        with contextlib.suppress(BaseException):
            notifier.close()
        if child is not None:
            for pipe in (child.stdout, child.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()
        for thread in drainers:
            thread.join(timeout=1.0)
        if monitor is not None:
            monitor.join(timeout=1.0)
        if (
            (child is None or child.poll() is not None)
            and snapshot.value.get("phase") != "failed"
        ):
            with contextlib.suppress(OSError, SupervisorError):
                snapshot.update(phase="stopped")
        for restore_signum, handler in old_handlers.items():
            signal.signal(restore_signum, handler)
        stdout_log.close()
        stderr_log.close()
        os.close(lock_fd)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--root", type=Path, required=True)
    result.add_argument("--snapshot", type=Path, required=True)
    result.add_argument("--revision", required=True)
    result.add_argument("--keep-awake", action="store_true")
    result.add_argument("--notify-local", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return supervise(parser().parse_args(argv))
    except (OSError, SupervisorError, KeyError, BlockingIOError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
