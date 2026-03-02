import time
import hmac
import hashlib
import base64
import json
import math
import os
import uuid
import requests
from urllib.parse import urlencode
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─── CONFIG — set all of these as environment variables in Railway ─────────────
API_KEY         = os.environ.get("WEEX_API_KEY",    "")
SECRET_KEY      = os.environ.get("WEEX_SECRET_KEY", "")
PASSPHRASE      = os.environ.get("WEEX_PASSPHRASE", "")
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET",  "")

MARGIN_PCT      = float(os.environ.get("MARGIN_PCT",      "0.20"))
LEVERAGE        = int(os.environ.get("LEVERAGE",          "10"))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES",   "5"))

BASE_URL = "https://api-contract.weex.com"

# ─── SYMBOL MAP — TradingView ticker → WEEX futures symbol ───────────────────
SYMBOL_MAP = {
    "GUNUSD":      "cmt_gunusdt",
    "PIPPINUSD":   "cmt_pippinusdt",
    "RIVERUSD":    "cmt_riverusdt",
    "VVVUSD":      "cmt_vvvusdt",
    "MOGUSD":      "cmt_mogusdt",
    "USUALUSD":    "cmt_usualusdt",
    "BROCCOLIUSD": "cmt_broccoliusdt",
    "SPX6USD":     "cmt_spx6900usdt",
    "RESOLVUSD":   "cmt_resolveusdt",
    "API3USD":     "cmt_api3usdt",
    "BIOUSDT":     "cmt_biousdt",
    "THEUSDT":     "cmt_theusdt",
    "PENGUUSDT":   "cmt_penguusdt",
    "AEROUSDT":    "cmt_aerousdt",
    "PUMPUSDT":    "cmt_pumpusdt",
}

# ─── SIGNATURE ────────────────────────────────────────────────────────────────
# WEEX format: BASE64( HMAC-SHA256( timestamp + METHOD + path + query_string + body ) )
# query_string includes the "?" prefix when present, body is "" for GET

def _sign(timestamp: str, method: str, path: str, query_string: str = "", body: str = "") -> str:
    message = timestamp + method.upper() + path + query_string + body
    digest  = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def _headers(method: str, path: str, query_string: str = "", body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY":        API_KEY,
        "ACCESS-SIGN":       _sign(ts, method, path, query_string, body),
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "en-US",
    }

def weex_get(path: str, params: dict = None) -> dict:
    query_string = ("?" + urlencode(params)) if params else ""
    url = BASE_URL + path + query_string
    r = requests.get(url, headers=_headers("GET", path, query_string), timeout=10)
    return r.json()

def weex_post(path: str, body: dict) -> dict:
    body_str = json.dumps(body)
    r = requests.post(
        BASE_URL + path,
        headers=_headers("POST", path, "", body_str),
        data=body_str,
        timeout=10
    )
    return r.json()

# ─── ACCOUNT EQUITY ───────────────────────────────────────────────────────────
def get_account_equity() -> float:
    """
    GET /capi/v2/account/getAccounts
    Returns the USDT collateral `amount` (available balance).
    """
    try:
        resp = weex_get("/capi/v2/account/getAccounts")
        collateral = resp.get("collateral", [])
        for item in collateral:
            if item.get("coin", "").upper() == "USDT":
                equity = float(item.get("amount", 0))
                print(f"[ACCOUNT] Live equity: ${equity:.2f}")
                return equity
        print(f"[ACCOUNT] USDT collateral not found. Full response: {resp}")
        return 0.0
    except Exception as e:
        print(f"[ACCOUNT] Error: {e}")
        return 0.0

# ─── OPEN POSITIONS ───────────────────────────────────────────────────────────
def get_open_positions() -> list:
    """
    GET /capi/v2/account/getAccounts → account.modeSetting gives us symbols with positions.
    But for actual position list we use the positions endpoint.
    """
    try:
        resp = weex_get("/capi/v2/mix/position/allPosition")
        data = resp.get("data", [])
        if not isinstance(data, list):
            print(f"[POSITIONS] Unexpected response: {resp}")
            return []
        open_pos = [p for p in data if float(p.get("total", 0)) != 0]
        return open_pos
    except Exception as e:
        print(f"[POSITIONS] Error: {e}")
        return []

