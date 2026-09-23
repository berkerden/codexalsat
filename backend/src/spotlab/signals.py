"""Explain a computed scenario without equating target profit with expectation."""

from decimal import Decimal
from typing import Any

from spotlab.contracts import Candle, Quote
from spotlab.finance import CostModel, margin, position_size
from spotlab.research import strategy_signal

D = Decimal


def signal_card(
    quote: Quote,
    candles: list[Candle],
    healthy: bool,
    paper: dict[str, Any],
    report: dict[str, Any] | None,
    step: Decimal,
    minimum: Decimal,
) -> dict[str, Any]:
    cfg = paper.get("config")
    if cfg and cfg["symbol"] != quote.symbol:
        cfg = None
    selected = report.get("selected_candidate") if report else None
    card: dict[str, Any] = {
        "action": "İŞLEM YAPMA",
        "reason": "Örneklem dışı avantaj doğrulanmadı.",
        "data_time": quote.observed_at,
        "valid_until": quote.observed_at + 5000,
        "entry": str(quote.ask),
        "margin": None,
        "quantity": None,
        "expected_net_result": None,
        "evidence": "Hedef kârı beklenen kâr değildir; canlı işlem yetkisi verilmez.",
    }
    if not cfg and not selected:
        card["reason"] = "Önce araştırmayı çalıştırın veya sanal işlem senaryosunu yapılandırın."
        return card
    if cfg:
        strategy, target_pct, stop_pct = cfg["strategy"], D(cfg["target_pct"]), D(cfg["stop_pct"])
        hold = cfg["hold_minutes"]
        costs = CostModel(
            entry_fee=D(cfg["fee_rate"]),
            exit_fee=D(cfg["fee_rate"]),
            spread_bps=quote.spread_bps,
            slippage_bps=D(cfg["slippage_bps"]),
        )
    else:
        assert report is not None and selected is not None
        strategy = selected["strategy"]
        target_pct, stop_pct = D(selected["target_pct"]), D(selected["stop_pct"])
        hold = report["parameters"]["hold_minutes"]
        raw = report["parameters"]["costs"]
        costs = CostModel(
            entry_fee=D(raw["entry_fee"]),
            exit_fee=D(raw["exit_fee"]),
            spread_bps=quote.spread_bps,
            slippage_bps=D(raw["slippage_bps"]),
        )
    mid = (quote.bid + quote.ask) / 2
    target, stop = mid * (1 + target_pct), mid * (1 - stop_pct)
    quantity = D(1)
    if cfg:
        available = min(D(paper["cash"]), D(cfg["capital"]))
        if available > 0:
            sized = position_size(
                available,
                D(cfg["risk_per_trade"]),
                mid,
                stop,
                costs,
                step=step,
                min_notional=minimum,
            )
            quantity = D(sized["quantity"])
            card["quantity"] = str(quantity)
    if quantity > 0:
        card["margin"] = margin(mid, target, stop, quantity, costs)
    card.update(
        {
            "strategy": strategy,
            "target": target,
            "stop": stop,
            "holding_minutes": hold,
            "costs": costs.as_dict(),
            "quantity_basis": "Paper risk bütçesi"
            if cfg
            else "1 coin için birim hesap; miktar önerisi değil",
            "scenario_only": True,
            "invalidation": "5 saniyede süre aşımı, veri/strateji değişimi",
        }
    )
    signals = strategy_signal(candles, strategy)
    if healthy and signals and not signals[-1]:
        card["action"] = "BEKLE"
        card["reason"] = "Kapanmış mumda strateji giriş koşulu oluşmadı."
    if not healthy:
        card["action"], card["reason"] = "İŞLEM YAPMA", "Veri sağlığı uygun değil."
    return card
