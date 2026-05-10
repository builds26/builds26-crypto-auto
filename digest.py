import os
import logging
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client

import notify

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(message)s”,
)
log = logging.getLogger(“digest”)

SUPABASE_URL = os.environ[“SUPABASE_URL”]
SUPABASE_KEY = os.environ[“SUPABASE_SERVICE_KEY”]
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def save_digest_to_db(digest_date, equity, starting, realised,
open_positions, trades_today, total_trades,
win_rate, expectancy):
wins_t = sum(1 for t in trades_today if t[“result”] == “win”)
losses_t = sum(1 for t in trades_today if t[“result”] == “loss”)
bes_t = sum(1 for t in trades_today if t[“result”] == “be”)
pnl_t = sum(float(t[“pnl”]) for t in trades_today)

```
record = {
    "digest_date": digest_date,
    "equity": equity,
    "starting_balance": starting,
    "realised_pnl": realised,
    "open_positions_count": len(open_positions),
    "trades_24h_count": len(trades_today),
    "trades_24h_wins": wins_t,
    "trades_24h_losses": losses_t,
    "trades_24h_be": bes_t,
    "trades_24h_pnl": pnl_t,
    "total_trades": total_trades,
    "win_rate": win_rate,
    "expectancy": expectancy,
}

try:
    sb.table("daily_digests").upsert(
        record, on_conflict="digest_date"
    ).execute()
    log.info(f"Digest saved to db for {digest_date}")
except Exception as e:
    log.error(f"Failed to save digest to db: {e}")
```

def run():
log.info(“Daily digest starting”)

```
account_res = sb.table("account").select("*").eq("id", 1).execute()
if not account_res.data:
    log.error("No account row")
    return
account = account_res.data[0]

open_res = sb.table("positions").select("*").execute()
open_positions = open_res.data or []

cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
today_res = sb.table("trades").select("*").gte("closed_at", cutoff).execute()
trades_today = today_res.data or []

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

today_utc = datetime.now(timezone.utc).date().isoformat()

save_digest_to_db(
    digest_date=today_utc,
    equity=equity,
    starting=starting,
    realised=realised,
    open_positions=open_positions,
    trades_today=trades_today,
    total_trades=len(all_trades),
    win_rate=win_rate,
    expectancy=expectancy,
)

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
```

if **name** == “**main**”:
run()
