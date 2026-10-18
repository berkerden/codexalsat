"""Account identity and history-loss regressions use no real credentials or network."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal as D
from threading import Barrier

import httpx
import pytest
from backend.tests.test_orders import database_url as database_url
from backend.tests.test_orders import filters
from backend.tests.test_testnet import (
    NOW,
    account,
    credentials,
    order_response,
    request,
    response_for_basics,
)
from sqlalchemy import text

from spotlab import testnet_cli
from spotlab.orders import (
    TESTNET_ACCOUNT_VENUE,
    AccountBindingError,
    ExchangeUpdate,
    OrderLifecycle,
    OrderState,
    OrderStore,
    SubmissionPrevented,
)
from spotlab.protection import ProtectionLifecycle, ProtectionRequest
from spotlab.startup_audit import audit_testnet_startup, local_ledger_marker
from spotlab.testnet import BinanceTestnetTransport
from spotlab.testnet import TestnetAPIError as APIError

UID = 12345


def evidence(store):
    with store._writer() as conn:
        return local_ledger_marker(conn)


def bound_store(database):
    store = OrderStore(database, recover_submitting=False)
    store.bind_account(TESTNET_ACCOUNT_VENUE, UID)
    return store


def wire(handler, *, key=None, clock=None):
    return BinanceTestnetTransport(
        "BTCUSDT",
        credentials=key or credentials(),
        mock_transport=httpx.MockTransport(handler),
        clock_ms=clock or (lambda: NOW),
    )


def test_binding_is_immutable_across_restart_and_competing_writers(database_url):
    first = OrderStore(database_url, recover_submitting=False)
    second = OrderStore(database_url, recover_submitting=False)
    barrier = Barrier(2)

    def bind(store, uid):
        barrier.wait(timeout=5)
        try:
            store.bind_account(TESTNET_ACCOUNT_VENUE, uid)
            return uid
        except AccountBindingError as error:
            assert error.reason_code == "account_binding_mismatch"
            return None

    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(bind, store, uid) for store, uid in [(first, UID), (second, UID + 1)]
        ]
        winners = [result for future in futures if (result := future.result()) is not None]
    assert len(winners) == 1
    first.db.dispose()
    second.db.dispose()
    restarted = OrderStore(database_url, recover_submitting=False)
    try:
        restarted.require_account_binding(TESTNET_ACCOUNT_VENUE, winners[0])
        restarted.bind_account(TESTNET_ACCOUNT_VENUE, winners[0])
        with pytest.raises(AccountBindingError):
            restarted.bind_account("production", winners[0])
    finally:
        restarted.db.dispose()


@pytest.mark.parametrize("table", ["order_intents", "protection_intents", "testnet_cycles"])
def test_historical_unbound_evidence_is_not_adopted(database_url, table):
    store = OrderStore(database_url, recover_submitting=False)
    if table == "order_intents":
        store.create_or_get(request())
        store.claim_submission(request().intent_id)
    else:
        with store._writer() as conn:
            conn.execute(text(f"CREATE TABLE {table} (evidence VARCHAR(32) PRIMARY KEY)"))
            conn.execute(text(f"INSERT INTO {table} VALUES ('preserve')"))
    with pytest.raises(AccountBindingError, match="migration_required"):
        store.bind_account(TESTNET_ACCOUNT_VENUE, UID)
    assert store.account_binding_reason() == "migration_required"
    if table == "order_intents":
        assert store.get(request().intent_id).state is OrderState.SUBMITTING
    else:
        with store.db.connect() as conn:
            assert conn.execute(text(f"SELECT evidence FROM {table}")).scalar_one() == "preserve"
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [None, True, 0, -1, "12345", 12.5, UID + 1])
async def test_wrong_or_invalid_identity_prevents_all_lifecycle_changes(database_url, uid):
    calls = []

    def handle(req):
        calls.append(req.url.path)
        if req.url.path == "/api/v3/account":
            return httpx.Response(200, json={**account(), "uid": uid})
        basic = response_for_basics(req)
        assert basic is not None
        return basic

    async with wire(handle) as transport:
        life = OrderLifecycle(database_url, transport)
        life.store.bind_account(TESTNET_ACCOUNT_VENUE, UID)
        life.store.create_or_get(request())
        life.store.claim_submission(request().intent_id)
        original = evidence(life.store)
        protection = ProtectionLifecycle(life.store, transport)
        assert life.store.get(request().intent_id).state is OrderState.SUBMITTING
        for action in [
            lambda: life.reconcile(request().intent_id),
            lambda: life.cancel(request().intent_id),
            lambda: life.submit(request(), filters()),
            lambda: protection.reconcile(request().intent_id),
            lambda: protection.submit(
                ProtectionRequest(
                    replace(request(), side="SELL", order_type="OCO", price=D("1200")), D("900")
                )
            ),
        ]:
            with pytest.raises(AccountBindingError):
                await action()
            assert evidence(life.store) == original
        assert set(calls) <= {"/api/v3/time", "/api/v3/account"}
        life.store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v3/order"),
        ("GET", "/api/v3/orderList"),
        ("GET", "/api/v3/myTrades"),
        ("GET", "/api/v3/openOrders"),
        ("POST", "/api/v3/order"),
        ("POST", "/api/v3/order/test"),
        ("POST", "/api/v3/orderList/oco"),
        ("DELETE", "/api/v3/order"),
        ("DELETE", "/api/v3/orderList"),
    ],
)
async def test_direct_routes_cannot_skip_binding_or_use_unsigned_mode(method, path):
    calls = []

    def handle(req):
        calls.append(req.url.path)
        return response_for_basics(req)

    async with wire(handle) as transport:
        for signed in (True, False):
            with pytest.raises(AccountBindingError):
                await transport.request(method, path, signed=signed)
    assert calls == []


@pytest.mark.asyncio
async def test_rotated_key_same_uid_works_but_verification_is_never_cached(database_url):
    unavailable = False
    calls = []

    def handle(req):
        calls.append(req.url.path)
        if unavailable and req.url.path == "/api/v3/account":
            raise httpx.ReadTimeout("private payload", request=req)
        return response_for_basics(req)

    store = bound_store(database_url)
    for key in [credentials(), credentials().model_copy(update={"api_key": credentials().secret})]:
        async with wire(handle, key=key) as transport:
            transport.attach_order_store(store)
            await transport.verify_account_binding()
            await transport.request("GET", "/api/v3/openOrders")
    async with wire(handle) as transport:
        transport.attach_order_store(store)
        await transport.verify_account_binding()
        unavailable = True
        before = list(calls)
        with pytest.raises(AccountBindingError, match="account_identity_unavailable"):
            await transport.request("GET", "/api/v3/openOrders")
        assert calls[len(before) :] == ["/api/v3/time", "/api/v3/account"]
    store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
@pytest.mark.parametrize("numeric_id", [False, True, "string"])
async def test_missing_history_retains_facts_and_only_acknowledged_history_latches(
    database_url, acknowledged, numeric_id
):
    store = bound_store(database_url)
    store.create_or_get(request())
    if acknowledged:
        store.apply_update(
            request().intent_id,
            ExchangeUpdate(request().client_order_id, "BTCUSDT:42", OrderState.CANCELED, D(0)),
        )
    else:
        store.mark_unknown(request().intent_id, "accepted_response_lost")
    before = store.get(request().intent_id)
    missing = True
    mutations = []

    def handle(req):
        if req.method != "GET":
            mutations.append(req.method)
            return httpx.Response(200, json={})
        if req.url.path == "/api/v3/order":
            if missing:
                return httpx.Response(400, json={"code": -2013, "msg": "private exchange text"})
            return httpx.Response(200, json=order_response(request().client_order_id))
        return response_for_basics(req)

    async with wire(handle) as transport:
        transport.attach_order_store(store)
        params = (
            {"orderId": "42" if numeric_id == "string" else 42}
            if numeric_id
            else {"origClientOrderId": request().client_order_id}
        )
        with pytest.raises(APIError):
            await transport.request("GET", "/api/v3/order", params)
        assert store.get(request().intent_id) == before
        assert store.account_binding_reason() == (
            "account_history_missing" if acknowledged else None
        )
        missing = False
        # Queries remain available even with a persistent history-loss incident.
        await transport.request("GET", "/api/v3/order", params)
    store.db.dispose()
    restarted = OrderStore(database_url, recover_submitting=False)
    async with wire(handle) as transport:
        transport.attach_order_store(restarted)
        if acknowledged:
            result = await audit_testnet_startup(restarted, transport)
            assert result.reason_codes == ("account_history_missing",)
            assert result.account_binding_verified
            with pytest.raises(AccountBindingError, match="account_history_missing"):
                await transport.request("POST", "/api/v3/orderList/oco")
        else:
            # A transient miss before acknowledgement cannot permanently bar protection.
            await transport.verify_mutation_allowed()
        assert mutations == []
    restarted.db.dispose()


@pytest.mark.asyncio
async def test_identity_lookup_cannot_extend_entry_authority(database_url):
    current = NOW
    armed = False
    mutations = []

    def handle(req):
        nonlocal current
        if req.method != "GET":
            mutations.append(req.url.path)
        if req.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": current})
        if req.url.path == "/api/v3/account" and armed:
            current += 1001
        return response_for_basics(req)

    store = bound_store(database_url)
    async with wire(handle, clock=lambda: current) as transport:
        transport.attach_order_store(store)

        async def audit():
            nonlocal armed
            result = await audit_testnet_startup(store, transport)
            armed = True
            return result.observed_at_ms, result.open_order_marker

        with pytest.raises(SubmissionPrevented):
            await transport.submit(request(), deadline_ms=NOW + 1000, entry_audit=audit)
        assert mutations == []
    store.db.dispose()


def test_bind_cli_is_local_only_redacted_and_refuses_legacy(tmp_path, monkeypatch, capsys):
    database = "sqlite:///" + str(tmp_path / "binding.sqlite")
    calls = []

    def handle(req):
        calls.append((req.method, req.url.path))
        return response_for_basics(req)

    monkeypatch.setattr(testnet_cli, "load_credentials", lambda _: credentials())
    monkeypatch.setattr(testnet_cli, "BinanceTestnetTransport", lambda *a, **k: wire(handle))
    args = ["testnet_cli", "bind-account", "--confirm-testnet", "--database", database]
    monkeypatch.setattr(sys, "argv", args)
    assert testnet_cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "bound", "production_enabled": False}
    assert set(calls) == {("GET", "/api/v3/time"), ("GET", "/api/v3/account")}
    historical = "sqlite:///" + str(tmp_path / "historical.sqlite")
    store = OrderStore(historical, recover_submitting=False)
    store.create_or_get(request())
    store.claim_submission(request().intent_id)
    before = evidence(store)
    args[-1] = historical
    assert testnet_cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "migration_required"
    assert evidence(store) == before
    args[1] = "audit"
    assert testnet_cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason_codes"] == ["migration_required"]
    assert not result["account_binding_verified"]
    assert not result["remote_snapshot_observed"]
    assert result["remote_open_order_count"] is None
    assert result["observed_at_ms"] is None
    assert evidence(store) == before
    store.db.dispose()


@pytest.mark.asyncio
async def test_transport_attachment_cannot_be_replaced_even_for_equal_database_urls():
    first = bound_store("sqlite:///:memory:")
    second = bound_store("sqlite:///:memory:")
    async with wire(response_for_basics) as transport:
        transport.attach_order_store(first)
        transport.attach_order_store(first)
        with pytest.raises(AccountBindingError, match="account_store_already_attached"):
            transport.attach_order_store(second)
    first.db.dispose()
    second.db.dispose()


@pytest.mark.parametrize("fault", ["wrong_uid", "unavailable", "auth", "malformed", "legacy"])
def test_cycle_cli_identity_failure_preserves_stop_and_order_evidence(
    tmp_path, monkeypatch, capsys, fault
):
    from backend.tests.test_testnet_cycle import plan

    from spotlab import cycle_cli
    from spotlab.testnet_cycle import TestnetCycle as Cycle

    database = "sqlite:///" + str(tmp_path / "cycle.sqlite")
    transport = wire(response_for_basics)
    cycle = Cycle(database, transport)
    if fault != "legacy":
        cycle.store.bind_account(TESTNET_ACCOUNT_VENUE, UID)
    cycle._persist(plan())
    before = evidence(cycle.store)
    with cycle.store.db.connect() as conn:
        before_cycle = tuple(conn.execute(text("SELECT * FROM testnet_cycles")).one())

    def handle(req):
        if req.url.path == "/api/v3/account":
            if fault == "unavailable":
                raise httpx.ReadTimeout("secret payload", request=req)
            if fault == "auth":
                return httpx.Response(401, json={"code": -2015, "msg": "secret payload"})
            if fault == "malformed":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={**account(), "uid": UID + (fault == "wrong_uid")})
        assert req.url.path == "/api/v3/time"
        return response_for_basics(req)

    monkeypatch.setattr(cycle_cli, "load_credentials", lambda _: credentials())
    monkeypatch.setattr(cycle_cli, "BinanceTestnetTransport", lambda *a, **k: wire(handle))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cycle_cli",
            "manage",
            "--cycle",
            plan().cycle_id,
            "--database",
            database,
            "--confirm-testnet",
            "--stop-entry",
            "--watch-seconds",
            "0",
        ],
    )
    assert cycle_cli.main() == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["reason"] in {
        "account_binding_mismatch",
        "account_identity_unavailable",
        "migration_required",
    }
    assert "secret payload" not in output
    assert evidence(cycle.store) == before
    with cycle.store.db.connect() as conn:
        assert tuple(conn.execute(text("SELECT * FROM testnet_cycles")).one()) == before_cycle
    cycle.store.db.dispose()
    import asyncio

    asyncio.run(transport.aclose())


@pytest.mark.asyncio
async def test_known_incident_prevents_local_cancel_and_protection_claims(tmp_path):
    from backend.tests.test_protection import funded_store, protection

    database = "sqlite:///" + str(tmp_path / "orders.sqlite")
    store = funded_store(tmp_path / "orders.sqlite")
    store.record_account_incident("account_history_missing")
    store.db.dispose()
    async with wire(response_for_basics) as transport:
        life = OrderLifecycle(database, transport)
        protected = ProtectionLifecycle(life.store, transport)
        before = evidence(life.store)
        with pytest.raises(AccountBindingError, match="account_history_missing"):
            await life.cancel("buy")
        with pytest.raises(AccountBindingError, match="account_history_missing"):
            await protected.submit(protection())
        assert evidence(life.store) == before
        life.store.db.dispose()


@pytest.mark.asyncio
async def test_slow_final_identity_check_releases_only_proven_unsent_oco(tmp_path):
    from backend.tests.test_protection import Venue, funded_store, protection

    store = funded_store(tmp_path / "orders.sqlite")
    venue = Venue(store)
    current = NOW
    delay_once = True
    armed = False

    def handle(req):
        nonlocal current, delay_once
        if req.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": current})
        if req.url.path == "/api/v3/account" and armed and delay_once:
            current += 5001
            delay_once = False
        return venue(req)

    async with wire(handle, clock=lambda: current) as transport:
        life = ProtectionLifecycle(store, transport)
        prepare = transport.prepare_submission

        async def prepare_then_arm(*args, **kwargs):
            nonlocal armed
            result = await prepare(*args, **kwargs)
            armed = True
            return result

        transport.prepare_submission = prepare_then_arm
        with pytest.raises(SubmissionPrevented):
            await life.submit(protection())
        assert store.get("protect").state is OrderState.INTENDED
        assert venue.posts == 0
        # Same immutable intent may proceed only because no earlier POST was attempted.
        assert (await life.submit(protection())).state is OrderState.NEW
        assert venue.posts == 1
    store.db.dispose()
