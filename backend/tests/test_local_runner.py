from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTED_PROCESSES: list[subprocess.Popen[str]] = []


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


def _allocate_ports() -> tuple[int, int]:
    sockets = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(2)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return sockets[0].getsockname()[1], sockets[1].getsockname()[1]
    finally:
        for sock in sockets:
            sock.close()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    process_start = len(STARTED_PROCESSES)
    api_port, web_port = _allocate_ports()
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    bin_dir = repo / "fake-bin"
    venv_bin = repo / ".venv" / "bin"
    vite = repo / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    scripts.mkdir(parents=True)
    bin_dir.mkdir()
    venv_bin.mkdir(parents=True)
    vite.parent.mkdir(parents=True)
    vite.write_text("// fake vite\n", encoding="utf-8")
    runner = (REPO_ROOT / "scripts" / "run-local.sh").read_text(encoding="utf-8")
    port_map = {"8000": str(api_port), "5173": str(web_port)}
    runner = re.sub(r"\b(?:8000|5173)\b", lambda match: port_map[match.group()], runner)
    _write_executable(scripts / "run-local.sh", runner)
    shutil.copy2(REPO_ROOT / "scripts" / "node-env.sh", scripts / "node-env.sh")

    for name, target in (
        ("dirname", "/usr/bin/dirname"),
        ("mkdir", "/bin/mkdir"),
        ("sleep", "/bin/sleep"),
    ):
        (bin_dir / name).symlink_to(target)

    _write_executable(bin_dir / "npm", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "uname",
        "#!/bin/sh\nprintf '%s\\n' \"${FAKE_UNAME:-Darwin}\"\n",
    )
    _write_executable(
        venv_bin / "python",
        f"""#!/bin/sh
if [ "${{1:-}}" = "-c" ]; then
  exec {sys.executable!r} "$@"
fi
trap 'printf stopped > "$API_STOPPED_FILE"; exit 0' INT TERM HUP
printf '%s\\n' "$$" > "$API_PID_FILE"
if [ "${{API_DIE:-0}}" = 1 ]; then exit 7; fi
while :; do sleep 0.05; done
""",
    )
    _write_executable(venv_bin / "uvicorn", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "node",
        """#!/bin/sh
trap 'printf stopped > "$WEB_STOPPED_FILE"; exit 0' INT TERM HUP
printf '%s\n' "$$" > "$WEB_PID_FILE"
if [ "${WEB_DIE:-0}" = 1 ]; then exit 8; fi
while :; do sleep 0.05; done
""",
    )
    _write_executable(
        bin_dir / "caffeinate",
        """#!/bin/sh
printf '%s\n' "$*" > "$CAFFEINATE_ARGS_FILE"
trap 'printf stopped > "$CAFFEINATE_STOPPED_FILE"; exit 0' INT TERM HUP
printf '%s\n' "$$" > "$CAFFEINATE_PID_FILE"
if [ "${CAFFEINATE_DIE:-0}" = 1 ]; then exit 9; fi
while kill -0 "$3" 2>/dev/null; do sleep 0.05; done
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(bin_dir),
            "API_PID_FILE": str(tmp_path / "api.pid"),
            "API_STOPPED_FILE": str(tmp_path / "api.stopped"),
            "WEB_PID_FILE": str(tmp_path / "web.pid"),
            "WEB_STOPPED_FILE": str(tmp_path / "web.stopped"),
            "CAFFEINATE_ARGS_FILE": str(tmp_path / "caffeinate.args"),
            "CAFFEINATE_PID_FILE": str(tmp_path / "caffeinate.pid"),
            "CAFFEINATE_STOPPED_FILE": str(tmp_path / "caffeinate.stopped"),
            "TEST_API_PORT": str(api_port),
            "TEST_WEB_PORT": str(web_port),
        }
    )
    yield repo, env

    for marker in ("API_PID_FILE", "WEB_PID_FILE", "CAFFEINATE_PID_FILE"):
        path = Path(env[marker])
        if path.exists():
            try:
                os.kill(int(path.read_text()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
    for process in STARTED_PROCESSES[process_start:]:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=1)


def _run(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # Let fixture teardown stop children if communicate times out. subprocess.run
    # can wait forever for inherited pipes after killing only the launcher.
    process = _start(repo, env, *args)
    stdout, stderr = process.communicate(timeout=8)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _assert_service_gone(env: dict[str, str], name: str) -> None:
    marker = Path(env[f"{name}_PID_FILE"])
    if marker.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)


def _start(repo: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        ["/bin/bash", "scripts/run-local.sh", *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    STARTED_PROCESSES.append(process)
    return process


def test_help_succeeds_without_dependencies(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "scripts" / "run-local.sh"), "--help"],
        cwd=tmp_path,
        env={"PATH": ""},
        text=True,
        capture_output=True,
        timeout=3,
    )

    assert result.returncode == 0
    assert "--keep-awake" in result.stdout


def test_keep_awake_rejects_unsupported_platform_before_backend(fake_repo) -> None:
    repo, env = fake_repo
    env["FAKE_UNAME"] = "Linux"

    result = _run(repo, env, "--keep-awake")

    assert result.returncode == 1
    assert "yalnızca macOS" in result.stderr
    assert not Path(env["API_PID_FILE"]).exists()


def test_keep_awake_requires_caffeinate_before_backend(fake_repo) -> None:
    repo, env = fake_repo
    (repo / "fake-bin" / "caffeinate").unlink()

    result = _run(repo, env, "--keep-awake")

    assert result.returncode == 1
    assert "caffeinate bulunamadı" in result.stderr
    assert not Path(env["API_PID_FILE"]).exists()


def test_missing_vite_is_reported_before_backend(fake_repo) -> None:
    repo, env = fake_repo
    (repo / "frontend" / "node_modules" / "vite" / "bin" / "vite.js").unlink()

    result = _run(repo, env)

    assert result.returncode == 1
    assert "Vite bulunamadı" in result.stderr
    assert not Path(env["API_PID_FILE"]).exists()


@pytest.mark.parametrize("port_key", ["TEST_API_PORT", "TEST_WEB_PORT"])
def test_occupied_port_prevents_backend_start(fake_repo, port_key: str) -> None:
    repo, env = fake_repo
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", int(env[port_key])))
    listener.listen(1)
    try:
        result = _run(repo, env)
    finally:
        listener.close()

    assert result.returncode == 1
    assert f"{env['TEST_API_PORT']} ve {env['TEST_WEB_PORT']} portlarının boş" in result.stderr
    assert not Path(env["API_PID_FILE"]).exists()
    assert not Path(env["WEB_PID_FILE"]).exists()


def test_recently_closed_socket_does_not_block_preflight(fake_repo) -> None:
    repo, env = fake_repo
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", int(env["TEST_API_PORT"])))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    accepted, _ = listener.accept()
    accepted.close()
    assert client.recv(1) == b""
    client.close()
    listener.close()

    process = _start(repo, env)
    _wait_for(Path(env["API_PID_FILE"]))
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, (stdout, stderr)


@pytest.mark.parametrize(
    ("failure_env", "expected_message"),
    [
        ("API_DIE", "Backend durdu"),
        ("WEB_DIE", "Web sunucusu durdu"),
    ],
)
def test_service_failure_stops_peer(
    fake_repo, failure_env: str, expected_message: str
) -> None:
    repo, env = fake_repo
    env[failure_env] = "1"

    result = _run(repo, env)

    assert result.returncode == 1
    assert expected_message in result.stderr
    _assert_service_gone(env, "API")
    _assert_service_gone(env, "WEB")


def test_guard_failure_stops_both_services(fake_repo) -> None:
    repo, env = fake_repo
    process = _start(repo, env, "--keep-awake")
    _wait_for(Path(env["API_PID_FILE"]))
    _wait_for(Path(env["WEB_PID_FILE"]))
    _wait_for(Path(env["CAFFEINATE_PID_FILE"]))
    guard_pid = int(Path(env["CAFFEINATE_PID_FILE"]).read_text())

    os.kill(guard_pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 1, (stdout, stderr)
    assert "önleme isteği beklenmedik" in stderr
    assert Path(env["API_STOPPED_FILE"]).read_text() == "stopped"
    assert Path(env["WEB_STOPPED_FILE"]).read_text() == "stopped"


def test_immediate_guard_failure_leaves_no_services(fake_repo) -> None:
    repo, env = fake_repo
    env["CAFFEINATE_DIE"] = "1"

    result = _run(repo, env, "--keep-awake")

    assert result.returncode == 1
    assert "panel başlatılmadı" in result.stderr or "önleme isteği beklenmedik" in result.stderr
    # Scheduling can put the exit on either side of the pre-service check.
    _assert_service_gone(env, "API")
    _assert_service_gone(env, "WEB")
    _assert_service_gone(env, "CAFFEINATE")


@pytest.mark.parametrize("sent_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signal_cleans_up_services_and_guard(fake_repo, sent_signal: signal.Signals) -> None:
    repo, env = fake_repo
    process = _start(repo, env, "--keep-awake")
    _wait_for(Path(env["API_PID_FILE"]))
    _wait_for(Path(env["WEB_PID_FILE"]))
    _wait_for(Path(env["CAFFEINATE_PID_FILE"]))

    process.send_signal(sent_signal)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, (stdout, stderr)
    assert Path(env["API_STOPPED_FILE"]).read_text() == "stopped"
    assert Path(env["WEB_STOPPED_FILE"]).read_text() == "stopped"
    assert Path(env["CAFFEINATE_STOPPED_FILE"]).read_text() == "stopped"
    assert Path(env["CAFFEINATE_ARGS_FILE"]).read_text().startswith("-i -w ")


def test_default_mode_does_not_start_keep_awake_guard(fake_repo) -> None:
    repo, env = fake_repo
    process = _start(repo, env)
    _wait_for(Path(env["API_PID_FILE"]))
    _wait_for(Path(env["WEB_PID_FILE"]))

    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, (stdout, stderr)
    assert not Path(env["CAFFEINATE_PID_FILE"]).exists()
    assert Path(env["API_STOPPED_FILE"]).read_text() == "stopped"
    assert Path(env["WEB_STOPPED_FILE"]).read_text() == "stopped"
