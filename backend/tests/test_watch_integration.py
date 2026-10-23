"""Exercise watcher recovery through real signed transport and both database backends."""

from decimal import Decimal as D

import httpx
import pytest
from backend.tests.test_cycle_watch import FakeTime
from backend.tests.test_orders import database_url as database_url
from backend.tests.test_testnet import NOW as EXCHANGE_TIME
from backend.tests.test_testnet import credentials
from backend.tests.test_testnet_cycle import (
    SimulatedProcessLoss,
    StatefulTestnetVenue,
    recovery_terms,
)
from sqlalchemy import create_engine, text

from spotlab.cycle_watch import WatchOptions, run_watch
from spotlab.manager_lock import ManagerBusy, ManagerLock, ManagerLockError
from spotlab.orders import TESTNET_ACCOUNT_VENUE
from spotlab.testnet import BinanceTestnetTransport
from spotlab.testnet_cycle import TestnetCycle as Cycle


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_watch_existing_entry_does_not_buy_again(database_url: str, confirmed: bool) -> None:
    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)
    async with BinanceTestnetTransport(
        "BTCUSDT", credentials=credentials(), mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: EXCHANGE_TIME,
    ) as wire:
        cycle = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
        cycle.store.bind_account(TESTNET_ACCOUNT_VENUE, 12345)
        await cycle.start(terms)
        before = list(venue.mutation_payloads)
        if confirmed:
            venue.entry_status = "PARTIALLY_FILLED"
            venue.entry_executed = D("0.4")
            venue.entry_trades = [{
                "symbol": "BTCUSDT", "orderId": 100, "id": 1, "qty": "0.4", "price": "100",
                "quoteQty": "40", "commission": "0.04", "commissionAsset": "USDT",
            }]
        timer = FakeTime()
        result = await run_watch(
            cycle, "one", WatchOptions(allow_mutations=confirmed, run_seconds=2, poll_seconds=1),
            lambda _: None, sleep=timer.sleep, monotonic=timer.monotonic,
        )
        assert venue.entry_posts == 1
        assert result["stop_entry_requested"] is confirmed
        if confirmed:
            assert result["state"] == "PROTECTED"
            assert venue.cancel_attempts == 1
            assert venue.oco_posts == 1
        else:
            assert result["state"] == "ENTRY_OPEN"
            assert venue.mutation_payloads == before
        cycle.store.db.dispose()


@pytest.mark.asyncio
async def test_watch_restart_after_accepted_oco_reply_loss_never_resends(database_url: str) -> None:
    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)
    async with BinanceTestnetTransport(
        "BTCUSDT", credentials=credentials(), mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: EXCHANGE_TIME,
    ) as wire:
        cycle = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
        cycle.store.bind_account(TESTNET_ACCOUNT_VENUE, 12345)
        await cycle.start(terms)
        venue.entry_status = "FILLED"
        venue.entry_executed = D("0.5")
        venue.entry_trades = [{
            "symbol": "BTCUSDT", "orderId": 100, "id": 1, "qty": "0.5", "price": "100",
            "quoteQty": "50", "commission": "0.05", "commissionAsset": "USDT",
        }]
        venue.drop_accepted.add(("POST", "/api/v3/orderList/oco"))
        with pytest.raises(SimulatedProcessLoss):
            await run_watch(cycle, "one", WatchOptions(allow_mutations=True), lambda _: None)
        assert cycle.status("one")["stop_entry_requested"] is True
        cycle.store.db.dispose()

    async with BinanceTestnetTransport(
        "BTCUSDT", credentials=credentials(), mock_transport=httpx.MockTransport(venue),
        clock_ms=lambda: EXCHANGE_TIME,
    ) as wire:
        restarted = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
        timer = FakeTime()
        result = await run_watch(
            restarted, "one", WatchOptions(allow_mutations=True, run_seconds=2, poll_seconds=1),
            lambda _: None, sleep=timer.sleep, monotonic=timer.monotonic,
        )
        assert result["state"] == "PROTECTED"
        assert result["stop_entry_requested"] is True
        assert venue.entry_posts == venue.oco_posts == 1
        assert len(restarted.store.list_records()) == 2
        assert venue.cancel_attempts == 0
        restarted.store.db.dispose()


def test_database_manager_contention_release_and_session_loss(database_url: str) -> None:
    engine = create_engine(database_url)
    other = create_engine(database_url)
    try:
        with ManagerLock(engine) as lock:
            lock.check_alive()
            with pytest.raises(ManagerBusy):
                with ManagerLock(other):
                    pass
            if engine.dialect.name == "postgresql":
                assert lock._connection is not None
                assert not lock._connection.in_transaction()
                with other.connect() as admin:
                    state = admin.execute(
                        text("SELECT state FROM pg_stat_activity WHERE pid=:pid"),
                        {"pid": lock._backend_pid},
                    ).scalar_one()
                    assert state == "idle"
                    assert admin.execute(
                        text("SELECT pg_terminate_backend(:pid)"), {"pid": lock._backend_pid}
                    ).scalar_one()
                    admin.commit()
                with pytest.raises(ManagerLockError, match="manager_lock_lost"):
                    lock.check_alive()
        with ManagerLock(other) as successor:
            successor.check_alive()
    finally:
        engine.dispose()
        other.dispose()


def test_unknown_postgres_acquisition_discards_session() -> None:
    class InterruptedConnection:
        invalidated = False
        closed = False

        def execute(self, *_args):
            raise TimeoutError("reply lost after server acquired lock")

        def invalidate(self):
            self.invalidated = True

        def close(self):
            self.closed = True

    class Engine:
        connection = InterruptedConnection()

        def connect(self):
            return self.connection

    engine = Engine()
    with pytest.raises(TimeoutError):
        ManagerLock(engine)._acquire_postgresql()
    assert engine.connection.invalidated and engine.connection.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_legacy_cli_busy_manager_cannot_write_stop_or_access_exchange(
    database_url: str, confirmed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spotlab import cycle_cli

    terms = recovery_terms()
    venue = StatefulTestnetVenue(terms)

    def transport(symbol, **_kwargs):
        return BinanceTestnetTransport(
            symbol, credentials=credentials(), mock_transport=httpx.MockTransport(venue),
            clock_ms=lambda: EXCHANGE_TIME,
        )

    monkeypatch.setattr(cycle_cli, "load_credentials", lambda _: credentials())
    monkeypatch.setattr(cycle_cli, "BinanceTestnetTransport", transport)
    async with transport("BTCUSDT") as wire:
        cycle = Cycle(database_url, wire, clock_ms=lambda: EXCHANGE_TIME)
        cycle.store.bind_account(TESTNET_ACCOUNT_VENUE, 12345)
        await cycle.start(terms)
        before = list(venue.mutation_payloads)
        args = cycle_cli.parser().parse_args([
            "manage", "--cycle", "one", "--database", database_url,
            *(["--confirm-testnet"] if confirmed else []),
        ])
        with cycle.exclusive_management():
            with pytest.raises(ManagerBusy):
                await cycle_cli.run(args)
        assert cycle.status("one")["stop_entry_requested"] is False
        assert venue.mutation_payloads == before
        cycle.store.db.dispose()
