import asyncio
import math
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

import spotlab.main as main
from spotlab.main import create_app, paper_loop


class TaskStatus:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SPOTLAB_DATA_DIR", str(tmp_path / "data"))
    app = create_app(f"sqlite:///{tmp_path / 'paper.sqlite'}", run_worker=False)
    with TestClient(app) as session:
        try:
            yield session
        finally:
            # State-only task doubles exercise the health reader; they are not
            # lifecycle-managed asyncio tasks.
            app.state.worker_task = None


def worker_health(client: TestClient) -> dict[str, object]:
    response = client.get("/api/health")
    assert response.status_code == 200
    return response.json()["worker"]


def set_live_worker(client: TestClient, *, done: bool = False) -> None:
    client.app.state.background_enabled = True
    client.app.state.worker_task = TaskStatus(done)  # type: ignore[assignment]


def test_health_schema_is_cheap_and_does_not_change_runtime_state(client: TestClient) -> None:
    engine_snapshot = Mock(side_effect=AssertionError("health must not read the ledger"))
    quote = AsyncMock(side_effect=AssertionError("health must not use the network"))
    client.app.state.engine.snapshot = engine_snapshot
    client.app.state.market.quote = quote
    before = dict(client.app.state._state)

    with (
        patch("spotlab.main.version", side_effect=AssertionError("health must not invoke git")),
        patch(
            "spotlab.main.subprocess.check_output",
            side_effect=AssertionError("health must not spawn processes"),
        ),
    ):
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "instance_id",
        "backend_pid",
        "uptime_seconds",
        "worker",
    }
    assert payload["schema_version"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", payload["instance_id"])
    assert payload["backend_pid"] == os.getpid()
    assert math.isfinite(payload["uptime_seconds"])
    assert payload["uptime_seconds"] >= 0
    assert payload["worker"] == {
        "state": "disabled",
        "cycle_in_progress": False,
        "last_progress_age_seconds": None,
        "cycle_age_seconds": None,
        "data_degraded": False,
    }
    assert dict(client.app.state._state) == before
    engine_snapshot.assert_not_called()
    quote.assert_not_awaited()


def test_idle_progress_and_data_degradation_are_advisory(client: TestClient) -> None:
    set_live_worker(client)
    now = time.monotonic()
    client.app.state.worker_last_progress_monotonic = now - 10
    assert worker_health(client) == {
        "state": "running",
        "cycle_in_progress": False,
        "last_progress_age_seconds": pytest.approx(10, abs=1),
        "cycle_age_seconds": None,
        "data_degraded": False,
    }

    client.app.state.worker_last_progress_monotonic = time.monotonic()
    client.app.state.worker_data_degraded = True
    degraded = worker_health(client)
    assert degraded["state"] == "running"
    assert degraded["data_degraded"] is True


def test_in_progress_cycle_stalls_only_after_advisory_threshold(client: TestClient) -> None:
    set_live_worker(client)
    started = time.monotonic() - 301
    client.app.state.worker_cycle_in_progress = True
    client.app.state.worker_cycle_started_monotonic = started
    client.app.state.worker_last_progress_monotonic = started

    health = worker_health(client)
    assert health["state"] == "stalled"
    assert health["cycle_in_progress"] is True
    assert health["cycle_age_seconds"] == pytest.approx(301, abs=1)
    assert health["last_progress_age_seconds"] == pytest.approx(301, abs=1)


def test_worker_disabled_starting_and_stopped_states(client: TestClient) -> None:
    assert worker_health(client)["state"] == "disabled"

    set_live_worker(client)
    assert worker_health(client) == {
        "state": "starting",
        "cycle_in_progress": False,
        "last_progress_age_seconds": None,
        "cycle_age_seconds": None,
        "data_degraded": False,
    }

    client.app.state.worker_task = TaskStatus(done=True)
    client.app.state.worker_cycle_in_progress = True
    client.app.state.worker_cycle_started_monotonic = time.monotonic()
    stopped = worker_health(client)
    assert stopped["state"] == "stopped"
    assert stopped["cycle_in_progress"] is False
    assert stopped["cycle_age_seconds"] is None


@pytest.mark.parametrize("bad_timestamp", [float("nan"), float("inf"), -float("inf")])
def test_invalid_worker_timestamp_is_unknown_and_stalled(
    client: TestClient, bad_timestamp: float
) -> None:
    set_live_worker(client)
    client.app.state.worker_last_progress_monotonic = bad_timestamp
    health = worker_health(client)
    assert health["state"] == "stalled"
    assert health["last_progress_age_seconds"] is None


def test_future_worker_timestamp_is_unknown_and_stalled(client: TestClient) -> None:
    set_live_worker(client)
    client.app.state.worker_last_progress_monotonic = time.monotonic() + 1
    health = worker_health(client)
    assert health["state"] == "stalled"
    assert health["last_progress_age_seconds"] is None


def test_instance_identity_accepts_only_32_hex_and_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTLAB_DATA_DIR", str(tmp_path / "valid-data"))
    monkeypatch.setenv("SPOTLAB_SERVICE_INSTANCE_ID", "ABCDEF0123456789ABCDEF0123456789")
    valid_app = create_app(f"sqlite:///{tmp_path / 'valid.sqlite'}", run_worker=False)
    with TestClient(valid_app) as valid_client:
        first = valid_client.get("/api/health").json()["instance_id"]
        second = valid_client.get("/api/health").json()["instance_id"]
    assert first == second == "abcdef0123456789abcdef0123456789"

    monkeypatch.setenv("SPOTLAB_DATA_DIR", str(tmp_path / "invalid-data"))
    monkeypatch.setenv("SPOTLAB_SERVICE_INSTANCE_ID", "not-a-valid-instance")
    invalid_app = create_app(f"sqlite:///{tmp_path / 'invalid.sqlite'}", run_worker=False)
    with TestClient(invalid_app) as invalid_client:
        generated = invalid_client.get("/api/health").json()["instance_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    assert generated != "not-a-valid-instance"


@pytest.mark.asyncio
async def test_paper_loop_records_real_cycle_completion_and_degraded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SimpleNamespace(
        worker_cycle_in_progress=False,
        worker_cycle_started_monotonic=None,
        worker_last_progress_monotonic=None,
        worker_data_degraded=False,
        worker_error=None,
    )
    app = SimpleNamespace(state=state)
    cycle_observation: dict[str, object] = {}

    async def degraded_cycle(_app: object) -> None:
        cycle_observation.update(
            in_progress=state.worker_cycle_in_progress,
            started=state.worker_cycle_started_monotonic,
            progress=state.worker_last_progress_monotonic,
        )
        state.worker_error = "market data degraded"

    async def stop_after_completion(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "paper_cycle", degraded_cycle)
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_completion)

    with pytest.raises(asyncio.CancelledError):
        await paper_loop(app)  # type: ignore[arg-type]

    assert cycle_observation["in_progress"] is True
    assert isinstance(cycle_observation["started"], float)
    assert cycle_observation["progress"] == cycle_observation["started"]
    assert state.worker_cycle_in_progress is False
    assert state.worker_cycle_started_monotonic is None
    assert isinstance(state.worker_last_progress_monotonic, float)
    assert state.worker_data_degraded is True
