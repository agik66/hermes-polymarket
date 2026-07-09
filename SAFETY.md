# SAFETY

**Version one is paper trading only, by design.**

- The bot performs **read-only HTTP GETs** against public Polymarket APIs.
  There is no order-placement code, no wallet code, no signing code — enforced
  by a test (`test_bot.py` fails if signing/key/order tokens appear in source).
- **Private keys must never be stored here.** Any future execution layer belongs
  in a separate, audited component with its own key management — never in a
  research bot that auto-rewrites its own rules.
- **Autonomy later, maybe:** only after months of paper results prove an edge
  net of spread and slippage, and only with human-approved rule changes and
  hard position/loss limits.

## Why copy trading is dangerous
- **Leaderboards mislead**: survivorship bias, one lucky trade, huge bankrolls
  taking -EV variance you can't, wash-trading and points farming.
- **You are always late**: by the time a trade is visible, price has moved.
  The bot measures this drift and skips late entries — real fills would be worse.
- **Liquidity and spread eat the edge**: paper fills at midpoint are optimistic;
  real fills cross the spread and move thin books.
- **Stale data**: any API hiccup means decisions on old prices. The bot surfaces
  errors instead of faking data, but staleness risk never fully disappears.
- **Resolution risk**: markets can resolve ambiguously or be disputed.

Nothing here is financial advice. This is a research instrument.
