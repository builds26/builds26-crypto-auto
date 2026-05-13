"""
Builds26 AI - Trade Explainer (v0.2)

v0.2 changes from v0.1:
- Bumped HTTP timeout to 30s (was SDK default of ~10s)
- Added one automatic retry on connection errors
- Render cron containers cold-start their network connection on every run,
  which can take longer than the default SDK timeout. The retry gives a
  second chance after the connection has been established.

Two entry points:

    explain_signal(signal_data) -> str
        Called by the worker when a new signal fires.
        Returns 3-4 sentence explanation of WHY this signal fired and
        what would invalidate it.

    explain_close(close_data) -> str
        Called by the watcher when a position closes.
        Returns 3-4 sentence summary of the outcome and whether the
        pre-trade thesis played out.

Both functions are fault-tolerant: if the API is unavailable even after
retry, returns a fallback string rather than crashing the caller.

Required env var: ANTHROPIC_API_KEY
"""

import os
import time
import logging

from anthropic import Anthropic, APIError, APIConnectionError

log = logging.getLogger("ai_explain")

# Default model. claude-opus-4-7 is current flagship; if cost becomes
# a concern, swap to claude-sonnet-4-6 (cheaper, still capable).
MODEL = "claude-opus-4-7"

# Hard cap on response length. We want concise.
MAX_TOKENS = 300

# How long to wait for the API to respond before giving up. 30s is generous
# but accounts for Render cron cold-start network handshake.
REQUEST_TIMEOUT_SECONDS = 30.0

# How many retries on connection errors before giving up.
MAX_RETRIES = 1

# Get the API key from environment. Lazy initialisation - we only
# construct the Anthropic client on first use, so import never fails
# even if the env var is missing.
_client = None


def _get_client():
    """Returns a cached Anthropic client, or None if no API key set."""
    global _client
    if _client is not None:
        return _client

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set - AI explanations disabled")
        return None

    _client = Anthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )
    return _client


SIGNAL_SYSTEM_PROMPT = """You are explaining a live crypto trading signal from the Builds26 Signal Desk bot. The bot uses a gut strategy: EMA20 vs EMA50 trend filter, MACD momentum confirmation, RSI 14 (long 40-70, short 30-60), 1.2x volume threshold, ATR-based SL/TP. 1H candles on Binance Futures.

Be concise. Three to four sentences maximum. Structure:
1. Why this signal triggered (specific indicator values)
2. What would invalidate it (specific price level or condition)
3. Optional: how this compares to recent setups, if context is given

Never invent setups or indicator values not in the data provided. Never give financial advice. Never recommend the user trade this. This is documentation of what the bot did, not a trade recommendation.

Tone: matter-of-fact, like a trader's log entry. No emoji. No exclamation marks. No "exciting opportunity" language."""


CLOSE_SYSTEM_PROMPT = """You are explaining the close of a trade from the Builds26 Signal Desk bot. The bot has just closed a position - either at SL, at TP, or manually.

Be concise. Three to four sentences maximum. Structure:
1. What happened (TP/SL hit, PnL in dollars and R-multiple)
2. Why it played out (or didn't): reference price action, indicator context if provided
3. Optional: was the original thesis validated, or did something unexpected happen

Never give financial advice. Never recommend the user trade. This is post-trade documentation, not a recommendation.

Tone: honest and analytical. Wins and losses are equally interesting. No celebration on wins, no apology on losses. If the trade lost, do not soften it. If the trade won, do not gloat."""


def _call_api(system_prompt: str, user_msg: str, label: str) -> str:
    """
    Internal: makes the API call with explicit error handling.
    Returns text on success, fallback string on any failure.
    """
    client = _get_client()
    if client is None:
        return f"(AI {label} unavailable - no API key)"

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        log.info(f"AI {label}: generated {len(text)} chars")
        return text

    except APIConnectionError as e:
        log.error(f"AI {label}: connection error after retry: {e}")
        return f"(AI {label} unavailable - network timeout)"
    except APIError as e:
        log.error(f"AI {label}: API error: {e}")
        return f"(AI {label} unavailable - API error)"
    except Exception as e:
        log.error(f"AI {label}: unexpected error: {e}")
        return f"(AI {label} unavailable)"


def explain_signal(signal_data: dict) -> str:
    """
    Generate an AI explanation for a newly fired signal.

    signal_data should be a dict containing at minimum:
        symbol, side, entry, sl, tp, rsi, trend, macd, volume_ratio, funding_pct

    Returns a string. On API failure, returns a fallback string so the
    caller never crashes.
    """
    user_msg = f"""A new signal just fired:

symbol: {signal_data.get('symbol')}
side: {signal_data.get('side')}
entry: {signal_data.get('entry')}
stop loss: {signal_data.get('sl')}
take profit: {signal_data.get('tp')}
risk amount: ${signal_data.get('risk_usd')}

Indicators at entry:
  RSI 14: {signal_data.get('rsi')}
  EMA trend: {signal_data.get('trend')}
  MACD: {signal_data.get('macd')}
  Volume ratio: {signal_data.get('volume_ratio')}x
  Funding rate: {signal_data.get('funding_pct')}% per 8h

Explain in 3-4 sentences why this signal triggered and what would invalidate it."""

    return _call_api(SIGNAL_SYSTEM_PROMPT, user_msg, "explanation")


def explain_close(close_data: dict) -> str:
    """
    Generate an AI explanation for a position close.

    close_data should be a dict containing at minimum:
        symbol, side, entry, exit, sl, tp, pnl, r_multiple, reason
        (reason: 'tp', 'sl', 'be', 'manual', 'expired')

    Returns a string. On API failure, returns a fallback string.
    """
    user_msg = f"""A position just closed:

symbol: {close_data.get('symbol')}
side: {close_data.get('side')}
entry: {close_data.get('entry')}
exit: {close_data.get('exit')}
SL was: {close_data.get('sl')}
TP was: {close_data.get('tp')}
close reason: {close_data.get('reason')}
PnL: ${close_data.get('pnl')}
R-multiple: {close_data.get('r_multiple')}R

Explain in 3-4 sentences what happened and whether the setup played out as expected."""

    return _call_api(CLOSE_SYSTEM_PROMPT, user_msg, "summary")


# Standalone test mode - run this file directly to test the API connection
# without touching the bot.
if __name__ == "__main__":
    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    load_dotenv()

    print("\n=== Testing explain_signal ===")
    test_signal = {
        "symbol": "BTCUSDT",
        "side": "long",
        "entry": 80606.6,
        "sl": 80214.03,
        "tp": 81391.75,
        "risk_usd": 100,
        "rsi": 58,
        "trend": "up",
        "macd": "bullish cross",
        "volume_ratio": 1.4,
        "funding_pct": 0.0028,
    }
    result = explain_signal(test_signal)
    print(result)

    print("\n=== Testing explain_close (TP hit) ===")
    test_close_win = {
        "symbol": "XRPUSDT",
        "side": "long",
        "entry": 1.4400,
        "exit": 1.4636,
        "sl": 1.4280,
        "tp": 1.4636,
        "reason": "tp",
        "pnl": 200.00,
        "r_multiple": 2.00,
    }
    result = explain_close(test_close_win)
    print(result)

    print("\n=== Done ===")
