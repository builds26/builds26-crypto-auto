"""
Builds26 Crypto Auto — Paper Trade Worker
==========================================
Runs on Render cron every 15min. Scans Binance 1H candles for the configured
coin list, applies the EMA20/50 + RSI14 + MACD + ATR + volume strategy, opens
paper positions in Supabase, and closes any positions that hit SL/TP.

This is paper trading. No exchange API calls. No real money.
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from supabase import create_client, Client

# ---------- Config ----------
COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "ADAUSDT", "AVAXUSDT", "DOGEUSDT",
    "LINKUSDT", "MATICUSDT", "DOTUSDT", "LTCUSDT",
]

# Risk / sizing — read from env so Render env vars can override without redeploy
RISK_PCT       = float(os.getenv("RISK_PCT",       "1.0"))
LEVERAGE       = float(os.getenv("LEVERAGE",       "10"))
MAX_CONCURRENT = int(  os.getenv("MAX_CONCURRENT", "3"))
ATR_SL_MULT    = float(os.getenv("ATR_SL_MULT",    "1.5"))
ATR_TP_MULT    = float(os.getenv("ATR_TP_MULT",    "3.0"))

BINANCE_BASE = "https://api.binance.com/api/v3"

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("worker")

# ---------- Supabase ----------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ===========================================================================
# Indicators
# ===========================================================================
def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def macd(closes):
    if len(closes) < 35:
        return None
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    macd_line = [a - b for a, b in zip(e12, e26)]
    sig = ema(macd_line[-200:], 9)
    return {
        "macd":      macd_line[-1],
        "signal":    sig[-1],
        "hist":      macd_line[-1] - sig[-1],
        "bull_cross": macd_line[-2] <= sig[-2] and macd_line[-1] > sig[-1],
        "bear_cross": macd_line[-2] >= sig[-2] and macd_line[-1] < sig[-1],
        "above":      macd_line[-1] > sig[-1],
    }


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    return a


def avg_volume(candles, n=20):
    vols = [c["volume"] for c in candles[-n:]]
    return sum(vols) / len(vols)


# ===========================================================================
# Data
# ===========================================================================
def fetch_klines(symbol, interval="1h", limit=200):
    r = requests.get(
        f"{BINANCE_BASE}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return [
        {
            "time":   k[0],
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        }
        for k in r.json()
    ]


def fetch_price(symbol):
    r = requests.get(
        f"{BINANCE_BASE}/ticker/price",
        params={"symbol": symbol},
        timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["price"])


# ===========================================================================
# Strategy
# ===========================================================================
def analyse(candles):
    closes = [c["close"] for c in candles]
    last = candles[-1]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    rsi_now = rsi(closes, 14)
    m = macd(closes)
    atr_now = atr(candles, 14)
    vol_ratio = last["volume"] / avg_volume(candles, 20)

    if rsi_now is None or m is None or atr_now is None:
        return None

    trend = "up" if e20 > e50 else "down"
    signal = "flat"
    reasons = []

    if (
        trend == "up"
        and 40 <= rsi_now <= 70
        and (m["bull_cross"] or m["hist"] > 0)
        and vol_ratio > 1.2
        and last["close"] > e20
    ):
        signal = "long"
        reasons.append("MACD bull cross" if m["bull_cross"] else "MACD bullish")
        reasons.append(f"RSI {rsi_now:.0f}")
        reasons.append(f"vol {vol_ratio:.1f}x")
    elif (
        trend == "down"
        and 30 <= rsi_now <= 60
        and (m["bear_cross"] or m["hist"] < 0)
        and vol_ratio > 1.2
        and last["close"] < e20
    ):
        signal = "short"
        reasons.append("MACD bear cross" if m["bear_cross"] else "MACD bearish")
        reasons.append(f"RSI {rsi_now:.0f}")
        reasons.append(f"vol {vol_ratio:.1f}x")

    return {
        "price":   last["close"],
        "rsi":     rsi_now,
        "atr":     atr_now,
        "trend":   trend,
        "signal":  signal,
        "reasons": " · ".join(reasons),
    }


# ===========================================================================
# Account / Positions
# ===========================================================================
def get_account():
    res = sb.table("account").select("*").eq("id", 1).execute()
    return res.data[0] if res.data else None


def update_account(**fields):
    sb.table("account").update(fields).eq("id", 1).execute()


def equity(account):
    return float(account["starting_balance"]) + float(account["realised_pnl"])


def get_open_positions():
    res = sb.table("positions").select("*").execute()
    return res.data or []


def open_position(symbol, a, account):
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_CONCURRENT:
        log.info(f"skip {symbol}: at max concurrent ({MAX_CONCURRENT})")
        return
    if any(p["symbol"] == symbol for p in open_positions):
        log.info(f"skip {symbol}: already open")
        return

    eq = equity(account)
    risk_usd = eq * (RISK_PCT / 100)
    sl_dist = a["atr"] * ATR_SL_MULT
    tp_dist = a["atr"] * ATR_TP_MULT
    entry = a["price"]
    sl = entry - sl_dist if a["signal"] == "long" else entry + sl_dist
    tp = entry + tp_dist if a["signal"] == "long" else entry - tp_dist
    qty = risk_usd / sl_dist

    sb.table("positions").insert({
        "symbol":        symbol,
        "side":          a["signal"],
        "entry":         entry,
        "sl":            sl,
        "tp":            tp,
        "qty":           qty,
        "risk_usd":      risk_usd,
        "signal_reason": a["reasons"],
    }).execute()

    log.info(
        f"OPEN {a['signal'].upper()} {symbol} @ {entry:.4f} · "
        f"SL {sl:.4f} · TP {tp:.4f} · risk ${risk_usd:.2f} · {a['reasons']}"
    )


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
    update_account(realised_pnl=new_realised)

    tag = "✅" if result == "win" else "❌" if result == "loss" else "⚖️"
    log.info(
        f"{tag} CLOSE {pos['side'].upper()} {pos['symbol']} @ {exit_price:.4f} · "
        f"{'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if r_mult >= 0 else ''}{r_mult:.2f}R) [{reason}]"
    )


def check_open_positions():
    open_positions = get_open_positions()
    for pos in open_positions:
        try:
            price = fetch_price(pos["symbol"])
            entry, sl, tp = float(pos["entry"]), float(pos["sl"]), float(pos["tp"])
            if pos["side"] == "long":
                if price <= sl:
                    close_position(pos, sl, "sl")
                elif price >= tp:
                    close_position(pos, tp, "tp")
            else:  # short
                if price >= sl:
                    close_position(pos, sl, "sl")
                elif price <= tp:
                    close_position(pos, tp, "tp")
        except Exception as e:
            log.error(f"price check failed {pos['symbol']}: {e}")


# ===========================================================================
# Main
# ===========================================================================
def run():
    log.info("=" * 60)
    log.info("Crypto auto worker — paper trade scan")
    log.info(
        f"risk={RISK_PCT}% lev={LEVERAGE}x max_concurrent={MAX_CONCURRENT} "
        f"atr_sl={ATR_SL_MULT} atr_tp={ATR_TP_MULT}"
    )

    account = get_account()
    if not account:
        log.error("No account row found — did you run the SQL setup?")
        return
    if not account.get("is_running", True):
        log.info("Account is_running=false, skipping scan (resume in dashboard/SQL)")
        return

    log.info(f"Equity: ${equity(account):.2f} (start ${float(account['starting_balance']):.2f}, "
             f"realised {'+' if float(account['realised_pnl']) >= 0 else ''}${float(account['realised_pnl']):.2f})")

    # 1. Check & close any open positions that hit SL/TP
    check_open_positions()

    # 2. Refresh account after potential closes
    account = get_account()

    # 3. Scan all coins for new entries
    for symbol in COINS:
        try:
            candles = fetch_klines(symbol, "1h", 200)
            a = analyse(candles)
            if a is None:
                continue
            if a["signal"] != "flat":
                log.info(f"{symbol} → {a['signal'].upper()} ({a['reasons']})")
                open_position(symbol, a, account)
                # Refresh account after opening a position so concurrent-limit math stays right
                account = get_account()
            else:
                log.info(f"{symbol} flat (RSI {a['rsi']:.0f}, trend {a['trend']})")
        except Exception as e:
            log.error(f"scan {symbol} failed: {e}")
        time.sleep(0.15)  # be gentle on Binance

    update_account(last_scan_at=datetime.now(timezone.utc).isoformat())
    log.info("Scan complete")


if __name__ == "__main__":
    run()
