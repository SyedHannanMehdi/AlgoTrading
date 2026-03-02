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

# ─── CONFIG — set all of these as environment variables in Railway ────────────
API_KEY         = os.environ.get("WEEX_API_KEY",     "")
SECRET_KEY      = os.environ.get("WEEX_SECRET_KEY",  "")
PASSPHRASE      = os.environ.get("WEEX_PASSPHRASE",  "")
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET",   "")   # same string you paste in TradingView alert

MARGIN_PCT      = float(os.environ.get("MARGIN_PCT",      "0.20"))   # 20% of equity per trade
LEVERAGE        = int(os.environ.get("LEVERAGE",          "10"))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES",   "5"))

BASE_URL = "https://api-contract.weex.com"

# ─── SYMBOL MAP — TradingView ticker → WEEX futures contract ─────────────────
# Add/remove pairs here as needed. All 15 coins from the backtest:
SYMBOL_MAP = {
    # Crypto.com tickers → WEEX
    "GUNUSD":       "cmt_gunusdt",
    "PIPPINUSD":    "cmt_pippinusdt",
    "RIVERUSD":     "cmt_riverusdt",
    "VVVUSD":       "cmt_vvvusdt",
    "MOGUSD":       "cmt_mogusdt",
    "USUALUSD":     "cmt_usualusdt",
    "BROCCOLIUSD":  "cmt_broccoliusdt",
    "SPX6USD":      "cmt_spx6900usdt",
    "RESOLVUSD":    "cmt_resolveusdt",
    "API3USD":      "cmt_api3usdt",
    # Binance tickers (already USDT pairs)
    "BIOUSDT":      "cmt_biousdt",
    "THEUSDT":      "cmt_theusdt",
    "PENGUUSDT":    "cmt_penguusdt",
    "AEROUSDT":     "cmt_aerousdt",
    "PUMPUSDT":     "cmt_pumpusdt",
}

# ─── SIGNATURE ────────────────────────────────────────────────────────────────
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

def weex_get(path: str, params: dict = None) -> dict:
    from urllib.parse import urlencode
    query = ("?" + urlencode(params)) if params else ""
    path_with_query = path + query
    r = requests.get(
        BASE_URL + path_with_query,
        headers=make_headers("GET", path_with_query),
        timeout=10
    )
    return r.json()

# ─── ACCOUNT ──────────────────────────────────────────────────────────────────
def get_account_equity() -> float:
    """
    Fetch live USDT futures wallet equity from WEEX.
    Returns available equity (usdtEquity). Falls back to 0 on error.
    """
    try:
        resp = weex_get("/capi/v2/account/accountAssets", {"productType": "umcbl"})
        assets = resp.get("data", [])
        for asset in assets:
            if asset.get("marginCoin", "").upper() == "USDT":
                equity = float(asset.get("usdtEquity", 0))
                print(f"[ACCOUNT] Live equity: ${equity:.2f}")
                return equity
        print(f"[ACCOUNT] USDT asset not found in response: {resp}")
        return 0.0
    except Exception as e:
        print(f"[ACCOUNT] Error fetching equity: {e}")
        return 0.0

# ─── POSITIONS ────────────────────────────────────────────────────────────────
def get_open_positions() -> list:
    try:
        resp = weex_get("/capi/v2/mix/position/allPosition", {"productType": "umcbl"})
        return [p for p in resp.get("data", []) if float(p.get("total", 0)) != 0]
    except Exception as e:
        print(f"[POSITIONS] Error: {e}")
        return []

# ─── LEVERAGE & MARGIN MODE ───────────────────────────────────────────────────
def set_leverage_isolated(symbol: str):
    try:
        r1 = weex_post("/capi/v2/account/setLeverage", {
            "symbol":     symbol,
            "marginCoin": "USDT",
            "leverage":   str(LEVERAGE),
            "holdSide":   "long_short"
        })
        r2 = weex_post("/capi/v2/account/setMarginMode", {
            "symbol":     symbol,
            "marginCoin": "USDT",
            "marginMode": "isolated"
        })
        print(f"[LEVERAGE] {symbol} → {LEVERAGE}x isolated | lev:{r1.get('msg')} margin:{r2.get('msg')}")
    except Exception as e:
        print(f"[LEVERAGE] Error for {symbol}: {e}")

