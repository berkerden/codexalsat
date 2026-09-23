"""Durable, single-writer paper ledger. This module cannot send exchange orders.

Every price is a simulated fill from a fresh observed book. Base/third-asset fees
are deliberately not guessed: this simulator charges quote-asset fees only.
"""

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from spotlab.contracts import INTERVAL_MS, Quote

D = Decimal
ZERO = D(0)


class PaperConfig(BaseModel):
    venue: str = Field(pattern="^global$")
    symbol: str = Field(pattern="^[A-Z0-9]{5,20}$")
    interval: str = Field(pattern="^(1m|3m|5m|15m|1h)$")
    strategy: str = Field(pattern="^(pullback|breakout|mean_reversion)$")
    capital: Decimal = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    risk_per_trade: Decimal = Field(gt=0, le=D("0.1"), allow_inf_nan=False)
    daily_loss_limit: Decimal = Field(gt=0, le=D("0.5"), allow_inf_nan=False)
    max_drawdown: Decimal = Field(gt=0, le=D("0.8"), allow_inf_nan=False)
    max_open_risk: Decimal = Field(gt=0, le=D("0.2"), allow_inf_nan=False)
    target_pct: Decimal = Field(gt=0, le=D("0.5"), allow_inf_nan=False)
    stop_pct: Decimal = Field(gt=0, le=D("0.5"), allow_inf_nan=False)
    fee_rate: Decimal = Field(ge=0, le=D("0.02"), allow_inf_nan=False)
    slippage_bps: Decimal = Field(ge=0, le=500, allow_inf_nan=False)
    max_spread_bps: Decimal = Field(gt=0, le=500, allow_inf_nan=False)
    hold_minutes: int = Field(ge=1, le=10_080)
    max_trades_per_day: int = Field(ge=1, le=100)
    max_consecutive_losses: int = Field(ge=1, le=20)
    authorization_minutes: int = Field(ge=1, le=1440)

    @model_validator(mode="after")
    def coherent_risk(self) -> "PaperConfig":
        if self.risk_per_trade > self.max_open_risk:
            raise ValueError("İşlem riski toplam açık risk sınırını aşamaz")
        return self


def millis() -> int:
    return int(time.time() * 1000)


def empty_state() -> dict[str, Any]:
    return {
        "mode": "suggestions",
        "entries_enabled": False,
        "config": None,
        "cash": "0",
        "equity": "0",
        "peak": "0",
        "day_start_equity": "0",
        "day": "",
        "daily_trades": 0,
        "loss_streak": 0,
        "position": None,
        "orders": [],
        "fills": [],
        "audit": [],
        "last_signal_time": 0,
        "reason": "Paper trading henüz yapılandırılmadı.",
        "expires_at": 0,
        "daily_loss": "0",
        "drawdown": "0",
        "realized_pnl": "0",
    }


