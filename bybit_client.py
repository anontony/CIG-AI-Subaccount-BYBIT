import base64
import hashlib
import hmac
import json
import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except Exception:  # pragma: no cover
    hashes = serialization = padding = rsa = None  # type: ignore


class BybitAPIError(RuntimeError):
    pass


class BybitClient:
    """Minimal Bybit V5 REST client for HMAC/RSA sub-account API keys."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_private_key: Optional[str] = None,
        auth_type: Optional[str] = None,
        env: Optional[str] = None,
        recv_window: Optional[str] = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("BYBIT_API_KEY", "")).strip()
        self.api_secret = (api_secret if api_secret is not None else os.getenv("BYBIT_API_SECRET", "")).strip()
        self.api_private_key = (api_private_key if api_private_key is not None else os.getenv("BYBIT_API_PRIVATE_KEY", "")).strip()
        self.auth_type = (auth_type if auth_type is not None else os.getenv("BYBIT_AUTH_TYPE", "auto")).strip().lower()
        if self.auth_type not in {"auto", "hmac", "rsa"}:
            self.auth_type = "auto"
        self.env = (env if env is not None else os.getenv("BYBIT_ENV", "testnet")).strip().lower()
        self.recv_window = str(int(recv_window if recv_window is not None else os.getenv("RECV_WINDOW", "5000")))
        self.base_url = (
            "https://api-testnet.bybit.com"
            if self.env == "testnet"
            else "https://api.bybit.com"
        )
        self.timeout = httpx.Timeout(15.0, connect=10.0)

    @property
    def selected_auth_type(self) -> str:
        if self.auth_type == "rsa":
            return "rsa"
        if self.auth_type == "hmac":
            return "hmac"
        return "rsa" if self.api_private_key else "hmac"

    @property
    def is_configured(self) -> bool:
        if not self.api_key:
            return False
        if self.selected_auth_type == "rsa":
            return bool(self.api_private_key)
        return bool(self.api_secret)

    def masked_key(self) -> str:
        if not self.api_key:
            return "not-set"
        if len(self.api_key) <= 9:
            return self.api_key[:3] + "***"
        return f"{self.api_key[:5]}...{self.api_key[-4:]}"

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _compact_json(self, body: Dict[str, Any]) -> str:
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    def _sign(self, timestamp: str, payload: str) -> str:
        param_str = f"{timestamp}{self.api_key}{self.recv_window}{payload}"
        if self.selected_auth_type == "rsa":
            if serialization is None or padding is None or hashes is None:
                raise BybitAPIError("Thiếu thư viện cryptography để ký RSA. Hãy cài requirements.txt.")
            try:
                private_key = serialization.load_pem_private_key(
                    self.api_private_key.encode("utf-8"),
                    password=None,
                )
                signature = private_key.sign(
                    param_str.encode("utf-8"),
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
                return base64.b64encode(signature).decode("ascii")
            except Exception as exc:
                raise BybitAPIError(f"Không ký được request bằng RSA private key: {exc}") from exc
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def signing_label(self) -> str:
        return "RSA" if self.selected_auth_type == "rsa" else "HMAC"

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        retry: bool = True,
    ) -> Dict[str, Any]:
        method = method.upper()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        query = urlencode(sorted(params.items())) if params else ""
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "bybit-ai-prompt-bot/1.0",
            "X-Referer": "bybit-ai-prompt-bot",
        }
        content: Optional[str] = None

        if auth:
            if not self.is_configured:
                if self.selected_auth_type == "rsa":
                    raise BybitAPIError("Thiếu Bybit API Key hoặc RSA private key của user này.")
                raise BybitAPIError("Thiếu Bybit API Key hoặc Bybit API Secret.")
            timestamp = self._timestamp()
            payload = query if method == "GET" else self._compact_json(body or {})
            headers.update(
                {
                    "X-BAPI-API-KEY": self.api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-SIGN": self._sign(timestamp, payload),
                    "X-BAPI-RECV-WINDOW": self.recv_window,
                    "X-BAPI-SIGN-TYPE": "2" if self.selected_auth_type == "rsa" else "1",
                }
            )
            if method != "GET":
                content = payload
        elif method != "GET" and body is not None:
            content = self._compact_json(body)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, url, headers=headers, content=content)
        except httpx.ConnectTimeout as exc:
            raise BybitAPIError(f"Bybit connection timeout: không kết nối được {self.base_url} trong thời gian cho phép.") from exc
        except httpx.ReadTimeout as exc:
            raise BybitAPIError("Bybit read timeout: Bybit không phản hồi kịp thời.") from exc
        except httpx.RequestError as exc:
            raise BybitAPIError(f"Bybit network error: {exc}") from exc

        if response.status_code >= 400:
            raise BybitAPIError(f"HTTP {response.status_code}: {response.text[:500]}")

        try:
            data = response.json()
        except Exception as exc:
            raise BybitAPIError(f"Bybit trả response không phải JSON hợp lệ: {response.text[:300]}") from exc
        ret_code = data.get("retCode")
        if ret_code in (10006,) and retry:
            await asyncio_sleep(1.0)
            return await self._request(method, path, params, body, auth, retry=False)
        if ret_code not in (0, None):
            ret_msg = data.get("retMsg") or data.get("ret_msg") or "Unknown Bybit error"
            raise BybitAPIError(f"Bybit retCode={ret_code}: {ret_msg}")
        return data

    async def public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("GET", path, params=params, auth=False)

    async def private_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("GET", path, params=params, auth=True)

    async def private_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", path, body=body, auth=True)

    async def market_time(self) -> Dict[str, Any]:
        return await self.public_get("/v5/market/time")

    async def test_connection(self) -> Dict[str, Any]:
        time_data = await self.market_time()
        server_second = int(time_data.get("result", {}).get("timeSecond", 0))
        local_second = int(time.time())
        drift = abs(local_second - server_second) if server_second else -1
        if drift > 5:
            raise BybitAPIError(f"System clock lệch {drift}s so với Bybit. Hãy bật sync giờ tự động.")
        wallet = await self.get_wallet_balance()
        return {"env": self.env, "key": self.masked_key(), "clock_drift_seconds": drift, "wallet": wallet, "signing": self.signing_label()}

    async def get_wallet_balance(self) -> Dict[str, Any]:
        return await self.private_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})

    async def get_ticker(self, symbol: str, category: str = "linear") -> Dict[str, Any]:
        data = await self.public_get("/v5/market/tickers", {"category": category, "symbol": symbol})
        items = data.get("result", {}).get("list", [])
        if not items:
            raise BybitAPIError(f"Không tìm thấy ticker cho {category}:{symbol}")
        return items[0]

    async def get_klines(self, symbol: str, category: str = "linear", interval: str = "15", limit: int = 120) -> Dict[str, Any]:
        return await self.public_get(
            "/v5/market/kline",
            {"category": category, "symbol": symbol, "interval": interval, "limit": str(limit)},
        )

    async def get_instrument(self, symbol: str, category: str = "linear") -> Dict[str, Any]:
        data = await self.public_get("/v5/market/instruments-info", {"category": category, "symbol": symbol})
        items = data.get("result", {}).get("list", [])
        if not items:
            raise BybitAPIError(f"Không tìm thấy instrument info cho {category}:{symbol}")
        return items[0]

    async def get_positions(self, symbol: str, category: str = "linear") -> Dict[str, Any]:
        return await self.private_get("/v5/position/list", {"category": category, "symbol": symbol})

    async def set_leverage(self, symbol: str, leverage: int, category: str = "linear") -> Dict[str, Any]:
        if category not in {"linear", "inverse"}:
            return {"retCode": 0, "retMsg": "Skip leverage for non-futures category"}
        lev = str(int(leverage))
        return await self.private_post(
            "/v5/position/set-leverage",
            {"category": category, "symbol": symbol, "buyLeverage": lev, "sellLeverage": lev},
        )

    async def safe_set_leverage(self, symbol: str, leverage: int, category: str = "linear") -> Dict[str, Any]:
        """Set futures leverage but ignore harmless 'not modified' style errors.

        Some Bybit accounts return a non-zero retCode when leverage is already
        equal to the requested value. That should not block order execution.
        """
        try:
            return await self.set_leverage(symbol, leverage, category)
        except BybitAPIError as exc:
            msg = str(exc).lower()
            harmless = (
                "not modified" in msg
                or "same leverage" in msg
                or "leverage not" in msg and "change" in msg
                or "110043" in msg
            )
            if harmless:
                return {"retCode": 0, "retMsg": "Leverage already set"}
            raise

    async def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.private_post("/v5/order/create", payload)

    async def set_trading_stop(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.private_post("/v5/position/trading-stop", payload)

    async def detect_position_idx(self, symbol: str, side: str, category: str = "linear") -> int:
        data = await self.get_positions(symbol, category)
        items = data.get("result", {}).get("list", [])
        # One-way commonly returns positionIdx 0. Hedge may return 1/2.
        hedge_seen = any(str(p.get("positionIdx", "0")) in ("1", "2") for p in items)
        if not hedge_seen:
            return 0
        return 1 if side.lower() in ("buy", "long") else 2

    async def open_position(
        self,
        *,
        symbol: str,
        side: str,
        qty: str,
        leverage: int,
        category: str = "linear",
        take_profit: Optional[str] = None,
        stop_loss: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self.safe_set_leverage(symbol, leverage, category)
        position_idx = await self.detect_position_idx(symbol, side, category)
        payload: Dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "side": "Buy" if side.lower() in ("buy", "long") else "Sell",
            "orderType": "Market",
            "qty": qty,
            "timeInForce": "IOC",
            "positionIdx": position_idx,
        }
        if take_profit:
            payload["takeProfit"] = take_profit
            payload["tpslMode"] = "Full"
        if stop_loss:
            payload["stopLoss"] = stop_loss
            payload["tpslMode"] = "Full"
        return await self.place_order(payload)

    async def close_position(self, *, symbol: str, target: str, category: str = "linear") -> Dict[str, Any]:
        data = await self.get_positions(symbol, category)
        items = data.get("result", {}).get("list", [])
        target = target.lower()
        matching = []
        for p in items:
            size = Decimal(str(p.get("size") or "0"))
            if size <= 0:
                continue
            idx = int(p.get("positionIdx") or 0)
            p_side = str(p.get("side") or "").lower()
            if target == "long" and (p_side == "buy" or idx == 1):
                matching.append(p)
            if target == "short" and (p_side == "sell" or idx == 2):
                matching.append(p)
            if target == "all":
                matching.append(p)
        if not matching:
            raise BybitAPIError(f"Không có position {target} đang mở cho {symbol}.")

        results = []
        for p in matching:
            idx = int(p.get("positionIdx") or 0)
            p_side = str(p.get("side") or "").lower()
            close_side = "Sell" if p_side == "buy" or idx == 1 else "Buy"
            results.append(
                await self.place_order(
                    {
                        "category": category,
                        "symbol": symbol,
                        "side": close_side,
                        "orderType": "Market",
                        "qty": str(p.get("size")),
                        "timeInForce": "IOC",
                        "reduceOnly": True,
                        "positionIdx": idx,
                    }
                )
            )
        return {"retCode": 0, "retMsg": "OK", "result": results}


    async def get_spot_base_balance(self, symbol: str) -> Decimal:
        """Return available base coin balance for a spot pair like BTCUSDT."""
        base = symbol.upper().replace("USDT", "")
        wallet = await self.get_wallet_balance()
        accounts = wallet.get("result", {}).get("list", [])
        for acct in accounts:
            for coin in acct.get("coin", []) or []:
                if str(coin.get("coin", "")).upper() == base:
                    raw = coin.get("availableToWithdraw") or coin.get("walletBalance") or coin.get("availableToBorrow") or "0"
                    try:
                        return Decimal(str(raw))
                    except Exception:
                        return Decimal("0")
        return Decimal("0")

    async def spot_market_buy(
        self,
        *,
        symbol: str,
        quote_usdt: str,
        take_profit: Optional[str] = None,
        stop_loss: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a plain Spot market buy order.

        Important: Bybit V5 accepts TP/SL fields when creating Spot Limit
        orders, but Spot Market Buy should be sent as a plain market order.
        CIG AI Subaccount therefore keeps TP/SL for spot in the local trade
        tracker and does not attach takeProfit/stopLoss to this API payload.
        This avoids Bybit retCode=170130/170134 caused by unsupported or
        over-precise TP/SL params on spot market orders.
        """
        payload: Dict[str, Any] = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": str(quote_usdt),
            "timeInForce": "IOC",
            "isLeverage": 0,
            "orderFilter": "Order",
        }
        return await self.place_order(payload)

    async def spot_market_sell(self, *, symbol: str, qty: str) -> Dict[str, Any]:
        return await self.place_order({
            "category": "spot",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": qty,
            "timeInForce": "IOC",
            "isLeverage": 0,
            "orderFilter": "Order",
        })

    async def spot_market_sell_all(self, *, symbol: str, qty_step: Decimal, min_qty: Decimal) -> Dict[str, Any]:
        balance = await self.get_spot_base_balance(symbol)
        qty = round_down_to_step(balance, qty_step)
        if qty <= 0 or qty < min_qty:
            raise BybitAPIError(
                f"Số dư Spot {format(balance.normalize(), 'f')} không đủ để bán {symbol}. "
                f"Có thể qty sau khi làm tròn theo bước lệnh còn {format(qty.normalize(), 'f')}; "
                f"minQty={format(min_qty.normalize(), 'f')}; qtyStep={format(qty_step.normalize(), 'f')}."
            )
        return await self.spot_market_sell(symbol=symbol, qty=format(qty.normalize(), "f"))

def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
