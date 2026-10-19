"""Local credential setup and read-only Spot Testnet diagnostics.

There is deliberately no order creation command or production routing.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import stat
from pathlib import Path
from typing import Literal, cast

from spotlab.legacy_migration import LegacyMigrationError, LegacyTestnetReader, migrate_legacy
from spotlab.orders import AccountBindingError, OrderLifecycle, OrderStore
from spotlab.protection import ProtectionLifecycle
from spotlab.startup_audit import audit_testnet_startup
from spotlab.testnet import BinanceTestnetTransport, TestnetCredentials

DEFAULT_CREDENTIAL_FILE = Path("data/testnet-credentials.json")


def configure(path: Path) -> None:
    if path.exists():
        raise ValueError("credential file already exists; remove it explicitly before replacement")
    key = getpass.getpass("Spot Testnet API key (hidden): ")
    entered_secret = getpass.getpass("Spot Testnet API secret (hidden): ")
    credentials = TestnetCredentials.model_validate({"api_key": key, "secret": entered_secret})
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids overwriting credentials or following a symlink.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(
            {
                "api_key": credentials.api_key.get_secret_value(),
                "secret": credentials.secret.get_secret_value(),
            },
            stream,
        )
    print(json.dumps({"status": "configured", "production_enabled": False}))


def load_credentials(path: Path) -> TestnetCredentials:
    if os.getenv("SPOTLAB_TESTNET_API_KEY") or os.getenv("SPOTLAB_TESTNET_API_SECRET"):
        return TestnetCredentials.from_env()
    # Refuse links and group/world-readable files. Never include contents in errors.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise ValueError("credential file must be a regular private file (mode 600)")
        payload = json.load(stream)
    return TestnetCredentials.model_validate(payload)


async def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "status":
        store = OrderStore(args.database, recover_submitting=False)
        try:
            return {
                "mode": "testnet",
                "production_enabled": False,
                "orders": [
                    {
                        "intent_id": r.request.intent_id,
                        "symbol": r.request.symbol,
                        "type": r.request.order_type,
                        "state": r.state.value,
                        "confirmed_quantity": str(r.confirmed_quantity),
                    }
                    for r in store.list_records(symbol=args.symbol)
                ],
            }
        finally:
            store.db.dispose()
    credentials = load_credentials(args.credentials)
    if args.command == "migrate-legacy":
        store = OrderStore(args.database, recover_submitting=False)
        try:
            async with LegacyTestnetReader(credentials) as reader:
                return await migrate_legacy(store, reader, apply=args.confirm_testnet)
        finally:
            store.db.dispose()
    async with BinanceTestnetTransport(
        cast(Literal["BTCUSDT", "SOLUSDT"], args.symbol), credentials=credentials
    ) as transport:
        if args.command == "bind-account":
            store = OrderStore(args.database, recover_submitting=False)
            try:
                transport.attach_order_store(store)
                await transport.bind_attached_empty_ledger()
                return {"status": "bound", "production_enabled": False}
            finally:
                store.db.dispose()
        if args.command == "audit":
            store = OrderStore(args.database, recover_submitting=False)
            try:
                return (await audit_testnet_startup(store, transport)).diagnostic()
            finally:
                store.db.dispose()
        metadata = await transport.exchange_info()
        if args.command == "preflight":
            account = await transport.account_snapshot()
            quote = await transport.quote()
            return {
                "mode": "testnet",
                "production_enabled": False,
                "symbol": metadata.symbol,
                "status": "read_only_checks_passed",
                "can_trade": account.can_trade,
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "authenticated_order_tested": False,
            }
        lifecycle = OrderLifecycle(args.database, transport)
        try:
            record = lifecycle.store.get(args.intent)
            if record.request.symbol != args.symbol:
                raise ValueError("intent symbol differs from selected symbol")
            if record.request.order_type == "OCO":
                observed = await ProtectionLifecycle(lifecycle.store, transport).reconcile(
                    args.intent
                )
                return {
                    "state": observed.order.state.value,
                    "both_legs_open_at_observation": observed.both_legs_open,
                    "observed_at_ms": observed.observed_at_ms,
                    "production_enabled": False,
                }
            record = await lifecycle.reconcile(args.intent)
            return {"state": record.state.value, "production_enabled": False}
        finally:
            lifecycle.store.db.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "configure",
            "preflight",
            "status",
            "audit",
            "reconcile",
            "bind-account",
            "migrate-legacy",
        ],
    )
    parser.add_argument("--symbol", choices=["BTCUSDT", "SOLUSDT"], default="BTCUSDT")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--database", default="sqlite:///data/testnet-orders.sqlite")
    parser.add_argument("--intent", help="persisted intent ID, required for reconcile")
    parser.add_argument("--confirm-testnet", action="store_true")
    args = parser.parse_args()
    if args.command == "bind-account" and not args.confirm_testnet:
        parser.error("bind-account requires --confirm-testnet; local binding only")
    if args.command == "reconcile" and not args.intent:
        parser.error("reconcile requires --intent")
    try:
        if args.command == "configure":
            configure(args.credentials)
        else:
            if args.database == "sqlite:///data/testnet-orders.sqlite":
                Path("data").mkdir(exist_ok=True)
            result = asyncio.run(run(args))
            print(json.dumps(result))
            if args.command == "audit" and result.get("status") == "blocked":
                return 1
    except Exception as error:
        # Pydantic/httpx/JSON exceptions can contain input values or signed URLs.
        # Never print their message, traceback, account payload or chained cause.
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": error.reason_code
                    if isinstance(error, (AccountBindingError, LegacyMigrationError))
                    else type(error).__name__,
                    "production_enabled": False,
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
