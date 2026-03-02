import time
import hmac
import hashlib
import base64
import json
import math
import os
import uuid
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─── CONFIG — set these as environment variables in Railway ───────────────────
API_KEY          = os.environ.get("WEEX_API_KEY",      "")
SECRET_KEY       = os.environ.get("WEEX_SECRET_KEY",   "")
PASSPHRASE       = os.environ.get("WEEX_PASSPHRASE",   "")   # passphrase you chose when creating API key
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET",    "")   # any random string — same one in TradingView alert

ACCOUNT_EQUITY   = float(os.environ.get("ACCOUNT_EQUITY",   "500"))
MARGIN_PCT       = float(os.environ.get("MARGIN_PCT",        "0.20"))
LEVERAGE         = int(os.environ.get("LEVERAGE",            "10"))
MAX_OPEN_TRADES  = int(os.environ.get("MAX_OPEN_TRADES",     "3"))

BASE_URL = "https://api-contract.weex.com"

# TradingView ticker → WEEX futures symbol
SYMBOL_MAP = {
    "SPX6USD":     "cmt_spx6900usdt",
    "BIOUSDT":     "cmt_biousdt",
    "API3USD":     "cmt_api3usdt",
    "PENGUUSDT":   "cmt_penguusdt",
    "VIRTUALUSDT": "cmt_virtualusdt",
    "AEROUSD":     "cmt_aerousdt",
}

# ─── SIGNATURE ────────────────────────────────────────────────────────────────
# WEEX format: BASE64( HMAC-SHA256( timestamp + METHOD + path + body ) )

def sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    message = timestamp + method.upper() + path + body
    digest  = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def make_headers(method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY":        API_KEY,
        "ACCESS-SIGN":       sign(ts, method, path, body),
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "en-US",
    }

def weex_post(path: str, body: dict) -> dict:
    body_str = json.dumps(body)
    r = requests.post(
        BASE_URL + path,
        headers=make_headers("POST", path, body_str),
        data=body_str,
        timeout=10
    )
    return r.json()

def weex_get(path: str) -> dict:
    r = requests.get(
        BASE_URL + path,
        headers=make_headers("GET", path),
        timeout=10
    )
    return r.json()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_open_positions() -> list:
    try:
        resp = weex_get("/capi/v2/mix/position/allPosition")
        return [p for p in resp.get("data", []) if float(p.get("total", 0)) != 0]
    except Exception as e:
        print(f"get_positions error: {e}")
        return []

def set_leverage_isolated(symbol: str):
    try:
        weex_post("/capi/v2/account/setLeverage", {
            "symbol": symbol, "marginCoin": "USDT", "leverage": str(LEVERAGE)
        })
        weex_post("/capi/v2/account/setMarginMode", {
            "symbol": symbol, "marginCoin": "USDT", "marginMode": "isolated"
        })
    except Exception as e:
        print(f"set_leverage error: {e}")

def place_order(symbol: str, side: str, size: float) -> dict:
    # type: 1=open long, 2=open short
    order_type = "1" if side == "BUY" else "2"
    body = {
        "symbol":      symbol,
        "client_oid":  uuid.uuid4().hex[:32],
        "size":        str(size),
        "type":        order_type,
        "order_type":  "0",   # normal
        "match_price": "1",   # market
        "price":       "0",   # ignored for market
        "marginMode":  3,     # 3 = isolated
    }
    return weex_post("/capi/v2/order/placeOrder", body)

def calc_size(price: float) -> float:
    # notional = equity × margin% × leverage
    notional = ACCOUNT_EQUITY * MARGIN_PCT * LEVERAGE
    return math.floor((notional / price) * 10) / 10  # 1 decimal place

# ─── WEBHOOK ──────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "no data"}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    side   = data.get("side", "").upper()
    ticker = data.get("symbol", "")
    price  = float(data.get("price", 0))

    if side not in ("BUY", "SELL") or not ticker or not price:
        return jsonify({"error": "invalid payload"}), 400

    symbol = SYMBOL_MAP.get(ticker)
    if not symbol:
        return jsonify({"error": f"unknown ticker: {ticker}"}), 400

    # Enforce max concurrent trades
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_OPEN_TRADES:
        print(f"Skipped {side} {symbol} — {len(open_positions)}/{MAX_OPEN_TRADES} open")
        return jsonify({"status": "skipped", "reason": "max_open_trades"}), 200

    # Skip if already in this symbol
    if any(p.get("symbol") == symbol for p in open_positions):
        print(f"Skipped — already in {symbol}")
        return jsonify({"status": "skipped", "reason": "already_open"}), 200

    # Set leverage and isolated margin
    set_leverage_isolated(symbol)

    # Calculate size
    size = calc_size(price)
    if size <= 0:
        return jsonify({"error": "size too small"}), 400

    result = place_order(symbol, side, size)
    print(f"ORDER: {side} {size} {symbol} @ {price} → {result}")

    return jsonify({
        "status": "ok", "side": side,
        "symbol": symbol, "size": size,
        "price": price, "result": result
    }), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running", "open": len(get_open_positions())}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
