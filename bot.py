import time
import hmac
import hashlib
import math
import os
import uuid
import json
import requests
from urllib.parse import urlencode
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─── CONFIG — set these as environment variables in Railway ───────────────────
API_KEY        = os.environ.get("BINANCE_API_KEY",    "")
SECRET_KEY     = os.environ.get("BINANCE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET",     "")

MARGIN_PCT      = float(os.environ.get("MARGIN_PCT",      "0.20"))
LEVERAGE        = int(os.environ.get("LEVERAGE",          "10"))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES",   "5"))

BASE_URL = "https://fapi.binance.com"  # USDM Futures

# ─── SYMBOL MAP — TradingView ticker → Binance USDM symbol ───────────────────
SYMBOL_MAP = {
    # Crypto.com feed → Binance equivalent
    "GUNUSD":      "GUNUSDT",
    "PIPPINUSD":   "PIPPINUSDT",
    "RIVERUSD":    "RIVERUSDT",
    "VVVUSD":      "VVVUSDT",
    "MOGUSD":      "MOGUSDT",
    "USUALUSD":    "USUALUSDT",
    "BROCCOLIUSD": "BROCCOLIUSDT",
    "SPX6USD":     "SPX6900USDT",
    "RESOLVUSD":   "RESOLVEUSDT",
    "API3USD":     "API3USDT",
    # Binance feed — already correct
    "BIOUSDT":     "BIOUSDT",
    "THEUSDT":     "THEUSDT",
    "PENGUUSDT":   "PENGUUSDT",
    "AEROUSDT":    "AEROUSDT",
    "PUMPUSDT":    "PUMPUSDT",
}

# ─── SIGNATURE ────────────────────────────────────────────────────────────────
# Binance: HMAC-SHA256 over the full query string including timestamp
def _sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    query_string = urlencode(params)
    params["signature"] = hmac.new(
        SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return params

def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}

def b_get(path: str, params: dict = None) -> dict:
    r = requests.get(BASE_URL + path, headers=_headers(),
                     params=_sign(params or {}), timeout=10)
    return r.json()

def b_post(path: str, params: dict) -> dict:
    r = requests.post(BASE_URL + path, headers=_headers(),
                      params=_sign(params), timeout=10)
    return r.json()

# ─── ACCOUNT BALANCE ─────────────────────────────────────────────────────────
def get_balance() -> float:
    """GET /fapi/v3/balance → available USDT"""
    try:
        resp = b_get("/fapi/v3/balance")
        for asset in (resp if isinstance(resp, list) else []):
            if asset.get("asset") == "USDT":
                bal = float(asset.get("availableBalance", 0))
                print(f"[ACCOUNT] Balance: ${bal:.2f}")
                return bal
        print(f"[ACCOUNT] Unexpected: {resp}")
        return 0.0
    except Exception as e:
        print(f"[ACCOUNT] Error: {e}")
        return 0.0

# ─── OPEN POSITIONS ───────────────────────────────────────────────────────────
def get_open_positions() -> list:
    """GET /fapi/v3/positionRisk → non-zero positions"""
    try:
        resp = b_get("/fapi/v3/positionRisk")
        return [p for p in (resp if isinstance(resp, list) else [])
                if float(p.get("positionAmt", 0)) != 0]
    except Exception as e:
        print(f"[POSITIONS] Error: {e}")
        return []

# ─── LEVERAGE & MARGIN MODE ───────────────────────────────────────────────────
def set_leverage_isolated(symbol: str):
    try:
        r1 = b_post("/fapi/v1/leverage",    {"symbol": symbol, "leverage": LEVERAGE})
        r2 = b_post("/fapi/v1/marginType",  {"symbol": symbol, "marginType": "ISOLATED"})
        print(f"[LEVERAGE] {symbol} {LEVERAGE}x isolated | {r1.get('msg','ok')} | {r2.get('msg','ok')}")
    except Exception as e:
        print(f"[LEVERAGE] Error: {e}")

# ─── PLACE MARKET ENTRY ───────────────────────────────────────────────────────
def place_entry(symbol: str, side: str, qty: float) -> dict:
    """POST /fapi/v1/order — MARKET, one-way mode (positionSide=BOTH)"""
    return b_post("/fapi/v1/order", {
        "symbol":           symbol,
        "side":             side,       # BUY or SELL
        "type":             "MARKET",
        "quantity":         str(qty),
        "newClientOrderId": uuid.uuid4().hex[:32],
    })

# ─── PRICE PRECISION ─────────────────────────────────────────────────────────
_tick_cache = {}

def get_tick_size(symbol: str) -> float:
    """Fetch Binance tick size (price precision) for a symbol. Cached."""
    if symbol in _tick_cache:
        return _tick_cache[symbol]
    try:
        info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                for f in s.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                        _tick_cache[symbol] = tick
                        return tick
    except Exception as e:
        print(f"[TICK] Error fetching tick size for {symbol}: {e}")
    return 0.0001  # safe fallback

def round_price(price: float, tick: float) -> str:
    """Round price to Binance tick size and return as string."""
    if tick <= 0:
        return str(round(price, 4))
    precision = max(0, round(-math.log10(tick)))
    rounded = math.floor(price / tick) * tick
    return f"{rounded:.{int(precision)}f}"

