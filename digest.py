"""
Daily digest sender — runs once a day at 08:00 UTC on Render cron.
Pulls account state, open positions, and trades from the last 24h, then sends
a summary message via Telegram.
"""

import os
import logging
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client

import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("digest")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def run():
    log.info("Daily digest starting")

    account_res = sb.table("account").select("*").eq("id", 1).execute()
    if not account_res.data:
        log.error("No account row")
        return
    account = account_res.data[0]

    open_res = sb.table("positions").select("*").execute()
    open_positions = open_res.data or []

    # Trades resolved in the last 24 hours
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    today_res = sb.table("trades").select("*").gte("closed_at", cutoff).execute()
    trades_today = today_res.data or []

    # All-time totals (capped at 1000 for sanity)
    all_res = sb.table("trades").select("*").limit(1000).execute()
    all_trades = all_res.data or []

    starting = float(account["starting_balance"])
    realised = float(account["realised_pnl"])
    equity = starting + realised

    if all_trades:
        wins = sum(1 for t in all_trades if t["result"] == "win")
        total = len(all_trades)
        win_rate = (wins / total) * 100 if total else 0
        expectancy = sum(float(t["r"]) for t in all_trades) / total if total else 0
    else:
        win_rate = 0
        expectancy = 0

    notify.notify_digest(
        equity=equity,
        starting=starting,
        realised=realised,
        open_positions=open_positions,
        trades_today=trades_today,
        total_trades=len(all_trades),
        win_rate=win_rate,
        expectancy=expectancy,
    )

    log.info("Digest sent")


if __name__ == "__main__":
    run()
