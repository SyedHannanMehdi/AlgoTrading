import time
import hmac
import hashlib
import math
import os
import uuid
import json
import threading
import requests
from urllib.parse import urlencode
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
API_KEY        = os.environ.get("BINANCE_API_KEY",    "")
SECRET_KEY     = os.environ.get("BINANCE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET",     "")

MARGIN_PCT      = float(os.environ.get("MARGIN_PCT",      "0.20"))
LEVERAGE        = int(os.environ.get("LEVERAGE",          "10"))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES",   "5"))

BASE_URL = "https://fapi.binance.com"

# ─── SYMBOL MAP ───────────────────────────────────────────────────────────────
SYMBOL_MAP = {
    "GUNUSD":      "GUNUSDT",
    "PIPPINUSD":   "PIPPINUSDT",
    "RIVERUSD":    "RIVERUSDT",
    "VVVUSD":      "VVVUSDT",
    "MOGUSD":      "MOGUSDT",
    "USUALUSD":    "USUALUSDT",
    "BROCCOLIUSD": "BROCCOLIUSDT",
    "SPX6USD":     "SPX6900USDT",
    "RESOLVUSD":   "RESOLVUSDT",
    "API3USD":     "API3USDT",
    "BIOUSDT":     "BIOUSDT",
    "BIOUSDT.P":   "BIOUSDT",
    "THEUSDT":     "THEUSDT",
    "THEUSDT.P":   "THEUSDT",
    "PENGUUSDT":   "PENGUUSDT",
    "PENGUUSDT.P": "PENGUUSDT",
    "AEROUSDT":    "AEROUSDT",
    "AEROUSDT.P":  "AEROUSDT",
    "PUMPUSDT":    "PUMPUSDT",
    "PUMPUSDT.P":  "PUMPUSDT",
}

# ─── EXCHANGE INFO CACHE ──────────────────────────────────────────────────────
_exchange_info = {}

def load_exchange_info():
    global _exchange_info
    try:
        info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info.get("symbols", []):
            sym = s["symbol"]
            tick = 0.0001
            step = 0.1
            for f in s.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
            _exchange_info[sym] = {"tick": tick, "step": step}
        print(f"[INIT] Exchange info loaded for {len(_exchange_info)} symbols")
    except Exception as e:
        print(f"[INIT] Failed to load exchange info: {e}")

load_exchange_info()

def get_tick(symbol): return _exchange_info.get(symbol, {}).get("tick", 0.0001)
def get_step(symbol): return _exchange_info.get(symbol, {}).get("step", 0.1)

# ─── SIGNATURE ────────────────────────────────────────────────────────────────
def _sign(params):
    params["timestamp"] = int(time.time() * 1000)
    qs = urlencode(params)
    params["signature"] = hmac.new(SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return params

def _headers(): return {"X-MBX-APIKEY": API_KEY}

def b_get(path, params=None):
    r = requests.get(BASE_URL + path, headers=_headers(), params=_sign(params or {}), timeout=10)
    return r.json()

def b_post(path, params):
    r = requests.post(BASE_URL + path, headers=_headers(), params=_sign(params), timeout=10)
    return r.json()

# ─── ACCOUNT ─────────────────────────────────────────────────────────────────
def get_balance():
    try:
        resp = b_get("/fapi/v3/balance")
        for a in (resp if isinstance(resp, list) else []):
            if a.get("asset") == "USDT":
                bal = float(a.get("availableBalance", 0))
                print(f"[ACCOUNT] Balance: ${bal:.2f}")
                return bal
        print(f"[ACCOUNT] Unexpected: {resp}")
        return 0.0
    except Exception as e:
        print(f"[ACCOUNT] Error: {e}")
        return 0.0

def get_open_positions():
    try:
        resp = b_get("/fapi/v3/positionRisk")
        return [p for p in (resp if isinstance(resp, list) else []) if float(p.get("positionAmt", 0)) != 0]
    except Exception as e:
        print(f"[POSITIONS] Error: {e}")
        return []

# ─── LEVERAGE ─────────────────────────────────────────────────────────────────
def set_leverage_isolated(symbol):
    try:
        r1 = b_post("/fapi/v1/leverage",   {"symbol": symbol, "leverage": LEVERAGE})
        r2 = b_post("/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        print(f"[LEVERAGE] {symbol} {LEVERAGE}x isolated | {r1.get('msg','ok')} | {r2.get('msg','ok')}")
    except Exception as e:
        print(f"[LEVERAGE] Error: {e}")

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def round_price(price, tick):
    if tick <= 0: return str(round(price, 4))
    precision = max(0, round(-math.log10(tick)))
    return f"{math.floor(price / tick) * tick:.{int(precision)}f}"

def calc_qty(symbol, price, balance):
    if price <= 0 or balance <= 0: return 0.0
    raw = (balance * MARGIN_PCT * LEVERAGE) / price
    step = get_step(symbol)
    if step <= 0: return math.floor(raw * 10) / 10
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(raw / step) * step, int(precision))

# ─── ORDERS ───────────────────────────────────────────────────────────────────
def place_entry(symbol, side, qty):
    return b_post("/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "MARKET",
        "quantity": str(qty), "newClientOrderId": uuid.uuid4().hex[:32],
    })