# ─── PLACE TP + SL (separate orders, post Dec-2025 Binance API) ──────────────
# STOP_MARKET and TAKE_PROFIT_MARKET use closePosition=true + workingType=MARK_PRICE
def place_tp_sl(symbol: str, entry_side: str,
                tp_price: float, sl_price: float) -> dict:
    """
    Places TP and SL as separate conditional orders.
    entry_side BUY  → closing side is SELL
    entry_side SELL → closing side is BUY
    """
    close_side = "SELL" if entry_side == "BUY" else "BUY"
    tick = get_tick_size(symbol)
    results = {}

    # Small delay to ensure entry order is registered before placing conditionals
    time.sleep(1)

    if tp_price > 0:
        results["tp"] = b_post("/fapi/v1/order", {
            "symbol":           symbol,
            "side":             close_side,
            "type":             "TAKE_PROFIT_MARKET",
            "stopPrice":        round_price(tp_price, tick),
            "closePosition":    "true",
            "timeInForce":      "GTE_GTC",
            "workingType":      "MARK_PRICE",
            "newClientOrderId": uuid.uuid4().hex[:32],
        })
        print(f"[TP] {symbol} @ {round_price(tp_price, tick)} → {results['tp']}")

    if sl_price > 0:
        results["sl"] = b_post("/fapi/v1/order", {
            "symbol":           symbol,
            "side":             close_side,
            "type":             "STOP_MARKET",
            "stopPrice":        round_price(sl_price, tick),
            "closePosition":    "true",
            "timeInForce":      "GTE_GTC",
            "workingType":      "MARK_PRICE",
            "newClientOrderId": uuid.uuid4().hex[:32],
        })
        print(f"[SL] {symbol} @ {round_price(sl_price, tick)} → {results['sl']}")

    return results

# ─── QUANTITY CALC ────────────────────────────────────────────────────────────
def calc_qty(price: float, balance: float) -> float:
    """notional = balance × margin% × leverage → qty = notional / price"""
    if price <= 0 or balance <= 0:
        return 0.0
    notional = balance * MARGIN_PCT * LEVERAGE
    return math.floor((notional / price) * 10) / 10  # 1 decimal, round down

# ─── WEBHOOK ──────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "empty body"}), 400

    if request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    side     = data.get("side", "").upper()
    ticker   = data.get("symbol", "")
    price    = float(data.get("price", 0))
    sl_price = float(data.get("sl", 0))
    tp_price = float(data.get("tp", 0))

    if side not in ("BUY", "SELL"):
        return jsonify({"error": "side must be BUY or SELL"}), 400
    if not ticker or price <= 0:
        return jsonify({"error": "missing symbol or price"}), 400

    symbol = SYMBOL_MAP.get(ticker)
    if not symbol:
        return jsonify({"error": f"unknown ticker: {ticker}"}), 400

    balance = get_balance()
    if balance <= 0:
        return jsonify({"error": "could not fetch balance"}), 500

    open_positions = get_open_positions()
    if len(open_positions) >= MAX_OPEN_TRADES:
        print(f"[SKIP] Max trades: {len(open_positions)}/{MAX_OPEN_TRADES}")
        return jsonify({"status": "skipped", "reason": "max_open_trades"}), 200

    open_symbols = [p.get("symbol") for p in open_positions]
    if symbol in open_symbols:
        print(f"[SKIP] Already in {symbol}")
        return jsonify({"status": "skipped", "reason": "already_open"}), 200

    set_leverage_isolated(symbol)

    qty = calc_qty(price, balance)
    if qty <= 0:
        return jsonify({"error": "quantity is zero — check balance/price"}), 400

    entry_result = place_entry(symbol, side, qty)
    tp_sl_result = place_tp_sl(symbol, side, tp_price, sl_price) if (tp_price > 0 or sl_price > 0) else {}

    log = {
        "status":   "ok",
        "side":     side,
        "symbol":   symbol,
        "price":    price,
        "tp":       tp_price,
        "sl":       sl_price,
        "qty":      qty,
        "balance":  balance,
        "notional": round(balance * MARGIN_PCT * LEVERAGE, 2),
        "entry":    entry_result,
        "tp_sl":    tp_sl_result,
    }
    print(f"[ORDER] {json.dumps(log)}")
    return jsonify(log), 200

# ─── HEALTH ───────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    balance   = get_balance()
    positions = get_open_positions()
    return jsonify({
        "status":      "running",
        "exchange":    "Binance USDM Futures",
        "balance":     balance,
        "open_trades": len(positions),
        "max_trades":  MAX_OPEN_TRADES,
        "leverage":    LEVERAGE,
        "margin_pct":  MARGIN_PCT,
        "positions":   [p.get("symbol") for p in positions],
    }), 200

# ─── DEBUG ────────────────────────────────────────────────────────────────────
@app.route("/debug", methods=["GET"])
def debug():
    """Returns raw Binance responses — no processing, shows exact errors."""
    results = {}
    for label, path in [
        ("balance",   "/fapi/v3/balance"),
        ("account",   "/fapi/v3/account"),
        ("positions", "/fapi/v3/positionRisk"),
    ]:
        try:
            results[label] = b_get(path)
        except Exception as e:
            results[label] = {"exception": str(e)}
    return jsonify(results), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
