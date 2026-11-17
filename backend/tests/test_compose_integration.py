from __future__ import annotations

import io
import json
import os
import re
import secrets
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT_PATTERN = re.compile(r"^spotlab-it-[0-9a-f]{12}$")

SEED_SCRIPT = r"""
import os
from decimal import Decimal
from unittest.mock import Mock
from sqlalchemy import text
from spotlab.contracts import Quote
from spotlab.engine import PaperConfig, PaperEngine
from spotlab.orders import (
    ConfirmedFill, ExchangeUpdate, OrderState, TESTNET_ACCOUNT_VENUE,
)
from spotlab.testnet_cycle import CyclePlan, TestnetCycle

at = 1900000000000
engine = PaperEngine(os.environ['DATABASE_URL'])
config = PaperConfig(
    venue='global', symbol='BTCUSDT', interval='5m', strategy='pullback',
    capital=Decimal('1000'), risk_per_trade=Decimal('0.01'),
    daily_loss_limit=Decimal('0.5'), max_drawdown=Decimal('0.8'),
    max_open_risk=Decimal('0.02'), target_pct=Decimal('0.02'),
    stop_pct=Decimal('0.01'), fee_rate=Decimal('0.001'),
    slippage_bps=Decimal('0'), max_spread_bps=Decimal('30'),
    hold_minutes=60, max_trades_per_day=10, max_consecutive_losses=3,
    authorization_minutes=120)
first = engine.snapshot()['session_id']
engine.start(config, at, expected_session_id=first)
entry = Quote('BTCUSDT', Decimal('100'), Decimal('100'), Decimal('1000'), Decimal('1000'), at)
engine.tick(entry, signal=True, candle_time=at-1, step=Decimal('0.01'),
            min_notional=Decimal('5'), now=at, expected_session_id=first)
exit_quote = Quote(
    'BTCUSDT', Decimal('103'), Decimal('103'), Decimal('1000'), Decimal('1000'), at+1000
)
engine.tick(exit_quote, signal=False, candle_time=at-1, step=Decimal('0.01'),
            min_notional=Decimal('5'), now=at+1000, expected_session_id=first)
engine.stop(expected_session_id=first)
current = engine.archive(first, at+2000)
engine.start(config, at+3000, expected_session_id=current['session_id'])
engine.db.dispose()

ledger_url = 'sqlite:////app/data/testnet-orders.sqlite'
cycle_store = TestnetCycle(ledger_url, Mock())
ledger = cycle_store.store
ledger.bind_account(TESTNET_ACCOUNT_VENUE, 424242)
plan = CyclePlan(
    'integration-closed', 'BTCUSDT', Decimal('0.5'), Decimal('100'),
    Decimal('120'), Decimal('80'), Decimal('50'), at + 60000,
)
plan.validate()
entry_request = plan.entry()
ledger.create_or_get(entry_request)
assert ledger.claim_submission(entry_request.intent_id)
ledger.apply_update(
    entry_request.intent_id,
    ExchangeUpdate(
        entry_request.client_order_id, 'synthetic-exchange-order', OrderState.FILLED,
        Decimal('0.5'),
        (ConfirmedFill('synthetic-trade', Decimal('0.5'), Decimal('50'),
                       Decimal('0.05'), 'USDT'),),
    ),
)
with ledger.db.begin() as connection:
    connection.execute(
        text('INSERT INTO testnet_cycles '
             '(cycle_id,plan,state,protection_quantity,dust,reason,stop_entry,updated_at) '
             "VALUES (:cycle,:plan,'EXITED','0.5','0',NULL,1,:at)"),
        {'cycle': plan.cycle_id, 'plan': plan.serialize(), 'at': at},
    )
ledger.db.dispose()
print(current['session_id'])
"""

VERIFY_LEDGER_SCRIPT = r"""
import json, sqlite3
from spotlab.testnet_cycle import CyclePlan
connection = sqlite3.connect('/app/data/testnet-orders.sqlite')
connection.row_factory = sqlite3.Row
cycle = connection.execute(
    'SELECT cycle_id,plan,state,stop_entry FROM testnet_cycles WHERE cycle_id=?',
    ('integration-closed',),
).fetchone()
plan = CyclePlan.deserialize(cycle['plan'])
payload = {
    'binding': connection.execute(
        'SELECT venue,account_uid FROM ledger_account_binding'
    ).fetchone(),
    'intent': connection.execute(
        'SELECT state,confirmed_quantity,confirmed_quote_quantity,confirmed_fee_quote '
        'FROM order_intents WHERE intent_id=?', (plan.entry_id,),
    ).fetchone(),
    'fill_count': connection.execute('SELECT COUNT(*) FROM order_fills').fetchone()[0],
    'cycle': [cycle['cycle_id'], plan.symbol, cycle['state'], cycle['stop_entry']],
}
print(json.dumps({key: list(value) if hasattr(value, 'keys') else value
                  for key, value in payload.items()}))
connection.close()
"""


