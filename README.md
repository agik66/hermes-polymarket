# Hermes — Polymarket Copy-Trading Research Bot (paper only)

Self-improving copy-trading **research** bot. Scans the Polymarket leaderboard,
scores wallets, watches their new trades, paper-trades the good ones (simulated
stakes, 1–2% of paper equity), reviews every decision against the market's
**final resolution**, and learns from **realized, cost-inclusive results** —
never from short-term price drift, which is noise (see AUDIT.md P0). Every rule
change is versioned with evidence. Dashboard is a static page on GitHub Pages.

## What it does NOT do
No real trades. No private keys. No signing. No money. Read-only GETs against
Polymarket public APIs. See [SAFETY.md](SAFETY.md). Not financial advice.

## How it works
1. **scan** — pulls 500 wallets from `data-api/v1/leaderboard` (30d window), deep-profiles top 50
   (positions + REDEEM activity + trades), scores ROI / consistency / copyability,
   applies a one-hit-wonder penalty, tracks the top ≤15 above the score gate.
2. **monitor** — polls tracked wallets' trades; each new BUY is scored
   (wallet quality, entry drift, spread, liquidity, time-to-resolution, conviction)
   → `paper_copy` / `watchlist` / `skip`, with reasons. A SELL by the wallet closes
   our matching paper position. Guards: entry price band 0.05–0.92, one open
   position per market, |price drift| cap.
3. **pnl** — hourly mark-to-market via CLOB midpoints; resolves via gamma
   `outcomePrices`; also tracks a **blind-copy benchmark** ($10 on every observed
   trade, unfiltered) for comparison. Paper fills are **realistic**: BUY at the
   ask, SELL at the bid, +1c penalty on thin books — not at the midpoint.
4. **review** — each decision is judged against the market's **final resolution**
   (good copy / bad copy / skipped winner / skipped loser), then `learn_rules`
   adjusts thresholds (copy gate, sizing, spread/liquidity gates) from
   **realized PnL of resolved copies only** — net of costs, never from price
   drift. Anti-overfitting guardrails: ≥20 resolved samples, per-key evidence
   recency (stale rows never re-trigger), split-half sign agreement, bounded
   steps, hard bounds, 7-day per-key cooldown. Wallets with ≥5 resolved copies
   and negative mean realized return are downgraded. Risk guards: 1–2% of
   equity per trade, daily −5% kill-switch, max 2 open positions per category.
5. **report / export** — daily report + `docs/data.json` for the dashboard.

## Backtest
`python3 backtest.py` — out-of-sample walk-forward test of copying leaderboard
wallets (selection on in-sample only, evaluation on a later window, cost
scenarios +0/1/2c). Results in `backtest_results.json`; biases and their
direction are documented in the module docstring. Current verdict: **no edge
after costs** — see AUDIT.md.

## Run
```
python3 bot.py scan      # leaderboard + wallet scoring (heavy, ~1 min)
python3 bot.py cycle     # monitor + pnl + review + report + export (rescans if >6h old)
python3 bot.py loop 900  # cycle forever, every 15 min
python3 test_bot.py      # self-checks incl. resolution reviews, fills, read-only safety
```
No dependencies — Python 3.9+ stdlib only. State in `hermes.db` (sqlite).

Publish an update: `python3 bot.py cycle && git add docs && git commit -m data && git push`.

## Dashboard
`docs/index.html` + `docs/data.json`, served by GitHub Pages. Answers:
are we profitable on paper, which wallets are worth copying, what did the bot
learn today (rule changes + outcome reviews). Tabs: Wallet Rankings, Trade
Signals, Paper Trades, Decision Journal, Rules (with full change history), Reports.

## Env vars
None required. Optional Telegram delivery of the daily report is not wired —
the report is stored in the DB and shown on the dashboard.
