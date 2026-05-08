# crypto-auto

Server-side paper-trade automation for Builds26 Signal Desk.

- **Worker** (`worker.py`): runs on Render cron every 15min. Scans Binance 1H candles, applies the strategy, opens/closes paper positions in Supabase.
- **Dashboard** (`index.html`): read-only view, deploys to Netlify. Reads from Supabase via the public anon key.
- **Storage** (Supabase): three tables — `account`, `positions`, `trades`.

This is **paper trading**. No exchange API keys, no real money, no live orders.

---

## Strategy (1H candles, crypto-tuned)

- **Trend filter:** EMA20 vs EMA50
- **Momentum:** MACD (12/26/9) — bull/bear cross or histogram direction
- **Oscillator:** RSI 14, kept inside 40–70 long / 30–60 short to avoid late entries
- **Volume confirm:** current bar volume > 1.2x 20-bar average
- **Risk:** 1% of equity per trade, position sized so SL distance × qty = risk
- **SL / TP:** ATR(14) based — default 1.5x ATR stop, 3.0x ATR target (2:1 R:R)

All multipliers configurable via Render env vars without code changes.

---

## Deploy

### 1. Supabase
Project: `crypto-auto`. Tables: `account`, `positions`, `trades`. Anon key hardcoded in `index.html`.

### 2. Render (worker)
- New Cron Job → connect this repo
- `render.yaml` is auto-detected
- Set env vars in Render dashboard:
  - `SUPABASE_URL` = your Supabase project URL
  - `SUPABASE_SERVICE_KEY` = service_role key from Supabase API settings (secret — never commit)
- Trigger first run manually to test

### 3. Netlify (dashboard)
- Import this repo
- No build command, publish dir `.`
- Optional: custom domain `crypto.builds26.com`

---

## Tweaking risk parameters

In Render env vars — change, save, next scan picks them up.

| Var              | Default | Meaning                              |
|------------------|---------|--------------------------------------|
| `RISK_PCT`       | 1.0     | % of equity risked per trade         |
| `LEVERAGE`       | 10      | informational only in paper mode     |
| `MAX_CONCURRENT` | 3       | max positions open at once           |
| `ATR_SL_MULT`    | 1.5     | SL distance = ATR × this             |
| `ATR_TP_MULT`    | 3.0     | TP distance = ATR × this             |

---

## Pause / resume / reset

In Supabase SQL editor:

```sql
-- Pause (worker still manages open positions, but won't open new)
update account set is_running = false where id = 1;

-- Resume
update account set is_running = true where id = 1;

-- Reset paper account
truncate positions, trades restart identity;
update account set realised_pnl = 0, last_scan_at = null where id = 1;
