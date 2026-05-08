"""
Builds26 Crypto Auto — Position Watcher (v2.1)
================================================
Long-running process that connects to Binance Futures WebSocket and monitors
open paper positions in real-time. The moment any open position's mark price
crosses its SL or TP, the watcher closes it in Supabase and fires a Telegram
alert — within ~1 second of the actual touch.

v2.1 fixes a race condition where the sync_loop would run before the WebSocket
finished connecting, cache the position list as "subscribed", and then never
actually subscribe to the price feeds. Symptoms: positions opened, never closed,
even when price clearly crossed SL/TP.

Fix: subscription state is reset to empty on every (re)connect, and on_open
unconditionally subscribes to all currently-open positions. The sync_loop
remains as the periodic catch-up mechanism for newly-opened positions.

Runs as a Render Background Worker (continuous, $7/month).
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone

import websocket
from supabase import create_client, Client

import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("watcher")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

WS_BASE = "wss://fstream.binance.com/stream"
RESYNC_INTERVAL = 30
RECONNECT_DELAY = 5

_state = {
    "positions": {},
    "ws": None,
    "subscribed": set(),
    "ws_ready": False,
    "lock": threading.Lock(),
}


def get_open_positions():
    res = sb.table("positions").select("*").execute()
    return res.data or []


def get_account():
    res = sb.table("account").select("*").eq("id", 1).execute()
    return res.data[0] if res.data else None


def close_position(pos, exit_price, reason):
    direction = 1 if pos["side"] == "long" else -1
    pnl = (exit_price - float(pos["entry"])) * float(pos["qty"]) * direction
    r_mult = pnl / float(pos["risk_usd"])

    if reason == "tp":
        result = "win"
    elif reason == "sl":
        result = "loss"
    else:
        result = "win" if pnl > 0 else ("loss" if pnl < 0 else "be")

    sb.table("trades").insert({
        "symbol":        pos["symbol"],
        "side":          pos["side"],
        "entry":         pos["entry"],
        "exit":          exit_price,
        "sl":            pos["sl"],
        "tp":            pos["tp"],
        "qty":           pos["qty"],
        "risk_usd":      pos["risk_usd"],
        "pnl":           pnl,
        "r":             r_mult,
        "result":        result,
        "close_reason":  reason,
        "signal_reason": pos.get("signal_reason"),
        "opened_at":     pos["opened_at"],
    }).execute()

    sb.table("positions").delete().eq("id", pos["id"]).execute()

    account = get_account()
    new_realised = float(account["realised_pnl"]) + pnl
    sb.table("account").update({"realised_pnl": new_realised}).eq("id", 1).execute()

    tag = "✅" if result == "win" else "❌" if result == "loss" else "⚖️"
    log.info(
        f"{tag} CLOSE {pos['side'].upper()} {pos['symbol']} @ {exit_price:.6f} · "
        f"{'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if r_mult >= 0 else ''}{r_mult:.2f}R) [{reason}]"
    )
    notify.notify_close(
        pos["symbol"], pos["side"], pos["entry"], exit_price,
        pnl, r_mult, result, reason,
    )


def on_tick(symbol, price):
    with _state["lock"]:
        pos = _state["positions"].get(symbol)
        if not pos:
            return
        pos = dict(pos)

    side = pos["side"]
    sl = float(pos["sl"])
    tp = float(pos["tp"])

    if side == "long":
        if price <= sl:
            close_position(pos, sl, "sl")
            _drop_position(symbol)
        elif price >= tp:
            close_position(pos, tp, "tp")
            _drop_position(symbol)
    else:
        if price >= sl:
            close_position(pos, sl, "sl")
            _drop_position(symbol)
        elif price <= tp:
            close_position(pos, tp, "tp")
            _drop_position(symbol)


def _drop_position(symbol):
    with _state["lock"]:
        _state["positions"].pop(symbol, None)


def _ws_connected(ws):
    return bool(ws and ws.sock and ws.sock.connected)


def sync_subscriptions():
    if not _state.get("ws_ready"):
        return

    try:
        positions = get_open_positions()
    except Exception as e:
        log.error(f"sync: get_open_positions failed: {e}")
        return

    new_map = {p["symbol"]: p for p in positions}
    new_symbols = set(new_map.keys())

    with _state["lock"]:
        old_symbols = _state["subscribed"].copy()
        _state["positions"] = new_map

    to_add = new_symbols - old_symbols
    to_remove = old_symbols - new_symbols

    if not (to_add or to_remove):
        return

    ws = _state["ws"]
    if not _ws_connected(ws):
        log.warning(f"sync: WS disconnected, will resync on reconnect ({len(new_symbols)} positions)")
        return

    if to_add:
        sub_msg = {
            "method": "SUBSCRIBE",
            "params": [f"{s.lower()}@markPrice@1s" for s in to_add],
            "id": int(time.time()),
        }
        try:
            ws.send(json.dumps(sub_msg))
            with _state["lock"]:
                _state["subscribed"].update(to_add)
            log.info(f"sync: subscribed to {sorted(to_add)}")
        except Exception as e:
            log.error(f"sync: subscribe send failed: {e}")
            return

    if to_remove:
        unsub_msg = {
            "method": "UNSUBSCRIBE",
            "params": [f"{s.lower()}@markPrice@1s" for s in to_remove],
            "id": int(time.time()) + 1,
        }
        try:
            ws.send(json.dumps(unsub_msg))
            with _state["lock"]:
                _state["subscribed"].difference_update(to_remove)
            log.info(f"sync: unsubscribed from {sorted(to_remove)}")
        except Exception as e:
            log.error(f"sync: unsubscribe send failed: {e}")


def sync_loop():
    while True:
        time.sleep(RESYNC_INTERVAL)
        try:
            sync_subscriptions()
        except Exception as e:
            log.error(f"sync_loop error: {e}")


def on_message(ws, message):
    try:
        data = json.loads(message)
        if isinstance(data, dict) and "result" in data and "id" in data:
            return
        payload = data.get("data") or data
        if not isinstance(payload, dict):
            return
        symbol = payload.get("s")
        price_str = payload.get("p")
        if symbol and price_str:
            on_tick(symbol, float(price_str))
    except Exception as e:
        log.error(f"on_message error: {e}")


def on_open(ws):
    log.info("WebSocket connected — establishing subscriptions")

    with _state["lock"]:
        _state["subscribed"] = set()
        _state["ws_ready"] = False

    try:
        positions = get_open_positions()
    except Exception as e:
        log.error(f"on_open: get_open_positions failed: {e}")
        positions = []

    new_map = {p["symbol"]: p for p in positions}
    symbols = set(new_map.keys())

    with _state["lock"]:
        _state["positions"] = new_map

    if symbols:
        sub_msg = {
            "method": "SUBSCRIBE",
            "params": [f"{s.lower()}@markPrice@1s" for s in symbols],
            "id": 1,
        }
        try:
            ws.send(json.dumps(sub_msg))
            with _state["lock"]:
                _state["subscribed"] = symbols
                _state["ws_ready"] = True
            log.info(f"initial subscribe: {sorted(symbols)} ({len(symbols)} symbol(s))")
        except Exception as e:
            log.error(f"on_open: subscribe send failed: {e}")
    else:
        with _state["lock"]:
            _state["ws_ready"] = True
        log.info("no open positions on connect — watching for new positions")


def on_error(ws, err):
    log.error(f"WebSocket error: {err}")


def on_close(ws, code, msg):
    log.warning(f"WebSocket closed: code={code} msg={msg}")
    with _state["lock"]:
        _state["ws_ready"] = False
        _state["subscribed"] = set()


def run():
    log.info("=" * 60)
    log.info("Crypto auto watcher (v2.1) — Binance Futures WebSocket")
    log.info(f"resync every {RESYNC_INTERVAL}s · reconnect after {RECONNECT_DELAY}s on drop")

    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()

    url = f"{WS_BASE}?streams="

    while True:
        try:
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            _state["ws"] = ws
            ws.run_forever(ping_interval=180, ping_timeout=10)
        except Exception as e:
            log.error(f"run_forever crashed: {e}")
        log.info(f"reconnecting in {RECONNECT_DELAY}s...")
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    run()
