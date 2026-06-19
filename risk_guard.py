import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Any, Dict, Optional

from bybit_client import round_down_to_step


class RiskError(ValueError):
    pass


@dataclass
class RiskConfig:
    allowed_symbols: set[str]
    default_category: str
    dry_run: bool
    max_leverage: int
    max_margin_per_trade_usdt: Decimal
    max_notional_usdt: Decimal
    max_daily_trades: int
    require_tp_sl: bool
    default_take_profit_pct: Decimal
    default_stop_loss_pct: Decimal
    min_seconds_between_trades: int

    @classmethod
    def from_env(cls) -> "RiskConfig":
        allowed = {
            s.strip().upper()
            for s in os.getenv("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
            if s.strip()
        }
        return cls(
            allowed_symbols=allowed,
            default_category=os.getenv("DEFAULT_CATEGORY", "auto").strip().lower(),
            dry_run=os.getenv("DRY_RUN", "true").strip().lower() in {"1", "true", "yes", "on"},
            max_leverage=int(os.getenv("MAX_LEVERAGE", "5")),
            max_margin_per_trade_usdt=Decimal(os.getenv("MAX_MARGIN_PER_TRADE_USDT", "20")),
            max_notional_usdt=Decimal(os.getenv("MAX_NOTIONAL_USDT", "100")),
            max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "5")),
            require_tp_sl=os.getenv("REQUIRE_TP_SL", "true").strip().lower() == "true",
            default_take_profit_pct=Decimal(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "1.2")),
            default_stop_loss_pct=Decimal(os.getenv("DEFAULT_STOP_LOSS_PCT", "0.6")),
            min_seconds_between_trades=int(os.getenv("MIN_SECONDS_BETWEEN_TRADES", "0")),
        )

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "RiskConfig":
        allowed = {
            s.strip().upper()
            for s in str(settings.get("allowed_symbols") or "BTCUSDT,ETHUSDT").split(",")
            if s.strip()
        }
        return cls(
            allowed_symbols=allowed,
            default_category=str(settings.get("default_category") or "auto").strip().lower(),
            dry_run=bool(settings.get("dry_run", True)),
            max_leverage=int(settings.get("max_leverage") or 5),
            max_margin_per_trade_usdt=Decimal(str(settings.get("max_margin_per_trade_usdt") or "20")),
            max_notional_usdt=Decimal(str(settings.get("max_notional_usdt") or "100")),
            max_daily_trades=int(settings.get("max_daily_trades") or 5),
            require_tp_sl=bool(settings.get("require_tp_sl", True)),
            default_take_profit_pct=Decimal(str(settings.get("default_take_profit_pct") or "1.2")),
            default_stop_loss_pct=Decimal(str(settings.get("default_stop_loss_pct") or "0.6")),
            min_seconds_between_trades=int(settings.get("min_seconds_between_trades") or 0),
        )

    @property
    def live_trading_enabled(self) -> bool:
        return not self.dry_run