def place_tp_sl(symbol, entry_side, tp_price, sl_price):
    """Runs in background thread — places TP/SL after entry settles.
    Uses /fapi/v1/algoOrder (required since Binance Dec 2025 migration).
    """
    close_side = "SELL" if entry_side == "BUY" else "BUY"
    tick = get_tick(symbol)
    time.sleep(1)

    if tp_price > 0:
        r = b_post("/fapi/v1/algoOrder", {
            "symbol": symbol, "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": round_price(tp_price, tick),
            "closePosition": "true", "timeInForce": "GTE_GTC",
            "workingType": "MARK_PRICE",
            "newClientOrderId": uuid.uuid4().hex[:32],
        })
        print(f"[TP] {symbol} @ {round_price(tp_price, tick)} → {r}")

    if sl_price > 0:
        r = b_post("/fapi/v1/algoOrder", {
            "symbol": symbol, "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": round_price(sl_price, tick),
            "closePosition": "true", "timeInForce": "GTE_GTC",
            "workingType": "MARK_PRICE",
            "newClientOrderId": uuid.uuid4().hex[:32],
        })
        print(f"[SL] {symbol} @ {round_price(sl_price, tick)} → {r}")

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
        return jsonify({"status": "skipped", "reason": "max_open_trades"}), 200

    if symbol in [p.get("symbol") for p in open_positions]:
        return jsonify({"status": "skipped", "reason": "already_open"}), 200

    set_leverage_isolated(symbol)

    qty = calc_qty(symbol, price, balance)
    if qty <= 0:
        return jsonify({"error": "quantity is zero"}), 400

    # Entry order — synchronous
    entry_result = place_entry(symbol, side, qty)
    print(f"[ENTRY] {side} {qty} {symbol} @ ~{price} → {entry_result}")

    # TP/SL — background thread so response returns before TradingView 5s timeout
    if tp_price > 0 or sl_price > 0:
        threading.Thread(target=place_tp_sl, args=(symbol, side, tp_price, sl_price), daemon=True).start()

    return jsonify({
        "status": "ok", "side": side, "symbol": symbol,
        "price": price, "tp": tp_price, "sl": sl_price,
        "qty": qty, "balance": balance,
        "notional": round(balance * MARGIN_PCT * LEVERAGE, 2),
        "entry": entry_result,
    }), 200

# ─── HEALTH ───────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    balance   = get_balance()
    positions = get_open_positions()
    return jsonify({
        "status": "running", "exchange": "Binance USDM Futures",
        "balance": balance, "open_trades": len(positions),
        "max_trades": MAX_OPEN_TRADES, "leverage": LEVERAGE,
        "margin_pct": MARGIN_PCT,
        "positions": [p.get("symbol") for p in positions],
    }), 200

# ─── DEBUG ────────────────────────────────────────────────────────────────────
@app.route("/debug", methods=["GET"])
def debug():
    results = {}
    for label, path in [("balance", "/fapi/v3/balance"), ("account", "/fapi/v3/account"), ("positions", "/fapi/v3/positionRisk")]:
        try:
            results[label] = b_get(path)
        except Exception as e:
            results[label] = {"exception": str(e)}
    return jsonify(results), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
