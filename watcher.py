# Builds26 Crypto Auto - Position Watcher (v3.0)
#
# REST polling replacement for the v2.x WebSocket watcher.
# Binance Futures WSS endpoints silently drop traffic from Render IPs:
# the handshake completes but no market data is delivered, so positions
# never close. This version polls fapi.binance.com over HTTPS instead.
# Trade-off: roughly 3s latency vs roughly 1s. Irrelevant for 1H paper trading.
#
# Required env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

import os
import time
import logging
from datetime import datetime, timezone

import requests
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

# Binance Futures REST endpoint - returns mark price for all symbols.
BINANCE_PREMIUM_INDEX = "https://fapi.binance.com/fapi/v1/premiumIndex"

# Polling cadence in seconds. 3 is well inside Binance rate limits.
POLL_INTERVAL = 3

# How often to refresh the open positions list from Supabase, in seconds.
RESYNC_INTERVAL = 30

# How many consecutive HTTP failures before we log loudly.
ERROR_LOG_THRESHOLD = 3


def get_open_positions():
    res = sb.table("positions").select("*").execute()
    return res.data or []


def get_account():
    res = sb.table("account").select("*").eq("id", 1).execute()
    return res.data[0] if res.data else None


def fetch_mark_prices():
    """Returns dict mapping symbol to mark price (float), or None on failure."""
    try:
        r = requests.get(BINANCE_PREMIUM_INDEX, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {row["symbol"]: float(row["markPrice"]) for row in data}
    except requests.exceptions.RequestException as e:
        log.warning(f"fetch_mark_prices: HTTP error: {e}")
        return None
    except (ValueError, KeyError) as e:
        log.error(f"fetch_mark_prices: parse error: {e}")
        return None


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

    tag = "OK" if result == "win" else "LOSS" if result == "loss" else "BE"
    side_upper = pos["side"].upper()
    sym = pos["symbol"]
    pnl_sign = "+" if pnl >= 0 else ""
    r_sign = "+" if r_mult >= 0 else ""
    log.info(
        f"[{tag}] CLOSE {side_upper} {sym} @ {exit_price:.6f} | "
        f"{pnl_sign}${pnl:.2f} ({r_sign}{r_mult:.2f}R) [{reason}]"
    )
    notify.notify_close(
        pos["symbol"], pos["side"], pos["entry"], exit_price,
        pnl, r_mult, result, reason,
    )


def evaluate_position(pos, mark_price):
    """Returns True if the position was closed, else False."""
    side = pos["side"]
    sl = float(pos["sl"])
    tp = float(pos["tp"])

    if side == "long":
        if mark_price <= sl:
            close_position(pos, sl, "sl")
            return True
        elif mark_price >= tp:
            close_position(pos, tp, "tp")
            return True
    else:
        if mark_price >= sl:
            close_position(pos, sl, "sl")
            return True
        elif mark_price <= tp:
            close_position(pos, tp, "tp")
            return True

    return False


def run():
    log.info("=" * 60)
    log.info("Crypto auto watcher v3.0 - Binance Futures REST polling")
    log.info(f"poll every {POLL_INTERVAL}s | resync positions every {RESYNC_INTERVAL}s")
    log.info("=" * 60)

    positions = []
    last_resync = 0.0
    consecutive_errors = 0

    while True:
        loop_start = time.time()

        if loop_start - last_resync >= RESYNC_INTERVAL:
            try:
                positions = get_open_positions()
                last_resync = loop_start
                symbols = sorted(p["symbol"] for p in positions)
                log.info(f"resync: {len(positions)} open position(s): {symbols}")
            except Exception as e:
                log.error(f"resync: get_open_positions failed: {e}")

        if not positions:
            time.sleep(POLL_INTERVAL)
            continue

        prices = fetch_mark_prices()
        if prices is None:
            consecutive_errors += 1
            if consecutive_errors >= ERROR_LOG_THRESHOLD:
                log.error(
                    f"fetch_mark_prices failed {consecutive_errors} times in a row"
                )
            time.sleep(POLL_INTERVAL)
            continue
        consecutive_errors = 0

        closed_any = False
        for pos in list(positions):
            symbol = pos["symbol"]
            mark = prices.get(symbol)
            if mark is None:
                log.warning(f"no mark price returned for {symbol}")
                continue
            try:
                if evaluate_position(pos, mark):
                    closed_any = True
            except Exception as e:
                log.error(f"evaluate_position {symbol} failed: {e}")

        if closed_any:
            last_resync = 0.0

        elapsed = time.time() - loop_start
        sleep_for = max(0.0, POLL_INTERVAL - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    while True:
        try:
            run()
        except KeyboardInterrupt:
            log.info("watcher stopped by user")
            break
        except Exception as e:
            log.error(f"run() crashed: {e}")
            log.info("restarting in 5s...")
            time.sleep(5)
