"""
Telegram notifications for crypto-auto.
Sends formatted alerts on position open, position close, and daily digest.
Falls back gracefully if env vars aren't set (so worker still runs without notifications).

v3: notify_open and notify_close now accept an optional ai_explanation parameter
that gets appended to the message after the structured stats.
"""

import os
import logging
import requests

log = logging.getLogger("notify")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")


def _send(text: str):
    """Low-level send. Markdown-formatted, silent fail (logs the error)."""
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Telegram not configured - skipping notification")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not r.ok:
            log.error(f"Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Telegram send exception: {e}")


def fmt_price(p):
    p = float(p)
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.2f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.6f}"


def _format_ai_block(ai_explanation: str) -> str:
    """
    Format the AI explanation for appending to a Telegram message.
    Returns empty string if no explanation given.
    Visual separator + italicised text underneath, with an AI label.
    """
    if not ai_explanation:
        return ""
    # Strip any markdown asterisks Claude might have included, to avoid
    # breaking Telegram's Markdown parser.
    safe = ai_explanation.replace("*", "").replace("_", "")
    return f"\n\n---\n_🤖 Builds26 AI_\n{safe}"


def notify_open(symbol: str, side: str, entry, sl, tp, risk_usd, reasons: str,
                ai_explanation: str = ""):
    """Sent immediately when a paper position opens."""
    arrow = "🟢 LONG" if side == "long" else "🔴 SHORT"
    coin = symbol.replace("USDT", "")
    msg = (
        f"*{arrow} {coin}*\n"
        f"`Entry  ` {fmt_price(entry)}\n"
        f"`SL     ` {fmt_price(sl)}\n"
        f"`TP     ` {fmt_price(tp)}\n"
        f"`Risk   ` ${float(risk_usd):.2f}\n"
        f"\n_{reasons}_"
        f"{_format_ai_block(ai_explanation)}"
    )
    _send(msg)


def notify_close(symbol: str, side: str, entry, exit_price, pnl, r_mult,
                 result: str, reason: str, ai_explanation: str = ""):
    """Sent when a position closes (SL or TP hit)."""
    if result == "win":
        tag = "✅ WIN"
    elif result == "loss":
        tag = "❌ LOSS"
    else:
        tag = "⚖️ BE"

    coin = symbol.replace("USDT", "")
    pnl_f = float(pnl)
    r_f = float(r_mult)
    pnl_s = f"{'+' if pnl_f >= 0 else ''}${pnl_f:.2f}"
    r_s = f"{'+' if r_f >= 0 else ''}{r_f:.2f}R"
    side_label = side.upper()

    msg = (
        f"*{tag} {coin} {side_label}*\n"
        f"`Entry  ` {fmt_price(entry)}\n"
        f"`Exit   ` {fmt_price(exit_price)}  ({reason.upper()})\n"
        f"`P&L    ` {pnl_s}  ({r_s})"
        f"{_format_ai_block(ai_explanation)}"
    )
    _send(msg)


def notify_digest(equity, starting, realised, open_positions, trades_today, total_trades, win_rate, expectancy):
    """Daily 08:00 UTC summary."""
    eq = float(equity)
    start = float(starting)
    real = float(realised)
    pct = ((eq - start) / start) * 100 if start else 0

    pct_s = f"{'+' if pct >= 0 else ''}{pct:.2f}%"
    real_s = f"{'+' if real >= 0 else ''}${real:.2f}"

    lines = [
        "*📊 Daily Digest - Crypto Auto*",
        "",
        f"`Equity   ` ${eq:.2f} ({pct_s})",
        f"`Realised ` {real_s}",
        f"`Open     ` {len(open_positions)} position(s)",
        "",
    ]

    if trades_today:
        wins_t = sum(1 for t in trades_today if t["result"] == "win")
        losses_t = sum(1 for t in trades_today if t["result"] == "loss")
        bes_t = sum(1 for t in trades_today if t["result"] == "be")
        pnl_t = sum(float(t["pnl"]) for t in trades_today)
        pnl_t_s = f"{'+' if pnl_t >= 0 else ''}${pnl_t:.2f}"
        lines.append(f"*Last 24h:* {len(trades_today)} trade(s) - {wins_t}W / {losses_t}L / {bes_t}BE - {pnl_t_s}")
    else:
        lines.append("_No trades resolved in last 24h_")

    if total_trades > 0:
        wr_s = f"{win_rate:.0f}%"
        ex_s = f"{'+' if expectancy >= 0 else ''}{expectancy:.2f}R"
        lines.append("")
        lines.append(f"*All-time:* {total_trades} trades · WR {wr_s} · Expectancy {ex_s}/trade")
    else:
        lines.append("")
        lines.append("_No resolved trades yet - strategy is filtering hard, watching for setups_")

    if open_positions:
        lines.append("")
        lines.append("*Open positions:*")
        for p in open_positions:
            coin = p["symbol"].replace("USDT", "")
            arrow = "🟢" if p["side"] == "long" else "🔴"
            lines.append(f"  {arrow} {coin} {p['side'].upper()} @ {fmt_price(p['entry'])}")

    _send("\n".join(lines))
