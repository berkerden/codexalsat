"""HTTP-boundary regressions for the new-entry-only audit."""

from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest
from backend.tests.test_orders import database_url as database_url
from backend.tests.test_testnet import (
    NOW,
    bind_transport,
    credentials,
    exchange_info,
    request,
    response_for_basics,
)

from spotlab.orders import (
    TESTNET_ACCOUNT_VENUE,
    OrderInvariantError,
    OrderStore,
    SubmissionPrevented,
)
from spotlab.startup_audit import audit_testnet_startup
from spotlab.testnet import BinanceTestnetTransport


def manual_order():
    return {
        "symbol": "ETHUSDT",
        "orderId": 90,
        "clientOrderId": "manual-order",
        "orderListId": -1,
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "1",
        "executedQty": "0",
        "price": "100",
        "stopPrice": "0",
        "timeInForce": "GTC",
        "status": "NEW",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,reason",
    [
        ("stale", "preflight_stale_after_audit"),
        ("drift", "preflight_clock_drift_after_audit"),
        ("capacity", "open_orders_changed_after_preflight"),
        ("deadline", "SubmissionPrevented"),
        ("stop", "SubmissionPrevented"),
    ],
)
async def test_final_audit_cannot_outlive_preflight_or_authority(database_url, fault, reason):
    current = NOW
    server_offset = 0
    open_orders = []
    audit_finished = False
    mutations = []

    def handle(req):
        if req.method != "GET":
            mutations.append(req.url.path)
            raise AssertionError("blocked entry reached exchange")
        if req.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": current + server_offset})
        if req.url.path == "/api/v3/openOrders":
            return httpx.Response(200, json=open_orders)
        basic = response_for_basics(req)
        assert basic is not None
        return basic

    async with BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handle),
        clock_ms=lambda: current,
    ) as wire:
        store = OrderStore(database_url)
        store.bind_account(TESTNET_ACCOUNT_VENUE, 12345)
        wire.attach_order_store(store)

        async def audit():
            nonlocal current, server_offset, audit_finished
            if fault == "stale":
                current += 5001
            elif fault == "drift":
                server_offset = 1001
            elif fault == "capacity":
                open_orders.append(manual_order())
            elif fault == "deadline":
                current += 1001
            result = await audit_testnet_startup(store, wire)
            assert not result.blocked
            audit_finished = True
            return result.observed_at_ms, result.open_order_marker

        def authorize():
            assert audit_finished
            if fault == "stop":
                raise SubmissionPrevented("operator stopped during audit")

        try:
            with pytest.raises(SubmissionPrevented) as error:
                await wire.submit(
                    request(),
                    entry_audit=audit,
                    before_send=authorize,
                    deadline_ms=NOW + (1000 if fault == "deadline" else 60_000),
                )
            assert error.value.reason_code == reason
            assert mutations == []
        finally:
            store.db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("orderId", True),
        ("clientOrderId", None),
        ("orderListId", -2),
        ("origQty", "NaN"),
        ("executedQty", "2"),
        ("status", "FILLED"),
        ("side", "OTHER"),
        ("price", "-1"),
        ("timeInForce", ""),
    ],
)
async def test_audit_rejects_malformed_remote_identity_without_mutation(field, value):
    raw = manual_order()
    raw[field] = value
    calls = []

    def handle(req):
        calls.append(req.method)
        if req.url.path == "/api/v3/openOrders":
            return httpx.Response(200, json=[raw])
        basic = response_for_basics(req)
        assert basic is not None
        return basic

    async with BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handle),
        clock_ms=lambda: NOW,
    ) as wire:
        bind_transport(wire)
        with pytest.raises((OrderInvariantError, ValueError)):
            await wire.account_wide_snapshot()
    assert set(calls) == {"GET"}


@pytest.mark.asyncio
async def test_pending_manual_order_does_not_block_protective_preflight():
    raw = manual_order()
    raw["status"] = "PENDING_NEW"
    raw.pop("clientOrderId")  # Audit requires identity; existing exit capacity checks do not.
    methods = []

    def handle(req):
        methods.append(req.method)
        if req.url.path == "/api/v3/openOrders":
            return httpx.Response(200, json=[raw])
        if req.url.path == "/api/v3/exchangeInfo":
            info = exchange_info()
            info["symbols"][0]["orderTypes"] += ["LIMIT_MAKER", "STOP_LOSS"]
            return httpx.Response(200, json=info)
        if req.url.path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "1000"})
        basic = response_for_basics(req)
        assert basic is not None
        return basic

    async with BinanceTestnetTransport(
        "BTCUSDT",
        credentials=credentials(),
        mock_transport=httpx.MockTransport(handle),
        clock_ms=lambda: NOW,
    ) as wire:
        bind_transport(wire)
        with pytest.raises(OrderInvariantError):
            await wire.account_wide_snapshot()
        await wire.prepare_submission(
            replace(request(), side="SELL", order_type="OCO", price=D("1100")),
            stop_price=D("900"),
        )
    assert set(methods) == {"GET"}


@pytest.mark.parametrize("failure", ["unresolved", "timeout"])
def test_audit_cli_nonzero_and_private_payloads_stay_hidden(tmp_path, monkeypatch, capsys, failure):
    import json
    import sys

    from spotlab import testnet_cli

    database = "sqlite:///" + str(tmp_path / "audit.sqlite")
    store = OrderStore(database)
    store.bind_account(TESTNET_ACCOUNT_VENUE, 12345)
    store.create_or_get(request())
    store.claim_submission(request().intent_id)
    store.db.dispose()
    methods = []

    def handle(req):
        methods.append(req.method)
        if req.url.path == "/api/v3/openOrders":
            if failure == "timeout":
                raise httpx.ReadTimeout("fixture-secret signed-url fixture-key", request=req)
            raw = manual_order()
            raw["status"] = "PENDING_NEW"
            return httpx.Response(200, json=[raw])
        basic = response_for_basics(req)
        assert basic is not None
        return basic

    monkeypatch.setattr(sys, "argv", ["testnet_cli", "audit", "--database", database])
    monkeypatch.setattr(testnet_cli, "load_credentials", lambda _: credentials())
    monkeypatch.setattr(
        testnet_cli,
        "BinanceTestnetTransport",
        lambda symbol, **kwargs: BinanceTestnetTransport(
            symbol,
            **kwargs,
            mock_transport=httpx.MockTransport(handle),
            clock_ms=lambda: NOW,
        ),
    )
    assert testnet_cli.main() == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "blocked" and result["production_enabled"] is False
    for private in [
        "fixture-secret",
        "signed-url",
        "fixture-key",
        "manual-order",
        request().client_order_id,
    ]:
        assert private not in output
    if failure == "unresolved":
        assert result["reason_codes"] == ["needs_reconciliation"]
        assert result["untracked_other_order_count"] == 1
    assert set(methods) == {"GET"}
    observed = OrderStore(database, recover_submitting=False)
    observed.bind_account(TESTNET_ACCOUNT_VENUE, 12345)
    try:
        assert observed.get(request().intent_id).state.value == "SUBMITTING"
    finally:
        observed.db.dispose()