class PaperEngine:
    def __init__(self, database_url: str) -> None:
        options: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.db = create_engine(database_url, **options)
        with self.db.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS bot_state "
                    "(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO bot_state (id,payload) VALUES (1,:payload) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"payload": json.dumps(empty_state())},
            )
        # A process restart never restores entry authorization. Positions remain.
        with self.transaction() as (_, state):
            state["entries_enabled"] = False
            state["reason"] = "Başlangıç uzlaştırması: yeni girişler kapalı."
            self._audit(state, "restart", "Giriş yetkisi sıfırlandı; paper kayıtları korundu")

    @contextmanager
    def transaction(self) -> Iterator[tuple[Connection, dict[str, Any]]]:
        with self.db.connect() as conn:
            if self.db.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                suffix = ""
            else:
                conn.begin()
                suffix = " FOR UPDATE"
            try:
                payload = conn.execute(text("SELECT payload FROM bot_state WHERE id=1" + suffix))
                state = json.loads(payload.scalar_one())
                yield conn, state
                conn.execute(
                    text("UPDATE bot_state SET payload=:payload WHERE id=1"),
                    {"payload": json.dumps(state)},
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def snapshot(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            value = conn.execute(text("SELECT payload FROM bot_state WHERE id=1")).scalar_one()
        state: dict[str, Any] = json.loads(value)
        state["simulation"] = True
        state["fee_assumption"] = "Komisyon kotasyon varlığında; kullanıcının paper varsayımı."
        state["daily_timezone"] = "UTC"
        return state

    @staticmethod
    def _audit(state: dict[str, Any], event: str, message: str) -> None:
        state["audit"].append({"time": millis(), "event": event, "message": message})

    def start(self, config: PaperConfig, now: int | None = None) -> dict[str, Any]:
        at = millis() if now is None else now
        with self.transaction() as (_, state):
            previous = state["config"]
            new = config.model_dump(mode="json")
            if previous and previous != new:
                raise ValueError(
                    "Mevcut paper oturumunun ayarları değiştirilemez; "
                    "yeni deney için ayrı veritabanı kullanın."
                )
            if not previous:
                state.update(
                    {
                        "cash": str(config.capital),
                        "equity": str(config.capital),
                        "peak": str(config.capital),
                        "day_start_equity": str(config.capital),
                        "day": self._day(at),
                    }
                )
            state.update(
                {
                    "config": new,
                    "entries_enabled": True,
                    "mode": "paper",
                    "expires_at": at + config.authorization_minutes * 60_000,
                    "reason": "Sanal işlem açık; gerçek piyasa verisi bekleniyor.",
                }
            )
            self._audit(state, "paper_start", "Süreli paper giriş yetkisi etkinleştirildi")
        return self.snapshot()

    def stop(self, reason: str = "Kullanıcı yeni girişleri durdurdu.") -> dict[str, Any]:
        with self.transaction() as (_, state):
            state["entries_enabled"] = False
            state["reason"] = reason
            self._audit(state, "stop_entries", reason)
        return self.snapshot()

    def request_close(self) -> dict[str, Any]:
        """Persist an exit intent before any network operation; keep retrying its remainder."""
        with self.transaction() as (_, state):
            state["entries_enabled"] = False
            if state["position"]:
                state["position"]["exit_reason"] = "manual_close"
            state["reason"] = "Paper kapatma isteği kaydedildi; güncel kotasyon bekleniyor."
            self._audit(state, "request_close", state["reason"])
        return self.snapshot()

    @staticmethod
    def _day(at: int) -> str:
        return datetime.fromtimestamp(at / 1000, UTC).date().isoformat()

    @staticmethod
    def _valid_quote(quote: Quote, now: int, symbol: str) -> bool:
        return (
            quote.symbol == symbol
            and 0 <= now - quote.observed_at <= 5000
            and quote.bid.is_finite()
            and quote.ask.is_finite()
            and quote.ask >= quote.bid > 0
            and quote.bid_quantity.is_finite()
            and quote.ask_quantity.is_finite()
            and quote.bid_quantity >= 0
            and quote.ask_quantity >= 0
        )

    def tick(
        self,
        quote: Quote,
        *,
        signal: bool,
        candle_time: int,
        step: Decimal,
        min_notional: Decimal,
        healthy: bool = True,
        now: int | None = None,
        force_close: bool = False,
    ) -> dict[str, Any]:
        at = millis() if now is None else now
        with self.transaction() as (_, state):
            if not state["config"]:
                return self.snapshot()
            cfg = PaperConfig.model_validate(state["config"])
            if not self._valid_quote(quote, at, cfg.symbol):
                state["entries_enabled"] = False
                state["reason"] = (
                    "Eski veya geçersiz veri: girişler durdu; çıkış güncel fiyat bekliyor."
                )
            else:
                self._tick_valid(
                    state,
                    cfg,
                    quote,
                    signal,
                    candle_time,
                    step,
                    min_notional,
                    healthy,
                    at,
                    force_close,
                )
        return self.snapshot()

    def _mark(self, state: dict[str, Any], cfg: PaperConfig, bid: Decimal, at: int) -> None:
        equity = D(state["cash"])
        if state["position"]:
            equity += D(state["position"]["quantity"]) * bid * (1 - cfg.fee_rate)
        # Midnight baseline is the last known marked equity, not a newly reset capital.
        day = self._day(at)
        if day != state["day"]:
            state["day_start_equity"] = state["equity"]
            state["day"] = day
            state["daily_trades"] = 0
        peak = max(equity, D(state["peak"]))
        state["equity"], state["peak"] = str(equity), str(peak)
        state["daily_loss"] = str(max(D(0), D(state["day_start_equity"]) - equity))
        state["drawdown"] = str((peak - equity) / peak if peak else D(0))

    def _tick_valid(
        self,
        state: dict[str, Any],
        cfg: PaperConfig,
        quote: Quote,
        signal: bool,
        candle_time: int,
        step: Decimal,
        min_notional: Decimal,
        healthy: bool,
        at: int,
        force_close: bool,
    ) -> None:
        self._mark(state, cfg, quote.bid, at)
        risk_hit = (
            D(state["daily_loss"]) >= cfg.capital * cfg.daily_loss_limit
            or D(state["drawdown"]) >= cfg.max_drawdown
        )
        if risk_hit:
            state["entries_enabled"] = False
            state["reason"] = "Günlük zarar veya düşüş sınırı: yeni girişler durduruldu."
        if at >= state["expires_at"]:
            state["entries_enabled"] = False
            state["reason"] = "Paper giriş yetkisinin süresi doldu; pozisyon yönetimi sürüyor."
        position = state["position"]
        if position:
            exit_reason = (
                position.get("exit_reason", "") or "manual_close"
                if force_close or position.get("exit_reason")
                else "risk_exit"
                if risk_hit
                else "stop"
                if quote.bid <= D(position["stop"])
                else "target"
                if quote.bid >= D(position["target"])
                else "timeout"
                if at >= position["expires_at"]
                else ""
            )
            if exit_reason:
                position["exit_reason"] = exit_reason
                qty = min(D(position["quantity"]), quote.bid_quantity)
                if qty > 0:
                    price = quote.bid * (1 - cfg.slippage_bps / 10_000)
                    self._sell(state, qty, price, cfg, at, exit_reason)
                else:
                    state["reason"] = "Çıkış bekliyor: en iyi alışta likidite yok."
            self._mark(state, cfg, quote.bid, at)
            return
        if not state["entries_enabled"] or force_close:
            return
        if not healthy:
            state["entries_enabled"] = False
            state["reason"] = "Veri kalite veya sembol kuralı hatası; yeni girişler durduruldu."
            return
        if quote.spread_bps > cfg.max_spread_bps:
            state["reason"] = "İŞLEM YAPMA: spread sınırı aşıldı."
            return
        if (
            state["daily_trades"] >= cfg.max_trades_per_day
            or state["loss_streak"] >= cfg.max_consecutive_losses
        ):
            state["entries_enabled"] = False
            state["reason"] = "İşlem sıklığı veya art arda kayıp sınırı."
            return
        if not signal or candle_time <= state["last_signal_time"]:
            state["reason"] = "BEKLE: yeni kapanmış mumda strateji koşulu oluşmadı."
            return
        if not (0 <= at - candle_time <= INTERVAL_MS[cfg.interval] + 5000):
            state["reason"] = "İŞLEM YAPMA: sinyal mumu güncel değil."
            return
        state["last_signal_time"] = candle_time
        if not step.is_finite() or step <= 0 or not min_notional.is_finite() or min_notional < 0:
            state["reason"] = "İŞLEM YAPMA: miktar filtreleri doğrulanamadı."
            return
        price = quote.ask * (1 + cfg.slippage_bps / 10_000)
        stop = price * (1 - cfg.stop_pct)
        adverse_stop = stop * (1 - cfg.slippage_bps / 10_000)
        loss_per_unit = price * (1 + cfg.fee_rate) - adverse_stop * (1 - cfg.fee_rate)
        risk = min(cfg.capital * cfg.risk_per_trade, cfg.capital * cfg.max_open_risk)
        qty = min(
            risk / loss_per_unit,
            D(state["cash"]) / (price * (1 + cfg.fee_rate)),
            quote.ask_quantity,
        )
        qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty <= 0 or qty * price < min_notional:
            state["reason"] = "İŞLEM YAPMA: miktar veya minimum tutar yetersiz."
            return
        target = price * (1 + cfg.target_pct)
        net_target = target * (1 - cfg.slippage_bps / 10_000) * (1 - cfg.fee_rate)
        if net_target <= price * (1 + cfg.fee_rate):
            state["reason"] = "İŞLEM YAPMA: hedef hareketi maliyetleri karşılamıyor."
            return
        cost = qty * price * (1 + cfg.fee_rate)
        state["cash"] = str(D(state["cash"]) - cost)
        state["position"] = {
            "symbol": cfg.symbol,
            "quantity": str(qty),
            "entry": str(price),
            "cost_basis": str(cost),
            "exit_pnl": "0",
            "stop": str(stop),
            "target": str(target),
            "opened_at": at,
            "expires_at": at + cfg.hold_minutes * 60_000,
            "protection": "Sanal backend çıkışı; borsada koruyucu emir yok.",
        }
        self._record_fill(state, "BUY", qty, price, qty * price * cfg.fee_rate, at, "signal")
        state["daily_trades"] += 1
        state["reason"] = "Sanal pozisyon açık; ekonomik avantaj henüz onaylanmış değildir."
        self._mark(state, cfg, quote.bid, at)

    def _sell(
        self,
        state: dict[str, Any],
        qty: Decimal,
        price: Decimal,
        cfg: PaperConfig,
        at: int,
        reason: str,
    ) -> None:
        position = state["position"]
        old_qty = D(position["quantity"])
        allocated_cost = D(position["cost_basis"]) * qty / old_qty
        fee = qty * price * cfg.fee_rate
        proceeds = qty * price - fee
        pnl = proceeds - allocated_cost
        cumulative_pnl = D(position.get("exit_pnl", "0")) + pnl
        position["exit_pnl"] = str(cumulative_pnl)
        state["cash"] = str(D(state["cash"]) + proceeds)
        state["realized_pnl"] = str(D(state["realized_pnl"]) + pnl)
        self._record_fill(state, "SELL", qty, price, fee, at, reason, pnl)
        if qty == old_qty:
            state["loss_streak"] = state["loss_streak"] + 1 if cumulative_pnl < 0 else 0
            state["position"] = None
        else:
            position["quantity"] = str(old_qty - qty)
            position["cost_basis"] = str(D(position["cost_basis"]) - allocated_cost)
        state["reason"] = "Sanal çıkış gerçekleşti: " + reason

    @staticmethod
    def _record_fill(
        state: dict[str, Any],
        side: str,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        at: int,
        reason: str,
        pnl: Decimal = ZERO,
    ) -> None:
        identity = uuid.uuid4().hex
        state["orders"].append(
            {
                "intent_id": identity,
                "side": side,
                "status": "FILLED",
                "quantity": str(qty),
                "time": at,
                "simulation": True,
            }
        )
        state["fills"].append(
            {
                "trade_id": identity,
                "side": side,
                "quantity": str(qty),
                "price": str(price),
                "fee": str(fee),
                "pnl": str(pnl),
                "time": at,
                "reason": reason,
                "simulation": True,
            }
        )

    def backup(self, destination: str) -> None:
        # Portable transactional logical backup, no credentials in the payload.
        from pathlib import Path

        with self.transaction() as (_, state):
            Path(destination).write_text(
                json.dumps({"schema": 1, "state": state}), encoding="utf-8"
            )

    def restore(self, source: str) -> None:
        from pathlib import Path

        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        if payload.get("schema") != 1 or set(payload.get("state", {})) != set(empty_state()):
            raise ValueError("Yedek şeması geçersiz")
        restored = payload["state"]
        if restored["config"]:
            PaperConfig.model_validate(restored["config"])
        with self.transaction() as (_, state):
            if state["position"] or state["entries_enabled"]:
                raise ValueError("Etkin paper oturumu üstüne geri yükleme yapılamaz")
            state.clear()
            state.update(restored)
            state["entries_enabled"] = False
            state["reason"] = "Yedek yüklendi; girişler kapalı, uzlaştırma gerekli."
            self._audit(state, "restore", "Yedek yüklendi; giriş yetkisi geri yüklenmedi")
