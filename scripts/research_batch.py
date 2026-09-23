"""Fixed exploratory comparison. Market archives and reports stay out of Git."""
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from spotlab.contracts import INTERVAL_MS
from spotlab.finance import CostModel
from spotlab.market import BinanceMarket, ParquetStore
from spotlab.research import run_research


async def main() -> None:
    root = Path("data")
    output = root / "reports"
    output.mkdir(parents=True, exist_ok=True)
    store = ParquetStore(root / "market")
    # Fixed before looking at outcomes: two symbols, five periods, nine candidates,
    # one 60-minute holding horizon, default documented assumed fees and stress.
    end = int(datetime.now(UTC).timestamp() * 1000)
    summary = []
    code_hash = hashlib.sha256(Path("backend/src/spotlab/research.py").read_bytes()).hexdigest()
    async with BinanceMarket() as market:
        for symbol in ("BTCUSDT", "SOLUSDT"):
            for interval in ("1h", "15m", "5m", "3m", "1m"):
                start = (end - 181 * 86_400_000) // INTERVAL_MS[interval] * INTERVAL_MS[interval]
                cached = store.read(symbol, interval)
                cursor = start
                if cached and cached[0].open_time <= start:
                    cursor = max(start, cached[-1].open_time + INTERVAL_MS[interval])
                print(f"Downloading {symbol} {interval}", flush=True)
                fresh = await market.candles(symbol, interval, cursor, end)
                if fresh:
                    store.upsert(symbol, interval, fresh)
                candles = store.read(symbol, interval, start_ms=start, end_ms=end)
                candles = [c for c in candles if c.close_time < end]
                report = await asyncio.to_thread(
                    run_research, candles, interval, 60, CostModel(),
                    "source-sha256:" + code_hash, comparison_trials=90)
                report.update({"symbol": symbol, "interval": interval, "live_evidence_approved": False,
                               "research_use": "Exploratory comparison; no live approval"})
                (output / f"{symbol}-{interval}.json").write_text(json.dumps(report, indent=2))
                normal = report["sealed_test"]["normal_costs"]
                item = {"symbol": symbol, "interval": interval, "data": report["data"],
                        "decision": report["decision"], "selected": report["selected_candidate"],
                        "normal": normal, "stress": report["sealed_test"]["stress_costs"]}
                summary.append(item)
                (output / "comparison.json").write_text(json.dumps(summary, indent=2))
                print(json.dumps({"symbol": symbol, "interval": interval,
                                  "candles": len(candles), "decision": report["decision"]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
