"""
Builds26 Crypto Auto - End-Of-Day Summary (v0.3)

Runs once a day on Render cron at 22:00 UTC. Pulls today's resolved trades
and currently-open positions from Supabase, makes a single call to Claude,
and posts a pure-prose 3-5 sentence summary to the existing Telegram channel.

This replaces the per-event Builds26 AI commentary from v0.2 (which failed
in production because Render cron containers cold-start the network
connection on every event-driven Anthropic call). One call per day is far
more likely to complete than ten per hour.

Required env vars:
    SUPABASE_URL, SUPABASE_SERVICE_KEY,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    ANTHROPIC_API_KEY.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client, Client
from anthropic import Anthropic, APIError, APIConnectionError

import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("eod_summary")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Same Anthropic config as ai_explain.py - validated to handle Render's
# cold-start network handshake.
MODEL = "claude-opus-4-7"
MAX_TOKENS = 400
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 1


SYSTEM_PROMPT = """You are writing the end-of-day summary for the Builds26 Signal Desk paper-trading bot. The bot trades 1H candles on Binance USDT-M Futures using an EMA20/EMA50 trend filter, MACD confirmation, RSI 14, 1.2x volume threshold, and ATR-based SL/TP.

Write 3 to 5 sentences of plain prose. No headers, no bullet points, no markdown, no emoji. Describe what happened today: how many trades resolved, whether they won or lost, what is still open, and where equity sits relative to the starting balance.

Tone: matter-of-fact, like a trader's log entry. No celebration on winning days, no apology on losing days. Wins and losses are equally interesting. Never give financial advice. Never recommend the user trade. This is documentation of what the bot did, not a recommendation. Do not invent numbers, indicator values, or trades that are not in the data provided. If no trades resolved today, say so directly."""


def _today_start_utc_iso() -> str:
    """Returns ISO timestamp for 00:00 UTC of the current day."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _fetch_state():
    """Pulls account, today's resolved trades, and open positions from Supabase."""
    account_res = sb.table("account").select("*").eq("id", 1).execute()
    account = account_res.data[0] if account_res.data else None

    cutoff = _today_start_utc_iso()
    trades_res = sb.table("trades").select("*").gte("closed_at", cutoff).execute()
    trades_today = trades_res.data or []

    open_res = sb.table("positions").select("*").execute()
    open_positions = open_res.data or []

    return account, trades_today, open_positions


def _build_user_message(account, trades_today, open_positions) -> str:
    """Construct the context block sent to Claude."""
    today_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    starting = float(account["starting_balance"]) if account else 0.0
    realised = float(account["realised_pnl"]) if account else 0.0
    equity = starting + realised

    lines = [
        f"Date (UTC): {today_label}",
        f"Equity: ${equity:.2f}  (starting ${starting:.2f}, realised PnL ${realised:+.2f})",
        "",
    ]

    if trades_today:
        wins = sum(1 for t in trades_today if t["result"] == "win")
        losses = sum(1 for t in trades_today if t["result"] == "loss")
        bes = sum(1 for t in trades_today if t["result"] == "be")
        pnl_today = sum(float(t["pnl"]) for t in trades_today)
        lines.append(
            f"Trades resolved today: {len(trades_today)} "
            f"({wins}W / {losses}L / {bes}BE, net ${pnl_today:+.2f})"
        )
        for t in trades_today:
            lines.append(
                f"  - {t['symbol']} {t['side']} "
                f"entry {float(t['entry']):.6f} exit {float(t['exit']):.6f} "
                f"pnl ${float(t['pnl']):+.2f} ({float(t['r']):+.2f}R) "
                f"close_reason={t['close_reason']} result={t['result']}"
            )
    else:
        lines.append("Trades resolved today: none")

    lines.append("")
    if open_positions:
        lines.append(f"Currently open positions: {len(open_positions)}")
        for p in open_positions:
            lines.append(
                f"  - {p['symbol']} {p['side']} "
                f"entry {float(p['entry']):.6f} "
                f"sl {float(p['sl']):.6f} tp {float(p['tp']):.6f} "
                f"opened_at={p['opened_at']}"
            )
    else:
        lines.append("Currently open positions: none")

    lines.append("")
    lines.append("Write a 3-5 sentence prose summary of today.")
    return "\n".join(lines)


def _call_claude(user_msg: str) -> Optional[str]:
    """Returns Claude's prose summary, or None on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return None

    client = Anthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        log.info(f"Claude returned {len(text)} chars")
        return text
    except APIConnectionError as e:
        log.error(f"Anthropic connection error after retry: {e}")
        return None
    except APIError as e:
        log.error(f"Anthropic API error: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected Anthropic error: {e}")
        return None


def _fallback_prose(account, trades_today, open_positions) -> str:
    """Plain matter-of-fact prose used when the Claude call fails."""
    starting = float(account["starting_balance"]) if account else 0.0
    realised = float(account["realised_pnl"]) if account else 0.0
    equity = starting + realised

    if trades_today:
        wins = sum(1 for t in trades_today if t["result"] == "win")
        losses = sum(1 for t in trades_today if t["result"] == "loss")
        pnl_today = sum(float(t["pnl"]) for t in trades_today)
        trade_line = (
            f"{len(trades_today)} trade(s) resolved today, "
            f"{wins} winning and {losses} losing, for a net of ${pnl_today:+.2f}."
        )
    else:
        trade_line = "No trades resolved today."

    open_line = (
        f"{len(open_positions)} position(s) remain open into tomorrow."
        if open_positions else "No positions are currently open."
    )

    return (
        f"End-of-day summary, AI commentary unavailable. "
        f"{trade_line} {open_line} "
        f"Equity sits at ${equity:.2f} against a ${starting:.2f} starting balance."
    )


def run():
    log.info("EOD summary starting")

    account, trades_today, open_positions = _fetch_state()
    if not account:
        log.error("No account row found - skipping EOD summary")
        return

    log.info(
        f"State: {len(trades_today)} trade(s) today, "
        f"{len(open_positions)} open position(s)"
    )

    user_msg = _build_user_message(account, trades_today, open_positions)
    summary = _call_claude(user_msg)
    if summary is None:
        summary = _fallback_prose(account, trades_today, open_positions)
        log.warning("Posted fallback EOD summary")
    else:
        # Belt-and-braces: notify._send uses parse_mode=Markdown, so if
        # Claude ever slips a backtick, asterisk, or underscore into the
        # prose it would break Telegram's parser. Mirror the strip that
        # ai_explain._format_ai_block does.
        summary = summary.replace("`", "").replace("*", "").replace("_", "")

    # Reuse notify._send so env-var handling and error logging live in
    # one place. notify.py is intentionally unchanged in v0.3; we don't
    # add a wrapper function for what is essentially a free-form post.
    notify._send(summary)
    log.info("EOD summary sent")


if __name__ == "__main__":
    run()