# ─── ORDER PLACEMENT ──────────────────────────────────────────────────────────
def place_order(symbol: str, side: str, size: float,
                tp_price: float = None, sl_price: float = None) -> dict:
    """
    Places a market order with optional TP and SL attached.
    side: 'BUY' (long) or 'SELL' (short)
    """
    order_type = "open_long" if side == "BUY" else "open_short"

    body = {
        "symbol":      symbol,
        "marginCoin":  "USDT",
        "clientOid":   uuid.uuid4().hex[:32],
        "size":        str(size),
        "side":        order_type,
        "orderType":   "market",
        "marginMode":  "isolated",
    }

    # Attach TP/SL if provided
    preset_tps = []
    preset_sls = []

    if tp_price and tp_price > 0:
        preset_tps.append({
            "presetStopSurplusPrice": str(round(tp_price, 8)),
            "executePrice": "0",          # market exit
            "triggerType":  "fill_price"
        })

    if sl_price and sl_price > 0:
        preset_sls.append({
            "presetStopLossPrice": str(round(sl_price, 8)),
            "executePrice": "0",          # market exit
            "triggerType":  "fill_price"
        })

    if preset_tps:
        body["presetTakeProfitPrice"] = preset_tps[0]["presetStopSurplusPrice"]
    if preset_sls:
        body["presetStopLossPrice"]   = preset_sls[0]["presetStopLossPrice"]

    return weex_post("/capi/v2/order/placeOrder", body)

# ─── SIZE CALCULATION ─────────────────────────────────────────────────────────
def calc_size(price: float, equity: float) -> float:
    """
    notional = equity × margin% × leverage
    qty      = notional / price   (1 decimal place)
    """
    if price <= 0 or equity <= 0:
        return 0.0
    notional = equity * MARGIN_PCT * LEVERAGE
    raw_qty  = notional / price
    # Round down to 1 decimal (conservative, avoids over-sizing)
    return math.floor(raw_qty * 10) / 10

# ─── WEBHOOK HANDLER ──────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "empty body"}), 400

    # ── Auth
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    # ── Parse payload
    side     = data.get("side", "").upper()          # BUY or SELL
    ticker   = data.get("symbol", "")
    price    = float(data.get("price", 0))
    sl_price = float(data.get("sl", 0))              # stop loss price
    tp_price = float(data.get("tp", 0))              # take profit price

    if side not in ("BUY", "SELL"):
        return jsonify({"error": "side must be BUY or SELL"}), 400
    if not ticker or price <= 0:
        return jsonify({"error": "missing symbol or price"}), 400

    symbol = SYMBOL_MAP.get(ticker)
    if not symbol:
        return jsonify({"error": f"unknown ticker: {ticker}"}), 400

    # ── Live account equity
    equity = get_account_equity()
    if equity <= 0:
        return jsonify({"error": "could not fetch account equity"}), 500

    # ── Check max open trades
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_OPEN_TRADES:
        msg = f"Skipped {side} {symbol} — {len(open_positions)}/{MAX_OPEN_TRADES} trades open"
        print(f"[SKIP] {msg}")
        return jsonify({"status": "skipped", "reason": "max_open_trades", "open": len(open_positions)}), 200

    # ── Skip duplicate position in same symbol
    if any(p.get("symbol") == symbol for p in open_positions):
        print(f"[SKIP] Already in {symbol}")
        return jsonify({"status": "skipped", "reason": "already_open", "symbol": symbol}), 200

    # ── Set leverage + isolated mode
    set_leverage_isolated(symbol)

    # ── Size
    size = calc_size(price, equity)
    if size <= 0:
        return jsonify({"error": "calculated size is zero — check equity/price"}), 400

    # ── Place order
    result = place_order(symbol, side, size, tp_price=tp_price, sl_price=sl_price)

    log = {
        "status":   "ok",
        "side":     side,
        "symbol":   symbol,
        "price":    price,
        "sl":       sl_price,
        "tp":       tp_price,
        "size":     size,
        "equity":   equity,
        "notional": round(equity * MARGIN_PCT * LEVERAGE, 2),
        "result":   result
    }
    print(f"[ORDER] {json.dumps(log)}")
    return jsonify(log), 200


# ─── HEALTH CHECK ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    equity    = get_account_equity()
    positions = get_open_positions()
    return jsonify({
        "status":      "running",
        "equity":      equity,
        "open_trades": len(positions),
        "max_trades":  MAX_OPEN_TRADES,
        "leverage":    LEVERAGE,
        "margin_pct":  MARGIN_PCT,
        "positions":   [p.get("symbol") for p in positions]
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
