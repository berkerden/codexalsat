"""Durable, single-writer paper ledger. This module cannot send exchange orders.

Every price is a simulated fill from a fresh observed book. Base/third-asset fees
are deliberately not guessed: this simulator charges quote-asset fees only.
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from spotlab.contracts import INTERVAL_MS, Quote

D = Decimal
ZERO = D(0)


class PaperConflictError(ValueError):
    """The requested mutation no longer applies to the current paper session."""


class PaperArchiveNotFoundError(LookupError):
    """The requested immutable paper archive does not exist."""


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
        "session_id": str(uuid.uuid4()),
        "started_at": None,
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
        "observation": None,
    }


PRE_OBSERVATION_STATE_KEYS = frozenset(
    {
        "session_id",
        "started_at",
        "mode",
        "entries_enabled",
        "config",
        "cash",
        "equity",
        "peak",
        "day_start_equity",
        "day",
        "daily_trades",
        "loss_streak",
        "position",
        "orders",
        "fills",
        "audit",
        "last_signal_time",
        "reason",
        "expires_at",
        "daily_loss",
        "drawdown",
        "realized_pnl",
    }
)
LEGACY_STATE_KEYS = PRE_OBSERVATION_STATE_KEYS - {"session_id", "started_at"}
STATE_KEYS = frozenset(empty_state())

OBSERVATION_SCOPES = frozenset({"entry", "management"})
OBSERVATION_STATUSES = frozenset({"healthy", "unhealthy"})
OBSERVATION_REASONS = frozenset({"ok", "invalid_quote", "entry_data_unhealthy", "market_error"})
OBSERVATION_COUNT_KEYS = frozenset({"attempts", "healthy_entry", "healthy_management", "unhealthy"})
OBSERVATION_COVERAGE_KEYS = frozenset({"entry", "management"})
OBSERVATION_INTERRUPTION_KEYS = frozenset(
    {"restart", "stop", "restore", "code_change", "clock_regression"}
)
OBSERVATION_KEYS = frozenset(
    {
        "schema_version",
        "config_sha256",
        "first_observed_at",
        "last_observed_at",
        "first_code_identity",
        "last_code_identity",
        "mixed_code_identity",
        "process_count",
        "source_session_id",
        "counts",
        "coverage_ms",
        "gap_count",
        "max_gap_ms",
        "interruptions",
        "last_status",
        "last_scope",
        "last_reason",
        "continuity",
        "last_process_id",
    }
)
OBSERVATION_CONTINUITY_KEYS = frozenset({"process_id", "at", "scope", "healthy"})
SOURCE_FILES = (
    "engine.py",
    "main.py",
    "contracts.py",
    "finance.py",
    "market.py",
    "research.py",
    "signals.py",
)


class PaperEngine:
    def __init__(self, database_url: str) -> None:
        self._process_id = uuid.uuid4().hex
        self._code_identity = self._source_identity()
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
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS paper_archives "
                    "(session_id TEXT PRIMARY KEY, archived_at BIGINT NOT NULL, "
                    "payload TEXT NOT NULL)"
                )
            )
        # A process restart never restores entry authorization. Positions remain.
        with self.transaction() as (_, state):
            legacy = "session_id" not in state
            state.setdefault("session_id", str(uuid.uuid4()))
            state.setdefault("started_at", self._legacy_started_at(state))
            state.setdefault("observation", None)
            state["entries_enabled"] = False
            state["reason"] = "Başlangıç uzlaştırması: yeni girişler kapalı."
            if not legacy:
                self._interrupt_observation(state, "restart")
                self._audit(state, "restart", "Giriş yetkisi sıfırlandı; paper kayıtları korundu")

    @staticmethod
    def _source_identity() -> str:
        root = os.path.dirname(__file__)
        digest = hashlib.sha256()
        for name in SOURCE_FILES:
            path = os.path.join(root, name)
            digest.update(name.encode("utf-8"))
            try:
                with open(path, "rb") as source:
                    digest.update(source.read())
            except OSError:
                digest.update(b"<unavailable>")
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            commit = "unavailable"
        return f"source:{digest.hexdigest()};commit:{commit}"

    @staticmethod
    def _config_sha256(config: dict[str, Any]) -> str:
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _interrupt_observation(state: dict[str, Any], event: str) -> None:
        observation = state.get("observation")
        if observation is None:
            return
        observation["interruptions"][event] += 1
        observation["continuity"] = None

    @staticmethod
    def _legacy_started_at(state: dict[str, Any]) -> int | None:
        for event in state.get("audit", []):
            if (
                isinstance(event, dict)
                and event.get("event") == "paper_start"
                and isinstance(event.get("time"), int)
                and not isinstance(event.get("time"), bool)
                and event["time"] >= 0
            ):
                return int(event["time"])
        return None

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
        return self._public_state(state)

    @staticmethod
    def _public_state(state: dict[str, Any]) -> dict[str, Any]:
        state = json.loads(json.dumps(state))
        if state.get("observation") is not None:
            state["observation"].pop("continuity", None)
            state["observation"].pop("last_process_id", None)
        state["simulation"] = True
        state["fee_assumption"] = "Komisyon kotasyon varlığında; kullanıcının paper varsayımı."
        state["daily_timezone"] = "UTC"
        return state

    @classmethod
    def _capture(cls, state: dict[str, Any]) -> dict[str, Any]:
        return cls._public_state(json.loads(json.dumps(state)))

    @staticmethod
    def _expect_session(state: dict[str, Any], expected_session_id: str | None) -> None:
        if expected_session_id is not None and state["session_id"] != expected_session_id:
            raise PaperConflictError("Paper oturumu değişti; işlem uygulanmadı.")

    def assert_current_session(self, expected_session_id: str) -> dict[str, Any]:
        state = self.snapshot()
        self._expect_session(state, expected_session_id)
        return state

    @staticmethod
    def _audit(state: dict[str, Any], event: str, message: str) -> None:
        state["audit"].append({"time": millis(), "event": event, "message": message})

    def start(
        self,
        config: PaperConfig,
        now: int | None = None,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        at = millis() if now is None else now
        with self.transaction() as (_, state):
            self._expect_session(state, expected_session_id)
            previous = state["config"]
            new = config.model_dump(mode="json")
            if previous and previous != new:
                raise PaperConflictError(
                    "Mevcut paper oturumunun ayarları değiştirilemez; önce güvenle arşivleyin."
                )
            if not previous:
                state.update(
                    {
                        "started_at": at,
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
            if state.get("observation") is not None:
                state["observation"]["continuity"] = None
            self._audit(state, "paper_start", "Süreli paper giriş yetkisi etkinleştirildi")
            result = self._capture(state)
        return result

    def stop(
        self,
        reason: str = "Kullanıcı yeni girişleri durdurdu.",
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as (_, state):
            self._expect_session(state, expected_session_id)
            state["entries_enabled"] = False
            state["reason"] = reason
            self._interrupt_observation(state, "stop")
            self._audit(state, "stop_entries", reason)
            result = self._capture(state)
        return result

    def record_worker_failure(
        self,
        scope: str,
        reason: str = "Veri bağlantısı/kalitesi bozuldu; girişler durduruldu.",
        *,
        expected_session_id: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Atomically record a worker failure and revoke new-entry authority."""
        if scope not in OBSERVATION_SCOPES:
            raise ValueError("Paper gözlem kapsamı geçersiz")
        at = millis() if now is None else now
        with self.transaction() as (_, state):
            self._expect_session(state, expected_session_id)
            if not state["config"]:
                raise PaperConflictError("Yapılandırılmamış paper oturumu gözlemlenemez.")
            state["entries_enabled"] = False
            state["reason"] = reason
            self._record_observation(state, at, scope, False, "market_error")
            self._interrupt_observation(state, "stop")
            self._audit(state, "stop_entries", reason)
            result = self._capture(state)
        return result

    def request_close(self, *, expected_session_id: str | None = None) -> dict[str, Any]:
        """Persist an exit intent before any network operation; keep retrying its remainder."""
        with self.transaction() as (_, state):
            self._expect_session(state, expected_session_id)
            state["entries_enabled"] = False
            if state["position"]:
                state["position"]["exit_reason"] = "manual_close"
            self._interrupt_observation(state, "stop")
            state["reason"] = "Paper kapatma isteği kaydedildi; güncel kotasyon bekleniyor."
            self._audit(state, "request_close", state["reason"])
            result = self._capture(state)
        return result

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
        expected_session_id: str | None = None,
        observation_scope: str | None = None,
    ) -> dict[str, Any]:
        if observation_scope is not None and observation_scope not in OBSERVATION_SCOPES:
            raise ValueError("Paper gözlem kapsamı geçersiz")
        at = millis() if now is None else now
        with self.transaction() as (_, state):
            self._expect_session(state, expected_session_id)
            if not state["config"]:
                result = self._capture(state)
            else:
                cfg = PaperConfig.model_validate(state["config"])
                valid_quote = self._valid_quote(quote, at, cfg.symbol)
                valid_filters = (
                    step.is_finite() and step > 0 and min_notional.is_finite() and min_notional >= 0
                )
                current_candle = (
                    candle_time > 0 and 0 <= at - candle_time <= INTERVAL_MS[cfg.interval] + 5000
                )
                if not valid_quote:
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
                        healthy and (observation_scope != "entry" or valid_filters),
                        at,
                        force_close,
                    )
                if observation_scope is not None:
                    observation_healthy = valid_quote
                    observation_reason = "ok" if valid_quote else "invalid_quote"
                    if (
                        valid_quote
                        and observation_scope == "entry"
                        and (not healthy or not valid_filters or not current_candle)
                    ):
                        observation_healthy = False
                        observation_reason = "entry_data_unhealthy"
                    self._record_observation(
                        state,
                        at,
                        observation_scope,
                        observation_healthy,
                        observation_reason,
                    )
                result = self._capture(state)
            return result

    def _record_observation(
        self,
        state: dict[str, Any],
        at: int,
        scope: str,
        healthy: bool,
        reason: str,
    ) -> None:
        if type(at) is not int or at < 0:
            raise ValueError("Paper gözlem zamanı geçersiz")
        if scope not in OBSERVATION_SCOPES or reason not in OBSERVATION_REASONS:
            raise ValueError("Paper gözlem sınıflandırması geçersiz")
        status = "healthy" if healthy else "unhealthy"
        observation = state.get("observation")
        if observation is None:
            observation = {
                "schema_version": 1,
                "config_sha256": self._config_sha256(state["config"]),
                "first_observed_at": at,
                "last_observed_at": at,
                "first_code_identity": self._code_identity,
                "last_code_identity": self._code_identity,
                "mixed_code_identity": False,
                "process_count": 1,
                "source_session_id": None,
                "counts": {
                    "attempts": 0,
                    "healthy_entry": 0,
                    "healthy_management": 0,
                    "unhealthy": 0,
                },
                "coverage_ms": {"entry": 0, "management": 0},
                "gap_count": 0,
                "max_gap_ms": 0,
                "interruptions": {
                    "restart": 0,
                    "stop": 0,
                    "restore": 0,
                    "code_change": 0,
                    "clock_regression": 0,
                },
                "last_status": status,
                "last_scope": scope,
                "last_reason": reason,
                "continuity": None,
                "last_process_id": self._process_id,
            }
            state["observation"] = observation
        elif observation["last_process_id"] != self._process_id:
            observation["process_count"] += 1
            observation["last_process_id"] = self._process_id
            observation["continuity"] = None

        counts = observation["counts"]
        counts["attempts"] += 1
        counts[f"healthy_{scope}" if healthy else "unhealthy"] += 1
        observation["last_status"] = status
        observation["last_scope"] = scope
        observation["last_reason"] = reason

        if observation["last_code_identity"] != self._code_identity:
            observation["last_code_identity"] = self._code_identity
            observation["mixed_code_identity"] = True
            observation["interruptions"]["code_change"] += 1
            observation["continuity"] = None

        high_water = observation["last_observed_at"]
        if at <= high_water and counts["attempts"] > 1:
            observation["interruptions"]["clock_regression"] += 1
            observation["continuity"] = None
            return

        delta_from_high_water = at - high_water
        if counts["attempts"] > 1 and delta_from_high_water > 10_000:
            observation["gap_count"] += 1
            observation["max_gap_ms"] = max(observation["max_gap_ms"], delta_from_high_water)
        observation["last_observed_at"] = max(high_water, at)
        previous = observation["continuity"]
        if (
            healthy
            and previous is not None
            and previous["healthy"] is True
            and previous["scope"] == scope
            and previous["process_id"] == self._process_id
        ):
            delta = at - previous["at"]
            if 0 < delta <= 10_000:
                observation["coverage_ms"][scope] += delta
        observation["continuity"] = {
            "process_id": self._process_id,
            "at": at,
            "scope": scope,
            "healthy": healthy,
        }

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

    @staticmethod
    def _orders_resolved(state: dict[str, Any]) -> bool:
        orders = state.get("orders")
        return isinstance(orders, list) and all(
            isinstance(order, dict) and order.get("status") == "FILLED" for order in orders
        )

    @classmethod
    def _validate_state(
        cls,
        candidate: Any,
        *,
        legacy: bool = False,
        pre_observation: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            raise ValueError("Yedek durumu geçersiz")
        required = (
            LEGACY_STATE_KEYS
            if legacy
            else PRE_OBSERVATION_STATE_KEYS
            if pre_observation
            else STATE_KEYS
        )
        if set(candidate) != required:
            raise ValueError("Yedek şeması geçersiz")
        state = dict(candidate)
        if legacy or pre_observation:
            state["observation"] = None
        if not legacy:
            try:
                parsed = uuid.UUID(state["session_id"])
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("Paper oturum kimliği geçersiz") from exc
            if str(parsed) != state["session_id"]:
                raise ValueError("Paper oturum kimliği geçersiz")
            started_at = state["started_at"]
            if started_at is not None and (
                not isinstance(started_at, int) or isinstance(started_at, bool) or started_at < 0
            ):
                raise ValueError("Paper başlangıç zamanı geçersiz")

        def amount(value: Any, *, positive: bool = False, signed: bool = False) -> Decimal:
            if not isinstance(value, str):
                raise ValueError("Paper parasal değerleri ondalık metin olmalıdır")
            try:
                parsed_amount = D(value)
            except InvalidOperation as exc:
                raise ValueError("Paper parasal alanı geçersiz") from exc
            if not parsed_amount.is_finite() or (not signed and parsed_amount < 0):
                raise ValueError("Paper parasal alanı geçersiz")
            if positive and parsed_amount <= 0:
                raise ValueError("Paper miktarı/fiyatı pozitif olmalıdır")
            return parsed_amount

        def integer(value: Any) -> None:
            if type(value) is not int or value < 0:
                raise ValueError("Paper sayaç/zaman alanı geçersiz")

        def record(value: Any, fields: set[str], optional: set[str] | None = None) -> None:
            if not isinstance(value, dict) or not fields <= value.keys():
                raise ValueError("Paper kayıt alanları eksik")
            if value.keys() - fields - (optional or set()):
                raise ValueError("Paper kayıt alanları geçersiz")

        if type(state["entries_enabled"]) is not bool or not isinstance(state["reason"], str):
            raise ValueError("Paper giriş/açıklama durumu geçersiz")
        if state["mode"] not in ("paper", "suggestions"):
            raise ValueError("Paper çalışma kipi geçersiz")
        for key in ("daily_trades", "loss_streak", "last_signal_time", "expires_at"):
            integer(state[key])
        if not isinstance(state["day"], str):
            raise ValueError("Paper UTC günü geçersiz")
        if state["day"]:
            try:
                if datetime.strptime(state["day"], "%Y-%m-%d").date().isoformat() != state["day"]:
                    raise ValueError("Paper UTC günü geçersiz")
            except ValueError as exc:
                raise ValueError("Paper UTC günü geçersiz") from exc
        cfg = None
        if state["config"] is not None:
            record(state["config"], set(PaperConfig.model_fields))
            for key, field in PaperConfig.model_fields.items():
                if field.annotation is Decimal:
                    amount(state["config"][key])
                elif field.annotation is int:
                    integer(state["config"][key])
            cfg = PaperConfig.model_validate(state["config"])
            if state["mode"] != "paper" or not state["day"]:
                raise ValueError("Yapılandırılmış paper durumu tutarsız")
        elif state["position"] is not None or state["entries_enabled"]:
            raise ValueError("Yapılandırmasız pozisyon/yetki geri yüklenemez")
        observation = state["observation"]
        if observation is not None:
            if cfg is None:
                raise ValueError("Yapılandırmasız paper gözlemi olamaz")
            cls._validate_observation(observation, cls._config_sha256(state["config"]))
        for name in ("orders", "fills", "audit"):
            if not isinstance(state[name], list):
                raise ValueError("Paper geçmişi geçersiz")
        for name in ("cash", "equity", "peak", "day_start_equity", "daily_loss", "drawdown"):
            amount(state[name])
        if amount(state["drawdown"]) > 1:
            raise ValueError("Paper düşüş oranı geçersiz")
        amount(state["realized_pnl"], signed=True)
        for event in state["audit"]:
            record(event, {"time", "event", "message"})
            integer(event["time"])
            if not isinstance(event["event"], str) or not isinstance(event["message"], str):
                raise ValueError("Paper denetim kaydı geçersiz")
        orders = {}
        for order in state["orders"]:
            record(order, {"intent_id", "side", "status", "quantity", "time", "simulation"})
            identity = order["intent_id"]
            if not isinstance(identity, str) or not identity or identity in orders:
                raise ValueError("Paper emir kimliği geçersiz/yinelenmiş")
            if order["side"] not in ("BUY", "SELL") or order["status"] != "FILLED":
                raise ValueError("Paper emir durumu geçersiz")
            if order["simulation"] is not True:
                raise ValueError("Paper kaydı sanal olmalıdır")
            integer(order["time"])
            amount(order["quantity"], positive=True)
            orders[identity] = order
        seen_fills: set[str] = set()
        for fill in state["fills"]:
            record(
                fill,
                {
                    "trade_id",
                    "side",
                    "quantity",
                    "price",
                    "fee",
                    "pnl",
                    "time",
                    "reason",
                    "simulation",
                },
            )
            identity = fill["trade_id"]
            if not isinstance(identity, str) or identity not in orders or identity in seen_fills:
                raise ValueError("Paper dolum kimliği geçersiz/yinelenmiş")
            if fill["simulation"] is not True or not isinstance(fill["reason"], str):
                raise ValueError("Paper dolum kaydı geçersiz")
            integer(fill["time"])
            amount(fill["quantity"], positive=True)
            amount(fill["price"], positive=True)
            amount(fill["fee"])
            amount(fill["pnl"], signed=True)
            if any(fill[key] != orders[identity][key] for key in ("side", "quantity", "time")):
                raise ValueError("Paper emir/dolum kaydı uyuşmuyor")
            seen_fills.add(identity)
        if seen_fills != orders.keys():
            raise ValueError("Paper dolum kaydı eksik")
        if cfg is None:
            zero_fields = (
                "cash",
                "equity",
                "peak",
                "day_start_equity",
                "daily_loss",
                "drawdown",
                "realized_pnl",
            )
            counters = ("daily_trades", "loss_streak", "last_signal_time", "expires_at")
            if (
                any(D(state[key]) != 0 for key in zero_fields)
                or any(state[key] != 0 for key in counters)
                or state["orders"]
                or state["fills"]
                or state["day"]
                or state["mode"] != "suggestions"
                or (not legacy and state["started_at"] is not None)
            ):
                raise ValueError("Yapılandırmasız paper durumu boş olmalıdır")
        position = state["position"]
        if position is not None:
            record(
                position,
                {
                    "symbol",
                    "quantity",
                    "entry",
                    "cost_basis",
                    "stop",
                    "target",
                    "opened_at",
                    "expires_at",
                    "protection",
                },
                {"exit_pnl", "exit_reason"},
            )
            if cfg is None or position["symbol"] != cfg.symbol:
                raise ValueError("Paper pozisyon kapsamı uyuşmuyor")
            for key in ("quantity", "entry", "stop", "target"):
                amount(position[key], positive=True)
            amount(position["cost_basis"])
            if "exit_pnl" in position:
                amount(position["exit_pnl"], signed=True)
            for key in ("opened_at", "expires_at"):
                integer(position[key])
            if position["expires_at"] < position["opened_at"]:
                raise ValueError("Paper pozisyon süresi geçersiz")
            if not D(position["stop"]) < D(position["entry"]) < D(position["target"]):
                raise ValueError("Paper pozisyon seviyeleri geçersiz")
            if not isinstance(position["protection"], str) or not isinstance(
                position.get("exit_reason", ""), str
            ):
                raise ValueError("Paper pozisyon açıklaması geçersiz")
        return state

    @classmethod
    def _validate_observation(cls, candidate: Any, config_sha256: str) -> None:
        if not isinstance(candidate, dict) or set(candidate) != OBSERVATION_KEYS:
            raise ValueError("Paper gözlem şeması geçersiz")
        if (
            type(candidate["schema_version"]) is not int
            or candidate["schema_version"] != 1
            or candidate["config_sha256"] != config_sha256
        ):
            raise ValueError("Paper gözlem yapılandırması geçersiz")

        def integer(value: Any, *, positive: bool = False) -> None:
            if type(value) is not int or value < (1 if positive else 0):
                raise ValueError("Paper gözlem sayacı/zamanı geçersiz")

        integer(candidate["first_observed_at"])
        integer(candidate["last_observed_at"])
        if candidate["last_observed_at"] < candidate["first_observed_at"]:
            raise ValueError("Paper gözlem zaman aralığı geçersiz")
        for key in ("first_code_identity", "last_code_identity"):
            if not isinstance(candidate[key], str) or not candidate[key]:
                raise ValueError("Paper kaynak kimliği geçersiz")
        if type(candidate["mixed_code_identity"]) is not bool:
            raise ValueError("Paper kaynak karışımı geçersiz")
        if (
            not candidate["mixed_code_identity"]
            and candidate["first_code_identity"] != candidate["last_code_identity"]
        ):
            raise ValueError("Paper kaynak kimliği tutarsız")
        integer(candidate["process_count"], positive=True)
        if not isinstance(candidate["last_process_id"], str) or not candidate["last_process_id"]:
            raise ValueError("Paper gözlem süreç kimliği geçersiz")
        source_session_id = candidate["source_session_id"]
        if source_session_id is not None:
            try:
                parsed = uuid.UUID(source_session_id)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("Paper gözlem kaynak oturumu geçersiz") from exc
            if str(parsed) != source_session_id:
                raise ValueError("Paper gözlem kaynak oturumu geçersiz")

        counts = candidate["counts"]
        if not isinstance(counts, dict) or set(counts) != OBSERVATION_COUNT_KEYS:
            raise ValueError("Paper gözlem sayaçları geçersiz")
        for value in counts.values():
            integer(value)
        if (
            counts["attempts"]
            != (counts["healthy_entry"] + counts["healthy_management"] + counts["unhealthy"])
            or counts["attempts"] < 1
        ):
            raise ValueError("Paper gözlem sayaçları tutarsız")
        if candidate["process_count"] > counts["attempts"]:
            raise ValueError("Paper gözlem süreç sayacı tutarsız")

        coverage = candidate["coverage_ms"]
        if not isinstance(coverage, dict) or set(coverage) != OBSERVATION_COVERAGE_KEYS:
            raise ValueError("Paper gözlem kapsam süresi geçersiz")
        for value in coverage.values():
            integer(value)
        span = candidate["last_observed_at"] - candidate["first_observed_at"]
        if (
            sum(coverage.values()) > span
            or coverage["entry"] > max(0, counts["healthy_entry"] - 1) * 10_000
            or coverage["management"] > max(0, counts["healthy_management"] - 1) * 10_000
        ):
            raise ValueError("Paper gözlem kapsam süresi tutarsız")
        integer(candidate["gap_count"])
        integer(candidate["max_gap_ms"])
        if (candidate["gap_count"] == 0) != (candidate["max_gap_ms"] == 0) or (
            candidate["gap_count"] > 0 and candidate["max_gap_ms"] <= 10_000
        ):
            raise ValueError("Paper gözlem boşluk özeti tutarsız")

        interruptions = candidate["interruptions"]
        if (
            not isinstance(interruptions, dict)
            or set(interruptions) != OBSERVATION_INTERRUPTION_KEYS
        ):
            raise ValueError("Paper gözlem kesinti sayaçları geçersiz")
        for value in interruptions.values():
            integer(value)
        if candidate["last_status"] not in OBSERVATION_STATUSES:
            raise ValueError("Paper gözlem durumu geçersiz")
        if candidate["last_scope"] not in OBSERVATION_SCOPES:
            raise ValueError("Paper gözlem kapsamı geçersiz")
        if candidate["last_reason"] not in OBSERVATION_REASONS:
            raise ValueError("Paper gözlem nedeni geçersiz")
        if (candidate["last_status"] == "healthy") != (candidate["last_reason"] == "ok"):
            raise ValueError("Paper gözlem durumu/nedeni tutarsız")

        continuity = candidate["continuity"]
        if continuity is not None:
            if not isinstance(continuity, dict) or set(continuity) != OBSERVATION_CONTINUITY_KEYS:
                raise ValueError("Paper gözlem sürekliliği geçersiz")
            if not isinstance(continuity["process_id"], str) or not continuity["process_id"]:
                raise ValueError("Paper gözlem süreç kimliği geçersiz")
            integer(continuity["at"])
            if continuity["at"] != candidate["last_observed_at"]:
                raise ValueError("Paper gözlem süreklilik zamanı geçersiz")
            if continuity["scope"] not in OBSERVATION_SCOPES:
                raise ValueError("Paper gözlem süreklilik kapsamı geçersiz")
            if type(continuity["healthy"]) is not bool:
                raise ValueError("Paper gözlem süreklilik sağlığı geçersiz")

    @classmethod
    def _validate_archive(
        cls, candidate: Any, *, pre_observation: bool = False
    ) -> tuple[str, int, dict[str, Any]]:
        if not isinstance(candidate, dict) or set(candidate) != {
            "session_id",
            "archived_at",
            "state",
        }:
            raise ValueError("Arşiv yedeği geçersiz")
        state = cls._validate_state(candidate["state"], pre_observation=pre_observation)
        session_id = candidate["session_id"]
        archived_at = candidate["archived_at"]
        if session_id != state["session_id"]:
            raise ValueError("Arşiv oturum kimliği uyuşmuyor")
        if not isinstance(archived_at, int) or isinstance(archived_at, bool) or archived_at < 0:
            raise ValueError("Arşiv zamanı geçersiz")
        if (
            not state["config"]
            or state["entries_enabled"]
            or state["position"] is not None
            or not cls._orders_resolved(state)
        ):
            raise ValueError("Etkin veya tamamlanmamış durum arşiv geçmişi olamaz")
        return session_id, archived_at, state

    @staticmethod
    def _is_pristine(state: dict[str, Any]) -> bool:
        baseline = empty_state()
        ignored = {"session_id", "reason", "audit"}
        if any(state.get(key) != value for key, value in baseline.items() if key not in ignored):
            return False
        audit = state.get("audit")
        return isinstance(audit, list) and all(
            isinstance(event, dict) and event.get("event") == "restart" for event in audit
        )

    def archive(self, expected_session_id: str, now: int | None = None) -> dict[str, Any]:
        archived_at = millis() if now is None else now
        with self.transaction() as (conn, state):
            self._expect_session(state, expected_session_id)
            if not state["config"]:
                raise PaperConflictError("Yapılandırılmamış paper oturumu arşivlenemez.")
            if state["entries_enabled"]:
                raise PaperConflictError("Yeni girişler açıkken paper oturumu arşivlenemez.")
            if state["position"] is not None:
                raise PaperConflictError("Açık veya kısmi pozisyon arşivlenemez.")
            if not self._orders_resolved(state):
                raise PaperConflictError("Çözümlenmemiş veya bozuk paper emri var.")
            self._audit(state, "archive", "Paper oturumu değişmez geçmişe arşivlendi")
            archived_state = json.loads(json.dumps(state))
            conn.execute(
                text(
                    "INSERT INTO paper_archives (session_id,archived_at,payload) "
                    "VALUES (:session_id,:archived_at,:payload)"
                ),
                {
                    "session_id": state["session_id"],
                    "archived_at": archived_at,
                    "payload": json.dumps(archived_state),
                },
            )
            state.clear()
            state.update(empty_state())
            result = self._capture(state)
        return result

    def archives(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        with self.db.connect() as conn, conn.begin():
            total = int(conn.execute(text("SELECT COUNT(*) FROM paper_archives")).scalar_one())
            rows = conn.execute(
                text(
                    "SELECT session_id, archived_at, payload FROM paper_archives "
                    "ORDER BY archived_at DESC, session_id DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            ).mappings()
            items = []
            for row in rows:
                state = json.loads(row["payload"])
                items.append(
                    {
                        "session_id": row["session_id"],
                        "started_at": state["started_at"],
                        "archived_at": row["archived_at"],
                        "config": state["config"],
                        "cash": state["cash"],
                        "equity": state["equity"],
                        "realized_pnl": state["realized_pnl"],
                        "order_count": len(state["orders"]),
                        "fill_count": len(state["fills"]),
                    }
                )
        return {"items": items, "total": total}

    def archive_detail(self, session_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT archived_at, payload FROM paper_archives "
                        "WHERE session_id=:session_id"
                    ),
                    {"session_id": session_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PaperArchiveNotFoundError("Paper arşivi bulunamadı")
        return {
            "session_id": session_id,
            "archived_at": row["archived_at"],
            "state": self._public_state(json.loads(row["payload"])),
        }

    @staticmethod
    def _observation_report(state: dict[str, Any], *, archived: bool, as_of: int) -> dict[str, Any]:
        observation = state.get("observation")
        public_observation = None
        if observation is not None:
            public_observation = json.loads(json.dumps(observation))
            public_observation.pop("continuity", None)
            public_observation.pop("last_process_id", None)
        started_at = state.get("started_at")
        limitations = [
            "İşçi gözlemleri bağımsız strateji örnekleri veya işlemler değildir.",
            "Giriş değerlendirmesi ve pozisyon yönetimi farklı kapsamlardır.",
            "Kapsama yalnız en fazla 10 saniye aralıklı sağlıklı örnekler arasından hesaplanır; "
            "kesintisiz hizmet garantisi değildir.",
            "Sonuçlar bütün paper oturumuna aittir; gözlemden önceki işlemleri içerebilir.",
        ]
        if observation is not None and observation["source_session_id"] is not None:
            limitations.append(
                "Gözlem geçmişi yedekten devralındı; süre zinciri kesildi. "
                f"Kaynak oturum: {observation['source_session_id']}"
            )
        if observation is not None and observation["mixed_code_identity"]:
            limitations.append(
                "Gözlem geçmişinde kod kimliği değişti; tek sabit sürüm deneyi değildir."
            )
        if observation is not None and as_of < observation["last_observed_at"]:
            limitations.append(
                "Rapor saati son kayıttan geride; zaman aralığı değerlendirilmemelidir."
            )
        evidence_status = (
            "no_observations" if observation is None else "archived" if archived else "collecting"
        )
        return {
            "schema_version": 1,
            "session_id": state["session_id"],
            "archived": archived,
            "as_of": as_of,
            "simulation": True,
            "live_eligible": False,
            "evidence_status": evidence_status,
            "started_at": started_at,
            "calendar_elapsed_ms": max(0, as_of - started_at) if started_at is not None else 0,
            "observation_span_ms": (
                max(
                    0,
                    max(as_of, observation["last_observed_at"]) - observation["first_observed_at"],
                )
                if observation is not None
                else 0
            ),
            "observation": public_observation,
            "results": {
                "scope": "entire_paper_session",
                "cash": state["cash"],
                "equity": state["equity"],
                "realized_pnl": state["realized_pnl"],
                "fill_count": len(state["fills"]),
                "position_open": state["position"] is not None,
            },
            "limitations": limitations,
        }

    def observation_report(
        self, expected_session_id: str, *, now: int | None = None
    ) -> dict[str, Any]:
        state = self.snapshot()
        self._expect_session(state, expected_session_id)
        return self._observation_report(
            state, archived=False, as_of=millis() if now is None else now
        )

    def archive_observation_report(self, session_id: str) -> dict[str, Any]:
        detail = self.archive_detail(session_id)
        return self._observation_report(detail["state"], archived=True, as_of=detail["archived_at"])

    def backup(self, destination: str) -> None:
        # Portable transactional logical backup, no credentials in the payload.
        from pathlib import Path

        with self.transaction() as (conn, state):
            rows = conn.execute(
                text(
                    "SELECT session_id, archived_at, payload FROM paper_archives "
                    "ORDER BY archived_at, session_id"
                )
            ).mappings()
            archives = [
                {
                    "session_id": row["session_id"],
                    "archived_at": row["archived_at"],
                    "state": {
                        **json.loads(row["payload"]),
                        "observation": json.loads(row["payload"]).get("observation"),
                    },
                }
                for row in rows
            ]
            Path(destination).write_text(
                json.dumps({"schema": 3, "current": state, "archives": archives}),
                encoding="utf-8",
            )

    def restore(self, source: str) -> None:
        from pathlib import Path

        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Yedek şeması geçersiz")
        if payload.get("schema") == 1 and set(payload) == {"schema", "state"}:
            restored = self._validate_state(payload["state"], legacy=True)
            original_session_id = None
            restored["session_id"] = str(uuid.uuid4())
            restored["started_at"] = (
                self._legacy_started_at(restored) if restored["config"] else None
            )
            archives: list[tuple[str, int, dict[str, Any]]] = []
        elif payload.get("schema") == 2 and set(payload) == {
            "schema",
            "current",
            "archives",
        }:
            restored = self._validate_state(payload["current"], pre_observation=True)
            original_session_id = restored["session_id"]
            if not isinstance(payload["archives"], list):
                raise ValueError("Arşiv listesi geçersiz")
            archives = [
                self._validate_archive(item, pre_observation=True) for item in payload["archives"]
            ]
            archive_ids = [item[0] for item in archives]
            if len(archive_ids) != len(set(archive_ids)):
                raise ValueError("Yinelenen arşiv oturum kimliği")
            if original_session_id in set(archive_ids):
                raise ValueError("Etkin oturum arşiv geçmişinde yinelenemez")
            restored["session_id"] = str(uuid.uuid4())
        elif payload.get("schema") == 3 and set(payload) == {
            "schema",
            "current",
            "archives",
        }:
            restored = self._validate_state(payload["current"])
            original_session_id = restored["session_id"]
            if not isinstance(payload["archives"], list):
                raise ValueError("Arşiv listesi geçersiz")
            archives = [self._validate_archive(item) for item in payload["archives"]]
            archive_ids = [item[0] for item in archives]
            if len(archive_ids) != len(set(archive_ids)):
                raise ValueError("Yinelenen arşiv oturum kimliği")
            if original_session_id in set(archive_ids):
                raise ValueError("Etkin oturum arşiv geçmişinde yinelenemez")
            restored["session_id"] = str(uuid.uuid4())
        else:
            raise ValueError("Yedek şeması geçersiz")
        restored["entries_enabled"] = False
        restored["reason"] = "Yedek yüklendi; girişler kapalı, uzlaştırma gerekli."
        if restored["observation"] is not None:
            observation = restored["observation"]
            if observation["source_session_id"] is None:
                observation["source_session_id"] = original_session_id
            observation["interruptions"]["restore"] += 1
            observation["continuity"] = None
        identity_message = (
            f"Yedek yüklendi; önceki oturum kimliği {original_session_id}; "
            "giriş yetkisi geri yüklenmedi"
            if original_session_id
            else "Eski şema yüklendi; yeni oturum kimliği atandı; giriş yetkisi geri yüklenmedi"
        )
        self._audit(restored, "restore", identity_message)
        with self.transaction() as (conn, state):
            count = int(conn.execute(text("SELECT COUNT(*) FROM paper_archives")).scalar_one())
            if not self._is_pristine(state) or count:
                raise ValueError("Yalnızca boş bir paper veritabanına geri yükleme yapılabilir")
            for session_id, archived_at, archived_state in archives:
                conn.execute(
                    text(
                        "INSERT INTO paper_archives (session_id,archived_at,payload) "
                        "VALUES (:session_id,:archived_at,:payload)"
                    ),
                    {
                        "session_id": session_id,
                        "archived_at": archived_at,
                        "payload": json.dumps(archived_state),
                    },
                )
            state.clear()
            state.update(restored)
