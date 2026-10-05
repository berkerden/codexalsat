"""Explicit, bounded Spot Testnet entry and protective-order management.

All funds are virtual. This tool has no production endpoint or trading strategy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import text
from sqlalchemy.exc import NoResultFound

from spotlab.testnet import BinanceTestnetTransport
from spotlab.testnet_cli import DEFAULT_CREDENTIAL_FILE, load_credentials
from spotlab.testnet_cycle import CLOSED_CYCLES, CyclePlan, TestnetCycle


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=["start", "manage"])
    result.add_argument("--cycle", required=True, help="unique durable cycle ID")
    result.add_argument("--symbol", choices=["BTCUSDT", "SOLUSDT"], default="BTCUSDT")
    result.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    result.add_argument("--database", default="sqlite:///data/testnet-orders.sqlite")
    result.add_argument(
        "--confirm-testnet", action="store_true", help="permit this bounded virtual-funds operation"
    )
    result.add_argument(
        "--stop-entry",
        action="store_true",
        help="durably request entry cancellation; retain/manage exits",
    )
    result.add_argument(
        "--monitor",
        action="store_true",
        help="continue observing installed protection until the watch deadline",
    )
    result.add_argument("--watch-seconds", type=int, default=90)
    result.add_argument("--entry-seconds", type=int, default=60)
    for field in ("quantity", "entry", "target", "stop", "max-notional"):
        result.add_argument("--" + field, type=Decimal)
    return result


async def run(args: argparse.Namespace) -> dict[str, object]:
    credentials = load_credentials(args.credentials)
    if args.database == "sqlite:///data/testnet-orders.sqlite":
        Path("data").mkdir(exist_ok=True)
    async with BinanceTestnetTransport(
        cast(Literal["BTCUSDT", "SOLUSDT"], args.symbol), credentials=credentials
    ) as transport:
        cycle = TestnetCycle(args.database, transport)
        try:
            if args.command == "start":
                # A repeat start loads the persisted deadline rather than silently
                # renewing entry authority. All supplied price/size terms must match.
                try:
                    deadline = cycle._plan(args.cycle).entry_deadline_ms
                except NoResultFound:
                    deadline = time.time_ns() // 1_000_000 + args.entry_seconds * 1000
                plan = CyclePlan(
                    args.cycle,
                    args.symbol,
                    args.quantity,
                    args.entry,
                    args.target,
                    args.stop,
                    args.max_notional,
                    deadline,
                )
                state = await cycle.start(plan)
            else:
                state = await cycle.advance(
                    args.cycle, allow_mutations=args.confirm_testnet, stop_entry=args.stop_entry
                )
            until = time.monotonic() + args.watch_seconds
            terminal = CLOSED_CYCLES | {"BLOCKED", "UNPROTECTED"}
            if not args.monitor:
                terminal.add("PROTECTED")
            while (
                args.confirm_testnet and state["state"] not in terminal and time.monotonic() < until
            ):
                print(json.dumps(state), flush=True)
                await asyncio.sleep(min(2, max(0, until - time.monotonic())))
                state = await cycle.advance(args.cycle, allow_mutations=True)
            if args.confirm_testnet and state["state"] not in terminal:
                # Bounded observation ending must not silently leave a BUY authorized.
                state = await cycle.advance(args.cycle, allow_mutations=True, stop_entry=True)
            return {
                **state,
                "manager_running": False,
                "resume_required": state["state"] not in CLOSED_CYCLES,
            }
        except BaseException:
            # Record the operator's stop even when cancellation interrupted I/O.
            # Do not issue new network requests from an interruption handler.
            with cycle.store._writer() as conn:
                conn.execute(
                    text("UPDATE testnet_cycles SET stop_entry=1 WHERE cycle_id=:id"),
                    {"id": args.cycle},
                )
            raise
        finally:
            cycle.store.db.dispose()


def main() -> int:
    arg_parser = parser()
    args = arg_parser.parse_args()
    if not 0 <= args.watch_seconds <= 600 or not 1 <= args.entry_seconds <= 600:
        arg_parser.error("watch must be 0-600 seconds; entry authority must be 1-600 seconds")
    if args.command == "start":
        if not args.confirm_testnet:
            arg_parser.error("start requires --confirm-testnet (virtual funds only)")
        if any(
            getattr(args, field) is None
            for field in ("quantity", "entry", "target", "stop", "max_notional")
        ):
            arg_parser.error("start requires quantity, entry, target, stop and max-notional")
    if args.stop_entry and not args.confirm_testnet:
        arg_parser.error("--stop-entry requires --confirm-testnet")
    try:
        state = asyncio.run(run(args))
        print(json.dumps(state))
        return 1 if state["state"] in {"BLOCKED", "UNPROTECTED"} else 0
    except (Exception, KeyboardInterrupt) as error:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": type(error).__name__,
                    "cycle_id": args.cycle,
                    "resume_required": True,
                    "production_enabled": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