class RiskGuard:
    FUTURES_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "CLOSE_ALL"}
    SPOT_ACTIONS = {"SPOT_BUY", "SPOT_SELL", "SPOT_SELL_ALL"}

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.config = config or RiskConfig.from_env()
        self.last_trade_ts: float = 0.0

    def public_config(self) -> Dict[str, Any]:
        return {
            "allowed_symbols": sorted(self.config.allowed_symbols),
            "default_category": self.config.default_category,
            "dry_run": self.config.dry_run,
            "live_trading_enabled": self.config.live_trading_enabled,
            "max_leverage": self.config.max_leverage,
            "max_margin_per_trade_usdt": str(self.config.max_margin_per_trade_usdt),
            "max_notional_usdt": str(self.config.max_notional_usdt),
            "max_daily_trades": self.config.max_daily_trades,
            "require_tp_sl": self.config.require_tp_sl,
            "default_take_profit_pct": str(self.config.default_take_profit_pct),
            "default_stop_loss_pct": str(self.config.default_stop_loss_pct),
            "min_seconds_between_trades": self.config.min_seconds_between_trades,
            "supported_categories": ["spot", "linear"],
            "supported_actions": sorted(self.FUTURES_ACTIONS | self.SPOT_ACTIONS | {"WAIT"}),
        }

    def _decimal(self, value: Any, name: str, required: bool = True) -> Optional[Decimal]:
        if value in (None, ""):
            if required:
                raise RiskError(f"Thiếu {name}.")
            return None
        try:
            return Decimal(str(value).replace(",", ""))
        except (InvalidOperation, ValueError) as exc:
            raise RiskError(f"{name} không hợp lệ: {value}") from exc

    def _validate_symbol(self, symbol: str) -> str:
        symbol = (symbol or "").upper().strip()
        if not symbol:
            raise RiskError("Thiếu symbol.")
        if symbol not in self.config.allowed_symbols:
            raise RiskError(f"Symbol {symbol} không nằm trong allowed_symbols={sorted(self.config.allowed_symbols)}.")
        return symbol

    def resolve_category(self, action: Dict[str, Any]) -> str:
        raw_action = self._canonical_action(action.get("action"))
        requested = str(action.get("category") or "").lower().strip()
        default = self.config.default_category

        if raw_action in self.SPOT_ACTIONS:
            return "spot"
        if raw_action in {"OPEN_SHORT", "CLOSE_SHORT"}:
            return "linear" if default in {"auto", "spot"} else default
        if requested in {"spot", "linear", "inverse"}:
            return requested
        if default in {"spot", "linear", "inverse"}:
            return default
        return "linear"

    def _validate_category(self, category: Optional[str], raw_action: str) -> str:
        category = (category or self.config.default_category or "auto").lower().strip()
        if category == "auto":
            category = "spot" if raw_action in self.SPOT_ACTIONS else "linear"
        if category not in {"linear", "inverse", "spot"}:
            raise RiskError("Bot chỉ cho phép category: auto, linear, inverse, spot.")
        if category == "spot" and raw_action in {"OPEN_SHORT", "CLOSE_SHORT"}:
            raise RiskError("Spot thường không hỗ trợ short. Hãy dùng futures/linear nếu muốn short.")
        return category

    def _canonical_action(self, raw: Any) -> str:
        action = str(raw or "WAIT").upper().strip()
        aliases = {
            "HOLD": "WAIT",
            "NO_TRADE": "WAIT",
            "BUY_SPOT": "SPOT_BUY",
            "SPOT_LONG": "SPOT_BUY",
            "SELL_SPOT": "SPOT_SELL",
            "SELL_ALL_SPOT": "SPOT_SELL_ALL",
            "SPOT_CLOSE": "SPOT_SELL_ALL",
            "BUY": "SPOT_BUY",
            "SELL": "SPOT_SELL",
        }
        return aliases.get(action, action)

    def _apply_default_tp_sl(self, raw: str, take_profit: Optional[Decimal], stop_loss: Optional[Decimal], market_price: Decimal) -> tuple[Optional[Decimal], Optional[Decimal]]:
        if raw not in {"OPEN_LONG", "OPEN_SHORT", "SPOT_BUY"}:
            return take_profit, stop_loss
        tp_pct = self.config.default_take_profit_pct / Decimal("100")
        sl_pct = self.config.default_stop_loss_pct / Decimal("100")
        if take_profit is None and tp_pct > 0:
            take_profit = market_price * (Decimal("1") + tp_pct) if raw in {"OPEN_LONG", "SPOT_BUY"} else market_price * (Decimal("1") - tp_pct)
        if stop_loss is None and sl_pct > 0:
            stop_loss = market_price * (Decimal("1") - sl_pct) if raw in {"OPEN_LONG", "SPOT_BUY"} else market_price * (Decimal("1") + sl_pct)
        return take_profit, stop_loss

    def _round_tp_sl_to_tick(self, take_profit: Optional[Decimal], stop_loss: Optional[Decimal], price_tick: Optional[Decimal]) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Round TP/SL prices to Bybit instrument tickSize to avoid retCode 170134.

        Bybit requires order prices to match each symbol's price tick. We round down
        to the nearest tick because TP/SL are guardrail prices and must be accepted
        by the exchange precision rules. Direction is validated after rounding.
        """
        if price_tick is None or price_tick <= 0:
            return take_profit, stop_loss
        if take_profit is not None:
            take_profit = round_down_to_step(take_profit, price_tick)
        if stop_loss is not None:
            stop_loss = round_down_to_step(stop_loss, price_tick)
        return take_profit, stop_loss

    def _check_frequency(self, daily_trade_count: int) -> None:
        if daily_trade_count >= self.config.max_daily_trades:
            raise RiskError(f"Đã đạt MAX_DAILY_TRADES={self.config.max_daily_trades}.")
        # Global anti-spam cooldown is intentionally disabled.
        # Scheduled prompts still use their own interval gate, for example 10 USDT/1h.
        return


    def _apply_explicit_tp_sl_pct(self, raw: str, action: Dict[str, Any], market_price: Decimal) -> tuple[Optional[Decimal], Optional[Decimal]]:
        tp_pct = self._decimal(action.get("take_profit_pct"), "take_profit_pct", required=False)
        sl_pct = self._decimal(action.get("stop_loss_pct"), "stop_loss_pct", required=False)
        tp = None
        sl = None
        if tp_pct is not None:
            p = tp_pct / Decimal("100")
            tp = market_price * (Decimal("1") + p) if raw in {"OPEN_LONG", "SPOT_BUY"} else market_price * (Decimal("1") - p)
        if sl_pct is not None:
            p = sl_pct / Decimal("100")
            sl = market_price * (Decimal("1") - p) if raw in {"OPEN_LONG", "SPOT_BUY"} else market_price * (Decimal("1") + p)
        return tp, sl

    def _validate_tp_sl_direction(self, raw: str, market_price: Decimal, take_profit: Optional[Decimal], stop_loss: Optional[Decimal]) -> None:
        if not self.config.require_tp_sl or raw not in {"OPEN_LONG", "OPEN_SHORT", "SPOT_BUY"}:
            return
        if take_profit is None:
            raise RiskError("Thiếu take_profit/TP và chưa có TP mặc định hợp lệ.")
        if stop_loss is None:
            raise RiskError("Thiếu stop_loss/SL và chưa có SL mặc định hợp lệ.")
        if raw in {"OPEN_LONG", "SPOT_BUY"} and not (stop_loss < market_price < take_profit):
            raise RiskError("Lệnh mua/long cần stop_loss < giá hiện tại < take_profit.")
        if raw == "OPEN_SHORT" and not (take_profit < market_price < stop_loss):
            raise RiskError("OPEN_SHORT cần take_profit < giá hiện tại < stop_loss.")

    def normalize_action(
        self,
        action: Dict[str, Any],
        *,
        market_price: Decimal,
        qty_step: Decimal,
        min_qty: Decimal,
        min_order_amt: Optional[Decimal] = None,
        price_tick: Optional[Decimal] = None,
        daily_trade_count: int,
    ) -> Dict[str, Any]:
        raw = self._canonical_action(action.get("action"))
        reason = str(action.get("reason") or "No reason provided")[:500]
        if raw == "WAIT":
            return {"action": "WAIT", "reason": reason}

        if raw not in self.FUTURES_ACTIONS | self.SPOT_ACTIONS:
            raise RiskError(f"Action không được phép: {raw}")

        symbol = self._validate_symbol(str(action.get("symbol") or ""))
        category = self._validate_category(action.get("category"), raw)
        self._check_frequency(daily_trade_count)

        if market_price <= 0:
            raise RiskError("market_price không hợp lệ.")

        # CLOSE futures actions.
        if raw in {"CLOSE_LONG", "CLOSE_SHORT", "CLOSE_ALL"}:
            if category == "spot":
                raw = "SPOT_SELL_ALL"
            else:
                return {"action": raw, "symbol": symbol, "category": category, "reason": reason}

        # Spot sell all needs no sizing from user.
        if raw == "SPOT_SELL_ALL":
            return {"action": raw, "symbol": symbol, "category": "spot", "reason": reason}

        pct_tp, pct_sl = self._apply_explicit_tp_sl_pct(raw, action, market_price)
        take_profit = pct_tp if pct_tp is not None else self._decimal(action.get("take_profit"), "take_profit", required=False)
        stop_loss = pct_sl if pct_sl is not None else self._decimal(action.get("stop_loss"), "stop_loss", required=False)
        take_profit, stop_loss = self._apply_default_tp_sl(raw, take_profit, stop_loss, market_price)
        take_profit, stop_loss = self._round_tp_sl_to_tick(take_profit, stop_loss, price_tick)

        if raw == "SPOT_BUY":
            order_usdt = self._decimal(action.get("order_usdt", action.get("margin_usdt")), "order_usdt")
            if order_usdt is None or order_usdt <= 0:
                raise RiskError("order_usdt phải lớn hơn 0.")
            if order_usdt > self.config.max_margin_per_trade_usdt:
                raise RiskError(f"order_usdt={order_usdt} vượt vốn tối đa/lệnh={self.config.max_margin_per_trade_usdt}.")
            if order_usdt > self.config.max_notional_usdt:
                raise RiskError(f"Giá trị spot order={order_usdt} vượt MAX_NOTIONAL_USDT={self.config.max_notional_usdt}.")
            if min_order_amt is not None and min_order_amt > 0 and order_usdt < min_order_amt:
                raise RiskError(f"order_usdt={order_usdt} nhỏ hơn minOrderAmt={min_order_amt}.")
            self._validate_tp_sl_direction(raw, market_price, take_profit, stop_loss)
            approx_qty = round_down_to_step(order_usdt / market_price, qty_step)
            return {
                "action": raw,
                "symbol": symbol,
                "category": "spot",
                "side": "buy",
                "order_usdt": str(order_usdt),
                "approx_base_qty": format(approx_qty.normalize(), "f") if approx_qty > 0 else None,
                "take_profit": format(take_profit.normalize(), "f") if take_profit else None,
                "stop_loss": format(stop_loss.normalize(), "f") if stop_loss else None,
                "reason": reason,
            }

        if raw == "SPOT_SELL":
            qty_value = self._decimal(action.get("qty"), "qty", required=False)
            order_usdt = self._decimal(action.get("order_usdt", action.get("margin_usdt")), "order_usdt", required=False)
            if qty_value is None:
                if order_usdt is None or order_usdt <= 0:
                    raise RiskError("SPOT_SELL cần qty coin hoặc order_usdt.")
                qty_value = round_down_to_step(order_usdt / market_price, qty_step)
            else:
                qty_value = round_down_to_step(qty_value, qty_step)
            if qty_value <= 0 or qty_value < min_qty:
                raise RiskError(f"qty={qty_value} nhỏ hơn minQty={min_qty}.")
            notional = qty_value * market_price
            if notional > self.config.max_notional_usdt:
                raise RiskError(f"Giá trị spot sell≈{notional} vượt MAX_NOTIONAL_USDT={self.config.max_notional_usdt}.")
            return {
                "action": raw,
                "symbol": symbol,
                "category": "spot",
                "side": "sell",
                "qty": format(qty_value.normalize(), "f"),
                "notional_usdt_est": str(notional.quantize(Decimal("0.01"))),
                "reason": reason,
            }

        # Futures open actions.
        leverage = int(action.get("leverage") or 1)
        if leverage < 1 or leverage > self.config.max_leverage:
            raise RiskError(f"Leverage {leverage} vượt MAX_LEVERAGE={self.config.max_leverage}.")

        margin = self._decimal(action.get("margin_usdt", action.get("order_usdt")), "margin_usdt")
        if margin is None or margin <= 0:
            raise RiskError("margin_usdt phải lớn hơn 0.")
        if margin > self.config.max_margin_per_trade_usdt:
            raise RiskError(f"margin_usdt={margin} vượt MAX_MARGIN_PER_TRADE_USDT={self.config.max_margin_per_trade_usdt}.")

        min_notional_required = market_price * min_qty
        if min_notional_required > self.config.max_notional_usdt:
            raise RiskError(
                f"Không thể mở {symbol} futures với cấu hình hiện tại: giá trị lệnh tối thiểu theo minQty={min_qty} "
                f"là khoảng {min_notional_required.quantize(Decimal('0.01'))} USDT, nhưng MAX_NOTIONAL_USDT={self.config.max_notional_usdt}. "
                "Hãy tăng giới hạn giá trị vị thế tối đa hoặc chọn symbol có minQty thấp hơn."
            )

        notional = margin * Decimal(leverage)
        if notional > self.config.max_notional_usdt:
            raise RiskError(f"Notional={notional} vượt MAX_NOTIONAL_USDT={self.config.max_notional_usdt}.")

        qty = round_down_to_step(notional / market_price, qty_step)
        if qty <= 0 or qty < min_qty:
            min_margin_current_lev = (min_notional_required / Decimal(leverage)).quantize(Decimal('0.01'), rounding=ROUND_UP)
            min_leverage_needed = (min_notional_required / margin).to_integral_value(rounding=ROUND_UP) if margin > 0 else Decimal('0')
            raise RiskError(
                f"Lệnh futures {symbol} quá nhỏ: vốn {margin} USDT x đòn bẩy {leverage}x = notional {notional} USDT, "
                f"chưa đủ minQty={min_qty} (~{min_notional_required.quantize(Decimal('0.01'))} USDT). "
                f"Cần tối thiểu khoảng {min_margin_current_lev} USDT ở {leverage}x"
                + (f" hoặc đòn bẩy tối thiểu {int(min_leverage_needed)}x với vốn {margin} USDT." if min_leverage_needed > 0 else ".")
                + " Hãy tăng vốn, tăng max leverage/risk setting, hoặc chọn symbol có minQty thấp hơn."
            )

        self._validate_tp_sl_direction(raw, market_price, take_profit, stop_loss)

        return {
            "action": raw,
            "symbol": symbol,
            "category": category,
            "side": "long" if raw == "OPEN_LONG" else "short",
            "leverage": leverage,
            "margin_usdt": str(margin),
            "notional_usdt": str(notional),
            "qty": format(qty.normalize(), "f"),
            "take_profit": format(take_profit.normalize(), "f") if take_profit else None,
            "stop_loss": format(stop_loss.normalize(), "f") if stop_loss else None,
            "reason": reason,
        }

    def mark_trade_sent(self) -> None:
        self.last_trade_ts = time.time()
