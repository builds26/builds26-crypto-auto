# “””
Builds26 Crypto Auto - Position Watcher (v3.0)

Long-running process that polls Binance Futures REST API for mark prices
of open paper positions. The moment any open position’s mark price crosses
its SL or TP, the watcher closes it in Supabase and fires a Telegram alert

- within ~3 seconds of the actual touch.

v3.0 replaces the WebSocket-based monitoring of v2.x. Binance Futures WSS
endpoints (fstream.binance.com) silently drop traffic from Render’s
outbound IP range: TCP/WS handshake completes, but no market data is
delivered. v2.x looked healthy in logs (subscriptions sent, acks received)
but on_message was never triggered, so positions never closed even when
mark price clearly crossed SL/TP.

Fix: poll https://fapi.binance.com/fapi/v1/premiumIndex every POLL_INTERVAL
seconds. One request returns mark prices for ALL futures symbols. Standard
HTTPS is not subject to the same IP filtering as WSS. Trade-off: ~3s
latency vs ~1s, which is irrelevant for 1H-candle paper trading.

Runs as a Render Background Worker (continuous, $7/month).

Environment variables required:

- SUPABASE_URL
- SUPABASE_SERVICE_KEY
- TELEGRAM_BOT_TOKEN (read by notify module)
- TELEGRAM_CHAT_ID (read by notify module)
  “””

import os
import time
import logging
from datetime import datetime, timezone

import requests
from supabase import create_client, Client

import notify

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(message)s”,
)
log = logging.getLogger(“watcher”)

SUPABASE_URL = os.environ[“SUPABASE_URL”]
SUPABASE_KEY = os.environ[“SUPABASE_SERVICE_KEY”]
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Binance Futures REST endpoint - returns mark price for all symbols.

BINANCE_PREMIUM_INDEX = “https://fapi.binance.com/fapi/v1/premiumIndex”

# Polling cadence. 3s gives sub-candle responsiveness while staying well

# inside Binance’s rate limits (premiumIndex with no symbol is weight 10,

# limit 2400/min - so 3s = 200 calls/min = weight 2000/min, safe).

POLL_INTERVAL = 3

# How often to refresh the open positions list from Supabase. Positions

# rarely change (worker opens trades on 1H candle close), so 30s is plenty.

RESYNC_INTERVAL = 30

# How many consecutive HTTP failures before we log loudly. Transient

# network blips happen; we don’t want to spam Telegram or logs over a

# single 500.

ERROR_LOG_THRESHOLD = 3

def get_open_positions():
“”“Fetch all currently-open paper positions from Supabase.”””
res = sb.table(“positions”).select(”*”).execute()
return res.data or []

def get_account():
“”“Fetch the single account row (id=1).”””
res = sb.table(“account”).select(”*”).eq(“id”, 1).execute()
return res.data[0] if res.data else None

def fetch_mark_prices():
“””
Fetch mark prices for all Binance Futures symbols in one request.
Returns dict mapping symbol (e.g. ‘ADAUSDT’) -> mark price (float).
Returns None on failure.
“””
try:
r = requests.get(BINANCE_PREMIUM_INDEX, timeout=10)
r.raise_for_status()
data = r.json()
return {row[“symbol”]: float(row[“markPrice”]) for row in data}
except requests.exceptions.RequestException as e:
log.warning(f”fetch_mark_prices: HTTP error: {e}”)
return None
except (ValueError, KeyError) as e:
log.error(f”fetch_mark_prices: parse error: {e}”)
return None

def close_position(pos, exit_price, reason):
“””
Close a position: insert a trade row, delete the position row, update
realised PnL on the account, log the close, and send a Telegram alert.
“””
direction = 1 if pos[“side”] == “long” else -1
pnl = (exit_price - float(pos[“entry”])) * float(pos[“qty”]) * direction
r_mult = pnl / float(pos[“risk_usd”])

```
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
log.info(
    f"[{tag}] CLOSE {pos['side'].upper()} {pos['symbol']} @ {exit_price:.6f} | "
    f"{'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if r_mult >= 0 else ''}{r_mult:.2f}R) [{reason}]"
)
notify.notify_close(
    pos["symbol"], pos["side"], pos["entry"], exit_price,
    pnl, r_mult, result, reason,
)
```

def evaluate_position(pos, mark_price):
“””
Check whether the current mark price has crossed SL or TP for this
position. If it has, close at the level (TP or SL), not at the live
mark - this matches the original v2.x behaviour and gives clean,
backtest-comparable trade records.

```
Returns True if the position was closed, else False.
"""
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
else:  # short
    if mark_price >= sl:
        close_position(pos, sl, "sl")
        return True
    elif mark_price <= tp:
        close_position(pos, tp, "tp")
        return True

return False
```

def run():
“”“Main polling loop.”””
log.info(”=” * 60)
log.info(“Crypto auto watcher (v3.0) - Binance Futures REST polling”)
log.info(f”poll every {POLL_INTERVAL}s | resync positions every {RESYNC_INTERVAL}s”)
log.info(”=” * 60)

```
positions = []
last_resync = 0.0
consecutive_errors = 0

while True:
    loop_start = time.time()

    # Refresh open positions from Supabase periodically.
    if loop_start - last_resync >= RESYNC_INTERVAL:
        try:
            positions = get_open_positions()
            last_resync = loop_start
            symbols = sorted(p["symbol"] for p in positions)
            log.info(f"resync: {len(positions)} open position(s): {symbols}")
        except Exception as e:
            log.error(f"resync: get_open_positions failed: {e}")

    # If no positions, just sleep and try again next cycle.
    if not positions:
        time.sleep(POLL_INTERVAL)
        continue

    # Fetch latest mark prices for all symbols (one HTTP request).
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

    # Evaluate each open position against its current mark price.
    # Iterate over a copy so close_position can mutate the underlying
    # list via the next resync cycle without affecting this loop.
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

    # If anything closed, force an immediate resync next cycle so the
    # in-memory positions list reflects the database.
    if closed_any:
        last_resync = 0.0

    # Sleep for the remainder of the poll interval (so we poll at a
    # steady cadence regardless of how long the iteration took).
    elapsed = time.time() - loop_start
    sleep_for = max(0.0, POLL_INTERVAL - elapsed)
    time.sleep(sleep_for)
```

if **name** == “**main**”:
while True:
try:
run()
except KeyboardInterrupt:
log.info(“watcher stopped by user”)
break
except Exception as e:
log.error(f”run() crashed: {e}”)
log.info(“restarting in 5s…”)
time.sleep(5)