def run(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 180,
    secret: str = "",
) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True,
            timeout=timeout, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        detail = (stdout + "\n" + stderr)[-4000:]
        if secret:
            detail = detail.replace(secret, "<redacted>")
        raise AssertionError(f"command failed: {' '.join(command[:2])}\n{detail}") from error
    return result.stdout.strip()


def get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        assert response.status == 200
        value = json.load(response)
    assert isinstance(value, dict)
    return value


def wait_json(url: str, seconds: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return get_json(url)
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(1)
    raise AssertionError(f"service did not become ready: {url}") from last_error


def post_status(url: str) -> int:
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as error:
        return error.code
    return 200


def wait_worker_running(api_url: str, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if get_json(api_url + "/api/health")["worker"]["state"] == "running":
            return
        time.sleep(0.5)
    raise AssertionError("default paper worker did not report running")


def clean_context(destination: Path, env: dict[str, str]) -> str:
    revision = run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=env, timeout=10)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision], cwd=ROOT, env=env,
        capture_output=True, timeout=30, check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        source.extractall(destination, filter="data")
    return revision


def host_port(
    compose: list[str], service: str, port: str, *, cwd: Path, env: dict[str, str]
) -> int:
    output = run(compose + ["port", service, port], cwd=cwd, env=env, timeout=20)
    assert output.startswith("127.0.0.1:"), f"{service} was not bound to IPv4 loopback"
    return int(output.rsplit(":", 1)[1])


def project_objects(project: str, env: dict[str, str]) -> list[str]:
    label = f"label=com.docker.compose.project={project}"
    return [
        run(["docker", "ps", "--all", "--quiet", "--filter", label], cwd=ROOT, env=env),
        run(["docker", "volume", "ls", "--quiet", "--filter", label], cwd=ROOT, env=env),
        run(["docker", "network", "ls", "--quiet", "--filter", label], cwd=ROOT, env=env),
    ]


