# Hermes — Polymarket Copy-Trading Research Bot (paper only)

Self-improving copy-trading **research** bot. Scans the Polymarket leaderboard,
scores wallets, watches their new trades, paper-trades the good ones ($5–$20
simulated), reviews outcomes, and **rewrites its own rules** — every change
versioned with evidence. Dashboard is a static page on GitHub Pages.

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
   trade, unfiltered) for comparison.
4. **review** — after ≥1h each decision is judged (good copy / bad copy /
   missed winner / avoided loser) and **rules auto-update** on evidence
   (≥3 samples per bucket): tighten `max_spread` when spread-heavy copies lose,
   raise `min_liquidity`, lower/raise the copy-score gate, downgrade wallets with
   bad paper performance. Every change: before/after/reason/evidence/new version.
5. **report / export** — daily report + `docs/data.json` for the dashboard.

## Run
```
python3 bot.py scan      # leaderboard + wallet scoring (heavy, ~1 min)
python3 bot.py cycle     # monitor + pnl + review + report + export (rescans if >6h old)
python3 bot.py loop 900  # cycle forever, every 15 min
python3 test_bot.py      # self-checks incl. rule-learning and read-only safety
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