# ─── LEVERAGE & MARGIN MODE ───────────────────────────────────────────────────
def set_leverage_isolated(symbol: str):
    try:
        r1 = weex_post("/capi/v2/account/setLeverage", {
            "symbol":   symbol,
            "leverage": str(LEVERAGE),
            "holdSide": "long_short"
        })
        r2 = weex_post("/capi/v2/account/setMarginMode", {
            "symbol":     symbol,
            "marginMode": "isolated"
        })
        print(f"[LEVERAGE] {symbol} {LEVERAGE}x isolated | {r1.get('msg')} | {r2.get('msg')}")
    except Exception as e:
        print(f"[LEVERAGE] Error: {e}")

# ─── PLACE ORDER ──────────────────────────────────────────────────────────────
def place_order(symbol: str, side: str, size: float,
                tp_price: float = None, sl_price: float = None) -> dict:
    """
    POST /capi/v2/order/placeOrder
    type: 1=open long, 2=open short, 3=close long, 4=close short
    order_type: 0=Normal
    match_price: 1=Market
    """
    order_type_code = "1" if side == "BUY" else "2"   # 1=open long, 2=open short

    body = {
        "symbol":      symbol,
        "client_oid":  uuid.uuid4().hex[:32],
        "size":        str(size),
        "type":        order_type_code,
        "order_type":  "0",   # Normal
        "match_price": "1",   # Market execution
        "price":       "0",   # Ignored for market orders
        "marginMode":  3,     # 3 = Isolated
    }

    if tp_price and tp_price > 0:
        body["presetTakeProfitPrice"] = str(round(tp_price, 8))
    if sl_price and sl_price > 0:
        body["presetStopLossPrice"] = str(round(sl_price, 8))

    return weex_post("/capi/v2/order/placeOrder", body)

# ─── POSITION SIZE ────────────────────────────────────────────────────────────
def calc_size(price: float, equity: float) -> float:
    """notional = equity × margin% × leverage  →  qty = notional / price"""
    if price <= 0 or equity <= 0:
        return 0.0
    notional = equity * MARGIN_PCT * LEVERAGE
    return math.floor((notional / price) * 10) / 10   # round down to 1 decimal

# ─── WEBHOOK ──────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "empty body"}), 400

    # Auth via URL query param: /webhook?secret=YOUR_SECRET
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

    # Live equity
    equity = get_account_equity()
    if equity <= 0:
        return jsonify({"error": "could not fetch account equity"}), 500

    # Max open trades guard
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_OPEN_TRADES:
        print(f"[SKIP] Max trades reached: {len(open_positions)}/{MAX_OPEN_TRADES}")
        return jsonify({"status": "skipped", "reason": "max_open_trades"}), 200

    # No duplicate in same symbol
    open_symbols = [p.get("symbol") for p in open_positions]
    if symbol in open_symbols:
        print(f"[SKIP] Already in {symbol}")
        return jsonify({"status": "skipped", "reason": "already_open", "symbol": symbol}), 200

    # Set leverage + isolated
    set_leverage_isolated(symbol)

    # Calculate size
    size = calc_size(price, equity)
    if size <= 0:
        return jsonify({"error": "calculated size is zero"}), 400

    # Place order
    result = place_order(symbol, side, size, tp_price=tp_price, sl_price=sl_price)

    log = {
        "status":   "ok",
        "side":     side,
        "symbol":   symbol,
        "price":    price,
        "tp":       tp_price,
        "sl":       sl_price,
        "size":     size,
        "equity":   equity,
        "notional": round(equity * MARGIN_PCT * LEVERAGE, 2),
        "result":   result,
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
        "positions":   [p.get("symbol") for p in positions],
    }), 200

# ─── DEBUG (remove after confirming working) ──────────────────────────────────
@app.route("/debug", methods=["GET"])
def debug():
    """Raw WEEX account response — use to verify API connectivity."""
    try:
        resp = weex_get("/capi/v2/account/getAccounts")
        return jsonify({"raw": resp}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