def test_clean_compose_stack_persists_synthetic_ledgers() -> None:
    required = os.getenv("SPOTLAB_REQUIRE_COMPOSE") == "1"
    if not required:
        pytest.skip("set SPOTLAB_REQUIRE_COMPOSE=1 to run the Compose integration")
    browser_node = os.getenv("SPOTLAB_COMPOSE_BROWSER_NODE")
    if not browser_node:
        pytest.fail("Compose integration requires SPOTLAB_COMPOSE_BROWSER_NODE")
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR"}
    }
    try:
        run(["docker", "compose", "version"], cwd=ROOT, env=base_env, timeout=15)
    except (AssertionError, FileNotFoundError):
        pytest.fail("SPOTLAB_REQUIRE_COMPOSE=1 but Docker Compose is unavailable")

    project = f"spotlab-it-{secrets.token_hex(6)}"
    assert PROJECT_PATTERN.fullmatch(project)
    credential = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix="spotlab-compose-") as temporary:
        context = Path(temporary).resolve()
        assert context.parent == Path(tempfile.gettempdir()).resolve()
        revision = clean_context(context, base_env)
        env_file = context / "integration.env"
        env_file.write_text(
            "\n".join(
                [
                    "POSTGRES_DB=spotlab",
                    "POSTGRES_USER=spotlab",
                    f"POSTGRES_PASSWORD={credential}",
                    "POSTGRES_PORT=0",
                    "API_PORT=0",
                    "WEB_PORT=0",
                    "APP_ENV=integration",
                    f"DATABASE_URL=postgresql+psycopg://spotlab:{credential}@db:5432/spotlab",
                    "CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173",
                    "LIVE_TRADING_ENABLED=false",
                    f"SPOTLAB_BUILD_REVISION={revision}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        # Keep normal published-port networking; block market HTTP at the API's
        # client layer instead. HTTPX honors these environment proxies by default.
        override = context / "integration.compose.json"
        override.write_text(json.dumps({"services": {"api": {"environment": {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost,db",
        }}}}), encoding="utf-8")
        compose = [
            "docker", "compose", "--project-name", project,
            "--env-file", str(env_file),
            "--file", str(context / "compose.yaml"), "--file", str(override),
        ]
        cleanup_allowed = False
        try:
            assert not any(project_objects(project, base_env))
            assert run(compose + ["ps", "--all", "--quiet"], cwd=context, env=base_env) == ""
            cleanup_allowed = True
            run(
                compose + ["up", "--build", "--detach", "--wait", "--wait-timeout", "180"],
                cwd=context, env=base_env, timeout=600, secret=credential,
            )
            api_port = host_port(compose, "api", "8000", cwd=context, env=base_env)
            web_port = host_port(compose, "web", "5173", cwd=context, env=base_env)
            assert host_port(compose, "db", "5432", cwd=context, env=base_env) > 0
            api_url = f"http://127.0.0.1:{api_port}"
            web_url = f"http://127.0.0.1:{web_port}"
            status = wait_json(api_url + "/api/status")
            assert status["commit"] == revision
            assert status["live_enabled"] is False
            assert status["runtime"]["real_order_transport"] is False
            assert post_status(api_url + "/api/live/activate") == 409
            wait_worker_running(api_url)
            assert wait_json(web_url + "/api/status")["commit"] == revision
            # Vite permits IP hosts; the API must still reject a non-loopback
            # Host forwarded through the proxy instead of silently trusting it.
            foreign_host = urllib.request.Request(
                web_url + "/api/status", headers={"Host": "192.0.2.5"}
            )
            with pytest.raises(urllib.error.HTTPError) as host_rejected:
                urllib.request.urlopen(foreign_host, timeout=3)
            assert host_rejected.value.code == 400
            with pytest.raises(urllib.error.HTTPError) as market_unavailable:
                get_json(web_url + "/api/symbols?venue=global")
            assert market_unavailable.value.code == 503
            assert get_json(api_url + "/api/status")["worker_error"] is None

            run(compose + ["stop", "api"], cwd=context, env=base_env, timeout=60)
            session_id = run(
                compose + ["run", "--rm", "--no-deps", "api", "python", "-c", SEED_SCRIPT],
                cwd=context, env=base_env, timeout=120, secret=credential,
            ).splitlines()[-1]
            assert session_id
            run(
                compose
                + ["up", "--detach", "--force-recreate", "--wait", "db", "api", "web"],
                cwd=context, env=base_env, timeout=180, secret=credential,
            )

            api_port = host_port(compose, "api", "8000", cwd=context, env=base_env)
            web_port = host_port(compose, "web", "5173", cwd=context, env=base_env)
            assert host_port(compose, "db", "5432", cwd=context, env=base_env) > 0
            api_url = f"http://127.0.0.1:{api_port}"
            web_url = f"http://127.0.0.1:{web_port}"

            paper = wait_json(api_url + "/api/paper")
            assert paper["session_id"] == session_id
            assert paper["config"]["symbol"] == "BTCUSDT"
            assert paper["entries_enabled"] is False
            assert paper["position"] is None
            assert paper["cash"] == "1000"
            assert paper["equity"] == "1000"
            archives = get_json(api_url + "/api/paper/archives")
            assert archives["total"] == 1
            assert archives["items"][0]["fill_count"] > 0
            assert archives["items"][0]["order_count"] == 2
            assert archives["items"][0]["realized_pnl"] == "23.32698"
            detail = get_json(api_url + f"/api/paper/archives/{archives['items'][0]['session_id']}")
            archived_state = detail["state"]
            assert {
                "cash": archived_state["cash"],
                "equity": archived_state["equity"],
                "realized_pnl": archived_state["realized_pnl"],
                "daily_trades": archived_state["daily_trades"],
                "position": archived_state["position"],
            } == {
                "cash": "1023.32698",
                "equity": "1023.32698",
                "realized_pnl": "23.32698",
                "daily_trades": 1,
                "position": None,
            }
            assert [fill["side"] for fill in archived_state["fills"]] == ["BUY", "SELL"]
            assert [fill["price"] for fill in archived_state["fills"]] == ["100", "103"]
            assert [fill["quantity"] for fill in archived_state["fills"]] == ["8.34", "8.34"]
            testnet = get_json(api_url + "/api/testnet/status")
            assert testnet["ledger_status"] == "ready"
            assert testnet["cycles"] == [
                {
                    "cycle_id": "integration-closed",
                    "symbol": "BTCUSDT",
                    "state": "EXITED",
                    "stop_entry_requested": True,
                    "updated_at_ms": 1_900_000_000_000,
                }
            ]
            ledger = json.loads(
                run(
                    compose + ["exec", "-T", "api", "python", "-c", VERIFY_LEDGER_SCRIPT],
                    cwd=context,
                    env=base_env,
                    timeout=30,
                    secret=credential,
                ).splitlines()[-1]
            )
            assert ledger == {
                "binding": ["binance_spot_testnet", "424242"],
                "intent": ["FILLED", "0.5", "50", "0.05"],
                "fill_count": 1,
                "cycle": ["integration-closed", "BTCUSDT", "EXITED", 1],
            }

            browser_env = dict(base_env)
            browser_env.update(
                {
                    "SPOTLAB_COMPOSE_WEB_URL": web_url,
                    "SPOTLAB_COMPOSE_EXPECTED_SESSION_ID": session_id,
                    "SPOTLAB_COMPOSE_EXPECTED_COMMIT": revision,
                }
            )
            run(
                [browser_node, str(ROOT / "scripts" / "compose-browser-smoke.mjs")],
                cwd=ROOT, env=browser_env, timeout=90,
            )
        finally:
            if cleanup_allowed:
                assert PROJECT_PATTERN.fullmatch(project)
                run(
                    compose + ["down", "--volumes", "--remove-orphans", "--timeout", "20"],
                    cwd=context, env=base_env, timeout=90, secret=credential,
                )
                assert not any(project_objects(project, base_env))
